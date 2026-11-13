"""Mines — uncover gems, avoid the mines, cash out whenever you like."""
from __future__ import annotations

import asyncio
import logging
import random
from contextlib import suppress
from typing import Optional

import discord
from redbot.core import bank, commands

from .casino_core import (
    currency, get_game_settings, mark_played, refund_wager, reply, resolve_bet, settle_game, validate_bet,
)
from .ui import GameOverView

GAME = "mines"
LOG = logging.getLogger("red.crewcogs.casino.mines")
TILES = 20            # 4 rows × 5 — the 5th row holds the Cash out button
HOUSE = 0.97
DEFAULT_MINES = 3
TIMEOUT = 120


def multiplier(safe_revealed: int, mines: int, tiles: int = TILES) -> float:
    """Fair odds of surviving `safe_revealed` picks, times the house factor."""
    if safe_revealed <= 0:
        return 1.0
    fair = 1.0
    for i in range(safe_revealed):
        fair *= (tiles - i) / (tiles - mines - i)
    return round(HOUSE * fair, 2)


class TileButton(discord.ui.Button):
    def __init__(self, index: int) -> None:
        super().__init__(label="​", emoji="⬛", style=discord.ButtonStyle.secondary, row=index // 5)
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.reveal(interaction, self.index)  # type: ignore[union-attr]


class MinesView(discord.ui.View):
    def __init__(self, cog: "Mines", ctx: commands.Context, bet: int, mines: int, cap: int, cur: str) -> None:
        super().__init__(timeout=TIMEOUT)
        self.cog, self.ctx, self.bet, self.mines, self.cap, self.cur = cog, ctx, bet, mines, cap, cur
        self.mine_set = set(random.sample(range(TILES), mines))
        self.revealed: set = set()
        self.finished = False
        self.lock = asyncio.Lock()
        self.message: Optional[discord.Message] = None
        self.tiles = [TileButton(i) for i in range(TILES)]
        for t in self.tiles:
            self.add_item(t)
        self.cash = discord.ui.Button(label="Cash out", emoji="💰", style=discord.ButtonStyle.success, row=4, disabled=True)
        self.cash.callback = self._cash
        self.add_item(self.cash)
        self._sync_cash()

    def payout(self) -> int:
        value = int(self.bet * multiplier(len(self.revealed), self.mines))
        return min(value, self.cap) if self.cap else value

    def _sync_cash(self) -> None:
        n = len(self.revealed)
        self.cash.disabled = n == 0
        self.cash.label = f"Cash out {multiplier(n, self.mines):.2f}× · {self.payout():,}" if n else "Cash out"

    def embed(self, text: Optional[str] = None, color: int = 0x34495E) -> discord.Embed:
        n = len(self.revealed)
        nxt = multiplier(n + 1, self.mines) if n < TILES - self.mines else None
        desc = text or "Pick a tile. Every 💎 raises your payout — hit a 💣 and you lose the bet."
        embed = discord.Embed(title=f"💣 Mines — {self.mines} mine{'s' if self.mines != 1 else ''}", description=desc, color=color)
        embed.add_field(name="Bet", value=f"{self.bet:,}", inline=True)
        embed.add_field(name="Gems found", value=f"{n}/{TILES - self.mines}", inline=True)
        embed.add_field(name="Now", value=f"{multiplier(n, self.mines):.2f}× · {self.payout():,}", inline=True)
        if nxt and not self.finished:
            embed.set_footer(text=f"Next gem: {nxt:.2f}× · 97% return · auto cash-out after {TIMEOUT}s idle")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("Not your minefield — start your own with `mines <bet>`!", ephemeral=True)
            return False
        return True

    def _reveal_all(self) -> None:
        for t in self.tiles:
            t.disabled = True
            t.label = "​"
            if t.index in self.mine_set:
                t.emoji, t.style = "💣", discord.ButtonStyle.danger
            elif t.index in self.revealed:
                t.emoji, t.style = "💎", discord.ButtonStyle.success
            else:
                t.emoji, t.style = "💎", discord.ButtonStyle.secondary
        self.cash.disabled = True

    async def reveal(self, interaction: discord.Interaction, index: int) -> None:
        async with self.lock:
            if self.finished or index in self.revealed:
                await interaction.response.defer()
                return
            tile = self.tiles[index]
            if index in self.mine_set:
                self.finished = True
                self._reveal_all()
                await self.cog.finish(self, interaction, won=False)
                return
            self.revealed.add(index)
            tile.emoji, tile.style, tile.disabled = "💎", discord.ButtonStyle.success, True
            if len(self.revealed) == TILES - self.mines:
                self.finished = True
                self._reveal_all()
                await self.cog.finish(self, interaction, won=True, cleared=True)
                return
            self._sync_cash()
            await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _cash(self, interaction: discord.Interaction) -> None:
        async with self.lock:
            if self.finished or not self.revealed:
                await interaction.response.defer()
                return
            self.finished = True
            self._reveal_all()
            await self.cog.finish(self, interaction, won=True)

    async def on_timeout(self) -> None:
        async with self.lock:
            if self.finished:
                return
            self.finished = True
            self._reveal_all()
            await self.cog.finish(self, None, won=bool(self.revealed), timed_out=True)


class Mines(commands.Cog):
    """Mines: find gems, dodge mines, cash out."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.active: set = set()

    @commands.command(name="mines", aliases=["minesweeper"])
    @commands.guild_only()
    async def mines_cmd(self, ctx: commands.Context, bet: str, mines: int = DEFAULT_MINES) -> None:
        """Uncover gems and cash out before you hit a mine. Usage: `mines <bet> [mines 1-10]`

        More mines = bigger multipliers per gem. Example: `mines 500 5`.
        """
        await self.start(ctx, bet, mines)

    async def start(self, ctx: commands.Context, raw_bet, mines: int = DEFAULT_MINES,
                    interaction: Optional[discord.Interaction] = None) -> None:
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.defer()
        bet, err = await resolve_bet(ctx.author, GAME, raw_bet)
        if err:
            await reply(ctx, interaction, err)
            return
        if not 1 <= mines <= 10:
            await reply(ctx, interaction, "Choose between 1 and 10 mines.")
            return
        if ctx.author.id in self.active:
            await reply(ctx, interaction, "Finish your current minefield first!")
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
        try:
            settings = await get_game_settings(ctx.guild, GAME)
            view = MinesView(self, ctx, bet, mines, int(settings.get("payout_cap", 0) or 0), await currency(ctx.guild))
            view.message = await ctx.send(embed=view.embed(), view=view)
        except Exception:
            LOG.exception("Mines failed to start for %s", ctx.author.id)
            self.active.discard(ctx.author.id)
            await refund_wager(ctx.author, GAME, bet, reason="mines failed to start")
            await reply(ctx, interaction, "Mines couldn't start — your wager was refunded.")

    async def finish(self, view: MinesView, interaction: Optional[discord.Interaction], *, won: bool,
                     cleared: bool = False, timed_out: bool = False) -> None:
        ctx, bet = view.ctx, view.bet
        try:
            if timed_out and not view.revealed:
                await refund_wager(ctx.author, GAME, bet, reason="mines timed out before any pick")
                text, color = "⏱️ Timed out before you picked a tile — your bet was refunded.", 0x95A5A6
            else:
                payout = view.payout() if won else 0
                outcome = "win" if payout > bet else ("push" if payout == bet else "loss")
                s = await settle_game(ctx.author, GAME, bet, payout, outcome, channel=ctx.channel)
                net = s.deposited - bet
                if not won:
                    text, color = f"💥 **BOOM!** You hit a mine. **{net:+,} {view.cur}**", 0xE74C3C
                elif cleared:
                    text, color = f"🏆 **Cleared the whole field!** {multiplier(len(view.revealed), view.mines):.2f}× · **{net:+,} {view.cur}**", 0xFFD700
                else:
                    prefix = "⏱️ Timed out — auto cashed out" if timed_out else "💰 **Cashed out**"
                    text, color = f"{prefix} at {multiplier(len(view.revealed), view.mines):.2f}× · **{net:+,} {view.cur}**", 0x2ECC71
            balance = await bank.get_balance(ctx.author)
            embed = view.embed(text + f"\nBalance: {balance:,}", color)
            view.stop()
            # One view: the revealed board on rows 1-4, replay buttons on row 5.
            again = GameOverView(ctx.author.id, bet, lambda i, b: self.start(ctx, b, view.mines, i),
                                 again_label="Play", row=4)
            for t in view.tiles:
                again.add_item(discord.ui.Button(label="\u200b", emoji=t.emoji, style=t.style, disabled=True, row=t.row))
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=again)
                again.message = view.message
            elif view.message is not None:
                with suppress(discord.HTTPException):
                    await view.message.edit(embed=embed, view=again)
                    again.message = view.message
        finally:
            self.active.discard(ctx.author.id)


async def setup(bot):
    await bot.add_cog(Mines(bot))
