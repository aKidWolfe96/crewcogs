"""PokéAPI helpers – async, with file-based caching."""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

MAX_POKEMON = 1025

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
    if CACHE_DIR:
        cache_file = CACHE_DIR / f"{id_or_name}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text())
    data = await _get(session, f"https://pokeapi.co/api/v2/pokemon/{id_or_name}")
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


def is_shiny() -> bool:
    return random.random() < (1 / 512)


async def build_pokemon_instance(
    session: aiohttp.ClientSession,
    id_or_name,
    level: Optional[int] = None,
    force_shiny: bool = False,
) -> Dict:
    raw = await fetch_pokemon(session, id_or_name)
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
