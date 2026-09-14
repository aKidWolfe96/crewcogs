from __future__ import annotations

import asyncio
import io
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from discord.ext import tasks
from redbot.core import Config, checks, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path

from .analytics import (
    accumulate_all_play,
    all_play_for_week,
    luck_for_week,
    matchup_results,
    optimal_lineup,
    power_rankings,
    season_luck,
    season_management,
    starter_slots,
    team_projection,
    weekly_trophies,
)
from .charts import line_chart
from .reports import (
    ROASTS,
    TROPHY_META,
    all_play_embed,
    base_embed,
    fmt_points,
    lineup_alert_embed,
    management_embed,
    maybe_mention,
    playoff_embed,
    rankings_embed,
    recap_embed,
    scoreboard_embed,
    standings_embed,
    team_name,
    transaction_embed,
    trophies_embed,
    trophy_case_embed,
    player_label,
)
from .sleeper import SleeperAPIError, SleeperClient

log = logging.getLogger("red.krewfantasy")

__red_end_user_data_statement__ = (
    "KrewFantasy stores per-server Sleeper league settings, Discord user IDs explicitly linked "
    "to Sleeper rosters, report preferences, and derived fantasy-football history. It does not "
    "store message content or authentication credentials."
)

DEFAULT_TOGGLES = {
    "final_scores": True,
    "trophies": True,
    "power": True,
    "standings": True,
    "matrix": True,
    "waivers": True,
    "matchups": True,
    "lineup": True,
    "score_updates": True,
    "trades": True,
    "recap": True,
    "charts": False,
}

NFL_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET", "GB",
    "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
    "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
}


class KrewFantasy(commands.Cog):
    """Sleeper fantasy football automation, analytics and weekly chaos for Krusty."""

    __version__ = "1.0.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=947312680526091, force_registration=True)
        self.config.register_guild(
            enabled=True,
            league_id=None,
            channel_id=None,
            timezone="America/New_York",
            links={},  # roster_id -> discord user id
            toggles=DEFAULT_TOGGLES,
            roast_mode="ruthless",
            seen_trade_ids=[],
            history={},
            trophy_case={},
            scheduler_keys=[],
            last_finalized_week=0,
        )
        self.session: Optional[aiohttp.ClientSession] = None
        self.sleeper: Optional[SleeperClient] = None
        self._guild_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()
        self.sleeper = SleeperClient(self.session, Path(cog_data_path(self)) / "cache")
        self.scheduler.start()

    def cog_unload(self) -> None:
        self.scheduler.cancel()
        if self.session and not self.session.closed:
            asyncio.create_task(self.session.close())

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        all_guilds = await self.config.all_guilds()
        for gid, data in all_guilds.items():
            links = data.get("links") or {}
            changed = False
            for rid, did in list(links.items()):
                if int(did) == int(user_id):
                    links.pop(rid, None)
                    changed = True
            if changed:
                await self.config.guild_from_id(int(gid)).links.set(links)

    # ---------------------------- helpers ----------------------------

    def _api(self) -> SleeperClient:
        if not self.sleeper:
            raise RuntimeError("KrewFantasy is not ready yet.")
        return self.sleeper

    @staticmethod
    def _extract_league_id(raw: str) -> Optional[str]:
        raw = raw.strip()
        if raw.isdigit():
            return raw
        nums = re.findall(r"(?<!\d)(\d{10,})(?!\d)", raw)
        return nums[-1] if nums else None

    async def _league_id(self, guild: discord.Guild) -> str:
        league_id = await self.config.guild(guild).league_id()
        if not league_id:
            raise commands.UserFeedbackCheckFailure("KrewFantasy is not set up. Use `!ff setup <Sleeper league URL or ID>` first.")
        return str(league_id)

    async def _bundle(self, guild: discord.Guild) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, str]]:
        league_id = await self._league_id(guild)
        league, rosters, users = await asyncio.gather(
            self._api().league(league_id), self._api().rosters(league_id), self._api().users(league_id)
        )
        names = self._roster_names(rosters, users)
        return league_id, league, rosters, users, names

    @staticmethod
    def _roster_names(rosters: Sequence[Mapping[str, Any]], users: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
        user_map = {str(u.get("user_id")): u for u in users}
        names: Dict[str, str] = {}
        for r in rosters:
            rid = str(r.get("roster_id"))
            u = user_map.get(str(r.get("owner_id")), {})
            meta = u.get("metadata") or {}
            name = meta.get("team_name") or meta.get("team_name_update") or u.get("display_name") or u.get("username") or f"Roster {rid}"
            names[rid] = str(name)
        return names

    @staticmethod
    def _owner_labels(rosters: Sequence[Mapping[str, Any]], users: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, str]]:
        user_map = {str(u.get("user_id")): u for u in users}
        out = {}
        for r in rosters:
            rid = str(r.get("roster_id"))
            u = user_map.get(str(r.get("owner_id")), {})
            meta = u.get("metadata") or {}
            out[rid] = {
                "team": str(meta.get("team_name") or u.get("display_name") or u.get("username") or f"Roster {rid}"),
                "display": str(u.get("display_name") or ""),
                "username": str(u.get("username") or ""),
                "user_id": str(u.get("user_id") or ""),
            }
        return out

    async def _match_roster(self, guild: discord.Guild, query: str) -> Tuple[Optional[str], List[str]]:
        _, _, rosters, users, names = await self._bundle(guild)
        q = query.strip().casefold()
        if q.isdigit() and any(str(r.get("roster_id")) == q for r in rosters):
            return q, []
        labels = self._owner_labels(rosters, users)
        exact, partial = [], []
        for rid, item in labels.items():
            candidates = {names.get(rid, ""), item["display"], item["username"], item["user_id"]}
            lowered = [c.casefold() for c in candidates if c]
            if q in lowered:
                exact.append(rid)
            elif any(q in c for c in lowered):
                partial.append(rid)
        found = exact or partial
        if len(found) == 1:
            return found[0], []
        return None, [f"{rid}: {names.get(rid, rid)}" for rid in found]

    async def _links(self, guild: discord.Guild) -> Dict[str, int]:
        raw = await self.config.guild(guild).links()
        return {str(k): int(v) for k, v in raw.items()}

    async def _timezone(self, guild: discord.Guild) -> ZoneInfo:
        raw = await self.config.guild(guild).timezone()
        try:
            return ZoneInfo(raw or "America/New_York")
        except ZoneInfoNotFoundError:
            return ZoneInfo("America/New_York")

    async def _safe_projections(self, season: int, week: int) -> Dict[str, Dict[str, Any]]:
        try:
            return await self._api().projections(season, week)
        except SleeperAPIError:
            log.warning("Projection feed unavailable for %s week %s", season, week)
            return {}

    async def _team_projections(self, league: Mapping[str, Any], matchups: Sequence[Mapping[str, Any]], week: int) -> Dict[str, float]:
        season = int(league.get("season") or datetime.now().year)
        projections = await self._safe_projections(season, week)
        if not projections:
            return {}
        scoring = league.get("scoring_settings") or {}
        return {
            str(row["roster_id"]): team_projection(row.get("starters") or [], projections, scoring)
            for row in matchups
        }

    async def _bye_teams(self, season: int, week: int) -> set[str]:
        try:
            schedule = await self._api().schedule(season)
        except SleeperAPIError:
            return set()
        playing: set[str] = set()
        for g in schedule:
            if int(g.get("week") or 0) != int(week):
                continue
            home = str(g.get("home") or "").upper()
            away = str(g.get("away") or "").upper()
            aliases = {"JAC": "JAX", "LA": "LAR", "WSH": "WAS"}
            if home:
                playing.add(aliases.get(home, home))
            if away:
                playing.add(aliases.get(away, away))
        return NFL_TEAMS - playing if playing else set()

    async def _analyze_week(self, guild: discord.Guild, week: int, *, save: bool = False, force: bool = False) -> Dict[str, Any]:
        async with self._guild_locks[guild.id]:
            league_id, league, rosters, users, names = await self._bundle(guild)
            matchups = await self._api().matchups(league_id, week)
            if not matchups:
                raise commands.UserFeedbackCheckFailure(f"Sleeper has no matchup data for week {week}.")
            scores = {str(m["roster_id"]): round(float(m.get("points") or 0.0), 2) for m in matchups}
            players = await self._api().players()
            roster_positions = league.get("roster_positions") or []
            efficiency: Dict[str, float] = {}
            points_left: Dict[str, float] = {}
            optimal: Dict[str, float] = {}
            for row in matchups:
                rid = str(row["roster_id"])
                pp = row.get("players_points") or {}
                result = optimal_lineup(roster_positions, row.get("players") or [], pp, players)
                opt = float(result["points"])
                actual = float(row.get("points") or 0.0)
                optimal[rid] = round(opt, 2)
                points_left[rid] = round(max(0.0, opt - actual), 2)
                efficiency[rid] = round(min(100.0, (actual / opt * 100.0)) if opt > 0 else 100.0, 1)

            all_play = all_play_for_week(scores)
            luck = luck_for_week(scores, matchups)
            projected = await self._team_projections(league, matchups, week)
            trophies = weekly_trophies(scores, matchups, luck, efficiency, points_left, projected or None)
            results = matchup_results(matchups)
            margins = {rid: float(rec.get("margin") or 0.0) for rid, rec in results.items()}
            block: Dict[str, Any] = {
                "week": int(week),
                "scores": scores,
                "optimal": optimal,
                "efficiency": efficiency,
                "points_left": points_left,
                "all_play": all_play,
                "luck": luck,
                "projected": projected,
                "trophies": trophies,
                "margins": margins,
                "finalized": bool(save),
                "archived_at": datetime.utcnow().isoformat() + "Z",
            }

            if save:
                history = await self.config.guild(guild).history()
                old = history.get(str(week))
                if old and old.get("finalized") and not force:
                    return old
                history[str(week)] = block
                history = self._recompute_power(history)
                await self.config.guild(guild).history.set(history)
                case = await self.config.guild(guild).trophy_case()
                if not (old and old.get("finalized")):
                    for award, rid in trophies.items():
                        case.setdefault(str(rid), {})
                        case[str(rid)][award] = int(case[str(rid)].get(award, 0)) + 1
                    await self.config.guild(guild).trophy_case.set(case)
                await self.config.guild(guild).last_finalized_week.set(max(int(week), await self.config.guild(guild).last_finalized_week()))
                block = history[str(week)]
            return block

    @staticmethod
    def _recompute_power(history: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        season_scores: Dict[str, List[float]] = defaultdict(list)
        margins: Dict[str, List[float]] = defaultdict(list)
        all_play_totals: Dict[str, Dict[str, int]] = defaultdict(lambda: {"wins": 0, "losses": 0, "ties": 0})
        h2h: Dict[str, Dict[str, int]] = defaultdict(lambda: {"wins": 0, "losses": 0, "ties": 0})
        for week in sorted(history, key=lambda w: int(w)):
            block = history[week]
            for rid, score in (block.get("scores") or {}).items():
                season_scores[str(rid)].append(float(score))
            for rid, margin in (block.get("margins") or {}).items():
                margins[str(rid)].append(float(margin))
            for rid, rec in (block.get("all_play") or {}).items():
                for key in ("wins", "losses", "ties"):
                    all_play_totals[str(rid)][key] += int(rec.get(key, 0))
            for rid, margin in (block.get("margins") or {}).items():
                if float(margin) > 0:
                    h2h[str(rid)]["wins"] += 1
                elif float(margin) < 0:
                    h2h[str(rid)]["losses"] += 1
                else:
                    h2h[str(rid)]["ties"] += 1
            block["standing_pct"] = {
                rid: round((rec["wins"] + 0.5 * rec["ties"]) / max(1, rec["wins"] + rec["losses"] + rec["ties"]) * 100.0, 1)
                for rid, rec in h2h.items()
            }
            block["power"] = power_rankings(season_scores, all_play_totals, margins)
        return history

    async def _sync_completed_history(self, guild: discord.Guild) -> Tuple[int, int]:
        state = await self._api().state()
        current_week = int(state.get("week") or 1)
        history = await self.config.guild(guild).history()
        synced = 0
        # The official state points at the current NFL week; archive earlier weeks.
        for week in range(1, max(1, current_week)):
            if str(week) in history and history[str(week)].get("finalized"):
                continue
            try:
                await self._analyze_week(guild, week, save=True)
                synced += 1
            except Exception:
                log.exception("Failed to archive week %s for guild %s", week, guild.id)
        return synced, current_week

    async def _latest_scored_week(self, guild: discord.Guild, current_week: int) -> int:
        league_id = await self._league_id(guild)
        candidates = list(dict.fromkeys([current_week, max(1, current_week - 1), max(1, current_week - 2)]))
        for week in candidates:
            try:
                rows = await self._api().matchups(league_id, week)
            except SleeperAPIError:
                continue
            if rows and sum(abs(float(r.get("points") or 0.0)) for r in rows) > 1:
                return week
        return max(1, current_week - 1)

    def _recap_text(self, week: int, block: Mapping[str, Any], names: Mapping[str, str], previous_power: Optional[Mapping[str, float]] = None) -> str:
        scores = block.get("scores") or {}
        trophies = block.get("trophies") or {}
        luck = block.get("luck") or {}
        left = block.get("points_left") or {}
        lines: List[str] = []
        king = trophies.get("king")
        bum = trophies.get("bum")
        if king:
            lines.append(f"👑 **{team_name(king, names)}** led the league with **{scores.get(king, 0):.2f}** points.")
        if trophies.get("murder"):
            rid = trophies["murder"]
            margin = abs(float((block.get("margins") or {}).get(rid, 0)))
            lines.append(f"💥 **{team_name(rid, names)}** delivered the week's biggest beating by **{margin:.2f}**.")
        robbed = trophies.get("robbed")
        if robbed:
            lines.append(f"😡 **{team_name(robbed, names)}** got the roughest draw, posting a luck score of **{luck.get(robbed, 0):+.1f}**.")
        bench = trophies.get("bench_coach")
        if bench:
            lines.append(f"🤡 **{team_name(bench, names)}** left **{left.get(bench, 0):.2f}** optimal points on the table.")
        if bum:
            lines.append(f"💩 Low score belonged to **{team_name(bum, names)}** at **{scores.get(bum, 0):.2f}**.")
        power = block.get("power") or {}
        if power:
            leader = max(power, key=power.get)
            lines.append(f"⚡ **{team_name(leader, names)}** exits Week {week} at #1 in the Krusty Power Rankings (**{power[leader]:.1f}**).")
        return "\n\n".join(lines) or "Not enough data to build a recap yet."

    async def _lineup_alerts(self, guild: discord.Guild, week: int) -> Tuple[Dict[str, List[str]], Dict[str, str], Dict[str, int]]:
        league_id, league, _, users, names = await self._bundle(guild)
        matchups = await self._api().matchups(league_id, week)
        players = await self._api().players()
        byes = await self._bye_teams(int(league.get("season") or datetime.now().year), week)
        slots = starter_slots(league.get("roster_positions") or [])
        alerts: Dict[str, List[str]] = defaultdict(list)
        for row in matchups:
            rid = str(row["roster_id"])
            starters = row.get("starters") or []
            for idx, slot in enumerate(slots):
                pid = str(starters[idx]) if idx < len(starters) and starters[idx] else ""
                if not pid:
                    alerts[rid].append(f"⚠️ **{slot}** is empty")
                    continue
                p = players.get(pid, {})
                label = player_label(pid, players)
                injury = str(p.get("injury_status") or "").strip()
                team = str(p.get("team") or "").upper()
                if injury.lower() in {"out", "ir", "pup", "suspended", "doubtful"}:
                    alerts[rid].append(f"🚫 **{slot}: {label}** — {injury}")
                elif injury.lower() in {"questionable", "q"}:
                    alerts[rid].append(f"⚠️ **{slot}: {label}** — Questionable")
                if team in byes:
                    alerts[rid].append(f"🏖️ **{slot}: {label}** — BYE")
        return dict(alerts), names, await self._links(guild)

    async def _post_embed(self, guild: discord.Guild, embed: discord.Embed) -> bool:
        channel_id = await self.config.guild(guild).channel_id()
        if not channel_id:
            return False
        channel = guild.get_channel(int(channel_id)) or guild.get_thread(int(channel_id))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return False
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        return True

    async def _post_file(self, guild: discord.Guild, path: Path, filename: str) -> bool:
        channel_id = await self.config.guild(guild).channel_id()
        if not channel_id:
            return False
        channel = guild.get_channel(int(channel_id)) or guild.get_thread(int(channel_id))
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return False
        await channel.send(file=discord.File(path, filename=filename))
        return True

    async def _scheduler_key_done(self, guild: discord.Guild, key: str) -> bool:
        keys = await self.config.guild(guild).scheduler_keys()
        return key in keys

    async def _mark_scheduler_key(self, guild: discord.Guild, key: str) -> None:
        keys = await self.config.guild(guild).scheduler_keys()
        if key not in keys:
            keys.append(key)
        await self.config.guild(guild).scheduler_keys.set(keys[-240:])

    async def _due(self, guild: discord.Guild, now: datetime, weekday: int, hour: int, minute: int, event: str) -> bool:
        if now.weekday() != weekday:
            return False
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not (scheduled <= now < scheduled + timedelta(minutes=60)):
            return False
        key = f"{now.date().isoformat()}:{event}"
        if await self._scheduler_key_done(guild, key):
            return False
        await self._mark_scheduler_key(guild, key)
        return True

    # ---------------------------- commands ----------------------------

    @commands.group(name="fantasy", aliases=["ff"], invoke_without_command=True)
    @commands.guild_only()
    async def fantasy(self, ctx: commands.Context) -> None:
        """KrewFantasy dashboard. Use `!help ff` for all commands."""
        try:
            league_id, league, rosters, users, names = await self._bundle(ctx.guild)
            state = await self._api().state()
        except commands.UserFeedbackCheckFailure:
            await ctx.send("🏈 KrewFantasy isn't configured yet. An admin can run `!ff setup <Sleeper league URL or ID>`.")
            return
        toggles = await self.config.guild(ctx.guild).toggles()
        channel_id = await self.config.guild(ctx.guild).channel_id()
        tz = await self.config.guild(ctx.guild).timezone()
        desc = (
            f"**{league.get('name', 'Sleeper League')}**\n"
            f"Season: **{league.get('season', '—')}** • NFL Week: **{state.get('week', '—')}**\n"
            f"Teams: **{len(rosters)}** • Timezone: **{tz}**\n"
            f"Auto channel: {f'<#{channel_id}>' if channel_id else '**not set**'}\n"
            f"Reports enabled: **{sum(1 for v in toggles.values() if v)} / {len(toggles)}**"
        )
        e = base_embed("🏈 KrewFantasy", desc)
        e.add_field(name="Quick Commands", value="`!ff scores` • `!ff standings` • `!ff power` • `!ff trophies` • `!ff matchup` • `!ff recap`", inline=False)
        await ctx.send(embed=e)

    @fantasy.command(name="setup")
    @checks.admin_or_permissions(manage_guild=True)
    async def ff_setup(self, ctx: commands.Context, *, league: str) -> None:
        """Connect this server to a Sleeper league URL or league ID."""
        league_id = self._extract_league_id(league)
        if not league_id:
            await ctx.send("I couldn't find a Sleeper league ID in that. Paste the league URL or numeric league ID.")
            return
        async with ctx.typing():
            data = await self._api().league(league_id)
            if str(data.get("sport")) != "nfl":
                await ctx.send("That Sleeper league is not an NFL league.")
                return
            await self.config.guild(ctx.guild).league_id.set(league_id)
            if not await self.config.guild(ctx.guild).channel_id():
                await self.config.guild(ctx.guild).channel_id.set(ctx.channel.id)
            # Baseline existing trades so setup does not dump old trade history into chat.
            state = await self._api().state()
            week = int(state.get("week") or 1)
            try:
                tx = await self._api().transactions(league_id, week)
                seen = [str(t.get("transaction_id")) for t in tx if t.get("type") == "trade" and t.get("transaction_id")]
                await self.config.guild(ctx.guild).seen_trade_ids.set(seen[-300:])
            except SleeperAPIError:
                pass
            synced, current = await self._sync_completed_history(ctx.guild)
        await ctx.send(
            f"✅ **{data.get('name', 'Sleeper league')}** is connected. Auto-posting channel: {ctx.channel.mention}. "
            f"Archived **{synced}** completed week(s); Sleeper currently reports Week **{current}**.\n"
            "Next: map owners with `!ff link @DiscordMember <Sleeper team/owner name>`."
        )

    @fantasy.command(name="channel")
    @checks.admin_or_permissions(manage_guild=True)
    async def ff_channel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        """Set the automatic fantasy report channel."""
        channel = channel or ctx.channel
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        await ctx.send(f"✅ KrewFantasy reports will post in {channel.mention}.")

    @fantasy.command(name="timezone")
    @checks.admin_or_permissions(manage_guild=True)
    async def ff_timezone(self, ctx: commands.Context, *, timezone_name: str) -> None:
        """Set IANA timezone, e.g. America/New_York."""
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            await ctx.send("❌ Unknown timezone. Example: `America/New_York`.")
            return
        await self.config.guild(ctx.guild).timezone.set(timezone_name)
        await ctx.send(f"✅ KrewFantasy timezone set to **{timezone_name}**.")

    @fantasy.command(name="link")
    @checks.admin_or_permissions(manage_guild=True)
    async def ff_link(self, ctx: commands.Context, member: discord.Member, *, sleeper_team: str) -> None:
        """Link a Discord member to a Sleeper team/owner."""
        rid, choices = await self._match_roster(ctx.guild, sleeper_team)
        if not rid:
            msg = "I couldn't uniquely match that Sleeper team/owner."
            if choices:
                msg += " Matches: " + ", ".join(choices[:8])
            await ctx.send(msg)
            return
        links = await self.config.guild(ctx.guild).links()
        links[str(rid)] = member.id
        await self.config.guild(ctx.guild).links.set(links)
        _, _, _, _, names = await self._bundle(ctx.guild)
        await ctx.send(f"✅ {member.mention} → **{names.get(str(rid), rid)}**")

    @fantasy.command(name="unlink")
    @checks.admin_or_permissions(manage_guild=True)
    async def ff_unlink(self, ctx: commands.Context, member: discord.Member) -> None:
        links = await self.config.guild(ctx.guild).links()
        removed = [rid for rid, did in links.items() if int(did) == member.id]
        for rid in removed:
            links.pop(rid, None)
        await self.config.guild(ctx.guild).links.set(links)
        await ctx.send("✅ Link removed." if removed else "That member wasn't linked to a Sleeper roster.")

    @fantasy.command(name="links")
    async def ff_links(self, ctx: commands.Context) -> None:
        _, _, _, _, names = await self._bundle(ctx.guild)
        links = await self._links(ctx.guild)
        lines = [f"**{names.get(rid, rid)}** → <@{did}>" for rid, did in links.items()]
        await ctx.send(embed=base_embed("🔗 Discord ↔ Sleeper Links", "\n".join(lines) or "No owners linked yet."))

    @fantasy.command(name="toggle")
    @checks.admin_or_permissions(manage_guild=True)
    async def ff_toggle(self, ctx: commands.Context, report: str, state: Optional[bool] = None) -> None:
        """Enable/disable an automatic report."""
        key = report.lower().replace("-", "_")
        toggles = await self.config.guild(ctx.guild).toggles()
        if key not in toggles:
            await ctx.send("Unknown report. Valid: " + ", ".join(f"`{k}`" for k in toggles))
            return
        toggles[key] = (not toggles[key]) if state is None else bool(state)
        await self.config.guild(ctx.guild).toggles.set(toggles)
        await ctx.send(f"✅ `{key}` is now **{'ON' if toggles[key] else 'OFF'}**.")

    @fantasy.command(name="roast")
    @checks.admin_or_permissions(manage_guild=True)
    async def ff_roast(self, ctx: commands.Context, mode: str) -> None:
        """Set roast mode: off, mild, ruthless."""
        mode = mode.lower()
        if mode not in {"off", "mild", "ruthless"}:
            await ctx.send("Use `off`, `mild`, or `ruthless`.")
            return
        await self.config.guild(ctx.guild).roast_mode.set(mode)
        await ctx.send(f"✅ Roast mode: **{mode}**.")

    @fantasy.command(name="scores")
    async def ff_scores(self, ctx: commands.Context, week: Optional[int] = None) -> None:
        league_id, league, _, _, names = await self._bundle(ctx.guild)
        if week is None:
            week = int((await self._api().state()).get("week") or 1)
        rows = await self._api().matchups(league_id, week)
        projs = await self._team_projections(league, rows, week)
        await ctx.send(embed=scoreboard_embed(week, rows, names, projs))

    @fantasy.command(name="matchup", aliases=["matchups", "preview"])
    async def ff_matchup(self, ctx: commands.Context, week: Optional[int] = None) -> None:
        league_id, league, _, _, names = await self._bundle(ctx.guild)
        if week is None:
            week = int((await self._api().state()).get("week") or 1)
        rows = await self._api().matchups(league_id, week)
        projs = await self._team_projections(league, rows, week)
        e = scoreboard_embed(week, rows, names, projs, title_prefix="MATCHUP PREVIEW")
        if not projs:
            e.set_footer(text="Sleeper's unofficial projection feed is unavailable; actual matchup data is still live.")
        await ctx.send(embed=e)

    @fantasy.command(name="standings")
    async def ff_standings(self, ctx: commands.Context) -> None:
        _, _, rosters, _, names = await self._bundle(ctx.guild)
        await ctx.send(embed=standings_embed(rosters, names))

    @fantasy.command(name="power")
    async def ff_power(self, ctx: commands.Context) -> None:
        await self._sync_completed_history(ctx.guild)
        history = await self.config.guild(ctx.guild).history()
        _, _, _, _, names = await self._bundle(ctx.guild)
        if not history:
            state = await self._api().state()
            block = await self._analyze_week(ctx.guild, int(state.get("week") or 1), save=False)
            values = block.get("power") or block.get("scores") or {}
            await ctx.send(embed=rankings_embed("⚡ Live Week Ranking (season power starts after a completed week)", values, names))
            return
        latest = history[sorted(history, key=lambda w: int(w))[-1]]
        await ctx.send(embed=rankings_embed("⚡ Krusty Power Rankings", latest.get("power") or {}, names))

    @fantasy.command(name="matrix", aliases=["allplay"])
    async def ff_matrix(self, ctx: commands.Context) -> None:
        await self._sync_completed_history(ctx.guild)
        history = await self.config.guild(ctx.guild).history()
        _, _, _, _, names = await self._bundle(ctx.guild)
        await ctx.send(embed=all_play_embed(accumulate_all_play(history), names))

    @fantasy.command(name="luck")
    async def ff_luck(self, ctx: commands.Context) -> None:
        await self._sync_completed_history(ctx.guild)
        history = await self.config.guild(ctx.guild).history()
        _, _, _, _, names = await self._bundle(ctx.guild)
        await ctx.send(embed=rankings_embed("🍀 Krusty Luck Index", season_luck(history), names, suffix=""))

    @fantasy.command(name="management", aliases=["efficiency", "bench"])
    async def ff_management(self, ctx: commands.Context) -> None:
        await self._sync_completed_history(ctx.guild)
        history = await self.config.guild(ctx.guild).history()
        _, _, _, _, names = await self._bundle(ctx.guild)
        await ctx.send(embed=management_embed(season_management(history), names))

    @fantasy.command(name="trophies")
    async def ff_trophies(self, ctx: commands.Context, week: Optional[int] = None) -> None:
        history = await self.config.guild(ctx.guild).history()
        if week is None:
            if history:
                week = max(int(w) for w in history)
            else:
                week = int((await self._api().state()).get("week") or 1)
        block = history.get(str(week)) or await self._analyze_week(ctx.guild, week, save=False)
        _, _, _, _, names = await self._bundle(ctx.guild)
        roast = await self.config.guild(ctx.guild).roast_mode()
        await ctx.send(embed=trophies_embed(week, block.get("trophies") or {}, names, roast_mode=roast))

    @fantasy.command(name="trophycase")
    async def ff_trophycase(self, ctx: commands.Context) -> None:
        _, _, _, _, names = await self._bundle(ctx.guild)
        await ctx.send(embed=trophy_case_embed(await self.config.guild(ctx.guild).trophy_case(), names))

    @fantasy.command(name="waivers")
    async def ff_waivers(self, ctx: commands.Context, week: Optional[int] = None) -> None:
        league_id, _, _, _, names = await self._bundle(ctx.guild)
        if week is None:
            week = int((await self._api().state()).get("week") or 1)
        txs = await self._api().transactions(league_id, week)
        rows = [t for t in txs if t.get("status") == "complete" and t.get("type") in {"waiver", "free_agent"}]
        players = await self._api().players()
        links = await self._links(ctx.guild)
        roast = await self.config.guild(ctx.guild).roast_mode()
        if not rows:
            await ctx.send(f"No completed waiver/free-agent moves found for Week {week}.")
            return
        for tx in sorted(rows, key=lambda x: int(x.get("created") or 0), reverse=True)[:10]:
            await ctx.send(embed=transaction_embed(tx, names, players, links, roast))

    @fantasy.command(name="trades")
    async def ff_trades(self, ctx: commands.Context, week: Optional[int] = None) -> None:
        league_id, _, _, _, names = await self._bundle(ctx.guild)
        if week is None:
            week = int((await self._api().state()).get("week") or 1)
        txs = await self._api().transactions(league_id, week)
        rows = [t for t in txs if t.get("status") == "complete" and t.get("type") == "trade"]
        players = await self._api().players()
        links = await self._links(ctx.guild)
        roast = await self.config.guild(ctx.guild).roast_mode()
        if not rows:
            await ctx.send(f"No completed trades found for Week {week}.")
            return
        for tx in sorted(rows, key=lambda x: int(x.get("created") or 0), reverse=True)[:10]:
            await ctx.send(embed=transaction_embed(tx, names, players, links, roast))

    @fantasy.command(name="faab")
    async def ff_faab(self, ctx: commands.Context) -> None:
        _, league, rosters, _, names = await self._bundle(ctx.guild)
        budget = int((league.get("settings") or {}).get("waiver_budget") or 100)
        rows = []
        for r in rosters:
            used = int((r.get("settings") or {}).get("waiver_budget_used") or 0)
            rows.append((budget - used, str(r["roster_id"])))
        rows.sort(reverse=True)
        lines = [f"**{team_name(rid, names)}** — ${remaining} remaining" for remaining, rid in rows]
        await ctx.send(embed=base_embed("💰 FAAB", "\n".join(lines)))

    @fantasy.command(name="lineup")
    async def ff_lineup(self, ctx: commands.Context, week: Optional[int] = None) -> None:
        if week is None:
            week = int((await self._api().state()).get("week") or 1)
        alerts, names, links = await self._lineup_alerts(ctx.guild, week)
        await ctx.send(embed=lineup_alert_embed(week, alerts, names, links))

    @fantasy.command(name="team")
    async def ff_team(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        member = member or ctx.author
        links = await self._links(ctx.guild)
        rid = next((r for r, did in links.items() if int(did) == member.id), None)
        if not rid:
            await ctx.send(f"{member.mention} isn't linked yet. An admin can use `!ff link @member <Sleeper team>`.")
            return
        league_id, league, rosters, _, names = await self._bundle(ctx.guild)
        roster = next((r for r in rosters if str(r.get("roster_id")) == rid), None)
        if not roster:
            await ctx.send("Linked Sleeper roster no longer exists in this league.")
            return
        players = await self._api().players()
        s = roster.get("settings") or {}
        wins, losses, ties = int(s.get("wins") or 0), int(s.get("losses") or 0), int(s.get("ties") or 0)
        rec = f"{wins}-{losses}" + (f"-{ties}" if ties else "")
        e = base_embed(f"🏈 {names.get(rid, 'Fantasy Team')}", f"Manager: {member.mention}\nRecord: **{rec}**")
        roster_ids = roster.get("players") or []
        labels = [player_label(str(pid), players) for pid in roster_ids]
        if labels:
            chunks = [labels[i:i+12] for i in range(0, len(labels), 12)]
            for i, chunk in enumerate(chunks[:2]):
                e.add_field(name="Roster" if i == 0 else "Roster (cont.)", value="\n".join(f"• {x}" for x in chunk), inline=False)
        await ctx.send(embed=e)

    @fantasy.command(name="recap")
    async def ff_recap(self, ctx: commands.Context, week: Optional[int] = None) -> None:
        history = await self.config.guild(ctx.guild).history()
        if week is None:
            week = max((int(w) for w in history), default=int((await self._api().state()).get("week") or 1))
        block = history.get(str(week)) or await self._analyze_week(ctx.guild, week, save=False)
        _, _, _, _, names = await self._bundle(ctx.guild)
        await ctx.send(embed=recap_embed(week, self._recap_text(week, block, names)))

    @fantasy.command(name="playoff", aliases=["playoffs"])
    async def ff_playoff(self, ctx: commands.Context) -> None:
        league_id, _, _, _, names = await self._bundle(ctx.guild)
        winners = await self._api().winners_bracket(league_id)
        losers = await self._api().losers_bracket(league_id)
        await ctx.send(embed=playoff_embed(winners, names, "🏆 Championship Bracket"))
        if losers:
            await ctx.send(embed=playoff_embed(losers, names, "🚽 Consolation / Toilet Bracket"))

    @fantasy.command(name="trending")
    async def ff_trending(self, ctx: commands.Context, kind: str = "add") -> None:
        kind = kind.lower()
        if kind not in {"add", "drop"}:
            kind = "add"
        rows = await self._api().trending(kind, 24, 15)
        players = await self._api().players()
        lines = [f"**{i}. {player_label(str(r.get('player_id')), players)}** — {r.get('count', 0)} {kind}s" for i, r in enumerate(rows, 1)]
        await ctx.send(embed=base_embed(f"🔥 Sleeper Trending {kind.title()}s", "\n".join(lines) or "No trending data."))

    @fantasy.command(name="chart")
    async def ff_chart(self, ctx: commands.Context, metric: str = "power") -> None:
        """Generate a season chart: power, scores, luck, efficiency."""
        metric = metric.lower()
        metric_map = {"power": ("power", "Power Ranking History"), "scores": ("scores", "Weekly Scoring"), "points": ("scores", "Weekly Scoring"), "luck": ("luck", "Luck Index by Week"), "efficiency": ("efficiency", "Manager Efficiency"), "standings": ("standing_pct", "Head-to-Head Win % History")}
        if metric not in metric_map:
            await ctx.send("Chart options: `power`, `scores`, `standings`, `luck`, `efficiency`.")
            return
        await self._sync_completed_history(ctx.guild)
        history = await self.config.guild(ctx.guild).history()
        _, _, _, _, names = await self._bundle(ctx.guild)
        field, title = metric_map[metric]
        out = Path(cog_data_path(self)) / "charts" / f"{ctx.guild.id}_{metric}.png"
        try:
            await asyncio.to_thread(line_chart, history, names, field, out, title)
        except RuntimeError as exc:
            await ctx.send(f"❌ {exc}")
            return
        await ctx.send(file=discord.File(out, filename=f"krewfantasy_{metric}.png"))

    @fantasy.command(name="schedule")
    async def ff_schedule(self, ctx: commands.Context) -> None:
        tz = await self.config.guild(ctx.guild).timezone()
        text = (
            f"All times use **{tz}**.\n\n"
            "**Tuesday 9:00 AM** — final scores, awards, recap\n"
            "**Tuesday 7:00 PM** — power rankings\n"
            "**Wednesday 9:00 AM** — standings + all-play matrix\n"
            "**Wednesday 11:00 AM** — waiver report\n"
            "**Thursday 7:00 PM** — matchup preview\n"
            "**Sunday 8:30 AM** — lineup/injury/bye alerts\n"
            "**Sunday 4:15 PM & 8:15 PM** — score updates\n"
            "**Monday 9:00 AM & 7:00 PM** — score updates\n"
            "**Trades** — announced automatically when detected (polls every 5 minutes)."
        )
        await ctx.send(embed=base_embed("🗓️ KrewFantasy Automation Schedule", text))

    @fantasy.command(name="status")
    async def ff_status(self, ctx: commands.Context) -> None:
        league_id = await self.config.guild(ctx.guild).league_id()
        channel_id = await self.config.guild(ctx.guild).channel_id()
        toggles = await self.config.guild(ctx.guild).toggles()
        history = await self.config.guild(ctx.guild).history()
        state = await self._api().state()
        lines = [
            f"Version: **{self.__version__}**",
            f"Sleeper league: **{league_id or 'not configured'}**",
            f"Channel: {f'<#{channel_id}>' if channel_id else '**not set**'}",
            f"NFL state: **{state.get('season', '—')} Week {state.get('week', '—')} ({state.get('season_type', '—')})**",
            f"Archived weeks: **{len(history)}**",
            f"Auto reports: **{sum(1 for x in toggles.values() if x)} enabled**",
            f"Scheduler: **{'running' if self.scheduler.is_running() else 'stopped'}**",
        ]
        await ctx.send(embed=base_embed("🛠️ KrewFantasy Status", "\n".join(lines)))

    @fantasy.command(name="sync")
    @checks.admin_or_permissions(manage_guild=True)
    async def ff_sync(self, ctx: commands.Context) -> None:
        async with ctx.typing():
            synced, current = await self._sync_completed_history(ctx.guild)
        await ctx.send(f"✅ Sync complete. Added **{synced}** completed week(s). Current Sleeper week: **{current}**.")

    @fantasy.command(name="finalize")
    @checks.admin_or_permissions(manage_guild=True)
    async def ff_finalize(self, ctx: commands.Context, week: Optional[int] = None) -> None:
        if week is None:
            state = await self._api().state()
            week = await self._latest_scored_week(ctx.guild, int(state.get("week") or 1))
        block = await self._analyze_week(ctx.guild, int(week), save=True, force=True)
        await ctx.send(f"✅ Week **{week}** finalized: {len(block.get('scores') or {})} teams archived.")

    @fantasy.command(name="refreshplayers")
    @checks.admin_or_permissions(manage_guild=True)
    async def ff_refreshplayers(self, ctx: commands.Context) -> None:
        async with ctx.typing():
            players = await self._api().players(force=True)
        await ctx.send(f"✅ Sleeper player cache refreshed: **{len(players):,}** player/team records.")

    @fantasy.command(name="test")
    @checks.admin_or_permissions(manage_guild=True)
    async def ff_test(self, ctx: commands.Context, report: str = "scores") -> None:
        """Preview a report immediately without touching scheduler state."""
        report = report.lower()
        if report in {"scores", "score"}:
            await ctx.invoke(self.ff_scores)
        elif report in {"matchup", "preview"}:
            await ctx.invoke(self.ff_matchup)
        elif report == "standings":
            await ctx.invoke(self.ff_standings)
        elif report == "power":
            await ctx.invoke(self.ff_power)
        elif report == "trophies":
            await ctx.invoke(self.ff_trophies)
        elif report == "lineup":
            await ctx.invoke(self.ff_lineup)
        elif report == "recap":
            await ctx.invoke(self.ff_recap)
        else:
            await ctx.send("Test options: scores, matchup, standings, power, trophies, lineup, recap.")

    # ---------------------------- automation ----------------------------

    @tasks.loop(minutes=5.0)
    async def scheduler(self) -> None:
        guilds = [g for g in self.bot.guilds if await self.config.guild(g).enabled() and await self.config.guild(g).league_id()]
        if not guilds:
            return
        try:
            state = await self._api().state()
        except Exception:
            log.exception("KrewFantasy could not fetch NFL state")
            return
        current_week = int(state.get("week") or 1)
        for guild in guilds:
            try:
                await self._monitor_trades(guild, current_week)
                now = datetime.now(await self._timezone(guild))
                toggles = await self.config.guild(guild).toggles()

                if await self._due(guild, now, 1, 9, 0, "weekly-final"):
                    week = await self._latest_scored_week(guild, current_week)
                    block = await self._analyze_week(guild, week, save=True)
                    league_id, _, _, _, names = await self._bundle(guild)
                    rows = await self._api().matchups(league_id, week)
                    if toggles.get("final_scores", True):
                        await self._post_embed(guild, scoreboard_embed(week, rows, names, block.get("projected") or None, "FINAL SCORES"))
                    if toggles.get("trophies", True):
                        await self._post_embed(guild, trophies_embed(week, block.get("trophies") or {}, names, roast_mode=await self.config.guild(guild).roast_mode()))
                    if toggles.get("recap", True):
                        await self._post_embed(guild, recap_embed(week, self._recap_text(week, block, names)))

                if toggles.get("power", True) and await self._due(guild, now, 1, 19, 0, "power"):
                    history = await self.config.guild(guild).history()
                    _, _, _, _, names = await self._bundle(guild)
                    if history:
                        latest = history[sorted(history, key=lambda w: int(w))[-1]]
                        await self._post_embed(guild, rankings_embed("⚡ Krusty Power Rankings", latest.get("power") or {}, names))
                        if toggles.get("charts", False) and len(history) >= 2:
                            out = Path(cog_data_path(self)) / "charts" / f"{guild.id}_power_auto.png"
                            try:
                                await asyncio.to_thread(line_chart, history, names, "power", out, "Power Ranking History")
                                await self._post_file(guild, out, "krewfantasy_power.png")
                            except Exception:
                                log.exception("Could not generate automatic power chart for guild %s", guild.id)

                if await self._due(guild, now, 2, 9, 0, "standings-matrix"):
                    _, _, rosters, _, names = await self._bundle(guild)
                    if toggles.get("standings", True):
                        await self._post_embed(guild, standings_embed(rosters, names))
                    if toggles.get("matrix", True):
                        history = await self.config.guild(guild).history()
                        await self._post_embed(guild, all_play_embed(accumulate_all_play(history), names))

                if toggles.get("waivers", True) and await self._due(guild, now, 2, 11, 0, "waivers"):
                    await self._post_waiver_digest(guild, current_week)

                if toggles.get("matchups", True) and await self._due(guild, now, 3, 19, 0, "matchup-preview"):
                    league_id, league, _, _, names = await self._bundle(guild)
                    rows = await self._api().matchups(league_id, current_week)
                    projs = await self._team_projections(league, rows, current_week)
                    await self._post_embed(guild, scoreboard_embed(current_week, rows, names, projs, "MATCHUP PREVIEW"))

                if toggles.get("lineup", True) and await self._due(guild, now, 6, 8, 30, "lineup"):
                    alerts, names, links = await self._lineup_alerts(guild, current_week)
                    await self._post_embed(guild, lineup_alert_embed(current_week, alerts, names, links))

                score_due = False
                for wd, hour, minute, key in ((6, 16, 15, "score-1"), (6, 20, 15, "score-2"), (0, 9, 0, "score-3"), (0, 19, 0, "score-4")):
                    if toggles.get("score_updates", True) and await self._due(guild, now, wd, hour, minute, key):
                        score_due = True
                if score_due:
                    league_id, league, _, _, names = await self._bundle(guild)
                    rows = await self._api().matchups(league_id, current_week)
                    await self._post_embed(guild, scoreboard_embed(current_week, rows, names, None, "LIVE SCORES"))

            except Exception:
                log.exception("KrewFantasy scheduler failed for guild %s", guild.id)

    @scheduler.before_loop
    async def before_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    async def _monitor_trades(self, guild: discord.Guild, week: int) -> None:
        toggles = await self.config.guild(guild).toggles()
        if not toggles.get("trades", True):
            return
        league_id, _, _, _, names = await self._bundle(guild)
        txs = await self._api().transactions(league_id, week)
        trades = [t for t in txs if t.get("type") == "trade" and t.get("status") == "complete" and t.get("transaction_id")]
        seen = await self.config.guild(guild).seen_trade_ids()
        seen_set = set(map(str, seen))
        new = [t for t in trades if str(t.get("transaction_id")) not in seen_set]
        if not new:
            return
        players = await self._api().players()
        links = await self._links(guild)
        roast = await self.config.guild(guild).roast_mode()
        for tx in sorted(new, key=lambda x: int(x.get("created") or 0)):
            await self._post_embed(guild, transaction_embed(tx, names, players, links, roast))
            seen.append(str(tx.get("transaction_id")))
        await self.config.guild(guild).seen_trade_ids.set(seen[-300:])

    async def _post_waiver_digest(self, guild: discord.Guild, week: int) -> None:
        league_id, _, _, _, names = await self._bundle(guild)
        txs = await self._api().transactions(league_id, week)
        rows = [t for t in txs if t.get("status") == "complete" and t.get("type") in {"waiver", "free_agent"}]
        players = await self._api().players()
        if not rows:
            await self._post_embed(guild, base_embed(f"💰 Week {week} Waiver Report", "No completed waiver/free-agent moves found."))
            return
        lines: List[str] = []
        for tx in sorted(rows, key=lambda x: int(x.get("created") or 0), reverse=True)[:20]:
            adds = tx.get("adds") or {}
            drops = tx.get("drops") or {}
            settings = tx.get("settings") or {}
            rid = str((tx.get("roster_ids") or [next(iter(adds.values()), "?")])[0])
            add_labels = ", ".join(player_label(str(pid), players) for pid in adds) or "—"
            drop_labels = ", ".join(player_label(str(pid), players) for pid in drops) or "—"
            bid = f" • ${settings.get('waiver_bid')}" if settings.get("waiver_bid") is not None else ""
            lines.append(f"**{team_name(rid, names)}**{bid}\n➕ {add_labels}\n➖ {drop_labels}")
        await self._post_embed(guild, base_embed(f"💰 Week {week} Waiver Report", "\n\n".join(lines)))

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, SleeperAPIError):
            await ctx.send(f"❌ Sleeper API error: {original}")
            return
        if isinstance(error, commands.UserFeedbackCheckFailure):
            await ctx.send(str(error))
            return
        # Let Red's normal handler deal with unrelated converter/check errors.
