from .plexstream import PlexStream

async def setup(bot):
    await bot.add_cog(PlexStream(bot))
