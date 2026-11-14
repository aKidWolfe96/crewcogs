from .fortnitestats import FortniteStats

__red_end_user_data_statement__ = (
    "This cog stores your linked Fortnite account name/ID and your item wishlist, "
    "keyed by your Discord user ID."
)


async def setup(bot):
    await bot.add_cog(FortniteStats(bot))
