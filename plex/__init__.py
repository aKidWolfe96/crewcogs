from redbot.core import bot, commands
from .plex import Plex

__red_end_user_data_statement__ = "This cog does not store any end user data."

def setup(bot: bot.Red):
    bot.add_cog(Plex(bot))
