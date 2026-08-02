import random
import time
from pathlib import Path

import discord
from redbot.core import bank, commands

from .casino_core import CONFIG, safe_deposit, settle_game


class DailySpin(commands.Cog):
    """Daily reward with an optional higher-or-lower dice gamble."""

    def __init__(self, bot):
        self.bot = bot
        self.dice_path = Path(__file__).parent / "dice"

    @commands.command()
    @commands.max_concurrency(1, per=commands.BucketType.user, wait=False)
    @commands.guild_only()
    async def dailyspin(self, ctx: commands.Context):
        """Claim daily CrewCoin or risk it in a higher/lower dice game."""
        now = time.time()
        last = float(await CONFIG.member(ctx.author).daily_spin_at())
        cooldown = 86400
        remaining = int(cooldown - (now - last))
        if remaining > 0:
            retry_timestamp = int(now + remaining)
            return await ctx.send(
                f"🕒 You already claimed your daily spin. Try again <t:{retry_timestamp}:R>."
            )

        # Start the cooldown as soon as the offer is generated, matching the old
        # command cooldown behavior even if the player lets the offer expire.
        await CONFIG.member(ctx.author).daily_spin_at.set(now)
        amount = random.randint(100, 1000)
        await ctx.send(
            f"🎲 **Ruthless Dealer Daily Spin**\nYou earned **{amount:,} CrewCoin**!\n"
            "Ruthless Dealer offers: type `accept` to claim it or `risk` to gamble it in a **Higher or Lower** dice roll."
        )

        def accept_check(message):
            return (
                message.author == ctx.author
                and message.channel == ctx.channel
                and message.content.lower() in {"accept", "risk"}
            )

        try:
            choice = await self.bot.wait_for("message", timeout=30, check=accept_check)
        except TimeoutError:
            return await ctx.send("⏰ Ruthless Dealer closed the offer. No reward was given.")

        if choice.content.lower() == "accept":
            deposited = await safe_deposit(ctx.author, amount)
            return await ctx.send(
                f"✅ Ruthless Dealer paid you **{deposited:,} CrewCoin**."
            )

        first = random.randint(1, 6)
        first_file = discord.File(self.dice_path / f"{first}.png", filename="first.png")
        await ctx.send(
            file=first_file,
            content=f"🎲 **Ruthless Dealer Gamble**\nFirst roll: **{first}**\nGuess: `higher` or `lower`?",
        )

        def guess_check(message):
            return (
                message.author == ctx.author
                and message.channel == ctx.channel
                and message.content.lower() in {"higher", "lower"}
            )

        try:
            guess = await self.bot.wait_for("message", timeout=20, check=guess_check)
        except TimeoutError:
            return await ctx.send("⏰ Ruthless Dealer closed the offer. No reward was given.")

        second = random.randint(1, 6)
        second_file = discord.File(self.dice_path / f"{second}.png", filename="second.png")
        await ctx.send(file=second_file, content=f"🎲 Second roll: **{second}**")

        if second == first:
            settlement = await settle_game(
                ctx.author,
                "dailyspin",
                wager=amount,
                payout=amount,
                outcome="push",
                include_economy=False,
                channel=ctx.channel,
            )
            text = f"😐 It's a tie! Your **{settlement.deposited:,} CrewCoin** reward was returned."
            if settlement.capped:
                text += " The bank balance cap limited the deposit."
            return await ctx.send(text)

        correct = (
            guess.content.lower() == "higher" and second > first
        ) or (
            guess.content.lower() == "lower" and second < first
        )

        if correct:
            payout = amount * 2
            settlement = await settle_game(
                ctx.author,
                "dailyspin",
                wager=amount,
                payout=payout,
                outcome="win",
                include_economy=False,
                channel=ctx.channel,
            )
            text = f"🔥 You won the gamble! **{settlement.deposited:,} CrewCoin** was added to your balance."
            if settlement.capped:
                text += " The bank balance cap limited the deposit."
            await ctx.send(text)
        else:
            await settle_game(
                ctx.author,
                "dailyspin",
                wager=amount,
                payout=0,
                outcome="loss",
                include_economy=False,
                channel=ctx.channel,
            )
            await ctx.send("💀 Ruthless Dealer wins. Your reward was forfeited.")
