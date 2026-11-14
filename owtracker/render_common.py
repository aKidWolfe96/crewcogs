"""Shared drawing helpers for the Overwatch cog image cards.

Fonts are bundled in ./fonts (SIL Open Font License) so cards look the same on
every host, including slim Docker images with no system fonts.
"""

from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).with_name("fonts")
FONT_FILES = {
    "display": "LilitaOne-Regular.ttf",   # chunky Fortnite-style headings
    "bold": "Inter-ExtraBold.woff",
    "semi": "Inter-SemiBold.woff",
}
SYSTEM_FALLBACKS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
)

WHITE = (255, 255, 255)
MUTED = (196, 208, 232)


@lru_cache(maxsize=64)
def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    candidates = [FONT_DIR / FONT_FILES.get(kind, FONT_FILES["bold"])] + [Path(p) for p in SYSTEM_FALLBACKS]
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def hex_rgb(value, fallback=(52, 86, 140)):
    if not value or not isinstance(value, str):
        return fallback
    s = value.lstrip("#").strip()
    if len(s) >= 6:
        try:
            return tuple(int(s[i : i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            pass
    return fallback


def shade(rgb, factor):
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)


def gradient(size, top, bottom, horizontal=False):
    """Fast two-colour linear gradient."""
    w, h = size
    steps = w if horizontal else h
    strip = Image.new("RGB", (steps, 1) if horizontal else (1, steps))
    px = strip.load()
    for i in range(steps):
        t = i / max(steps - 1, 1)
        c = tuple(int(top[k] + (bottom[k] - top[k]) * t) for k in range(3))
        px[(i, 0) if horizontal else (0, i)] = c
    return strip.resize((w, h))


def rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius, fill=255)
    return mask


def round_corners(img, radius):
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), rounded_mask(img.size, radius))
    return out


def bottom_scrim(w, h, strength=225, power=1.4):
    """Transparent → dark fade used under text on top of artwork."""
    col = Image.new("L", (1, h))
    px = col.load()
    for y in range(h):
        px[0, y] = int(strength * (y / max(h - 1, 1)) ** power)
    alpha = col.resize((w, h))
    scrim = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    scrim.putalpha(alpha)
    return scrim


def cover_fit(img, w, h, anchor_top=False):
    img = img.convert("RGBA")
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = 0 if anchor_top else (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def contain_fit(img, w, h):
    img = img.convert("RGBA")
    img.thumbnail((w, h), Image.LANCZOS)
    return img


def open_image(data):
    if not data:
        return None
    try:
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None


def shrink_png(data, max_side=512):
    """Downscale downloaded artwork once so caches stay small and renders fast."""
    img = open_image(data)
    if img is None:
        return None
    img.thumbnail((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def shadow_text(draw, xy, text, fnt, fill=WHITE, shadow=(0, 0, 0), offset=2, anchor=None):
    x, y = xy
    draw.text((x + offset, y + offset), text, font=fnt, fill=shadow, anchor=anchor)
    draw.text((x, y), text, font=fnt, fill=fill, anchor=anchor)


def fit_text(draw, text, kind, size, max_w, min_size=12):
    """Largest font (down to min_size) that fits `text` in max_w."""
    while size > min_size and draw.textlength(text, font=font(kind, size)) > max_w:
        size -= 1
    return font(kind, size)


def ellipsize(draw, text, fnt, max_w):
    if draw.textlength(text, font=fnt) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=fnt) > max_w:
        text = text[:-1]
    return (text.rstrip() + "…") if text else "…"


def wrap(draw, text, fnt, max_w, max_lines=2):
    words, lines, cur = text.split(), [], ""
    for i, word in enumerate(words):
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=fnt) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        cur = word
        if len(lines) == max_lines - 1:
            rest = " ".join(words[i:])
            lines.append(ellipsize(draw, rest, fnt, max_w))
            return lines
    if cur:
        lines.append(ellipsize(draw, cur, fnt, max_w))
    return lines[:max_lines]


def pill(draw, xy, text, fnt, bg, fg=WHITE, pad_x=10, pad_y=5, radius=None, anchor_right=False):
    """Draw a rounded label. Returns its width."""
    x, y = xy
    tw = draw.textlength(text, font=fnt)
    th = fnt.getbbox("Ag")[3]
    w, h = int(tw + pad_x * 2), int(th + pad_y * 2)
    if anchor_right:
        x -= w
    draw.rounded_rectangle([x, y, x + w, y + h], radius if radius is not None else h // 2, fill=bg)
    draw.text((x + pad_x, y + pad_y - 1), text, font=fnt, fill=fg)
    return w


def to_png(img, max_bytes=9_000_000) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    data = out.getvalue()
    if len(data) > max_bytes:
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=88, optimize=True)
        data = out.getvalue()
    return data
