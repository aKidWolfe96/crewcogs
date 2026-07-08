"""CrewStats — weekly activity + cross-cog leaderboard for KrustyKrew.

Tracks, per guild, per week:
  * messages sent          (owned by this cog)
  * time spent in voice    (owned by this cog)
  * casino credits wagered + wins   (read from the casino cogs)
  * Pokemon caught / raid wins / new dex entries  (read from PokeBot)
  * Overwatch spray challenges cleared            (read from owtracker)

The casino / pokebot / owtracker cogs all store *lifetime cumulative*
totals, so a "weekly" number is `current_total - baseline`, where the
baseline is snapshotted every time the week resets.  Messages and voice
seconds are owned by this cog and simply zeroed on reset.

Auto-posts a leaderboard embed to a configured channel on a schedule
(default: Mondays 09:00 in the guild's configured timezone) and then
resets for the new week.
"""

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone

import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import humanize_list

try:  # stdlib on 3.9+, present on any modern Red
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

log = logging.getLogger("red.crewstats")

# ---------------------------------------------------------------------------
# External cog Config coordinates (must match the source cogs EXACTLY).
# ---------------------------------------------------------------------------
# Casino games were written with `Config.get_conf(None, identifier=...)`,
# which resolves the cog_name to "NoneType".  We re-open the same namespace.
CASINO_COGNAME = None  # -> type(None).__name__ == "NoneType"
CF_ID = 9876543210   # coinflip:  total_cf_wins / total_cf_losses / total_cf_bet
BJ_ID = 1234567890   # blackjack: total_wins / total_losses / total_bet
SL_ID = 5557771234   # slots:     total_slot_wins / total_slot_losses / total_slot_bet
HR_ID = 7654321098   # horserace: hr_wins / hr_losses / hr_bet / hr_earned

# PokeBot: get_conf(self, ...) with class name "PokéBot" (accented e).
POKE_COGNAME = "PokéBot"
POKE_ID = 0x504F4B45424F54

# owtracker: get_conf(self, ...) with class name "Overwatch".
OW_COGNAME = "Overwatch"
OW_ID = 2026061501

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class CrewStats(commands.Cog):
    """Weekly activity tracking + an auto-posted cross-cog leaderboard."""

    __version__ = "1.2.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=84175392006, force_registration=True)

        # Per member (per guild): the metrics we own outright.
        # pokestop_days holds the ET date strings this member spun this week; the
        # weekly streak is the longest consecutive run within that list.
        self.config.register_member(
            messages=0,
            voice_seconds=0,
            pokestop_days=[],
        )

        # Per guild: settings + the weekly baseline snapshot of external totals.
        self.config.register_guild(
            lb_channel=None,      # channel id to auto-post into
            enabled=True,         # auto-post on/off
            reset_weekday=0,      # 0=Mon .. 6=Sun
            reset_hour=9,         # local hour (0-23) to post & reset
            tz="America/New_York",
            top_n=5,              # entries shown per category
            ping_role=None,       # optional role id to ping with the post
            last_reset=None,      # unix ts of the last reset
            baselines={},         # { "user_id": {casino_bet, casino_wins, poke_caught, ...} }
        )

        # In-memory: unix ts of when each (guild, member) currently in voice joined.
        self._vc_since: dict[tuple[int, int], float] = {}

        self._task = self.bot.loop.create_task(self._scheduler())

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def cog_unload(self):
        if self._task:
            self._task.cancel()
        # Best-effort flush of any live voice timers so time isn't lost on reload.
        self.bot.loop.create_task(self._flush_all_voice())

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        """Remove a user's owned data from every guild (baselines are aggregate)."""
        for guild_id in await self.config.all_members():
            await self.config.member_from_ids(guild_id, user_id).clear()

    # ------------------------------------------------------------------ #
    # Listeners — the data we own
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return
        try:
            await self.config.member(message.author).messages.set(
                await self.config.member(message.author).messages() + 1
            )
        except Exception:  # never let tracking crash message handling
            log.exception("Failed to record message for %s", message.author.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or member.guild is None:
            return
        key = (member.guild.id, member.id)
        afk = member.guild.afk_channel

        def is_counted(channel):
            return channel is not None and channel != afk

        was = is_counted(before.channel)
        now_ = is_counted(after.channel)

        try:
            if not was and now_:            # joined a counted channel
                self._vc_since[key] = datetime.now(timezone.utc).timestamp()
            elif was and not now_:          # left all counted channels
                await self._accrue_voice(member.guild, member.id)
            # counted -> counted (channel move / mute change): timer keeps running
        except Exception:
            log.exception("voice update handling failed for %s", member.id)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context):
        # PokeBot keeps no cumulative spin counter, only lastPokestop (once/day).
        # So we count a spin ourselves when the `pokestop` command actually spins.
        if ctx.guild is None or ctx.command is None:
            return
        if ctx.command.qualified_name != "pokestop":
            return
        member = ctx.author
        if not isinstance(member, discord.Member) or member.bot:
            return
        try:
            today = self._et_today_str()
            last_spin = await self._poke_last_spin(ctx.guild, member.id)
            if last_spin != today:
                return  # command ran but no real spin (already spun / not started)
            mc = self.config.member(member)
            days = await mc.pokestop_days()
            if today in days:
                return  # already recorded this member's spin today
            days.append(today)
            await mc.pokestop_days.set(days)
        except Exception:
            log.exception("pokestop spin tracking failed for %s", member.id)

    # ------------------------------------------------------------------ #
    # Voice accrual helpers
    # ------------------------------------------------------------------ #
    async def _accrue_voice(self, guild: discord.Guild, user_id: int):
        """Add elapsed time for a member and stop their timer."""
        key = (guild.id, user_id)
        since = self._vc_since.pop(key, None)
        if since is None:
            return
        elapsed = int(datetime.now(timezone.utc).timestamp() - since)
        if elapsed <= 0:
            return
        member_conf = self.config.member_from_ids(guild.id, user_id)
        await member_conf.voice_seconds.set(await member_conf.voice_seconds() + elapsed)

    async def _flush_live_voice(self, guild: discord.Guild):
        """Persist current voice time for everyone still in voice, keep them running."""
        now_ts = datetime.now(timezone.utc).timestamp()
        for (gid, uid), since in list(self._vc_since.items()):
            if gid != guild.id:
                continue
            elapsed = int(now_ts - since)
            if elapsed > 0:
                mc = self.config.member_from_ids(gid, uid)
                await mc.voice_seconds.set(await mc.voice_seconds() + elapsed)
            self._vc_since[(gid, uid)] = now_ts  # keep counting from now

    async def _flush_all_voice(self):
        for (gid, uid) in list(self._vc_since.keys()):
            guild = self.bot.get_guild(gid)
            if guild:
                with contextlib.suppress(Exception):
                    await self._accrue_voice(guild, uid)

    def _seed_voice_timers(self):
        """On load, start timers for anyone already sitting in a voice channel."""
        now_ts = datetime.now(timezone.utc).timestamp()
        for guild in self.bot.guilds:
            afk = guild.afk_channel
            for channel in guild.voice_channels:
                if channel == afk:
                    continue
                for member in channel.members:
                    if not member.bot:
                        self._vc_since[(guild.id, member.id)] = now_ts

    # ------------------------------------------------------------------ #
    # Reading external cog totals (bulk, defensive)
    # ------------------------------------------------------------------ #
    async def _casino_totals(self) -> dict[int, dict]:
        """{uid: {'bet': int, 'wins': int}} summed across all casino games."""
        out: dict[int, dict] = {}

        async def add(conf_id, bet_key, win_key):
            try:
                data = await Config.get_conf(
                    None, identifier=conf_id, cog_name=CASINO_COGNAME
                ).all_users()
            except Exception:
                return
            for uid, d in data.items():
                uid = int(uid)
                slot = out.setdefault(uid, {"bet": 0, "wins": 0})
                slot["bet"] += int(d.get(bet_key, 0) or 0)
                slot["wins"] += int(d.get(win_key, 0) or 0)

        await add(CF_ID, "total_cf_bet", "total_cf_wins")
        await add(BJ_ID, "total_bet", "total_wins")
        await add(SL_ID, "total_slot_bet", "total_slot_wins")
        await add(HR_ID, "hr_bet", "hr_wins")
        return out

    async def _poke_totals(self, guild: discord.Guild) -> dict[int, dict]:
        """{uid: {'caught', 'wins', 'dex'}} lifetime totals from PokeBot."""
        out: dict[int, dict] = {}
        try:
            data = await Config.get_conf(
                None, identifier=POKE_ID, cog_name=POKE_COGNAME
            ).all_members(guild)
        except Exception:
            return out
        for uid, d in data.items():
            out[int(uid)] = {
                "caught": len(d.get("pokemon", []) or []),
                "wins": int(d.get("wins", 0) or 0),
                "dex": len(d.get("caughtDex", []) or []),
            }
        return out

    async def _poke_last_spin(self, guild: discord.Guild, uid: int):
        """Read a single member's lastPokestop date string from PokeBot."""
        try:
            conf = Config.get_conf(None, identifier=POKE_ID, cog_name=POKE_COGNAME)
            data = await conf.member_from_ids(guild.id, uid).all()
            return data.get("lastPokestop")
        except Exception:
            return None

    def _et_today_str(self) -> str:
        """Today's date in America/New_York, matching PokeBot's reset boundary."""
        return datetime.now(self._tzinfo("America/New_York")).strftime("%Y-%m-%d")

    @staticmethod
    def _longest_streak(date_strs: list) -> int:
        """Longest run of consecutive calendar dates in a list of 'YYYY-MM-DD'."""
        if not date_strs:
            return 0
        dates = sorted(
            {datetime.strptime(d, "%Y-%m-%d").date() for d in date_strs if d}
        )
        if not dates:
            return 0
        best = run = 1
        for prev, cur in zip(dates, dates[1:]):
            run = run + 1 if (cur - prev).days == 1 else 1
            best = max(best, run)
        return best

    async def _ow_totals(self, guild: discord.Guild) -> dict[int, int]:
        """{uid: cleared_challenge_count} from owtracker."""
        out: dict[int, int] = {}
        try:
            data = await Config.get_conf(
                None, identifier=OW_ID, cog_name=OW_COGNAME
            ).all_members(guild)
        except Exception:
            return out
        for uid, d in data.items():
            out[int(uid)] = len(d.get("done", []) or [])
        return out

    async def _current_externals(self, guild: discord.Guild) -> dict[int, dict]:
        """Merge all external lifetime totals into one {uid: metrics} map."""
        casino = await self._casino_totals()
        poke = await self._poke_totals(guild)
        ow = await self._ow_totals(guild)

        uids = set(casino) | set(poke) | set(ow)
        merged: dict[int, dict] = {}
        for uid in uids:
            c = casino.get(uid, {})
            p = poke.get(uid, {})
            merged[uid] = {
                "casino_bet": int(c.get("bet", 0)),
                "casino_wins": int(c.get("wins", 0)),
                "poke_caught": int(p.get("caught", 0)),
                "poke_wins": int(p.get("wins", 0)),
                "poke_dex": int(p.get("dex", 0)),
                "ow_done": int(ow.get(uid, 0)),
            }
        return merged

    # ------------------------------------------------------------------ #
    # Standings assembly
    # ------------------------------------------------------------------ #
    async def _gather_standings(self, guild: discord.Guild) -> dict[int, dict]:
        """Return {uid: {metric: weekly_value}} for every eligible member."""
        await self._flush_live_voice(guild)

        owned = await self.config.all_members(guild)          # our messages/voice
        externals = await self._current_externals(guild)      # lifetime totals now
        baselines = await self.config.guild(guild).baselines()

        uids = set(owned) | set(externals)
        standings: dict[int, dict] = {}
        for uid in uids:
            uid = int(uid)
            member = guild.get_member(uid)
            if member is None or member.bot:
                continue

            o = owned.get(uid, {}) or owned.get(str(uid), {}) or {}
            cur = externals.get(uid, {})
            base = baselines.get(str(uid), {})

            def delta(key):
                return max(0, int(cur.get(key, 0)) - int(base.get(key, cur.get(key, 0))))

            standings[uid] = {
                "name": member.display_name,
                "messages": int(o.get("messages", 0) or 0),
                "voice_seconds": int(o.get("voice_seconds", 0) or 0),
                "pokestop_streak": self._longest_streak(o.get("pokestop_days", []) or []),
                "casino_bet": delta("casino_bet"),
                "casino_wins": delta("casino_wins"),
                "poke_caught": delta("poke_caught"),
                "poke_wins": delta("poke_wins"),
                "poke_dex": delta("poke_dex"),
                "ow_done": delta("ow_done"),
            }
        return standings

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fmt_duration(seconds: int) -> str:
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, _ = divmod(rem, 60)
        if h and m:
            return f"{h}h {m}m"
        if h:
            return f"{h}h"
        return f"{m}m"

    @staticmethod
    def _medal(rank: int) -> str:
        return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"`#{rank}`")

    def _section(self, standings, key, top_n, value_fmt, extra=None):
        rows = [(d["name"], d[key], d) for d in standings.values() if d[key] > 0]
        if not rows:
            return None
        rows.sort(key=lambda r: r[1], reverse=True)
        lines = []
        for i, (name, val, d) in enumerate(rows[:top_n], 1):
            suffix = f" {extra(d)}" if extra else ""
            lines.append(f"{self._medal(i)} **{name}** — {value_fmt(val)}{suffix}")
        return "\n".join(lines)

    async def _build_embed(self, guild: discord.Guild, standings: dict) -> discord.Embed:
        gconf = self.config.guild(guild)
        top_n = await gconf.top_n()
        last_reset = await gconf.last_reset()

        start = (
            datetime.fromtimestamp(last_reset, tz=timezone.utc) if last_reset else None
        )
        now = datetime.now(timezone.utc)

        embed = discord.Embed(
            title="📊 KrustyKrew Weekly Leaderboard",
            color=0xF4900C,
        )
        if start:
            embed.description = (
                f"Activity from <t:{int(start.timestamp())}:d> to <t:{int(now.timestamp())}:d>"
            )

        sections = [
            ("💬 Most Messages", "messages", lambda v: f"{v:,} msgs", None),
            ("🔊 Most Time in Voice", "voice_seconds",
             lambda v: self._fmt_duration(v), None),
            ("🎰 Casino — Credits Wagered", "casino_bet",
             lambda v: f"{v:,} 💰",
             lambda d: f"({d['casino_wins']}W)" if d["casino_wins"] else ""),
            ("🔴 Pokébot — Pokémon Caught", "poke_caught",
             lambda v: f"{v} caught",
             lambda d: f"· {d['poke_wins']} raid W · {d['poke_dex']} new dex"
                       if (d["poke_wins"] or d["poke_dex"]) else ""),
            ("🗺️ Pokébot — Longest Pokéstop Streak", "pokestop_streak",
             lambda v: f"{v} day{'s' if v != 1 else ''}", None),
            ("🎯 Overwatch — Challenges Cleared", "ow_done",
             lambda v: f"{v} cleared", None),
        ]

        any_section = False
        for title, key, vfmt, extra in sections:
            body = self._section(standings, key, top_n, vfmt, extra)
            if body:
                embed.add_field(name=title, value=body, inline=False)
                any_section = True

        if not any_section:
            embed.description = (
                (embed.description + "\n\n") if embed.description else ""
            ) + "No activity recorded this week yet. Get in there! 🦀"

        wd = WEEKDAYS[await gconf.reset_weekday()]
        hr = await gconf.reset_hour()
        embed.set_footer(text=f"Resets {wd} at {hr:02d}:00 · CrewStats v{self.__version__}")
        return embed

    # ------------------------------------------------------------------ #
    # Reset / post cycle
    # ------------------------------------------------------------------ #
    async def _snapshot_baselines(self, guild: discord.Guild):
        """Set baselines = current lifetime external totals for all members."""
        externals = await self._current_externals(guild)
        await self.config.guild(guild).baselines.set(
            {str(uid): m for uid, m in externals.items()}
        )

    async def _reset_owned(self, guild: discord.Guild):
        """Zero messages + voice_seconds, and restart live voice timers from now."""
        all_members = await self.config.all_members(guild)
        for uid in all_members:
            mc = self.config.member_from_ids(guild.id, int(uid))
            await mc.messages.set(0)
            await mc.voice_seconds.set(0)
            await mc.pokestop_days.set([])
        now_ts = datetime.now(timezone.utc).timestamp()
        for k in list(self._vc_since.keys()):
            if k[0] == guild.id:
                self._vc_since[k] = now_ts

    async def _run_cycle(self, guild: discord.Guild, *, post: bool) -> bool:
        """Optionally post the board, then snapshot baselines & reset. Returns posted?."""
        gconf = self.config.guild(guild)
        posted = False

        if post:
            channel_id = await gconf.lb_channel()
            channel = guild.get_channel(channel_id) if channel_id else None
            if channel is not None:
                standings = await self._gather_standings(guild)
                embed = await self._build_embed(guild, standings)
                content = None
                role_id = await gconf.ping_role()
                if role_id and guild.get_role(role_id):
                    content = guild.get_role(role_id).mention
                with contextlib.suppress(discord.HTTPException):
                    await channel.send(
                        content=content,
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions(roles=True),
                    )
                    posted = True

        await self._snapshot_baselines(guild)
        await self._reset_owned(guild)
        await gconf.last_reset.set(datetime.now(timezone.utc).timestamp())
        return posted

    # ------------------------------------------------------------------ #
    # Scheduler
    # ------------------------------------------------------------------ #
    def _tzinfo(self, name: str):
        if ZoneInfo is not None:
            try:
                return ZoneInfo(name)
            except Exception:
                pass
        return timezone.utc

    @staticmethod
    def _last_scheduled(now: datetime, weekday: int, hour: int) -> datetime:
        """Most recent datetime <= now matching weekday(0=Mon) at hour:00 local."""
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        days_behind = (candidate.weekday() - weekday) % 7
        candidate -= timedelta(days=days_behind)
        if candidate > now:
            candidate -= timedelta(days=7)
        return candidate

    async def _scheduler(self):
        await self.bot.wait_until_red_ready()
        with contextlib.suppress(Exception):
            self._seed_voice_timers()
        while True:
            try:
                for guild in list(self.bot.guilds):
                    await self._maybe_fire(guild)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(300)  # check every 5 minutes

    async def _maybe_fire(self, guild: discord.Guild):
        gconf = self.config.guild(guild)
        if not await gconf.enabled():
            return
        if not await gconf.lb_channel():
            return

        tz = self._tzinfo(await gconf.tz())
        now_local = datetime.now(tz)
        scheduled = self._last_scheduled(
            now_local, await gconf.reset_weekday(), await gconf.reset_hour()
        )
        scheduled_ts = scheduled.timestamp()

        last_reset = await gconf.last_reset()
        if last_reset is None:
            # First run: establish a clean baseline without posting an empty board.
            await self._snapshot_baselines(guild)
            await gconf.last_reset.set(datetime.now(timezone.utc).timestamp())
            return

        if last_reset < scheduled_ts:
            log.info("CrewStats firing weekly cycle for guild %s", guild.id)
            await self._run_cycle(guild, post=True)

    # ================================================================== #
    # User-facing commands
    # ================================================================== #
    @commands.guild_only()
    @commands.group(invoke_without_command=True)
    async def weekly(self, ctx: commands.Context):
        """Show this week's KrustyKrew leaderboard (does not reset)."""
        async with ctx.typing():
            standings = await self._gather_standings(ctx.guild)
            embed = await self._build_embed(ctx.guild, standings)
        await ctx.send(embed=embed)

    @commands.guild_only()
    @weekly.command(name="me")
    async def weekly_me(self, ctx: commands.Context, member: discord.Member = None):
        """Show your (or someone's) numbers for the current week."""
        member = member or ctx.author
        standings = await self._gather_standings(ctx.guild)
        d = standings.get(member.id)
        if not d:
            await ctx.send(f"No activity tracked for **{member.display_name}** this week yet.")
            return
        embed = discord.Embed(
            title=f"📈 {member.display_name} — this week",
            color=0xF4900C,
        )
        embed.add_field(name="💬 Messages", value=f"{d['messages']:,}")
        embed.add_field(name="🔊 Voice", value=self._fmt_duration(d["voice_seconds"]))
        embed.add_field(name="🎰 Wagered", value=f"{d['casino_bet']:,} ({d['casino_wins']}W)")
        embed.add_field(name="🔴 Caught", value=f"{d['poke_caught']} ({d['poke_wins']} raid W)")
        embed.add_field(name="🗺️ Streak", value=f"{d['pokestop_streak']}d")
        embed.add_field(name="🆕 New dex", value=str(d["poke_dex"]))
        embed.add_field(name="🎯 OW cleared", value=str(d["ow_done"]))
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ #
    # Admin config
    # ------------------------------------------------------------------ #
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @commands.group(name="weeklyset")
    async def weeklyset(self, ctx: commands.Context):
        """Configure the weekly leaderboard."""

    @weeklyset.command(name="channel")
    async def weeklyset_channel(self, ctx, channel: discord.TextChannel = None):
        """Set (or clear) the channel the leaderboard auto-posts to."""
        await self.config.guild(ctx.guild).lb_channel.set(channel.id if channel else None)
        if channel:
            await ctx.send(f"✅ Weekly leaderboard will post to {channel.mention}.")
        else:
            await ctx.send("✅ Auto-post channel cleared.")

    @weeklyset.command(name="time")
    async def weeklyset_time(self, ctx, weekday: str, hour: int):
        """Set the post/reset time. Weekday name (e.g. Monday) and hour 0-23.

        Example: `[p]weeklyset time Monday 9`
        """
        weekday = weekday.strip().capitalize()
        if weekday not in WEEKDAYS:
            await ctx.send(f"Weekday must be one of: {humanize_list(WEEKDAYS)}")
            return
        if not 0 <= hour <= 23:
            await ctx.send("Hour must be between 0 and 23.")
            return
        await self.config.guild(ctx.guild).reset_weekday.set(WEEKDAYS.index(weekday))
        await self.config.guild(ctx.guild).reset_hour.set(hour)
        await ctx.send(f"✅ Leaderboard will post every **{weekday} at {hour:02d}:00**.")

    @weeklyset.command(name="tz")
    async def weeklyset_tz(self, ctx, tzname: str):
        """Set the timezone (IANA name, e.g. America/New_York)."""
        if ZoneInfo is None:
            await ctx.send("zoneinfo isn't available; the schedule will use UTC.")
            return
        try:
            ZoneInfo(tzname)
        except Exception:
            await ctx.send(
                "Unknown timezone. Use an IANA name like `America/New_York` or `UTC`."
            )
            return
        await self.config.guild(ctx.guild).tz.set(tzname)
        await ctx.send(f"✅ Timezone set to **{tzname}**.")

    @weeklyset.command(name="top")
    async def weeklyset_top(self, ctx, count: int):
        """How many members to list per category (1-15)."""
        if not 1 <= count <= 15:
            await ctx.send("Pick a number between 1 and 15.")
            return
        await self.config.guild(ctx.guild).top_n.set(count)
        await ctx.send(f"✅ Showing top **{count}** per category.")

    @weeklyset.command(name="role")
    async def weeklyset_role(self, ctx, role: discord.Role = None):
        """Set (or clear) a role to ping when the leaderboard posts."""
        await self.config.guild(ctx.guild).ping_role.set(role.id if role else None)
        await ctx.send(
            f"✅ Will ping {role.mention} on post." if role else "✅ Ping role cleared.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @weeklyset.command(name="toggle")
    async def weeklyset_toggle(self, ctx):
        """Turn automatic weekly posting on or off."""
        cur = await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(not cur)
        await ctx.send(f"✅ Auto-post is now **{'ON' if not cur else 'OFF'}**.")

    @weeklyset.command(name="forcepost")
    async def weeklyset_forcepost(self, ctx):
        """Post the leaderboard now AND reset the week (the full cycle)."""
        posted = await self._run_cycle(ctx.guild, post=True)
        if posted:
            await ctx.tick()
        else:
            await ctx.send(
                "Posted nothing (is the channel set with `[p]weeklyset channel`?), "
                "but the week was reset."
            )

    @weeklyset.command(name="rebaseline")
    async def weeklyset_rebaseline(self, ctx):
        """Reset counters & re-snapshot baselines WITHOUT posting."""
        await self._run_cycle(ctx.guild, post=False)
        await ctx.send("✅ Week reset and baselines re-snapshotted (nothing posted).")

    @weeklyset.command(name="settings")
    async def weeklyset_settings(self, ctx):
        """Show the current configuration."""
        g = await self.config.guild(ctx.guild).all()
        channel = ctx.guild.get_channel(g["lb_channel"]) if g["lb_channel"] else None
        role = ctx.guild.get_role(g["ping_role"]) if g["ping_role"] else None
        last = (
            f"<t:{int(g['last_reset'])}:R>" if g["last_reset"] else "never"
        )
        embed = discord.Embed(title="CrewStats settings", color=0xF4900C)
        embed.add_field(name="Auto-post", value="ON" if g["enabled"] else "OFF")
        embed.add_field(name="Channel", value=channel.mention if channel else "—")
        embed.add_field(
            name="Schedule",
            value=f"{WEEKDAYS[g['reset_weekday']]} {g['reset_hour']:02d}:00 ({g['tz']})",
        )
        embed.add_field(name="Top N", value=str(g["top_n"]))
        embed.add_field(name="Ping role", value=role.mention if role else "—")
        embed.add_field(name="Last reset", value=last)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
