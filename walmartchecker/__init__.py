from .walmartchecker import WalmartChecker

async def setup(bot):
    await bot.add_cog(WalmartChecker(bot))
