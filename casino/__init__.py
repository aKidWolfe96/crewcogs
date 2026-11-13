from .blackjack import Blackjack
from .casino import Casino
from .coinflip import CoinFlip
from .crash import Crash
from .dailyspin import DailySpin
from .highlow import HighLow
from .mines import Mines
from .roulette import Roulette
from .slots import Slots

__red_end_user_data_statement__ = (
    "The casino stores per-server game statistics, achievements, challenge progress and reward "
    "cooldowns keyed by Discord user ID. Credits live in Red's bank. No message content is stored."
)


async def setup(bot):
    await bot.add_cog(Casino(bot))
    await bot.add_cog(Blackjack())
    await bot.add_cog(CoinFlip(bot))
    await bot.add_cog(Slots(bot))
    await bot.add_cog(Roulette(bot))
    await bot.add_cog(HighLow(bot))
    await bot.add_cog(Crash(bot))
    await bot.add_cog(Mines(bot))
    await bot.add_cog(DailySpin(bot))
