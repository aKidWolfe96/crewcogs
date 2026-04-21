from .tts_cog import ChatterboxTTS

async def setup(bot):
    await bot.add_cog(ChatterboxTTS(bot))
