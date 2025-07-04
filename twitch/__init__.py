from .twitch import Twitch  # if using folder, otherwise from twitch import Twitch

def setup(bot):
    bot.add_cog(Twitch(bot))
