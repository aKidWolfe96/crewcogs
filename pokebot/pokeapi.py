"""PokéAPI helpers – async, with file-based caching."""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

MAX_POKEMON = 1025

# Some Pokémon have alternate-form IDs on PokéAPI that differ from their Pokédex number.
# Map any problem IDs to their correct API slug so fetches don't silently fail.
FORM_OVERRIDES: Dict[int, str] = {
    487: "giratina-altered",   # Giratina — default form slug avoids 404s
    646: "kyurem",             # Kyurem base form
    641: "tornadus-incarnate", # Tornadus
    642: "thundurus-incarnate",# Thundurus
    645: "landorus-incarnate", # Landorus
    900: "kleavor",            # Kleavor (sometimes returns empty sprites)
}

TYPE_CHART: Dict[str, Dict[str, float]] = {
    "normal":   {"rock": 0.5, "ghost": 0, "steel": 0.5},
    "fire":     {"fire": 0.5, "water": 0.5, "grass": 2, "ice": 2, "bug": 2, "rock": 0.5, "dragon": 0.5, "steel": 2},
    "water":    {"fire": 2, "water": 0.5, "grass": 0.5, "ground": 2, "rock": 2, "dragon": 0.5},
    "electric": {"water": 2, "electric": 0.5, "grass": 0.5, "ground": 0, "flying": 2, "dragon": 0.5},
    "grass":    {"fire": 0.5, "water": 2, "grass": 0.5, "poison": 0.5, "ground": 2, "flying": 0.5, "bug": 0.5, "rock": 2, "dragon": 0.5, "steel": 0.5},
    "ice":      {"fire": 0.5, "water": 0.5, "grass": 2, "ice": 0.5, "ground": 2, "flying": 2, "dragon": 2, "steel": 0.5},
    "fighting": {"normal": 2, "ice": 2, "poison": 0.5, "flying": 0.5, "psychic": 0.5, "bug": 0.5, "rock": 2, "ghost": 0, "dark": 2, "steel": 2, "fairy": 0.5},
    "poison":   {"grass": 2, "poison": 0.5, "ground": 0.5, "rock": 0.5, "ghost": 0.5, "steel": 0, "fairy": 2},
    "ground":   {"fire": 2, "electric": 2, "grass": 0.5, "poison": 2, "flying": 0, "bug": 0.5, "rock": 2, "steel": 2},
    "flying":   {"electric": 0.5, "grass": 2, "fighting": 2, "bug": 2, "rock": 0.5, "steel": 0.5},
    "psychic":  {"fighting": 2, "poison": 2, "psychic": 0.5, "dark": 0, "steel": 0.5},
    "bug":      {"fire": 0.5, "grass": 2, "fighting": 0.5, "poison": 0.5, "flying": 0.5, "psychic": 2, "ghost": 0.5, "dark": 2, "steel": 0.5, "fairy": 0.5},
    "rock":     {"fire": 2, "ice": 2, "fighting": 0.5, "ground": 0.5, "flying": 2, "bug": 2, "steel": 0.5},
    "ghost":    {"normal": 0, "psychic": 2, "ghost": 2, "dark": 0.5},
    "dragon":   {"dragon": 2, "steel": 0.5, "fairy": 0},
    "dark":     {"fighting": 0.5, "psychic": 2, "ghost": 2, "dark": 0.5, "fairy": 0.5},
    "steel":    {"fire": 0.5, "water": 0.5, "electric": 0.5, "ice": 2, "rock": 2, "steel": 0.5, "fairy": 2},
    "fairy":    {"fire": 0.5, "fighting": 2, "poison": 0.5, "dragon": 2, "dark": 2, "steel": 0.5},
}

CACHE_DIR: Optional[Path] = None


def set_cache_dir(path: Path) -> None:
    global CACHE_DIR
    CACHE_DIR = path
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


async def _get(session: aiohttp.ClientSession, url: str) -> Any:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_pokemon(session: aiohttp.ClientSession, id_or_name) -> Dict:
    slug = resolve_pokemon_id(id_or_name)
    if CACHE_DIR:
        cache_file = CACHE_DIR / f"{slug}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())
    data = await _get(session, f"https://pokeapi.co/api/v2/pokemon/{slug}")
    if CACHE_DIR:
        cache_file.write_text(json.dumps(data))
    return data


async def fetch_move_data(session: aiohttp.ClientSession, move_name: str) -> Dict:
    if CACHE_DIR:
        cache_file = CACHE_DIR / f"move_{move_name}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())
    data = await _get(session, f"https://pokeapi.co/api/v2/move/{move_name}")
    if CACHE_DIR:
        cache_file.write_text(json.dumps(data))
    return data


def get_random_pokemon_id() -> int:
    return random.randint(1, MAX_POKEMON)


def resolve_pokemon_id(id_or_name) -> str:
    """Return the correct PokéAPI slug for a given ID, applying form overrides."""
    if isinstance(id_or_name, int) and id_or_name in FORM_OVERRIDES:
        return FORM_OVERRIDES[id_or_name]
    return str(id_or_name)


def is_shiny() -> bool:
    return random.random() < (1 / 512)


async def build_pokemon_instance(
    session: aiohttp.ClientSession,
    id_or_name,
    level: Optional[int] = None,
    force_shiny: bool = False,
) -> Dict:
    slug = resolve_pokemon_id(id_or_name)
    raw = await fetch_pokemon(session, slug)
    lvl = level if level is not None else random.randint(2, 21)
    shiny = force_shiny or is_shiny()
    types = [t["type"]["name"] for t in raw["types"]]

    learnable = [
        m["move"]["name"]
        for m in raw["moves"]
        if any(
            d["move_learn_method"]["name"] == "level-up" and d["level_learned_at"] <= lvl
            for d in m["version_group_details"]
        )
    ]
    pool = learnable if learnable else [m["move"]["name"] for m in raw["moves"]]
    pool = list(pool)
    random.shuffle(pool)
    selected_moves = pool[:4]

    stats: Dict[str, int] = {}
    for s in raw["stats"]:
        base = s["base_stat"]
        name = s["stat"]["name"]
        if name == "hp":
            stats["maxHp"] = math.floor(((2 * base * lvl) / 100) + lvl + 10)
            stats["hp"] = stats["maxHp"]
        else:
            stats[name] = math.floor(((2 * base * lvl) / 100) + 5)

    display_name = raw["name"].capitalize()
    sprite_url = (
        raw["sprites"].get("front_shiny") or raw["sprites"]["front_default"]
        if shiny
        else raw["sprites"]["front_default"]
    )

    return {
        "id": raw["id"],
        "uid": new_uid(),
        "name": raw["name"],
        "displayName": display_name,
        "types": types,
        "level": lvl,
        "xp": 0,
        "xpToNext": lvl * lvl * 10,
        "shiny": shiny,
        "moves": selected_moves,
        "stats": stats,
        "spriteUrl": sprite_url,
        "caughtAt": __import__("time").time(),
        "nickname": None,
    }


def calculate_type_effectiveness(attack_type: str, defender_types: List[str]) -> float:
    multiplier = 1.0
    chart = TYPE_CHART.get(attack_type, {})
    for def_type in defender_types:
        if def_type in chart:
            multiplier *= chart[def_type]
    return multiplier


def effectiveness_label(mult: float) -> str:
    if mult == 0:
        return "It had no effect!"
    if mult >= 4:
        return "It's super effective!! (4×)"
    if mult >= 2:
        return "It's super effective!"
    if mult <= 0.25:
        return "It's not very effective... (¼×)"
    if mult < 1:
        return "It's not very effective..."
    return ""


def catch_rate(pokemon: Dict, ball_type: str) -> float:
    rates = {"pokeball": 1.0, "greatball": 1.5, "ultraball": 2.0}
    mult = rates.get(ball_type, 1.0)
    hp_ratio = pokemon["stats"]["hp"] / pokemon["stats"]["maxHp"]
    base = (1 - (2 / 3) * hp_ratio) * mult * 0.45
    return min(base, 0.95)


# ──────────────────────────────────────────────────────────────────────────────
# Party / collection helpers  (pure — unit-testable, no discord/redbot needed)
# ──────────────────────────────────────────────────────────────────────────────

def new_uid() -> str:
    """Return a short, collision-safe unique id for a single Pokémon instance."""
    return uuid.uuid4().hex


def ensure_uids(player: dict) -> bool:
    """Give every Pokémon in the collection a stable unique id.

    Backfills legacy Pokémon (created before uids existed) and repairs any
    accidental duplicate uids (e.g. raid-catch copies that shared an id).
    Returns True if anything changed so the caller knows to persist.
    """
    changed = False
    seen: set = set()
    for mon in player.get("pokemon", []):
        uid = mon.get("uid")
        if not uid or uid in seen:
            uid = new_uid()
            mon["uid"] = uid
            changed = True
        seen.add(uid)
    return changed


def uid_index(player: dict, uid: str) -> int:
    """Collection index of the Pokémon with this uid, or -1."""
    for i, m in enumerate(player.get("pokemon", [])):
        if m.get("uid") == uid:
            return i
    return -1


def party_mons(player: dict) -> List[dict]:
    """Resolve the player's party uids to live Pokémon dicts, in party order.

    Stale uids (released/traded away) are silently skipped.
    """
    by_uid = {m.get("uid"): m for m in player.get("pokemon", [])}
    return [by_uid[u] for u in player.get("party", []) if u in by_uid]


def ensure_party(player: dict) -> bool:
    """Keep the party list valid and in sync with the lead Pokémon.

    Invariants enforced:
      • every party uid still exists in the collection (drop stale)
      • no duplicate uids, max 6 members
      • party[0] is always the lead (== pokemon[activePokemonIndex])

    Requires uids to already be assigned (call ensure_uids first).
    Returns True if anything changed.
    """
    changed = False
    mons = player.get("pokemon", [])
    if not mons:
        if player.get("party"):
            player["party"] = []
            changed = True
        return changed

    uid_set = {m["uid"] for m in mons}
    # Drop stale uids + dedupe while preserving order
    seen: set = set()
    party: List[str] = []
    for u in player.get("party", []):
        if u in uid_set and u not in seen:
            party.append(u)
            seen.add(u)

    lead_idx = player.get("activePokemonIndex", 0)
    if not (0 <= lead_idx < len(mons)):
        lead_idx = 0
        if player.get("activePokemonIndex") != 0:
            player["activePokemonIndex"] = 0
            changed = True
    lead_uid = mons[lead_idx]["uid"]

    if not party:
        party = [lead_uid]
        changed = True

    # Lead must sit at the front of the party
    if lead_uid in party:
        if party[0] != lead_uid:
            party.remove(lead_uid)
            party.insert(0, lead_uid)
            changed = True
    else:
        party.insert(0, lead_uid)
        changed = True

    if len(party) > 6:
        party = party[:6]
        changed = True

    if player.get("party") != party:
        player["party"] = party
        changed = True
    return changed


# ──────────────────────────────────────────────────────────────────────────────
# Raid balance helpers  (pure)
# ──────────────────────────────────────────────────────────────────────────────

def estimate_hit(mon: dict, boss_def: int, power: int = 75) -> int:
    """Average single-hit damage a Pokémon would deal to the boss.

    Uses a nominal move power (no fetch needed) and ignores STAB/crit/type so
    it can size boss HP cheaply and deterministically. Mirrors the live battle
    formula's core term.
    """
    lvl = mon.get("level", 1)
    stats = mon.get("stats", {})
    atk = stats.get("attack") or stats.get("special-attack") or 50
    bd = min(max(int(boss_def), 1), 80)
    return max(1, math.floor((((2 * lvl / 5 + 2) * power * atk / bd) / 50 + 2)))


def boss_counter_damage(target_max_hp: int, frac: float, roll: float) -> int:
    """Boss counter-attack damage, capped as a fraction of the TARGET's max HP.

    This is the core fix for the instant-wipe: damage scales with the victim's
    own HP pool instead of exploding from the boss/player level gap, so a mon
    always survives several hits regardless of level.

    `frac` is the per-hit fraction (by star tier); `roll` ∈ [0,1) adds ±15%.
    """
    return max(1, math.floor(target_max_hp * frac * (0.85 + roll * 0.30)))
