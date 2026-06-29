from .rlstats import RLStats


async def setup(bot):
    await bot.add_cog(RLStats(bot))
