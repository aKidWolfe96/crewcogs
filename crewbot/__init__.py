from .crewbot import CrewBot
from .imagine import Imagine

async def setup(bot):
    await bot.add_cog(CrewBot(bot))
    await bot.add_cog(Imagine(bot))

