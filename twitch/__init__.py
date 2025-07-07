from .twitch import TwitchWebhook as Twitch

async def setup(bot):
    await bot.add_cog(Twitch(bot))
