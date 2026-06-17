"""
Graphical item-shop renderer for the FortniteStats cog.

EasyFnStats-style board: shop-blue background, "SHOP" + date header, every
section packed into a balanced masonry (largest section first) so columns stay
even. Square tiles with full-bleed art, name, V-Bucks price (struck original
when discounted), and a corner badge for discounts / bonuses / bundles.

Reads ALL shop item types — outfits/emotes (br), jam tracks, instruments, cars,
lego kits and bundles — and skips unresolved "[VIRTUAL]/SID_Placeholder" entries
that Epic ships in the raw feed.

All network I/O is async; PIL compositing runs in an executor.

Public entry point:
    await render_shop_image(loop, shop) -> bytes  (PNG, or JPEG if very large)
"""

import asyncio
import io
import logging
import os

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("red.fortnitestats")

# ----------------------------- layout constants ----------------------------- #
TILE = 168                            # square tile (width == height)
TILE_GAP = 8
SECTION_COLS = 3
SECTION_GAP = 26
SECTION_HEADER_H = 40
MASONRY_COLS = 5
COL_GAP = 22
MARGIN = 40
HEADER_H = 150
CARD_RADIUS = 14
MAX_CARDS = 400
DOWNLOAD_CONCURRENCY = 12
MAX_PNG_BYTES = 9_000_000

BG_TOP = (32, 150, 236)
BG_BOTTOM = (20, 116, 200)
TEXT = (255, 255, 255)
SUBTEXT = (220, 235, 252)
STRUCK_COLOR = (150, 205, 245)
BADGE_BG = (255, 216, 60)
BADGE_TEXT = (40, 35, 10)
TRACK_RGB = (78, 52, 120)             # default tile color for jam tracks

FONT_PATHS = {
    "bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf", "C:\\Windows\\Fonts\\arialbd.ttf",
    ],
    "bolditalic": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
        "C:\\Windows\\Fonts\\arialbi.ttf",
    ],
    "regular": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf", "C:\\Windows\\Fonts\\arial.ttf",
    ],
}

RARITY_RGB = {
    "common": (130, 130, 138), "uncommon": (96, 185, 50), "rare": (44, 138, 222),
    "epic": (170, 60, 240), "legendary": (230, 132, 50), "mythic": (232, 196, 50),
    "icon_series": (38, 210, 200), "marvel": (200, 55, 56), "dc": (70, 90, 220),
    "starwars": (40, 44, 70), "gaming_legends": (110, 50, 168), "slurp": (40, 220, 210),
    "lava": (216, 92, 34), "frozen": (120, 205, 230), "shadow": (62, 62, 70),
    "dark": (150, 50, 188),
}
DEFAULT_RGB = (52, 86, 140)


# ------------------------------- font helpers ------------------------------- #
def _font(kind, size):
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
        base.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return base.resize((w, h))


def _rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius, fill=255)
    return mask


def _cover_fit(img, box_w, box_h, anchor_top=True):
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
    lines, cur, i = [], "", 0
    while i < len(words):
        trial = f"{cur} {words[i]}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
            i += 1
        else:
            if cur:
                lines.append(cur)
                cur = ""
            else:
                lines.append(words[i])
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


# ------------------------------- tile drawing ------------------------------- #
def _render_tile(card, fonts, vbuck_icon):
    f_name, f_price, f_badge = fonts["name"], fonts["price"], fonts["badge"]
    top, bottom = card["c_top"], card["c_bottom"]
    tile = _vertical_gradient((TILE, TILE), top, bottom).convert("RGBA")

    art_bytes = card.get("art_bytes")
    if art_bytes:
        try:
            art = Image.open(io.BytesIO(art_bytes))
            if card.get("full_bleed"):
                tile.alpha_composite(_cover_fit(art, TILE, TILE, anchor_top=True), (0, 0))
            else:
                fitted = _contain_fit(art, TILE - 24, int(TILE * 0.62))
                ax = (TILE - fitted.width) // 2
                ay = max(8, int(TILE * 0.62) - fitted.height + 6)
                tile.alpha_composite(fitted, (ax, ay))
        except Exception:  # noqa: BLE001
            pass

    scrim_h = int(TILE * 0.46)
    scrim = Image.new("RGBA", (TILE, scrim_h), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(scrim)
    for yy in range(scrim_h):
        sdraw.line([(0, yy), (TILE, yy)], fill=(0, 0, 0, int(215 * (yy / scrim_h) ** 1.25)))
    tile.alpha_composite(scrim, (0, TILE - scrim_h))

    tdraw = ImageDraw.Draw(tile)
    tdraw.rectangle([0, TILE - 4, TILE, TILE], fill=top + (255,))

    name_lines = _wrap(tdraw, card["name"], f_name, TILE - 18, max_lines=2)
    line_h = f_name.getbbox("Ag")[3] + 3
    price_y = TILE - 30
    ny = price_y - len(name_lines) * line_h - 4
    for line in name_lines:
        tdraw.text((10, ny + 1), line, font=f_name, fill=(0, 0, 0))
        tdraw.text((9, ny), line, font=f_name, fill=TEXT)
        ny += line_h

    px = 9
    if vbuck_icon is not None:
        tile.alpha_composite(vbuck_icon, (px, price_y))
        px += vbuck_icon.width + 4
    price_str = f"{card['price']:,}" if card["price"] is not None else "—"
    tdraw.text((px, price_y), price_str, font=f_price, fill=TEXT)
    px += tdraw.textlength(price_str, font=f_price) + 6
    reg = card.get("regular_price")
    if reg and card["price"] is not None and reg > card["price"]:
        reg_str = f"{reg:,}"
        tdraw.text((px, price_y + 2), reg_str, font=f_badge, fill=STRUCK_COLOR)
        w = tdraw.textlength(reg_str, font=f_badge)
        midy = price_y + 2 + f_badge.getbbox("0")[3] // 2 + 1
        tdraw.line([(px, midy), (px + w, midy)], fill=STRUCK_COLOR, width=1)

    badge = card.get("badge_text")
    if badge:
        pad = 5
        bw = tdraw.textlength(badge, font=f_badge) + pad * 2
        bh = f_badge.getbbox("Ag")[3] + pad
        tdraw.rounded_rectangle([7, 7, 7 + bw, 7 + bh], 5, fill=BADGE_BG)
        tdraw.text((7 + pad, 7 + pad // 2), badge, font=f_badge, fill=BADGE_TEXT)

    rounded = Image.new("RGBA", (TILE, TILE), (0, 0, 0, 0))
    rounded.paste(tile, (0, 0), _rounded_mask((TILE, TILE), CARD_RADIUS))
    return rounded


# ------------------------------- composition -------------------------------- #
def _block_height(n_items):
    rows = max(1, (n_items + SECTION_COLS - 1) // SECTION_COLS)
    return SECTION_HEADER_H + rows * TILE + (rows - 1) * TILE_GAP


def _compose(sections, vbuck_bytes, date_str) -> bytes:
    fonts = {
        "title": _font("bold", 70), "date": _font("bolditalic", 26),
        "section": _font("bolditalic", 24), "name": _font("bold", 16),
        "price": _font("bold", 16), "badge": _font("bold", 13),
    }
    block_w = SECTION_COLS * TILE + (SECTION_COLS - 1) * TILE_GAP
    canvas_w = MARGIN * 2 + MASONRY_COLS * block_w + (MASONRY_COLS - 1) * COL_GAP

    # largest section first → balanced columns (LPT bin-packing heuristic)
    ordered = sorted(sections, key=lambda s: _block_height(len(s[1])), reverse=True)
    col_heights = [0] * MASONRY_COLS
    placements = []
    for name, cards in ordered:
        c = min(range(MASONRY_COLS), key=lambda i: col_heights[i])
        placements.append((c, col_heights[c], name, cards))
        col_heights[c] += _block_height(len(cards)) + SECTION_GAP

    content_h = max(col_heights) if col_heights else 0
    canvas_h = HEADER_H + content_h + MARGIN

    canvas = Image.new("RGB", (canvas_w, canvas_h), BG_TOP)
    canvas.paste(_vertical_gradient((canvas_w, canvas_h), BG_TOP, BG_BOTTOM), (0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.text((MARGIN, 36), "SHOP", font=fonts["title"], fill=TEXT)
    draw.text((MARGIN + 4, 116), date_str, font=fonts["date"], fill=SUBTEXT)

    vbuck_icon = None
    if vbuck_bytes:
        try:
            vbuck_icon = Image.open(io.BytesIO(vbuck_bytes)).convert("RGBA")
            vbuck_icon.thumbnail((19, 19), Image.LANCZOS)
        except Exception:  # noqa: BLE001
            vbuck_icon = None

    for col, y0, name, cards in placements:
        bx = MARGIN + col * (block_w + COL_GAP)
        by = HEADER_H + y0
        draw.text((bx + 2, by + 4), name.upper(), font=fonts["section"], fill=TEXT)
        draw.line([(bx + 2, by + SECTION_HEADER_H - 8), (bx + block_w, by + SECTION_HEADER_H - 8)],
                  fill=(255, 255, 255), width=1)
        gy = by + SECTION_HEADER_H
        for idx, card in enumerate(cards):
            r, c = divmod(idx, SECTION_COLS)
            tx = bx + c * (TILE + TILE_GAP)
            ty = gy + r * (TILE + TILE_GAP)
            tile = _render_tile(card, fonts, vbuck_icon)
            canvas.paste(tile, (tx, ty), tile)

    draw.text((MARGIN, canvas_h - 26),
              "data via fortnite-api.com · not affiliated with Epic Games",
              font=_font("regular", 15), fill=(210, 230, 248))

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    data = out.getvalue()
    if len(data) > MAX_PNG_BYTES:
        out = io.BytesIO()
        canvas.convert("RGB").save(out, format="JPEG", quality=86, optimize=True)
        data = out.getvalue()
    return data


# ------------------------------- entry resolution --------------------------- #
def _is_placeholder(name):
    if not name:
        return True
    n = name.strip().lower()
    return (
        "placeholder" in n
        or "sid_" in n
        or n.startswith("[virtual]")
        or n.startswith("tbd")
        or n == "tbd"
        or n == "unknown"
    )


def _img_asset(images):
    if not images:
        return None
    return getattr(images, "featured", None) or getattr(images, "icon", None) \
        or getattr(images, "large", None) or getattr(images, "small", None)


def _resolve(entry):
    """Return (asset, full_bleed, name, rarity_item) or None to skip the entry."""
    nda = getattr(entry, "new_display_asset", None)
    nda_offer = None
    if nda and getattr(nda, "images", None):
        imgs = nda.images
        nda_offer = getattr(imgs, "offer_image", None)
        if not nda_offer and hasattr(imgs, "get"):
            nda_offer = imgs.get("OfferImage")

    bundle = getattr(entry, "bundle", None)
    br = getattr(entry, "br", None) or []
    item = br[0] if br else None

    # bundles: combined art + bundle name
    if bundle and getattr(bundle, "image", None):
        return bundle.image, True, getattr(bundle, "name", None), item

    # standard cosmetics (outfits, emotes, etc.)
    if item:
        if nda_offer:
            return nda_offer, True, item.name, item
        if item.images and item.images.featured:
            return item.images.featured, True, item.name, item
        if item.images and item.images.icon:
            return item.images.icon, False, item.name, item
        return None, False, item.name, item

    # jam tracks → square album art + title
    tracks = getattr(entry, "tracks", None) or []
    if tracks:
        t = tracks[0]
        return getattr(t, "album_art", None), True, getattr(t, "title", None), None

    # instruments / cars / lego — fall back to the shop render if present
    for attr in ("instruments", "cars", "lego_kits"):
        seq = getattr(entry, attr, None) or []
        if seq:
            obj = seq[0]
            asset = nda_offer or _img_asset(getattr(obj, "images", None))
            return asset, bool(nda_offer), getattr(obj, "name", None), obj

    # nothing resolvable
    if nda_offer:
        return nda_offer, True, getattr(entry, "dev_name", None), None
    return None


def _badge_for(entry):
    final = getattr(entry, "final_price", None)
    regular = getattr(entry, "regular_price", None)
    if final is not None and regular and regular > final:
        return f"{regular - final:,} V-Bucks Off"
    tag = getattr(entry, "offer_tag", None)
    if tag and getattr(tag, "text", None):
        return tag.text
    bundle = getattr(entry, "bundle", None)
    if bundle and getattr(bundle, "info", None):
        return bundle.info
    banner = getattr(entry, "banner", None)
    if banner and getattr(banner, "value", None):
        return banner.value
    return None


def _colors_for(entry, rarity_item, is_track):
    colors = getattr(entry, "colors", None)
    if colors and colors.color1:
        top = _hex_to_rgb(colors.color1)
        bottom = _hex_to_rgb(colors.color3 or colors.color2 or colors.color1, top)
        return top, tuple(max(0, int(c * 0.5)) for c in bottom)
    if is_track:
        return TRACK_RGB, tuple(int(c * 0.5) for c in TRACK_RGB)
    rar = rarity_item.rarity.value.lower() if (rarity_item and getattr(rarity_item, "rarity", None)) else None
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

        resolved = _resolve(entry)
        if not resolved:
            continue
        asset, full_bleed, name, rarity_item = resolved
        if _is_placeholder(name):
            continue  # drop "[VIRTUAL]/SID_Placeholder/TBD" junk

        if oid:
            seen.add(oid)
        is_track = bool(getattr(entry, "tracks", None)) and not (getattr(entry, "br", None))
        layout = getattr(entry, "layout", None)
        section = layout.name if layout and layout.name else "Featured"
        top, bottom = _colors_for(entry, rarity_item, is_track)
        if section not in sections:
            sections[section] = []
            order.append(section)
        sections[section].append({
            "asset": asset, "full_bleed": full_bleed, "name": name,
            "price": getattr(entry, "final_price", None),
            "regular_price": getattr(entry, "regular_price", None),
            "badge_text": _badge_for(entry), "c_top": top, "c_bottom": bottom,
        })
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

    date_str = shop.date.strftime("%A – %b %d, %Y") if shop.date else "Today"
    ordered_sections = [(s, sections[s]) for s in order if sections[s]]
    return await loop.run_in_executor(None, _compose, ordered_sections, vbuck_bytes, date_str)
