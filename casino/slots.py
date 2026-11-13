import asyncio
import logging
import random
from typing import Optional

import discord
from redbot.core import bank, commands

from .casino_core import currency, mark_played, refund_wager, reply, resolve_bet, settle_game, validate_bet
from .ui import GameOverView

GAME = "slots"
LOG = logging.getLogger("red.crewcogs.casino.slots")
REEL = {"🍒": 25, "🍋": 20, "🍊": 18, "🍇": 14, "🔔": 10, "⭐": 7, "💎": 4, "7️⃣": 2}
TRIPLE_PAYOUTS = {"🍒": 5, "🍋": 6, "🍊": 8, "🍇": 10, "🔔": 15, "⭐": 25, "💎": 50, "7️⃣": 100}
PAIR_PAYOUT = 1.5
SYMBOLS = list(REEL)
WEIGHTS = list(REEL.values())
BLUR = ("🍒", "🍋", "🔔", "⭐", "🍇", "💎")


def spin_reels():
    return random.choices(SYMBOLS, weights=WEIGHTS, k=3)


def evaluate(reels, bet):
    a, b, c = reels
    if a == b == c:
        return int(bet * TRIPLE_PAYOUTS[a]), "triple"
    if a == b or b == c or a == c:
        return int(bet * PAIR_PAYOUT), "pair"
    return 0, "none"


def machine(shown) -> str:
    return f"╔════════════╗\n║  {' │ '.join(shown)}  ║\n╚════════════╝"


class Slots(commands.Cog):
    """Slot machine."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="slots")
    @commands.guild_only()
    async def slots(self, ctx: commands.Context, bet: str):
        """Spin the slot machine. Usage: `slots <bet>` — try `slots 500`, `slots half`, `slots all`."""
        await self.start(ctx, bet)

    async def start(self, ctx: commands.Context, raw_bet, interaction: Optional[discord.Interaction] = None) -> None:
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.defer()
        bet, err = await resolve_bet(ctx.author, GAME, raw_bet)
        if err:
            await reply(ctx, interaction, err)
            return
        error = await validate_bet(ctx, GAME, bet)
        if error:
            await reply(ctx, interaction, error)
            return
        try:
            await bank.withdraw_credits(ctx.author, bet)
            mark_played(ctx.guild.id, ctx.author.id, GAME)
        except ValueError:
            await reply(ctx, interaction, "Your balance changed — try again.")
            return

        settled = False
        try:
            cur = await currency(ctx.guild)
            reels = spin_reels()
            shown = [random.choice(BLUR) for _ in range(3)]
            embed = discord.Embed(title="🎰 Slots", color=0x9B59B6,
                                  description=f"**{ctx.author.display_name}** bets **{bet:,}**\n```\n{machine(shown)}\n```")
            message = await ctx.send(embed=embed)
            for index in range(3):
                await asyncio.sleep(0.6)
                shown[index] = reels[index]
                for j in range(index + 1, 3):
                    shown[j] = random.choice(BLUR)
                embed.description = f"**{ctx.author.display_name}** bets **{bet:,}**\n```\n{machine(shown)}\n```"
                await message.edit(embed=embed)

            payout, kind = evaluate(reels, bet)
            settlement = await settle_game(ctx.author, GAME, bet, payout, "win" if payout > bet else ("push" if payout == bet else "loss"),
                                           channel=ctx.channel)
            settled = True
            net = settlement.deposited - bet
            if kind == "triple" and reels[0] == "7️⃣":
                headline, color = f"🎰 **JACKPOT! 777!** {net:+,} {cur}", 0xFFD700
            elif kind == "triple":
                headline, color = f"🎉 **Three {reels[0]}!** {net:+,} {cur}", 0x2ECC71
            elif kind == "pair":
                headline, color = f"✨ **A pair!** {net:+,} {cur}", 0x2ECC71
            else:
                headline, color = f"💸 No match. {net:+,} {cur}", 0xE74C3C
            if settlement.capped:
                headline += "\n_Payout limited by the bank balance cap._"
            balance = await bank.get_balance(ctx.author)
            embed.description = f"```\n{machine(reels)}\n```\n{headline}"
            embed.color = color
            embed.set_footer(text=f"Bet {bet:,} · Balance {balance:,} {cur} · 3 alike up to 100× · any pair 1.5×")
            view = GameOverView(ctx.author.id, bet, lambda i, b: self.start(ctx, b, i), again_label="Spin")
            await message.edit(embed=embed, view=view)
            view.message = message
        except Exception:
            if not settled:
                await refund_wager(ctx.author, GAME, bet, reason="command failure before settlement")
            LOG.exception("Slots failed for user %s", ctx.author.id)
            await ctx.send("Slots hit an error. " + ("Your result was recorded." if settled else "Your wager was refunded."))


async def setup(bot):
    await bot.add_cog(Slots(bot))
