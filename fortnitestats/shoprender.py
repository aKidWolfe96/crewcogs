"""
Item-shop model + renderers for the FortniteStats cog.

Two kinds of image, both sized to read well on a phone:

* **Cover**: one card per shop section (art collage, name, item count) plus
  quick facts: total items, new today, on sale, leaving soon.
* **Page**: one section (or a 16-item slice of a big one) as large tiles with
  art, name, type, V-Bucks price, struck-through original price, and
  NEW / sale / LAST DAY badges.

`build_model(shop)` turns the API response into plain data once; the browser
then renders pages on demand. Downloads are async; PIL runs in an executor.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field

from PIL import Image, ImageDraw

from . import render_common as rc

log = logging.getLogger("red.fortnitestats")

PAGE_SIZE = 16           # tiles per page (4 x 4)
PAGE_COLS = 4
TILE = 256
GAP = 14
MARGIN = 36
HEADER_H = 172
COVER_COLS = 4
COVER_CARD = (300, 196)
DOWNLOAD_CONCURRENCY = 10

BG_TOP = (24, 34, 78)
BG_BOTTOM = (12, 16, 38)
ACCENT = (255, 214, 64)
NEW_BG = (64, 200, 96)
SALE_BG = (255, 214, 64)
LAST_BG = (232, 64, 72)
TRACK_RGB = (88, 56, 140)

RARITY_RGB = {
    "common": (128, 132, 140), "uncommon": (96, 185, 50), "rare": (44, 138, 222),
    "epic": (170, 60, 240), "legendary": (230, 132, 50), "mythic": (232, 196, 50),
    "icon": (38, 210, 200), "marvel": (200, 55, 56), "dc": (70, 90, 220),
    "starwars": (40, 44, 70), "gaminglegends": (110, 50, 168), "slurp": (40, 220, 210),
    "lava": (216, 92, 34), "frozen": (120, 205, 230), "shadow": (62, 62, 70),
    "dark": (150, 50, 188), "lamborghini": (220, 180, 40),
}
DEFAULT_RGB = (52, 86, 140)
JAM_SECTION = "Jam Tracks"
OTHER_SECTION = "More Items"


# ─────────────────────────────── data model ──────────────────────────────── #
@dataclass
class ShopCard:
    name: str
    kind: str
    price: int | None
    regular: int | None
    section: str
    ids: list[str]
    art: object | None            # fortnite_api.Asset
    full_bleed: bool
    top: tuple
    bottom: tuple
    badge: str | None = None
    new: bool = False
    last_day: bool = False
    out_date: dt.datetime | None = None

    @property
    def on_sale(self) -> bool:
        return bool(self.regular and self.price is not None and self.regular > self.price)

    @property
    def art_url(self) -> str | None:
        return getattr(self.art, "url", None) if self.art is not None else None


@dataclass
class ShopModel:
    hash: str
    date: dt.datetime | None
    sections: list[tuple[str, list[ShopCard]]]
    vbuck: object | None = None
    pages: list[tuple[str, int, int, list[ShopCard]]] = field(default_factory=list)

    @property
    def cards(self) -> list[ShopCard]:
        return [c for _, cards in self.sections for c in cards]

    @property
    def date_label(self) -> str:
        return self.date.strftime("%A, %B %-d") if self.date else "Today"

    def stats(self) -> dict:
        cards = self.cards
        return {
            "items": len(cards),
            "new": sum(c.new for c in cards),
            "sale": sum(c.on_sale for c in cards),
            "leaving": sum(c.last_day for c in cards),
        }

    def find(self, query: str) -> list[ShopCard]:
        q = query.lower().strip()
        exact = [c for c in self.cards if c.name.lower() == q]
        return exact or [c for c in self.cards if q in c.name.lower()]


def _rarity_key(item) -> str | None:
    info = getattr(item, "rarity", None)
    value = getattr(info, "value", None)
    value = getattr(value, "value", value)
    return str(value).lower() if value else None


def _display(obj, attr="type") -> str | None:
    info = getattr(obj, attr, None)
    return getattr(info, "display_value", None) if info else None


def _is_placeholder(name: str | None) -> bool:
    if not name:
        return True
    n = name.strip().lower()
    return "placeholder" in n or n.startswith("[virtual]") or n.startswith("sid_") or n in {"tbd", "unknown"}


def _entry_art(entry):
    """Best shop artwork for an entry: Epic's own shop render when present."""
    nda = getattr(entry, "new_display_asset", None)
    if nda:
        for render in getattr(nda, "render_images", None) or []:
            if getattr(render, "image", None):
                return render.image
        for mat in getattr(nda, "material_instances", None) or []:
            img = getattr(getattr(mat, "images", None), "offer_image", None)
            if img:
                return img
    return None


def _card_for(entry, now: dt.datetime, shop_date: dt.datetime | None) -> ShopCard | None:
    br = list(getattr(entry, "br", None) or [])
    tracks = list(getattr(entry, "tracks", None) or [])
    others = []
    for attr in ("instruments", "cars", "lego_kits"):
        others.extend(getattr(entry, attr, None) or [])
    bundle = getattr(entry, "bundle", None)
    layout = getattr(entry, "layout", None)
    section = (layout.name or "").strip() if layout and layout.name else ""

    art = _entry_art(entry)
    full_bleed = art is not None
    rarity_item = br[0] if br else None

    if bundle:
        name = bundle.name
        count = len(br) + len(tracks) + len(others)
        kind = f"Bundle · {count} items" if count > 1 else "Bundle"
        art = art or bundle.image
        full_bleed = True
    elif br:
        item = br[0]
        name = item.name
        kind = _display(item) or "Item"
        if art is None and item.images:
            art = item.images.featured or item.images.icon
            full_bleed = bool(item.images.featured)
    elif tracks:
        t = tracks[0]
        name = t.title
        kind = f"Jam Track · {t.artist}" if getattr(t, "artist", None) else "Jam Track"
        art = art or t.album_art
        full_bleed = True
        section = section or JAM_SECTION
    elif others:
        obj = others[0]
        name = getattr(obj, "name", None)
        kind = _display(obj) or ("Car" if getattr(entry, "cars", None) else "Item")
        if art is None:
            imgs = getattr(obj, "images", None)
            art = getattr(imgs, "large", None) or getattr(imgs, "small", None) if imgs else None
    else:
        return None

    if _is_placeholder(name):
        return None

    # Colours: the shop's own tile colours, else the item's rarity.
    colors = getattr(entry, "colors", None)
    if colors and colors.color1:
        top = rc.hex_rgb(colors.color3 or colors.color1)
        bottom = rc.shade(rc.hex_rgb(colors.color1), 0.6)
    elif tracks and not br:
        top, bottom = TRACK_RGB, rc.shade(TRACK_RGB, 0.45)
    else:
        base = RARITY_RGB.get(_rarity_key(rarity_item), DEFAULT_RGB)
        top, bottom = base, rc.shade(base, 0.4)

    ids = [getattr(o, "id", None) for o in (*br, *tracks, *others)]
    in_date = getattr(entry, "in_date", None)
    out_date = getattr(entry, "out_date", None)
    banner = getattr(getattr(entry, "banner", None), "value", None)
    new = bool(
        (banner and banner.strip().lower() == "new")
        or (in_date and shop_date and in_date >= shop_date)
    )
    last_day = bool(out_date and out_date - now <= dt.timedelta(hours=24))
    badge = banner if banner and banner.strip().lower() != "new" else None

    return ShopCard(
        name=name.strip(), kind=kind, price=getattr(entry, "final_price", None),
        regular=getattr(entry, "regular_price", None), section=section or OTHER_SECTION,
        ids=[i for i in ids if i], art=art, full_bleed=full_bleed, top=top, bottom=bottom,
        badge=badge, new=new, last_day=last_day, out_date=out_date,
    )


def build_model(shop) -> ShopModel:
    """Group the raw shop feed into ordered sections of clean cards."""
    now = dt.datetime.now(dt.timezone.utc)
    shop_date = getattr(shop, "date", None)

    def order_key(entry):
        layout = getattr(entry, "layout", None)
        idx = getattr(layout, "index", None) if layout else None
        rank = getattr(layout, "rank", None) if layout else None
        return (
            idx if idx is not None else 10_000,
            -(getattr(entry, "sort_priority", 0) or 0),
            rank if rank is not None else 10_000,
        )

    sections: dict[str, list[ShopCard]] = {}
    seen_offers, seen_names = set(), set()
    for entry in sorted(shop.entries, key=order_key):
        oid = getattr(entry, "offer_id", None)
        if oid and oid in seen_offers:
            continue
        try:
            card = _card_for(entry, now, shop_date)
        except Exception:  # noqa: BLE001 - one odd entry must not break the shop
            log.debug("skipping shop entry %s", oid, exc_info=True)
            continue
        if card is None:
            continue
        dedupe = (card.section, card.name.lower(), card.price)
        if dedupe in seen_names:
            continue
        seen_offers.add(oid)
        seen_names.add(dedupe)
        sections.setdefault(card.section, []).append(card)

    # Big themed sections first is how the game shows it; keep that order but
    # push the long Jam Tracks list and the catch-all bucket to the end.
    ordered = [(n, c) for n, c in sections.items() if n not in (JAM_SECTION, OTHER_SECTION)]
    for tail in (OTHER_SECTION, JAM_SECTION):
        if tail in sections:
            ordered.append((tail, sections[tail]))

    model = ShopModel(hash=getattr(shop, "hash", "") or "", date=shop_date, sections=ordered,
                      vbuck=getattr(shop, "vbuck_icon", None))
    for name, cards in ordered:
        total = (len(cards) + PAGE_SIZE - 1) // PAGE_SIZE
        for i in range(total):
            model.pages.append((name, i + 1, total, cards[i * PAGE_SIZE:(i + 1) * PAGE_SIZE]))
    return model


# ─────────────────────────────── downloads ───────────────────────────────── #
class ArtCache:
    """Downloads + downscales artwork once per shop rotation."""

    def __init__(self):
        self.data: dict[str, bytes | None] = {}
        self._sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

    async def _get(self, asset, loop, max_side):
        url = getattr(asset, "url", None)
        if not url:
            return
        if url in self.data:
            return
        async with self._sem:
            if url in self.data:
                return
            try:
                raw = asset.read()
                if asyncio.iscoroutine(raw):
                    raw = await raw
                self.data[url] = await loop.run_in_executor(None, rc.shrink_png, raw, max_side)
            except Exception:  # noqa: BLE001
                self.data[url] = None

    async def fetch(self, assets, loop, max_side=512):
        await asyncio.gather(*(self._get(a, loop, max_side) for a in assets if a is not None))

    def get(self, asset) -> bytes | None:
        url = getattr(asset, "url", None)
        return self.data.get(url) if url else None


# ─────────────────────────────── drawing ─────────────────────────────────── #
def _background(w, h):
    bg = rc.gradient((w, h), BG_TOP, BG_BOTTOM).convert("RGBA")
    # soft diagonal light streak for depth
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for i in range(0, w + h, 6):
        alpha = max(0, 16 - abs(i - (w + h) // 3) // 40)
        if alpha:
            gd.line([(i, 0), (i - h, h)], fill=(120, 160, 255, alpha), width=6)
    bg.alpha_composite(glow)
    return bg


def _header(canvas, title, subtitle, chips, vbuck_img=None):
    d = ImageDraw.Draw(canvas)
    w = canvas.width
    title_font = rc.fit_text(d, title.upper(), "display", 72, w - MARGIN * 2 - 40, 34)
    rc.shadow_text(d, (MARGIN, 26), title.upper(), title_font, offset=3)
    d.text((MARGIN + 2, 106), subtitle, font=rc.font("semi", 24), fill=rc.MUTED)
    x = w - MARGIN
    for text, bg, fg in reversed(chips):
        x -= rc.pill(d, (x, 108), text, rc.font("bold", 18), bg, fg, anchor_right=True) + 10


def _price_row(tile, d, card, x, y, vbuck_img, size=22):
    if vbuck_img is not None:
        icon = vbuck_img.copy()
        icon.thumbnail((size + 4, size + 4), Image.LANCZOS)
        tile.alpha_composite(icon, (x, y - 1))
        x += icon.width + 5
    price = f"{card.price:,}" if card.price is not None else "—"
    d.text((x, y), price, font=rc.font("bold", size), fill=rc.WHITE)
    x += int(d.textlength(price, font=rc.font("bold", size))) + 8
    if card.on_sale:
        reg = f"{card.regular:,}"
        f = rc.font("semi", size - 6)
        ry = y + 5
        d.text((x, ry), reg, font=f, fill=(200, 210, 230))
        rw = d.textlength(reg, font=f)
        mid = ry + f.getbbox("0")[3] // 2 + 1
        d.line([(x, mid), (x + rw, mid)], fill=(240, 90, 90), width=2)


def _tile(card: ShopCard, art_bytes, vbuck_img):
    tile = rc.gradient((TILE, TILE), card.top, card.bottom).convert("RGBA")
    # subtle radial spotlight behind non full-bleed art
    art = rc.open_image(art_bytes)
    if art is not None:
        if card.full_bleed:
            tile.alpha_composite(rc.cover_fit(art, TILE, TILE, anchor_top=True))
        else:
            fitted = rc.contain_fit(art, TILE - 30, int(TILE * 0.68))
            tile.alpha_composite(fitted, ((TILE - fitted.width) // 2, max(10, int(TILE * 0.66) - fitted.height)))

    tile.alpha_composite(rc.bottom_scrim(TILE, int(TILE * 0.55)), (0, TILE - int(TILE * 0.55)))
    d = ImageDraw.Draw(tile)

    # name (up to 2 lines) + type, then price
    name_font = rc.font("bold", 21)
    lines = rc.wrap(d, card.name, name_font, TILE - 24, max_lines=2)
    line_h = 25
    price_y = TILE - 36
    kind_y = price_y - 24
    y = kind_y - len(lines) * line_h - 2
    for line in lines:
        rc.shadow_text(d, (12, y), line, name_font, offset=2)
        y += line_h
    kind = rc.ellipsize(d, card.kind.upper(), rc.font("semi", 13), TILE - 24)
    d.text((12, kind_y + 2), kind, font=rc.font("semi", 13), fill=(214, 222, 240))
    _price_row(tile, d, card, 12, price_y, vbuck_img)

    # badges
    bf = rc.font("bold", 15)
    bx = 10
    if card.new:
        bx += rc.pill(d, (bx, 10), "NEW", bf, NEW_BG, rc.WHITE, pad_x=9, pad_y=4) + 6
    if card.on_sale:
        bx += rc.pill(d, (bx, 10), f"-{card.regular - card.price:,}", bf, SALE_BG, (40, 32, 0), pad_x=9, pad_y=4) + 6
    elif card.badge and len(card.badge) <= 16:
        bx += rc.pill(d, (bx, 10), card.badge.upper(), bf, (255, 255, 255), (30, 34, 60), pad_x=9, pad_y=4) + 6
    if card.last_day:
        rc.pill(d, (TILE - 10, 10), "LAST DAY", bf, LAST_BG, rc.WHITE, pad_x=9, pad_y=4, anchor_right=True)

    # rarity / shop colour stripe
    d.rectangle([0, TILE - 6, TILE, TILE], fill=rc.shade(card.top, 1.25) + (255,))
    return rc.round_corners(tile, 18)


def render_page(model: ShopModel, page_index: int, art: ArtCache) -> bytes:
    section, num, total, cards = model.pages[page_index]
    vbuck_img = rc.open_image(art.get(model.vbuck))
    cols = PAGE_COLS
    rows = max(1, (len(cards) + cols - 1) // cols)
    w = MARGIN * 2 + cols * TILE + (cols - 1) * GAP
    h = HEADER_H + rows * TILE + (rows - 1) * GAP + MARGIN + 30
    canvas = _background(w, h)

    sale = sum(c.on_sale for c in cards)
    new = sum(c.new for c in cards)
    chips = []
    if new:
        chips.append((f"{new} NEW", NEW_BG, rc.WHITE))
    if sale:
        chips.append((f"{sale} ON SALE", SALE_BG, (40, 32, 0)))
    if total > 1:
        chips.append((f"PAGE {num}/{total}", (255, 255, 255), (30, 34, 60)))
    _header(canvas, section, f"Item Shop · {model.date_label}", chips, vbuck_img)

    for i, card in enumerate(cards):
        r, c = divmod(i, cols)
        x = MARGIN + c * (TILE + GAP)
        y = HEADER_H + r * (TILE + GAP)
        canvas.alpha_composite(_tile(card, art.get(card.art), vbuck_img), (x, y))

    d = ImageDraw.Draw(canvas)
    d.text((MARGIN, h - 34), "Prices in V-Bucks · data via fortnite-api.com · not affiliated with Epic Games",
           font=rc.font("semi", 14), fill=(150, 162, 196))
    return rc.to_png(canvas)


def _cover_card(name, cards, art: ArtCache):
    cw, ch = COVER_CARD
    first = cards[0]
    card = rc.gradient((cw, ch), first.top, first.bottom).convert("RGBA")
    # collage of up to three items
    picks = [c for c in cards if art.get(c.art)][:3] or cards[:1]
    slice_w = cw // len(picks)
    for i, c in enumerate(picks):
        img = rc.open_image(art.get(c.art))
        if img is None:
            continue
        x0 = i * slice_w
        width = cw - x0 if i == len(picks) - 1 else slice_w
        part = rc.cover_fit(img, width, ch, anchor_top=True)
        card.alpha_composite(part, (x0, 0))
    card.alpha_composite(rc.bottom_scrim(cw, int(ch * 0.7), strength=235), (0, ch - int(ch * 0.7)))
    d = ImageDraw.Draw(card)
    title_font = rc.fit_text(d, name.upper(), "display", 30, cw - 28, 18)
    rc.shadow_text(d, (14, ch - 70), name.upper(), title_font)
    count = f"{len(cards)} item{'s' if len(cards) != 1 else ''}"
    sale = sum(c.on_sale for c in cards)
    new = sum(c.new for c in cards)
    extra = " · ".join(x for x in (f"{new} new" if new else "", f"{sale} on sale" if sale else "") if x)
    d.text((15, ch - 32), " · ".join(x for x in (count, extra) if x), font=rc.font("semi", 16), fill=(220, 228, 245))
    if any(c.last_day for c in cards):
        rc.pill(d, (cw - 10, 10), "LAST DAY", rc.font("bold", 13), LAST_BG, rc.WHITE, pad_x=8, pad_y=3, anchor_right=True)
    return rc.round_corners(card, 16)


def render_cover(model: ShopModel, art: ArtCache) -> bytes:
    cw, ch = COVER_CARD
    sections = model.sections
    rows = max(1, (len(sections) + COVER_COLS - 1) // COVER_COLS)
    w = MARGIN * 2 + COVER_COLS * cw + (COVER_COLS - 1) * GAP
    h = HEADER_H + rows * ch + (rows - 1) * GAP + MARGIN + 30
    canvas = _background(w, h)
    s = model.stats()
    chips = [(f"{s['items']} ITEMS", (255, 255, 255), (30, 34, 60))]
    if s["new"]:
        chips.append((f"{s['new']} NEW", NEW_BG, rc.WHITE))
    if s["sale"]:
        chips.append((f"{s['sale']} ON SALE", SALE_BG, (40, 32, 0)))
    if s["leaving"]:
        chips.append((f"{s['leaving']} LEAVING", LAST_BG, rc.WHITE))
    _header(canvas, "Item Shop", model.date_label, chips)

    for i, (name, cards) in enumerate(sections):
        r, c = divmod(i, COVER_COLS)
        x = MARGIN + c * (cw + GAP)
        y = HEADER_H + r * (ch + GAP)
        canvas.alpha_composite(_cover_card(name, cards, art), (x, y))

    d = ImageDraw.Draw(canvas)
    d.text((MARGIN, h - 34), "Tap “Browse the shop” to see every item · data via fortnite-api.com",
           font=rc.font("semi", 14), fill=(150, 162, 196))
    return rc.to_png(canvas)


# ─────────────────────────────── entry points ─────────────────────────────── #
async def cover_png(loop, model: ShopModel, art: ArtCache) -> bytes:
    assets = [c.art for _, cards in model.sections for c in cards[:3]]
    await art.fetch(assets, loop)
    return await loop.run_in_executor(None, render_cover, model, art)


async def page_png(loop, model: ShopModel, page_index: int, art: ArtCache) -> bytes:
    _, _, _, cards = model.pages[page_index]
    await art.fetch([c.art for c in cards] + [model.vbuck], loop)
    return await loop.run_in_executor(None, render_page, model, page_index, art)
