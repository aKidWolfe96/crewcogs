from .youtubealerts import YouTubeAlerts


async def setup(bot):
    await bot.add_cog(YouTubeAlerts(bot))
