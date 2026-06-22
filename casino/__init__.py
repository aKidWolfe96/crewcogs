from .blackjack import Blackjack
from .coinflip import CoinFlip
from .leaderboard import CasinoLeaderboard
from .dailyspin import DailySpin
from .horserace import HorseRace
from .slots import Slots

async def setup(bot):
    await bot.add_cog(Blackjack())
    await bot.add_cog(CoinFlip(bot))
    await bot.add_cog(CasinoLeaderboard(bot))
    await bot.add_cog(DailySpin(bot))
    await bot.add_cog(HorseRace(bot))
    await bot.add_cog(Slots(bot))
