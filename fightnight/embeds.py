"""
All Discord embed builders for the UFC cog.
"""
import discord
from typing import Optional

UFC_RED  = 0xD20A0A
UFC_GOLD = 0xC8A951
UFC_BLUE = 0x1A3A6B
GREEN    = 0x00C851

WEIGHT_EMOJIS = {
    "strawweight":          "🍓",
    "flyweight":            "🪰",
    "bantamweight":         "🐓",
    "featherweight":        "🦅",
    "lightweight":          "⚡",
    "welterweight":         "🥊",
    "middleweight":         "🔥",
    "light heavyweight":    "💪",
    "heavyweight":          "🏔️",
}

RESULT_EMOJI = {
    "win":  "✅",
    "loss": "❌",
    "draw": "🟡",
    "nc":   "⬜",
}


def _wemoji(weight_class: str) -> str:
    wc = weight_class.lower()
    for key, emoji in WEIGHT_EMOJIS.items():
        if key in wc:
            return emoji
    return "🥊"


# ── fight card ────────────────────────────────────────────────────────────────

def card_embed(event: dict) -> discord.Embed:
    e = discord.Embed(title=f"🥊  {event['name']}", color=UFC_RED)

    parts = []
    if event.get("date"):
        parts.append(f"📅  {event['date']}")
    if event.get("location"):
        parts.append(f"📍  {event['location']}")
    if event.get("timestamp"):
        parts.append(f"⏰  <t:{event['timestamp']}:R>")
    e.description = "\n".join(parts)

    fights = event.get("fights", [])
    if not fights:
        e.add_field(name="Card", value="No bouts announced yet.", inline=False)
        e.set_footer(text="UFC • via ESPN")
        return e

    main   = [f for f in fights if f.get("is_title")]
    others = [f for f in fights if not f.get("is_title")]

    def fight_line(f) -> str:
        emoji = _wemoji(f.get("weight_class", ""))
        red, blue = f["red"], f["blue"]
        rr, br = f.get("red_record", ""), f.get("blue_record", "")
        title = "🏆 " if f.get("is_title") else ""
        line = f"{title}{emoji}  **{red}** vs **{blue}**"
        if rr or br:
            line += f"\n> `{rr}` vs `{br}`"
        wc = f.get("weight_class", "")
        if wc:
            line += f"\n> *{wc}*"
        return line

    if main:
        e.add_field(
            name="🎯  Main Card",
            value="\n\n".join(fight_line(f) for f in main),
            inline=False,
        )

    if others:
        # split into chunks ≤ 1000 chars
        chunks, cur, cur_len = [], [], 0
        for f in others:
            line = fight_line(f)
            if cur_len + len(line) > 950 and cur:
                chunks.append("\n\n".join(cur))
                cur, cur_len = [], 0
            cur.append(line)
            cur_len += len(line)
        if cur:
            chunks.append("\n\n".join(cur))

        labels = ["📋  Prelims", "📋  Early Prelims"]
        for i, chunk in enumerate(chunks):
            e.add_field(name=labels[i] if i < len(labels) else "📋  Bouts",
                        value=chunk, inline=False)

    e.set_footer(text="UFC • via ESPN")
    return e


# ── results ───────────────────────────────────────────────────────────────────

def results_embed(event: dict) -> discord.Embed:
    e = discord.Embed(
        title=f"📊  Results — {event['name']}",
        color=UFC_GOLD,
        description=f"📅 {event.get('date','')}  •  📍 {event.get('location','')}",
    )

    fights = event.get("fights", [])
    if not fights:
        e.add_field(name="Results", value="No results available yet.", inline=False)
        e.set_footer(text="UFC • via ESPN")
        return e

    for f in fights:
        red, blue = f["red"], f["blue"]
        winner  = f.get("winner", "")
        method  = f.get("method", "")
        rnd     = f.get("round", "")
        time    = f.get("time", "")
        wc      = f.get("weight_class", "")
        emoji   = _wemoji(wc)
        title   = "🏆 " if f.get("is_title") else ""

        if winner:
            loser = blue if winner == red else red
            result_line = f"**{winner}** def. {loser}"
        else:
            result_line = f"**{red}** vs **{blue}**  *(pending)*"

        detail_parts = [p for p in [method, f"R{rnd}" if rnd else "", time] if p]
        detail = "  •  ".join(detail_parts) or "—"

        e.add_field(
            name=f"{title}{emoji}  {wc or 'Bout'}",
            value=f"{result_line}\n`{detail}`",
            inline=False,
        )

    e.set_footer(text="UFC • via ESPN")
    return e


# ── fighter ───────────────────────────────────────────────────────────────────

def fighter_embed(fighter: dict) -> discord.Embed:
    name     = fighter.get("name", "Unknown")
    nickname = fighter.get("nickname", "")
    record   = fighter.get("record", "")
    wc       = fighter.get("weight_class", "")

    title = f"🥊  {name}"
    if nickname:
        title += f'  "{nickname}"'

    e = discord.Embed(title=title, color=UFC_RED)

    desc = []
    if record:
        desc.append(f"**Record:** {record}")
    if wc:
        desc.append(f"**Division:** {_wemoji(wc)} {wc}")
    e.description = "\n".join(desc)

    bio = []
    for label, key in [
        ("📏 Height", "height"),
        ("⚖️ Weight", "weight"),
        ("🎂 Age",    "age"),
        ("🌍 Country","country"),
        ("🏋️ Team",   "gym"),
        ("🏆 Ranking","ranking"),
    ]:
        val = fighter.get(key) or fighter.get("nationality") if key == "country" else fighter.get(key)
        if val:
            bio.append(f"**{label}:** {val}")
    if bio:
        e.add_field(name="Bio", value="\n".join(bio), inline=False)

    # stat categories (ESPN)
    for cat in fighter.get("stat_categories", [])[:2]:
        stats = cat.get("stats", {})
        if stats:
            lines = [f"**{k}:** {v}" for k, v in list(stats.items())[:8]]
            e.add_field(name=f"📈 {cat['name']}", value="\n".join(lines), inline=True)

    # recent fights (Sherdog)
    fights = fighter.get("fights", [])
    if fights:
        lines = []
        for f in fights[:5]:
            res   = f.get("result", "").lower()
            emoji = RESULT_EMOJI.get(res, "❔")
            opp   = f.get("opponent", "?")
            method= f.get("method", "")
            rnd   = f.get("round", "")
            lines.append(f"{emoji} vs **{opp}** — {method} R{rnd}")
        e.add_field(name="🕐 Recent Fights", value="\n".join(lines), inline=False)

    headshot = fighter.get("headshot", "")
    if headshot:
        e.set_thumbnail(url=headshot)

    src = "ESPN + Sherdog" if fighter.get("fights") and fighter.get("source") == "espn" else \
          "Sherdog" if fighter.get("source") == "sherdog" else "ESPN"
    e.set_footer(text=f"UFC • via {src}")
    return e


# ── picks ─────────────────────────────────────────────────────────────────────

def picks_embed(event: dict, picks: dict, guild: discord.Guild) -> discord.Embed:
    """
    picks structure:  { "Fighter A|Fighter B": { "user_id": "Fighter A", ... } }
    We iterate the stored picks dict directly — no dependency on card order.
    """
    e = discord.Embed(
        title=f"🗳️  Server Picks — {event.get('shortname', event.get('name', 'UFC Event'))}",
        color=UFC_BLUE,
        description=f"📅 {event.get('date', 'TBD')}",
    )

    if not picks:
        e.add_field(
            name="No picks yet!",
            value="Use `!ufc pick <fighter name>` to make your pick.",
            inline=False,
        )
        e.set_footer(text="UFC Pick Em • lock in before the event!")
        return e

    # build weight-class lookup from card
    wc_lookup = {}  # fight_key -> weight_class
    for f in event.get("fights", []):
        key = f"{f['red']}|{f['blue']}"
        wc_lookup[key] = f.get("weight_class", "")

    fields_added = 0
    for fight_key, fight_picks in picks.items():
        if not fight_picks:
            continue
        parts = fight_key.split("|", 1)
        if len(parts) != 2:
            continue
        red, blue = parts

        wc    = wc_lookup.get(fight_key, "")
        emoji = _wemoji(wc)

        red_pickers, blue_pickers = [], []
        for uid, pick in fight_picks.items():
            member = guild.get_member(int(uid))
            display = member.display_name if member else f"<@{uid}>"
            if pick == red:
                red_pickers.append(display)
            else:
                blue_pickers.append(display)

        total = len(red_pickers) + len(blue_pickers)
        if total == 0:
            continue

        red_pct  = round(len(red_pickers) / total * 100)
        blue_pct = 100 - red_pct
        red_bars = round(len(red_pickers) / total * 10)
        bar = "🟥" * red_bars + "🟦" * (10 - red_bars)

        value = (
            f"{bar}\n"
            f"🟥 **{red}** ({red_pct}%) — {', '.join(red_pickers) or '—'}\n"
            f"🟦 **{blue}** ({blue_pct}%) — {', '.join(blue_pickers) or '—'}"
        )
        e.add_field(name=f"{emoji}  {red} vs {blue}", value=value, inline=False)
        fields_added += 1

    if fields_added == 0:
        e.add_field(
            name="No picks yet!",
            value="Use `!ufc pick <fighter name>` to make your pick.",
            inline=False,
        )

    e.set_footer(text="UFC Pick Em • Use !ufc pick <fighter> to join")
    return e


# ── standings ─────────────────────────────────────────────────────────────────

def standings_embed(standings: dict, guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(title="🏆  UFC Pick Em Standings", color=UFC_GOLD)

    if not standings:
        e.description = "No history yet — make some picks!"
        return e

    ranked = sorted(
        standings.items(),
        key=lambda x: (x[1].get("correct", 0), -x[1].get("total", 999)),
        reverse=True,
    )

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, stats) in enumerate(ranked[:15]):
        member  = guild.get_member(int(uid))
        display = member.display_name if member else f"<@{uid}>"
        correct = stats.get("correct", 0)
        total   = stats.get("total", 0)
        pct     = round(correct / total * 100) if total else 0
        medal   = medals[i] if i < 3 else f"`{i+1}.`"
        lines.append(f"{medal} **{display}** — {correct}/{total} ({pct}%)")

    e.description = "\n".join(lines)
    e.set_footer(text="UFC Pick Em • updates after each event is settled")
    return e


# ── pick confirm ──────────────────────────────────────────────────────────────

def pick_confirm_embed(member: discord.Member, picked: str,
                       opponent: str, event_name: str) -> discord.Embed:
    return discord.Embed(
        title="✅  Pick Locked In!",
        description=(
            f"{member.mention} picked **{picked}** over **{opponent}**\n"
            f"*{event_name}*"
        ),
        color=GREEN,
    )


# ── error ─────────────────────────────────────────────────────────────────────

def error_embed(msg: str) -> discord.Embed:
    return discord.Embed(title="❌  Error", description=msg, color=0xFF4444)
