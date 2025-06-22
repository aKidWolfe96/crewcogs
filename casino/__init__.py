from .blackjack import Blackjack
from .coinflip import CoinFlip
from .leaderboard import CasinoLeaderboard

async def setup(bot):
    await bot.add_cog(Blackjack())
    await bot.add_cog(CoinFlip(bot))
    await bot.add_cog(CasinoLeaderboard(bot))
