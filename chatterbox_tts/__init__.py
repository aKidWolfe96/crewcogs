from .tts import ChatterboxTTS

__red_end_user_data_statement__ = "This cog does not store any data about users."


async def setup(bot):
    await bot.add_cog(ChatterboxTTS(bot))
