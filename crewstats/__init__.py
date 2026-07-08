from .crewstats import CrewStats


async def setup(bot):
    await bot.add_cog(CrewStats(bot))
