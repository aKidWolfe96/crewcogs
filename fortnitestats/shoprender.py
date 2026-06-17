"""
Graphical item-shop renderer for the FortniteStats cog.

Builds a single PNG that mimics the in-game shop: rarity/section-tinted gradient
tiles, the real item art, item names and V-Bucks prices. All network I/O is async;
the actual PIL compositing runs in an executor so the bot's event loop never blocks.

Public entry point:
    await render_shop_image(loop, shop) -> bytes  (PNG)
"""

import asyncio
import io
import logging
import os

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("red.fortnitestats")

# ----------------------------- layout constants ----------------------------- #
CARD_W = 300
CARD_H = 380
COLS = 3
GAP = 18
MARGIN = 36
TITLE_H = 110
SECTION_H = 64
CARD_RADIUS = 22
ART_FRAC = 0.72            # top fraction of the card used for item art
MAX_CARDS = 60             # safety cap so the image stays a sane height
DOWNLOAD_CONCURRENCY = 10

BG_TOP = (16, 18, 32)
BG_BOTTOM = (10, 11, 20)
TEXT = (255, 255, 255)
SUBTEXT = (210, 214, 224)

FONT_PATHS = {
    "bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
    ],
    "regular": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ],
}

RARITY_RGB = {
    "common": (118, 118, 118),
    "uncommon": (96, 185, 50),
    "rare": (54, 117, 214),
    "epic": (177, 58, 240),
    "legendary": (226, 132, 60),
    "mythic": (229, 193, 59),
    "icon_series": (31, 201, 195),
    "marvel": (197, 61, 62),
    "dc": (79, 93, 214),
    "starwars": (40, 40, 60),
    "gaming_legends": (92, 45, 145),
    "slurp": (47, 217, 210),
    "lava": (212, 90, 34),
    "frozen": (111, 203, 227),
    "shadow": (58, 58, 58),
    "dark": (139, 47, 176),
}
DEFAULT_RGB = (60, 64, 92)


# ------------------------------- font helpers ------------------------------- #
def _font(kind: str, size: int):
    for path in FONT_PATHS.get(kind, []):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    # Pillow >= 10.1 returns a TrueType DejaVu when a size is supplied.
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


# ------------------------------- color helpers ------------------------------ #
def _hex_to_rgb(value, fallback=DEFAULT_RGB):
    if not value or not isinstance(value, str):
        return fallback
    s = value.lstrip("#").strip()
    if len(s) >= 6:
        try:
            return tuple(int(s[i : i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return fallback
    return fallback


def _vertical_gradient(size, top, bottom):
    w, h = size
    base = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(h - 1, 1)
        base.putpixel(
            (0, y),
            (
                int(top[0] + (bottom[0] - top[0]) * t),
                int(top[1] + (bottom[1] - top[1]) * t),
                int(top[2] + (bottom[2] - top[2]) * t),
            ),
        )
    return base.resize((w, h))


def _rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius, fill=255)
    return mask


def _fit_contain(img, box_w, box_h):
    img = img.convert("RGBA")
    img.thumbnail((box_w, box_h), Image.LANCZOS)
    return img


def _wrap(draw, text, font, max_w, max_lines=2):
    words = text.split()
    lines, cur, truncated = [], "", False
    i = 0
    while i < len(words):
        word = words[i]
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
            i += 1
        else:
            if cur:
                lines.append(cur)
                cur = ""
            else:
                # single word too long for the line; force it on and move on
                lines.append(word)
                i += 1
            if len(lines) == max_lines:
                truncated = i < len(words)
                cur = ""
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if i < len(words):
        truncated = True

    # add an ellipsis to the final line if we dropped content or it's too wide
    if lines and (truncated or draw.textlength(lines[-1], font=font) > max_w):
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines[-1] = (last + "…") if last else "…"
    return lines[:max_lines]


# ------------------------------- compositing -------------------------------- #
def _compose(sections, vbuck_bytes, date_str) -> bytes:
    """Synchronous, executor-friendly. `sections` is a list of (name, cards),
    where each card is a dict: art_bytes|None, name, price, c_top, c_bottom."""
    f_title = _font("bold", 52)
    f_date = _font("regular", 24)
    f_section = _font("bold", 30)
    f_name = _font("bold", 22)
    f_price = _font("bold", 22)

    grid_w = COLS * CARD_W + (COLS - 1) * GAP
    canvas_w = grid_w + MARGIN * 2

    # measure total height
    total_h = TITLE_H + MARGIN
    for _, cards in sections:
        rows = (len(cards) + COLS - 1) // COLS
        total_h += SECTION_H + rows * CARD_H + (rows - 1) * GAP + GAP
    total_h += MARGIN

    canvas = Image.new("RGB", (canvas_w, total_h), BG_TOP)
    canvas.paste(_vertical_gradient((canvas_w, total_h), BG_TOP, BG_BOTTOM), (0, 0))
    draw = ImageDraw.Draw(canvas)

    # title
    draw.text((MARGIN, 30), "FORTNITE ITEM SHOP", font=f_title, fill=TEXT)
    draw.text((MARGIN, 88), date_str, font=f_date, fill=SUBTEXT)

    # prepared V-Bucks icon (small)
    vbuck_icon = None
    if vbuck_bytes:
        try:
            vbuck_icon = Image.open(io.BytesIO(vbuck_bytes)).convert("RGBA")
            vbuck_icon.thumbnail((26, 26), Image.LANCZOS)
        except Exception:  # noqa: BLE001
            vbuck_icon = None

    y = TITLE_H + MARGIN
    for section_name, cards in sections:
        # section header with an accent bar
        draw.rounded_rectangle([MARGIN, y + 14, MARGIN + 6, y + 44], 3, fill=(90, 200, 250))
        draw.text((MARGIN + 18, y + 12), section_name.upper(), font=f_section, fill=TEXT)
        y += SECTION_H

        for idx, card in enumerate(cards):
            col = idx % COLS
            row = idx // COLS
            cx = MARGIN + col * (CARD_W + GAP)
            cy = y + row * (CARD_H + GAP)

            # gradient tile
            tile = _vertical_gradient((CARD_W, CARD_H), card["c_top"], card["c_bottom"]).convert(
                "RGBA"
            )

            # item art, contained in the top region
            if card["art_bytes"]:
                try:
                    art = Image.open(io.BytesIO(card["art_bytes"]))
                    art = _fit_contain(art, CARD_W - 24, int(CARD_H * ART_FRAC))
                    ax = (CARD_W - art.width) // 2
                    ay = max(8, int(CARD_H * ART_FRAC) - art.height + 8)
                    tile.alpha_composite(art, (ax, ay))
                except Exception:  # noqa: BLE001
                    pass

            # bottom scrim for legibility
            scrim_h = int(CARD_H * 0.42)
            scrim = Image.new("RGBA", (CARD_W, scrim_h), (0, 0, 0, 0))
            sdraw = ImageDraw.Draw(scrim)
            for i in range(scrim_h):
                alpha = int(180 * (i / scrim_h))
                sdraw.line([(0, i), (CARD_W, i)], fill=(0, 0, 0, alpha))
            tile.alpha_composite(scrim, (0, CARD_H - scrim_h))

            tdraw = ImageDraw.Draw(tile)
            # name (wrapped, bottom-anchored above price)
            name_lines = _wrap(tdraw, card["name"], f_name, CARD_W - 28, max_lines=2)
            line_h = f_name.getbbox("Ag")[3] + 4
            price_y = CARD_H - 44
            name_block_h = len(name_lines) * line_h
            ny = price_y - name_block_h - 6
            for line in name_lines:
                tdraw.text((16, ny), line, font=f_name, fill=TEXT)
                ny += line_h

            # price row with V-Bucks icon
            price_str = f"{card['price']:,}" if card["price"] is not None else "—"
            px = 16
            if vbuck_icon is not None:
                tile.alpha_composite(vbuck_icon, (px, price_y - 2))
                px += vbuck_icon.width + 6
            tdraw.text((px, price_y), price_str, font=f_price, fill=(255, 236, 120))

            # round the corners and drop it on the canvas
            mask = _rounded_mask((CARD_W, CARD_H), CARD_RADIUS)
            canvas.paste(tile, (cx, cy), mask)

        rows = (len(cards) + COLS - 1) // COLS
        y += rows * CARD_H + (rows - 1) * GAP + GAP

    # footer
    draw.text(
        (MARGIN, total_h - 28),
        "data via fortnite-api.com · not affiliated with Epic Games",
        font=_font("regular", 16),
        fill=(120, 124, 140),
    )

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ------------------------------- async driver ------------------------------- #
def _pick_art(entry):
    """Return the best Asset for an entry's display item, or None."""
    items = entry.br or []
    for item in items:
        if not item.images:
            continue
        asset = item.images.featured or item.images.icon
        if asset:
            return asset, item
    return None, (items[0] if items else None)


def _colors_for(entry, item):
    """Gradient (top, bottom) using the shop tile colors, else rarity, else default."""
    colors = getattr(entry, "colors", None)
    if colors and colors.color1:
        top = _hex_to_rgb(colors.color1)
        bottom = _hex_to_rgb(colors.color3 or colors.color2 or colors.color1, top)
        # darken the bottom a touch for depth
        bottom = tuple(max(0, int(c * 0.55)) for c in bottom)
        return top, bottom
    rar = None
    if item is not None and item.rarity is not None:
        rar = item.rarity.value.lower()
    base = RARITY_RGB.get(rar, DEFAULT_RGB)
    return base, tuple(max(0, int(c * 0.45)) for c in base)


async def _download(asset, sem):
    async with sem:
        try:
            data = asset.read()
            if asyncio.iscoroutine(data):
                data = await data
            return data
        except Exception:  # noqa: BLE001
            return None


async def render_shop_image(loop, shop) -> bytes:
    """Build the shop PNG. Raises nothing fatal — returns bytes or raises on a
    truly unexpected error (the cog wraps this in try/except)."""
    sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

    # group entries by section, dedupe by offer id, cap total
    sections: dict[str, list] = {}
    order: list[str] = []
    seen = set()
    count = 0
    for entry in shop.entries:
        if count >= MAX_CARDS:
            break
        oid = getattr(entry, "offer_id", None)
        if oid and oid in seen:
            continue
        if oid:
            seen.add(oid)
        layout = getattr(entry, "layout", None)
        section = layout.name if layout and layout.name else "Featured"
        asset, item = _pick_art(entry)
        if item is None:
            continue
        name = item.name or getattr(entry, "dev_name", None) or "Unknown"
        top, bottom = _colors_for(entry, item)
        if section not in sections:
            sections[section] = []
            order.append(section)
        sections[section].append(
            {
                "asset": asset,
                "name": name,
                "price": getattr(entry, "final_price", None),
                "c_top": top,
                "c_bottom": bottom,
            }
        )
        count += 1

    # download all art + the V-Bucks icon concurrently
    tasks, refs = [], []
    for section in order:
        for card in sections[section]:
            if card["asset"] is not None:
                tasks.append(_download(card["asset"], sem))
                refs.append(card)
    vbuck_task = _download(shop.vbuck_icon, sem) if getattr(shop, "vbuck_icon", None) else None

    results = await asyncio.gather(*tasks) if tasks else []
    for card, data in zip(refs, results):
        card["art_bytes"] = data
    for section in order:
        for card in sections[section]:
            card.setdefault("art_bytes", None)
            card.pop("asset", None)
    vbuck_bytes = await vbuck_task if vbuck_task is not None else None

    date_str = shop.date.strftime("%A, %B %d, %Y") if shop.date else "Today"
    ordered_sections = [(s, sections[s]) for s in order if sections[s]]

    # composite off the event loop
    return await loop.run_in_executor(None, _compose, ordered_sections, vbuck_bytes, date_str)
