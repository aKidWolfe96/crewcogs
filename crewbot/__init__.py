from .crewbot import CrewBot

async def setup(bot):
    await bot.add_cog(CrewBot(bot))
