import asyncio
import logging
import math
import random
import tempfile
from pathlib import Path
from typing import Optional

import discord
from PIL import Image, ImageDraw, ImageFont
from redbot.core import Config, bank, commands

from .casino_core import mark_played, refund_wager, settle_game, validate_bet

ROULETTE_CONFIG_ID = 8642097531
WHEEL_ORDER = ["0", "28", "9", "26", "30", "11", "7", "20", "32", "17", "5", "22", "34", "15", "3", "24", "36", "13", "1", "00", "27", "10", "25", "29", "12", "8", "19", "31", "18", "6", "21", "33", "16", "4", "23", "35", "14", "2"]
RED_NUMBERS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
BET_ALIASES = {
    "red":"red", "black":"black", "odd":"odd", "even":"even", "low":"low", "1-18":"low",
    "high":"high", "19-36":"high", "1st12":"dozen1", "first12":"dozen1", "dozen1":"dozen1",
    "2nd12":"dozen2", "second12":"dozen2", "dozen2":"dozen2", "3rd12":"dozen3",
    "third12":"dozen3", "dozen3":"dozen3", "column1":"column1", "col1":"column1",
    "column2":"column2", "col2":"column2", "column3":"column3", "col3":"column3",
}


def pocket_color(pocket: str) -> str:
    if pocket in {"0", "00"}:
        return "green"
    return "red" if int(pocket) in RED_NUMBERS else "black"


def display_bet(bet_type: str) -> str:
    labels = {"red":"Red", "black":"Black", "odd":"Odd", "even":"Even", "low":"1–18", "high":"19–36", "dozen1":"1st 12", "dozen2":"2nd 12", "dozen3":"3rd 12", "column1":"Column 1", "column2":"Column 2", "column3":"Column 3"}
    return bet_type.split(":", 1)[1] if bet_type.startswith("number:") else labels[bet_type]


def parse_bet(raw_bet: str) -> Optional[str]:
    value = raw_bet.lower().strip()
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


class Roulette(commands.Cog):
    """American roulette using Red's shared economy."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=ROULETTE_CONFIG_ID, force_registration=True)
        self.config.register_user(total_roulette_wins=0, total_roulette_losses=0, total_roulette_bet=0, biggest_roulette_win=0)
        self.active_players = set()

    @commands.command(aliases=["roul"])
    @commands.max_concurrency(1, per=commands.BucketType.user, wait=False)
    async def roulette(self, ctx: commands.Context, bet: int, choice: str):
        if ctx.author.id in self.active_players:
            return await ctx.send("You already have a roulette spin in progress.")
        parsed_bet = parse_bet(choice)
        if parsed_bet is None:
            return await ctx.send("Invalid bet. Use 0–36, 00, red, black, odd, even, low, high, 1st12, 2nd12, 3rd12, or column1–3.")
        error = await validate_bet(ctx, "roulette", bet)
        if error:
            return await ctx.send(error)

        self.active_players.add(ctx.author.id)
        animation_path = result_path = None
        withdrawn = False
        settled = False
        try:
            await bank.withdraw_credits(ctx.author, bet)
            withdrawn = True
            mark_played(ctx.guild.id, ctx.author.id, "roulette")
            winning_pocket = random.choice(WHEEL_ORDER)
            winning_index = WHEEL_ORDER.index(winning_pocket)
            animation_path = self._make_spin_gif(winning_index)
            embed = discord.Embed(title="🎡 Roulette", description=f"**{ctx.author.display_name}** bet **{bet:,}** on **{display_bet(parsed_bet)}**.\n\nThe wheel is spinning...", color=discord.Color.gold())
            file = discord.File(animation_path, filename="roulette_spin.gif")
            embed.set_image(url="attachment://roulette_spin.gif")
            message = await ctx.send(embed=embed, file=file)
            await asyncio.sleep(4)

            won = is_winner(parsed_bet, winning_pocket)
            payout = bet * payout_multiplier(parsed_bet) if won else 0
            outcome = "win" if won else "loss"
            settlement = await settle_game(ctx.author, "roulette", bet, payout, outcome)
            deposited = settlement.deposited
            settled = True

            cfg = self.config.user(ctx.author)
            await cfg.total_roulette_bet.set(await cfg.total_roulette_bet() + bet)
            if won:
                await cfg.total_roulette_wins.set(await cfg.total_roulette_wins() + 1)
                if deposited > await cfg.biggest_roulette_win():
                    await cfg.biggest_roulette_win.set(deposited)
            else:
                await cfg.total_roulette_losses.set(await cfg.total_roulette_losses() + 1)
            result_path = self._make_result_image(winning_index)
            color_name = pocket_color(winning_pocket)
            emoji = {"red":"🔴", "black":"⚫", "green":"🟢"}[color_name]
            if won:
                result_text = f"🎉 **You won!**\nReturned: **{deposited:,}** CrewCoin\nNet profit: **{deposited - bet:+,}** CrewCoin"
                embed_color = discord.Color.green()
            else:
                result_text = f"💸 **You lost.**\nLoss: **{bet:,}** CrewCoin"
                embed_color = discord.Color.red()
            result_embed = discord.Embed(title="🎡 Roulette Result", description=f"The ball landed on {emoji} **{winning_pocket} {color_name.title()}**.\n\nYour bet: **{display_bet(parsed_bet)}** for **{bet:,}** CrewCoin\n\n{result_text}", color=embed_color)
            result_file = discord.File(result_path, filename="roulette_result.png")
            result_embed.set_image(url="attachment://roulette_result.png")
            await message.edit(embed=result_embed, attachments=[result_file])
        except Exception:
            if withdrawn and not settled:
                await refund_wager(ctx.author, "roulette", bet, reason="command failure before settlement")
            logging.getLogger("red.crewcogs.casino.roulette").exception(
                "Roulette failed for user %s", ctx.author.id
            )
            await ctx.send(
                "Roulette settled, but the result message or legacy stats failed."
                if settled
                else "Roulette hit an unexpected error. The unsettled wager was refunded."
            )
        finally:
            self.active_players.discard(ctx.author.id)
            for path in (animation_path, result_path):
                if path:
                    Path(path).unlink(missing_ok=True)

    @commands.command()
    async def roulettebets(self, ctx):
        description = "**Straight number** — `0`, `00`, or `1–36`: **35:1**\n**Color** — `red` or `black`: **1:1**\n**Parity** — `odd` or `even`: **1:1**\n**Range** — `low` or `high`: **1:1**\n**Dozens** — `1st12`, `2nd12`, `3rd12`: **2:1**\n**Columns** — `column1`, `column2`, `column3`: **2:1**"
        await ctx.send(embed=discord.Embed(title="🎡 Roulette Bets", description=description, color=discord.Color.gold()))

    @commands.command()
    async def roulettestats(self, ctx):
        data = await self.config.user(ctx.author).all()
        games = data["total_roulette_wins"] + data["total_roulette_losses"]
        rate = data["total_roulette_wins"] / games * 100 if games else 0
        embed = discord.Embed(title=f"🎡 {ctx.author.display_name}'s Roulette Stats", color=discord.Color.gold())
        embed.add_field(name="Record", value=f"Wins: **{data['total_roulette_wins']:,}**\nLosses: **{data['total_roulette_losses']:,}**\nWin rate: **{rate:.1f}%**")
        embed.add_field(name="Wagering", value=f"Total bet: **{data['total_roulette_bet']:,}**\nBiggest return: **{data['biggest_roulette_win']:,}**")
        await ctx.send(embed=embed)

    def _make_spin_gif(self, winning_index: int) -> str:
        frames = []
        for frame_number in range(18):
            progress = frame_number / 17
            eased = 1 - (1 - progress) ** 3
            offset = winning_index + 3.5 * (1 - eased) * len(WHEEL_ORDER)
            frames.append(self._draw_wheel(offset))
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".gif")
        temp.close()
        frames[0].save(temp.name, save_all=True, append_images=frames[1:], duration=130, loop=0, disposal=2)
        for frame in frames:
            frame.close()
        return temp.name

    def _make_result_image(self, winning_index: int) -> str:
        image = self._draw_wheel(winning_index)
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        temp.close()
        image.save(temp.name, "PNG")
        image.close()
        return temp.name

    def _draw_wheel(self, offset: float) -> Image.Image:
        size, center, outer_radius, inner_radius = 520, 260, 240, 112
        image = Image.new("RGBA", (size, size), (20, 24, 29, 255))
        draw = ImageDraw.Draw(image)
        segment_angle = 360 / len(WHEEL_ORDER)
        font, center_font = self._font(16), self._font(23)
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
        label = "CREW\nROULETTE"
        box = draw.multiline_textbbox((0,0), label, font=center_font, align="center")
        draw.multiline_text((center-(box[2]-box[0])/2, center-(box[3]-box[1])/2), label, font=center_font, fill="white", align="center")
        draw.polygon([(center,10),(center-18,48),(center+18,48)], fill=(245,205,68,255), outline="white")
        draw.ellipse((center-8,35,center+8,51), fill=(245,245,245,255))
        return image

    @staticmethod
    def _font(size: int):
        try:
            return ImageFont.truetype("DejaVuSans-Bold.ttf", size)
        except OSError:
            return ImageFont.load_default()


async def setup(bot):
    await bot.add_cog(Roulette(bot))
