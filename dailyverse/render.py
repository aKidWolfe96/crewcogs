"""Verse cards: soft, elegant images with big readable text.

Pure Pillow, no Discord. Every function returns JPEG bytes, so the cog runs them
in a thread pool and nothing blocks the bot.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger("red.dailyverse.render")

FONT_DIR = Path(__file__).parent / "fonts"
SS = 2                 # draw at 2x, then downscale for smooth lines
W, H = 1200, 800       # final card size
MAX_FONT, MIN_FONT, FLOOR_FONT = 76, 40, 30   # verse text sizes (px at final size)
BOX_W, BOX_H = 960, 430                       # verse text area


@dataclass(frozen=True)
class Style:
    key: str
    name: str
    top: tuple[int, int, int]
    bottom: tuple[int, int, int]
    accent: tuple[int, int, int]
    text: tuple[int, int, int]
    soft: tuple[int, int, int]
    vignette: float = 0.15
    grain: float = 0.04

    @property
    def color(self) -> int:
        r, g, b = self.accent
        return (r << 16) | (g << 8) | b


STYLES: dict[str, Style] = {s.key: s for s in (
    Style("parchment", "Parchment", (246, 236, 214), (233, 216, 184), (160, 118, 48), (58, 40, 24), (140, 112, 78), 0.18, 0.05),
    Style("dawn", "Dawn", (255, 236, 210), (244, 196, 170), (176, 110, 60), (70, 38, 30), (156, 104, 84), 0.0, 0.03),
    Style("midnight", "Midnight", (26, 30, 46), (12, 14, 24), (214, 178, 104), (244, 236, 220), (170, 150, 118), 0.35, 0.04),
    Style("sage", "Sage", (232, 238, 224), (206, 219, 196), (104, 128, 84), (38, 52, 34), (110, 128, 100), 0.12, 0.04),
    Style("dove", "Dove", (236, 240, 246), (210, 220, 234), (92, 116, 150), (34, 44, 62), (110, 124, 146), 0.10, 0.03),
    Style("rose", "Rose", (250, 234, 236), (238, 206, 212), (166, 92, 108), (72, 34, 44), (150, 106, 114), 0.08, 0.03),
    Style("twilight", "Twilight", (52, 40, 78), (24, 20, 44), (226, 186, 132), (246, 238, 230), (184, 160, 176), 0.30, 0.04),
)}
ROTATION = ("parchment", "dawn", "midnight", "sage", "dove", "rose", "twilight")


def style_for(day: dt.date, setting: str = "rotate") -> Style:
    """The configured style, or today's style in the daily rotation."""
    if setting in STYLES:
        return STYLES[setting]
    o = day.toordinal()
    return STYLES[ROTATION[(o + o // 7) % len(ROTATION)]]  # shifts each week, not tied to weekdays


# ── fonts ───────────────────────────────────────────────────────────────
_FONT_FILES = {
    "verse": ("EBGaramond-Variable.ttf", 500),
    "italic": ("EBGaramond-Italic-Variable.ttf", 500),
    "caps": ("Cinzel-Variable.ttf", 600),
    "caps_bold": ("Cinzel-Variable.ttf", 700),
}


@lru_cache(maxsize=64)
def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    file, weight = _FONT_FILES[kind]
    try:
        f = ImageFont.truetype(str(FONT_DIR / file), size * SS)
        try:
            f.set_variation_by_axes([weight])
        except Exception:
            pass
        return f
    except OSError:
        log.warning("font %s missing, using fallback", file)
        for fallback in ("DejaVuSerif.ttf", "LiberationSerif-Regular.ttf"):
            try:
                return ImageFont.truetype(fallback, size * SS)
            except OSError:
                continue
        return ImageFont.load_default(size * SS)


# ── background (cached per style; it's the slow part) ───────────────────
@lru_cache(maxsize=len(STYLES))
def _background(key: str) -> Image.Image:
    s = STYLES[key]
    w, h = W * SS, H * SS
    # vertical gradient, drawn small then stretched (fast and smooth)
    grad = Image.new("RGB", (1, 256))
    for y in range(256):
        t = y / 255
        grad.putpixel((0, y), tuple(int(s.top[i] + (s.bottom[i] - s.top[i]) * t) for i in range(3)))
    img = grad.resize((w, h), Image.BICUBIC)
    if s.vignette:
        small = (w // 8, h // 8)
        mask = Image.new("L", small, 0)
        ImageDraw.Draw(mask).ellipse([-small[0] * 0.15, -small[1] * 0.25, small[0] * 1.15, small[1] * 1.25], fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(15)).resize((w, h), Image.BICUBIC)
        img = Image.composite(img, Image.blend(img, Image.new("RGB", (w, h)), s.vignette), mask)
    if s.grain:
        noise = Image.effect_noise((w // 4, h // 4), 40).resize((w, h)).convert("RGB")
        img = Image.blend(img, noise, s.grain)
    _frame(ImageDraw.Draw(img), s)
    return img


def _frame(d: ImageDraw.ImageDraw, s: Style):
    m1, m2 = 34 * SS, 46 * SS
    d.rectangle([m1, m1, W * SS - m1, H * SS - m1], outline=s.accent, width=3 * SS)
    d.rectangle([m2, m2, W * SS - m2, H * SS - m2], outline=s.soft, width=1 * SS)
    cm, L, r = 60 * SS, 70 * SS, 7 * SS
    for sx, sy, x, y in ((1, 1, cm, cm), (-1, 1, W * SS - cm, cm),
                         (1, -1, cm, H * SS - cm), (-1, -1, W * SS - cm, H * SS - cm)):
        d.line([(x, y), (x + sx * L, y)], fill=s.accent, width=2 * SS)
        d.line([(x, y), (x, y + sy * L)], fill=s.accent, width=2 * SS)
        cx, cy = x + sx * 16 * SS, y + sy * 16 * SS
        d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=s.accent)


def _divider(d: ImageDraw.ImageDraw, cx: float, y: float, s: Style, span: int = 150):
    w = 2 * SS
    d.line([(cx - span * SS, y), (cx - 18 * SS, y)], fill=s.accent, width=w)
    d.line([(cx + 18 * SS, y), (cx + span * SS, y)], fill=s.accent, width=w)
    r = 8 * SS
    d.polygon([(cx, y - r), (cx + r, y), (cx, y + r), (cx - r, y)], outline=s.accent, width=w)
    r2 = 3 * SS
    for dx in (-span * SS, span * SS):
        d.ellipse([cx + dx - r2, y - r2, cx + dx + r2, y + r2], fill=s.accent)


# ── text layout ─────────────────────────────────────────────────────────
_MEASURE = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def _wrap(text: str, font, max_w: float) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        test = f"{cur} {word}".strip()
        if _MEASURE.textlength(test, font=font) <= max_w or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _layout(text: str, minimum: int = MIN_FONT):
    """Biggest font size (MAX_FONT down to minimum) where the text fits the box."""
    for size in range(MAX_FONT, minimum - 1, -2):
        f = _font("verse", size)
        lines = _wrap(text, f, BOX_W * SS)
        lh = int(size * SS * 1.32)
        if lh * len(lines) <= BOX_H * SS:
            return f, lines, lh
    return None


def fits(text: str) -> bool:
    return _layout(f"“{text}”") is not None


def split_for_cards(verses: list[str], max_cards: int = 10) -> list[str]:
    """Group verses into as few cards as possible, each at a readable size."""
    chunks, cur = [], []
    for v in verses:
        trial = " ".join(cur + [v])
        if cur and not fits(trial):
            chunks.append(" ".join(cur))
            cur = [v]
        else:
            cur.append(v)
    if cur:
        chunks.append(" ".join(cur))
    return chunks[:max_cards]


# ── the card ────────────────────────────────────────────────────────────
def render_card(text: str, reference: str, translation: str, notice: str,
                header: str, style: Style, page: Optional[tuple[int, int]] = None) -> bytes:
    img = _background(style.key).copy()
    d = ImageDraw.Draw(img)
    cx = W * SS / 2

    head = header.upper()
    if page and page[1] > 1:
        head += f"  ·  {page[0]} OF {page[1]}"
    d.text((cx, 108 * SS), head, font=_font("caps", 28), fill=style.accent, anchor="mm")

    layout = _layout(f"“{text}”") or _layout(f"“{text}”", FLOOR_FONT)
    if layout is None:  # absurdly long: shrink to the floor and cut
        f = _font("verse", FLOOR_FONT)
        lh = int(FLOOR_FONT * SS * 1.32)
        lines = _wrap(f"“{text}”", f, BOX_W * SS)[: (BOX_H * SS) // lh]
        lines[-1] = lines[-1].rstrip("”") + " …”"
    else:
        f, lines, lh = layout
    top = 150 * SS + (BOX_H * SS - lh * len(lines)) / 2
    for i, line in enumerate(lines):
        d.text((cx, top + i * lh + lh / 2), line, font=f, fill=style.text, anchor="mm")

    y = 625 * SS
    _divider(d, cx, y, style)
    ref_font = _font("caps_bold", 36)
    ref = reference.upper()
    while _MEASURE.textlength(ref, font=ref_font) > 1000 * SS and ref_font.size > 24 * SS:
        ref_font = _font("caps_bold", ref_font.size // SS - 2)
    d.text((cx, y + 52 * SS), ref, font=ref_font, fill=style.accent, anchor="mm")

    foot = f"{translation}  ·  {notice}"
    foot_font = _font("italic", 30)
    while _MEASURE.textlength(foot, font=foot_font) > 1040 * SS and foot_font.size > 20 * SS:
        foot_font = _font("italic", foot_font.size // SS - 2)
    d.text((cx, H * SS - 78 * SS), foot, font=foot_font, fill=style.soft, anchor="mm")

    out = io.BytesIO()
    # High-quality JPEG with full colour detail: crisp text, ~10x faster and smaller than PNG here.
    img.resize((W, H), Image.LANCZOS).save(out, format="JPEG", quality=92, subsampling=0, optimize=True)
    return out.getvalue()


def render_passage(verses: list[str], reference: str, translation: str, notice: str,
                   header: str, style: Style) -> list[bytes]:
    chunks = split_for_cards(verses)
    return [render_card(c, reference, translation, notice, header, style, (i, len(chunks)))
            for i, c in enumerate(chunks, 1)]
