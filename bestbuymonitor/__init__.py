from .bestbuymonitor import BestBuyMonitor

async def setup(bot):
    await bot.add_cog(BestBuyMonitor(bot))
