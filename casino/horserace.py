import discord
import random
import asyncio
from redbot.core import commands, bank, Config
from discord import Embed
from discord.ui import View, Button

# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────
CONFIG = Config.get_conf(None, identifier=7654321098)
CONFIG.register_user(hr_wins=0, hr_losses=0, hr_bet=0, hr_earned=0)

# ─────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────
TRACK_LENGTH   = 20
TURN_DELAY     = 2.5
NUM_HORSES     = 6
PRESET_AMOUNTS = [100, 500, 1000]

HORSE_NAMES = [
    "Midnight Thunder", "Golden Arrow", "Iron Duchess",
    "Crimson Blaze", "Silent Storm", "Lucky Charm",
    "Phantom Stride", "Velvet Fury", "Royal Gambit",
    "Desert Wind", "Neon Horizon", "Shadow Dancer",
    "Steel Compass", "Wild Ember", "Copper Crown",
    "Frost Fang", "Scarlet Run", "Bolt from Blue",
]

JOCKEY_COLORS = [
    ("🔴", "Red"), ("🔵", "Blue"), ("🟢", "Green"),
    ("🟡", "Yellow"), ("🟣", "Purple"), ("🟠", "Orange"),
]

# (odds_label, win_mult, place_mult, show_mult, weight)
ODDS_TIERS = [
    ("2/1",  2.0,  1.2, 1.1, 5),
    ("3/1",  3.0,  1.5, 1.2, 4),
    ("5/1",  5.0,  2.0, 1.4, 3),
    ("8/1",  8.0,  3.0, 1.8, 2),
    ("12/1", 12.0, 4.5, 2.5, 1),
    ("20/1", 20.0, 7.0, 3.5, 1),
]

# ─────────────────────────────────────────────
#  Horse generation
# ─────────────────────────────────────────────
def generate_horses():
    names  = random.sample(HORSE_NAMES, NUM_HORSES)
    colors = random.sample(JOCKEY_COLORS, NUM_HORSES)
    tiers  = random.sample(ODDS_TIERS, NUM_HORSES)
    return [
        {
            "id": i, "num": i + 1,
            "name": name, "emoji": color[0], "color": color[1],
            "odds_label": tier[0], "win_mult": tier[1],
            "place_mult": tier[2], "show_mult": tier[3], "weight": tier[4],
            "position": 0, "finished": False, "finish_pos": None,
        }
        for i, (name, color, tier) in enumerate(zip(names, colors, tiers))
    ]

# ─────────────────────────────────────────────
#  Race simulation
# ─────────────────────────────────────────────
def simulate_turn(horses, finished_order):
    total_w = sum(h["weight"] for h in horses)
    for h in horses:
        if h["finished"]:
            continue
        base  = random.randint(1, 3)
        bonus = 1 if random.random() < (h["weight"] / total_w) * 2 else 0
        h["position"] = min(h["position"] + base + bonus, TRACK_LENGTH)
        if h["position"] >= TRACK_LENGTH and not h["finished"]:
            h["finished"]   = True
            h["finish_pos"] = len(finished_order) + 1
            finished_order.append(h["id"])

def build_track_embed(horses, turn, race_over=False):
    e = Embed(
        title="🏇  RACE IN PROGRESS" if not race_over else "🏆  RACE COMPLETE",
        color=0x1a6b2e if not race_over else 0xFFD700
    )
    lines = []
    for h in horses:
        bar   = "█" * min(h["position"], TRACK_LENGTH) + "░" * (TRACK_LENGTH - min(h["position"], TRACK_LENGTH))
        badge = {1: " 🥇", 2: " 🥈", 3: " 🥉"}.get(h["finish_pos"], f" #{h['finish_pos']}" if h["finished"] else "")
        lines.append(f"{h['emoji']} `{h['name'][:16].ljust(16)}` `[{bar}]`{badge}")
    e.description = "\n".join(lines)
    if race_over:
        all_finished = sorted(horses, key=lambda x: x["finish_pos"])
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        results_lines = []
        for h in all_finished:
            medal = medals.get(h["finish_pos"], f"#{h['finish_pos']} ")
            results_lines.append(f"{medal} {h['emoji']} **{h['name']}** ({h['odds_label']})")
        e.add_field(name="🏁  Final Order", value="\n".join(results_lines), inline=False)
        e.set_footer(text="Race complete — payouts below")
    else:
        e.set_footer(text=f"Turn {turn} — horses are running...")
    return e

# ─────────────────────────────────────────────
#  Lobby embed
# ─────────────────────────────────────────────
def build_lobby_embed(horses, joined, seconds_left):
    desc = (
        "Click **Join Race** to place your bet!\n"
        "The host will start the race when everyone is ready.\n\u200b"
    ) if seconds_left == 0 else (
        f"🚦 Race starting in **{seconds_left} seconds**...\n\u200b"
    )
    e = Embed(title="🏇  POST TIME — JOIN THE RACE", description=desc, color=0x1a6b2e)
    for h in horses:
        e.add_field(
            name=f"#{h['num']}  {h['emoji']}  {h['name']}",
            value=f"Odds: **{h['odds_label']}**\nWin `{h['win_mult']}x` · Place `{h['place_mult']}x` · Show `{h['show_mult']}x`",
            inline=True
        )
    if joined:
        e.add_field(name=f"🎟️  {len(joined)} Joined", value=", ".join(f"**{n}**" for n in joined.values()), inline=False)
    else:
        e.add_field(name="🎟️  No bets yet", value="Be the first to join!", inline=False)
    e.set_footer(text="🎰  Pick your horse — may the best beast win.")
    return e

# ─────────────────────────────────────────────
#  Views — Step 3: Amount
# ─────────────────────────────────────────────
class AmountView(View):
    def __init__(self, cog, race, user, horse, bet_type):
        super().__init__(timeout=60)
        self.cog      = cog
        self.race     = race
        self.user     = user
        self.horse    = horse
        self.bet_type = bet_type
        self.mult     = {"win": horse["win_mult"], "place": horse["place_mult"], "show": horse["show_mult"]}[bet_type]

        for amt in PRESET_AMOUNTS:
            btn = Button(label=f"{amt} KrustyCoins", style=discord.ButtonStyle.secondary)
            btn.callback = self._make_preset(amt)
            self.add_item(btn)

        allin = Button(label="💰 All In", style=discord.ButtonStyle.danger)
        allin.callback = self._allin
        self.add_item(allin)

        custom = Button(label="✏️ Custom", style=discord.ButtonStyle.primary)
        custom.callback = self._custom
        self.add_item(custom)

    def _make_preset(self, amount):
        async def cb(interaction: discord.Interaction):
            if interaction.user != self.user:
                return await interaction.response.send_message("Not your bet!", ephemeral=True)
            await self._confirm(interaction, amount)
        return cb

    async def _allin(self, interaction: discord.Interaction):
        if interaction.user != self.user:
            return await interaction.response.send_message("Not your bet!", ephemeral=True)
        bal = await bank.get_balance(self.user)
        if bal <= 0:
            return await interaction.response.send_message("You have no CrewCoin!", ephemeral=True)
        await self._confirm(interaction, bal)

    async def _custom(self, interaction: discord.Interaction):
        if interaction.user != self.user:
            return await interaction.response.send_message("Not your bet!", ephemeral=True)
        await interaction.response.send_message(
            "💬 Type your custom bet amount in chat now (30 seconds).", ephemeral=True
        )
        def check(m):
            return m.author == self.user and m.channel == interaction.channel and m.content.isdigit()
        try:
            msg = await self.cog.bot.wait_for("message", timeout=30, check=check)
            try:
                await msg.delete()
            except Exception:
                pass
            await self._confirm(interaction, int(msg.content), followup=True)
        except asyncio.TimeoutError:
            await interaction.followup.send("⏰ Timed out. No bet placed.", ephemeral=True)

    async def _confirm(self, interaction, amount, followup=False):
        if not self.race.get("open"):
            txt = "Betting is closed — race has started!"
            return await (interaction.followup.send(txt, ephemeral=True) if followup
                          else interaction.response.send_message(txt, ephemeral=True))
        if amount <= 0:
            txt = "Bet must be positive."
            return await (interaction.followup.send(txt, ephemeral=True) if followup
                          else interaction.response.send_message(txt, ephemeral=True))
        bal = await bank.get_balance(self.user)
        if amount > bal:
            txt = f"Not enough CrewCoin. You have **{bal}**."
            return await (interaction.followup.send(txt, ephemeral=True) if followup
                          else interaction.response.send_message(txt, ephemeral=True))

        # refund previous bet
        if self.user.id in self.race["bets"]:
            await bank.deposit_credits(self.user, self.race["bets"][self.user.id]["amount"])

        await bank.withdraw_credits(self.user, amount)
        self.race["bets"][self.user.id] = {
            "horse_id": self.horse["id"], "horse_num": self.horse["num"],
            "horse_name": self.horse["name"], "bet_type": self.bet_type,
            "amount": amount, "mult": self.mult,
        }
        self.race["joined"][self.user.id] = self.user.display_name

        # refresh lobby embed
        try:
            updated = build_lobby_embed(self.race["horses"], self.race["joined"], self.race.get("seconds_left", 0))
            await self.race["lobby_msg"].edit(embed=updated)
        except Exception:
            pass

        # delete the ephemeral betting flow message so the user sees the lobby embed
        try:
            ephemeral_msg = self.race.get("ephemeral_msgs", {}).get(self.user.id)
            if ephemeral_msg:
                await ephemeral_msg.delete()
                self.race["ephemeral_msgs"].pop(self.user.id, None)
        except Exception:
            pass

        if followup:
            await interaction.followup.send("✅ Bet placed! Check the lobby above.", ephemeral=True, delete_after=3)
        else:
            await interaction.response.defer()
        self.stop()

# ─────────────────────────────────────────────
#  Views — Step 2: Bet type
# ─────────────────────────────────────────────
class BetTypeView(View):
    def __init__(self, cog, race, user, horse):
        super().__init__(timeout=60)
        self.cog   = cog
        self.race  = race
        self.user  = user
        self.horse = horse

        for bet_type, label, style in [
            ("win",   f"🥇 Win ({horse['win_mult']}x)",    discord.ButtonStyle.success),
            ("place", f"🥈 Place ({horse['place_mult']}x)", discord.ButtonStyle.primary),
            ("show",  f"🥉 Show ({horse['show_mult']}x)",   discord.ButtonStyle.secondary),
        ]:
            btn = Button(label=label, style=style)
            btn.callback = self._make_cb(bet_type)
            self.add_item(btn)

        back = Button(label="← Back", style=discord.ButtonStyle.danger)
        back.callback = self._back
        self.add_item(back)

    def _make_cb(self, bet_type):
        async def cb(interaction: discord.Interaction):
            if interaction.user != self.user:
                return await interaction.response.send_message("Not your bet!", ephemeral=True)
            h = self.horse
            mult = {"win": h["win_mult"], "place": h["place_mult"], "show": h["show_mult"]}[bet_type]
            e = Embed(
                title=f"{h['emoji']} {h['name']} — Choose Amount",
                description=(
                    f"Bet type: **{bet_type.upper()}** ({mult}x)\n\n"
                    f"Pick a preset or enter a custom amount."
                ),
                color=0x1a6b2e
            )
            view = AmountView(self.cog, self.race, self.user, h, bet_type)
            await interaction.response.edit_message(embed=e, view=view)
            self.stop()
        return cb

    async def _back(self, interaction: discord.Interaction):
        if interaction.user != self.user:
            return await interaction.response.send_message("Not your bet!", ephemeral=True)
        e = Embed(title="🏇 Pick Your Horse", description="Select a horse to bet on:", color=0x1a6b2e)
        await interaction.response.edit_message(embed=e, view=HorseSelectView(self.cog, self.race, self.user))
        self.stop()

# ─────────────────────────────────────────────
#  Views — Step 1: Horse select
# ─────────────────────────────────────────────
class HorseSelectView(View):
    def __init__(self, cog, race, user):
        super().__init__(timeout=60)
        self.cog  = cog
        self.race = race
        self.user = user

        for h in race["horses"]:
            btn = Button(
                label=f"#{h['num']} {h['name']} ({h['odds_label']})",
                emoji=h["emoji"],
                style=discord.ButtonStyle.secondary,
                row=(h["num"] - 1) // 3
            )
            btn.callback = self._make_cb(h)
            self.add_item(btn)

    def _make_cb(self, horse):
        async def cb(interaction: discord.Interaction):
            if interaction.user != self.user:
                return await interaction.response.send_message("Not your bet!", ephemeral=True)
            e = Embed(
                title=f"{horse['emoji']} {horse['name']}",
                description=(
                    f"Odds: **{horse['odds_label']}**\n\n"
                    f"🥇 **Win** — must finish 1st → `{horse['win_mult']}x`\n"
                    f"🥈 **Place** — must finish top 2 → `{horse['place_mult']}x`\n"
                    f"🥉 **Show** — must finish top 3 → `{horse['show_mult']}x`"
                ),
                color=0x1a6b2e
            )
            await interaction.response.edit_message(embed=e, view=BetTypeView(self.cog, self.race, self.user, horse))
            self.stop()
        return cb

# ─────────────────────────────────────────────
#  Views — Lobby join button
# ─────────────────────────────────────────────
class JoinView(View):
    def __init__(self, cog, race):
        super().__init__(timeout=600)
        self.cog  = cog
        self.race = race

    @discord.ui.button(label="🏇  Join Race", style=discord.ButtonStyle.success, row=0)
    async def join(self, interaction: discord.Interaction, button: Button):
        if not self.race.get("open"):
            return await interaction.response.send_message("Betting is closed — race has started!", ephemeral=True)
        e = Embed(title="🏇 Pick Your Horse", description="Select a horse to place your bet on:", color=0x1a6b2e)
        await interaction.response.send_message(
            embed=e,
            view=HorseSelectView(self.cog, self.race, interaction.user),
            ephemeral=True
        )
        # store the ephemeral message so later steps can edit/delete it
        msg = await interaction.original_response()
        self.race.setdefault("ephemeral_msgs", {})[interaction.user.id] = msg

    @discord.ui.button(label="🚦  Start Race", style=discord.ButtonStyle.danger, row=0)
    async def start_race(self, interaction: discord.Interaction, button: Button):
        race = self.race
        if interaction.user != race["ctx"].author:
            return await interaction.response.send_message("Only the race host can start the race!", ephemeral=True)
        if not race.get("open"):
            return await interaction.response.send_message("Race is already starting!", ephemeral=True)
        if not race["bets"]:
            return await interaction.response.send_message("No bets placed yet — wait for players to join!", ephemeral=True)

        race["open"] = False
        self.stop()
        await interaction.response.defer()

        # 10 second countdown: 10 → 5 → 3 → 2 → 1
        for i, wait in [(10, 5), (5, 2), (3, 1), (2, 1), (1, 1)]:
            try:
                countdown_e = build_lobby_embed(race["horses"], race["joined"], 0)
                countdown_e.title = f"🚦  RACE STARTING IN {i}..."
                await race["lobby_msg"].edit(embed=countdown_e, view=None)
            except Exception:
                pass
            await asyncio.sleep(wait)

        await self.cog._run_race(race["ctx"], race)

# ─────────────────────────────────────────────
#  Cog
# ─────────────────────────────────────────────
class HorseRace(commands.Cog):
    """Horse racing casino game with interactive button betting."""

    def __init__(self, bot):
        self.bot          = bot
        self.active_races = {}

    @commands.command()
    async def startrace(self, ctx):
        """Open a horse race lobby. Players click to join and bet."""
        if ctx.channel.id in self.active_races:
            return await ctx.send("🏇 A race is already running in this channel!")

        horses = generate_horses()
        race = {
            "horses": horses, "bets": {}, "joined": {},
            "open": True, "lobby_msg": None, "ctx": ctx,
        }
        self.active_races[ctx.channel.id] = race

        join_view = JoinView(self, race)
        lobby_msg = await ctx.send(embed=build_lobby_embed(horses, {}, 0), view=join_view)
        race["lobby_msg"] = lobby_msg

    async def _run_race(self, ctx, race):
        horses, bets = race["horses"], race["bets"]

        race_msg = await ctx.send(embed=Embed(
            title="🚨  AND THEY'RE OFF!",
            description=f"**{len(bets)}** bet{'s' if len(bets) != 1 else ''} placed. The gates are open!",
            color=0x1a6b2e
        ))
        await asyncio.sleep(1.5)

        finished_order, turn = [], 0
        while len(finished_order) < NUM_HORSES:
            turn += 1
            simulate_turn(horses, finished_order)
            await race_msg.edit(embed=build_track_embed(horses, turn))
            await asyncio.sleep(TURN_DELAY)

        # build payout results before editing final embed
        payout_lines = []
        if bets:
            for user_id, b in bets.items():
                member = ctx.guild.get_member(user_id)
                if not member:
                    continue
                horse = next(h for h in horses if h["id"] == b["horse_id"])
                fp    = horse["finish_pos"]
                won   = (
                    (b["bet_type"] == "win"   and fp == 1) or
                    (b["bet_type"] == "place" and fp <= 2) or
                    (b["bet_type"] == "show"  and fp <= 3)
                )
                cfg = CONFIG.user(member)
                await cfg.hr_bet.set(await cfg.hr_bet() + b["amount"])
                if won:
                    winnings = int(b["amount"] * b["mult"])
                    await bank.deposit_credits(member, winnings)
                    await cfg.hr_wins.set(await cfg.hr_wins() + 1)
                    await cfg.hr_earned.set(await cfg.hr_earned() + winnings)
                    payout_lines.append(f"✅ {member.display_name} — {horse['emoji']} **{horse['name']}** #{fp} ({b['bet_type'].upper()}) → **+{winnings} KrustyCoins**")
                else:
                    await cfg.hr_losses.set(await cfg.hr_losses() + 1)
                    payout_lines.append(f"❌ {member.display_name} — {horse['emoji']} **{horse['name']}** #{fp} ({b['bet_type'].upper()}) → lost {b['amount']} KrustyCoins")

        final_e = build_track_embed(horses, turn, race_over=True)
        if payout_lines:
            final_e.add_field(name="💰  Payouts", value="\n".join(payout_lines), inline=False)
        else:
            final_e.add_field(name="💰  Payouts", value="No bets were placed this race.", inline=False)
        await race_msg.edit(embed=final_e)

        del self.active_races[ctx.channel.id]

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
