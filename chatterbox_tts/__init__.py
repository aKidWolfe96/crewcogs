from .tts import ChatterboxTTS

async def setup(bot):
    await bot.add_cog(ChatterboxTTS(bot))
