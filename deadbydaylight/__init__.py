from .dbd import DeadByDaylight

__red_end_user_data_statement__ = (
    "This cog stores which characters you've marked as Prestige 100 and your perk wishlist, "
    "keyed by your Discord user ID."
)


async def setup(bot):
    await bot.add_cog(DeadByDaylight(bot))
