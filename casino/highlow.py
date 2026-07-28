from __future__ import annotations

import asyncio
import logging
import random
from contextlib import suppress
from typing import Dict, List, Optional

import discord
from redbot.core import bank, commands

from .casino_core import (
    get_game_settings,
    mark_played,
    refund_wager,
    settle_game,
    validate_bet,
)

LOG = logging.getLogger("red.crewcogs.casino.highlow")
GAME = "highlow"
MAX_STREAK = 7
MULTIPLIERS = (1.20, 1.50, 1.90, 2.40, 3.00, 3.80, 4.75)
SUITS = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
RANK_VALUE = {rank: value for value, rank in enumerate(
    ("2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"), start=2
)}


def make_deck() -> List[str]:
    return [f"{rank}{suit}" for rank in RANK_VALUE for suit in SUITS]


def card_rank(card: str) -> str:
    return card[:-1]


def format_card(card: str) -> str:
    return f"**{card_rank(card)}{SUITS[card[-1]]}**"


class HighLowView(discord.ui.View):
    def __init__(self, cog: "HighLow", ctx: commands.Context, bet: int, deck: List[str], current: str):
        super().__init__(timeout=45)
        self.cog = cog
        self.ctx = ctx
        self.bet = bet
        self.deck = deck
        self.current = current
        self.streak = 0
        self.current_payout = 0
        self.message: Optional[discord.Message] = None
        self.finished = False
        self.lock = asyncio.Lock()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("This isn't your High/Low game.", ephemeral=True)
            return False
        return True

    def disable_controls(self) -> None:
        for item in self.children:
            item.disabled = True

    async def on_timeout(self) -> None:
        async with self.lock:
            if self.finished:
                return
            self.finished = True
            self.disable_controls()
            try:
                if self.streak:
                    result = await self.cog.finish(self, "win", self.current_payout)
                    text = f"⏱️ Time expired, so your current return of **{result.deposited:,}** was cashed out."
                else:
                    refunded = await refund_wager(
                        self.ctx.author, GAME, self.bet, reason="High/Low timed out before first guess"
                    )
                    text = f"⏱️ Time expired before your first guess. Refunded: **{refunded:,}**."
            except Exception:
                LOG.exception("High/Low timeout settlement failed for user %s", self.ctx.author.id)
                text = "⏱️ The game expired, but settlement encountered an error. An administrator should review the logs."
            if self.message:
                embed = self.cog.make_embed(self, description=text, color=discord.Color.orange())
                with suppress(discord.HTTPException, discord.NotFound):
                    await self.message.edit(embed=embed, view=self)
            self.cog.active_players.discard(self.ctx.author.id)
            self.stop()

    async def resolve_guess(self, interaction: discord.Interaction, guess: str) -> None:
        async with self.lock:
            if self.finished:
                return
            await interaction.response.defer()
            next_card = self.deck.pop()
            old_value = RANK_VALUE[card_rank(self.current)]
            new_value = RANK_VALUE[card_rank(next_card)]
            correct = new_value > old_value if guess == "higher" else new_value < old_value
            tied = new_value == old_value
            previous = self.current
            self.current = next_card

            if tied:
                description = (
                    f"{format_card(previous)} → {format_card(next_card)}\n\n"
                    "🤝 **Tie — your streak stays the same. Choose again.**"
                )
                await interaction.message.edit(embed=self.cog.make_embed(self, description=description), view=self)
                return

            if not correct:
                self.finished = True
                self.disable_controls()
                await self.cog.finish(self, "loss", 0)
                description = (
                    f"{format_card(previous)} → {format_card(next_card)}\n\n"
                    f"💸 **{next_card[:-1]} was not {guess}. You lost {self.bet:,}.**"
                )
                await interaction.message.edit(
                    embed=self.cog.make_embed(self, description=description, color=discord.Color.red()), view=self
                )
                self.cog.active_players.discard(self.ctx.author.id)
                self.stop()
                return

            self.streak += 1
            self.current_payout = await self.cog.calculate_payout(self.ctx, self.bet, self.streak)
            await self.refresh_cashout()
            if self.streak >= MAX_STREAK:
                self.finished = True
                self.disable_controls()
                result = await self.cog.finish(self, "win", self.current_payout)
                description = (
                    f"{format_card(previous)} → {format_card(next_card)}\n\n"
                    f"🏆 **Seven correct guesses! Auto-cashed out for {result.deposited:,}.**"
                )
                await interaction.message.edit(
                    embed=self.cog.make_embed(self, description=description, color=discord.Color.green()), view=self
                )
                self.cog.active_players.discard(self.ctx.author.id)
                self.stop()
                return

            description = (
                f"{format_card(previous)} → {format_card(next_card)}\n\n"
                f"✅ **Correct!** Cash out **{self.current_payout:,}** or continue."
            )
            await interaction.message.edit(
                embed=self.cog.make_embed(self, description=description, color=discord.Color.green()), view=self
            )

    @discord.ui.button(label="Higher", emoji="⬆️", style=discord.ButtonStyle.primary)
    async def higher(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.resolve_guess(interaction, "higher")

    @discord.ui.button(label="Lower", emoji="⬇️", style=discord.ButtonStyle.primary)
    async def lower(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.resolve_guess(interaction, "lower")

    @discord.ui.button(label="Cash Out", emoji="💰", style=discord.ButtonStyle.success, disabled=True)
    async def cash_out(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        async with self.lock:
            if self.finished or not self.streak:
                return
            await interaction.response.defer()
            self.finished = True
            self.disable_controls()
            result = await self.cog.finish(self, "win", self.current_payout)
            description = f"💰 **Cashed out after {self.streak} correct guess{'es' if self.streak != 1 else ''} for {result.deposited:,}.**"
            await interaction.message.edit(
                embed=self.cog.make_embed(self, description=description, color=discord.Color.green()), view=self
            )
            self.cog.active_players.discard(self.ctx.author.id)
            self.stop()

    async def refresh_cashout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.label == "Cash Out":
                item.disabled = self.streak == 0


class HighLow(commands.Cog):
    """Push-your-luck card High/Low with a configurable payout cap."""

    def __init__(self, bot):
        self.bot = bot
        self.active_players: set[int] = set()

    async def calculate_payout(self, ctx: commands.Context, bet: int, streak: int) -> int:
        payout = int(bet * MULTIPLIERS[streak - 1])
        settings = await get_game_settings(ctx.guild, GAME)
        cap = max(0, int(settings.get("payout_cap", 0)))
        return min(payout, cap) if cap else payout

    async def finish(self, view: HighLowView, outcome: str, payout: int):
        return await settle_game(
            view.ctx.author, GAME, view.bet, payout, outcome,
            metadata={"highlow_streak": view.streak}, channel=view.ctx.channel
        )

    def make_embed(
        self,
        view: HighLowView,
        *,
        description: str = "Choose whether the next card will be higher or lower.",
        color: discord.Color = discord.Color.gold(),
    ) -> discord.Embed:
        multiplier = MULTIPLIERS[view.streak - 1] if view.streak else 0
        return_amount = f"{view.current_payout:,}" if view.streak else "—"
        embed = discord.Embed(title="🃏 High/Low", description=description, color=color)
        embed.add_field(name="Current Card", value=format_card(view.current), inline=False)
        embed.add_field(name="Wager", value=f"**{view.bet:,}**", inline=True)
        embed.add_field(name="Streak", value=f"**{view.streak}/{MAX_STREAK}**", inline=True)
        embed.add_field(
            name="Cash Out",
            value=f"**{return_amount}**" + (f" ({multiplier:.2f}× ladder)" if view.streak else ""),
            inline=True,
        )
        embed.set_footer(text="Equal ranks are a push and do not advance the streak.")
        return embed

    @commands.command(name="highlow", aliases=["hl", "higherlower"])
    @commands.guild_only()
    @commands.max_concurrency(1, per=commands.BucketType.user, wait=False)
    async def highlow(self, ctx: commands.Context, bet: int):
        """Start a seven-step card High/Low game."""
        if ctx.author.id in self.active_players:
            return await ctx.send("You already have a High/Low game in progress.")
        error = await validate_bet(ctx, GAME, bet)
        if error:
            return await ctx.send(error)

        self.active_players.add(ctx.author.id)
        withdrawn = False
        view: Optional[HighLowView] = None
        try:
            await bank.withdraw_credits(ctx.author, bet)
            withdrawn = True
            mark_played(ctx.guild.id, ctx.author.id, GAME)
            deck = make_deck()
            random.shuffle(deck)
            current = deck.pop()
            view = HighLowView(self, ctx, bet, deck, current)
            message = await ctx.send(embed=self.make_embed(view), view=view)
            view.message = message
        except Exception:
            LOG.exception("Failed to start High/Low for user %s", ctx.author.id)
            if withdrawn:
                await refund_wager(ctx.author, GAME, bet, reason="High/Low failed before interaction began")
            self.active_players.discard(ctx.author.id)
            await ctx.send("High/Low could not start. Any withdrawn wager was refunded.")

    @commands.command(name="highlowpayouts", aliases=["hlpayouts"])
    async def highlowpayouts(self, ctx: commands.Context):
        lines = [f"**{index} correct:** {multiplier:.2f}× total return" for index, multiplier in enumerate(MULTIPLIERS, 1)]
        settings = await get_game_settings(ctx.guild, GAME) if ctx.guild else {"payout_cap": 0}
        cap = settings.get("payout_cap", 0)
        lines.append(f"\n**Configured payout cap:** {cap:,}" if cap else "\n**Configured payout cap:** Unlimited")
        await ctx.send(embed=discord.Embed(title="🃏 High/Low Payout Ladder", description="\n".join(lines), color=discord.Color.gold()))


async def setup(bot):
    await bot.add_cog(HighLow(bot))
