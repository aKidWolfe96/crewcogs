import asyncio
import random

from discord import Embed
from redbot.core import Config, bank, commands

from .casino_core import mark_played, record_game, safe_deposit, validate_bet

SLOT_CONFIG_ID = 5557771234
REEL = {"🍒": 25, "🍋": 20, "🍊": 18, "🍇": 14, "🔔": 10, "⭐": 7, "💎": 4, "7️⃣": 2}
TRIPLE_PAYOUTS = {"🍒": 5, "🍋": 6, "🍊": 8, "🍇": 10, "🔔": 15, "⭐": 25, "💎": 50, "7️⃣": 100}
PAIR_PAYOUT = 1.5
SYMBOLS = list(REEL)
WEIGHTS = list(REEL.values())


class Slots(commands.Cog):
    """Slot machine casino game using Red economy."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(None, identifier=SLOT_CONFIG_ID, force_registration=True)
        self.config.register_user(total_slot_wins=0, total_slot_losses=0, total_slot_bet=0, biggest_slot_win=0)

    @staticmethod
    def spin_reels():
        return [random.choices(SYMBOLS, weights=WEIGHTS, k=1)[0] for _ in range(3)]

    @staticmethod
    def evaluate(reels, bet):
        a, b, c = reels
        if a == b == c:
            return int(bet * TRIPLE_PAYOUTS[a]), "triple"
        if a == b or b == c or a == c:
            return int(bet * PAIR_PAYOUT), "pair"
        return 0, "none"

    @commands.command()
    async def slots(self, ctx, bet: int):
        """Spin the slot machine for CrewCoin."""
        error = await validate_bet(ctx, "slots", bet)
        if error:
            return await ctx.send(error)
        try:
            await bank.withdraw_credits(ctx.author, bet)
            mark_played(ctx.guild.id, ctx.author.id, "slots")
        except ValueError:
            return await ctx.send("Your balance changed before the wager could be placed. Try again.")

        reels = self.spin_reels()
        embed = Embed(title="🎰 Slots", description=f"Bet: **{bet:,}** CrewCoin")
        embed.add_field(name="Reels", value="[ ❓ | ❓ | ❓ ]", inline=False)
        message = await ctx.send(embed=embed)
        shown = ["❓", "❓", "❓"]
        for index in range(3):
            await asyncio.sleep(0.8)
            shown[index] = reels[index]
            embed.set_field_at(0, name="Reels", value=f"[ {' | '.join(shown)} ]", inline=False)
            await message.edit(embed=embed)

        payout, kind = self.evaluate(reels, bet)
        deposited = await safe_deposit(ctx.author, payout)
        cfg = self.config.user(ctx.author)
        if payout:
            net = deposited - bet
            if kind == "triple" and reels[0] == "7️⃣":
                outcome = f"🎰 **JACKPOT!** Returned **{deposited:,}** CrewCoin (net {net:+,})."
            elif kind == "triple":
                outcome = f"🎉 Three {reels[0]}! Returned **{deposited:,}** CrewCoin (net {net:+,})."
            else:
                outcome = f"✨ A pair! Returned **{deposited:,}** CrewCoin (net {net:+,})."
            await cfg.total_slot_wins.set(await cfg.total_slot_wins() + 1)
            if deposited > await cfg.biggest_slot_win():
                await cfg.biggest_slot_win.set(deposited)
            result = "win"
        else:
            outcome = f"💸 No match. You lost **{bet:,}** CrewCoin."
            await cfg.total_slot_losses.set(await cfg.total_slot_losses() + 1)
            result = "loss"
        await cfg.total_slot_bet.set(await cfg.total_slot_bet() + bet)
        await record_game(ctx.author, "slots", bet, deposited, result)
        embed.add_field(name="Outcome", value=outcome, inline=False)
        await message.edit(embed=embed)

    @commands.command()
    async def slotstats(self, ctx):
        data = await self.config.user(ctx.author).all()
        await ctx.send(f"Slots — Wins: {data['total_slot_wins']}, Losses: {data['total_slot_losses']}, Bet total: {data['total_slot_bet']:,}, Biggest win: {data['biggest_slot_win']:,}")

    @commands.command()
    async def slotpayouts(self, ctx):
        lines = ["**Three of a kind** (total return):"]
        lines.extend(f"{sym} {sym} {sym} → {mult}x" for sym, mult in TRIPLE_PAYOUTS.items())
        lines.append(f"\n**Any two matching** → {PAIR_PAYOUT}x")
        await ctx.send(embed=Embed(title="🎰 Slots Payouts", description="\n".join(lines), color=0xFFD700))


async def setup(bot):
    await bot.add_cog(Slots(bot))
