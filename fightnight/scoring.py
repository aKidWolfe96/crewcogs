"""Pure pick'em / betting maths for the UFC cog (no Discord, no network).

Points per fight:
  • 1 point   correct winner
  • +1 bonus  correct method (KO/TKO, Submission, Decision)
  • +1 bonus  correct round (finishes only, method must also be right)

Bets pay at the moneyline locked in when the bet was placed:
  +250 underdog: 100 stake returns 350 · -300 favourite: 100 stake returns 133.
Bets placed before odds existed (or old bets) pay even money (2x).
"""

from __future__ import annotations

from typing import Optional

METHOD_LABELS = {"ko": "KO/TKO", "sub": "Submission", "dec": "Decision"}


def norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def fight_key(fight: dict) -> str:
    return f"{fight['red']}|{fight['blue']}"


def same_fight(key: str, fight: dict) -> bool:
    """Does a stored 'Red|Blue' key belong to this fight (either corner order)?"""
    a, _, b = key.partition("|")
    names = {norm(a), norm(b)}
    return names == {norm(fight["red"]), norm(fight["blue"])}


def payout(amount: int, moneyline: Optional[int]) -> int:
    """Total returned for a winning bet (stake included)."""
    if not moneyline:
        return amount * 2
    if moneyline > 0:
        return amount + (amount * moneyline) // 100
    return amount + round(amount * 100 / abs(moneyline))


def fmt_odds(ml: Optional[int]) -> str:
    if ml is None:
        return "—"
    return f"+{ml}" if ml > 0 else str(ml)


def implied_label(ml: Optional[int]) -> str:
    if ml is None:
        return ""
    return "underdog" if ml > 0 else "favourite"


def method_choice_label(value: str) -> str:
    """'ko:2' -> 'KO/TKO · Round 2', 'dec' -> 'Decision'."""
    method, _, rnd = value.partition(":")
    label = METHOD_LABELS.get(method, method)
    return f"{label} · Round {rnd}" if rnd else label


def method_choices(rounds: int) -> list[str]:
    out = []
    for m in ("ko", "sub"):
        out += [f"{m}:{r}" for r in range(1, rounds + 1)]
    out.append("dec")
    return out


def blank_score() -> dict:
    return {"correct": 0, "total": 0, "bonus": 0, "net": 0, "won": 0, "lost": 0}


def settle_fight(bucket: dict, fight: dict) -> Optional[dict]:
    """Score one finished fight against a guild's event bucket, in place.

    Removes that fight's picks/bets/method picks from the bucket, adds the key
    to bucket['settled'], and accumulates bucket['scores'].
    Returns a summary dict, or None if nothing in the bucket referenced it.
    """
    if not fight.get("completed"):
        return None
    winner = fight.get("winner") or ""
    method = fight.get("method_key") or ""
    rnd = str(fight.get("round") or "")
    void = not winner  # draw / no contest / overturned

    picks = bucket.setdefault("picks", {})
    bets = bucket.setdefault("bets", {})
    methods = bucket.setdefault("methods", {})
    scores = bucket.setdefault("scores", {})
    odds = (bucket.get("odds") or {})

    keys = {k for k in list(picks) + list(bets) + list(methods) if same_fight(k, fight)}
    summary = {"fight": fight, "void": void, "pickers": [], "bets": [], "refunds": [],
               "called": 0, "total": 0, "upsets": [], "deltas": {}}
    deltas = summary["deltas"]
    if not keys:
        bucket.setdefault("settled", []).append(fight_key(fight))
        return summary

    for key in keys:
        for uid, picked in (picks.pop(key, {}) or {}).items():
            mp = (methods.get(key) or {}).get(uid)
            if void:
                continue
            s = deltas.setdefault(uid, blank_score())
            s["total"] += 1
            summary["total"] += 1
            correct = norm(picked) == norm(winner)
            bonus = 0
            if correct:
                s["correct"] += 1
                summary["called"] += 1
                if mp and method and mp.get("method") == method:
                    bonus += 1
                    if method in ("ko", "sub") and str(mp.get("round") or "") == rnd:
                        bonus += 1
                s["bonus"] += bonus
                ml = (odds.get(key) or {}).get(norm(picked))
                if ml and ml > 0:
                    summary["upsets"].append((uid, picked, ml))
                    best = s.get("best_upset") or {}
                    if ml > (best.get("ml") or 0):
                        s["best_upset"] = {"ml": ml, "fighter": picked}
            summary["pickers"].append((uid, picked, correct, bonus))
        methods.pop(key, None)

        for uid, bet in (bets.pop(key, {}) or {}).items():
            amount = int(bet.get("amount", 0) or 0)
            if amount <= 0:
                continue
            if void:
                summary["refunds"].append((uid, amount))
                continue
            s = deltas.setdefault(uid, blank_score())
            won = norm(bet.get("pick", "")) == norm(winner)
            paid = payout(amount, bet.get("odds")) if won else 0
            s["net"] += (paid - amount)
            s["won" if won else "lost"] += 1
            summary["bets"].append((uid, bet.get("pick", ""), amount, paid, won, bet.get("odds")))

    for uid, d in deltas.items():
        sc = scores.setdefault(uid, blank_score())
        for k in ("correct", "total", "bonus", "net", "won", "lost"):
            sc[k] = sc.get(k, 0) + d.get(k, 0)
        up = d.get("best_upset")
        if up and up["ml"] > (sc.get("best_upset") or {}).get("ml", 0):
            sc["best_upset"] = up
    bucket.setdefault("settled", []).append(fight_key(fight))
    return summary


def merge_into(standings: dict, scores: dict, keys=("correct", "total", "bonus")):
    """Add per-card scores into a standings table {uid: {...}}."""
    for uid, sc in scores.items():
        t = standings.setdefault(uid, {"correct": 0, "total": 0, "bonus": 0})
        for k in keys:
            t[k] = t.get(k, 0) + sc.get(k, 0)
        up = sc.get("best_upset")
        if up and up.get("ml", 0) > (t.get("best_upset") or {}).get("ml", 0):
            t["best_upset"] = up


def points(row: dict) -> int:
    return row.get("correct", 0) + row.get("bonus", 0)
