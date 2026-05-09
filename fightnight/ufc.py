"""
UFC Cog for Red-DiscordBot v3
Commands:
    !ufc card           upcoming fight card
    !ufc results        most recent event results
    !ufc fighter <name> fighter stats
    !ufc pick <name>    lock in a pick
    !ufc picks          see server picks
    !ufc standings      pick em leaderboard
    !ufc settle         [admin] score picks and update standings
    !ufc clearpicks     [admin] clear picks without settling
    !ufc resetstandings [admin] wipe standings
"""
import asyncio
import aiohttp
import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red

from .api import get_upcoming_event, get_recent_event, get_fighter
from .embeds import (
    card_embed, results_embed, fighter_embed,
    picks_embed, standings_embed,
    pick_confirm_embed, error_embed,
)


class UFC(commands.Cog):
    """UFC fight cards, results, fighter stats, and server pick em."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=7833209, force_registration=True
        )
        self.config.register_guild(
            picks={},       # {"Fighter A|Fighter B": {"user_id": "Fighter A"}}
            standings={},   # {"user_id": {"correct": int, "total": int}}
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

    # ── group ─────────────────────────────────────────────────────────────────

    @commands.group(name="ufc", invoke_without_command=True)
    async def ufc(self, ctx: commands.Context):
        """UFC commands."""
        p = ctx.clean_prefix
        embed = discord.Embed(title="🥊  UFC Bot", color=0xD20A0A, description=(
            f"`{p}ufc card` — upcoming fight card\n"
            f"`{p}ufc results` — recent event results\n"
            f"`{p}ufc fighter <name>` — fighter stats\n"
            f"`{p}ufc pick <name>` — lock in your pick\n"
            f"`{p}ufc picks` — server picks\n"
            f"`{p}ufc standings` — pick em leaderboard\n"
        ))
        await ctx.send(embed=embed)

    # ── card ──────────────────────────────────────────────────────────────────

    @ufc.command(name="card")
    async def ufc_card(self, ctx: commands.Context):
        """Upcoming UFC fight card."""
        async with ctx.typing():
            event = await get_upcoming_event(self.session)
        if not event:
            return await ctx.send(embed=error_embed(
                "Couldn't fetch the upcoming card right now. Try again in a moment."
            ))
        await ctx.send(embed=card_embed(event))

    # ── results ───────────────────────────────────────────────────────────────

    @ufc.command(name="results")
    async def ufc_results(self, ctx: commands.Context):
        """Most recent UFC event results."""
        async with ctx.typing():
            event = await get_recent_event(self.session)
        if not event:
            return await ctx.send(embed=error_embed(
                "Couldn't fetch recent results right now. Try again in a moment."
            ))
        await ctx.send(embed=results_embed(event))

    # ── fighter ───────────────────────────────────────────────────────────────

    @ufc.command(name="fighter")
    async def ufc_fighter(self, ctx: commands.Context, *, name: str):
        """Look up a fighter's stats and recent fight history.

        Example: !ufc fighter Jon Jones
        """
        async with ctx.typing():
            f = await get_fighter(self.session, name)
        if not f:
            return await ctx.send(embed=error_embed(
                f"Couldn't find **{name}**.\n\n"
                "• Use their full name (`Jon Jones` not `Jones`)\n"
                "• Check spelling — use their official fight name\n"
                "• ESPN only covers active UFC fighters; "
                "Sherdog covers most of MMA history"
            ))
        await ctx.send(embed=fighter_embed(f))

    # ── pick ──────────────────────────────────────────────────────────────────

    @ufc.command(name="pick")
    async def ufc_pick(self, ctx: commands.Context, *, fighter_name: str):
        """Lock in your pick for a fight on the upcoming card.

        The bot matches your input to the right fight automatically.
        Example: !ufc pick Jon Jones
        """
        async with ctx.typing():
            event = await get_upcoming_event(self.session)

        if not event:
            return await ctx.send(embed=error_embed(
                "Couldn't fetch the upcoming card to match your pick."
            ))

        # match to a fight
        query = fighter_name.lower().strip()
        matched_fight = matched_fighter = opponent = None

        for fight in event.get("fights", []):
            red, blue = fight["red"], fight["blue"]
            if query in red.lower():
                matched_fight   = fight
                matched_fighter = red
                opponent        = blue
                break
            if query in blue.lower():
                matched_fight   = fight
                matched_fighter = blue
                opponent        = red
                break

        if not matched_fight:
            return await ctx.send(embed=error_embed(
                f"**{fighter_name}** isn't on the upcoming card.\n"
                f"Use `{ctx.clean_prefix}ufc card` to see current matchups."
            ))

        fight_key = f"{matched_fight['red']}|{matched_fight['blue']}"
        uid = str(ctx.author.id)

        async with self.config.guild(ctx.guild).picks() as picks:
            if fight_key not in picks:
                picks[fight_key] = {}
            old = picks[fight_key].get(uid)
            picks[fight_key][uid] = matched_fighter

        if old and old != matched_fighter:
            await ctx.send(
                f"🔄 {ctx.author.mention} changed pick: "
                f"**{old}** → **{matched_fighter}**"
            )
        else:
            await ctx.send(embed=pick_confirm_embed(
                ctx.author, matched_fighter, opponent, event["shortname"]
            ))

    # ── picks ─────────────────────────────────────────────────────────────────

    @ufc.command(name="picks")
    async def ufc_picks(self, ctx: commands.Context):
        """Show everyone's picks for the upcoming event."""
        async with ctx.typing():
            event = await get_upcoming_event(self.session)
            current_picks = await self.config.guild(ctx.guild).picks()

        if not event:
            return await ctx.send(embed=error_embed("Couldn't fetch the upcoming event."))

        await ctx.send(embed=picks_embed(event, current_picks, ctx.guild))

    # ── standings ─────────────────────────────────────────────────────────────

    @ufc.command(name="standings")
    async def ufc_standings(self, ctx: commands.Context):
        """Show the server pick em leaderboard."""
        s = await self.config.guild(ctx.guild).standings()
        await ctx.send(embed=standings_embed(s, ctx.guild))

    # ── settle ────────────────────────────────────────────────────────────────

    @ufc.command(name="settle")
    @checks.admin_or_permissions(manage_guild=True)
    async def ufc_settle(self, ctx: commands.Context):
        """[Admin] Score picks against the most recent results and update standings.

        Run this after an event finishes. Picks are cleared automatically.
        """
        async with ctx.typing():
            event = await get_recent_event(self.session)
            current_picks = await self.config.guild(ctx.guild).picks()

        if not event:
            return await ctx.send(embed=error_embed("Couldn't fetch recent results."))
        if not current_picks:
            return await ctx.send("No picks to settle.")

        # build winner map from results
        winner_map = {}  # fight_key -> winner name
        for f in event.get("fights", []):
            w = f.get("winner", "")
            if w:
                key = f"{f['red']}|{f['blue']}"
                winner_map[key] = w

        if not winner_map:
            return await ctx.send(embed=error_embed(
                "No fight results found yet — run this after the event finishes."
            ))

        # tally
        deltas = {}  # uid -> {correct, total}
        for fight_key, fight_picks in current_picks.items():
            actual = winner_map.get(fight_key)
            if not actual:
                continue
            for uid, picked in fight_picks.items():
                deltas.setdefault(uid, {"correct": 0, "total": 0})
                deltas[uid]["total"] += 1
                if picked.lower() == actual.lower():
                    deltas[uid]["correct"] += 1

        if not deltas:
            return await ctx.send(
                "Picks exist but no fight results matched — "
                "are results posted yet?"
            )

        # update standings
        async with self.config.guild(ctx.guild).standings() as standings:
            for uid, d in deltas.items():
                if uid not in standings:
                    standings[uid] = {"correct": 0, "total": 0}
                standings[uid]["correct"] += d["correct"]
                standings[uid]["total"]   += d["total"]

        # clear picks
        await self.config.guild(ctx.guild).picks.set({})

        # summary
        lines = [f"**Picks settled for {event['shortname']}!**\n"]
        for uid, d in sorted(deltas.items(), key=lambda x: -x[1]["correct"]):
            member  = ctx.guild.get_member(int(uid))
            display = member.display_name if member else f"<@{uid}>"
            c, t    = d["correct"], d["total"]
            pct     = round(c / t * 100) if t else 0
            icon    = "🔥" if c == t else ("✅" if c else "❌")
            lines.append(f"{icon} **{display}**: {c}/{t} ({pct}%)")

        lines.append(f"\nPicks cleared. Use `{ctx.clean_prefix}ufc pick` for the next event!")

        embed = discord.Embed(
            title="📊  Picks Settled",
            description="\n".join(lines),
            color=UFC_GOLD if deltas else 0x888888,
        )
        await ctx.send(embed=embed)

    # ── clearpicks ────────────────────────────────────────────────────────────

    @ufc.command(name="clearpicks")
    @checks.admin_or_permissions(manage_guild=True)
    async def ufc_clearpicks(self, ctx: commands.Context):
        """[Admin] Clear all picks without updating standings."""
        await self.config.guild(ctx.guild).picks.set({})
        await ctx.send("✅ Picks cleared.")

    # ── resetstandings ────────────────────────────────────────────────────────

    @ufc.command(name="resetstandings")
    @checks.admin_or_permissions(administrator=True)
    async def ufc_resetstandings(self, ctx: commands.Context):
        """[Admin] Permanently wipe all standings. Asks for confirmation."""
        await ctx.send(
            "⚠️ This will **permanently delete** all standings. "
            "Type `confirm` to proceed or anything else to cancel."
        )

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=30)
        except asyncio.TimeoutError:
            return await ctx.send("Timed out — standings not reset.")

        if msg.content.strip().lower() == "confirm":
            await self.config.guild(ctx.guild).standings.set({})
            await ctx.send("✅ Standings reset.")
        else:
            await ctx.send("Cancelled — standings unchanged.")




async def setup(bot: Red):
    await bot.add_cog(UFC(bot))
