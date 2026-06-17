from .ufc import UFC


async def setup(bot):
    await bot.add_cog(UFC(bot))
