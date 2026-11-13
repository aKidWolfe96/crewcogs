"""Krew Roulette — solo spins and a multiplayer table."""
import asyncio
import logging
import math
import random
import time
from io import BytesIO
from typing import Dict, Optional

import discord
from PIL import Image, ImageDraw, ImageFont
from redbot.core import bank, commands

from .casino_core import (
    currency, get_game_settings, mark_played, refund_wager, render, reply, resolve_bet, settle_game, validate_bet,
)
from .ui import ChoiceView, GameOverView, OwnedView, short

GAME = "roulette"
LOG = logging.getLogger("red.crewcogs.casino.roulette")
TABLE_SECONDS = 45
MAX_TABLE_BETS = 5
WHEEL_ORDER = ["0", "28", "9", "26", "30", "11", "7", "20", "32", "17", "5", "22", "34", "15", "3", "24", "36", "13", "1", "00", "27", "10", "25", "29", "12", "8", "19", "31", "18", "6", "21", "33", "16", "4", "23", "35", "14", "2"]
RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
BET_ALIASES = {
    "red": "red", "r": "red", "black": "black", "b": "black", "odd": "odd", "even": "even",
    "low": "low", "1-18": "low", "high": "high", "19-36": "high",
    "1st12": "dozen1", "first12": "dozen1", "dozen1": "dozen1", "d1": "dozen1",
    "2nd12": "dozen2", "second12": "dozen2", "dozen2": "dozen2", "d2": "dozen2",
    "3rd12": "dozen3", "third12": "dozen3", "dozen3": "dozen3", "d3": "dozen3",
    "column1": "column1", "col1": "column1", "column2": "column2", "col2": "column2",
    "column3": "column3", "col3": "column3",
}
OUTSIDE_CHOICES = [
    ("Red", "🔴", discord.ButtonStyle.danger, "red"), ("Black", "⚫", discord.ButtonStyle.secondary, "black"),
    ("Odd", None, discord.ButtonStyle.primary, "odd"), ("Even", None, discord.ButtonStyle.primary, "even"),
    ("1–18", None, discord.ButtonStyle.secondary, "low"), ("19–36", None, discord.ButtonStyle.secondary, "high"),
    ("1st 12", None, discord.ButtonStyle.secondary, "dozen1"), ("2nd 12", None, discord.ButtonStyle.secondary, "dozen2"),
    ("3rd 12", None, discord.ButtonStyle.secondary, "dozen3"),
]


def pocket_color(pocket: str) -> str:
    if pocket in {"0", "00"}:
        return "green"
    return "red" if int(pocket) in RED_NUMBERS else "black"


def display_bet(bet_type: str) -> str:
    labels = {"red": "🔴 Red", "black": "⚫ Black", "odd": "Odd", "even": "Even", "low": "1–18", "high": "19–36",
              "dozen1": "1st 12", "dozen2": "2nd 12", "dozen3": "3rd 12", "column1": "Column 1",
              "column2": "Column 2", "column3": "Column 3"}
    return f"#{bet_type.split(':', 1)[1]}" if bet_type.startswith("number:") else labels[bet_type]


def parse_choice(raw: str) -> Optional[str]:
    value = str(raw).lower().strip().lstrip("#")
    if value in {"0", "00"}:
        return f"number:{value}"
    if value.isdigit() and 1 <= int(value) <= 36:
        return f"number:{int(value)}"
    return BET_ALIASES.get(value)


def payout_multiplier(bet_type: str) -> int:
    if bet_type.startswith("number:"):
        return 36
    if bet_type.startswith("dozen") or bet_type.startswith("column"):
        return 3
    return 2


def is_winner(bet_type: str, pocket: str) -> bool:
    if bet_type.startswith("number:"):
        return pocket == bet_type.split(":", 1)[1]
    if pocket in {"0", "00"}:
        return False
    number = int(pocket)
    color = pocket_color(pocket)
    return {
        "red": color == "red", "black": color == "black", "odd": number % 2 == 1,
        "even": number % 2 == 0, "low": 1 <= number <= 18, "high": 19 <= number <= 36,
        "dozen1": 1 <= number <= 12, "dozen2": 13 <= number <= 24, "dozen3": 25 <= number <= 36,
        "column1": number % 3 == 1, "column2": number % 3 == 2, "column3": number % 3 == 0,
    }[bet_type]


def draw_wheel(offset: float) -> Image.Image:
    size, center, outer_radius, inner_radius = 520, 260, 240, 112
    image = Image.new("RGBA", (size, size), (20, 24, 29, 255))
    draw = ImageDraw.Draw(image)
    segment_angle = 360 / len(WHEEL_ORDER)
    font, center_font = _font(16), _font(23)
    for index, pocket in enumerate(WHEEL_ORDER):
        start = -90 + (index - offset) * segment_angle
        end = start + segment_angle
        fill = {"red":(176,32,37,255), "black":(30,33,38,255), "green":(25,130,78,255)}[pocket_color(pocket)]
        draw.pieslice((center-outer_radius, center-outer_radius, center+outer_radius, center+outer_radius), start=start, end=end, fill=fill, outline=(220,220,220,255), width=1)
        angle = math.radians((start + end) / 2)
        x, y = center + math.cos(angle)*196, center + math.sin(angle)*196
        box = draw.textbbox((0,0), pocket, font=font)
        draw.text((x-(box[2]-box[0])/2, y-(box[3]-box[1])/2), pocket, fill="white", font=font)
    draw.ellipse((center-inner_radius, center-inner_radius, center+inner_radius, center+inner_radius), fill=(195,151,55,255), outline=(245,220,145,255), width=4)
    draw.ellipse((center-82, center-82, center+82, center+82), fill=(49,55,62,255))
    label = "KREW\nROULETTE"
    box = draw.multiline_textbbox((0,0), label, font=center_font, align="center")
    draw.multiline_text((center-(box[2]-box[0])/2, center-(box[3]-box[1])/2), label, font=center_font, fill="white", align="center")
    draw.polygon([(center,10),(center-18,48),(center+18,48)], fill=(245,205,68,255), outline="white")
    draw.ellipse((center-8,35,center+8,51), fill=(245,245,245,255))
    return image

def _font(size: int):
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default()



def make_spin_gif(winning_index: int) -> bytes:
    frames = []
    for frame_number in range(18):
        progress = frame_number / 17
        eased = 1 - (1 - progress) ** 3
        frames.append(draw_wheel(winning_index + 3.5 * (1 - eased) * len(WHEEL_ORDER)))
    out = BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=130, loop=0, disposal=2)
    for frame in frames:
        frame.close()
    return out.getvalue()


def make_result_png(winning_index: int) -> bytes:
    image = draw_wheel(winning_index)
    out = BytesIO()
    image.save(out, format="PNG")
    image.close()
    return out.getvalue()


POCKET_EMOJI = {"red": "🔴", "black": "⚫", "green": "🟢"}


def pocket_text(pocket: str) -> str:
    color = pocket_color(pocket)
    return f"{POCKET_EMOJI[color]} **{pocket} {color.title()}**"


class NumberModal(discord.ui.Modal, title="Bet on a number"):
    number = discord.ui.TextInput(label="Number (0, 00 or 1–36) — pays 35:1", max_length=2)

    def __init__(self, on_pick) -> None:
        super().__init__()
        self.on_pick = on_pick

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.on_pick(interaction, str(self.number.value))


class TableBetView(OwnedView):
    """Private panel for placing a bet at the multiplayer table."""

    def __init__(self, cog: "Roulette", table: dict, owner_id: int) -> None:
        super().__init__(owner_id, timeout=TABLE_SECONDS + 15)
        self.cog, self.table, self.choice = cog, table, None
        self.select = discord.ui.Select(
            placeholder="1️⃣ Pick what to bet on…",
            options=[discord.SelectOption(label=label, value=value, emoji=emoji) for label, emoji, _, value in OUTSIDE_CHOICES]
            + [discord.SelectOption(label="Columns 1 / 2 / 3", value="columns", emoji="🏛️"),
               discord.SelectOption(label="A single number (35:1)", value="number", emoji="🎯")],
            row=0,
        )
        self.select.callback = self._picked
        self.add_item(self.select)
        for amount in (100, 500, 1000, 5000):
            btn = discord.ui.Button(label=f"Bet {short(amount)}", style=discord.ButtonStyle.success, row=1)
            btn.callback = self._amount(str(amount))
            self.add_item(btn)
        custom = discord.ui.Button(label="Custom…", emoji="✏️", style=discord.ButtonStyle.secondary, row=1)
        custom.callback = self._custom
        self.add_item(custom)

    async def _picked(self, interaction: discord.Interaction) -> None:
        value = self.select.values[0]
        if value == "number":
            async def chosen(i: discord.Interaction, raw: str) -> None:
                parsed = parse_choice(raw)
                if not parsed or not parsed.startswith("number:"):
                    await i.response.send_message("That's not a roulette number (0, 00, 1–36).", ephemeral=True)
                    return
                self.choice = parsed
                await i.response.send_message(f"Betting on **{display_bet(parsed)}** — now tap an amount.", ephemeral=True)
            await interaction.response.send_modal(NumberModal(chosen))
            return
        if value == "columns":
            view = ChoiceView(self.owner_id, [(f"Column {n}", None, discord.ButtonStyle.secondary, f"column{n}") for n in (1, 2, 3)],
                              self._column_chosen)
            await interaction.response.send_message("Which column?", view=view, ephemeral=True)
            return
        self.choice = value
        await interaction.response.edit_message(
            content=f"Betting on **{display_bet(value)}** — now tap an amount.", view=self)

    async def _column_chosen(self, interaction: discord.Interaction, value: str) -> None:
        self.choice = value
        await interaction.followup.send(f"Betting on **{display_bet(value)}** — now tap an amount.", ephemeral=True)

    def _amount(self, raw: str):
        async def go(interaction: discord.Interaction) -> None:
            await self.cog.place_table_bet(interaction, self.table, self.choice, raw)
        return go

    async def _custom(self, interaction: discord.Interaction) -> None:
        from .ui import CustomBetModal
        await interaction.response.send_modal(
            CustomBetModal(lambda i, raw: self.cog.place_table_bet(i, self.table, self.choice, raw))
        )


class TableView(discord.ui.View):
    def __init__(self, cog: "Roulette", table: dict) -> None:
        super().__init__(timeout=TABLE_SECONDS + 30)
        self.cog, self.table = cog, table

    @discord.ui.button(label="Place a bet", emoji="🪙", style=discord.ButtonStyle.success)
    async def place(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.table["closed"]:
            await interaction.response.send_message("Betting is closed for this spin.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Pick a bet type, then an amount. You can place up to 5 bets.",
            view=TableBetView(self.cog, self.table, interaction.user.id), ephemeral=True,
        )

    @discord.ui.button(label="Spin now", emoji="🎡", style=discord.ButtonStyle.danger)
    async def spin_now(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.table["host"]:
            await interaction.response.send_message("Only the player who opened the table can spin early.", ephemeral=True)
            return
        if not self.table["bets"]:
            await interaction.response.send_message("Nobody has bet yet!", ephemeral=True)
            return
        await interaction.response.defer()
        self.table["spin_early"].set()


class Roulette(commands.Cog):
    """American roulette — solo or at a shared table."""

    def __init__(self, bot):
        self.bot = bot
        self.active_players = set()
        self.tables: Dict[int, dict] = {}

    # ── Solo ──────────────────────────────────────────────────────────────────

    @commands.command(aliases=["roul"])
    @commands.guild_only()
    async def roulette(self, ctx: commands.Context, bet: str, choice: Optional[str] = None):
        """Spin the wheel. Usage: `roulette <bet> [red|black|odd|even|low|high|1st12|col1|0-36]`

        Leave out the choice to pick it with buttons. Straight numbers pay 35:1,
        dozens/columns 2:1, everything else 1:1.
        """
        if parse_choice(bet) and choice is not None and not parse_choice(choice):
            bet, choice = choice, bet  # old order: roulette red 100
        await self.start(ctx, bet, choice)

    async def start(self, ctx: commands.Context, raw_bet, choice: Optional[str] = None,
                    interaction: Optional[discord.Interaction] = None) -> None:
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.defer()
        bet, err = await resolve_bet(ctx.author, GAME, raw_bet)
        if err:
            await reply(ctx, interaction, err)
            return
        if choice is None:
            choices = OUTSIDE_CHOICES + [("Number…", "🎯", discord.ButtonStyle.success, "number")]

            async def picked(i: discord.Interaction, value: str) -> None:
                if value == "number":
                    await i.followup.send("Type a number with `roulette <bet> <number>`, e.g. "
                                          f"`roulette {bet} 17`.", ephemeral=True)
                    return
                await self.start(ctx, bet, value, i)
            view = ChoiceView(ctx.author.id, choices, picked)
            view.message = await ctx.send(embed=discord.Embed(
                title="🎡 Krew Roulette", color=0xF1C40F,
                description=f"Betting **{bet:,}** — what are you backing?\n_Numbers pay 35:1 · dozens 2:1 · colours & halves 1:1_",
            ), view=view)
            return
        parsed = parse_choice(choice)
        if parsed is None:
            await reply(ctx, interaction, "Bet on: a number 0–36 or 00, red, black, odd, even, low, high, 1st12/2nd12/3rd12, col1/col2/col3.")
            return
        if ctx.author.id in self.active_players:
            await reply(ctx, interaction, "Your wheel is still spinning!")
            return
        error = await validate_bet(ctx, GAME, bet)
        if error:
            await reply(ctx, interaction, error)
            return

        self.active_players.add(ctx.author.id)
        withdrawn = settled = False
        try:
            await bank.withdraw_credits(ctx.author, bet)
            withdrawn = True
            mark_played(ctx.guild.id, ctx.author.id, GAME)
            cur = await currency(ctx.guild)
            pocket = random.choice(WHEEL_ORDER)
            index = WHEEL_ORDER.index(pocket)
            gif = await render(make_spin_gif, index)
            embed = discord.Embed(title="🎡 Krew Roulette", color=0xF1C40F,
                                  description=f"**{ctx.author.display_name}** bets **{bet:,}** on **{display_bet(parsed)}**…")
            embed.set_image(url="attachment://spin.gif")
            message = await ctx.send(embed=embed, file=discord.File(BytesIO(gif), filename="spin.gif"))
            await asyncio.sleep(3)

            won = is_winner(parsed, pocket)
            settlement = await settle_game(ctx.author, GAME, bet, bet * payout_multiplier(parsed) if won else 0,
                                           "win" if won else "loss", channel=ctx.channel)
            settled = True
            balance = await bank.get_balance(ctx.author)
            if won:
                text = f"🎉 **You win {settlement.deposited - bet:+,} {cur}!**"
            else:
                text = f"💸 You lose **{bet:,} {cur}**."
            result = discord.Embed(
                title="🎡 Krew Roulette", color=0x2ECC71 if won else 0xE74C3C,
                description=f"The ball lands on {pocket_text(pocket)}\nYour bet: **{display_bet(parsed)}**\n\n{text}",
            )
            result.set_image(url="attachment://result.png")
            result.set_footer(text=f"Balance: {balance:,} {cur}")
            png = await render(make_result_png, index)
            view = GameOverView(ctx.author.id, bet, lambda i, b: self.start(ctx, b, choice, i), again_label="Spin")
            await message.edit(embed=result, attachments=[discord.File(BytesIO(png), filename="result.png")], view=view)
            view.message = message
        except Exception:
            if withdrawn and not settled:
                await refund_wager(ctx.author, GAME, bet, reason="command failure before settlement")
            LOG.exception("Roulette failed for user %s", ctx.author.id)
            await ctx.send("Roulette hit an error. " + ("Your result was recorded." if settled else "Your wager was refunded."))
        finally:
            self.active_players.discard(ctx.author.id)

    # ── Multiplayer table ─────────────────────────────────────────────────────

    @commands.command(name="rtable", aliases=["roulettetable", "table"])
    @commands.guild_only()
    async def rtable(self, ctx: commands.Context):
        """Open a shared roulette table: everyone has 45 seconds to bet, then one spin for all."""
        if ctx.channel.id in self.tables:
            await ctx.send("A table is already open here — tap **Place a bet** on it!")
            return
        settings = await get_game_settings(ctx.guild, GAME)
        if not settings["enabled"]:
            await ctx.send("Roulette is currently disabled.")
            return
        table = {"host": ctx.author.id, "bets": [], "closed": False, "spin_early": asyncio.Event(),
                 "ends": time.time() + TABLE_SECONDS, "guild": ctx.guild, "channel": ctx.channel, "message": None}
        self.tables[ctx.channel.id] = table
        try:
            view = TableView(self, table)
            table["message"] = await ctx.send(embed=self._table_embed(table), view=view)
            try:
                await asyncio.wait_for(table["spin_early"].wait(), timeout=TABLE_SECONDS)
            except asyncio.TimeoutError:
                pass
            table["closed"] = True
            view.stop()
            for item in view.children:
                item.disabled = True
            await table["message"].edit(embed=self._table_embed(table), view=view)
            if not table["bets"]:
                await ctx.send(embed=discord.Embed(description="🎡 Nobody bet — the table closes.", color=0x95A5A6))
                return
            await self._spin_table(table)
        finally:
            self.tables.pop(ctx.channel.id, None)

    def _table_embed(self, table: dict) -> discord.Embed:
        lines = [f"**{b['name']}** — {b['amount']:,} on {display_bet(b['choice'])}" for b in table["bets"]]
        total = sum(b["amount"] for b in table["bets"])
        status = "🔒 **Betting closed — spinning!**" if table["closed"] else f"⏳ Spins <t:{int(table['ends'])}:R>"
        embed = discord.Embed(
            title="🎡 Krew Roulette Table",
            description=f"{status}\nAnyone can join — tap **Place a bet**.\n\n" + ("\n".join(lines) or "_No bets yet._"),
            color=0x1ABC9C,
        )
        embed.set_footer(text=f"{len(table['bets'])} bets · {total:,} on the table · numbers 35:1 · dozens 2:1 · colours 1:1")
        return embed

    async def place_table_bet(self, interaction: discord.Interaction, table: dict, choice: Optional[str], raw: str) -> None:
        member = interaction.user
        if table["closed"]:
            await interaction.response.send_message("Too late — betting is closed for this spin.", ephemeral=True)
            return
        if not choice:
            await interaction.response.send_message("Pick what to bet on first (the menu above).", ephemeral=True)
            return
        if sum(1 for b in table["bets"] if b["user"] == member.id) >= MAX_TABLE_BETS:
            await interaction.response.send_message(f"You've placed the maximum of {MAX_TABLE_BETS} bets this spin.", ephemeral=True)
            return
        bet, err = await resolve_bet(member, GAME, raw)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        settings = await get_game_settings(member.guild, GAME)
        if bet < settings["min_bet"] or (settings["max_bet"] and bet > settings["max_bet"]):
            limit = f"{settings['min_bet']:,}–{settings['max_bet']:,}" if settings["max_bet"] else f"at least {settings['min_bet']:,}"
            await interaction.response.send_message(f"Table bets must be {limit}.", ephemeral=True)
            return
        try:
            await bank.withdraw_credits(member, bet)
        except ValueError:
            balance = await bank.get_balance(member)
            await interaction.response.send_message(f"You only have **{balance:,}**.", ephemeral=True)
            return
        if table["closed"]:  # closed while we were withdrawing
            await refund_wager(member, GAME, bet, reason="table closed during bet")
            await interaction.response.send_message("Too late — betting just closed. Refunded.", ephemeral=True)
            return
        table["bets"].append({"user": member.id, "member": member, "name": member.display_name, "choice": choice, "amount": bet})
        await interaction.response.send_message(f"✅ **{bet:,}** on **{display_bet(choice)}** — good luck!", ephemeral=True)
        if table["message"] is not None:
            try:
                await table["message"].edit(embed=self._table_embed(table))
            except discord.HTTPException:
                pass

    async def _spin_table(self, table: dict) -> None:
        channel, guild = table["channel"], table["guild"]
        per_player: Dict[int, dict] = {}
        for b in table["bets"]:
            p = per_player.setdefault(b["user"], {"member": b["member"], "wager": 0, "payout": 0, "settled": False})
            p["wager"] += b["amount"]
        try:
            pocket = random.choice(WHEEL_ORDER)
            index = WHEEL_ORDER.index(pocket)
            gif = await render(make_spin_gif, index)
            embed = discord.Embed(title="🎡 The table spins…", color=0xF1C40F)
            embed.set_image(url="attachment://spin.gif")
            message = await channel.send(embed=embed, file=discord.File(BytesIO(gif), filename="spin.gif"))
            await asyncio.sleep(3)
            for b in table["bets"]:
                if is_winner(b["choice"], pocket):
                    per_player[b["user"]]["payout"] += b["amount"] * payout_multiplier(b["choice"])
            cur = await currency(guild)
            lines = []
            for uid, p in sorted(per_player.items(), key=lambda kv: kv[1]["payout"] - kv[1]["wager"], reverse=True):
                outcome = "win" if p["payout"] > p["wager"] else ("push" if p["payout"] == p["wager"] else "loss")
                s = await settle_game(p["member"], GAME, p["wager"], p["payout"], outcome, channel=channel)
                p["settled"] = True
                net = s.deposited - p["wager"]
                icon = "🎉" if net > 0 else ("🤝" if net == 0 else "💸")
                lines.append(f"{icon} **{p['member'].display_name}** {net:+,} {cur}")
            png = await render(make_result_png, index)
            result = discord.Embed(title="🎡 Table results", color=0x1ABC9C,
                                   description=f"The ball lands on {pocket_text(pocket)}\n\n" + "\n".join(lines))
            result.set_image(url="attachment://result.png")
            result.set_footer(text="Open another round with rtable")
            await message.edit(embed=result, attachments=[discord.File(BytesIO(png), filename="result.png")])
        except Exception:
            LOG.exception("Roulette table failed")
            for p in per_player.values():
                if not p["settled"]:
                    await refund_wager(p["member"], GAME, p["wager"], reason="table failure before settlement")
            await channel.send("The roulette table hit an error — unsettled bets were refunded.")

    @commands.command(name="roulettebets", hidden=True)
    async def roulettebets(self, ctx: commands.Context):
        """Roulette bet types and payouts."""
        await ctx.send(embed=discord.Embed(
            title="🎡 Roulette bets",
            description="**Number** `0`, `00`, `1–36` — 35:1\n**Red / Black · Odd / Even · Low / High** — 1:1\n"
                        "**Dozens** `1st12` `2nd12` `3rd12` — 2:1\n**Columns** `col1` `col2` `col3` — 2:1",
            color=0xF1C40F,
        ))


async def setup(bot):
    await bot.add_cog(Roulette(bot))
