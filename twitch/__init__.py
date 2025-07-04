from .twitch import Twitch  # or from twitch import Twitch for single file

async def setup(bot):
    await bot.add_cog(Twitch(bot))
