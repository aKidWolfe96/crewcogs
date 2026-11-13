import asyncio
import logging
import os
import random
from typing import Optional

import discord
from redbot.core import bank, commands

from .casino_core import currency, mark_played, refund_wager, reply, resolve_bet, settle_game, validate_bet
from .ui import ChoiceView, GameOverView

GAME = "coinflip"
LOG = logging.getLogger("red.crewcogs.casino.coinflip")
SIDES = {"heads": "heads", "h": "heads", "head": "heads", "tails": "tails", "t": "tails", "tail": "tails"}


class CoinFlip(commands.Cog):
    """Coin Flip — call it in the air."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="coinflip", aliases=["cf"])
    @commands.guild_only()
    async def coinflip(self, ctx: commands.Context, bet: str, side: Optional[str] = None):
        """Flip a coin for double your bet. Usage: `cf <bet> [heads|tails]`

        Leave out the side to pick it with a button. Bets can be `500`, `2.5k`, `half`, `all`.
        """
        if bet.lower() in SIDES:  # old order: coinflip heads 100
            bet, side = side or "", bet
        await self.start(ctx, bet, side)

    async def start(self, ctx: commands.Context, raw_bet, side: Optional[str] = None,
                    interaction: Optional[discord.Interaction] = None) -> None:
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.defer()
        bet, err = await resolve_bet(ctx.author, GAME, raw_bet)
        if err:
            await reply(ctx, interaction, err)
            return
        if side is None:
            view = ChoiceView(
                ctx.author.id,
                [("Heads", "🙂", discord.ButtonStyle.primary, "heads"), ("Tails", "🪙", discord.ButtonStyle.secondary, "tails")],
                lambda i, value: self.start(ctx, bet, value, i),
            )
            embed = discord.Embed(title="🪙 Coin Flip", description=f"Betting **{bet:,}** — call it!", color=0xF1C40F)
            view.message = await ctx.send(embed=embed, view=view)
            return
        side = SIDES.get(side.lower())
        if side is None:
            await reply(ctx, interaction, "Pick `heads` or `tails`.")
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

        settled = False
        try:
            cur = await currency(ctx.guild)
            msg = await ctx.send(embed=discord.Embed(
                title="🪙 Coin Flip", color=0xF1C40F,
                description=f"**{ctx.author.display_name}** bets **{bet:,}** on **{side.title()}**…\n\n🪙 *flipping…*",
            ))
            await asyncio.sleep(1.2)
            result = random.choice(("heads", "tails"))
            won = result == side
            settlement = await settle_game(ctx.author, GAME, bet, bet * 2 if won else 0,
                                           "win" if won else "loss", channel=ctx.channel)
            settled = True
            if won:
                text = f"🎉 **{result.title()}!** You win **{settlement.deposited - bet:+,} {cur}**."
                if settlement.capped:
                    text += "\n_Payout limited by the bank balance cap._"
            else:
                text = f"💸 **{result.title()}.** You lose **{bet:,} {cur}**."
            balance = await bank.get_balance(ctx.author)
            embed = discord.Embed(title="🪙 Coin Flip", description=text, color=0x2ECC71 if won else 0xE74C3C)
            embed.set_footer(text=f"Balance: {balance:,} {cur}")
            image_path = os.path.join(os.path.dirname(__file__), "cards", f"{result}.png")
            file = discord.File(image_path, filename="coin.png")
            embed.set_thumbnail(url="attachment://coin.png")
            other = "tails" if side == "heads" else "heads"
            view = GameOverView(
                ctx.author.id, bet, lambda i, b: self.start(ctx, b, side, i),
                extras=[(f"Switch to {other.title()}", "🔄", discord.ButtonStyle.secondary,
                         lambda i: self.start(ctx, bet, other, i))],
            )
            await msg.edit(embed=embed, attachments=[file], view=view)
            view.message = msg
        except Exception:
            if not settled:
                await refund_wager(ctx.author, GAME, bet, reason="command failure before settlement")
            LOG.exception("Coin Flip failed for user %s", ctx.author.id)
            await ctx.send("Coin Flip hit an error. " + ("Your result was recorded." if settled else "Your wager was refunded."))


async def setup(bot):
    await bot.add_cog(CoinFlip(bot))
