"""Image cards for the Dead by Daylight cog.

Everything is drawn here (no game art is downloaded): foggy blood-red
backgrounds, diamond perk badges tinted by role/type, and a Prestige 100 board.
All functions are pure and CPU-only; run them in an executor.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFilter

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
KIND_RGB = {
    "hex": (142, 52, 170),
    "boon": (48, 162, 118),
    "scourge": (210, 96, 32),
    "invocation": (120, 120, 140),
}
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
    # faint claw scratches top-right
    sd = ImageDraw.Draw(bg, "RGBA")
    for i in range(3):
        x = w - 150 + i * 26
        sd.line([(x, 18), (x - 60, 120)], fill=(220, 40, 50, 46), width=5)
    return bg  # RGB so translucent shapes drawn later blend properly


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


def perk_badge(size: int, name: str, role: str) -> Image.Image:
    """A diamond perk badge (transparent background)."""
    s = size
    color = perk_color(name, role)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    def diamond(inset):
        c = s / 2
        r = s / 2 - inset
        return [(c, c - r), (c + r, c), (c, c + r), (c - r, c)]

    # glow
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(glow).polygon(diamond(s * 0.06), fill=color + (150,))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(s * 0.05)))

    # frame: bright → dark diagonal gradient
    frame = rc.gradient((s, s), rc.shade(color, 1.55), rc.shade(color, 0.55)).convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).polygon(diamond(s * 0.08), fill=255)
    img.paste(frame, (0, 0), mask)

    # inner well
    inner = rc.gradient((s, s), rc.shade(color, 0.42), rc.shade(color, 0.16)).convert("RGBA")
    mask2 = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask2).polygon(diamond(s * 0.15), fill=255)
    img.paste(inner, (0, 0), mask2)
    d = ImageDraw.Draw(img)
    d.polygon(diamond(s * 0.15), outline=rc.shade(color, 1.8) + (200,), width=max(1, s // 90))

    # sheen
    sheen = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(sheen).polygon([(s / 2, s * 0.15), (s * 0.85, s / 2), (s / 2, s * 0.5), (s * 0.15, s / 2)],
                                  fill=(255, 255, 255, 26))
    sheen_mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(sheen_mask).polygon(diamond(s * 0.15), fill=255)
    img.paste(Image.alpha_composite(img.copy(), sheen), (0, 0), sheen_mask)

    # glyph
    d = ImageDraw.Draw(img)
    text = initials(name)
    f = rc.font("display", int(s * (0.30 if len(text) > 1 else 0.36)))
    d.text((s / 2 + s * 0.012, s / 2 + s * 0.02), text, font=f, fill=(0, 0, 0, 170), anchor="mm")
    d.text((s / 2, s / 2), text, font=f, fill=BONE, anchor="mm")
    return img


def _title(canvas, text, sub=None, y=28):
    d = ImageDraw.Draw(canvas)
    f = rc.fit_text(d, text.upper(), "display", 60, canvas.width - PAD * 2, 28)
    rc.shadow_text(d, (PAD, y), text.upper(), f, fill=BONE, shadow=(60, 0, 6), offset=3)
    if sub:
        d.text((PAD + 2, y + 72), sub, font=rc.font("semi", 22), fill=MUTED)
    d.rectangle([PAD, y + (108 if sub else 80), PAD + 90, y + (112 if sub else 84)], fill=BLOOD)


def _footer(canvas, text="Perk data via dbd.tricky.lol · not affiliated with Behaviour Interactive"):
    d = ImageDraw.Draw(canvas)
    d.text((PAD, canvas.height - 32), text, font=rc.font("semi", 14), fill=DIM)


def _perk_column(canvas, x, y, w, perk, badge_size, extra_lines=()):
    """Badge centred in a column with the perk name and owner under it."""
    badge = perk_badge(badge_size, perk["name"], perk["role"])
    canvas.paste(badge, mask=badge, box=(int(x + (w - badge_size) / 2), y))
    d = ImageDraw.Draw(canvas)
    ny = y + badge_size + 8
    for line in rc.wrap(d, perk["name"], rc.font("bold", 21), w - 10, max_lines=2):
        d.text((x + w / 2, ny), line, font=rc.font("bold", 21), fill=rc.WHITE, anchor="ma")
        ny += 26
    owner = perk.get("owner") or "General perk"
    d.text((x + w / 2, ny + 2), rc.ellipsize(d, owner, rc.font("semi", 16), w - 10),
           font=rc.font("semi", 16), fill=MUTED, anchor="ma")
    ny += 26
    for text, color in extra_lines:
        d.text((x + w / 2, ny), text, font=rc.font("bold", 17), fill=color, anchor="ma")
        ny += 24
    return ny


# ─────────────────────────────── shrine ─────────────────────────────── #
def render_shrine(perks: list[dict], refresh_label: str) -> bytes:
    """perks: name, role, owner, shards, bloodpoints."""
    w, h = 1200, 560
    canvas = background(w, h)
    _title(canvas, "Shrine of Secrets", f"This week's perks · refreshes {refresh_label}")
    col = (w - PAD * 2) / max(len(perks), 1)
    for i, p in enumerate(perks):
        extra = []
        if p.get("shards"):
            extra.append((f"{p['shards']:,} shards", SHARD))
        if p.get("bloodpoints"):
            extra.append((f"or {p['bloodpoints']:,} BP if owned", (230, 190, 120)))
        role_tag = p["role"].upper()
        d = ImageDraw.Draw(canvas)
        x = PAD + i * col
        d.text((x + col / 2, 168), role_tag, font=rc.font("semi", 14), fill=ROLE_RGB.get(p["role"], MUTED), anchor="ma")
        _perk_column(canvas, x, 192, col, p, 190, extra)
    _footer(canvas)
    return rc.to_png(canvas)


# ─────────────────────────────── builds ─────────────────────────────── #
def render_build(character: str, role: str, perks: list[dict], who: str | None = None, note: str | None = None,
                 tag: str | None = None) -> bytes:
    w, h = 1200, 540
    canvas = background(w, h)
    d = ImageDraw.Draw(canvas)
    tag = tag or ("KILLER BUILD" if role == "killer" else "SURVIVOR BUILD")
    d.text((PAD + 2, 30), tag + (f" · FOR {who.upper()}" if who else ""), font=rc.font("semi", 18),
           fill=ROLE_RGB.get(role, MUTED))
    f = rc.fit_text(d, character.upper(), "display", 64, w - PAD * 2, 28)
    rc.shadow_text(d, (PAD, 54), character.upper(), f, fill=BONE, shadow=(60, 0, 6), offset=3)
    if note:
        d.text((PAD + 2, 132), note, font=rc.font("semi", 20), fill=MUTED)
    d.rectangle([PAD, 170, PAD + 90, 174], fill=BLOOD)
    col = (w - PAD * 2) / max(len(perks), 1) if len(perks) > 2 else (w - PAD * 2) / 4
    x0 = PAD + ((w - PAD * 2) - col * len(perks)) / 2
    for i, p in enumerate(perks):
        _perk_column(canvas, x0 + i * col, 196, col, p, 180)
    _footer(canvas)
    return rc.to_png(canvas)


def render_squad(rows: list[dict], title: str) -> bytes:
    """rows: player, role, character, perks [perk dicts]."""
    row_h = 168
    w = 1200
    h = 150 + len(rows) * row_h + 50
    canvas = background(w, h)
    _title(canvas, title, f"{len(rows)} player{'s' if len(rows) != 1 else ''} · good luck in the fog")
    d = ImageDraw.Draw(canvas, "RGBA")
    y = 150
    for r in rows:
        color = ROLE_RGB.get(r["role"], MUTED)
        d.rounded_rectangle([PAD, y, w - PAD, y + row_h - 14], 16, fill=(255, 255, 255, 12), outline=(255, 255, 255, 26), width=2)
        d.rounded_rectangle([PAD, y, PAD + 8, y + row_h - 14], 4, fill=color)
        nf = rc.fit_text(d, r["player"], "bold", 26, 300, 14)
        d.text((PAD + 26, y + 26), r["player"], font=nf, fill=rc.WHITE)
        cf = rc.fit_text(d, r["character"].upper(), "display", 30, 300, 14)
        d.text((PAD + 26, y + 64), r["character"].upper(), font=cf, fill=BONE)
        d.text((PAD + 26, y + 108), r["role"].upper(), font=rc.font("semi", 14), fill=color)
        px = PAD + 350
        cw = (w - PAD - 20 - px) / 4
        for i, p in enumerate(r["perks"]):
            bx = int(px + i * cw)
            badge = perk_badge(96, p["name"], p["role"])
            canvas.paste(badge, mask=badge, box=(int(bx + (cw - 96) / 2), y + 8))
            label = rc.ellipsize(d, p["name"], rc.font("semi", 15), cw - 8)
            d.text((bx + cw / 2, y + 112), label, font=rc.font("semi", 15), fill=rc.WHITE, anchor="ma")
        y += row_h
    _footer(canvas)
    return rc.to_png(canvas)


# ─────────────────────────────── prestige ─────────────────────────────── #
def render_prestige(name: str, avatar: bytes | None, done: set, killers: list, survivors: list) -> bytes:
    """killers/survivors: [(char_id, label)]. done: set of char ids at P100."""
    cols, tw, th, gap = 7, 148, 74, 10
    sections = [("Killers", killers, ROLE_RGB["killer"]), ("Survivors", survivors, ROLE_RGB["survivor"])]
    body = sum(54 + ((len(chars) + cols - 1) // cols) * (th + gap) + 22 for _, chars, _ in sections)
    w = PAD * 2 + cols * tw + (cols - 1) * gap
    h = 200 + body + 50
    canvas = background(w, h)
    d = ImageDraw.Draw(canvas, "RGBA")

    av = rc.open_image(avatar)
    tx = PAD
    if av is not None:
        av = rc.round_corners(rc.cover_fit(av, 104, 104), 52)
        canvas.paste(av, mask=av, box=(PAD, 30))
        d.ellipse([PAD - 3, 27, PAD + 107, 137], outline=BLOOD, width=4)
        tx = PAD + 128
    total = len(killers) + len(survivors)
    got = len(done & {c for c, _ in killers + survivors})
    tf = rc.fit_text(d, f"{name}'s Prestige 100s", "display", 54, w - tx - PAD, 24)
    rc.shadow_text(d, (tx, 30), f"{name}'s Prestige 100s", tf, fill=BONE, shadow=(60, 0, 6), offset=3)
    d.text((tx + 2, 98), f"{got} of {total} characters maxed · {got / max(total, 1) * 100:.0f}%",
           font=rc.font("semi", 22), fill=MUTED)
    bx0, bx1, by = PAD, w - PAD, 158
    d.rounded_rectangle([bx0, by, bx1, by + 14], 7, fill=(255, 255, 255, 22))
    if got:
        d.rounded_rectangle([bx0, by, bx0 + (bx1 - bx0) * got / max(total, 1), by + 14], 7, fill=BLOOD)

    y = 200
    for title, chars, color in sections:
        n = sum(1 for c, _ in chars if c in done)
        d.text((PAD, y + 6), title.upper(), font=rc.font("display", 30), fill=color)
        d.text((PAD + d.textlength(title.upper(), font=rc.font("display", 30)) + 14, y + 16),
               f"{n}/{len(chars)}", font=rc.font("semi", 18), fill=MUTED)
        y += 54
        for i, (cid, label) in enumerate(chars):
            r, c = divmod(i, cols)
            x = PAD + c * (tw + gap)
            ty = y + r * (th + gap)
            maxed = cid in done
            if maxed:
                tile = rc.gradient((tw, th), rc.shade(color, 0.95), rc.shade(color, 0.35)).convert("RGBA")
                tile = rc.round_corners(tile, 12)
                canvas.paste(tile, (x, ty), tile)
                d.rounded_rectangle([x, ty, x + tw, ty + th], 12, outline=rc.shade(color, 1.7), width=2)
                rc.pill(d, (x + tw - 8, ty + 7), "P100", rc.font("bold", 12), BONE, (70, 8, 14),
                        pad_x=6, pad_y=2, anchor_right=True)
                fill = rc.WHITE
            else:
                d.rounded_rectangle([x, ty, x + tw, ty + th], 12, fill=(255, 255, 255, 10), outline=(255, 255, 255, 22), width=1)
                fill = DIM
            lines = rc.wrap(d, label, rc.font("bold", 17), tw - 16, max_lines=2)
            ly = ty + th - 12 - len(lines) * 21
            for line in lines:
                d.text((x + 10, ly), line, font=rc.font("bold", 17), fill=fill)
                ly += 21
        rows = (len(chars) + cols - 1) // cols
        y += rows * (th + gap) + 22
    _footer(canvas, "Tap “Update my P100s” to tick off maxed characters · self-reported")
    return rc.to_png(canvas)


def badge_png(name: str, role: str, size: int = 256) -> bytes:
    return rc.to_png(perk_badge(size, name, role))
