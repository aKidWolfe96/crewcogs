from .dailyverse import DailyVerse

__red_end_user_data_statement__ = (
    "This cog stores the verses you save as favorites and whether you've asked to get the daily verse "
    "in your DMs, keyed by your Discord user ID."
)


async def setup(bot):
    await bot.add_cog(DailyVerse(bot))
