from .audioplayer import AudioPlayer

async def setup(bot):
    await bot.add_cog(AudioPlayer(bot))
