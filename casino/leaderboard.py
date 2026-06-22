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

        self.slot_config = Config.get_conf(None, identifier=5557771234, force_registration=True)
        self.slot_config.register_user(
            total_slot_wins=0, total_slot_losses=0, total_slot_bet=0, biggest_slot_win=0
        )

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
            sl = await self.slot_config.user(user).all()

            total_bet = cf['total_cf_bet'] + bj['total_bet'] + sl['total_slot_bet']
            if total_bet == 0:
                continue

            leaderboard.append((user.display_name, cf, bj, sl, total_bet))

        leaderboard.sort(key=lambda x: x[4], reverse=True)
        top = leaderboard[:10]

        embed = Embed(title="🎰 Casino Leaderboard", description="Top 10 high rollers by total bet across all games", color=0xFFD700)
        for i, (name, cf, bj, sl, _) in enumerate(top, 1):
            embed.add_field(
                name=f"#{i} - {name}",
                value=(
                    f"🎲 **Blackjack**\n"
                    f"💰 Bet: {bj['total_bet']} | ✅ Wins: {bj['total_wins']} | ❌ Losses: {bj['total_losses']}\n\n"
                    f"🪙 **Coinflip**\n"
                    f"💰 Bet: {cf['total_cf_bet']} | ✅ Wins: {cf['total_cf_wins']} | ❌ Losses: {cf['total_cf_losses']}\n\n"
                    f"🎰 **Slots**\n"
                    f"💰 Bet: {sl['total_slot_bet']} | ✅ Wins: {sl['total_slot_wins']} | ❌ Losses: {sl['total_slot_losses']}"
                ),
                inline=False
            )

        if not top:
            embed.description = "No bets placed yet. Be the first to play!"

        await ctx.send(embed=embed)

def setup(bot):
    bot.add_cog(CasinoLeaderboard(bot))
