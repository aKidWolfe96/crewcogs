"""
Discord embed builders for the UFC cog.
"""
import discord

UFC_RED  = 0xD20A0A
UFC_GOLD = 0xC8A951
UFC_BLUE = 0x1A3A6B
GREEN    = 0x00C851
GREY     = 0x888888

WEIGHT_EMOJIS = {
    "strawweight":       "🍓",
    "flyweight":         "🪰",
    "bantamweight":      "🐓",
    "featherweight":     "🦅",
    "lightweight":       "⚡",
    "welterweight":      "🥊",
    "middleweight":      "🔥",
    "light heavyweight": "💪",
    "heavyweight":       "🏔️",
}
RESULT_EMOJI = {"win": "✅", "loss": "❌", "draw": "🟡", "nc": "⬜"}


def _wemoji(weight_class: str) -> str:
    wc = (weight_class or "").lower()
    for key, emoji in WEIGHT_EMOJIS.items():
        if key in wc:
            return emoji
    return "🥊"


def _clean(val) -> str:
    s = str(val).strip()
    return "" if s.lower() in ("", "0", "none", "n/a") else s


# ── card ──────────────────────────────────────────────────────────────────────

def card_embed(event: dict) -> discord.Embed:
    e = discord.Embed(title=f"🥊  {event['name']}", color=UFC_RED)

    parts = []
    if event.get("date"):
        parts.append(f"📅  {event['date']}")
    if event.get("location"):
        parts.append(f"📍  {event['location']}")
    if event.get("timestamp"):
        parts.append(f"⏰  <t:{event['timestamp']}:R>")
    e.description = "\n".join(parts) or "Details TBD"

    fights = event.get("fights", [])
    if not fights:
        e.add_field(name="Card", value="No bouts announced yet.", inline=False)
        e.set_footer(text="UFC • via ESPN")
        return e

    def line(f):
        emoji = _wemoji(f.get("weight_class", ""))
        title = "🏆 " if f.get("is_title") else ""
        s = f"{title}{emoji}  **{f['red']}** vs **{f['blue']}**"
        rr, br = f.get("red_record", ""), f.get("blue_record", "")
        if rr or br:
            s += f"\n> `{rr or '—'}` vs `{br or '—'}`"
        if f.get("weight_class"):
            s += f"\n> *{f['weight_class']}*"
        return s

    main   = [f for f in fights if f.get("is_title")]
    others = [f for f in fights if not f.get("is_title")]

    if main:
        e.add_field(name="🎯  Main Card",
                    value="\n\n".join(line(f) for f in main), inline=False)

    if others:
        chunks, cur, ln = [], [], 0
        for f in others:
            t = line(f)
            if ln + len(t) > 950 and cur:
                chunks.append("\n\n".join(cur)); cur, ln = [], 0
            cur.append(t); ln += len(t)
        if cur:
            chunks.append("\n\n".join(cur))
        labels = ["📋  Prelims", "📋  Early Prelims", "📋  More Bouts"]
        for i, ch in enumerate(chunks):
            e.add_field(name=labels[i] if i < len(labels) else "📋  Bouts",
                        value=ch, inline=False)

    e.set_footer(text="UFC • via ESPN")
    return e


# ── results ─────────────────────────────────────────────────────────────────-

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
        winner = f.get("winner", "")
        emoji  = _wemoji(f.get("weight_class", ""))
        title  = "🏆 " if f.get("is_title") else ""

        if winner:
            loser = blue if winner == red else red
            head = f"**{winner}** def. {loser}"
        else:
            head = f"**{red}** vs **{blue}**  *(pending)*"

        detail = "  •  ".join(p for p in [
            f.get("method", ""),
            f"R{f['round']}" if f.get("round") else "",
            f.get("time", ""),
        ] if p) or "—"

        e.add_field(name=f"{title}{emoji}  {f.get('weight_class') or 'Bout'}",
                    value=f"{head}\n`{detail}`", inline=False)

    e.set_footer(text="UFC • via ESPN")
    return e


# ── fighter ─────────────────────────────────────────────────────────────────-

def fighter_embed(fighter: dict) -> discord.Embed:
    name     = fighter.get("name", "Unknown")
    nickname = _clean(fighter.get("nickname", ""))
    record   = _clean(fighter.get("record", ""))
    wc       = _clean(fighter.get("weight_class", ""))

    title = f"🥊  {name}" + (f'  "{nickname}"' if nickname else "")
    e = discord.Embed(title=title, color=UFC_RED)

    desc = []
    if record:
        desc.append(f"**Record:** {record}")
    if wc:
        desc.append(f"**Division:** {_wemoji(wc)} {wc}")
    e.description = "\n".join(desc) or "Profile"

    bio = []
    for label, key in [("📏 Height", "height"), ("⚖️ Weight", "weight"),
                       ("🎂 Age", "age"), ("🌍 Country", "country"),
                       ("🏋️ Team", "gym"), ("🏆 Rank", "ranking")]:
        v = _clean(fighter.get(key, "")) or (
            _clean(fighter.get("nationality", "")) if key == "country" else ""
        )
        if v:
            bio.append(f"**{label}:** {v}")
    if bio:
        e.add_field(name="Bio", value="\n".join(bio), inline=False)

    for cat in fighter.get("stat_categories", [])[:2]:
        stats = cat.get("stats", {})
        if stats:
            e.add_field(name=f"📈 {cat['name']}",
                        value="\n".join(f"**{k}:** {v}" for k, v in list(stats.items())[:8]),
                        inline=True)

    fights = fighter.get("fights", [])
    if fights:
        lines = []
        for f in fights[:5]:
            emoji  = RESULT_EMOJI.get(f.get("result", "").lower(), "❔")
            opp    = f.get("opponent", "?")
            method = f.get("method", "")
            rnd    = f.get("round", "")
            tail   = f" — {method}" + (f" R{rnd}" if rnd else "")
            lines.append(f"{emoji} vs **{opp}**{tail}")
        e.add_field(name="🕐 Recent Fights", value="\n".join(lines), inline=False)

    if fighter.get("headshot"):
        e.set_thumbnail(url=fighter["headshot"])

    src = {
        "espn+sherdog": "ESPN + Sherdog",
        "sherdog": "Sherdog",
        "espn": "ESPN",
    }.get(fighter.get("source", ""), "ESPN")
    e.set_footer(text=f"UFC • via {src}")
    return e


# ── picks ─────────────────────────────────────────────────────────────────────

def picks_embed(event: dict, picks: dict, guild: discord.Guild) -> discord.Embed:
    e = discord.Embed(
        title=f"🗳️  Server Picks — {event.get('shortname', event.get('name','UFC Event'))}",
        color=UFC_BLUE,
        description=f"📅 {event.get('date','TBD')}",
    )

    # weight-class lookup from the current card (cosmetic only)
    wc_lookup = {f"{f['red']}|{f['blue']}": f.get("weight_class", "")
                 for f in event.get("fights", [])}

    added = 0
    for fight_key, fight_picks in (picks or {}).items():
        if not fight_picks:
            continue
        parts = fight_key.split("|", 1)
        if len(parts) != 2:
            continue
        red, blue = parts
        emoji = _wemoji(wc_lookup.get(fight_key, ""))

        red_p, blue_p = [], []
        for uid, pick in fight_picks.items():
            m = guild.get_member(int(uid))
            disp = m.display_name if m else f"<@{uid}>"
            (red_p if pick == red else blue_p).append(disp)

        total = len(red_p) + len(blue_p)
        if total == 0:
            continue
        red_pct  = round(len(red_p) / total * 100)
        red_bars = round(len(red_p) / total * 10)
        bar = "🟥" * red_bars + "🟦" * (10 - red_bars)

        e.add_field(
            name=f"{emoji}  {red} vs {blue}",
            value=(f"{bar}\n"
                   f"🟥 **{red}** ({red_pct}%) — {', '.join(red_p) or '—'}\n"
                   f"🟦 **{blue}** ({100-red_pct}%) — {', '.join(blue_p) or '—'}"),
            inline=False,
        )
        added += 1

    if added == 0:
        e.add_field(name="No picks yet!",
                    value="Use `!ufc pick <fighter name>` to make your pick.",
                    inline=False)

    e.set_footer(text="UFC Pick Em • Use !ufc pick <fighter> to join")
    return e


# ── standings ─────────────────────────────────────────────────────────────────

def standings_embed(standings: dict, guild: discord.Guild,
                    pending: int = 0, betting: dict = None) -> discord.Embed:
    betting = betting or {}
    e = discord.Embed(title="🏆  UFC Pick Em Standings", color=UFC_GOLD)

    if not standings:
        msg = "No settled results yet."
        if pending:
            msg += (f"\n\nThere are **{pending}** pick(s) locked in for the next event. "
                    "Standings fill in once an admin runs `!ufc settle` after the event.")
        else:
            msg += "\n\nMake picks with `!ufc pick <fighter>`, then settle after the event."
        e.description = msg
        return e

    ranked = sorted(standings.items(),
                    key=lambda x: (x[1].get("correct", 0), -x[1].get("total", 999)),
                    reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, st) in enumerate(ranked[:15]):
        m = guild.get_member(int(uid))
        disp = m.display_name if m else f"<@{uid}>"
        c, t = st.get("correct", 0), st.get("total", 0)
        pct = round(c / t * 100) if t else 0
        medal = medals[i] if i < 3 else f"`{i+1}.`"

        row = f"{medal} **{disp}** — {c}/{t} ({pct}%)"

        # betting column — only for users who have actually bet
        b = betting.get(uid)
        if b and (b.get("won", 0) or b.get("lost", 0)):
            net = b.get("net", 0)
            sign = "+" if net >= 0 else "−"
            money_emoji = "💰" if net >= 0 else "📉"
            row += f"  •  {money_emoji} {sign}{abs(net):,} ({b.get('won',0)}W-{b.get('lost',0)}L)"

        lines.append(row)
    e.description = "\n".join(lines)

    if pending:
        e.set_footer(text=f"UFC Pick Em • {pending} pick(s) pending for the next event")
    else:
        e.set_footer(text="UFC Pick Em • updates after each settle")
    return e


# ── betting embeds ────────────────────────────────────────────────────────────

def bet_confirm_embed(member, fighter, opponent, amount, event_name,
                      currency="credits", slots_left=0, changed_from=None) -> discord.Embed:
    if changed_from is not None:
        title = "🔁  Bet Updated!"
        desc = (f"{member.mention} now has **{amount:,} {currency}** on **{fighter}** "
                f"(was {changed_from:,})\nto beat **{opponent}** — *{event_name}*")
    else:
        title = "💰  Bet Placed!"
        desc = (f"{member.mention} bet **{amount:,} {currency}** on **{fighter}** "
                f"to beat **{opponent}**\n*{event_name}*\n\n"
                f"Win pays **{amount*2:,} {currency}** • {slots_left} bet slot(s) left")
    return discord.Embed(title=title, description=desc, color=GREEN)


def unbet_embed(member, fighter, amount, currency="credits", slots_left=0) -> discord.Embed:
    return discord.Embed(
        title="↩️  Bet Cancelled",
        description=(f"{member.mention} cancelled their bet on **{fighter}** and was "
                     f"refunded **{amount:,} {currency}**.\n"
                     f"You now have {slots_left} bet slot(s) free."),
        color=UFC_BLUE,
    )


def bets_embed(member, bets: list, currency="credits", max_bets=3) -> discord.Embed:
    """bets: list of {fighter, opponent, amount}"""
    e = discord.Embed(title=f"💰  {member.display_name}'s Active Bets", color=UFC_GOLD)
    if not bets:
        e.description = ("No active bets.\nUse `!ufc bet <fighter> <amount>` to place one "
                         f"(max {max_bets} per card).")
        return e
    lines, total = [], 0
    for b in bets:
        total += b["amount"]
        lines.append(f"🥊 **{b['amount']:,}** on **{b['fighter']}** vs {b['opponent']}")
    e.description = "\n".join(lines)
    e.add_field(name="Total staked", value=f"{total:,} {currency}", inline=True)
    e.add_field(name="Slots used", value=f"{len(bets)}/{max_bets}", inline=True)
    e.set_footer(text="Win pays 2× your stake • !ufc unbet <fighter> to cancel")
    return e


# ── pick confirm / error ──────────────────────────────────────────────────────

def pick_confirm_embed(member, picked, opponent, event_name) -> discord.Embed:
    return discord.Embed(
        title="✅  Pick Locked In!",
        description=f"{member.mention} picked **{picked}** over **{opponent}**\n*{event_name}*",
        color=GREEN,
    )


def settle_embed(event_name: str, lines: list) -> discord.Embed:
    return discord.Embed(title=f"📊  Picks Settled — {event_name}",
                         description="\n".join(lines), color=UFC_GOLD)


def error_embed(msg: str) -> discord.Embed:
    return discord.Embed(title="❌  Error", description=msg, color=0xFF4444)
