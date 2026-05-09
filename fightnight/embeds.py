"""
Embed builders for the UFC cog.
All Discord embed formatting lives here.
"""
import discord
from datetime import datetime
from typing import Optional

# UFC brand colors
UFC_RED = 0xD20A0A
UFC_GOLD = 0xC8A951
UFC_DARK = 0x1A1A1A
UFC_BLUE = 0x003087

WEIGHT_CLASS_EMOJI = {
    "strawweight": "🍓",
    "flyweight": "🪰",
    "bantamweight": "🐓",
    "featherweight": "🦅",
    "lightweight": "⚡",
    "welterweight": "🥊",
    "middleweight": "🔥",
    "light heavyweight": "💪",
    "heavyweight": "🏔️",
    "women's strawweight": "🍓",
    "women's flyweight": "🪰",
    "women's bantamweight": "🐓",
    "women's featherweight": "🦅",
}

RESULT_EMOJI = {"win": "✅", "loss": "❌", "draw": "🟡", "nc": "⬜"}


def _weight_emoji(weight_class: str) -> str:
    wc = weight_class.lower()
    for key, emoji in WEIGHT_CLASS_EMOJI.items():
        if key in wc:
            return emoji
    return "🥊"


def _title_indicator(fight: dict) -> str:
    if fight.get("is_title"):
        return "🏆 **TITLE FIGHT** 🏆\n"
    return ""


# ─── Fight Card Embed ──────────────────────────────────────────────────────────

def build_card_embed(event: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"🥊 {event['name']}",
        color=UFC_RED,
    )

    date = event.get("date", "TBD")
    location = event.get("location", "TBD")
    timestamp = event.get("timestamp")

    header_parts = [f"📅 {date}"]
    if location:
        header_parts.append(f"📍 {location}")
    if timestamp:
        header_parts.append(f"⏰ <t:{timestamp}:R>")

    embed.description = "\n".join(header_parts)

    fights = event.get("fights", [])
    if not fights:
        embed.add_field(name="Card", value="No bouts announced yet.", inline=False)
        embed.set_footer(text="UFC • Data via ESPN")
        return embed

    # Main event first, then rest
    main_fights = [f for f in fights if f.get("is_main_event") or f.get("is_title")]
    other_fights = [f for f in fights if f not in main_fights]

    def fight_line(f):
        wc = f.get("weight_class", "")
        emoji = _weight_emoji(wc)
        red = f.get("red_name", "TBD")
        blue = f.get("blue_name", "TBD")
        red_rec = f.get("red_record", "")
        blue_rec = f.get("blue_record", "")
        title = "🏆 " if f.get("is_title") else ""
        rec_line = ""
        if red_rec or blue_rec:
            rec_line = f"\n> `{red_rec}` vs `{blue_rec}`"
        return f"{title}{emoji} **{red}** vs **{blue}**{rec_line}\n> *{wc}*"

    if main_fights:
        main_text = "\n\n".join(fight_line(f) for f in main_fights)
        embed.add_field(name="🎯 Main Card", value=main_text, inline=False)

    if other_fights:
        chunks = []
        current = []
        current_len = 0
        for f in other_fights:
            line = fight_line(f)
            if current_len + len(line) > 900 and current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += len(line)
        if current:
            chunks.append("\n\n".join(current))

        for i, chunk in enumerate(chunks):
            label = "Prelims" if i == 0 else "Early Prelims"
            embed.add_field(name=f"📋 {label}", value=chunk, inline=False)

    embed.set_footer(text="UFC • Data via ESPN • All times UTC")
    return embed


# ─── Results Embed ─────────────────────────────────────────────────────────────

def build_results_embed(event: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"📊 Results: {event['name']}",
        color=UFC_GOLD,
        description=f"📅 {event.get('date', 'TBD')} • 📍 {event.get('location', 'TBD')}",
    )

    fights = event.get("fights", [])
    if not fights:
        embed.add_field(name="Results", value="No results available.", inline=False)
        embed.set_footer(text="UFC • Data via ESPN")
        return embed

    for f in fights:
        result = f.get("result") or {}
        red = f.get("red_name", "TBD")
        blue = f.get("blue_name", "TBD")
        wc = f.get("weight_class", "")
        emoji = _weight_emoji(wc)
        title_tag = "🏆 " if f.get("is_title") else ""

        winner = result.get("winner", "")
        method = result.get("method", "")
        rnd = result.get("round", "")
        time = result.get("time", "")

        if winner:
            loser = blue if winner == red else red
            result_line = f"**{winner}** def. {loser}"
        else:
            result_line = f"**{red}** vs **{blue}**"

        detail_parts = []
        if method:
            detail_parts.append(method)
        if rnd:
            detail_parts.append(f"R{rnd}")
        if time:
            detail_parts.append(time)
        detail = " • ".join(detail_parts) if detail_parts else "Result pending"

        field_name = f"{title_tag}{emoji} {wc}" if wc else f"{title_tag}🥊 Bout"
        embed.add_field(
            name=field_name,
            value=f"{result_line}\n`{detail}`",
            inline=False,
        )

    embed.set_footer(text="UFC • Data via ESPN")
    return embed


# ─── Fighter Stats Embed ───────────────────────────────────────────────────────

def build_fighter_embed(fighter: dict) -> discord.Embed:
    name = fighter.get("name", "Unknown")
    nickname = fighter.get("nickname", "")
    record = fighter.get("record", "")
    weight_class = fighter.get("weight_class", "")
    headshot = fighter.get("headshot", "")

    title = f"🥊 {name}"
    if nickname:
        title += f' "{nickname}"'

    embed = discord.Embed(title=title, color=UFC_RED)

    desc_parts = []
    if record:
        desc_parts.append(f"**Record:** {record}")
    if weight_class:
        wc_emoji = _weight_emoji(weight_class)
        desc_parts.append(f"**Division:** {wc_emoji} {weight_class}")

    embed.description = "\n".join(desc_parts)

    # Bio
    bio_lines = []
    if fighter.get("height"):
        bio_lines.append(f"📏 **Height:** {fighter['height']}")
    if fighter.get("weight"):
        bio_lines.append(f"⚖️ **Weight:** {fighter['weight']}")
    if fighter.get("age"):
        bio_lines.append(f"🎂 **Age:** {fighter['age']}")
    if fighter.get("country") or fighter.get("nationality"):
        bio_lines.append(f"🌍 **Country:** {fighter.get('country') or fighter.get('nationality')}")
    if fighter.get("gym") or fighter.get("association"):
        bio_lines.append(f"🏋️ **Team:** {fighter.get('gym') or fighter.get('association')}")
    if fighter.get("ranking"):
        bio_lines.append(f"🏆 **Ranking:** #{fighter['ranking']}")

    if bio_lines:
        embed.add_field(name="Bio", value="\n".join(bio_lines), inline=False)

    # Stats categories (ESPN)
    for cat in fighter.get("stat_categories", [])[:3]:
        cat_name = cat.get("name", "Stats")
        stats = cat.get("stats", {})
        if stats:
            stat_lines = [f"**{k}:** {v}" for k, v in list(stats.items())[:8]]
            if stat_lines:
                embed.add_field(name=f"📈 {cat_name}", value="\n".join(stat_lines), inline=True)

    # Recent fights (Sherdog fallback)
    fights = fighter.get("fights", [])
    if fights:
        fight_lines = []
        for f in fights[:5]:
            res = f.get("result", "").upper()
            res_emoji = RESULT_EMOJI.get(res.lower(), "❔")
            opp = f.get("opponent", "?")
            method = f.get("method", "")
            rnd = f.get("round", "")
            fight_lines.append(f"{res_emoji} vs **{opp}** — {method} R{rnd}")
        embed.add_field(name="🕐 Recent Fights", value="\n".join(fight_lines), inline=False)

    if headshot:
        embed.set_thumbnail(url=headshot)

    source = "Sherdog" if fighter.get("source") == "sherdog" else "ESPN"
    embed.set_footer(text=f"UFC • Data via {source}")
    return embed


# ─── Picks Embed ──────────────────────────────────────────────────────────────

def build_picks_embed(event: dict, picks: dict, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title=f"🗳️ Server Picks: {event.get('short_name', event.get('name', 'UFC Event'))}",
        color=UFC_BLUE,
        description=f"📅 {event.get('date', 'TBD')}",
    )

    if not picks:
        embed.add_field(name="No picks yet!", value="Use `!ufc pick <fighter>` to make your picks.", inline=False)
        embed.set_footer(text="UFC Picks • Lock in before the event!")
        return embed

    # Build a lookup from the current card for weight class enrichment
    fights = event.get("fights", [])
    card_fight_meta = {}  # fight_key -> fight dict
    for fight in fights:
        red = fight.get("red_name", "")
        blue = fight.get("blue_name", "")
        if red and blue:
            card_fight_meta[f"{red}|{blue}"] = fight

    # Render every stored pick group — fall back to raw key if not on current card
    fields_added = 0
    for fight_key, fight_picks in picks.items():
        if not fight_picks:
            continue

        parts = fight_key.split("|", 1)
        if len(parts) != 2:
            continue
        red, blue = parts[0], parts[1]

        fight_meta = card_fight_meta.get(fight_key, {})
        wc = fight_meta.get("weight_class", "")
        emoji = _weight_emoji(wc)

        red_pickers = []
        blue_pickers = []
        for user_id, pick in fight_picks.items():
            member = guild.get_member(int(user_id))
            display = member.display_name if member else f"User {user_id}"
            if pick == red:
                red_pickers.append(display)
            else:
                blue_pickers.append(display)

        red_count = len(red_pickers)
        blue_count = len(blue_pickers)
        total = red_count + blue_count
        if total == 0:
            continue

        red_pct = int((red_count / total) * 100)
        blue_pct = 100 - red_pct

        bar_filled = 10
        red_bars = round((red_count / total) * bar_filled)
        blue_bars = bar_filled - red_bars
        bar = "🟥" * red_bars + "🟦" * blue_bars

        red_names = ", ".join(red_pickers) if red_pickers else "—"
        blue_names = ", ".join(blue_pickers) if blue_pickers else "—"

        value = (
            f"{bar}\n"
            f"🟥 **{red}** ({red_pct}%) — {red_names}\n"
            f"🟦 **{blue}** ({blue_pct}%) — {blue_names}"
        )
        embed.add_field(
            name=f"{emoji} {red} vs {blue}",
            value=value,
            inline=False,
        )
        fields_added += 1

    if fields_added == 0:
        embed.add_field(
            name="No picks yet!",
            value="Use `!ufc pick <fighter>` to make your picks.",
            inline=False,
        )

    embed.set_footer(text="UFC Picks • Use !ufc pick <fighter> to join in")
    return embed


# ─── Standings Embed ──────────────────────────────────────────────────────────

def build_standings_embed(standings: dict, guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🏆 UFC Pick 'Em Standings",
        color=UFC_GOLD,
    )

    if not standings:
        embed.description = "No pick history yet. Make some picks and see results!"
        return embed

    sorted_users = sorted(
        standings.items(),
        key=lambda x: (x[1].get("correct", 0), -x[1].get("total", 1)),
        reverse=True,
    )

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (user_id, stats) in enumerate(sorted_users[:15]):
        member = guild.get_member(int(user_id))
        display = member.display_name if member else f"User {user_id}"
        correct = stats.get("correct", 0)
        total = stats.get("total", 0)
        pct = int((correct / total) * 100) if total > 0 else 0
        medal = medals[i] if i < 3 else f"`{i+1}.`"
        lines.append(f"{medal} **{display}** — {correct}/{total} ({pct}%)")

    embed.description = "\n".join(lines)
    embed.set_footer(text="UFC Pick 'Em • Standings update after each event")
    return embed


# ─── Pick Confirmation Embed ──────────────────────────────────────────────────

def build_pick_confirm_embed(user: discord.Member, fighter: str, event_name: str, opponent: str) -> discord.Embed:
    embed = discord.Embed(
        title="✅ Pick Locked In!",
        color=0x00C851,
        description=(
            f"{user.mention} picked **{fighter}** to beat **{opponent}**\n"
            f"*{event_name}*"
        ),
    )
    embed.set_footer(text="Good luck! Use !ufc picks to see everyone's picks.")
    return embed


# ─── Error Embed ──────────────────────────────────────────────────────────────

def build_error_embed(message: str) -> discord.Embed:
    return discord.Embed(
        title="❌ Error",
        description=message,
        color=0xFF4444,
    )
