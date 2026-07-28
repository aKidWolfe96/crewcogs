import asyncio
import math
import random
import tempfile
from pathlib import Path
from typing import Optional, Tuple

import discord
from PIL import Image, ImageDraw, ImageFont
from redbot.core import Config, bank, commands


ROULETTE_CONFIG_ID = 8642097531

# American roulette wheel order.
WHEEL_ORDER = [
    "0", "28", "9", "26", "30", "11", "7", "20", "32", "17",
    "5", "22", "34", "15", "3", "24", "36", "13", "1", "00",
    "27", "10", "25", "29", "12", "8", "19", "31", "18", "6",
    "21", "33", "16", "4", "23", "35", "14", "2",
]

RED_NUMBERS = {
    1, 3, 5, 7, 9, 12, 14, 16, 18,
    19, 21, 23, 25, 27, 30, 32, 34, 36,
}

BET_ALIASES = {
    "red": "red",
    "black": "black",
    "odd": "odd",
    "even": "even",
    "low": "low",
    "1-18": "low",
    "high": "high",
    "19-36": "high",
    "1st12": "dozen1",
    "first12": "dozen1",
    "dozen1": "dozen1",
    "2nd12": "dozen2",
    "second12": "dozen2",
    "dozen2": "dozen2",
    "3rd12": "dozen3",
    "third12": "dozen3",
    "dozen3": "dozen3",
    "column1": "column1",
    "col1": "column1",
    "column2": "column2",
    "col2": "column2",
    "column3": "column3",
    "col3": "column3",
}


def pocket_color(pocket: str) -> str:
    if pocket in {"0", "00"}:
        return "green"
    return "red" if int(pocket) in RED_NUMBERS else "black"


def display_bet(bet_type: str) -> str:
    labels = {
        "red": "Red",
        "black": "Black",
        "odd": "Odd",
        "even": "Even",
        "low": "1–18",
        "high": "19–36",
        "dozen1": "1st 12",
        "dozen2": "2nd 12",
        "dozen3": "3rd 12",
        "column1": "Column 1",
        "column2": "Column 2",
        "column3": "Column 3",
    }
    if bet_type.startswith("number:"):
        return bet_type.split(":", 1)[1]
    return labels[bet_type]


def parse_bet(raw_bet: str) -> Optional[str]:
    value = raw_bet.lower().strip()

    if value in {"0", "00"}:
        return f"number:{value}"

    if value.isdigit() and 1 <= int(value) <= 36:
        return f"number:{int(value)}"

    return BET_ALIASES.get(value)


def payout_multiplier(bet_type: str) -> int:
    """Total return multiplier, including the original stake."""
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

    checks = {
        "red": color == "red",
        "black": color == "black",
        "odd": number % 2 == 1,
        "even": number % 2 == 0,
        "low": 1 <= number <= 18,
        "high": 19 <= number <= 36,
        "dozen1": 1 <= number <= 12,
        "dozen2": 13 <= number <= 24,
        "dozen3": 25 <= number <= 36,
        "column1": number % 3 == 1,
        "column2": number % 3 == 2,
        "column3": number % 3 == 0,
    }
    return checks[bet_type]


class Roulette(commands.Cog):
    """American roulette using Red's shared economy."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=ROULETTE_CONFIG_ID,
            force_registration=True,
        )
        self.config.register_user(
            total_roulette_wins=0,
            total_roulette_losses=0,
            total_roulette_bet=0,
            biggest_roulette_win=0,
        )
        self.active_players = set()

    @commands.command(aliases=["roul"])
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def roulette(self, ctx: commands.Context, bet: int, choice: str):
        """
        Bet CrewCoin on American roulette.

        Examples:
        `[p]roulette 500 red`
        `[p]roulette 500 17`
        `[p]roulette 500 1st12`
        `[p]roulette 500 column2`
        """
        if ctx.author.id in self.active_players:
            return await ctx.send("You already have a roulette spin in progress.")

        parsed_bet = parse_bet(choice)
        if parsed_bet is None:
            return await ctx.send(
                "Invalid bet. Use a number from **0–36**, **00**, red, black, "
                "odd, even, low, high, 1st12, 2nd12, 3rd12, column1, "
                "column2, or column3."
            )

        if bet <= 0:
            return await ctx.send("Bet must be positive.")

        balance = await bank.get_balance(ctx.author)
        if bet > balance:
            currency = await bank.get_currency_name(ctx.guild)
            return await ctx.send(
                f"You do not have enough {currency}. "
                f"Your balance is **{balance:,}**."
            )

        self.active_players.add(ctx.author.id)
        animation_path = None
        result_path = None

        try:
            await bank.withdraw_credits(ctx.author, bet)

            winning_pocket = random.choice(WHEEL_ORDER)
            winning_index = WHEEL_ORDER.index(winning_pocket)

            animation_path = self._make_spin_gif(winning_index)
            spin_embed = discord.Embed(
                title="🎡 Roulette",
                description=(
                    f"**{ctx.author.display_name}** bet **{bet:,}** "
                    f"on **{display_bet(parsed_bet)}**.\n\nThe wheel is spinning..."
                ),
                color=discord.Color.gold(),
            )
            spin_file = discord.File(animation_path, filename="roulette_spin.gif")
            spin_embed.set_image(url="attachment://roulette_spin.gif")
            message = await ctx.send(embed=spin_embed, file=spin_file)

            await asyncio.sleep(4.0)

            won = is_winner(parsed_bet, winning_pocket)
            multiplier = payout_multiplier(parsed_bet)
            payout = bet * multiplier if won else 0
            net = payout - bet if won else -bet

            user_config = self.config.user(ctx.author)
            await user_config.total_roulette_bet.set(
                await user_config.total_roulette_bet() + bet
            )

            if won:
                await bank.deposit_credits(ctx.author, payout)
                await user_config.total_roulette_wins.set(
                    await user_config.total_roulette_wins() + 1
                )
                if payout > await user_config.biggest_roulette_win():
                    await user_config.biggest_roulette_win.set(payout)
            else:
                await user_config.total_roulette_losses.set(
                    await user_config.total_roulette_losses() + 1
                )

            result_path = self._make_result_image(winning_index)
            color_name = pocket_color(winning_pocket)
            color_emoji = {
                "red": "🔴",
                "black": "⚫",
                "green": "🟢",
            }[color_name]

            if won:
                result_text = (
                    f"🎉 **You won!**\n"
                    f"Returned: **{payout:,}** CrewCoin\n"
                    f"Net profit: **+{net:,}** CrewCoin"
                )
                embed_color = discord.Color.green()
            else:
                result_text = (
                    f"💸 **You lost.**\n"
                    f"Loss: **{bet:,}** CrewCoin"
                )
                embed_color = discord.Color.red()

            result_embed = discord.Embed(
                title="🎡 Roulette Result",
                description=(
                    f"The ball landed on {color_emoji} **{winning_pocket} "
                    f"{color_name.title()}**.\n\n"
                    f"Your bet: **{display_bet(parsed_bet)}** for "
                    f"**{bet:,}** CrewCoin\n\n{result_text}"
                ),
                color=embed_color,
            )
            result_file = discord.File(result_path, filename="roulette_result.png")
            result_embed.set_image(url="attachment://roulette_result.png")
            await message.edit(
                embed=result_embed,
                attachments=[result_file],
            )

        except ValueError:
            # Red's bank can raise ValueError if a deposit exceeds max balance.
            # Refund the wager when possible so a player is not unfairly charged.
            try:
                await bank.deposit_credits(ctx.author, bet)
            except Exception:
                pass
            await ctx.send(
                "The payout would exceed the bank's maximum balance. "
                "Your original wager was refunded."
            )
        finally:
            self.active_players.discard(ctx.author.id)
            for path in (animation_path, result_path):
                if path:
                    Path(path).unlink(missing_ok=True)

    @commands.command()
    async def roulettebets(self, ctx: commands.Context):
        """Show available roulette bets and payouts."""
        embed = discord.Embed(
            title="🎡 Roulette Bets",
            description=(
                "**Straight number** — `0`, `00`, or `1–36`: **35:1**\n"
                "**Color** — `red` or `black`: **1:1**\n"
                "**Parity** — `odd` or `even`: **1:1**\n"
                "**Range** — `low` (1–18) or `high` (19–36): **1:1**\n"
                "**Dozens** — `1st12`, `2nd12`, or `3rd12`: **2:1**\n"
                "**Columns** — `column1`, `column2`, or `column3`: **2:1**\n\n"
                "Examples:\n"
                f"`{ctx.clean_prefix}roulette 500 red`\n"
                f"`{ctx.clean_prefix}roulette 500 17`\n"
                f"`{ctx.clean_prefix}roulette 500 column2`"
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="American wheel: both 0 and 00 are included.")
        await ctx.send(embed=embed)

    @commands.command()
    async def roulettestats(self, ctx: commands.Context):
        """Show your roulette statistics."""
        data = await self.config.user(ctx.author).all()
        total_games = (
            data["total_roulette_wins"] + data["total_roulette_losses"]
        )
        win_rate = (
            data["total_roulette_wins"] / total_games * 100
            if total_games
            else 0
        )

        embed = discord.Embed(
            title=f"🎡 {ctx.author.display_name}'s Roulette Stats",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="Record",
            value=(
                f"Wins: **{data['total_roulette_wins']:,}**\n"
                f"Losses: **{data['total_roulette_losses']:,}**\n"
                f"Win rate: **{win_rate:.1f}%**"
            ),
        )
        embed.add_field(
            name="Wagering",
            value=(
                f"Total bet: **{data['total_roulette_bet']:,}**\n"
                f"Biggest return: **{data['biggest_roulette_win']:,}**"
            ),
        )
        await ctx.send(embed=embed)

    def _make_spin_gif(self, winning_index: int) -> str:
        frames = []
        total_frames = 18

        # Begin several rotations away and ease into the winning pocket.
        for frame_number in range(total_frames):
            progress = frame_number / (total_frames - 1)
            eased = 1 - (1 - progress) ** 3
            rotations = 3.5 * (1 - eased)
            offset = winning_index + rotations * len(WHEEL_ORDER)
            frames.append(self._draw_wheel(offset))

        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".gif")
        temp.close()
        frames[0].save(
            temp.name,
            save_all=True,
            append_images=frames[1:],
            duration=130,
            loop=0,
            disposal=2,
        )
        return temp.name

    def _make_result_image(self, winning_index: int) -> str:
        image = self._draw_wheel(winning_index)
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        temp.close()
        image.save(temp.name, "PNG")
        return temp.name

    def _draw_wheel(self, offset: float) -> Image.Image:
        size = 520
        center = size // 2
        outer_radius = 240
        inner_radius = 112
        image = Image.new("RGBA", (size, size), (20, 24, 29, 255))
        draw = ImageDraw.Draw(image)

        segment_angle = 360 / len(WHEEL_ORDER)
        font = self._font(16)
        center_font = self._font(23)

        for index, pocket in enumerate(WHEEL_ORDER):
            start = -90 + (index - offset) * segment_angle
            end = start + segment_angle
            color = pocket_color(pocket)
            fill = {
                "red": (176, 32, 37, 255),
                "black": (30, 33, 38, 255),
                "green": (25, 130, 78, 255),
            }[color]

            draw.pieslice(
                (
                    center - outer_radius,
                    center - outer_radius,
                    center + outer_radius,
                    center + outer_radius,
                ),
                start=start,
                end=end,
                fill=fill,
                outline=(220, 220, 220, 255),
                width=1,
            )

            angle = math.radians((start + end) / 2)
            text_radius = 196
            x = center + math.cos(angle) * text_radius
            y = center + math.sin(angle) * text_radius
            bbox = draw.textbbox((0, 0), pocket, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            draw.text(
                (x - text_width / 2, y - text_height / 2),
                pocket,
                fill=(255, 255, 255, 255),
                font=font,
            )

        # Hub and inner trim.
        draw.ellipse(
            (
                center - inner_radius,
                center - inner_radius,
                center + inner_radius,
                center + inner_radius,
            ),
            fill=(195, 151, 55, 255),
            outline=(245, 220, 145, 255),
            width=4,
        )
        draw.ellipse(
            (
                center - 82,
                center - 82,
                center + 82,
                center + 82,
            ),
            fill=(49, 55, 62, 255),
        )
        label = "CREW\nROULETTE"
        bbox = draw.multiline_textbbox((0, 0), label, font=center_font, align="center")
        draw.multiline_text(
            (
                center - (bbox[2] - bbox[0]) / 2,
                center - (bbox[3] - bbox[1]) / 2,
            ),
            label,
            font=center_font,
            fill=(255, 255, 255, 255),
            align="center",
            spacing=2,
        )

        # Fixed pointer at the top.
        draw.polygon(
            [
                (center, 10),
                (center - 18, 48),
                (center + 18, 48),
            ],
            fill=(245, 205, 68, 255),
            outline=(255, 255, 255, 255),
        )
        draw.ellipse(
            (center - 8, 35, center + 8, 51),
            fill=(245, 245, 245, 255),
        )
        return image

    @staticmethod
    def _font(size: int):
        # DejaVu Sans is normally available with Pillow. Pillow's default
        # bitmap font is used as a safe fallback.
        try:
            return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
        except OSError:
            return ImageFont.load_default()


async def setup(bot):
    await bot.add_cog(Roulette(bot))
