from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover
    Image = ImageDraw = ImageFont = None


def _font(size: int):
    if ImageFont is None:
        return None
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def line_chart(
    history: Mapping[str, Mapping[str, object]],
    names: Mapping[str, str],
    metric: str,
    out_path: Path,
    title: str,
) -> Path:
    if Image is None:
        raise RuntimeError("Pillow is not installed. Reinstall the cog requirements.")
    weeks = sorted((int(w) for w in history.keys()))
    if not weeks:
        raise RuntimeError("No archived weekly history yet.")
    series: Dict[str, Dict[int, float]] = {}
    for w in weeks:
        block = history.get(str(w), {}).get(metric, {}) or {}
        for rid, value in block.items():
            series.setdefault(str(rid), {})[w] = float(value)
    if not series:
        raise RuntimeError(f"No {metric} history is available yet.")

    width, height = 1200, 700
    img = Image.new("RGB", (width, height), (24, 27, 33))
    draw = ImageDraw.Draw(img)
    title_font = _font(34)
    font = _font(20)
    small = _font(16)
    draw.text((55, 28), title, fill=(245, 245, 245), font=title_font)

    left, top, right, bottom = 85, 100, width - 250, height - 80
    values = [v for s in series.values() for v in s.values()]
    lo, hi = min(values), max(values)
    if abs(hi - lo) < 1e-9:
        hi = lo + 1
    pad = (hi - lo) * 0.08
    lo -= pad
    hi += pad

    # axes + horizontal grid
    draw.line((left, top, left, bottom), fill=(160, 160, 160), width=2)
    draw.line((left, bottom, right, bottom), fill=(160, 160, 160), width=2)
    for i in range(6):
        y = top + (bottom - top) * i / 5
        val = hi - (hi - lo) * i / 5
        draw.line((left, y, right, y), fill=(55, 60, 68), width=1)
        draw.text((12, y - 10), f"{val:.1f}", fill=(190, 190, 190), font=small)
    for i, week in enumerate(weeks):
        x = left if len(weeks) == 1 else left + (right - left) * i / (len(weeks) - 1)
        draw.text((x - 8, bottom + 12), str(week), fill=(190, 190, 190), font=small)

    palette = [
        (88, 166, 255), (255, 109, 109), (111, 220, 140), (255, 196, 89),
        (192, 132, 252), (85, 214, 205), (245, 145, 66), (238, 105, 190),
        (160, 160, 160), (140, 200, 100), (100, 140, 240), (230, 180, 100),
    ]
    for idx, (rid, points) in enumerate(sorted(series.items(), key=lambda kv: names.get(kv[0], kv[0]))):
        color = palette[idx % len(palette)]
        coords = []
        for i, week in enumerate(weeks):
            if week not in points:
                continue
            x = left if len(weeks) == 1 else left + (right - left) * i / (len(weeks) - 1)
            y = bottom - (points[week] - lo) / (hi - lo) * (bottom - top)
            coords.append((x, y))
        if len(coords) > 1:
            draw.line(coords, fill=color, width=4)
        for x, y in coords:
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=color)
        ly = top + idx * 32
        draw.rectangle((right + 25, ly + 4, right + 45, ly + 24), fill=color)
        label = names.get(rid, f"Roster {rid}")[:23]
        draw.text((right + 55, ly), label, fill=(235, 235, 235), font=small)

    draw.text((left, height - 36), "Week", fill=(190, 190, 190), font=small)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG")
    return out_path
