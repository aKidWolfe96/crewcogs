"""Overwatch stats + a self-report spray-challenge board for Red-DiscordBot.

Stats are pulled live from the unofficial OverFast API, which scrapes Blizzard's
public career profiles. Challenge/spray *progress* is not exposed by any API, so
that half is a manually-curated board members check off themselves.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import pagify

from .cute_sprays import CUTE_SPRAYS

__version__ = "1.1.0"

OVERFAST_BASE = "https://overfast-api.tekrop.fr"
REQUEST_TIMEOUT = 15  # seconds per OverFast call
CACHE_TTL = 600  # 10 min, matches OverFast's own cache TTL
CONCURRENCY = 5  # cap simultaneous OverFast calls when building the board
MAX_EMBED_FIELDS = 25  # Discord hard limit
REWARD_MAXLEN = 100
CONDITION_MAXLEN = 300

ROLE_ORDER = ("tank", "damage", "support", "open")
ROLE_EMOJI = {
    "tank": "\U0001F6E1\uFE0F",
    "damage": "\u2694\uFE0F",
    "support": "\u271A",
    "open": "\U0001F500",
}
SPRAY = "\U0001F3A8"  # 🎨


class Overwatch(commands.Cog):
    """Overwatch stats and a self-report spray-challenge board."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=2026061501, force_registration=True)
        # Per member: their linked tag and which challenge IDs they've cleared.
        self.config.register_member(battletag=None, player_id=None, done=[])
        # Per guild: the challenge board. challenges = { "1": {"name", "reward"} }
        self.config.register_guild(challenges={}, next_id=1)
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": f"RedDiscordBot-Overwatch-Cog/{__version__}"},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        )
        self._cache: dict[str, tuple[float, dict]] = {}
        self._sem = asyncio.Semaphore(CONCURRENCY)

    async def cog_unload(self) -> None:
        # Close the aiohttp session cleanly on unload/reload.
        await self.session.close()

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Delete a user's stored data (linked tag + progress) from every guild."""
        all_members = await self.config.all_members()
        for guild_id, guild_members in all_members.items():
            if user_id in guild_members:
                await self.config.member_from_ids(guild_id, user_id).clear()

    # ------------------------------------------------------------------ #
    # OverFast API layer
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_player_id(battletag: str) -> str:
        # OverFast wants the BattleTag with '#' -> '-', e.g. Aaron#1234 -> Aaron-1234
        return battletag.replace("#", "-").strip()

    async def _get(self, path: str) -> Optional[dict]:
        """GET an OverFast path with a small TTL cache. Returns None on 404/error.

        Failures are deliberately not cached, so a transient rate-limit or a
        profile that was just set public will resolve on the next call.
        """
        now = time.monotonic()
        hit = self._cache.get(path)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
        async with self._sem:
            try:
                async with self.session.get(f"{OVERFAST_BASE}{path}") as r:
                    if r.status != 200:
                        # 404 = no such player; 429/503 = rate limited / throttled.
                        return None
                    data = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return None
        self._cache[path] = (now, data)
        return data

    async def fetch_summary(self, player_id: str) -> Optional[dict]:
        return await self._get(f"/players/{player_id}/summary")

    async def fetch_stats_summary(self, player_id: str) -> Optional[dict]:
        # Computed winrate/kda/games across heroes & roles.
        # NOTE: verify exact field names against https://overfast-api.tekrop.fr/docs
        # if Blizzard's profile layout shifts. Everything below uses .get() so a
        # field rename degrades to a blank rather than throwing.
        return await self._get(f"/players/{player_id}/stats/summary")

    # ------------------------------------------------------------------ #
    # Formatting helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _rank_line(summary: Optional[dict]) -> str:
        comp = (summary or {}).get("competitive") or {}
        platform = comp.get("pc") or comp.get("console") or {}
        parts = []
        for role in ROLE_ORDER:
            r = platform.get(role)
            if not r:
                continue
            div = (r.get("division") or "").title()
            if not div:
                continue
            tier = r.get("tier")
            label = f"{div} {tier}" if tier else div
            parts.append(f"{ROLE_EMOJI.get(role, '')} {label}")
        return " | ".join(parts) if parts else "Unranked / hidden"

    @staticmethod
    def _stat_line(stats: Optional[dict]) -> str:
        general = (stats or {}).get("general") or {}
        bits = []
        if (wr := general.get("winrate")) is not None:
            bits.append(f"{wr}% WR")
        if (kda := general.get("kda")) is not None:
            bits.append(f"{kda} KDA")
        if (games := general.get("games_played")) is not None:
            bits.append(f"{games} games")
        return " \u00b7 ".join(bits) if bits else "No public stats"

    @staticmethod
    def _bar(done: int, total: int, segments: int = 10) -> str:
        if total <= 0:
            return ""
        filled = round(segments * done / total)
        return "\u25B0" * filled + "\u25B1" * (segments - filled)

    @staticmethod
    def _done_on_board(done: list, challenges: dict) -> list:
        """Member's completed IDs that still exist on the board (filters stale)."""
        return [cid for cid in done if cid in challenges]

    # ------------------------------------------------------------------ #
    # Command group
    # ------------------------------------------------------------------ #
    @commands.guild_only()
    @commands.group(invoke_without_command=True)
    async def ow(self, ctx: commands.Context):
        """Overwatch stats and the server's spray-challenge board."""
        await ctx.send_help()

    @ow.command(name="link")
    async def ow_link(self, ctx: commands.Context, *, battletag: str):
        """Link your BattleTag, e.g. `[p]ow link Aaron#1234`.

        Your Overwatch career profile must be set to **Public** in-game,
        or stats will come back empty.
        """
        player_id = self._to_player_id(battletag)
        async with ctx.typing():
            summary = await self.fetch_summary(player_id)
        if summary is None:
            return await ctx.send(
                f"Couldn't find **{battletag}** on OverFast. Double-check the tag "
                f"(format `Name#1234`) and that your career profile is set to Public."
            )
        await self.config.member(ctx.author).battletag.set(battletag)
        await self.config.member(ctx.author).player_id.set(player_id)
        name = summary.get("username", battletag)
        await ctx.send(f"Linked **{ctx.author.display_name}** \u2192 **{name}**. \u2705")

    @ow.command(name="unlink")
    async def ow_unlink(self, ctx: commands.Context):
        """Unlink your BattleTag (keeps your challenge progress)."""
        await self.config.member(ctx.author).battletag.set(None)
        await self.config.member(ctx.author).player_id.set(None)
        await ctx.send("Unlinked your BattleTag.")

    @ow.command(name="profile")
    async def ow_profile(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Show one member's full Overwatch profile. Defaults to you."""
        member = member or ctx.author
        conf = self.config.member(member)
        player_id = await conf.player_id()
        if not player_id:
            who = "You haven't" if member == ctx.author else f"{member.display_name} hasn't"
            return await ctx.send(f"{who} linked a BattleTag yet. Use `{ctx.clean_prefix}ow link`.")

        async with ctx.typing():
            summary, stats = await asyncio.gather(
                self.fetch_summary(player_id), self.fetch_stats_summary(player_id)
            )
        if summary is None:
            return await ctx.send(
                "OverFast returned nothing — the profile may be private or the API is "
                "rate-limited right now. Try again shortly."
            )

        done = await conf.done()
        challenges = await self.config.guild(ctx.guild).challenges()
        emb = discord.Embed(
            title=summary.get("username") or await conf.battletag(),
            color=await ctx.embed_color(),
        )
        if summary.get("avatar"):
            emb.set_thumbnail(url=summary["avatar"])
        if summary.get("title"):
            emb.description = f"*{summary['title']}*"
        emb.add_field(name="Competitive", value=self._rank_line(summary), inline=False)
        emb.add_field(name="Stats", value=self._stat_line(stats), inline=False)
        if (endo := (summary.get("endorsement") or {}).get("level")) is not None:
            emb.add_field(name="Endorsement", value=str(endo), inline=True)
        if challenges:
            count = len(self._done_on_board(done, challenges))
            total = len(challenges)
            bar = self._bar(count, total)
            emb.add_field(name="Sprays", value=f"{count}/{total}  {bar}", inline=True)
        await ctx.send(embed=emb)

    @ow.command(name="tracker")
    async def ow_tracker(self, ctx: commands.Context):
        """The board: every linked member's ranks, stats, and spray progress."""
        members = await self.config.all_members(ctx.guild)
        linked = [
            (m, data)
            for uid, data in members.items()
            if data.get("player_id") and (m := ctx.guild.get_member(uid))
        ]
        if not linked:
            return await ctx.send(f"Nobody's linked yet. Start with `{ctx.clean_prefix}ow link`.")

        challenges = await self.config.guild(ctx.guild).challenges()
        total = len(challenges)

        async def build(member: discord.Member, data: dict):
            summary, stats = await asyncio.gather(
                self.fetch_summary(data["player_id"]),
                self.fetch_stats_summary(data["player_id"]),
            )
            done_count = len(self._done_on_board(data.get("done", []), challenges))
            return member, data, summary, stats, done_count

        async with ctx.typing():
            rows = await asyncio.gather(*(build(m, d) for m, d in linked))

        # Sort by spray progress, then name for a stable, readable order.
        rows.sort(key=lambda x: (-x[4], x[0].display_name.lower()))
        truncated = len(rows) > MAX_EMBED_FIELDS

        emb = discord.Embed(
            title=f"Overwatch Tracker \u2014 {ctx.guild.name}",
            color=await ctx.embed_color(),
        )
        for member, data, summary, stats, done_count in rows[:MAX_EMBED_FIELDS]:
            name = (summary or {}).get("username") or data.get("battletag") or member.display_name
            spray = f" \u2014 {SPRAY} {done_count}/{total}" if total else ""
            value = f"{self._rank_line(summary)}\n{self._stat_line(stats)}{spray}"
            emb.add_field(name=f"{member.display_name} ({name})", value=value, inline=False)
        footer = []
        if total:
            footer.append(f"{SPRAY} = spray challenges done \u00b7 {total} on the board")
        if truncated:
            footer.append(f"showing top {MAX_EMBED_FIELDS} of {len(rows)} linked")
        if footer:
            emb.set_footer(text=" \u00b7 ".join(footer))
        await ctx.send(embed=emb)

    # ------------------------------------------------------------------ #
    # Challenge board (self-report)
    # ------------------------------------------------------------------ #
    @ow.group(name="challenge", invoke_without_command=True)
    async def ow_challenge(self, ctx: commands.Context):
        """Manage and check off spray challenges."""
        await ctx.send_help()

    async def _send_paged(self, ctx: commands.Context, title: str, body: str):
        """Send a long body as one or more embeds, split on newlines."""
        pages = list(pagify(body, delims=["\n"], page_length=3900)) or [""]
        for i, page in enumerate(pages, 1):
            suffix = f" ({i}/{len(pages)})" if len(pages) > 1 else ""
            await ctx.send(
                embed=discord.Embed(
                    title=title + suffix, description=page, color=await ctx.embed_color()
                )
            )

    @ow_challenge.command(name="list")
    async def challenge_list(self, ctx: commands.Context):
        """Show the challenge board with a completion count per entry."""
        challenges = await self.config.guild(ctx.guild).challenges()
        if not challenges:
            return await ctx.send(
                f"No challenges yet. Admins can bulk-load the cute sprays with "
                f"`{ctx.clean_prefix}ow challenge seedcute`, or add one with "
                f"`{ctx.clean_prefix}ow challenge add`."
            )
        members = await self.config.all_members(ctx.guild)
        lines = []
        for cid, c in sorted(challenges.items(), key=lambda kv: int(kv[0])):
            count = sum(1 for d in members.values() if cid in d.get("done", []))
            lines.append(f"`#{cid}` **{c['reward']}** \u2014 {c['name']} (\u2705 {count})")
        await self._send_paged(ctx, f"{SPRAY} Spray Challenges", "\n".join(lines))

    @ow_challenge.command(name="mine")
    async def challenge_mine(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Show which challenges you (or another member) have completed."""
        member = member or ctx.author
        challenges = await self.config.guild(ctx.guild).challenges()
        if not challenges:
            return await ctx.send("No challenges on the board yet.")
        done = self._done_on_board(await self.config.member(member).done(), challenges)
        total = len(challenges)
        header = f"**{member.display_name}** \u2014 {len(done)}/{total}  {self._bar(len(done), total)}\n\n"
        if not done:
            return await ctx.send(header + "*Nothing checked off yet.*")
        body = "\n".join(
            f"\u2705 **{challenges[cid]['reward']}** \u2014 {challenges[cid]['name']}"
            for cid in sorted(done, key=int)
        )
        await self._send_paged(ctx, f"{SPRAY} {member.display_name}'s Sprays", header + body)

    @ow_challenge.command(name="who")
    async def challenge_who(self, ctx: commands.Context, challenge_id: str):
        """Show exactly who has completed one challenge."""
        challenges = await self.config.guild(ctx.guild).challenges()
        if challenge_id not in challenges:
            return await ctx.send(f"No challenge with that ID. See `{ctx.clean_prefix}ow challenge list`.")
        members = await self.config.all_members(ctx.guild)
        doers = [
            m.display_name
            for uid, d in members.items()
            if challenge_id in d.get("done", []) and (m := ctx.guild.get_member(uid))
        ]
        c = challenges[challenge_id]
        who = ", ".join(sorted(doers, key=str.lower)) if doers else "Nobody yet"
        await ctx.send(f"**{c['reward']}** \u2014 {c['name']}\nDone by: {who}")

    @ow_challenge.command(name="done")
    async def challenge_done(self, ctx: commands.Context, challenge_id: str):
        """Mark a challenge complete for yourself, e.g. `[p]ow challenge done 3`."""
        challenges = await self.config.guild(ctx.guild).challenges()
        if challenge_id not in challenges:
            return await ctx.send(f"No challenge with that ID. See `{ctx.clean_prefix}ow challenge list`.")
        async with self.config.member(ctx.author).done() as done:
            if challenge_id in done:
                return await ctx.send(f"You've already got **{challenges[challenge_id]['reward']}**.")
            done.append(challenge_id)
        await ctx.send(f"Marked **{challenges[challenge_id]['reward']}** done for you. {SPRAY}")

    @ow_challenge.command(name="undo")
    async def challenge_undo(self, ctx: commands.Context, challenge_id: str):
        """Un-mark a challenge for yourself."""
        async with self.config.member(ctx.author).done() as done:
            if challenge_id not in done:
                return await ctx.send("That one wasn't marked.")
            done.remove(challenge_id)
        await ctx.send("Updated.")

    @commands.admin_or_permissions(manage_guild=True)
    @ow_challenge.command(name="add")
    async def challenge_add(self, ctx: commands.Context, *, text: str):
        """Add a challenge. Format: `reward | challenge name`.

        e.g. `[p]ow challenge add Cute Venture Spray | Get 4 kills with one Tectonic Shock`
        """
        if "|" not in text:
            return await ctx.send("Use the format `reward | challenge name`.")
        reward, _, name = text.partition("|")
        reward, name = reward.strip()[:REWARD_MAXLEN], name.strip()[:CONDITION_MAXLEN]
        if not reward or not name:
            return await ctx.send("Both a reward and a name are required.")
        next_id = await self.config.guild(ctx.guild).next_id()
        async with self.config.guild(ctx.guild).challenges() as challenges:
            challenges[str(next_id)] = {"name": name, "reward": reward}
        await self.config.guild(ctx.guild).next_id.set(next_id + 1)
        await ctx.send(f"Added challenge **#{next_id}**: {name} \u2192 *{reward}*")

    @commands.admin_or_permissions(manage_guild=True)
    @ow_challenge.command(name="remove")
    async def challenge_remove(self, ctx: commands.Context, challenge_id: str):
        """Remove a challenge from the board."""
        async with self.config.guild(ctx.guild).challenges() as challenges:
            removed = challenges.pop(challenge_id, None)
        if removed is None:
            return await ctx.send("No such challenge.")
        await ctx.send(f"Removed **{removed['reward']}**.")

    @commands.admin_or_permissions(manage_guild=True)
    @ow_challenge.command(name="clear")
    async def challenge_clear(self, ctx: commands.Context, confirmation: str = ""):
        """Wipe the entire challenge board. Re-run with `confirm` to proceed."""
        if confirmation.lower() != "confirm":
            return await ctx.send(
                f"This wipes **all** challenges on the board. "
                f"Re-run `{ctx.clean_prefix}ow challenge clear confirm` to proceed."
            )
        await self.config.guild(ctx.guild).challenges.set({})
        await self.config.guild(ctx.guild).next_id.set(1)
        await ctx.send("Cleared the challenge board. (Members' progress entries are kept but hidden.)")

    @commands.admin_or_permissions(manage_guild=True)
    @ow_challenge.command(name="seedcute")
    async def challenge_seedcute(self, ctx: commands.Context):
        """Bulk-load the researched cute-spray challenges onto the board.

        Skips any whose reward is already present, so it's safe to re-run.
        """
        next_id = await self.config.guild(ctx.guild).next_id()
        added = 0
        async with self.config.guild(ctx.guild).challenges() as challenges:
            existing = {c["reward"] for c in challenges.values()}
            for hero, condition in CUTE_SPRAYS:
                reward = f"Cute {hero} Spray"
                if reward in existing:
                    continue
                challenges[str(next_id)] = {"name": condition, "reward": reward}
                existing.add(reward)
                next_id += 1
                added += 1
        await self.config.guild(ctx.guild).next_id.set(next_id)
        await ctx.send(
            f"Seeded **{added}** cute-spray challenges. "
            f"See them with `{ctx.clean_prefix}ow challenge list`.\n"
            f"Note: Shion (newest hero) has no cute spray yet \u2014 add it with "
            f"`{ctx.clean_prefix}ow challenge add` once it appears in-game."
        )
