from .blackjack import Blackjack
from .coinflip import CoinFlip

async def setup(bot):
    await bot.add_cog(Blackjack())
    await bot.add_cog(CoinFlip(bot))
