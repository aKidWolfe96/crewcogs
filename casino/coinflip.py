from redbot.core import commands, bank
import random
import os
from discord import File, Embed

class CoinFlip(commands.Cog):
    """Coin Flip casino game using Red economy."""

    def __init__(self, bot):
        self.bot = bot

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

def setup(bot):
    bot.add_cog(CoinFlip(bot))
