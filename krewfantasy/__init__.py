from .krewfantasy import KrewFantasy

__red_end_user_data_statement__ = (
    "KrewFantasy stores per-server Sleeper league settings, Discord user IDs explicitly linked "
    "to Sleeper rosters, report preferences, and derived fantasy-football history. It does not "
    "store message content or authentication credentials."
)


async def setup(bot):
    await bot.add_cog(KrewFantasy(bot))
