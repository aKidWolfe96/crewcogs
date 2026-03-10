from .retailtracker import RetailTracker

async def setup(bot):
    await bot.add_cog(RetailTracker(bot))
