from .restockmonitor import RestockMonitor

__red_end_user_data_statement__ = (
    "This cog stores product links, store links and alert settings per server, plus the "
    "Discord user ID of whoever added each link. It does not store message content."
)


async def setup(bot):
    await bot.add_cog(RestockMonitor(bot))
