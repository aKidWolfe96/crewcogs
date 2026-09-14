from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
from statistics import mean
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


NON_STARTER_SLOTS = {"BN", "IR", "RESERVE", "TAXI"}
FLEX_ELIGIBILITY = {
    "FLEX": {"RB", "WR", "TE"},
    "REC_FLEX": {"WR", "TE"},
    "WRRB_FLEX": {"WR", "RB"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "OP": {"QB", "RB", "WR", "TE"},
    "IDP_FLEX": {"DL", "LB", "DB", "DE", "DT", "CB", "S"},
}


def starter_slots(roster_positions: Sequence[str]) -> List[str]:
    return [str(s).upper() for s in roster_positions if str(s).upper() not in NON_STARTER_SLOTS]


def player_positions(player: Mapping[str, Any]) -> set[str]:
    positions = set()
    pos = player.get("position")
    if pos:
        positions.add(str(pos).upper())
    for p in player.get("fantasy_positions") or []:
        positions.add(str(p).upper())
    return positions


def is_eligible(slot: str, player: Mapping[str, Any], player_id: str = "") -> bool:
    slot = slot.upper()
    positions = player_positions(player)
    if slot in FLEX_ELIGIBILITY:
        return bool(positions & FLEX_ELIGIBILITY[slot])
    if slot == "DEF":
        return "DEF" in positions or (player_id.isalpha() and len(player_id) <= 4)
    return slot in positions


def optimal_lineup(
    roster_positions: Sequence[str],
    roster_player_ids: Sequence[str],
    player_points: Mapping[str, float],
    players: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Find the maximum legal lineup using DP over the small fantasy roster."""
    slots = starter_slots(roster_positions)
    ids = [str(pid) for pid in roster_player_ids if pid]
    # Order constrained slots first to reduce state branching.
    slot_candidates: List[Tuple[str, Tuple[int, ...]]] = []
    for slot in slots:
        eligible = tuple(
            i for i, pid in enumerate(ids) if is_eligible(slot, players.get(pid, {}), pid)
        )
        slot_candidates.append((slot, eligible))
    slot_candidates.sort(key=lambda x: len(x[1]))

    @lru_cache(maxsize=None)
    def solve(idx: int, used_mask: int) -> Tuple[float, Tuple[Tuple[str, int], ...]]:
        if idx >= len(slot_candidates):
            return 0.0, ()
        slot, candidates = slot_candidates[idx]
        available = [i for i in candidates if not (used_mask & (1 << i))]
        if not available:
            score, lineup = solve(idx + 1, used_mask)
            return score, ((slot, -1),) + lineup
        best_score = float("-inf")
        best_lineup: Tuple[Tuple[str, int], ...] = ()
        for i in available:
            pid = ids[i]
            pts = float(player_points.get(pid, 0.0) or 0.0)
            rest_score, rest_lineup = solve(idx + 1, used_mask | (1 << i))
            total = pts + rest_score
            if total > best_score:
                best_score = total
                best_lineup = ((slot, i),) + rest_lineup
        return best_score, best_lineup

    score, lineup_idx = solve(0, 0)
    lineup = []
    for slot, idx in lineup_idx:
        pid = ids[idx] if idx >= 0 else None
        lineup.append({"slot": slot, "player_id": pid, "points": float(player_points.get(pid, 0.0) or 0.0) if pid else 0.0})
    return {"points": round(max(0.0, score), 2), "lineup": lineup}


def pair_matchups(matchups: Iterable[Mapping[str, Any]]) -> List[List[Mapping[str, Any]]]:
    grouped: Dict[Any, List[Mapping[str, Any]]] = defaultdict(list)
    byes: List[List[Mapping[str, Any]]] = []
    for row in matchups:
        mid = row.get("matchup_id")
        if mid is None:
            byes.append([row])
        else:
            grouped[mid].append(row)
    return list(grouped.values()) + byes


def all_play_for_week(scores: Mapping[str, float]) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    items = list(scores.items())
    for rid, score in items:
        wins = losses = ties = 0
        for other, other_score in items:
            if other == rid:
                continue
            if score > other_score:
                wins += 1
            elif score < other_score:
                losses += 1
            else:
                ties += 1
        out[str(rid)] = {"wins": wins, "losses": losses, "ties": ties, "pct": (wins + ties * 0.5) / max(1, len(items) - 1)}
    return out


def matchup_results(matchups: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for group in pair_matchups(matchups):
        if len(group) != 2:
            for row in group:
                result[str(row["roster_id"])] = {"result": "bye", "margin": 0.0, "opponent": None}
            continue
        a, b = group
        a_score = float(a.get("points") or 0.0)
        b_score = float(b.get("points") or 0.0)
        for row, score, opp, opp_score in ((a, a_score, b, b_score), (b, b_score, a, a_score)):
            if score > opp_score:
                res = "win"
            elif score < opp_score:
                res = "loss"
            else:
                res = "tie"
            result[str(row["roster_id"])] = {
                "result": res,
                "margin": round(score - opp_score, 2),
                "opponent": str(opp["roster_id"]),
            }
    return result


def luck_for_week(scores: Mapping[str, float], matchups: Sequence[Mapping[str, Any]]) -> Dict[str, float]:
    """Luck = actual result points minus all-play expected win percentage, scaled to 100."""
    all_play = all_play_for_week(scores)
    results = matchup_results(matchups)
    out: Dict[str, float] = {}
    for rid in scores:
        actual = results.get(str(rid), {}).get("result")
        actual_value = 1.0 if actual == "win" else 0.5 if actual == "tie" else 0.0
        expected = all_play.get(str(rid), {}).get("pct", 0.0)
        out[str(rid)] = round((actual_value - expected) * 100.0, 1)
    return out


def power_rankings(
    season_scores: Mapping[str, Sequence[float]],
    all_play_totals: Mapping[str, Mapping[str, float]],
    margins: Mapping[str, Sequence[float]],
) -> Dict[str, float]:
    """Transparent 0-100 score: 80% all-play dominance, 15% scoring, 5% margin."""
    roster_ids = list(season_scores)
    if not roster_ids:
        return {}
    avg_scores = {rid: mean(vals) if vals else 0.0 for rid, vals in season_scores.items()}
    avg_margins = {rid: mean(vals) if vals else 0.0 for rid, vals in margins.items()}

    def minmax(values: Mapping[str, float]) -> Dict[str, float]:
        if not values:
            return {}
        lo, hi = min(values.values()), max(values.values())
        if abs(hi - lo) < 1e-9:
            return {k: 0.5 for k in values}
        return {k: (v - lo) / (hi - lo) for k, v in values.items()}

    scoring_norm = minmax(avg_scores)
    margin_norm = minmax(avg_margins)
    dominance: Dict[str, float] = {}
    for rid in roster_ids:
        rec = all_play_totals.get(rid, {})
        w = float(rec.get("wins", 0))
        l = float(rec.get("losses", 0))
        t = float(rec.get("ties", 0))
        games = w + l + t
        dominance[rid] = (w + 0.5 * t) / games if games else 0.5
    scores = {
        rid: round(100 * (0.80 * dominance.get(rid, 0.5) + 0.15 * scoring_norm.get(rid, 0.5) + 0.05 * margin_norm.get(rid, 0.5)), 1)
        for rid in roster_ids
    }
    return scores


def accumulate_all_play(history: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, int]]:
    totals: Dict[str, Dict[str, int]] = defaultdict(lambda: {"wins": 0, "losses": 0, "ties": 0})
    for week in history.values():
        for rid, rec in (week.get("all_play") or {}).items():
            totals[str(rid)]["wins"] += int(rec.get("wins", 0))
            totals[str(rid)]["losses"] += int(rec.get("losses", 0))
            totals[str(rid)]["ties"] += int(rec.get("ties", 0))
    return dict(totals)


def season_luck(history: Mapping[str, Mapping[str, Any]]) -> Dict[str, float]:
    totals: Dict[str, float] = defaultdict(float)
    for week in history.values():
        for rid, val in (week.get("luck") or {}).items():
            totals[str(rid)] += float(val)
    return {rid: round(v, 1) for rid, v in totals.items()}


def season_management(history: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, float]]:
    data: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: {"eff": [], "left": []})
    for week in history.values():
        for rid, eff in (week.get("efficiency") or {}).items():
            data[str(rid)]["eff"].append(float(eff))
        for rid, left in (week.get("points_left") or {}).items():
            data[str(rid)]["left"].append(float(left))
    out = {}
    for rid, vals in data.items():
        out[rid] = {
            "avg_efficiency": round(mean(vals["eff"]) if vals["eff"] else 0.0, 1),
            "points_left": round(sum(vals["left"]), 2),
        }
    return out


def projected_points_for_player(row: Mapping[str, Any], scoring_settings: Mapping[str, float]) -> float:
    """Score raw projection fields using league scoring when possible; otherwise use canned totals."""
    total = 0.0
    matched = 0
    for stat, weight in scoring_settings.items():
        if stat in row and isinstance(row.get(stat), (int, float)):
            total += float(row[stat]) * float(weight)
            matched += 1
    if matched:
        return round(total, 2)
    rec = float(scoring_settings.get("rec", 0) or 0)
    field = "pts_ppr" if rec >= 0.75 else "pts_half_ppr" if rec >= 0.25 else "pts_std"
    val = row.get(field)
    return round(float(val or 0.0), 2)


def team_projection(starters: Sequence[str], projections: Mapping[str, Mapping[str, Any]], scoring_settings: Mapping[str, float]) -> float:
    return round(sum(projected_points_for_player(projections.get(str(pid), {}), scoring_settings) for pid in starters if pid), 2)


def weekly_trophies(
    scores: Mapping[str, float],
    matchups: Sequence[Mapping[str, Any]],
    luck: Mapping[str, float],
    efficiency: Mapping[str, float],
    points_left: Mapping[str, float],
    projected: Optional[Mapping[str, float]] = None,
) -> Dict[str, str]:
    if not scores:
        return {}
    trophies: Dict[str, str] = {}
    trophies["king"] = max(scores, key=scores.get)
    trophies["bum"] = min(scores, key=scores.get)
    results = matchup_results(matchups)
    winners = [(rid, rec["margin"]) for rid, rec in results.items() if rec.get("result") == "win"]
    if winners:
        trophies["murder"] = max(winners, key=lambda x: x[1])[0]
        trophies["survivor"] = min(winners, key=lambda x: x[1])[0]
    if luck:
        trophies["lucky"] = max(luck, key=luck.get)
        trophies["robbed"] = min(luck, key=luck.get)
    if efficiency:
        trophies["best_manager"] = max(efficiency, key=efficiency.get)
        trophies["bench_coach"] = max(points_left, key=points_left.get) if points_left else min(efficiency, key=efficiency.get)
    if projected:
        delta = {rid: float(scores.get(rid, 0.0)) - float(projected.get(rid, 0.0)) for rid in scores}
        trophies["overachiever"] = max(delta, key=delta.get)
        trophies["underachiever"] = min(delta, key=delta.get)
    else:
        # Keep ten awards even if the unofficial projection feed is unavailable.
        avg = mean(scores.values())
        delta = {rid: s - avg for rid, s in scores.items()}
        trophies["overachiever"] = max(delta, key=delta.get)
        trophies["underachiever"] = min(delta, key=delta.get)
    return trophies
