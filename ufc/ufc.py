"""
UFC Cog for Red-DiscordBot v3.

Commands
  !ufc card            upcoming fight card
  !ufc results         most recent event results
  !ufc fighter <name>  fighter stats + recent fights
  !ufc pick <name>     lock in a pick for the upcoming card
  !ufc picks           show server picks
  !ufc standings       pick-em leaderboard
  !ufc settle          [admin] score picks vs results, update standings
  !ufc clearpicks      [admin] clear picks without scoring
  !ufc resetstandings  [admin] wipe standings (confirmation required)
"""
import asyncio
import aiohttp
import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red

from .api import get_upcoming_event, get_recent_event, get_fighter
from . import embeds


def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


class UFC(commands.Cog):
    """UFC fight cards, results, fighter stats, and a server pick-em game."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=7833209, force_registration=True)
        self.config.register_guild(
            picks={},      # { "Red Name|Blue Name": { "user_id": "Picked Name" } }
            standings={},  # { "user_id": { "correct": int, "total": int } }
        )
        self._session: aiohttp.ClientSession = None

    async def cog_load(self):
        self._session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ── group ──────────────────────────────────────────────────────────────-

    @commands.group(name="ufc", invoke_without_command=True)
    async def ufc(self, ctx: commands.Context):
        """UFC commands."""
        p = ctx.clean_prefix
        e = discord.Embed(title="🥊  UFC Bot", color=0xD20A0A, description=(
            f"`{p}ufc card` — upcoming fight card\n"
            f"`{p}ufc results` — recent event results\n"
            f"`{p}ufc fighter <name>` — fighter stats\n"
            f"`{p}ufc pick <name>` — lock in your pick\n"
            f"`{p}ufc picks` — server picks\n"
            f"`{p}ufc standings` — pick-em leaderboard\n"
        ))
        await ctx.send(embed=e)

    # ── card / results ───────────────────────────────────────────────────────

    @ufc.command(name="card")
    async def ufc_card(self, ctx):
        async with ctx.typing():
            event = await get_upcoming_event(self.session)
        if not event:
            return await ctx.send(embed=embeds.error_embed(
                "Couldn't fetch the upcoming card right now. Try again shortly."))
        await ctx.send(embed=embeds.card_embed(event))

    @ufc.command(name="results")
    async def ufc_results(self, ctx):
        async with ctx.typing():
            event = await get_recent_event(self.session)
        if not event:
            return await ctx.send(embed=embeds.error_embed(
                "Couldn't fetch recent results right now. Try again shortly."))
        await ctx.send(embed=embeds.results_embed(event))

    # ── fighter ────────────────────────────────────────────────────────────-

    @ufc.command(name="fighter")
    async def ufc_fighter(self, ctx, *, name: str):
        """Look up a fighter's stats and recent fights. Example: !ufc fighter Jon Jones"""
        async with ctx.typing():
            f = await get_fighter(self.session, name)
        if not f:
            return await ctx.send(embed=embeds.error_embed(
                f"Couldn't find **{name}**.\n\n"
                "• Use their full name (`Jon Jones`, not `Jones`)\n"
                "• Check the spelling / use their official fight name"))
        await ctx.send(embed=embeds.fighter_embed(f))

    # ── pick ───────────────────────────────────────────────────────────────-

    @ufc.command(name="pick")
    async def ufc_pick(self, ctx, *, fighter_name: str):
        """Lock in your pick for a fight on the upcoming card. Example: !ufc pick Jon Jones"""
        async with ctx.typing():
            event = await get_upcoming_event(self.session)
        if not event:
            return await ctx.send(embed=embeds.error_embed(
                "Couldn't fetch the upcoming card to match your pick."))

        q = _norm(fighter_name)
        matched = picked = opponent = None
        for fight in event.get("fights", []):
            if q in _norm(fight["red"]):
                matched, picked, opponent = fight, fight["red"], fight["blue"]; break
            if q in _norm(fight["blue"]):
                matched, picked, opponent = fight, fight["blue"], fight["red"]; break

        if not matched:
            return await ctx.send(embed=embeds.error_embed(
                f"**{fighter_name}** isn't on the upcoming card.\n"
                f"Use `{ctx.clean_prefix}ufc card` to see current matchups."))

        fight_key = f"{matched['red']}|{matched['blue']}"
        uid = str(ctx.author.id)
        async with self.config.guild(ctx.guild).picks() as picks:
            picks.setdefault(fight_key, {})
            old = picks[fight_key].get(uid)
            picks[fight_key][uid] = picked

        if old and old != picked:
            await ctx.send(f"🔄 {ctx.author.mention} changed pick: **{old}** → **{picked}**")
        else:
            await ctx.send(embed=embeds.pick_confirm_embed(
                ctx.author, picked, opponent, event["shortname"]))

    # ── picks ──────────────────────────────────────────────────────────────-

    @ufc.command(name="picks")
    async def ufc_picks(self, ctx):
        async with ctx.typing():
            event = await get_upcoming_event(self.session)
            picks = await self.config.guild(ctx.guild).picks()
        if not event:
            # still show stored picks even if the card fetch failed
            event = {"shortname": "Upcoming Event", "name": "Upcoming Event",
                     "date": "TBD", "fights": []}
        await ctx.send(embed=embeds.picks_embed(event, picks, ctx.guild))

    # ── standings ────────────────────────────────────────────────────────────

    @ufc.command(name="standings")
    async def ufc_standings(self, ctx):
        standings = await self.config.guild(ctx.guild).standings()
        picks = await self.config.guild(ctx.guild).picks()
        pending = sum(len(v) for v in picks.values()) if picks else 0
        await ctx.send(embed=embeds.standings_embed(standings, ctx.guild, pending))

    # ── settle ─────────────────────────────────────────────────────────────-

    @ufc.command(name="settle")
    @checks.admin_or_permissions(manage_guild=True)
    async def ufc_settle(self, ctx):
        """[Admin] Score picks against the most recent results and update standings.

        Picks are matched to results by FIGHTER NAME, so this is robust even if
        ESPN reorders the card or tweaks names between pick-time and settle-time.
        Only resolved fights are scored; unresolved picks stay for a later settle.
        """
        async with ctx.typing():
            event = await get_recent_event(self.session)
            picks = await self.config.guild(ctx.guild).picks()

        if not event:
            return await ctx.send(embed=embeds.error_embed("Couldn't fetch recent results."))
        if not picks:
            return await ctx.send("No picks to settle.")

        result_fights = event.get("fights", [])

        def find_result(picked_name: str):
            """Find the results fight a picked fighter appears in (order-independent)."""
            pn = _norm(picked_name)
            for rf in result_fights:
                r, b = _norm(rf["red"]), _norm(rf["blue"])
                if pn == r or pn == b or pn in r or pn in b:
                    return rf
            return None

        # score by fighter name, collect which picks were resolved
        deltas = {}             # uid -> {correct, total}
        resolved = []           # (fight_key, uid)
        for fight_key, fight_picks in picks.items():
            for uid, picked in fight_picks.items():
                rf = find_result(picked)
                if not rf or not rf.get("winner"):
                    continue  # no result yet — leave it for next settle
                resolved.append((fight_key, uid))
                d = deltas.setdefault(uid, {"correct": 0, "total": 0})
                d["total"] += 1
                if _norm(rf["winner"]) == _norm(picked):
                    d["correct"] += 1

        if not deltas:
            return await ctx.send(embed=embeds.error_embed(
                "No fight results matched the locked-in picks yet.\n"
                "Run this once the event has finished and results are posted."))

        # apply to standings
        async with self.config.guild(ctx.guild).standings() as standings:
            for uid, d in deltas.items():
                s = standings.setdefault(uid, {"correct": 0, "total": 0})
                s["correct"] += d["correct"]
                s["total"]   += d["total"]

        # remove only the resolved picks (keep unresolved ones)
        async with self.config.guild(ctx.guild).picks() as picks_w:
            for fight_key, uid in resolved:
                if fight_key in picks_w and uid in picks_w[fight_key]:
                    del picks_w[fight_key][uid]
                if fight_key in picks_w and not picks_w[fight_key]:
                    del picks_w[fight_key]

        lines = [f"Scored **{len(resolved)}** pick(s):\n"]
        for uid, d in sorted(deltas.items(), key=lambda x: -x[1]["correct"]):
            m = ctx.guild.get_member(int(uid))
            disp = m.display_name if m else f"<@{uid}>"
            c, t = d["correct"], d["total"]
            pct = round(c / t * 100) if t else 0
            icon = "🔥" if c == t else ("✅" if c else "❌")
            lines.append(f"{icon} **{disp}**: {c}/{t} ({pct}%)")
        leftover = sum(len(v) for v in (await self.config.guild(ctx.guild).picks()).values())
        if leftover:
            lines.append(f"\n*{leftover} pick(s) not yet scored (fights pending).*")
        lines.append(f"\nUse `{ctx.clean_prefix}ufc standings` to see the leaderboard.")

        await ctx.send(embed=embeds.settle_embed(event["shortname"], lines))

    # ── clearpicks / resetstandings ───────────────────────────────────────────

    @ufc.command(name="clearpicks")
    @checks.admin_or_permissions(manage_guild=True)
    async def ufc_clearpicks(self, ctx):
        """[Admin] Clear all picks without scoring."""
        await self.config.guild(ctx.guild).picks.set({})
        await ctx.send("✅ Picks cleared.")

    @ufc.command(name="resetstandings")
    @checks.admin_or_permissions(administrator=True)
    async def ufc_resetstandings(self, ctx):
        """[Admin] Permanently wipe standings (asks for confirmation)."""
        await ctx.send("⚠️ This permanently deletes all standings. "
                       "Type `confirm` to proceed, or anything else to cancel.")

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=30)
        except asyncio.TimeoutError:
            return await ctx.send("Timed out — standings unchanged.")
        if _norm(msg.content) == "confirm":
            await self.config.guild(ctx.guild).standings.set({})
            await ctx.send("✅ Standings reset.")
        else:
            await ctx.send("Cancelled — standings unchanged.")


async def setup(bot: Red):
    await bot.add_cog(UFC(bot))
