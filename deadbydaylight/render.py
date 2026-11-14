"""Image cards for the Dead by Daylight cog.

Uses the official icons you exported (perks, portraits, items, add-ons,
offerings, powers) when they're present, and falls back to drawn badges when
they aren't. All functions are pure and CPU-only; run them in an executor.

Every function takes plain dicts. Icon bytes ride along in the dicts as
"icon" (perks / gear) and "portrait" (characters).
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from . import render_common as rc

PAD = 40
BLOOD = (196, 28, 40)
BONE = (236, 226, 208)
MUTED = (196, 178, 178)
DIM = (120, 104, 108)
SHARD = (120, 205, 235)
BG_TOP = (36, 10, 14)
BG_BOTTOM = (8, 6, 10)

ROLE_RGB = {"killer": (190, 32, 44), "survivor": (44, 116, 206)}
KIND_RGB = {"hex": (142, 52, 170), "boon": (48, 162, 118), "scourge": (210, 96, 32), "invocation": (120, 120, 140)}
RARITY_RGB = {
    "common": (122, 86, 58), "uncommon": (190, 162, 52), "rare": (58, 138, 64), "veryrare": (120, 60, 172),
    "visceral": (196, 38, 98), "event": (222, 120, 30), "none": (90, 90, 100),
}
RARITY_LABELS = {"common": "Common", "uncommon": "Uncommon", "rare": "Rare", "veryrare": "Very Rare",
                 "visceral": "Ultra Rare", "event": "Event", "none": ""}
SKIP_WORDS = {"of", "the", "a", "an", "and", "&", "to", "for", "in", "on", "til"}


# ─────────────────────────────── base ─────────────────────────────── #
def background(w, h):
    bg = rc.gradient((w, h), BG_TOP, BG_BOTTOM).convert("RGBA")
    fog = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    fd = ImageDraw.Draw(fog)
    fd.ellipse([-w * 0.2, -h * 0.35, w * 0.7, h * 0.45], fill=(150, 20, 30, 70))
    fd.ellipse([w * 0.45, h * 0.55, w * 1.3, h * 1.4], fill=(90, 10, 20, 55))
    fog = fog.filter(ImageFilter.GaussianBlur(max(w, h) // 10))
    bg.alpha_composite(fog)
    bg = bg.convert("RGB")
    sd = ImageDraw.Draw(bg, "RGBA")
    for i in range(3):  # faint claw scratches top-right
        x = w - 150 + i * 26
        sd.line([(x, 18), (x - 60, 120)], fill=(220, 40, 50, 46), width=5)
    return bg  # RGB so translucent shapes drawn later blend properly


def _paste(canvas, img, xy):
    canvas.paste(img, (int(xy[0]), int(xy[1])), img)


def perk_kind(name: str) -> str | None:
    n = name.lower()
    for kind, prefix in (("hex", "hex:"), ("boon", "boon:"), ("scourge", "scourge hook:"), ("invocation", "invocation:")):
        if n.startswith(prefix):
            return kind
    return None


def perk_color(name: str, role: str):
    return KIND_RGB.get(perk_kind(name) or "", ROLE_RGB.get(role, (120, 120, 130)))


def initials(name: str) -> str:
    for prefix in ("Hex: ", "Boon: ", "Scourge Hook: ", "Invocation: "):
        if name.startswith(prefix):
            name = name[len(prefix):]
    words = [w for w in name.replace("-", " ").split() if w.lower().strip("'‘’") not in SKIP_WORDS]
    letters = "".join(w[0] for w in words if w[0].isalnum())[:2]
    return (letters or name[:2]).upper()


def perk_badge(size: int, name: str, role: str, icon: bytes | None = None) -> Image.Image:
    """Diamond perk badge with the official icon inside (or initials as a fallback)."""
    s = size
    color = perk_color(name, role)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    def diamond(inset):
        c, r = s / 2, s / 2 - inset
        return [(c, c - r), (c + r, c), (c, c + r), (c - r, c)]

    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(glow).polygon(diamond(s * 0.06), fill=color + (150,))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(s * 0.05)))
    frame = rc.gradient((s, s), rc.shade(color, 1.55), rc.shade(color, 0.55)).convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).polygon(diamond(s * 0.08), fill=255)
    img.paste(frame, (0, 0), mask)
    inner = rc.gradient((s, s), rc.shade(color, 0.42), rc.shade(color, 0.16)).convert("RGBA")
    mask2 = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask2).polygon(diamond(s * 0.15), fill=255)
    img.paste(inner, (0, 0), mask2)
    d = ImageDraw.Draw(img)
    d.polygon(diamond(s * 0.15), outline=rc.shade(color, 1.8) + (200,), width=max(1, s // 90))

    art = rc.open_image(icon)
    if art is not None:
        art = rc.contain_fit(art, int(s * 0.78), int(s * 0.78))
        _paste(img, art, ((s - art.width) / 2, (s - art.height) / 2))
        return img
    text = initials(name)
    f = rc.font("display", int(s * (0.30 if len(text) > 1 else 0.36)))
    d.text((s / 2 + s * 0.012, s / 2 + s * 0.02), text, font=f, fill=(0, 0, 0, 170), anchor="mm")
    d.text((s / 2, s / 2), text, font=f, fill=BONE, anchor="mm")
    return img


def gear_tile(size: int, gear: dict) -> Image.Image:
    """Item / add-on / power / offering icon on its rarity colour (offerings get a hexagon)."""
    s = size
    rarity = gear.get("rarity") or "none"
    color = RARITY_RGB.get(rarity, RARITY_RGB["none"])
    if gear.get("kind") == "power":
        color = ROLE_RGB["killer"]
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    bg = rc.gradient((s, s), rc.shade(color, 1.25), rc.shade(color, 0.45)).convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    md = ImageDraw.Draw(mask)
    if gear.get("kind") == "offering":
        q = s * 0.06
        md.polygon([(s / 2, q), (s - q, s * 0.27), (s - q, s * 0.73), (s / 2, s - q), (q, s * 0.73), (q, s * 0.27)], fill=255)
    else:
        md.rounded_rectangle([0, 0, s - 1, s - 1], max(6, s // 9), fill=255)
    img.paste(bg, (0, 0), mask)
    # top sheen + rim
    sheen = Image.new("RGBA", (s, s), (255, 255, 255, 0))
    ImageDraw.Draw(sheen).rectangle([0, 0, s, s * 0.42], fill=(255, 255, 255, 22))
    img.paste(Image.alpha_composite(img.copy(), sheen), (0, 0), mask)
    edge = mask.filter(ImageFilter.FIND_EDGES)
    rim = Image.new("RGBA", (s, s), rc.shade(color, 1.7) + (210,))
    img.paste(rim, (0, 0), edge)
    art = rc.open_image(gear.get("icon"))
    if art is not None:
        art = rc.contain_fit(art, int(s * 0.84), int(s * 0.84))
        _paste(img, art, ((s - art.width) / 2, (s - art.height) / 2))
    else:
        d = ImageDraw.Draw(img)
        f = rc.font("display", int(s * 0.3))
        d.text((s / 2, s / 2), initials(gear.get("name", "?")), font=f, fill=BONE, anchor="mm")
    return img


def portrait_tile(w: int, h: int, portrait: bytes | None, role: str, name: str = "", dim: bool = False) -> Image.Image:
    color = ROLE_RGB.get(role, (100, 100, 110))
    tile = rc.gradient((w, h), rc.shade(color, 0.75), rc.shade(color, 0.18)).convert("RGBA")
    art = rc.open_image(portrait)
    if art is not None:
        tile.alpha_composite(rc.cover_fit(art, w, h, anchor_top=True))
    else:
        d = ImageDraw.Draw(tile)
        d.text((w / 2, h / 2), initials(name or "?"), font=rc.font("display", int(min(w, h) * 0.34)), fill=BONE, anchor="mm")
    if dim:
        gray = ImageOps.grayscale(tile.convert("RGB")).convert("RGBA")
        tile = Image.blend(gray, Image.new("RGBA", gray.size, (10, 8, 12, 255)), 0.58)
    return rc.round_corners(tile, max(8, min(w, h) // 10))


def _title(canvas, text, sub=None, y=28):
    d = ImageDraw.Draw(canvas)
    f = rc.fit_text(d, text.upper(), "display", 60, canvas.width - PAD * 2, 28)
    rc.shadow_text(d, (PAD, y), text.upper(), f, fill=BONE, shadow=(60, 0, 6), offset=3)
    if sub:
        d.text((PAD + 2, y + 72), sub, font=rc.font("semi", 22), fill=MUTED)
    d.rectangle([PAD, y + (108 if sub else 80), PAD + 90, y + (112 if sub else 84)], fill=BLOOD)


def _footer(canvas, text="Game data via dbd.tricky.lol · not affiliated with Behaviour Interactive"):
    d = ImageDraw.Draw(canvas)
    d.text((PAD, canvas.height - 32), text, font=rc.font("semi", 14), fill=DIM)


def _label(d, x, y, w, title, sub=None, title_size=19, lines=2):
    ny = y
    for line in rc.wrap(d, title, rc.font("bold", title_size), w - 8, max_lines=lines):
        d.text((x + w / 2, ny), line, font=rc.font("bold", title_size), fill=rc.WHITE, anchor="ma")
        ny += title_size + 5
    if sub:
        d.text((x + w / 2, ny + 1), rc.ellipsize(d, sub, rc.font("semi", 14), w - 8), font=rc.font("semi", 14),
               fill=MUTED, anchor="ma")
        ny += 20
    return ny


# ─────────────────────────────── shrine ─────────────────────────────── #
def render_shrine(perks: list[dict], refresh_label: str) -> bytes:
    """perks: name, role, owner, owner_portrait, icon, shards, bloodpoints."""
    w, h = 1200, 600
    canvas = background(w, h)
    _title(canvas, "Shrine of Secrets", f"This week's perks · refreshes {refresh_label}")
    d = ImageDraw.Draw(canvas, "RGBA")
    col = (w - PAD * 2) / max(len(perks), 1)
    for i, p in enumerate(perks):
        x = PAD + i * col
        d.text((x + col / 2, 168), p["role"].upper(), font=rc.font("semi", 14), fill=ROLE_RGB.get(p["role"], MUTED), anchor="ma")
        badge = perk_badge(200, p["name"], p["role"], p.get("icon"))
        _paste(canvas, badge, (x + (col - 200) / 2, 190))
        ny = _label(d, x, 398, col, p["name"], None, 21)
        owner = p.get("owner") or "General perk"
        pt = portrait_tile(34, 34, p.get("owner_portrait"), p["role"], owner) if p.get("owner") else None
        ow = d.textlength(owner, font=rc.font("semi", 16)) + (40 if pt else 0)
        ox = x + (col - ow) / 2
        if pt:
            _paste(canvas, pt, (ox, ny))
            ox += 40
        d.text((ox, ny + 8), owner, font=rc.font("semi", 16), fill=MUTED)
        ny += 44
        if p.get("shards"):
            d.text((x + col / 2, ny), f"{p['shards']:,} shards", font=rc.font("bold", 17), fill=SHARD, anchor="ma")
            ny += 24
        if p.get("bloodpoints"):
            d.text((x + col / 2, ny), f"or {p['bloodpoints']:,} BP if owned", font=rc.font("bold", 16),
                   fill=(230, 190, 120), anchor="ma")
    _footer(canvas)
    return rc.to_png(canvas)


# ─────────────────────────────── loadout ─────────────────────────────── #
def _hero(canvas, d, x, y, w, h, character: dict, who: str | None, tag: str):
    role = character["role"]
    tile = portrait_tile(w, h, character.get("portrait"), role, character["name"])
    _paste(canvas, tile, (x, y))
    d.rounded_rectangle([x - 2, y - 2, x + w + 2, y + h + 2], 18, outline=rc.shade(ROLE_RGB[role], 1.5), width=3)
    ny = y + h + 16
    d.text((x, ny), tag + (f" · FOR {who.upper()}" if who else ""), font=rc.font("semi", 15), fill=ROLE_RGB.get(role, MUTED))
    nf = rc.fit_text(d, character["name"].upper(), "display", 38, w, 18)
    rc.shadow_text(d, (x, ny + 22), character["name"].upper(), nf, fill=BONE, shadow=(60, 0, 6), offset=2)
    return ny + 70


def render_loadout(character: dict, perks: list[dict], gear: list[dict], who: str | None = None) -> bytes:
    """character: name, role, portrait. perks: name, role, owner, icon. gear: kind, name, rarity, icon, sub."""
    w, h = 1200, 720
    canvas = background(w, h)
    d = ImageDraw.Draw(canvas, "RGBA")
    role = character["role"]
    _hero(canvas, d, PAD, PAD, 320, 360, character, who, "KILLER LOADOUT" if role == "killer" else "SURVIVOR LOADOUT")

    rx = PAD + 360
    rw = w - rx - PAD
    d.text((rx, PAD - 4), "PERKS", font=rc.font("display", 26), fill=BONE)
    col = rw / 4
    for i, p in enumerate(perks):
        x = rx + i * col
        _paste(canvas, perk_badge(150, p["name"], p["role"], p.get("icon")), (x + (col - 150) / 2, PAD + 34))
        _label(d, x, PAD + 192, col, p["name"], p.get("owner") or "General perk", 18)

    gy = PAD + 330
    d.text((rx, gy), "LOADOUT", font=rc.font("display", 26), fill=BONE)
    for i, g in enumerate(gear):
        x = rx + i * col
        _paste(canvas, gear_tile(132, g), (x + (col - 132) / 2, gy + 40))
        sub = g.get("sub") or RARITY_LABELS.get(g.get("rarity") or "", "")
        ny = _label(d, x, gy + 186, col, g["name"], sub, 17)
        if g.get("rarity") in RARITY_RGB and g.get("kind") != "power":
            c = RARITY_RGB[g["rarity"]]
            d.rounded_rectangle([x + col / 2 - 22, ny + 4, x + col / 2 + 22, ny + 8], 2, fill=rc.shade(c, 1.4))
    _footer(canvas)
    return rc.to_png(canvas)


def render_character(character: dict, perks: list[dict], note: str = "") -> bytes:
    w, h = 1200, 560
    canvas = background(w, h)
    d = ImageDraw.Draw(canvas, "RGBA")
    tag = character["role"].upper() + (f" · {character['difficulty'].upper()}" if character.get("difficulty") else "")
    _hero(canvas, d, PAD, PAD, 300, 340, character, None, tag)
    rx = PAD + 360
    rw = w - rx - PAD
    d.text((rx, PAD - 4), "TEACHABLE PERKS", font=rc.font("display", 26), fill=BONE)
    if note:
        d.text((rx, PAD + 30), note, font=rc.font("semi", 17), fill=MUTED)
    col = rw / 3
    for i, p in enumerate(perks):
        x = rx + i * col
        _paste(canvas, perk_badge(190, p["name"], p["role"], p.get("icon")), (x + (col - 190) / 2, PAD + 74))
        _label(d, x, PAD + 276, col, p["name"], None, 21)
    _footer(canvas)
    return rc.to_png(canvas)


def render_squad(rows: list[dict], title: str) -> bytes:
    """rows: player, character{name, role, portrait}, perks[], gear[]."""
    row_h, w = 176, 1200
    h = 150 + len(rows) * row_h + 50
    canvas = background(w, h)
    _title(canvas, title, f"{len(rows)} player{'s' if len(rows) != 1 else ''} · good luck in the fog")
    d = ImageDraw.Draw(canvas, "RGBA")
    y = 150
    for r in rows:
        ch = r["character"]
        color = ROLE_RGB.get(ch["role"], MUTED)
        d.rounded_rectangle([PAD, y, w - PAD, y + row_h - 14], 16, fill=(255, 255, 255, 12), outline=(255, 255, 255, 26), width=2)
        d.rounded_rectangle([PAD, y, PAD + 8, y + row_h - 14], 4, fill=color)
        _paste(canvas, portrait_tile(130, 146, ch.get("portrait"), ch["role"], ch["name"]), (PAD + 16, y + 8))
        tx = PAD + 160
        d.text((tx, y + 16), rc.ellipsize(d, r["player"], rc.font("bold", 22), 200), font=rc.font("bold", 22), fill=rc.WHITE)
        cf = rc.fit_text(d, ch["name"].upper(), "display", 24, 200, 13)
        d.text((tx, y + 48), ch["name"].upper(), font=cf, fill=BONE)
        d.text((tx, y + 82), ch["role"].upper(), font=rc.font("semi", 13), fill=color)
        px = PAD + 380
        for i, p in enumerate(r["perks"]):
            bx = px + i * 96
            _paste(canvas, perk_badge(88, p["name"], p["role"], p.get("icon")), (bx + 4, y + 10))
            d.text((bx + 48, y + 104), rc.ellipsize(d, p["name"], rc.font("semi", 12), 92), font=rc.font("semi", 12),
                   fill=rc.WHITE, anchor="ma")
        gx = px + 4 * 96 + 12
        for i, g in enumerate(r.get("gear", [])):
            bx = gx + i * 84
            _paste(canvas, gear_tile(72, g), (bx + 4, y + 18))
            d.text((bx + 40, y + 104), rc.ellipsize(d, g["name"], rc.font("semi", 12), 82), font=rc.font("semi", 12),
                   fill=MUTED, anchor="ma")
        y += row_h
    _footer(canvas)
    return rc.to_png(canvas)


# ─────────────────────────────── add-on gallery ─────────────────────────────── #
def render_addons(title: str, subtitle: str, parent: dict, addons: list[dict]) -> bytes:
    """parent: gear dict (power or item). addons: gear dicts (sorted by rarity)."""
    cols, tile, cw = 7, 108, 158
    groups: list[tuple[str, list]] = []
    for a in addons:
        if not groups or groups[-1][0] != a["rarity"]:
            groups.append((a["rarity"], []))
        groups[-1][1].append(a)
    body = sum(44 + ((len(g) + cols - 1) // cols) * 176 for _, g in groups)
    w = PAD * 2 + cols * cw
    h = 190 + body + 50
    canvas = background(w, h)
    d = ImageDraw.Draw(canvas, "RGBA")
    _paste(canvas, gear_tile(120, parent), (PAD, 34))
    tf = rc.fit_text(d, title.upper(), "display", 52, w - PAD * 2 - 150, 24)
    rc.shadow_text(d, (PAD + 146, 40), title.upper(), tf, fill=BONE, shadow=(60, 0, 6), offset=3)
    d.text((PAD + 148, 110), subtitle, font=rc.font("semi", 20), fill=MUTED)
    y = 180
    for rarity, items in groups:
        color = RARITY_RGB.get(rarity, MUTED)
        d.text((PAD, y), RARITY_LABELS.get(rarity, rarity).upper() or "OTHER", font=rc.font("display", 24),
               fill=rc.shade(color, 1.5))
        y += 44
        for i, a in enumerate(items):
            r, c = divmod(i, cols)
            x = PAD + c * cw
            ty = y + r * 176
            _paste(canvas, gear_tile(tile, a), (x + (cw - tile) / 2, ty))
            _label(d, x, ty + tile + 8, cw, a["name"], None, 14, lines=2)
        y += ((len(items) + cols - 1) // cols) * 176
    _footer(canvas)
    return rc.to_png(canvas)


# ─────────────────────────────── prestige ─────────────────────────────── #
def render_prestige(name: str, avatar: bytes | None, done: set, killers: list, survivors: list) -> bytes:
    """killers/survivors: [(char_id, label, portrait_bytes)]. done: set of char ids at P100."""
    cols, tw, ph, gap = 8, 128, 128, 10
    th = ph + 40
    sections = [("Killers", killers, "killer"), ("Survivors", survivors, "survivor")]
    body = sum(54 + ((len(chars) + cols - 1) // cols) * (th + gap) + 22 for _, chars, _ in sections)
    w = PAD * 2 + cols * tw + (cols - 1) * gap
    h = 200 + body + 50
    canvas = background(w, h)
    d = ImageDraw.Draw(canvas, "RGBA")

    av = rc.open_image(avatar)
    tx = PAD
    if av is not None:
        av = rc.round_corners(rc.cover_fit(av, 104, 104), 52)
        _paste(canvas, av, (PAD, 30))
        d.ellipse([PAD - 3, 27, PAD + 107, 137], outline=BLOOD, width=4)
        tx = PAD + 128
    total = len(killers) + len(survivors)
    got = len(done & {c[0] for c in killers + survivors})
    tf = rc.fit_text(d, f"{name}'s Prestige 100s", "display", 54, w - tx - PAD, 24)
    rc.shadow_text(d, (tx, 30), f"{name}'s Prestige 100s", tf, fill=BONE, shadow=(60, 0, 6), offset=3)
    d.text((tx + 2, 98), f"{got} of {total} characters maxed · {got / max(total, 1) * 100:.0f}%",
           font=rc.font("semi", 22), fill=MUTED)
    bx0, bx1, by = PAD, w - PAD, 158
    d.rounded_rectangle([bx0, by, bx1, by + 14], 7, fill=(255, 255, 255, 22))
    if got:
        d.rounded_rectangle([bx0, by, bx0 + (bx1 - bx0) * got / max(total, 1), by + 14], 7, fill=BLOOD)

    y = 200
    for title, chars, role in sections:
        color = ROLE_RGB[role]
        n = sum(1 for c in chars if c[0] in done)
        d.text((PAD, y + 6), title.upper(), font=rc.font("display", 30), fill=color)
        d.text((PAD + d.textlength(title.upper(), font=rc.font("display", 30)) + 14, y + 16),
               f"{n}/{len(chars)}", font=rc.font("semi", 18), fill=MUTED)
        y += 54
        for i, (cid, label, portrait) in enumerate(chars):
            r, c = divmod(i, cols)
            x = PAD + c * (tw + gap)
            ty = y + r * (th + gap)
            maxed = cid in done
            _paste(canvas, portrait_tile(tw, ph, portrait, role, label, dim=not maxed), (x, ty))
            if maxed:
                d.rounded_rectangle([x - 2, ty - 2, x + tw + 2, ty + ph + 2], 14, outline=rc.shade(color, 1.7), width=3)
                rc.pill(d, (x + tw - 6, ty + 6), "P100", rc.font("bold", 12), BONE, (70, 8, 14),
                        pad_x=6, pad_y=2, anchor_right=True)
            lines = rc.wrap(d, label, rc.font("bold", 14), tw, max_lines=2)
            ly = ty + ph + 4
            for line in lines:
                d.text((x + tw / 2, ly), line, font=rc.font("bold", 14), fill=rc.WHITE if maxed else DIM, anchor="ma")
                ly += 17
        rows = (len(chars) + cols - 1) // cols
        y += rows * (th + gap) + 22
    _footer(canvas, "Tap “Update my P100s” to tick off maxed characters · self-reported")
    return rc.to_png(canvas)


def badge_png(name: str, role: str, icon: bytes | None = None, size: int = 256) -> bytes:
    return rc.to_png(perk_badge(size, name, role, icon))


def gear_png(gear: dict, size: int = 256) -> bytes:
    return rc.to_png(gear_tile(size, gear))
