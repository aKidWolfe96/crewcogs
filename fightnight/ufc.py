"""
UFC Cog for Red-DiscordBot
Displays UFC fight cards, results, fighter stats, and runs a server picks game.

Commands:
    !ufc card       - Upcoming fight card
    !ufc results    - Most recent event results
    !ufc fighter    - Fighter stats
    !ufc pick       - Log your pick for a fight
    !ufc picks      - View server picks for upcoming event
    !ufc standings  - Pick 'em leaderboard
    !ufc settle     - [Admin] Settle picks after an event
    !ufc clearpicks - [Admin] Clear picks for a new event
"""
import asyncio
import aiohttp
import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red
from typing import Optional

from .api import (
    get_upcoming_event,
    get_recent_event,
    search_fighter_espn,
    get_fighter_sherdog,
)
from .embeds import (
    build_card_embed,
    build_results_embed,
    build_fighter_embed,
    build_picks_embed,
    build_standings_embed,
    build_pick_confirm_embed,
    build_error_embed,
)


class UFC(commands.Cog):
    """UFC fight cards, results, fighter stats, and server picks."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xUFC2024, force_registration=True)

        # Per-guild defaults
        self.config.register_guild(
            picks={},       # {fight_key: {user_id: fighter_name}}
            standings={},   # {user_id: {correct: int, total: int}}
            current_event=None,  # cached event name for active picks
        )

        self._session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        self._session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self._session:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ─── Helper ───────────────────────────────────────────────────────────────

    async def _send_error(self, ctx: commands.Context, msg: str):
        await ctx.send(embed=build_error_embed(msg))

    # ─── Main Command Group ───────────────────────────────────────────────────

    @commands.group(name="ufc", invoke_without_command=True)
    async def ufc(self, ctx: commands.Context):
        """UFC commands — fight cards, results, fighter stats, and picks."""
        prefix = ctx.clean_prefix
        help_text = (
            f"**UFC Commands**\n"
            f"`{prefix}ufc card` — Upcoming fight card\n"
            f"`{prefix}ufc results` — Most recent event results\n"
            f"`{prefix}ufc fighter <name>` — Fighter stats & record\n"
            f"`{prefix}ufc pick <fighter name>` — Lock in your pick\n"
            f"`{prefix}ufc picks` — View server picks for next event\n"
            f"`{prefix}ufc standings` — Pick 'em leaderboard\n"
        )
        embed = discord.Embed(
            title="🥊 UFC Bot",
            description=help_text,
            color=0xD20A0A,
        )
        embed.set_footer(text="Data via ESPN • Picks powered by your server")
        await ctx.send(embed=embed)

    # ─── !ufc card ────────────────────────────────────────────────────────────

    @ufc.command(name="card")
    async def ufc_card(self, ctx: commands.Context):
        """Show the upcoming UFC fight card."""
        async with ctx.typing():
            event = await get_upcoming_event(self.session)

        if not event:
            await self._send_error(
                ctx,
                "Couldn't fetch the upcoming fight card right now. Try again shortly.",
            )
            return

        embed = build_card_embed(event)
        await ctx.send(embed=embed)

    # ─── !ufc results ─────────────────────────────────────────────────────────

    @ufc.command(name="results")
    async def ufc_results(self, ctx: commands.Context):
        """Show results from the most recent UFC event."""
        async with ctx.typing():
            event = await get_recent_event(self.session)

        if not event:
            await self._send_error(
                ctx,
                "Couldn't fetch recent results right now. Try again shortly.",
            )
            return

        embed = build_results_embed(event)
        await ctx.send(embed=embed)

        # Auto-trigger standing settlement if picks exist for this event
        guild_picks = await self.config.guild(ctx.guild).picks()
        current_event = await self.config.guild(ctx.guild).current_event()
        if guild_picks and current_event and current_event == event.get("name"):
            await ctx.send(
                f"📢 Results are in! Admins: use `{ctx.clean_prefix}ufc settle` "
                f"to process picks and update standings."
            )

    # ─── !ufc fighter ─────────────────────────────────────────────────────────

    @ufc.command(name="fighter")
    async def ufc_fighter(self, ctx: commands.Context, *, name: str):
        """Look up a UFC fighter's stats and recent fights.

        Example: !ufc fighter Jon Jones
        """
        async with ctx.typing():
            # Try ESPN first
            fighter = await search_fighter_espn(self.session, name)

            # If ESPN gives us no stat categories, enrich with Sherdog
            if not fighter or (
                not fighter.get("stat_categories") and not fighter.get("fights")
            ):
                sherdog = await get_fighter_sherdog(self.session, name)
                if sherdog:
                    if fighter:
                        # Merge: use ESPN bio, Sherdog fights/record if missing
                        fighter["fights"] = sherdog.get("fights", [])
                        if not fighter.get("record"):
                            fighter["record"] = sherdog.get("record", "")
                        if not fighter.get("gym"):
                            fighter["gym"] = sherdog.get("association", "")
                    else:
                        fighter = sherdog

        if not fighter:
            await self._send_error(
                ctx,
                f"Couldn't find a fighter named **{name}**.\n"
                "Check the spelling — use their full fight name.",
            )
            return

        embed = build_fighter_embed(fighter)
        await ctx.send(embed=embed)

    # ─── !ufc pick ────────────────────────────────────────────────────────────

    @ufc.command(name="pick")
    async def ufc_pick(self, ctx: commands.Context, *, fighter_name: str):
        """Lock in your pick for a fight on the upcoming card.

        Example: !ufc pick Jon Jones
        The bot will match your pick to the right fight automatically.
        """
        async with ctx.typing():
            event = await get_upcoming_event(self.session)

        if not event:
            await self._send_error(ctx, "Couldn't fetch the upcoming card to match your pick.")
            return

        fights = event.get("fights", [])
        fighter_name_lower = fighter_name.lower().strip()

        matched_fight = None
        matched_fighter = None
        matched_opponent = None

        for fight in fights:
            red = fight.get("red_name", "")
            blue = fight.get("blue_name", "")
            if fighter_name_lower in red.lower():
                matched_fight = fight
                matched_fighter = red
                matched_opponent = blue
                break
            if fighter_name_lower in blue.lower():
                matched_fight = fight
                matched_fighter = blue
                matched_opponent = red
                break

        if not matched_fight:
            await self._send_error(
                ctx,
                f"**{fighter_name}** doesn't appear on the upcoming card.\n"
                f"Use `{ctx.clean_prefix}ufc card` to see the full fight card.",
            )
            return

        fight_key = f"{matched_fight['red_name']}|{matched_fight['blue_name']}"
        user_id = str(ctx.author.id)

        async with self.config.guild(ctx.guild).picks() as picks:
            if fight_key not in picks:
                picks[fight_key] = {}
            old_pick = picks[fight_key].get(user_id)
            picks[fight_key][user_id] = matched_fighter

        # Store which event picks are for
        await self.config.guild(ctx.guild).current_event.set(event.get("name"))

        if old_pick and old_pick != matched_fighter:
            await ctx.send(
                f"🔄 {ctx.author.mention} changed pick from **{old_pick}** → **{matched_fighter}**"
            )
        else:
            embed = build_pick_confirm_embed(
                ctx.author, matched_fighter, event["short_name"], matched_opponent
            )
            await ctx.send(embed=embed)

    # ─── !ufc picks ───────────────────────────────────────────────────────────

    @ufc.command(name="picks")
    async def ufc_picks(self, ctx: commands.Context):
        """Show everyone's picks for the upcoming event."""
        async with ctx.typing():
            event = await get_upcoming_event(self.session)
            picks = await self.config.guild(ctx.guild).picks()

        if not event:
            await self._send_error(ctx, "Couldn't fetch the upcoming event.")
            return

        embed = build_picks_embed(event, picks, ctx.guild)
        await ctx.send(embed=embed)

    # ─── !ufc standings ───────────────────────────────────────────────────────

    @ufc.command(name="standings")
    async def ufc_standings(self, ctx: commands.Context):
        """Show the server's pick 'em leaderboard."""
        standings = await self.config.guild(ctx.guild).standings()
        embed = build_standings_embed(standings, ctx.guild)
        await ctx.send(embed=embed)

    # ─── !ufc settle (Admin) ──────────────────────────────────────────────────

    @ufc.command(name="settle")
    @checks.admin_or_permissions(manage_guild=True)
    async def ufc_settle(self, ctx: commands.Context):
        """[Admin] Score picks against results and update standings.

        Run this after an event completes. The bot will fetch the most recent
        results and compare against locked-in picks.
        """
        async with ctx.typing():
            event = await get_recent_event(self.session)
            picks = await self.config.guild(ctx.guild).picks()

        if not event:
            await self._send_error(ctx, "Couldn't fetch recent results to settle picks.")
            return

        if not picks:
            await ctx.send("No picks to settle for this event.")
            return

        fights = event.get("fights", [])

        # Build winner map: fight_key -> winner name
        winner_map = {}
        for fight in fights:
            red = fight.get("red_name", "")
            blue = fight.get("blue_name", "")
            result = fight.get("result") or {}
            winner = result.get("winner", "")
            if red and blue and winner:
                key = f"{red}|{blue}"
                winner_map[key] = winner

        if not winner_map:
            await self._send_error(
                ctx,
                "No fight results found yet — results may not be posted. Try again after the event.",
            )
            return

        # Tally scores
        score_deltas = {}  # user_id -> {correct, total}

        for fight_key, fight_picks in picks.items():
            actual_winner = winner_map.get(fight_key)
            if not actual_winner:
                continue  # fight not yet resolved

            for user_id, picked in fight_picks.items():
                if user_id not in score_deltas:
                    score_deltas[user_id] = {"correct": 0, "total": 0}
                score_deltas[user_id]["total"] += 1
                if picked.lower() == actual_winner.lower():
                    score_deltas[user_id]["correct"] += 1

        if not score_deltas:
            await ctx.send("Picks exist but no fight results matched yet. Are results posted?")
            return

        # Update standings
        async with self.config.guild(ctx.guild).standings() as standings:
            for user_id, delta in score_deltas.items():
                if user_id not in standings:
                    standings[user_id] = {"correct": 0, "total": 0}
                standings[user_id]["correct"] += delta["correct"]
                standings[user_id]["total"] += delta["total"]

        # Clear picks for next event
        await self.config.guild(ctx.guild).picks.set({})
        await self.config.guild(ctx.guild).current_event.set(None)

        # Build summary
        lines = ["**Picks settled!** Here's how everyone did:\n"]
        for user_id, delta in sorted(score_deltas.items(), key=lambda x: -x[1]["correct"]):
            member = ctx.guild.get_member(int(user_id))
            display = member.display_name if member else f"User {user_id}"
            correct = delta["correct"]
            total = delta["total"]
            pct = int((correct / total) * 100) if total > 0 else 0
            emoji = "🔥" if correct == total else ("✅" if correct > 0 else "❌")
            lines.append(f"{emoji} **{display}**: {correct}/{total} ({pct}%)")

        lines.append(f"\nPicks cleared. Use `{ctx.clean_prefix}ufc pick` for the next event!")

        embed = discord.Embed(
            title=f"📊 Picks Settled: {event.get('short_name', event.get('name', 'UFC Event'))}",
            description="\n".join(lines),
            color=0xC8A951,
        )
        await ctx.send(embed=embed)

    # ─── !ufc clearpicks (Admin) ──────────────────────────────────────────────

    @ufc.command(name="clearpicks")
    @checks.admin_or_permissions(manage_guild=True)
    async def ufc_clearpicks(self, ctx: commands.Context):
        """[Admin] Manually clear all picks without settling scores."""
        await self.config.guild(ctx.guild).picks.set({})
        await self.config.guild(ctx.guild).current_event.set(None)
        await ctx.send("✅ All picks cleared.")

    # ─── !ufc resetstandings (Admin) ──────────────────────────────────────────

    @ufc.command(name="resetstandings")
    @checks.admin_or_permissions(administrator=True)
    async def ufc_resetstandings(self, ctx: commands.Context):
        """[Admin] Wipe the pick 'em standings entirely. Cannot be undone."""
        await ctx.send(
            "⚠️ This will **permanently delete** all standings. "
            "Type `confirm` to proceed."
        )

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() == "confirm"

        try:
            await self.bot.wait_for("message", check=check, timeout=30)
        except asyncio.TimeoutError:
            await ctx.send("Reset cancelled.")
            return

        await self.config.guild(ctx.guild).standings.set({})
        await ctx.send("✅ Standings reset.")
