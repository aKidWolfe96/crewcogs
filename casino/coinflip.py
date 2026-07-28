import logging
import os
import random

from discord import Embed, File
from redbot.core import Config, bank, commands

from .casino_core import mark_played, refund_wager, settle_game, validate_bet


class CoinFlip(commands.Cog):
    """Coin Flip casino game using Red economy."""

    def __init__(self, bot):
        self.bot = bot
        self.CONFIG = Config.get_conf(None, identifier=9876543210)
        self.CONFIG.register_user(total_cf_wins=0, total_cf_losses=0, total_cf_bet=0)

    @commands.command()
    @commands.max_concurrency(1, per=commands.BucketType.user, wait=False)
    async def coinflip(self, ctx, side: str, bet: int):
        """Bet on heads or tails."""
        side = side.lower()
        if side not in ("heads", "tails"):
            return await ctx.send("Choose either `heads` or `tails`.")

        error = await validate_bet(ctx, "coinflip", bet)
        if error:
            return await ctx.send(error)

        try:
            await bank.withdraw_credits(ctx.author, bet)
            mark_played(ctx.guild.id, ctx.author.id, "coinflip")
        except ValueError:
            return await ctx.send("Your balance changed before the wager could be placed. Try again.")

        settled = False
        try:
            result = random.choice(("heads", "tails"))
            won = result == side
            payout = bet * 2 if won else 0
            settlement = await settle_game(
                ctx.author, "coinflip", bet, payout, "win" if won else "loss"
            )
            settled = True

            cfg = self.CONFIG.user(ctx.author)
            await cfg.total_cf_bet.set(await cfg.total_cf_bet() + bet)
            if won:
                await cfg.total_cf_wins.set(await cfg.total_cf_wins() + 1)
            else:
                await cfg.total_cf_losses.set(await cfg.total_cf_losses() + 1)

            image_path = os.path.join(os.path.dirname(__file__), "cards", f"{result}.png")
            file = File(image_path, filename="coin.png")
            embed = Embed(title="🪙 Coin Flip", description=f"You bet **{bet:,}** on **{side.title()}**.")
            embed.add_field(name="Result", value=f"**{result.title()}**", inline=False)
            if won:
                text = (
                    f"🎉 Returned **{settlement.deposited:,}** CrewCoin "
                    f"(net **{settlement.deposited - bet:+,}**)."
                )
                if settlement.capped:
                    text += " Your payout was limited by the bank balance cap."
            else:
                text = f"💸 You lost **{bet:,}** CrewCoin."
            embed.add_field(name="Outcome", value=text, inline=False)
            embed.set_image(url="attachment://coin.png")
            await ctx.send(embed=embed, file=file)
        except Exception:
            if not settled:
                await refund_wager(ctx.author, "coinflip", bet, reason="command failure before settlement")
            logging.getLogger("red.crewcogs.casino.coinflip").exception(
                "Coin Flip failed for user %s", ctx.author.id
            )
            await ctx.send(
                "Coin Flip settled, but the result message or legacy stats failed."
                if settled
                else "Coin Flip hit an unexpected error. The unsettled wager was refunded."
            )

    @commands.command()
    async def cfstats(self, ctx):
        """Show your legacy coinflip stats."""
        data = await self.CONFIG.user(ctx.author).all()
        await ctx.send(f"Coinflip Wins: {data['total_cf_wins']}, Losses: {data['total_cf_losses']}, Bet total: {data['total_cf_bet']:,}")


async def setup(bot):
    await bot.add_cog(CoinFlip(bot))
