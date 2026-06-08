from .twitchalerts import TwitchAlerts


async def setup(bot):
    await bot.add_cog(TwitchAlerts(bot))
