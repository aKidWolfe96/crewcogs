"""Heal-everything planner (pure — no Discord / Red imports, unit-testable).

Given a list of Pokémon, the trainer's healing items and the shop prices, work
out the cheapest sensible way to revive fainted Pokémon and top everyone up,
using items already in the bag first and (optionally) buying whatever is
missing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

POTIONS: Dict[str, Optional[int]] = {"potion": 20, "superpotion": 50, "maxpotion": None}  # None = full heal
HEAL_ITEM_IDS = ("potion", "superpotion", "maxpotion", "revive")

MODES = ("all", "revive", "potion")


@dataclass
class MonPlan:
    uid: str
    name: str
    hp_before: int
    max_hp: int
    hp_after: int
    revived: bool = False
    items: List[str] = field(default_factory=list)   # every item applied, in order
    unresolved: Optional[str] = None                 # why it couldn't be fully healed

    @property
    def changed(self) -> bool:
        return self.hp_after != self.hp_before


@dataclass
class HealPlan:
    mode: str
    mons: List[MonPlan]
    use: Dict[str, int]    # taken from the bag
    buy: Dict[str, int]    # bought (and consumed) on the spot
    cost: int

    @property
    def targets(self) -> int:
        return len(self.mons)

    @property
    def healed(self) -> List[MonPlan]:
        return [m for m in self.mons if m.changed]

    @property
    def complete(self) -> bool:
        return all(m.unresolved is None for m in self.mons)

    @property
    def does_anything(self) -> bool:
        return any(m.changed for m in self.mons)


def _heal_amount(item: str, missing: int) -> int:
    amt = POTIONS[item]
    return missing if amt is None else min(amt, missing)


def _pick_owned(missing: int, inv: Dict[str, int], prices: Dict[str, int]) -> Optional[str]:
    """Best potion already in the bag for this much missing HP (least waste per coin)."""
    best: Optional[Tuple[float, int, str]] = None
    for item in POTIONS:
        if inv.get(item, 0) <= 0:
            continue
        healed = _heal_amount(item, missing)
        if healed <= 0:
            continue
        score = (prices.get(item, 1) / healed, -healed)
        if best is None or score < best[:2]:
            best = (score[0], score[1], item)
    return best[2] if best else None


def cheapest_purchase(missing: int, prices: Dict[str, int]) -> Dict[str, int]:
    """Cheapest mix of potions that restores `missing` HP."""
    if missing <= 0:
        return {}
    pp, ps, px = prices.get("potion", 100), prices.get("superpotion", 200), prices.get("maxpotion", 500)
    best_cost, best = px, {"maxpotion": 1}
    for supers in range(0, math.ceil(missing / 50) + 1):
        rest = max(0, missing - 50 * supers)
        pots = math.ceil(rest / 20)
        cost = supers * ps + pots * pp
        if cost < best_cost or (cost == best_cost and supers + pots < sum(best.values())):
            best_cost = cost
            best = {k: v for k, v in (("superpotion", supers), ("potion", pots)) if v}
    return best


def plan_heal(
    mons: Iterable[dict],
    inventory: Dict[str, int],
    prices: Dict[str, int],
    *,
    mode: str = "all",
    buy: bool = False,
    only_item: Optional[str] = None,
) -> HealPlan:
    """Plan healing for `mons`.

    mode = "all"    → revive fainted Pokémon, then restore everyone to full HP
           "revive" → only revive fainted Pokémon (to ½ HP)
           "potion" → only top up Pokémon that are hurt but not fainted
    buy  = False    → use only what is in the bag
           True     → buy whatever is missing so every target is fully handled
    only_item       → restrict potions to a single item type (e.g. "superpotion")
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if only_item is not None and only_item not in POTIONS:
        raise ValueError("only_item must be a potion id")
    inv = {k: max(0, int(inventory.get(k, 0) or 0)) for k in HEAL_ITEM_IDS}
    if only_item is not None:
        for k in POTIONS:
            if k != only_item:
                inv[k] = 0
    use: Dict[str, int] = {}
    bought: Dict[str, int] = {}

    def take(item: str) -> None:
        inv[item] -= 1
        use[item] = use.get(item, 0) + 1

    def purchase(item: str, qty: int = 1) -> None:
        bought[item] = bought.get(item, 0) + qty

    plans: List[MonPlan] = []
    for mon in mons:
        hp, mx = int(mon["stats"]["hp"]), int(mon["stats"]["maxHp"])
        fainted = hp <= 0
        hurt = 0 < hp < mx
        wanted = (
            (mode == "all" and (fainted or hurt))
            or (mode == "revive" and fainted)
            or (mode == "potion" and hurt)
        )
        if not wanted:
            continue
        name = mon.get("nickname") or mon.get("displayName") or mon.get("name", "?")
        plans.append(MonPlan(uid=mon.get("uid", ""), name=name, hp_before=hp, max_hp=mx, hp_after=hp))

    # ── 1. Revives ────────────────────────────────────────────────────────────
    if mode in ("all", "revive"):
        for p in plans:
            if p.hp_after > 0:
                continue
            if inv["revive"] > 0:
                take("revive")
            elif buy:
                purchase("revive")
            else:
                p.unresolved = "needs a Revive"
                continue
            p.revived = True
            p.items.append("revive")
            p.hp_after = max(1, p.max_hp // 2)

    # ── 2. Potions ────────────────────────────────────────────────────────────
    #    a) Max Potions go to the biggest wounds, where they're worth the most.
    #    b) Everything else is spread smallest-wound-first, so a short bag fully
    #       heals as many Pokémon as possible instead of half-healing one.
    if mode in ("all", "potion"):
        def apply(p: MonPlan, item: str) -> None:
            take(item)
            healed = _heal_amount(item, p.max_hp - p.hp_after)
            p.items.append(item)
            p.hp_after += healed

        alive = [p for p in plans if p.hp_after > 0]  # still-fainted ones can't take potions
        for p in sorted(alive, key=lambda x: x.max_hp - x.hp_after, reverse=True):
            missing = p.max_hp - p.hp_after
            if missing > 0 and inv["maxpotion"] > 0 and _pick_owned(missing, inv, prices) == "maxpotion":
                apply(p, "maxpotion")

        for p in sorted(alive, key=lambda x: x.max_hp - x.hp_after):
            while p.hp_after < p.max_hp:
                item = _pick_owned(p.max_hp - p.hp_after, inv, prices)
                if item is None:
                    break
                apply(p, item)
            missing = p.max_hp - p.hp_after
            if missing > 0:
                if buy:
                    if only_item is None:
                        shopping = cheapest_purchase(missing, prices)
                    elif POTIONS[only_item] is None:
                        shopping = {only_item: 1}
                    else:
                        shopping = {only_item: math.ceil(missing / POTIONS[only_item])}
                    for item, qty in shopping.items():
                        purchase(item, qty)
                        p.items.extend([item] * qty)
                    p.hp_after = p.max_hp
                elif p.unresolved is None:
                    p.unresolved = f"still missing {missing} HP"

    cost = sum(prices.get(item, 0) * qty for item, qty in bought.items())
    return HealPlan(mode=mode, mons=plans, use=use, buy=bought, cost=cost)
