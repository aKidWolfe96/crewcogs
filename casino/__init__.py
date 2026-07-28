from .blackjack import Blackjack
from .coinflip import CoinFlip
from .casino import Casino
from .dailyspin import DailySpin
from .slots import Slots
from .roulette import Roulette
from .highlow import HighLow


async def setup(bot):
    await bot.add_cog(Blackjack())
    await bot.add_cog(CoinFlip(bot))
    await bot.add_cog(Casino(bot))
    await bot.add_cog(DailySpin(bot))
    await bot.add_cog(Slots(bot))
    await bot.add_cog(Roulette(bot))
    await bot.add_cog(HighLow(bot))
