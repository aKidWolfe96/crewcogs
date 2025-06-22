from redbot.core import commands, Config
from discord import Embed

class CasinoLeaderboard(commands.Cog):
    """Leaderboard for all casino games combined."""

    def __init__(self, bot):
        self.bot = bot
        self.cf_config = Config.get_conf(None, identifier=9876543210, force_registration=True)
        self.cf_config.register_user(total_cf_wins=0, total_cf_losses=0, total_cf_bet=0)

        self.bj_config = Config.get_conf(None, identifier=1234567890, force_registration=True)
        self.bj_config.register_user(total_wins=0, total_losses=0, total_bet=0)

    @commands.command()
    async def casinoboard(self, ctx):
        """Show a server-wide casino leaderboard."""
        users = ctx.guild.members
        leaderboard = []

        for user in users:
            if user.bot:
                continue
            cf = await self.cf_config.user(user).all()
            bj = await self.bj_config.user(user).all()
            total_bet = cf['total_cf_bet'] + bj['total_bet']
            total_wins = cf['total_cf_wins'] + bj['total_wins']
            total_losses = cf['total_cf_losses'] + bj['total_losses']
            if total_bet == 0:
                continue
            leaderboard.append((user.display_name, total_bet, total_wins, total_losses))

        leaderboard.sort(key=lambda x: x[1], reverse=True)
        top = leaderboard[:10]

        embed = Embed(title="🎰 Casino Leaderboard", description="Top 10 high rollers by total bet", color=0xFFD700)
        for i, (name, bet, wins, losses) in enumerate(top, 1):
            embed.add_field(
                name=f"#{i} - {name}",
                value=f"💰 Bet: {bet} CrewCoin\n✅ Wins: {wins} | ❌ Losses: {losses}",
                inline=False
            )

        if not top:
            embed.description = "No bets placed yet. Be the first to play!"

        await ctx.send(embed=embed)

def setup(bot):
    bot.add_cog(CasinoLeaderboard(bot))
