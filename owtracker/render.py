"""Image cards for the Overwatch cog: profile, hero, compare and spray board.

All functions are pure (no network): callers pass plain dicts plus an
`images` mapping of url -> bytes. Run them in an executor.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageOps

from . import render_common as rc

W = 1200
PAD = 40
ORANGE = (249, 158, 26)
BG_TOP = (28, 36, 62)
BG_BOTTOM = (10, 13, 26)
PANEL = (255, 255, 255, 16)
EDGE = (255, 255, 255, 30)
GREEN = (72, 214, 120)
MUTED = rc.MUTED
ROLE_COLORS = {"tank": (77, 148, 255), "damage": (238, 84, 92), "support": (72, 214, 120), "open": (249, 158, 26)}
ROLE_LABELS = {"tank": "TANK", "damage": "DAMAGE", "support": "SUPPORT", "open": "OPEN QUEUE"}


# ─────────────────────────────── helpers ─────────────────────────────── #
def hours(seconds) -> str:
    if not seconds:
        return "0h"
    h = seconds / 3600
    return f"{h:,.0f}h" if h >= 10 else f"{h:.1f}h"


def num(n, decimals=0) -> str:
    if n is None:
        return "—"
    if decimals:
        return f"{n:,.{decimals}f}"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n:,.0f}"


def _bg(w, h):
    bg = rc.gradient((w, h), BG_TOP, BG_BOTTOM)  # RGB so translucent shapes blend
    d = ImageDraw.Draw(bg, "RGBA")
    # Overwatch-style orange chevrons in the corner
    for i, a in enumerate((26, 16, 10)):
        x = w - 120 - i * 70
        d.polygon([(x, 0), (x + 46, 0), (x - 54, h), (x - 100, h)], fill=ORANGE + (a,))
    return bg


def _panel(d, box, radius=16, fill=PANEL, edge=EDGE):
    d.rounded_rectangle(box, radius, fill=fill, outline=edge, width=2)


def _img(images, url):
    return rc.open_image(images.get(url)) if url else None


def _paste(canvas, img, xy, size, round_px=0, cover=True):
    if img is None:
        return
    w, h = size
    fitted = rc.cover_fit(img, w, h) if cover else rc.contain_fit(img, w, h)
    if round_px:
        fitted = rc.round_corners(fitted, round_px)
    x, y = xy
    if not cover:
        x += (w - fitted.width) // 2
        y += (h - fitted.height) // 2
    canvas.paste(fitted, (x, y), fitted)


def _tile(d, box, label, value, accent=None, size=46):
    x0, y0, x1, y1 = box
    _panel(d, box)
    if accent:
        d.rounded_rectangle([x0 + 16, y0 + 14, x0 + 52, y0 + 19], 3, fill=accent)
    d.text((x0 + 16, y0 + (26 if accent else 16)), label.upper(), font=rc.font("semi", 14), fill=MUTED)
    f = rc.fit_text(d, value, "display", size, x1 - x0 - 32, 20)
    d.text((x0 + 16, y1 - 14), value, font=f, fill=rc.WHITE, anchor="ls")


def _banner(canvas, d, p, images, height=210):
    """Namecard banner with avatar, name, title and endorsement."""
    banner = _img(images, p.get("namecard"))
    if banner is not None:
        art = rc.cover_fit(banner, W, height).convert("RGB")
        canvas.paste(art, (0, 0))
    shade = Image.new("RGBA", (W, height), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for x in range(W):  # dark on the left so text is readable over any banner
        a = int(200 * max(0.0, 1 - x / (W * 0.75)) + 40)
        sd.line([(x, 0), (x, height)], fill=(8, 10, 20, min(a, 235)))
    canvas.paste(shade, (0, 0), shade)
    d.rectangle([0, height - 4, W, height], fill=ORANGE)

    av = _img(images, p.get("avatar"))
    ax, ay, asz = PAD, (height - 128) // 2, 128
    if av is not None:
        _paste(canvas, av, (ax, ay), (asz, asz), round_px=20)
    else:
        d.rounded_rectangle([ax, ay, ax + asz, ay + asz], 20, fill=(255, 255, 255, 30))
    d.rounded_rectangle([ax - 3, ay - 3, ax + asz + 3, ay + asz + 3], 22, outline=ORANGE, width=3)

    tx = ax + asz + 28
    nf = rc.fit_text(d, p["name"], "display", 64, W - tx - 260, 30)
    rc.shadow_text(d, (tx, ay - 4), p["name"], nf, offset=3)
    if p.get("title"):
        d.text((tx + 2, ay + 74), p["title"], font=rc.font("semi", 22), fill=(230, 230, 240))
    chips = [c for c in (p.get("mode_label"), p.get("platform_label")) if c]
    cx = tx
    for i, text in enumerate(chips):
        bg, fg = (ORANGE, (40, 24, 0)) if i == 0 else ((255, 255, 255), (20, 24, 40))
        cx += rc.pill(d, (cx, ay + 106), text.upper(), rc.font("bold", 15), bg, fg) + 8
    if p.get("endorsement"):
        x1 = W - PAD
        d.rounded_rectangle([x1 - 150, ay + 10, x1, ay + 90], 14, fill=(0, 0, 0, 120), outline=EDGE, width=2)
        d.text((x1 - 75, ay + 28), "ENDORSEMENT", font=rc.font("semi", 13), fill=MUTED, anchor="mm")
        d.text((x1 - 75, ay + 62), f"LVL {p['endorsement']}", font=rc.font("display", 32), fill=ORANGE, anchor="mm")


def _footer(d, h, text="data via OverFast API · not affiliated with Blizzard"):
    d.text((PAD, h - 34), text, font=rc.font("semi", 14), fill=(150, 158, 190))


# ─────────────────────────────── profile ─────────────────────────────── #
def render_profile(p: dict, images: dict) -> bytes:
    """p: name, title, avatar, namecard, endorsement, mode_label, platform_label,
    ranks [(role, rank_dict)], season, general, roles {role: stats}, heroes [hero dicts]."""
    has_heroes = bool(p.get("heroes"))
    h = 210 + 24 + 150 + 24 + 230 + (330 if has_heroes else 0) + 100
    canvas = _bg(W, h)
    d = ImageDraw.Draw(canvas, "RGBA")
    _banner(canvas, d, p, images)

    # ranks
    y = 234
    ranks = p.get("ranks") or []
    label = "COMPETITIVE RANKS" + (f" · SEASON {p['season']}" if p.get("season") else "")
    d.text((PAD, y), label, font=rc.font("display", 24), fill=rc.WHITE)
    y += 36
    slots = ranks or [("none", None)]
    gap = 14
    rw = (W - PAD * 2 - gap * (len(slots) - 1)) // len(slots)
    for i, (role, rank) in enumerate(slots):
        x = PAD + i * (rw + gap)
        box = [x, y, x + rw, y + 110]
        _panel(d, box)
        if rank is None:
            d.text((x + 20, y + 40), "Unranked or private career", font=rc.font("semi", 22), fill=MUTED)
            continue
        color = ROLE_COLORS.get(role, ORANGE)
        d.rounded_rectangle([x, y, x + 8, y + 110], 4, fill=color)
        icon = _img(images, rank.get("rank_icon"))
        if icon is not None:
            _paste(canvas, icon, (x + 22, y + 15), (80, 80), cover=False)
        tx = x + 116
        role_text = ROLE_LABELS.get(role, role.upper()) + (f" · {rank['platform'].upper()}" if rank.get("platform") else "")
        d.text((tx, y + 20), role_text, font=rc.font("semi", 15), fill=color)
        div = (rank.get("division") or "").title()
        f = rc.fit_text(d, div, "display", 40, rw - 140 - 40, 20)
        d.text((tx, y + 82), div, font=f, fill=rc.WHITE, anchor="ls")
        if rank.get("tier"):
            dx = tx + d.textlength(div, font=f) + 10
            d.text((dx, y + 82), str(rank["tier"]), font=rc.font("display", 40), fill=ORANGE, anchor="ls")

    # overview tiles
    y += 134
    g = p.get("general") or {}
    avg = g.get("average") or {}
    tiles = (
        ("Win rate", f"{g.get('winrate') or 0:.1f}%", ORANGE),
        ("KDA", num(g.get("kda"), 2), GREEN),
        ("Games", num(g.get("games_played")), (120, 170, 255)),
        ("Time played", hours(g.get("time_played")), (238, 84, 92)),
        ("Elims / game", num(avg.get("eliminations"), 1), None),
        ("Deaths / game", num(avg.get("deaths"), 1), None),
        ("Damage / game", num(avg.get("damage")), None),
        ("Healing / game", num(avg.get("healing")), None),
    )
    tw = (W - PAD * 2 - 14 * 3) // 4
    for i, (lab, val, acc) in enumerate(tiles):
        r, c = divmod(i, 4)
        x = PAD + c * (tw + 14)
        ty = y + r * 108
        _tile(d, [x, ty, x + tw, ty + 96], lab, val, acc, size=44 if r == 0 else 36)

    # role time split
    y += 220
    roles = p.get("roles") or {}
    total = sum((roles.get(r) or {}).get("time_played") or 0 for r in ("tank", "damage", "support"))
    if total:
        bx0, bx1 = PAD, W - PAD
        d.rounded_rectangle([bx0, y, bx1, y + 14], 7, fill=(255, 255, 255, 26))
        x = bx0
        legend = PAD
        for role in ("tank", "damage", "support"):
            t = (roles.get(role) or {}).get("time_played") or 0
            if not t:
                continue
            w = (bx1 - bx0) * t / total
            if w >= 2:
                d.rounded_rectangle([x, y, x + w, y + 14], 7, fill=ROLE_COLORS[role])
            x += w
            wr = (roles.get(role) or {}).get("winrate")
            text = f"{role.title()} {t / total * 100:.0f}%" + (f" · {wr:.0f}% WR" if wr is not None else "")
            d.ellipse([legend, y + 26, legend + 12, y + 38], fill=ROLE_COLORS[role])
            d.text((legend + 18, y + 22), text, font=rc.font("semi", 16), fill=rc.WHITE)
            legend += int(d.textlength(text, font=rc.font("semi", 16))) + 46

    # top heroes
    if has_heroes:
        y += 70
        d.text((PAD, y), "TOP HEROES", font=rc.font("display", 24), fill=rc.WHITE)
        y += 38
        heroes = p["heroes"][:3]
        hw = (W - PAD * 2 - 14 * 2) // 3
        for i, hero in enumerate(heroes):
            x = PAD + i * (hw + 14)
            _hero_panel(canvas, d, [x, y, x + hw, y + 250], hero, images)

    _footer(d, h)
    return rc.to_png(canvas)


def _hero_panel(canvas, d, box, hero, images):
    x0, y0, x1, y1 = box
    color = ROLE_COLORS.get(hero.get("role"), ORANGE)
    _panel(d, box)
    portrait = _img(images, hero.get("portrait"))
    if portrait is not None:
        _paste(canvas, portrait, (x0 + 16, y0 + 16), (96, 96), round_px=14)
    d.rounded_rectangle([x0 + 14, y0 + 14, x0 + 114, y0 + 114], 16, outline=color, width=3)
    nf = rc.fit_text(d, hero["name"], "display", 32, x1 - x0 - 150, 18)
    d.text((x0 + 128, y0 + 22), hero["name"], font=nf, fill=rc.WHITE)
    s = hero["stats"]
    d.text((x0 + 128, y0 + 66), f"{hours(s.get('time_played'))} · {num(s.get('games_played'))} games",
           font=rc.font("semi", 16), fill=MUTED)
    avg = s.get("average") or {}
    main_stat = ("Healing", avg.get("healing")) if hero.get("role") == "support" else ("Damage", avg.get("damage"))
    rows = (
        ("Win rate", f"{s.get('winrate') or 0:.1f}%"),
        ("KDA", num(s.get("kda"), 2)),
        (f"{main_stat[0]} / game", num(main_stat[1])),
    )
    ry = y0 + 132
    for label, value in rows:
        d.text((x0 + 18, ry), label, font=rc.font("semi", 17), fill=MUTED)
        d.text((x1 - 18, ry), value, font=rc.font("bold", 19), fill=rc.WHITE, anchor="ra")
        ry += 36


# ─────────────────────────────── hero ─────────────────────────────── #
def render_hero(p: dict, images: dict) -> bytes:
    """p: player, hero {name, role, portrait}, stats, mode_label, meta {winrate, pickrate} or None."""
    h = 640
    canvas = _bg(W, h)
    d = ImageDraw.Draw(canvas, "RGBA")
    hero = p["hero"]
    color = ROLE_COLORS.get(hero.get("role"), ORANGE)

    portrait = _img(images, hero.get("portrait"))
    if portrait is not None:
        _paste(canvas, portrait, (PAD, PAD), (220, 220), round_px=24)
    d.rounded_rectangle([PAD - 4, PAD - 4, PAD + 224, PAD + 224], 26, outline=color, width=4)
    tx = PAD + 252
    d.text((tx, PAD + 4), ROLE_LABELS.get(hero.get("role"), "").replace(" QUEUE", ""), font=rc.font("semi", 18), fill=color)
    nf = rc.fit_text(d, hero["name"], "display", 76, W - tx - PAD, 30)
    rc.shadow_text(d, (tx, PAD + 30), hero["name"], nf, offset=3)
    d.text((tx + 2, PAD + 120), p["player"], font=rc.font("bold", 26), fill=rc.WHITE)
    cx = tx
    for i, text in enumerate(c for c in (p.get("mode_label"), p.get("platform_label")) if c):
        bg, fg = (ORANGE, (40, 24, 0)) if i == 0 else ((255, 255, 255), (20, 24, 40))
        cx += rc.pill(d, (cx, PAD + 168), text.upper(), rc.font("bold", 15), bg, fg) + 8

    s = p["stats"]
    avg = s.get("average") or {}
    tiles = (
        ("Time played", hours(s.get("time_played")), ORANGE),
        ("Games", num(s.get("games_played")), (120, 170, 255)),
        ("Win rate", f"{s.get('winrate') or 0:.1f}%", GREEN),
        ("KDA", num(s.get("kda"), 2), (238, 84, 92)),
        ("Elims / game", num(avg.get("eliminations"), 1), None),
        ("Deaths / game", num(avg.get("deaths"), 1), None),
        ("Damage / game", num(avg.get("damage")), None),
        ("Healing / game", num(avg.get("healing")), None),
    )
    y = PAD + 252
    tw = (W - PAD * 2 - 14 * 3) // 4
    for i, (lab, val, acc) in enumerate(tiles):
        r, c = divmod(i, 4)
        x = PAD + c * (tw + 14)
        ty = y + r * 108
        _tile(d, [x, ty, x + tw, ty + 96], lab, val, acc, size=44 if r == 0 else 36)

    meta = p.get("meta")
    if meta:
        mine = s.get("winrate") or 0
        diff = mine - (meta.get("winrate") or 0)
        verdict = "above" if diff >= 0 else "below"
        text = (f"Everyone's {hero['name']}: {meta['winrate']:.1f}% win rate · {meta['pickrate']:.1f}% pick rate "
                f"· you're {abs(diff):.1f} pts {verdict} average")
        d.text((PAD, h - 70), text, font=rc.font("semi", 18), fill=GREEN if diff >= 0 else (238, 120, 120))
    _footer(d, h)
    return rc.to_png(canvas)


# ─────────────────────────────── compare ─────────────────────────────── #
COMPARE_ROWS = (
    ("Win rate", lambda g: g.get("winrate") or 0, lambda v: f"{v:.1f}%"),
    ("KDA", lambda g: g.get("kda") or 0, lambda v: f"{v:.2f}"),
    ("Games", lambda g: g.get("games_played") or 0, num),
    ("Time played", lambda g: g.get("time_played") or 0, hours),
    ("Elims / game", lambda g: (g.get("average") or {}).get("eliminations") or 0, lambda v: f"{v:.1f}"),
    ("Damage / game", lambda g: (g.get("average") or {}).get("damage") or 0, num),
    ("Healing / game", lambda g: (g.get("average") or {}).get("healing") or 0, num),
    ("Best rank", lambda g: g.get("_best_rank") or 0, None),
)


def render_compare(a: dict, b: dict, mode_label: str, images: dict) -> bytes:
    """a/b: name, avatar, general (+ _best_rank value, _best_rank_label)."""
    row_h = 70
    h = 250 + len(COMPARE_ROWS) * row_h + 100
    canvas = _bg(W, h)
    d = ImageDraw.Draw(canvas, "RGBA")
    mid = W // 2
    for side, p in (("l", a), ("r", b)):
        av = _img(images, p.get("avatar"))
        ax = PAD if side == "l" else W - PAD - 110
        if av is not None:
            _paste(canvas, av, (ax, 34), (110, 110), round_px=18)
        nf = rc.fit_text(d, p["name"], "display", 50, mid - PAD - 190, 24)
        if side == "l":
            rc.shadow_text(d, (ax + 130, 60), p["name"], nf, offset=3)
        else:
            rc.shadow_text(d, (ax - 20, 60), p["name"], nf, offset=3, anchor="ra")
    d.ellipse([mid - 44, 44, mid + 44, 132], fill=ORANGE)
    d.text((mid, 88), "VS", font=rc.font("display", 42), fill=(40, 24, 0), anchor="mm")
    d.text((mid, 175), mode_label.upper(), font=rc.font("semi", 18), fill=MUTED, anchor="mm")

    score_a = score_b = 0
    y = 205
    for label, getter, fmt in COMPARE_ROWS:
        va, vb = getter(a["general"]), getter(b["general"])
        win_a, win_b = va > vb, vb > va
        score_a += win_a
        score_b += win_b
        _panel(d, [PAD, y, W - PAD, y + row_h - 12], radius=14)
        cy = y + (row_h - 12) // 2
        d.text((mid, cy), label.upper(), font=rc.font("semi", 17), fill=MUTED, anchor="mm")
        texts = (
            (a["general"].get("_best_rank_label", "—"), b["general"].get("_best_rank_label", "—"))
            if fmt is None else (fmt(va), fmt(vb))
        )
        for text, win, x, anchor in ((texts[0], win_a, PAD + 24, "lm"), (texts[1], win_b, W - PAD - 24, "rm")):
            color = GREEN if win else (rc.WHITE if va == vb else (185, 188, 210))
            d.text((x, cy), text, font=rc.font("display", 32), fill=color, anchor=anchor)
        if win_a or win_b:
            bx = PAD if win_a else W - PAD - 8
            d.rounded_rectangle([bx, y + 10, bx + 8, y + row_h - 22], 4, fill=GREEN)
        y += row_h

    if score_a == score_b:
        verdict = f"Dead even, {score_a}–{score_b}"
    else:
        lead = a["name"] if score_a > score_b else b["name"]
        verdict = f"{lead} takes it, {max(score_a, score_b)}–{min(score_a, score_b)}"
    d.text((mid, y + 30), verdict, font=rc.font("display", 32), fill=ORANGE, anchor="mm")
    _footer(d, h)
    return rc.to_png(canvas)


# ─────────────────────────────── sprays ─────────────────────────────── #
def render_sprays(p: dict, images: dict) -> bytes:
    """p: name, avatar, done, total, groups [(role_title, [(hero, portrait_url, done)])]."""
    cols, cell, gap = 8, 124, 12
    title_h = 50
    body = 0
    for _, entries in p["groups"]:
        rows = (len(entries) + cols - 1) // cols
        body += title_h + rows * (cell + 34) + 20
    h = 190 + body + 50
    w = PAD * 2 + cols * cell + (cols - 1) * gap
    canvas = _bg(w, h)
    d = ImageDraw.Draw(canvas, "RGBA")

    av = _img(images, p.get("avatar"))
    if av is not None:
        _paste(canvas, av, (PAD, 36), (96, 96), round_px=16)
    tx = PAD + (116 if av is not None else 0)
    nf = rc.fit_text(d, f"{p['name']}'s Cute Sprays", "display", 50, w - tx - PAD, 24)
    rc.shadow_text(d, (tx, 34), f"{p['name']}'s Cute Sprays", nf, offset=3)
    done, total = p["done"], max(p["total"], 1)
    d.text((tx + 2, 100), f"{done} of {p['total']} unlocked · {done / total * 100:.0f}%",
           font=rc.font("semi", 22), fill=MUTED)
    bx0, bx1, by = PAD, w - PAD, 150
    d.rounded_rectangle([bx0, by, bx1, by + 16], 8, fill=(255, 255, 255, 26))
    if done:
        d.rounded_rectangle([bx0, by, bx0 + (bx1 - bx0) * done / total, by + 16], 8, fill=ORANGE)

    y = 190
    for role, entries in p["groups"]:
        color = ROLE_COLORS.get(role.lower(), ORANGE)
        got = sum(1 for e in entries if e[2])
        d.text((PAD, y + 8), role.upper(), font=rc.font("display", 28), fill=color)
        d.text((PAD + d.textlength(role.upper(), font=rc.font("display", 28)) + 14, y + 16),
               f"{got}/{len(entries)}", font=rc.font("semi", 18), fill=MUTED)
        y += title_h
        for i, (hero, url, is_done) in enumerate(entries):
            r, c = divmod(i, cols)
            x = PAD + c * (cell + gap)
            cy = y + r * (cell + 34)
            img = _img(images, url)
            if img is not None:
                tile = rc.cover_fit(img, cell, cell)
                if not is_done:
                    gray = ImageOps.grayscale(tile.convert("RGB")).convert("RGBA")
                    tile = Image.blend(gray, Image.new("RGBA", gray.size, (10, 13, 26, 255)), 0.55)
                tile = rc.round_corners(tile, 16)
                canvas.paste(tile, (x, cy), tile)
            else:
                d.rounded_rectangle([x, cy, x + cell, cy + cell], 16, fill=(255, 255, 255, 20))
            if is_done:
                d.rounded_rectangle([x - 2, cy - 2, x + cell + 2, cy + cell + 2], 18, outline=color, width=3)
                d.ellipse([x + cell - 34, cy + 6, x + cell - 6, cy + 34], fill=GREEN)
                d.line([(x + cell - 27, cy + 20), (x + cell - 22, cy + 26), (x + cell - 13, cy + 13)],
                       fill=rc.WHITE, width=4, joint="curve")
            label = rc.ellipsize(d, hero, rc.font("semi", 15), cell)
            d.text((x + cell // 2, cy + cell + 16), label, font=rc.font("semi", 15),
                   fill=rc.WHITE if is_done else (130, 136, 160), anchor="mm")
        rows = (len(entries) + cols - 1) // cols
        y += rows * (cell + 34) + 20
    _footer(d, h, "Tap “Update my sprays” to tick yours off · progress is self-reported")
    return rc.to_png(canvas)
