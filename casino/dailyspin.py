from redbot.core import commands

from .rewards import open_panel


class DailySpin(commands.Cog):
    """Daily Spin — now part of the Daily Rewards panel."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="dailyspin", aliases=["spin"])
    @commands.guild_only()
    async def dailyspin(self, ctx: commands.Context):
        """Open your Daily Rewards (spin, stipend, scratch ticket and bailout)."""
        await open_panel(ctx)


async def setup(bot):
    await bot.add_cog(DailySpin(bot))
