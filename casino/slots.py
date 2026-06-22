import asyncio
import random
from redbot.core import commands, bank, Config
from discord import Embed

# Unique identifier — does NOT collide with blackjack (1234567890)
# or coinflip (9876543210). The leaderboard re-opens this same id.
SLOT_CONFIG_ID = 5557771234

# Reel symbol weights (per reel). Total = 100, so each value is a %.
# Rarer symbols pay more. Tune these to change the odds.
REEL = {
    "🍒": 25,
    "🍋": 20,
    "🍊": 18,
    "🍇": 14,
    "🔔": 10,
    "⭐": 7,
    "💎": 4,
    "7️⃣": 2,
}

# Three-of-a-kind payout = multiplier of the bet (amount returned).
TRIPLE_PAYOUTS = {
    "🍒": 5,
    "🍋": 6,
    "🍊": 8,
    "🍇": 10,
    "🔔": 15,
    "⭐": 25,
    "💎": 50,
    "7️⃣": 100,  # jackpot
}

# Any two matching symbols pays this multiplier of the bet.
PAIR_PAYOUT = 1.5

SYMBOLS = list(REEL.keys())
WEIGHTS = list(REEL.values())


class Slots(commands.Cog):
    """Slot machine casino game using Red economy."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            None, identifier=SLOT_CONFIG_ID, force_registration=True
        )
        self.config.register_user(
            total_slot_wins=0,
            total_slot_losses=0,
            total_slot_bet=0,
            biggest_slot_win=0,
        )

    def spin_reels(self):
        return [random.choices(SYMBOLS, weights=WEIGHTS, k=1)[0] for _ in range(3)]

    def evaluate(self, reels, bet):
        a, b, c = reels
        if a == b == c:
            return int(bet * TRIPLE_PAYOUTS[a]), "triple"
        if a == b or b == c or a == c:
            return int(bet * PAIR_PAYOUT), "pair"
        return 0, "none"

    @commands.command()
    async def slots(self, ctx, bet: int):
        """Spin the slot machine for CrewCoin."""
        balance = await bank.get_balance(ctx.author)
        if bet <= 0:
            return await ctx.send("Bet must be positive.")
        if bet > balance:
            return await ctx.send("Not enough CrewCoin.")

        await bank.withdraw_credits(ctx.author, bet)

        reels = self.spin_reels()

        # Suspense reveal: send placeholders, then flip reels left to right.
        e = Embed(title="🎰 Slots", description=f"Bet: **{bet}** CrewCoin")
        e.add_field(name="Reels", value="[ ❓ | ❓ | ❓ ]", inline=False)
        msg = await ctx.send(embed=e)

        shown = ["❓", "❓", "❓"]
        for i in range(3):
            await asyncio.sleep(0.8)
            shown[i] = reels[i]
            e.set_field_at(0, name="Reels", value=f"[ {' | '.join(shown)} ]", inline=False)
            await msg.edit(embed=e)

        payout, kind = self.evaluate(reels, bet)
        cfg = self.config.user(ctx.author)

        if payout > 0:
            await bank.deposit_credits(ctx.author, payout)
            net = payout - bet
            if kind == "triple" and reels[0] == "7️⃣":
                outcome = f"🎰 **JACKPOT!** Three 7️⃣ — you won **{payout}** CrewCoin (net +{net})."
            elif kind == "triple":
                outcome = f"🎉 Three {reels[0]}! You won **{payout}** CrewCoin (net +{net})."
            else:
                outcome = f"✨ A pair! You won **{payout}** CrewCoin (net +{net})."
            await cfg.total_slot_wins.set(await cfg.total_slot_wins() + 1)
            if payout > await cfg.biggest_slot_win():
                await cfg.biggest_slot_win.set(payout)
        else:
            outcome = f"💸 No match. You lost **{bet}** CrewCoin."
            await cfg.total_slot_losses.set(await cfg.total_slot_losses() + 1)

        await cfg.total_slot_bet.set(await cfg.total_slot_bet() + bet)

        e.add_field(name="Outcome", value=outcome, inline=False)
        await msg.edit(embed=e)

    @commands.command()
    async def slotstats(self, ctx):
        """Show your slots stats."""
        data = await self.config.user(ctx.author).all()
        await ctx.send(
            f"Slots — Wins: {data['total_slot_wins']}, "
            f"Losses: {data['total_slot_losses']}, "
            f"Bet total: {data['total_slot_bet']}, "
            f"Biggest win: {data['biggest_slot_win']}"
        )

    @commands.command()
    async def slotpayouts(self, ctx):
        """Show the slots payout table."""
        lines = ["**Three of a kind** (multiplier of bet):"]
        for sym, mult in TRIPLE_PAYOUTS.items():
            lines.append(f"{sym} {sym} {sym} → {mult}x")
        lines.append(f"\n**Any two matching** → {PAIR_PAYOUT}x")
        e = Embed(title="🎰 Slots Payouts", description="\n".join(lines), color=0xFFD700)
        await ctx.send(embed=e)


def setup(bot):
    bot.add_cog(Slots(bot))
