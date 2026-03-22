import discord
import random
import asyncio
import os
from pathlib import Path
from io import BytesIO
from redbot.core import commands, bank, Config
from discord import Embed, File
from PIL import Image, ImageDraw, ImageFont

# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────
CONFIG = Config.get_conf(None, identifier=7654321098)
CONFIG.register_user(hr_wins=0, hr_losses=0, hr_bet=0, hr_earned=0)

# ─────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────
TRACK_LENGTH = 20          # cells in the progress bar
TURN_DELAY   = 2.5         # seconds between turns
NUM_HORSES   = 6

HORSE_NAMES = [
    "Midnight Thunder", "Golden Arrow", "Iron Duchess",
    "Crimson Blaze", "Silent Storm", "Lucky Charm",
    "Phantom Stride", "Velvet Fury", "Royal Gambit",
    "Desert Wind", "Neon Horizon", "Shadow Dancer",
    "Steel Compass", "Wild Ember", "Copper Crown",
    "Frost Fang", "Scarlet Run", "Bolt from Blue",
]

JOCKEY_COLORS = [
    ("🔴", "Red"),
    ("🔵", "Blue"),
    ("🟢", "Green"),
    ("🟡", "Yellow"),
    ("🟣", "Purple"),
    ("🟠", "Orange"),
]

# Odds tiers — (label, win_multiplier, place_mult, show_mult, weight)
ODDS_TIERS = [
    ("2/1",  2.0,  1.2, 1.1,  5),   # heavy favorite
    ("3/1",  3.0,  1.5, 1.2,  4),
    ("5/1",  5.0,  2.0, 1.4,  3),
    ("8/1",  8.0,  3.0, 1.8,  2),
    ("12/1", 12.0, 4.5, 2.5,  1),
    ("20/1", 20.0, 7.0, 3.5,  1),
]

BET_TYPES = {
    "win":   "Win (1st place)",
    "place": "Place (top 2)",
    "show":  "Show (top 3)",
}

# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────
def generate_horses():
    names = random.sample(HORSE_NAMES, NUM_HORSES)
    colors = list(JOCKEY_COLORS)
    random.shuffle(colors)
    tiers = random.sample(ODDS_TIERS, NUM_HORSES)
    horses = []
    for i, (name, color, tier) in enumerate(zip(names, colors, tiers)):
        horses.append({
            "id":         i,
            "name":       name,
            "emoji":      color[0],
            "color":      color[1],
            "odds_label": tier[0],
            "win_mult":   tier[1],
            "place_mult": tier[2],
            "show_mult":  tier[3],
            "weight":     tier[4],
            "position":   0,
            "finished":   False,
            "finish_pos": None,
        })
    return horses

def build_track_embed(horses, turn, finished_order, race_over=False):
    """Build the animated race embed with progress bars."""
    color = 0x1a6b2e if not race_over else 0xFFD700

    title = "🏇  RACE IN PROGRESS" if not race_over else "🏆  RACE COMPLETE"
    e = Embed(title=title, color=color)

    lines = []
    for h in horses:
        pos = h["position"]
        filled = min(pos, TRACK_LENGTH)
        empty  = TRACK_LENGTH - filled
        bar    = "█" * filled + "░" * empty

        if h["finish_pos"] == 1:
            badge = " 🥇"
        elif h["finish_pos"] == 2:
            badge = " 🥈"
        elif h["finish_pos"] == 3:
            badge = " 🥉"
        elif h["finished"]:
            badge = f" #{h['finish_pos']}"
        else:
            badge = ""

        name_pad = h["name"][:16].ljust(16)
        lines.append(
            f"{h['emoji']} `{name_pad}` `[{bar}]`{badge}"
        )

    e.description = "\n".join(lines)

    if not race_over:
        e.set_footer(text=f"Turn {turn} — horses are running...")
    else:
        podium = [h for h in horses if h["finish_pos"] in (1, 2, 3)]
        podium.sort(key=lambda x: x["finish_pos"])
        podium_text = "\n".join(
            f"{'🥇🥈🥉'[h['finish_pos']-1]} {h['name']} ({h['odds_label']})"
            for h in podium
        )
        e.add_field(name="Podium", value=podium_text, inline=False)
        e.set_footer(text="Final results")

    return e

def simulate_turn(horses, finished_order):
    """Advance each horse by a weighted random amount."""
    weights = [h["weight"] for h in horses]
    total_w = sum(weights)
    for h in horses:
        if h["finished"]:
            continue
        # base move 1-3, favorites slightly faster on average
        base = random.randint(1, 3)
        bonus = 1 if random.random() < (h["weight"] / total_w) * 2 else 0
        h["position"] = min(h["position"] + base + bonus, TRACK_LENGTH)

        if h["position"] >= TRACK_LENGTH and not h["finished"]:
            h["finished"]   = True
            h["finish_pos"] = len(finished_order) + 1
            finished_order.append(h["id"])

def render_podium_image(horses):
    """Render a 900x400 podium finish image using Pillow."""
    W, H = 900, 420
    img = Image.new("RGB", (W, H), (10, 40, 15))
    draw = ImageDraw.Draw(img)

    # ── background turf stripes ──
    for i in range(0, H, 20):
        shade = (10, 42, 16) if (i // 20) % 2 == 0 else (12, 48, 18)
        draw.rectangle([0, i, W, i + 20], fill=shade)

    # ── gold header bar ──
    draw.rectangle([0, 0, W, 60], fill=(180, 140, 20))
    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", 28)
        font_name  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", 18)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf", 14)
    except:
        font_title = ImageFont.load_default()
        font_name  = font_title
        font_small = font_title

    draw.text((W // 2, 30), "🏆  FINAL RESULTS  🏆",
              font=font_title, fill=(255, 245, 180), anchor="mm")

    # ── podium blocks ──
    podium_data = [
        (1, "🥇", (218, 165, 32),  270, 180, 120),   # gold  — center
        (2, "🥈", (160, 160, 175), 80,  220, 100),   # silver — left
        (3, "🥉", (176, 115, 65),  480, 240,  80),   # bronze — right
    ]

    sorted_horses = sorted(
        [h for h in horses if h["finish_pos"] in (1, 2, 3)],
        key=lambda x: x["finish_pos"]
    )

    for place, medal, block_color, bx, by, bh in podium_data:
        horse = next((h for h in sorted_horses if h["finish_pos"] == place), None)
        if not horse:
            continue

        # podium block
        draw.rectangle([bx, by, bx + 180, by + bh], fill=block_color, outline=(255, 215, 0), width=2)

        # place number on block
        draw.text((bx + 90, by + bh // 2 + by // 10), str(place),
                  font=font_title, fill=(255, 255, 255), anchor="mm")

        # horse emoji circle
        circle_y = by - 55
        draw.ellipse([bx + 65, circle_y, bx + 115, circle_y + 50],
                     fill=(30, 80, 35), outline=(255, 215, 0), width=2)
        draw.text((bx + 90, circle_y + 25), horse["emoji"],
                  font=font_name, fill=(255, 255, 255), anchor="mm")

        # horse name
        name = horse["name"] if len(horse["name"]) <= 14 else horse["name"][:13] + "."
        draw.text((bx + 90, circle_y - 18), name,
                  font=font_small, fill=(255, 240, 180), anchor="mm")

        # odds
        draw.text((bx + 90, circle_y - 4), f"({horse['odds_label']})",
                  font=font_small, fill=(200, 200, 150), anchor="mm")

    # ── remaining finishers ──
    rest = sorted([h for h in horses if h["finish_pos"] not in (1, 2, 3)],
                  key=lambda x: x["finish_pos"])
    rx = 700
    draw.text((rx + 60, 80), "Also Ran", font=font_name, fill=(200, 200, 150), anchor="mm")
    for i, h in enumerate(rest):
        draw.text((rx + 60, 105 + i * 22),
                  f"#{h['finish_pos']} {h['emoji']} {h['name'][:14]}",
                  font=font_small, fill=(170, 200, 160), anchor="mm")

    # ── decorative border ──
    draw.rectangle([2, 2, W - 3, H - 3], outline=(180, 140, 20), width=3)

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

def payout_text(bet_type, multiplier):
    return {
        "win":   f"Win @ {multiplier}x",
        "place": f"Place @ {multiplier}x",
        "show":  f"Show @ {multiplier}x",
    }[bet_type]

# ─────────────────────────────────────────────
#  Cog
# ─────────────────────────────────────────────
class HorseRace(commands.Cog):
    """Horse racing casino game with live race updates."""

    def __init__(self, bot):
        self.bot = bot
        self.active_races = {}  # channel_id -> True (one race per channel)

    # ── !race ──────────────────────────────────
    @commands.command()
    async def race(self, ctx):
        """Start a horse race. Anyone can bet before the gates open!"""
        if ctx.channel.id in self.active_races:
            return await ctx.send("🏇 A race is already running in this channel!")

        self.active_races[ctx.channel.id] = True
        horses = generate_horses()

        # ── betting window embed ──
        e = Embed(
            title="🏇  POST TIME — PLACE YOUR BETS",
            description=(
                "Use `!bet <horse#> <win|place|show> <amount>` to wager.\n"
                "**Win** = must finish 1st | **Place** = top 2 | **Show** = top 3\n\n"
                "Gates open in **30 seconds**!"
            ),
            color=0x1a6b2e
        )

        for i, h in enumerate(horses, 1):
            e.add_field(
                name=f"#{i}  {h['emoji']}  {h['name']}",
                value=(
                    f"Odds: **{h['odds_label']}**\n"
                    f"Win: `{h['win_mult']}x` | Place: `{h['place_mult']}x` | Show: `{h['show_mult']}x`"
                ),
                inline=True
            )

        e.set_footer(text="🎰  Bet wisely — the track favors the bold.")
        await ctx.send(embed=e)

        # store race state in channel for !bet to access
        self.active_races[ctx.channel.id] = {
            "horses":  horses,
            "bets":    {},   # user_id -> {horse_id, bet_type, amount}
            "open":    True,
            "ctx":     ctx,
        }

        await asyncio.sleep(30)
        await self._run_race(ctx, horses)

    # ── !bet ───────────────────────────────────
    @commands.command()
    async def bet(self, ctx, horse_num: int, bet_type: str, amount: int):
        """Bet on a horse. Use during the 30s window after !race."""
        race = self.active_races.get(ctx.channel.id)
        if not race or not isinstance(race, dict):
            return await ctx.send("No race is accepting bets right now. Start one with `!race`.")
        if not race["open"]:
            return await ctx.send("Betting is closed — the race has started!")
        if horse_num < 1 or horse_num > NUM_HORSES:
            return await ctx.send(f"Pick a horse number between 1 and {NUM_HORSES}.")

        bet_type = bet_type.lower()
        if bet_type not in BET_TYPES:
            return await ctx.send("Bet type must be `win`, `place`, or `show`.")
        if amount <= 0:
            return await ctx.send("Bet must be positive.")

        bal = await bank.get_balance(ctx.author)
        if amount > bal:
            return await ctx.send("Not enough CrewCoin.")

        # one bet per user per race
        if ctx.author.id in race["bets"]:
            old = race["bets"][ctx.author.id]
            await bank.deposit_credits(ctx.author, old["amount"])  # refund old bet

        await bank.withdraw_credits(ctx.author, amount)
        horse = race["horses"][horse_num - 1]
        race["bets"][ctx.author.id] = {
            "horse_id":  horse["id"],
            "horse_num": horse_num,
            "horse_name": horse["name"],
            "bet_type":  bet_type,
            "amount":    amount,
            "mult": {
                "win":   horse["win_mult"],
                "place": horse["place_mult"],
                "show":  horse["show_mult"],
            }[bet_type],
        }

        e = Embed(
            title="✅  Bet Placed",
            description=(
                f"**{ctx.author.display_name}** bet **{amount} CrewCoin**\n"
                f"on #{horse_num} {horse['emoji']} **{horse['name']}** to **{bet_type.upper()}**\n"
                f"Payout multiplier: **{race['bets'][ctx.author.id]['mult']}x**"
            ),
            color=0xFFD700
        )
        await ctx.send(embed=e)

    # ── internal race runner ───────────────────
    async def _run_race(self, ctx, horses):
        race = self.active_races.get(ctx.channel.id)
        if not race or not isinstance(race, dict):
            return

        race["open"] = False
        bets = race["bets"]
        bet_count = len(bets)

        start_e = Embed(
            title="🚨  AND THEY'RE OFF!",
            description=f"{bet_count} bet{'s' if bet_count != 1 else ''} placed. The gates are open!",
            color=0x1a6b2e
        )
        race_msg = await ctx.send(embed=start_e)

        await asyncio.sleep(1.5)

        finished_order = []
        turn = 0

        # ── race loop ──
        while len(finished_order) < NUM_HORSES:
            turn += 1
            simulate_turn(horses, finished_order)
            e = build_track_embed(horses, turn, finished_order)
            await race_msg.edit(embed=e)
            await asyncio.sleep(TURN_DELAY)

        # ── final board ──
        final_e = build_track_embed(horses, turn, finished_order, race_over=True)
        await race_msg.edit(embed=final_e)

        # ── podium image ──
        buf = render_podium_image(horses)
        file = File(buf, filename="podium.png")
        podium_e = Embed(title="🏆  Official Podium", color=0xFFD700)
        podium_e.set_image(url="attachment://podium.png")
        await ctx.send(embed=podium_e, file=file)

        # ── pay out bets ──
        await asyncio.sleep(1)
        if bets:
            results = []
            for user_id, b in bets.items():
                member = ctx.guild.get_member(user_id)
                if not member:
                    continue

                horse = next(h for h in horses if h["id"] == b["horse_id"])
                fp = horse["finish_pos"]
                won = (
                    (b["bet_type"] == "win"   and fp == 1) or
                    (b["bet_type"] == "place" and fp <= 2) or
                    (b["bet_type"] == "show"  and fp <= 3)
                )

                user_cfg = CONFIG.user(member)
                await user_cfg.hr_bet.set(await user_cfg.hr_bet() + b["amount"])

                if won:
                    winnings = int(b["amount"] * b["mult"])
                    await bank.deposit_credits(member, winnings)
                    await user_cfg.hr_wins.set(await user_cfg.hr_wins() + 1)
                    await user_cfg.hr_earned.set(await user_cfg.hr_earned() + winnings)
                    results.append(
                        f"✅ {member.display_name} — #{b['horse_num']} {horse['emoji']} **{horse['name']}** "
                        f"finished **#{fp}** ({b['bet_type'].upper()}) → **+{winnings} CrewCoin**"
                    )
                else:
                    await user_cfg.hr_losses.set(await user_cfg.hr_losses() + 1)
                    results.append(
                        f"❌ {member.display_name} — #{b['horse_num']} {horse['emoji']} **{horse['name']}** "
                        f"finished **#{fp}** ({b['bet_type'].upper()}) → lost {b['amount']} CrewCoin"
                    )

            payout_e = Embed(
                title="💰  Race Payouts",
                description="\n".join(results) if results else "No bets were placed.",
                color=0x1a6b2e
            )
            await ctx.send(embed=payout_e)

        # cleanup
        del self.active_races[ctx.channel.id]

    # ── !racestats ─────────────────────────────
    @commands.command()
    async def racestats(self, ctx):
        """Show your horse racing stats."""
        d = await CONFIG.user(ctx.author).all()
        await ctx.send(
            f"🏇 **{ctx.author.display_name}'s Race Stats**\n"
            f"Wins: **{d['hr_wins']}** | Losses: **{d['hr_losses']}** | "
            f"Total Bet: **{d['hr_bet']}** | Total Earned: **{d['hr_earned']}** CrewCoin"
        )


def setup(bot):
    bot.add_cog(HorseRace(bot))
