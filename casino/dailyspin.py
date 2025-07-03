import discord
import random
from redbot.core import commands, bank
from pathlib import Path

class DailySpin(commands.Cog):
    """Daily reward with a risky twist — higher or lower dice game."""

    def __init__(self, bot):
        self.bot = bot
        self.dice_path = Path(__file__).parent / "dice"
        self._spin_cooldowns = commands.CooldownMapping.from_cooldown(1, 86400, commands.BucketType.user)

    @commands.command()
    async def dailyspin(self, ctx: commands.Context):
        """Claim daily CrewCoin. Risk it in a higher/lower dice game to double it."""
        bucket = self._spin_cooldowns.get_bucket(ctx.message)
        retry_after = bucket.update_rate_limit()
        if retry_after:
            return await ctx.send(f"🕒 You already claimed your daily spin. Try again <t:{int(ctx.message.created_at.timestamp() + retry_after)}:R>.")

        amount = random.randint(100, 1000)
        await ctx.send(
            f"🎉 You earned **{amount} CrewCoin**!\n"
            "Type `accept` to claim or `risk` to gamble it in a **Higher or Lower** dice roll."
        )

        def accept_check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() in ["accept", "risk"]

        try:
            msg = await self.bot.wait_for("message", timeout=30, check=accept_check)
        except:
            return await ctx.send("⏰ Timed out. No reward given.")

        if msg.content.lower() == "accept":
            await bank.deposit_credits(ctx.author, amount)
            return await ctx.send(f"✅ You accepted and received **{amount} CrewCoin**.")

        # Begin dice game
        first = random.randint(1, 6)
        first_file = discord.File(self.dice_path / f"{first}.png", filename="first.png")
        await ctx.send(file=first_file, content=f"🎲 First roll: **{first}**\nGuess: `higher` or `lower`?")

        def guess_check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() in ["higher", "lower"]

        try:
            guess = await self.bot.wait_for("message", timeout=20, check=guess_check)
        except:
            return await ctx.send("⏰ Timed out. No reward given.")

        second = random.randint(1, 6)
        second_file = discord.File(self.dice_path / f"{second}.png", filename="second.png")
        await ctx.send(file=second_file, content=f"🎲 Second roll: **{second}**")

        if second == first:
            return await ctx.send("😐 It's a tie! No win, no loss.")

        correct = (
            guess.content.lower() == "higher" and second > first
        ) or (
            guess.content.lower() == "lower" and second < first
        )

        if correct:
            await bank.deposit_credits(ctx.author, amount * 2)
            await ctx.send(f"🔥 You won the gamble! **{amount * 2} CrewCoin** added to your balance.")
        else:
            await ctx.send("💀 You lost the gamble. Reward forfeited.")
