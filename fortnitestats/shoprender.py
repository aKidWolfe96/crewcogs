"""
Graphical item-shop renderer for the FortniteStats cog.

Builds a single PNG that mimics the in-game shop: full-bleed item tile art
(the same renders Epic ships in the shop), with names and V-Bucks prices
overlaid on a gradient scrim. All network I/O is async; the PIL compositing
runs in an executor so the bot's event loop never blocks.

Public entry point:
    await render_shop_image(loop, shop) -> bytes  (PNG)
"""

import asyncio
import io
import logging
import os

from PIL import Image, ImageDraw, ImageFont, ImageFilter

log = logging.getLogger("red.fortnitestats")

# ----------------------------- layout constants ----------------------------- #
CARD_W = 256
CARD_H = 300
COLS = 4
GAP = 16
MARGIN = 32
TITLE_H = 120
SECTION_H = 58
CARD_RADIUS = 18
MAX_CARDS = 80
DOWNLOAD_CONCURRENCY = 12

BG_TOP = (18, 20, 34)
BG_BOTTOM = (8, 9, 16)
TEXT = (255, 255, 255)
SUBTEXT = (200, 205, 218)
ACCENT = (90, 200, 250)
PRICE_COLOR = (255, 233, 110)

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
    "common": (130, 130, 138),
    "uncommon": (96, 185, 50),
    "rare": (44, 138, 222),
    "epic": (170, 60, 240),
    "legendary": (230, 132, 50),
    "mythic": (232, 196, 50),
    "icon_series": (38, 210, 200),
    "marvel": (200, 55, 56),
    "dc": (70, 90, 220),
    "starwars": (40, 44, 70),
    "gaming_legends": (110, 50, 168),
    "slurp": (40, 220, 210),
    "lava": (216, 92, 34),
    "frozen": (120, 205, 230),
    "shadow": (62, 62, 70),
    "dark": (150, 50, 188),
}
DEFAULT_RGB = (70, 78, 110)


# ------------------------------- font helpers ------------------------------- #
def _font(kind: str, size: int):
    for path in FONT_PATHS.get(kind, []):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
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


def _cover_fit(img, box_w, box_h, anchor_top=True):
    """Scale to fully cover the box, then crop. Crops from the top by default so
    character faces (top of the render) are preserved and legs get cut instead."""
    img = img.convert("RGBA")
    iw, ih = img.size
    scale = max(box_w / iw, box_h / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - box_w) // 2
    top = 0 if anchor_top else (nh - box_h) // 2
    return img.crop((left, top, left + box_w, top + box_h))


def _contain_fit(img, box_w, box_h):
    img = img.convert("RGBA")
    img.thumbnail((box_w, box_h), Image.LANCZOS)
    return img


def _wrap(draw, text, font, max_w, max_lines=2):
    words = text.split()
    lines, cur = [], ""
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
                lines.append(word)
                i += 1
            if len(lines) == max_lines:
                cur = ""
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if lines and (i < len(words) or draw.textlength(lines[-1], font=font) > max_w):
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines[-1] = (last + "…") if last else "…"
    return lines[:max_lines]


# ------------------------------- compositing -------------------------------- #
def _render_card(card, fonts, vbuck_icon):
    """Return a finished RGBA tile (CARD_W x CARD_H) for one offer."""
    f_name, f_price = fonts["name"], fonts["price"]
    top, bottom = card["c_top"], card["c_bottom"]

    # base: always a gradient (shows through transparent icon art / art gaps)
    tile = _vertical_gradient((CARD_W, CARD_H), top, bottom).convert("RGBA")

    art_bytes = card.get("art_bytes")
    if art_bytes:
        try:
            art = Image.open(io.BytesIO(art_bytes))
            if card.get("full_bleed"):
                filled = _cover_fit(art, CARD_W, CARD_H, anchor_top=True)
                tile.alpha_composite(filled, (0, 0))
            else:
                fitted = _contain_fit(art, CARD_W - 28, int(CARD_H * 0.7))
                ax = (CARD_W - fitted.width) // 2
                ay = max(10, int(CARD_H * 0.7) - fitted.height + 6)
                tile.alpha_composite(fitted, (ax, ay))
        except Exception:  # noqa: BLE001
            pass

    # bottom scrim for text legibility
    scrim_h = int(CARD_H * 0.46)
    scrim = Image.new("RGBA", (CARD_W, scrim_h), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(scrim)
    for yy in range(scrim_h):
        a = int(205 * (yy / scrim_h) ** 1.3)
        sdraw.line([(0, yy), (CARD_W, yy)], fill=(0, 0, 0, a))
    tile.alpha_composite(scrim, (0, CARD_H - scrim_h))

    # rarity accent strip along the bottom edge
    tdraw = ImageDraw.Draw(tile)
    tdraw.rectangle([0, CARD_H - 5, CARD_W, CARD_H], fill=top + (255,))

    # name + price
    name_lines = _wrap(tdraw, card["name"], f_name, CARD_W - 28, max_lines=2)
    line_h = f_name.getbbox("Ag")[3] + 4
    price_y = CARD_H - 42
    ny = price_y - len(name_lines) * line_h - 6
    for line in name_lines:
        # subtle shadow for pop
        tdraw.text((15, ny + 1), line, font=f_name, fill=(0, 0, 0))
        tdraw.text((14, ny), line, font=f_name, fill=TEXT)
        ny += line_h

    price_str = f"{card['price']:,}" if card["price"] is not None else "—"
    px = 14
    if vbuck_icon is not None:
        tile.alpha_composite(vbuck_icon, (px, price_y - 1))
        px += vbuck_icon.width + 6
    tdraw.text((px, price_y), price_str, font=f_price, fill=PRICE_COLOR)

    # round the corners
    rounded = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    rounded.paste(tile, (0, 0), _rounded_mask((CARD_W, CARD_H), CARD_RADIUS))
    return rounded


def _compose(sections, vbuck_bytes, date_str) -> bytes:
    fonts = {
        "title": _font("bold", 52),
        "date": _font("regular", 24),
        "section": _font("bold", 30),
        "name": _font("bold", 21),
        "price": _font("bold", 21),
    }

    grid_w = COLS * CARD_W + (COLS - 1) * GAP
    canvas_w = grid_w + MARGIN * 2

    total_h = TITLE_H + MARGIN
    for _, cards in sections:
        rows = (len(cards) + COLS - 1) // COLS
        total_h += SECTION_H + rows * CARD_H + (rows - 1) * GAP + GAP
    total_h += MARGIN

    canvas = Image.new("RGB", (canvas_w, total_h), BG_TOP)
    canvas.paste(_vertical_gradient((canvas_w, total_h), BG_TOP, BG_BOTTOM), (0, 0))
    draw = ImageDraw.Draw(canvas)

    draw.text((MARGIN, 34), "FORTNITE ITEM SHOP", font=fonts["title"], fill=TEXT)
    draw.text((MARGIN, 92), date_str, font=fonts["date"], fill=SUBTEXT)

    vbuck_icon = None
    if vbuck_bytes:
        try:
            vbuck_icon = Image.open(io.BytesIO(vbuck_bytes)).convert("RGBA")
            vbuck_icon.thumbnail((24, 24), Image.LANCZOS)
        except Exception:  # noqa: BLE001
            vbuck_icon = None

    y = TITLE_H + MARGIN
    for section_name, cards in sections:
        draw.rounded_rectangle([MARGIN, y + 10, MARGIN + 6, y + 40], 3, fill=ACCENT)
        draw.text((MARGIN + 18, y + 8), section_name.upper(), font=fonts["section"], fill=TEXT)
        y += SECTION_H

        for idx, card in enumerate(cards):
            col = idx % COLS
            row = idx // COLS
            cx = MARGIN + col * (CARD_W + GAP)
            cy = y + row * (CARD_H + GAP)
            tile = _render_card(card, fonts, vbuck_icon)
            canvas.paste(tile, (cx, cy), tile)

        rows = (len(cards) + COLS - 1) // COLS
        y += rows * CARD_H + (rows - 1) * GAP + GAP

    draw.text(
        (MARGIN, total_h - 26),
        "data via fortnite-api.com · not affiliated with Epic Games",
        font=_font("regular", 15),
        fill=(115, 120, 138),
    )

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ------------------------------- async driver ------------------------------- #
def _pick_art(entry):
    """Return (asset, item, full_bleed). Prefers the full shop-tile render."""
    items = entry.br or []
    item = items[0] if items else None

    # 1) the real in-game shop tile render (best looking)
    nda = getattr(entry, "new_display_asset", None)
    if nda and getattr(nda, "images", None):
        imgs = nda.images
        offer = getattr(imgs, "offer_image", None) or imgs.get("OfferImage") if imgs else None
        if offer:
            return offer, item, True

    # 2) the cosmetic's featured poster art (also fills nicely)
    if item and item.images and item.images.featured:
        return item.images.featured, item, True

    # 3) plain icon on a gradient
    if item and item.images and item.images.icon:
        return item.images.icon, item, False

    return None, item, False


def _colors_for(entry, item):
    colors = getattr(entry, "colors", None)
    if colors and colors.color1:
        top = _hex_to_rgb(colors.color1)
        bottom = _hex_to_rgb(colors.color3 or colors.color2 or colors.color1, top)
        bottom = tuple(max(0, int(c * 0.5)) for c in bottom)
        return top, bottom
    rar = item.rarity.value.lower() if (item and item.rarity) else None
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
    sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

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
        asset, item, full_bleed = _pick_art(entry)
        name = (item.name if item else None) or getattr(entry, "dev_name", None) or "Unknown"
        top, bottom = _colors_for(entry, item)
        if section not in sections:
            sections[section] = []
            order.append(section)
        sections[section].append(
            {
                "asset": asset,
                "full_bleed": full_bleed,
                "name": name,
                "price": getattr(entry, "final_price", None),
                "c_top": top,
                "c_bottom": bottom,
            }
        )
        count += 1

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

    return await loop.run_in_executor(None, _compose, ordered_sections, vbuck_bytes, date_str)
