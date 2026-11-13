"""Discord embed + formatting helpers for PokéBot.

Everything visual lives here so the look stays consistent: type colours,
type badges, coloured HP bars, XP bars, rarity badges and official artwork.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional

import discord

COLORS = {
    "red":    0xE74C3C,
    "green":  0x2ECC71,
    "blue":   0x3498DB,
    "yellow": 0xF1C40F,
    "purple": 0x9B59B6,
    "orange": 0xE67E22,
    "shiny":  0xFFD700,
    "gray":   0x95A5A6,
    "dark":   0x2C3E50,
    "pink":   0xFF6FAE,
    "teal":   0x1ABC9C,
}

# Main-series type colours — used as the embed accent for a Pokémon.
TYPE_COLORS: Dict[str, int] = {
    "normal": 0xA8A77A, "fire": 0xEE8130, "water": 0x6390F0, "electric": 0xF7D02C,
    "grass": 0x7AC74C, "ice": 0x96D9D6, "fighting": 0xC22E28, "poison": 0xA33EA1,
    "ground": 0xE2BF65, "flying": 0xA98FF3, "psychic": 0xF95587, "bug": 0xA6B91A,
    "rock": 0xB6A136, "ghost": 0x735797, "dragon": 0x6F35FC, "dark": 0x705746,
    "steel": 0xB7B7CE, "fairy": 0xD685AD,
}

TYPE_EMOJIS: Dict[str, str] = {
    "normal": "⬜", "fire": "🔥", "water": "💧", "electric": "⚡",
    "grass": "🌿", "ice": "🧊", "fighting": "🥊", "poison": "☠️",
    "ground": "🟤", "flying": "🌬️", "psychic": "🔮", "bug": "🐛",
    "rock": "🪨", "ghost": "👻", "dragon": "🐉", "dark": "🌑",
    "steel": "⚙️", "fairy": "✨",
}

RARITY_BADGES: Dict[str, str] = {
    "common":    "",
    "rare":      "💠 Rare",
    "legendary": "🌟 LEGENDARY",
    "mythical":  "🔮 MYTHICAL",
}

ARTWORK_BASE = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork"
SPRITE_BASE  = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon"

DIVIDER = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬"


# ──────────────────────────────────────────────────────────────────────────────
# Small formatting helpers
# ──────────────────────────────────────────────────────────────────────────────

def type_tag(type_name: str) -> str:
    emoji = TYPE_EMOJIS.get(type_name, "❓")
    return f"{emoji} {type_name.capitalize()}"


def type_badges(types: Iterable[str]) -> str:
    return " · ".join(type_tag(t) for t in types)


def type_color(pokemon: dict) -> int:
    """Accent colour for a Pokémon: gold if shiny, otherwise its primary type."""
    if pokemon.get("shiny"):
        return COLORS["shiny"]
    types = pokemon.get("types") or []
    return TYPE_COLORS.get(types[0], COLORS["blue"]) if types else COLORS["blue"]


def dex_no(pokemon_id: int) -> str:
    try:
        return f"#{int(pokemon_id):04d}"
    except (TypeError, ValueError):
        return "#????"


def mon_name(pokemon: dict, *, shiny_mark: bool = True) -> str:
    """'Sparky (Pikachu) ✨' — nickname first when one is set."""
    base = pokemon.get("displayName") or pokemon.get("name", "???").capitalize()
    nick = pokemon.get("nickname")
    name = f"{nick} ({base})" if nick else base
    if shiny_mark and pokemon.get("shiny"):
        name += " ✨"
    return name


def artwork_url(pokemon: dict) -> Optional[str]:
    """Large official artwork, derived from the species id (works for old saves too)."""
    pid = pokemon.get("id")
    if not pid:
        return pokemon.get("spriteUrl")
    if pokemon.get("shiny"):
        return f"{ARTWORK_BASE}/shiny/{pid}.png"
    return f"{ARTWORK_BASE}/{pid}.png"


def sprite_url(pokemon: dict) -> Optional[str]:
    """Small pixel sprite (falls back to the id-derived URL)."""
    if pokemon.get("spriteUrl"):
        return pokemon["spriteUrl"]
    pid = pokemon.get("id")
    if not pid:
        return None
    return f"{SPRITE_BASE}/shiny/{pid}.png" if pokemon.get("shiny") else f"{SPRITE_BASE}/{pid}.png"


def hp_bar(current: int, maximum: int, length: int = 10) -> str:
    """Coloured HP bar: green above 50%, yellow above 20%, red below."""
    if maximum <= 0:
        return "⬛" * length
    ratio = max(0.0, min(1.0, current / maximum))
    filled = round(ratio * length)
    if current > 0 and filled == 0:
        filled = 1
    block = "🟩" if ratio > 0.5 else ("🟨" if ratio > 0.2 else "🟥")
    return block * filled + "⬛" * (length - filled)


def hp_line(pokemon: dict, length: int = 10) -> str:
    hp, mx = pokemon["stats"]["hp"], pokemon["stats"]["maxHp"]
    if hp <= 0:
        return f"{'⬛' * length} 💀 **Fainted**"
    return f"{hp_bar(hp, mx, length)} `{hp}/{mx}`"


def xp_bar(current: int, needed: int, length: int = 10) -> str:
    filled = round((current / needed) * length) if needed else length
    filled = max(0, min(length, filled))
    return "▰" * filled + "▱" * (length - filled)


def progress_bar(value: float, total: float, length: int = 10) -> str:
    filled = round((value / total) * length) if total else 0
    filled = max(0, min(length, filled))
    return "█" * filled + "░" * (length - filled)


def pretty_move(slug: Optional[str]) -> str:
    return slug.replace("-", " ").title() if slug else "—"


# ──────────────────────────────────────────────────────────────────────────────
# Embeds
# ──────────────────────────────────────────────────────────────────────────────

def pokemon_embed(
    pokemon: dict,
    title: Optional[str] = None,
    show_xp: bool = False,
    footer: Optional[str] = None,
    description: Optional[str] = None,
) -> discord.Embed:
    """A full 'summary screen' card for one Pokémon."""
    rarity = RARITY_BADGES.get(pokemon.get("rarity", "common"), "")
    header = f"{dex_no(pokemon.get('id'))} · Lv.**{pokemon['level']}** · {type_badges(pokemon['types'])}"
    if rarity:
        header += f"\n{rarity}"
    if pokemon.get("shiny"):
        header += "\n✨ **Shiny**"
    embed = discord.Embed(
        title=title or mon_name(pokemon),
        description=(f"{description}\n\n{header}" if description else header),
        color=type_color(pokemon),
    )
    art = artwork_url(pokemon)
    if art:
        embed.set_thumbnail(url=art)

    embed.add_field(name="❤️ HP", value=hp_line(pokemon), inline=False)
    stats = pokemon.get("stats", {})
    stat_bits = []
    for key, label in (("attack", "Atk"), ("defense", "Def"), ("speed", "Spe"),
                       ("special-attack", "SpA"), ("special-defense", "SpD")):
        if key in stats:
            stat_bits.append(f"**{label}** {stats[key]}")
    if stat_bits:
        embed.add_field(name="📊 Stats", value=" · ".join(stat_bits), inline=False)

    moves = [m for m in pokemon.get("moves", []) if m]
    moves_str = "\n".join(f"`{i}` {pretty_move(m)}" for i, m in enumerate(moves, 1)) or "None"
    embed.add_field(name="🎯 Moves", value=moves_str, inline=True)

    if show_xp:
        xp, need = pokemon.get("xp", 0), pokemon.get("xpToNext") or 1
        embed.add_field(name="⭐ XP", value=f"{xp_bar(xp, need, 8)}\n`{xp}/{need}`", inline=True)
    if footer:
        embed.set_footer(text=footer)
    return embed


def error_embed(message: str) -> discord.Embed:
    return discord.Embed(color=COLORS["red"], description=f"❌ {message}")


def success_embed(message: str) -> discord.Embed:
    return discord.Embed(color=COLORS["green"], description=f"✅ {message}")


def info_embed(message: str, color: int = COLORS["blue"]) -> discord.Embed:
    return discord.Embed(color=color, description=message)
