from .audioplayer import AudioPlayer

def setup(bot):
    bot.add_cog(AudioPlayer(bot))
