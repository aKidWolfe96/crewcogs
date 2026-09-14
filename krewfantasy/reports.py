from __future__ import annotations

import random
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import discord

from .analytics import pair_matchups

TROPHY_META = {
    "king": ("👑", "King of the Week"),
    "bum": ("💩", "Certified Bum"),
    "murder": ("💥", "Murder Scene"),
    "survivor": ("😅", "Barely Survived"),
    "lucky": ("🍀", "Lucky Bastard"),
    "robbed": ("😡", "Robbed"),
    "overachiever": ("📈", "Overachiever"),
    "underachiever": ("📉", "Underachiever"),
    "best_manager": ("🤖", "Best Manager"),
    "bench_coach": ("🤡", "Bench Coach"),
}

ROASTS = {
    "mild": [
        "Could have gone better.",
        "That one is going to sting a little.",
        "The fantasy gods noticed that decision.",
        "There is always next week.",
    ],
    "ruthless": [
        "Absolutely disgusting roster management.",
        "A masterclass in making the wrong choice.",
        "The bench outperformed the decision-making.",
        "Somebody check if the manager was actually awake.",
        "An elite performance in self-sabotage.",
    ],
}


def money(v: Any) -> str:
    try:
        return f"${int(v)}"
    except Exception:
        return "$0"


def fmt_points(v: Any) -> str:
    try:
        return f"{float(v):.2f}"
    except Exception:
        return "0.00"


def team_name(roster_id: Any, roster_names: Mapping[str, str]) -> str:
    return roster_names.get(str(roster_id), f"Roster {roster_id}")


def maybe_mention(roster_id: Any, links: Mapping[str, int], roster_names: Mapping[str, str]) -> str:
    did = links.get(str(roster_id))
    return f"<@{did}>" if did else team_name(roster_id, roster_names)


def base_embed(title: str, description: str = "", color: Optional[discord.Color] = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color or discord.Color.blurple())


def scoreboard_embed(
    week: int,
    matchups: Sequence[Mapping[str, Any]],
    names: Mapping[str, str],
    projections: Optional[Mapping[str, float]] = None,
    title_prefix: str = "KREW FANTASY",
) -> discord.Embed:
    e = base_embed(f"🏈 {title_prefix} — WEEK {week}")
    groups = pair_matchups(matchups)
    for group in groups:
        if len(group) == 1:
            row = group[0]
            rid = str(row["roster_id"])
            e.add_field(name=f"{team_name(rid, names)} — BYE", value=fmt_points(row.get("points")), inline=False)
            continue
        a, b = group[:2]
        arid, brid = str(a["roster_id"]), str(b["roster_id"])
        av, bv = float(a.get("points") or 0), float(b.get("points") or 0)
        leader_a = "▶ " if av > bv else ""
        leader_b = "▶ " if bv > av else ""
        ap = f"  (proj {fmt_points(projections.get(arid))})" if projections and arid in projections else ""
        bp = f"  (proj {fmt_points(projections.get(brid))})" if projections and brid in projections else ""
        value = f"{leader_a}**{team_name(arid, names)}** — {fmt_points(av)}{ap}\n{leader_b}**{team_name(brid, names)}** — {fmt_points(bv)}{bp}"
        e.add_field(name=f"Matchup {a.get('matchup_id', '—')}", value=value, inline=False)
    return e


def standings_embed(rosters: Sequence[Mapping[str, Any]], names: Mapping[str, str]) -> discord.Embed:
    rows = []
    for r in rosters:
        s = r.get("settings") or {}
        wins = int(s.get("wins") or 0)
        losses = int(s.get("losses") or 0)
        ties = int(s.get("ties") or 0)
        pf = float(s.get("fpts") or 0) + float(s.get("fpts_decimal") or 0) / 100
        rows.append((wins, -losses, pf, str(r["roster_id"]), ties))
    rows.sort(reverse=True)
    lines = []
    for i, (wins, neg_losses, pf, rid, ties) in enumerate(rows, 1):
        losses = -neg_losses
        rec = f"{wins}-{losses}" + (f"-{ties}" if ties else "")
        lines.append(f"**{i}. {team_name(rid, names)}** — {rec}  •  PF {pf:.2f}")
    return base_embed("📊 Krew Fantasy Standings", "\n".join(lines) or "No standings available.")


def rankings_embed(title: str, values: Mapping[str, float], names: Mapping[str, str], *, suffix: str = "") -> discord.Embed:
    ordered = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
    lines = [f"**{i}. {team_name(rid, names)}** — {val:.1f}{suffix}" for i, (rid, val) in enumerate(ordered, 1)]
    return base_embed(title, "\n".join(lines) or "No data yet.")


def all_play_embed(totals: Mapping[str, Mapping[str, int]], names: Mapping[str, str]) -> discord.Embed:
    def pct(rec: Mapping[str, int]) -> float:
        g = rec.get("wins", 0) + rec.get("losses", 0) + rec.get("ties", 0)
        return (rec.get("wins", 0) + 0.5 * rec.get("ties", 0)) / g if g else 0.0
    ordered = sorted(totals.items(), key=lambda kv: pct(kv[1]), reverse=True)
    lines = []
    for i, (rid, r) in enumerate(ordered, 1):
        ties = int(r.get("ties", 0))
        rec = f"{r.get('wins', 0)}-{r.get('losses', 0)}" + (f"-{ties}" if ties else "")
        lines.append(f"**{i}. {team_name(rid, names)}** — {rec}")
    return base_embed("⚔️ Krew Win Matrix (All-Play)", "\n".join(lines) or "No completed weeks archived yet.")


def management_embed(data: Mapping[str, Mapping[str, float]], names: Mapping[str, str]) -> discord.Embed:
    ordered = sorted(data.items(), key=lambda kv: kv[1].get("avg_efficiency", 0), reverse=True)
    lines = []
    for i, (rid, row) in enumerate(ordered, 1):
        lines.append(
            f"**{i}. {team_name(rid, names)}** — {row.get('avg_efficiency', 0):.1f}% efficient • {row.get('points_left', 0):.2f} pts left"
        )
    return base_embed("🤖 Manager Efficiency", "\n".join(lines) or "No completed weeks archived yet.")


def trophies_embed(
    week: int,
    trophies: Mapping[str, str],
    names: Mapping[str, str],
    detail: Optional[Mapping[str, Mapping[str, float]]] = None,
    roast_mode: str = "mild",
) -> discord.Embed:
    e = base_embed(f"🏆 Week {week} Awards")
    for key, rid in trophies.items():
        emoji, label = TROPHY_META.get(key, ("🏅", key.replace("_", " ").title()))
        extra = ""
        d = (detail or {}).get(key) or {}
        if "value" in d:
            extra = f"\n{d['value']}"
        e.add_field(name=f"{emoji} {label}", value=f"**{team_name(rid, names)}**{extra}", inline=True)
    if roast_mode in ROASTS and trophies.get("bench_coach"):
        e.set_footer(text=random.choice(ROASTS[roast_mode]))
    return e


def trophy_case_embed(case: Mapping[str, Mapping[str, int]], names: Mapping[str, str]) -> discord.Embed:
    e = base_embed("🏆 Season Trophy Case")
    if not case:
        e.description = "No trophies archived yet."
        return e
    for rid, awards in sorted(case.items(), key=lambda kv: sum(kv[1].values()), reverse=True):
        lines = []
        for key, count in sorted(awards.items(), key=lambda kv: kv[1], reverse=True):
            emoji, label = TROPHY_META.get(key, ("🏅", key.replace("_", " ").title()))
            lines.append(f"{emoji} {label} ×{count}")
        e.add_field(name=team_name(rid, names), value="\n".join(lines) or "—", inline=True)
    return e


def transaction_embed(
    tx: Mapping[str, Any],
    names: Mapping[str, str],
    players: Mapping[str, Mapping[str, Any]],
    links: Mapping[str, int],
    roast_mode: str = "mild",
) -> discord.Embed:
    tx_type = str(tx.get("type") or "transaction")
    title = "🤝 TRADE ALERT" if tx_type == "trade" else "💰 WAIVER WIRE" if tx_type == "waiver" else "🔄 ROSTER MOVE"
    e = base_embed(title)
    adds = tx.get("adds") or {}
    drops = tx.get("drops") or {}
    picks = tx.get("draft_picks") or []
    faab = tx.get("waiver_budget") or []
    involved = [str(r) for r in (tx.get("roster_ids") or [])]
    if not involved:
        involved = sorted(set([str(v) for v in adds.values()] + [str(v) for v in drops.values()]))
    for rid in involved:
        lines = []
        for pid, target in adds.items():
            if str(target) == rid:
                lines.append(f"➕ {player_label(str(pid), players)}")
        for pid, source in drops.items():
            if str(source) == rid:
                lines.append(f"➖ {player_label(str(pid), players)}")
        for p in picks:
            if str(p.get("owner_id")) == rid:
                lines.append(f"🎟️ Receives {p.get('season')} Round {p.get('round')} pick")
        for move in faab:
            if str(move.get("receiver")) == rid:
                lines.append(f"💵 Receives {money(move.get('amount'))} FAAB")
            if str(move.get("sender")) == rid:
                lines.append(f"💸 Sends {money(move.get('amount'))} FAAB")
        settings = tx.get("settings") or {}
        if tx_type == "waiver" and str((tx.get("roster_ids") or [None])[0]) == rid and settings.get("waiver_bid") is not None:
            lines.append(f"💰 Bid: {money(settings.get('waiver_bid'))}")
        if lines:
            e.add_field(name=maybe_mention(rid, links, names), value="\n".join(lines), inline=False)
    if roast_mode in ROASTS:
        e.set_footer(text=random.choice(ROASTS[roast_mode]))
    return e


def player_label(pid: str, players: Mapping[str, Mapping[str, Any]]) -> str:
    p = players.get(str(pid), {})
    full = p.get("full_name") or " ".join(filter(None, [p.get("first_name"), p.get("last_name")])).strip()
    if not full:
        if str(pid).isalpha() and len(str(pid)) <= 4:
            full = f"{str(pid).upper()} D/ST"
        else:
            full = p.get("search_full_name") or str(pid)
    pos = p.get("position") or ""
    team = p.get("team") or "FA"
    return f"{full} — {pos} {team}".strip()


def lineup_alert_embed(
    week: int,
    alerts: Mapping[str, Sequence[str]],
    names: Mapping[str, str],
    links: Mapping[str, int],
) -> discord.Embed:
    e = base_embed(f"🚨 Week {week} Lineup Check")
    if not alerts:
        e.description = "No obvious starting-lineup problems found."
        return e
    for rid, rows in alerts.items():
        e.add_field(name=maybe_mention(rid, links, names), value="\n".join(rows) or "✅ No obvious issues", inline=False)
    return e


def recap_embed(week: int, text: str) -> discord.Embed:
    return base_embed(f"📰 Krew Fantasy Week {week} Recap", text)


def playoff_embed(bracket: Sequence[Mapping[str, Any]], names: Mapping[str, str], title: str = "🏆 Playoff Bracket") -> discord.Embed:
    e = base_embed(title)
    if not bracket:
        e.description = "Bracket is not available yet."
        return e
    rounds: Dict[int, List[str]] = defaultdict(list)
    for m in bracket:
        t1 = m.get("t1")
        t2 = m.get("t2")
        def label(v: Any, src: Any) -> str:
            if v is not None:
                return team_name(v, names)
            if isinstance(src, dict):
                if "w" in src:
                    return f"Winner of Match {src['w']}"
                if "l" in src:
                    return f"Loser of Match {src['l']}"
            return "TBD"
        a = label(t1, m.get("t1_from"))
        b = label(t2, m.get("t2_from"))
        winner = team_name(m.get("w"), names) if m.get("w") is not None else "TBD"
        rounds[int(m.get("r") or 0)].append(f"**M{m.get('m')}**: {a} vs {b}\nWinner: {winner}")
    for r in sorted(rounds):
        e.add_field(name=f"Round {r}", value="\n\n".join(rounds[r]), inline=False)
    return e
