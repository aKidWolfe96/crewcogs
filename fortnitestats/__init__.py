from .fortnitestats import FortniteStats


async def setup(bot):
    await bot.add_cog(FortniteStats(bot))
