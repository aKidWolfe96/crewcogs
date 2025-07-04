from .twitch import Twitch

async def setup(bot):
    await bot.add_cog(Twitch(bot))
