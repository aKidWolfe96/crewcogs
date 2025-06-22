from redbot.core import commands, bank
import random
import os
from discord import File, Embed

from redbot.core import Config

class CoinFlip(commands.Cog):
    """Coin Flip casino game using Red economy."""

    def __init__(self, bot):
        self.bot = bot
        self.CONFIG = Config.get_conf(self, identifier=9876543210)
        self.CONFIG.register_user(total_cf_wins=0, total_cf_losses=0, total_cf_bet=0)

    @commands.command()
    async def coinflip(self, ctx, side: str, bet: int):
        """Bet on heads or tails."""
        side = side.lower()
        if side not in ["heads", "tails"]:
            return await ctx.send("Choose either 'heads' or 'tails'.")

        balance = await bank.get_balance(ctx.author)
        if bet <= 0:
            return await ctx.send("Bet must be positive.")
        if bet > balance:
            return await ctx.send("Not enough CrewCoin.")

        await bank.withdraw_credits(ctx.author, bet)
        result = random.choice(["heads", "tails"])
        win = result == side

        image_path = os.path.join(os.path.dirname(__file__), "cards", f"{result}.png")
        file = File(image_path, filename="coin.png")

        e = Embed(title="🪙 Coin Flip", description=f"You bet on **{side.capitalize()}**.")
        e.add_field(name="Result", value=f"**{result.capitalize()}**", inline=False)
        if win:
            winnings = bet * 2
            await bank.deposit_credits(ctx.author, winnings)
            e.add_field(name="Outcome", value=f"🎉 You won {winnings} CrewCoin!", inline=False)
        else:
            e.add_field(name="Outcome", value=f"💸 You lost {bet} CrewCoin.", inline=False)
        e.set_image(url="attachment://coin.png")

        await ctx.send(embed=e, file=file)

        await self.CONFIG.user(ctx.author).total_cf_bet.set(await self.CONFIG.user(ctx.author).total_cf_bet() + bet)
        if win:
            await self.CONFIG.user(ctx.author).total_cf_wins.set(await self.CONFIG.user(ctx.author).total_cf_wins() + 1)
        else:
            await self.CONFIG.user(ctx.author).total_cf_losses.set(await self.CONFIG.user(ctx.author).total_cf_losses() + 1)

    @commands.command()
    async def cfstats(self, ctx):
        """Show your coinflip stats."""
        data = await self.CONFIG.user(ctx.author).all()
        await ctx.send(
            f"Coinflip Wins: {data['total_cf_wins']}, Losses: {data['total_cf_losses']}, Bet total: {data['total_cf_bet']}"
        )


def setup(bot):
    bot.add_cog(CoinFlip(bot))
