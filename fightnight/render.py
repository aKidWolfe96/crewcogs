"""Image cards for the UFC cog: fight-card poster, Tale of the Tape, fighter
card and pick'em leaderboard. Pure functions (no network); pass in an
`images` dict of url -> bytes and run them in an executor.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageOps

from . import render_common as rc
from .scoring import fmt_odds

W = 1200
PAD = 36
RED = (210, 10, 10)
GOLD = (200, 169, 81)
BLUE = (40, 96, 200)
BG_TOP = (30, 30, 36)
BG_BOTTOM = (8, 8, 10)
PANEL = (255, 255, 255, 14)
EDGE = (255, 255, 255, 28)
MUTED = (178, 178, 190)
DIM = (110, 110, 120)
WIN = (72, 214, 120)


def _bg(w, h):
    bg = rc.gradient((w, h), BG_TOP, BG_BOTTOM)
    d = ImageDraw.Draw(bg, "RGBA")
    # octagon-ish red sweep
    d.polygon([(0, 0), (w * 0.42, 0), (w * 0.30, 150), (0, 150)], fill=RED + (40,))
    d.rectangle([0, 0, w, 6], fill=RED)
    return bg


def _img(images, url):
    return rc.open_image(images.get(url)) if url else None


def _paste(canvas, img, xy):
    canvas.paste(img, xy, img)


def _headshot(images, info, size, flip=False):
    """Headshot cropped to a rounded square; falls back to a blank silhouette tile."""
    im = _img(images, (info or {}).get("headshot"))
    tile = Image.new("RGBA", (size, size), (255, 255, 255, 18))
    if im is not None:
        im = rc.cover_fit(im, size, size, anchor_top=True)
        if flip:
            im = ImageOps.mirror(im)
        tile = Image.alpha_composite(tile, im)
    return rc.round_corners(tile, 14)


def _flag(images, info, h=22):
    im = _img(images, (info or {}).get("flag"))
    if im is None:
        return None
    im.thumbnail((h * 2, h), Image.LANCZOS)
    return im


def _title(canvas, title, sub):
    d = ImageDraw.Draw(canvas)
    f = rc.fit_text(d, title.upper(), "display", 56, W - PAD * 2, 26)
    rc.shadow_text(d, (PAD, 26), title.upper(), f, offset=3)
    if sub:
        d.text((PAD + 2, 96), sub, font=rc.font("semi", 21), fill=MUTED)


def _footer(canvas, text="data via ESPN · odds via DraftKings · not affiliated with the UFC"):
    d = ImageDraw.Draw(canvas)
    d.text((PAD, canvas.height - 30), text, font=rc.font("semi", 13), fill=DIM)


# ─────────────────────────────── card poster ─────────────────────────────── #
SEGMENT_LABELS = {"main": "MAIN CARD", "prelims1": "PRELIMS", "prelims2": "EARLY PRELIMS"}


def render_card(event: dict, images: dict, splits: dict | None = None) -> bytes:
    """splits: {fight_key: (red_count, blue_count)} for live pick percentages."""
    fights = event.get("fights", [])
    row_h, seg_h = 124, 46
    segments = []
    for f in fights:
        seg = f.get("segment") or "main"
        if not segments or segments[-1][0] != seg:
            segments.append((seg, []))
        segments[-1][1].append(f)
    h = 140 + sum(seg_h + len(fs) * row_h for _, fs in segments) + 50
    canvas = _bg(W, h)
    sub = " · ".join(x for x in (event.get("date"), event.get("location")) if x)
    _title(canvas, event.get("name", "UFC"), sub)
    d = ImageDraw.Draw(canvas, "RGBA")

    y = 140
    for seg, fs in segments:
        label = SEGMENT_LABELS.get(seg, (fs[0].get("segment_label") or seg).upper())
        d.text((PAD, y + 10), label, font=rc.font("display", 26), fill=GOLD)
        d.line([(PAD + d.textlength(label, font=rc.font("display", 26)) + 14, y + 26), (W - PAD, y + 26)],
               fill=(255, 255, 255, 30), width=1)
        y += seg_h
        for f in fs:
            _fight_row(canvas, d, f, PAD, y, W - PAD * 2, row_h - 12, images, (splits or {}).get(f"{f['red']}|{f['blue']}"))
            y += row_h
    _footer(canvas)
    return rc.to_png(canvas)


def _fight_row(canvas, d, f, x, y, w, h, images, split):
    done = f.get("completed")
    winner = f.get("winner")
    d.rounded_rectangle([x, y, x + w, y + h], 14, fill=PANEL, outline=GOLD + (120,) if f.get("is_title") else EDGE, width=2)
    hs = h - 16
    mid = x + w // 2
    for side in ("red", "blue"):
        info = f.get(f"{side}_info") or {}
        name = f[side]
        lost = done and winner and winner != name
        left = side == "red"
        hx = x + 8 if left else x + w - 8 - hs
        tile = _headshot(images, info, hs, flip=not left)
        if lost:
            tile = Image.blend(tile, Image.new("RGBA", tile.size, (8, 8, 10, 255)), 0.55)
        _paste(canvas, tile, (hx, y + 8))
        if done and winner == name:
            d.rounded_rectangle([hx - 2, y + 6, hx + hs + 2, y + 10 + hs], 16, outline=WIN, width=3)
        tx = hx + hs + 16 if left else hx - 16
        anchor = "la" if left else "ra"
        maxw = mid - 90 - (tx if left else 0) if left else tx - (mid + 90)
        nf = rc.fit_text(d, name, "bold", 24, maxw, 14)
        d.text((tx, y + 16), name, font=nf, fill=DIM if lost else rc.WHITE, anchor=anchor)
        # record · flag · odds
        rec = f.get(f"{side}_record") or ""
        flag = _flag(images, info, 18)
        line_y = y + 52
        cx = tx
        bits = []
        if rec:
            bits.append(rec)
        odds = f.get(f"{side}_odds")
        text = "  ".join(bits)
        tf = rc.font("semi", 17)
        tw = d.textlength(text, font=tf)
        if left:
            if flag is not None:
                _paste(canvas, flag, (int(cx), line_y + 1))
                cx += flag.width + 8
            d.text((cx, line_y), text, font=tf, fill=MUTED)
            cx += tw + 10
            if odds is not None and not done:
                rc.pill(d, (int(cx), line_y - 2), fmt_odds(odds), rc.font("bold", 14),
                        (60, 160, 90) if odds < 0 else (90, 90, 110), rc.WHITE, pad_x=7, pad_y=3)
        else:
            if flag is not None:
                _paste(canvas, flag, (int(cx - flag.width), line_y + 1))
                cx -= flag.width + 8
            d.text((cx, line_y), text, font=tf, fill=MUTED, anchor="ra")
            cx -= tw + 10
            if odds is not None and not done:
                rc.pill(d, (int(cx), line_y - 2), fmt_odds(odds), rc.font("bold", 14),
                        (60, 160, 90) if odds < 0 else (90, 90, 110), rc.WHITE, pad_x=7, pad_y=3, anchor_right=True)
        if split and not done:
            n = split[0] if left else split[1]
            total = max(sum(split), 1)
            pct = f"{n / total * 100:.0f}% of picks" if sum(split) else ""
            if pct:
                d.text((tx, y + 80), pct, font=rc.font("semi", 14), fill=MUTED, anchor=anchor)

    # centre column
    wc = f.get("weight_class") or ""
    tag = "TITLE FIGHT" if f.get("is_title") else ("MAIN EVENT" if f.get("main_event") else "")
    if done:
        res = (f.get("method") or "Final").replace("Decision - ", "Dec · ")
        rnd = f"R{f['round']} {f.get('time', '')}".strip() if f.get("round") and f.get("method_key") != "dec" else ""
        d.text((mid, y + 26), "FINAL", font=rc.font("display", 22), fill=WIN, anchor="mm")
        d.text((mid, y + 54), rc.ellipsize(d, res, rc.font("bold", 16), 170), font=rc.font("bold", 16), fill=rc.WHITE, anchor="mm")
        if rnd:
            d.text((mid, y + 78), rnd, font=rc.font("semi", 14), fill=MUTED, anchor="mm")
    else:
        d.text((mid, y + 30), "VS", font=rc.font("display", 30), fill=RED, anchor="mm")
        d.text((mid, y + 60), rc.ellipsize(d, wc, rc.font("semi", 14), 170), font=rc.font("semi", 14), fill=MUTED, anchor="mm")
        if tag:
            d.text((mid, y + 82), tag, font=rc.font("bold", 13), fill=GOLD, anchor="mm")


# ─────────────────────────────── tale of the tape ─────────────────────────── #
TAPE_ROWS = (
    ("Record", lambda i, f, s: f.get(f"{s}_record") or "—"),
    ("Age", lambda i, f, s: str(i.get("age") or "—")),
    ("Height", lambda i, f, s: i.get("height") or "—"),
    ("Reach", lambda i, f, s: i.get("reach") or "—"),
    ("Stance", lambda i, f, s: i.get("stance") or "—"),
    ("Sig. strikes / min", lambda i, f, s: (i.get("stats") or {}).get("strikeLPM") or "—"),
    ("Striking accuracy", lambda i, f, s: _pct((i.get("stats") or {}).get("strikeAccuracy"))),
    ("Takedowns / 15 min", lambda i, f, s: (i.get("stats") or {}).get("takedownAvg") or "—"),
    ("Takedown accuracy", lambda i, f, s: _pct((i.get("stats") or {}).get("takedownAccuracy"))),
    ("Subs / 15 min", lambda i, f, s: (i.get("stats") or {}).get("submissionAvg") or "—"),
    ("Odds", lambda i, f, s: fmt_odds(f.get(f"{s}_odds"))),
)


def _pct(v):
    try:
        return f"{float(v):.0f}%"
    except (TypeError, ValueError):
        return "—"


def render_tape(fight: dict, event_name: str, images: dict) -> bytes:
    h = 900
    canvas = _bg(W, h)
    d = ImageDraw.Draw(canvas, "RGBA")
    tag = "TITLE FIGHT" if fight.get("is_title") else ("MAIN EVENT" if fight.get("main_event") else "FEATURED BOUT")
    d.text((W // 2, 30), "TALE OF THE TAPE", font=rc.font("display", 46), fill=rc.WHITE, anchor="ma")
    d.text((W // 2, 86), f"{event_name} · {fight.get('weight_class', '')} · {tag}", font=rc.font("semi", 19),
           fill=GOLD, anchor="ma")

    body_w, body_h = 330, 640
    for side in ("red", "blue"):
        info = fight.get(f"{side}_info") or {}
        left = side == "red"
        url = info.get("stance_left") if left else info.get("stance_right")
        im = _img(images, url)
        x0 = 10 if left else W - 10 - body_w
        if im is not None:
            im = rc.contain_fit(im, body_w, body_h)
            _paste(canvas, im, (x0 + (body_w - im.width) // 2, h - 40 - im.height))
        else:
            hs = _headshot(images, info, 220, flip=not left)
            _paste(canvas, hs, (x0 + (body_w - 220) // 2, 260))
        # name plate
        nf = rc.fit_text(d, fight[side].upper(), "display", 34, 300, 18)
        ny = 130
        nx = PAD if left else W - PAD
        d.text((nx, ny), fight[side].upper(), font=nf, fill=rc.WHITE, anchor="la" if left else "ra")
        flag = _flag(images, info, 24)
        if flag is not None:
            _paste(canvas, flag, (nx if left else nx - flag.width, ny + 48))
        country = info.get("country") or ""
        if country:
            fx = (nx + (flag.width + 8 if flag is not None else 0)) if left else (nx - (flag.width + 8 if flag is not None else 0))
            d.text((fx, ny + 48), country, font=rc.font("semi", 17), fill=MUTED, anchor="la" if left else "ra")

    # stat table in the middle
    tx0, tx1 = 360, W - 360
    y = 210
    row_h = 54
    for label, getter in TAPE_ROWS:
        lv = getter(fight.get("red_info") or {}, fight, "red")
        rv = getter(fight.get("blue_info") or {}, fight, "blue")
        d.rounded_rectangle([tx0, y, tx1, y + row_h - 8], 10, fill=(0, 0, 0, 120), outline=EDGE, width=1)
        cy = y + (row_h - 8) // 2
        d.text((tx0 + 16, cy), lv, font=rc.font("bold", 20), fill=rc.WHITE, anchor="lm")
        d.text((tx1 - 16, cy), rv, font=rc.font("bold", 20), fill=rc.WHITE, anchor="rm")
        d.text(((tx0 + tx1) // 2, cy), label.upper(), font=rc.font("semi", 13), fill=MUTED, anchor="mm")
        y += row_h
    _footer(canvas)
    return rc.to_png(canvas)


# ─────────────────────────────── fighter card ─────────────────────────────── #
RESULT_COLORS = {"win": WIN, "loss": (238, 84, 92), "draw": GOLD, "nc": DIM}


def render_fighter(f: dict, images: dict) -> bytes:
    fights = (f.get("fights") or [])[:6]
    h = 330 + max(1, len(fights)) * 56 + 60
    canvas = _bg(W, h)
    d = ImageDraw.Draw(canvas, "RGBA")
    hs = _headshot(images, {"headshot": f.get("headshot")}, 230)
    _paste(canvas, hs, (PAD, 40))
    tx = PAD + 260
    nf = rc.fit_text(d, f.get("name", "").upper(), "display", 60, W - tx - PAD, 26)
    rc.shadow_text(d, (tx, 40), f.get("name", "").upper(), nf, offset=3)
    if f.get("nickname"):
        d.text((tx + 2, 112), f"“{f['nickname']}”", font=rc.font("semi", 24), fill=GOLD)
    rec = f.get("record") or ""
    if rec:
        d.text((tx, 150), rec, font=rc.font("display", 64), fill=rc.WHITE)
        d.text((tx + d.textlength(rec, font=rc.font("display", 64)) + 14, 186), "W-L-D", font=rc.font("semi", 16), fill=MUTED)
    def txt(v):
        if isinstance(v, dict):
            return v.get("name") or v.get("displayName") or v.get("abbreviation") or ""
        return str(v or "")

    bits = [txt(b) for b in (f.get("weight_class"), f"Age {f['age']}" if f.get("age") else "", f.get("height"),
                             f.get("country"), f.get("gym")) if txt(b)]
    d.text((tx + 2, 236), "  ·  ".join(bits), font=rc.font("semi", 18), fill=MUTED)

    y = 300
    d.text((PAD, y), "RECENT FIGHTS", font=rc.font("display", 26), fill=GOLD)
    y += 42
    if not fights:
        d.text((PAD, y + 10), "No fight history available.", font=rc.font("semi", 20), fill=MUTED)
    for fi in fights:
        res = (fi.get("result") or "").lower()
        color = RESULT_COLORS.get(res, DIM)
        d.rounded_rectangle([PAD, y, W - PAD, y + 46], 10, fill=PANEL, outline=EDGE, width=1)
        d.rounded_rectangle([PAD, y, PAD + 64, y + 46], 10, fill=color)
        d.text((PAD + 32, y + 23), res.upper()[:4] or "?", font=rc.font("bold", 16), fill=(10, 10, 12), anchor="mm")
        d.text((PAD + 80, y + 23), f"vs {fi.get('opponent', '?')}", font=rc.font("bold", 19), fill=rc.WHITE, anchor="lm")
        method = (fi.get("method") or "").replace("VIEW PLAY-BY-PLAY", "").strip()
        rnd = f" · R{fi['round']} {fi.get('time', '')}" if fi.get("round") else ""
        d.text((W - PAD - 16, y + 23), rc.ellipsize(d, method + rnd, rc.font("semi", 16), 520),
               font=rc.font("semi", 16), fill=MUTED, anchor="rm")
        y += 56
    _footer(canvas, "data via ESPN + Sherdog · not affiliated with the UFC")
    return rc.to_png(canvas)


# ─────────────────────────────── leaderboard ─────────────────────────────── #
def render_standings(title: str, subtitle: str, rows: list[dict], currency: str) -> bytes:
    """rows: name, points, correct, total, bonus, net, best_upset (str)."""
    row_h = 74
    h = 150 + max(1, len(rows)) * row_h + 50
    canvas = _bg(W, h)
    _title(canvas, title, subtitle)
    d = ImageDraw.Draw(canvas, "RGBA")
    medal = {0: GOLD, 1: (192, 192, 200), 2: (205, 127, 50)}
    y = 140
    if not rows:
        d.text((PAD, y + 20), "No picks scored yet.", font=rc.font("semi", 22), fill=MUTED)
    for i, r in enumerate(rows):
        d.rounded_rectangle([PAD, y, W - PAD, y + row_h - 10], 14, fill=PANEL, outline=EDGE, width=1)
        cy = y + (row_h - 10) // 2
        c = medal.get(i)
        if c:
            d.ellipse([PAD + 14, cy - 20, PAD + 54, cy + 20], fill=c)
            d.text((PAD + 34, cy), str(i + 1), font=rc.font("display", 24), fill=(20, 20, 24), anchor="mm")
        else:
            d.text((PAD + 34, cy), str(i + 1), font=rc.font("display", 24), fill=MUTED, anchor="mm")
        d.text((PAD + 74, cy - 12), rc.ellipsize(d, r["name"], rc.font("bold", 23), 330), font=rc.font("bold", 23),
               fill=rc.WHITE, anchor="lm")
        acc = f"{r['correct']}/{r['total']} · {r['correct'] / r['total'] * 100:.0f}%" if r["total"] else "no picks yet"
        extra = f" · {r['bonus']} bonus" if r.get("bonus") else ""
        d.text((PAD + 74, cy + 14), acc + extra, font=rc.font("semi", 15), fill=MUTED, anchor="lm")
        if r.get("best_upset"):
            d.text((PAD + 470, cy), r["best_upset"], font=rc.font("semi", 15), fill=GOLD, anchor="lm")
        net = r.get("net")
        if net:
            d.text((W - PAD - 150, cy), f"{'+' if net > 0 else '−'}{abs(net):,}", font=rc.font("bold", 19),
                   fill=WIN if net > 0 else (238, 84, 92), anchor="rm")
        d.text((W - PAD - 24, cy), f"{r['points']}", font=rc.font("display", 34), fill=rc.WHITE, anchor="rm")
        d.text((W - PAD - 24, cy + 24), "PTS", font=rc.font("semi", 11), fill=MUTED, anchor="rm")
        y += row_h
    if any(r.get("net") for r in rows):
        _footer(canvas, f"Points = correct picks + bonus (method/round) · money column = net {currency} from bets")
    else:
        _footer(canvas, "Points = correct picks + bonus (method/round)")
    return rc.to_png(canvas)


def card_image_urls(event: dict) -> list[str]:
    urls = []
    for f in event.get("fights", []):
        for side in ("red", "blue"):
            info = f.get(f"{side}_info") or {}
            urls += [info.get("headshot"), info.get("flag")]
    return [u for u in urls if u]


def tape_image_urls(fight: dict) -> list[str]:
    ri, bi = fight.get("red_info") or {}, fight.get("blue_info") or {}
    return [u for u in (ri.get("stance_left"), bi.get("stance_right"), ri.get("headshot"), bi.get("headshot"),
                        ri.get("flag"), bi.get("flag")) if u]
