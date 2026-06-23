from .overwatch import Overwatch


async def setup(bot):
    await bot.add_cog(Overwatch(bot))
