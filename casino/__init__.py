from .blackjack import Blackjack

async def setup(bot):
    await bot.add_cog(Blackjack())
