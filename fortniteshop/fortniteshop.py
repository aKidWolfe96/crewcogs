"""
Fortnite Item Shop Cog for Red-DiscordBot
Fetches today's Fortnite item shop and renders it as an image in chat.

Requirements (install in your Red venv):
    pip install aiohttp pillow

API: https://fortnite-api.com  (free, no key required)
"""

import asyncio
import io
from datetime import datetime, timezone

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont
from redbot.core import commands
from redbot.core.bot import Red

SHOP_URL = "https://fortnite-api.com/v2/shop"

# Rarity colours (background tint for each item tile)
RARITY_COLOURS = {
    "common":    (0x80, 0x80, 0x80),
    "uncommon":  (0x1E, 0x85, 0x1F),
    "rare":      (0x28, 0x6A, 0xCC),
    "epic":      (0x86, 0x27, 0xC8),
    "legendary": (0xC8, 0x7D, 0x27),
    "mythic":    (0xF0, 0xD0, 0x20),
    "exotic":    (0x26, 0xCC, 0xBF),
    "icon":      (0x00, 0xBE, 0xD5),
}
DEFAULT_COLOUR = (0x40, 0x40, 0x40)

# Tile dimensions
TILE_W, TILE_H = 180, 220
PADDING = 12
COLS = 6          # items per row
HEADER_H = 60     # space for date header
FOOTER_H = 30     # space for "powered by" footer
BG_COLOUR = (15, 15, 20)
TEXT_COLOUR = (255, 255, 255)
PRICE_COLOUR = (255, 220, 50)


async def _fetch_json(session: aiohttp.ClientSession, url: str) -> dict:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        return await resp.json()


async def _fetch_image(session: aiohttp.ClientSession, url: str) -> Image.Image | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.read()
                return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        pass
    return None


def _rarity_colour(rarity_str: str) -> tuple[int, int, int]:
    if not rarity_str:
        return DEFAULT_COLOUR
    return RARITY_COLOURS.get(rarity_str.lower(), DEFAULT_COLOUR)


def _draw_tile(
    draw: ImageDraw.ImageDraw,
    canvas: Image.Image,
    item_img: Image.Image | None,
    name: str,
    price: int | None,
    rarity: str,
    x: int,
    y: int,
) -> None:
    """Draw a single item tile onto the canvas."""
    colour = _rarity_colour(rarity)
    dark = tuple(max(0, c - 40) for c in colour)

    # Background gradient approximation (two-colour fill)
    for row in range(TILE_H):
        t = row / TILE_H
        r = int(colour[0] * (1 - t) + dark[0] * t)
        g = int(colour[1] * (1 - t) + dark[1] * t)
        b = int(colour[2] * (1 - t) + dark[2] * t)
        draw.line([(x, y + row), (x + TILE_W - 1, y + row)], fill=(r, g, b))

    # Border
    draw.rectangle([x, y, x + TILE_W - 1, y + TILE_H - 1], outline=(255, 255, 255, 80), width=1)

    # Item image (top 65 % of tile)
    img_area_h = int(TILE_H * 0.65)
    if item_img:
        thumb = item_img.copy()
        thumb.thumbnail((TILE_W - 4, img_area_h - 4), Image.LANCZOS)
        ix = x + (TILE_W - thumb.width) // 2
        iy = y + (img_area_h - thumb.height) // 2
        canvas.paste(thumb, (ix, iy), thumb)

    # Name (up to 2 lines, truncated)
    name_y = y + img_area_h + 4
    font_size = 13
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
    except OSError:
        font = ImageFont.load_default()
        small_font = font

    # Truncate name to fit tile width
    max_chars = 18
    display_name = name if len(name) <= max_chars else name[:max_chars - 1] + "…"
    draw.text((x + TILE_W // 2, name_y), display_name, fill=TEXT_COLOUR, font=font, anchor="mt")

    # Price
    if price is not None:
        price_y = y + TILE_H - 20
        draw.text(
            (x + TILE_W // 2, price_y),
            f"⟡ {price:,}",
            fill=PRICE_COLOUR,
            font=small_font,
            anchor="mt",
        )


def _build_shop_image(items: list[dict], date_str: str) -> io.BytesIO:
    """Render all shop items into a single PNG and return it as a BytesIO buffer."""
    n = len(items)
    rows = (n + COLS - 1) // COLS

    width = COLS * (TILE_W + PADDING) + PADDING
    height = HEADER_H + rows * (TILE_H + PADDING) + PADDING + FOOTER_H

    canvas = Image.new("RGB", (width, height), BG_COLOUR)
    draw = ImageDraw.Draw(canvas)

    # Header
    try:
        header_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 26
        )
        footer_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12
        )
    except OSError:
        header_font = ImageFont.load_default()
        footer_font = header_font

    draw.text(
        (width // 2, HEADER_H // 2),
        f"🛒  Fortnite Item Shop  •  {date_str}",
        fill=TEXT_COLOUR,
        font=header_font,
        anchor="mm",
    )

    # Tiles (images already downloaded and attached to each item dict)
    for idx, item in enumerate(items):
        col = idx % COLS
        row = idx // COLS
        x = PADDING + col * (TILE_W + PADDING)
        y = HEADER_H + PADDING + row * (TILE_H + PADDING)

        _draw_tile(
            draw,
            canvas,
            item.get("_img"),
            item.get("name", "Unknown"),
            item.get("price"),
            item.get("rarity", ""),
            x,
            y,
        )

    # Footer
    draw.text(
        (width // 2, height - FOOTER_H // 2),
        "Data provided by fortnite-api.com",
        fill=(150, 150, 150),
        font=footer_font,
        anchor="mm",
    )

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


class FortniteShop(commands.Cog):
    """Displays today's Fortnite Item Shop as an image."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot

    @commands.command(name="fortniteshop", aliases=["fnshop", "fortnite"])
    @commands.cooldown(1, 60, commands.BucketType.guild)
    async def fortniteshop(self, ctx: commands.Context) -> None:
        """Show today's Fortnite Item Shop."""
        async with ctx.typing():
            try:
                items, date_str = await self._get_shop_data()
            except aiohttp.ClientResponseError as exc:
                await ctx.send(f"❌ Fortnite API returned an error: HTTP {exc.status}")
                return
            except Exception as exc:
                await ctx.send(f"❌ Failed to fetch the item shop: {exc}")
                return

            if not items:
                await ctx.send("⚠️ The item shop appears to be empty right now.")
                return

            try:
                buf = await asyncio.get_event_loop().run_in_executor(
                    None, _build_shop_image, items, date_str
                )
            except Exception as exc:
                await ctx.send(f"❌ Failed to render the shop image: {exc}")
                return

            file = discord.File(buf, filename="fortnite_shop.png")
            embed = discord.Embed(
                title="🛒 Fortnite Item Shop",
                description=f"**{len(items)} items** available today • {date_str}",
                colour=discord.Colour.blue(),
            )
            embed.set_image(url="attachment://fortnite_shop.png")
            embed.set_footer(text="Data: fortnite-api.com • Updates daily at 00:00 UTC")
            await ctx.send(file=file, embed=embed)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_shop_data(self) -> tuple[list[dict], str]:
        """
        Fetch shop JSON from fortnite-api.com, download item images
        concurrently, and return (items_list, date_string).
        """
        async with aiohttp.ClientSession() as session:
            data = await _fetch_json(session, SHOP_URL)

        # Flatten all entries from the shop sections
        raw_entries: list[dict] = []
        shop_data = data.get("data", {})

        # fortnite-api.com v2 shop structure: data.entries  (single list)
        # or data.featured / data.daily for older structure
        if "entries" in shop_data:
            raw_entries = shop_data["entries"]
        else:
            for section_key in ("featured", "daily", "specialFeatured", "specialDaily"):
                section = shop_data.get(section_key) or {}
                raw_entries.extend(section.get("entries", []))

        # Parse useful fields
        items: list[dict] = []
        for entry in raw_entries:
            # Some entries have multiple "bundle" items; prefer bundle name if present
            bundle = entry.get("bundle") or {}
            name = bundle.get("name") or ""

            # Items list
            br_items = entry.get("items", []) or entry.get("brItems", [])
            if not name and br_items:
                name = br_items[0].get("name", "Unknown")

            price = entry.get("regularPrice") or entry.get("finalPrice")

            rarity = ""
            img_url = ""
            if br_items:
                first = br_items[0]
                rarity_obj = first.get("rarity") or {}
                rarity = rarity_obj.get("value", "") if isinstance(rarity_obj, dict) else str(rarity_obj)
                images = first.get("images") or {}
                img_url = (
                    images.get("featured")
                    or images.get("icon")
                    or images.get("smallIcon")
                    or ""
                )
            elif bundle:
                img_url = bundle.get("image", "")

            if name:
                items.append(
                    {
                        "name": name,
                        "price": price,
                        "rarity": rarity,
                        "_img_url": img_url,
                        "_img": None,
                    }
                )

        # Date string
        raw_date = shop_data.get("date", "")
        try:
            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            date_str = dt.strftime("%B %d, %Y")
        except Exception:
            date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

        # Download images concurrently (cap to 60 tiles to keep image manageable)
        items = items[:60]
        async with aiohttp.ClientSession() as session:
            tasks = [_fetch_image(session, item["_img_url"]) for item in items]
            images = await asyncio.gather(*tasks)
        for item, img in zip(items, images):
            item["_img"] = img

        return items, date_str


async def setup(bot: Red) -> None:
    await bot.add_cog(FortniteShop(bot))
