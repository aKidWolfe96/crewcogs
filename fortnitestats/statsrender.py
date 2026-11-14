"""
Stat cards for the FortniteStats cog.

`summarize(stats, input_key)` flattens a fortnite_api BrPlayerStats into plain
numbers; `render_card` and `render_compare` draw them with PIL (run these in an
executor). Nothing here touches the network.
"""

from __future__ import annotations

import datetime as dt

from PIL import ImageDraw

from . import render_common as rc

W = 1200
PAD = 40
BG_TOP = (58, 30, 110)
BG_BOTTOM = (14, 18, 44)
PANEL = (255, 255, 255, 18)
PANEL_EDGE = (255, 255, 255, 34)
GOLD = (255, 214, 64)
GREEN = (72, 214, 120)
RED = (238, 84, 92)

INPUTS = {
    "all": "All inputs",
    "keyboard_mouse": "Keyboard & Mouse",
    "gamepad": "Controller",
    "touch": "Touch",
}
INPUT_COLORS = {"keyboard_mouse": (88, 160, 255), "gamepad": (72, 214, 120), "touch": (255, 170, 64)}
MODES = (("solo", "SOLO"), ("duo", "DUOS"), ("squad", "SQUADS"))
HERO_ACCENTS = ((255, 214, 64), (72, 214, 120), (238, 84, 92), (120, 170, 255))


# ─────────────────────────────── data ─────────────────────────────── #
def _mode(m) -> dict | None:
    if m is None or not getattr(m, "matches", 0):
        return None
    return {
        "wins": m.wins, "win_rate": m.win_rate, "kd": m.kd, "kills": m.kills,
        "matches": m.matches, "deaths": m.deaths,
        "top10": m.top10, "top25": m.top25,
        "kpm": m.kills_per_match, "minutes": m.minutes_played,
        "outlived": m.players_outlived,
        "last": getattr(m, "last_modified", None),
    }


def summarize(stats, input_key: str = "all") -> dict | None:
    """Plain-dict view of one input type's stats, or None if it has no matches."""
    inputs = getattr(stats, "inputs", None)
    block = getattr(inputs, input_key, None) if inputs else None
    overall = _mode(getattr(block, "overall", None)) if block else None
    if overall is None:
        return None
    split = {}
    if inputs:
        for key in INPUT_COLORS:
            b = getattr(inputs, key, None)
            o = getattr(b, "overall", None) if b else None
            if o is not None and o.matches:
                split[key] = o.matches
    bp = getattr(stats, "battle_pass", None)
    return {
        "name": stats.user.name,
        "account_id": stats.user.id,
        "input": input_key,
        "overall": overall,
        "modes": {k: _mode(getattr(block, k, None)) for k, _ in MODES},
        "split": split,
        "bp_level": getattr(bp, "level", None),
        "bp_progress": getattr(bp, "progress", None),
    }


def fmt_num(n) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 100_000:
        return f"{n / 1_000:.0f}K"
    return f"{n:,}"


def fmt_hours(minutes) -> str:
    if not minutes:
        return "—"
    hours = minutes / 60
    return f"{hours:,.0f}h" if hours >= 10 else f"{hours:.1f}h"


# ─────────────────────────────── drawing ─────────────────────────────── #
def _background(h):
    bg = rc.gradient((W, h), BG_TOP, BG_BOTTOM)  # RGB so translucent shapes blend
    d = ImageDraw.Draw(bg, "RGBA")
    # angled light bands, Fortnite-lobby style
    for i, alpha in ((0, 22), (90, 14), (170, 10)):
        x = W - 360 + i
        d.polygon([(x, 0), (x + 70, 0), (x - 260, h), (x - 330, h)], fill=(255, 255, 255, alpha))
    return bg


def _panel(d, box, radius=18, fill=PANEL, edge=PANEL_EDGE):
    d.rounded_rectangle(box, radius, fill=fill, outline=edge, width=2)


def _stat_tile(d, box, label, value, accent=None, big=True):
    x0, y0, x1, y1 = box
    _panel(d, box)
    if accent:
        d.rounded_rectangle([x0 + 18, y0 + 16, x0 + 60, y0 + 21], 3, fill=accent)
    d.text((x0 + 18, y0 + (30 if accent else 18)), label.upper(), font=rc.font("semi", 17 if big else 15), fill=rc.MUTED)
    vf = rc.fit_text(d, value, "display", 64 if big else 40, x1 - x0 - 36, 24)
    d.text((x0 + 18, y1 - (20 if big else 14)), value, font=vf, fill=rc.WHITE, anchor="ls")


def _header(canvas, d, title, chips, bp_level=None, bp_progress=None):
    tf = rc.fit_text(d, title, "display", 68, W - PAD * 2 - 240, 34)
    rc.shadow_text(d, (PAD, 30), title, tf, offset=3)
    x = PAD
    for text, bg, fg in chips:
        x += rc.pill(d, (x, 116), text, rc.font("bold", 16), bg, fg) + 10
    if bp_level is not None:
        bx1, by0 = W - PAD, 34
        bx0 = bx1 - 200
        _panel(d, [bx0, by0, bx1, by0 + 104], radius=16)
        d.text((bx0 + 18, by0 + 14), "BATTLE PASS", font=rc.font("semi", 14), fill=rc.MUTED)
        d.text((bx0 + 18, by0 + 66), f"LVL {bp_level}", font=rc.font("display", 40), fill=GOLD, anchor="ls")
        prog = max(0, min(100, bp_progress or 0))
        d.rounded_rectangle([bx0 + 18, by0 + 80, bx1 - 18, by0 + 90], 5, fill=(255, 255, 255, 40))
        if prog:
            d.rounded_rectangle([bx0 + 18, by0 + 80, bx0 + 18 + (bx1 - bx0 - 36) * prog / 100, by0 + 90], 5, fill=GOLD)


def render_card(data: dict, window_label: str, platform_label: str) -> bytes:
    o = data["overall"]
    has_split = data["input"] == "all" and len(data["split"]) > 1
    h = 900 + (90 if has_split else 0)
    canvas = _background(h)
    d = ImageDraw.Draw(canvas, "RGBA")

    chips = [
        (window_label.upper(), GOLD, (40, 32, 0)),
        (INPUTS.get(data["input"], "All inputs").upper(), (255, 255, 255), (30, 30, 60)),
    ]
    if platform_label:
        chips.append((platform_label.upper(), (255, 255, 255, 40), rc.WHITE))
    _header(canvas, d, data["name"], chips, data["bp_level"], data["bp_progress"])

    # hero row
    y = 174
    gap = 16
    tw = (W - PAD * 2 - gap * 3) // 4
    hero = (
        ("Wins", fmt_num(o["wins"])),
        ("Win rate", f"{o['win_rate']:.1f}%"),
        ("K/D", f"{o['kd']:.2f}"),
        ("Kills", fmt_num(o["kills"])),
    )
    for i, (label, value) in enumerate(hero):
        x = PAD + i * (tw + gap)
        _stat_tile(d, [x, y, x + tw, y + 160], label, value, HERO_ACCENTS[i])

    # secondary row
    y += 176
    small = (
        ("Matches", fmt_num(o["matches"])),
        ("Top 10", fmt_num(o["top10"])),
        ("Kills / match", f"{o['kpm']:.2f}"),
        ("Time played", fmt_hours(o["minutes"])),
    )
    for i, (label, value) in enumerate(small):
        x = PAD + i * (tw + gap)
        _stat_tile(d, [x, y, x + tw, y + 110], label, value, big=False)

    # mode cards
    y += 134
    d.text((PAD, y), "BY MODE", font=rc.font("display", 30), fill=rc.WHITE)
    y += 46
    mw = (W - PAD * 2 - gap * 2) // 3
    rows = (("Wins", "wins", fmt_num), ("Win rate", "win_rate", lambda v: f"{v:.1f}%"),
            ("K/D", "kd", lambda v: f"{v:.2f}"), ("Kills", "kills", fmt_num), ("Matches", "matches", fmt_num))
    for i, (key, title) in enumerate(MODES):
        x = PAD + i * (mw + gap)
        box = [x, y, x + mw, y + 300]
        _panel(d, box)
        d.text((x + 22, y + 18), title, font=rc.font("display", 34), fill=GOLD)
        m = data["modes"].get(key)
        if m is None:
            d.text((x + 22, y + 140), "Not played", font=rc.font("semi", 22), fill=rc.MUTED)
            continue
        ry = y + 74
        for label, field, fmt in rows:
            d.text((x + 22, ry), label, font=rc.font("semi", 20), fill=rc.MUTED)
            d.text((x + mw - 22, ry), fmt(m[field]), font=rc.font("bold", 22), fill=rc.WHITE, anchor="ra")
            ry += 44
            if label != "Matches":
                d.line([(x + 22, ry - 10), (x + mw - 22, ry - 10)], fill=(255, 255, 255, 22), width=1)

    y += 324
    # input split bar
    if has_split:
        total = sum(data["split"].values())
        d.text((PAD, y), "MATCHES BY INPUT", font=rc.font("semi", 16), fill=rc.MUTED)
        bx0, bx1, by = PAD, W - PAD, y + 28
        d.rounded_rectangle([bx0, by, bx1, by + 22], 11, fill=(255, 255, 255, 30))
        x = bx0
        legend_x = PAD
        for key, matches in sorted(data["split"].items(), key=lambda kv: -kv[1]):
            share = matches / total
            w = (bx1 - bx0) * share
            color = INPUT_COLORS[key]
            if w >= 2:
                d.rounded_rectangle([x, by, x + w, by + 22], 11, fill=color)
            x += w
            label = f"{INPUTS[key]} {share * 100:.0f}%"
            d.ellipse([legend_x, by + 38, legend_x + 12, by + 50], fill=color)
            d.text((legend_x + 18, by + 34), label, font=rc.font("semi", 16), fill=rc.WHITE)
            legend_x += int(d.textlength(label, font=rc.font("semi", 16))) + 44
        y += 90

    last = o.get("last")
    foot = "data via fortnite-api.com · not affiliated with Epic Games"
    if isinstance(last, dt.datetime):
        foot = f"Last updated {last.strftime('%b %-d, %Y')} · " + foot
    d.text((PAD, h - 40), foot, font=rc.font("semi", 15), fill=(170, 170, 210))
    return rc.to_png(canvas)


COMPARE_ROWS = (
    ("Wins", "wins", fmt_num, True),
    ("Win rate", "win_rate", lambda v: f"{v:.1f}%", True),
    ("K/D", "kd", lambda v: f"{v:.2f}", True),
    ("Kills", "kills", fmt_num, True),
    ("Kills / match", "kpm", lambda v: f"{v:.2f}", True),
    ("Top 10s", "top10", fmt_num, True),
    ("Matches", "matches", fmt_num, True),
    ("Time played", "minutes", fmt_hours, True),
)


def render_compare(a: dict, b: dict, window_label: str) -> bytes:
    row_h = 72
    h = 250 + len(COMPARE_ROWS) * row_h + 90
    canvas = _background(h)
    d = ImageDraw.Draw(canvas, "RGBA")

    # names + VS
    mid = W // 2
    nf_a = rc.fit_text(d, a["name"], "display", 56, mid - PAD - 90, 26)
    nf_b = rc.fit_text(d, b["name"], "display", 56, mid - PAD - 90, 26)
    rc.shadow_text(d, (PAD, 40), a["name"], nf_a, offset=3)
    rc.shadow_text(d, (W - PAD, 40), b["name"], nf_b, offset=3, anchor="ra")
    d.ellipse([mid - 46, 34, mid + 46, 126], fill=GOLD)
    d.text((mid, 80), "VS", font=rc.font("display", 44), fill=(40, 32, 0), anchor="mm")
    sub = f"{window_label.upper()} · ALL INPUTS"
    d.text((mid, 160), sub, font=rc.font("semi", 18), fill=rc.MUTED, anchor="mm")

    score_a = score_b = 0
    y = 200
    for label, key, fmt, higher_better in COMPARE_ROWS:
        va, vb = a["overall"].get(key) or 0, b["overall"].get(key) or 0
        win_a = va > vb if higher_better else va < vb
        win_b = vb > va if higher_better else vb < va
        score_a += win_a
        score_b += win_b
        _panel(d, [PAD, y, W - PAD, y + row_h - 12], radius=14)
        d.text((mid, y + (row_h - 12) // 2), label.upper(), font=rc.font("semi", 18), fill=rc.MUTED, anchor="mm")
        for value, win, x, anchor in ((fmt(va), win_a, PAD + 24, "lm"), (fmt(vb), win_b, W - PAD - 24, "rm")):
            color = GREEN if win else (rc.WHITE if va == vb else (190, 190, 215))
            d.text((x, y + (row_h - 12) // 2), value, font=rc.font("display", 36), fill=color, anchor=anchor)
        if win_a or win_b:
            bar_x = PAD if win_a else W - PAD - 8
            d.rounded_rectangle([bar_x, y + 10, bar_x + 8, y + row_h - 22], 4, fill=GREEN)
        y += row_h

    if score_a == score_b:
        verdict = f"Dead even — {score_a} categories each"
    else:
        leader = a["name"] if score_a > score_b else b["name"]
        verdict = f"{leader} takes it, {max(score_a, score_b)}–{min(score_a, score_b)}"
    d.text((mid, y + 26), verdict, font=rc.font("display", 32), fill=GOLD, anchor="mm")
    d.text((PAD, h - 36), "data via fortnite-api.com · not affiliated with Epic Games",
           font=rc.font("semi", 15), fill=(170, 170, 210))
    return rc.to_png(canvas)
