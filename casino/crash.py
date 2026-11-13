"""Crash — ride the multiplier, cash out before it crashes."""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from contextlib import suppress
from typing import Optional

import discord
from redbot.core import bank, commands

from .casino_core import (
    currency, get_game_settings, mark_played, refund_wager, reply, resolve_bet, settle_game, validate_bet,
)
from .ui import GameOverView

GAME = "crash"
LOG = logging.getLogger("red.crewcogs.casino.crash")
HOUSE = 0.96          # every cash-out target has a 96% return to player
GROWTH = 0.10         # multiplier = e^(GROWTH × seconds): 2× ≈ 7s, 5× ≈ 16s, 10× ≈ 23s
TICK = 1.5            # seconds between live updates (Discord edit rate limits)
MAX_MULT = 100.0


def crash_point(rng: random.Random = random) -> float:
    u = rng.random()
    point = math.floor(HOUSE / max(1e-12, 1 - u) * 100) / 100
    return max(1.0, min(point, MAX_MULT))


def multiplier_at(seconds: float) -> float:
    return math.floor(math.exp(GROWTH * max(0.0, seconds)) * 100) / 100


def rocket_bar(mult: float) -> str:
    steps = min(12, int(math.log(mult, 1.35)) + 1) if mult > 1 else 1
    return "▁▂▃▄▅▆▇█"[min(7, steps // 2)] * steps + "🚀"


class CrashView(discord.ui.View):
    def __init__(self, cog: "Crash", game: dict) -> None:
        super().__init__(timeout=None)
        self.cog, self.game = cog, game

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.game["ctx"].author.id:
            await interaction.response.send_message("Not your rocket — launch your own with `crash <bet>`!", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Cash out", emoji="💰", style=discord.ButtonStyle.success)
    async def cash_out(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await self.cog.cash_out(self.game, manual=True)


class Crash(commands.Cog):
    """Crash: a rising multiplier — cash out before it crashes."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.active: set = set()

    @commands.command(name="crash", aliases=["rocket"])
    @commands.guild_only()
    async def crash(self, ctx: commands.Context, bet: str, auto_cashout: Optional[float] = None) -> None:
        """Launch a rocket. Cash out before it crashes! Usage: `crash <bet> [auto-cashout]`

        Example: `crash 500` (tap Cash out yourself) or `crash 500 2` (auto cash-out at 2×).
        """
        await self.start(ctx, bet, auto_cashout)

    async def start(self, ctx: commands.Context, raw_bet, auto: Optional[float] = None,
                    interaction: Optional[discord.Interaction] = None) -> None:
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.defer()
        bet, err = await resolve_bet(ctx.author, GAME, raw_bet)
        if err:
            await reply(ctx, interaction, err)
            return
        if auto is not None and not (1.01 <= auto <= MAX_MULT):
            await reply(ctx, interaction, f"Auto cash-out must be between 1.01× and {MAX_MULT:.0f}×.")
            return
        if ctx.author.id in self.active:
            await reply(ctx, interaction, "Your rocket is already flying!")
            return
        error = await validate_bet(ctx, GAME, bet)
        if error:
            await reply(ctx, interaction, error)
            return
        try:
            await bank.withdraw_credits(ctx.author, bet)
            mark_played(ctx.guild.id, ctx.author.id, GAME)
        except ValueError:
            await reply(ctx, interaction, "Your balance changed — try again.")
            return
        self.active.add(ctx.author.id)
        settings = await get_game_settings(ctx.guild, GAME)
        game = {
            "ctx": ctx, "bet": bet, "auto": auto, "crash": crash_point(), "state": "flying",
            "start": time.monotonic(), "lock": asyncio.Lock(), "message": None, "view": None,
            "cap": int(settings.get("payout_cap", 0) or 0), "cur": await currency(ctx.guild),
        }
        try:
            view = CrashView(self, game)
            game["view"] = view
            game["message"] = await ctx.send(embed=self._embed(game, 1.0), view=view)
            game["start"] = time.monotonic()
            await self._fly(game)
        except Exception:
            LOG.exception("Crash failed for %s", ctx.author.id)
            if game["state"] in ("flying", "settling"):
                game["state"] = "error"
                await refund_wager(ctx.author, GAME, bet, reason="crash game failure")
                await ctx.send("The rocket hit a glitch — your wager was refunded.")
        finally:
            self.active.discard(ctx.author.id)

    def _embed(self, game: dict, mult: float, *, title: Optional[str] = None, color: int = 0x3498DB,
               note: str = "") -> discord.Embed:
        bet = game["bet"]
        potential = self._payout(game, mult)
        auto = f" · auto cash-out at **{game['auto']:.2f}×**" if game["auto"] else ""
        embed = discord.Embed(
            title=title or "🚀 Crash — flying!",
            description=(f"# {mult:.2f}×\n{rocket_bar(mult)}\n\n"
                         f"Bet **{bet:,}** → cash out now for **{potential:,} {game['cur']}**{auto}{note}"),
            color=color,
        )
        embed.set_footer(text="Tap Cash out before it crashes · 96% return on any target")
        return embed

    def _payout(self, game: dict, mult: float) -> int:
        payout = int(game["bet"] * mult)
        return min(payout, game["cap"]) if game["cap"] else payout

    async def _fly(self, game: dict) -> None:
        while True:
            await asyncio.sleep(TICK)
            async with game["lock"]:
                if game["state"] != "flying":
                    return
                mult = multiplier_at(time.monotonic() - game["start"])
                if game["auto"] and game["auto"] <= mult and game["auto"] < game["crash"]:
                    await self._finish(game, game["auto"], won=True, how="auto")
                    return
                if mult >= game["crash"]:
                    await self._finish(game, game["crash"], won=False)
                    return
                with suppress(discord.HTTPException):
                    await game["message"].edit(embed=self._embed(game, mult))

    async def cash_out(self, game: dict, *, manual: bool) -> None:
        async with game["lock"]:
            if game["state"] != "flying":
                return
            mult = multiplier_at(time.monotonic() - game["start"])
            if mult >= game["crash"]:
                await self._finish(game, game["crash"], won=False)
            else:
                await self._finish(game, mult, won=True, how="manual")

    async def _finish(self, game: dict, mult: float, *, won: bool, how: str = "") -> None:
        """Caller holds game['lock']."""
        ctx, bet = game["ctx"], game["bet"]
        game["state"] = "settling"
        game["view"].stop()
        payout = self._payout(game, mult) if won else 0
        outcome = "win" if payout > bet else ("push" if payout == bet else "loss")
        settlement = await settle_game(ctx.author, GAME, bet, payout, outcome, channel=ctx.channel)
        game["state"] = "done"
        net = settlement.deposited - bet
        balance = await bank.get_balance(ctx.author)
        if won:
            title, color = f"💰 Cashed out at {mult:.2f}×{' (auto)' if how == 'auto' else ''}!", 0x2ECC71
            note = f"\n\n**{net:+,} {game['cur']}** — it would have crashed at **{game['crash']:.2f}×**."
        else:
            title, color = f"💥 CRASHED at {game['crash']:.2f}×", 0xE74C3C
            note = f"\n\n**{net:+,} {game['cur']}**"
        embed = self._embed(game, mult if won else game["crash"], title=title, color=color, note=note)
        embed.set_footer(text=f"Balance: {balance:,} {game['cur']}")
        again = GameOverView(ctx.author.id, bet, lambda i, b: self.start(ctx, b, game["auto"], i), again_label="Launch")
        with suppress(discord.HTTPException):
            await game["message"].edit(embed=embed, view=again)
            again.message = game["message"]


async def setup(bot):
    await bot.add_cog(Crash(bot))
