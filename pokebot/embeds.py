"""Discord embed helpers, ported from embeds.js."""
from __future__ import annotations

import discord
from typing import Dict, Optional

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
}

TYPE_EMOJIS: Dict[str, str] = {
    "normal": "⬜", "fire": "🔥", "water": "💧", "electric": "⚡",
    "grass": "🌿", "ice": "🧊", "fighting": "🥊", "poison": "☠️",
    "ground": "🟤", "flying": "🌬️", "psychic": "🔮", "bug": "🐛",
    "rock": "🪨", "ghost": "👻", "dragon": "🐉", "dark": "🌑",
    "steel": "⚙️", "fairy": "✨",
}


def type_tag(type_name: str) -> str:
    emoji = TYPE_EMOJIS.get(type_name, "❓")
    return f"{emoji} {type_name.capitalize()}"


def pokemon_embed(
    pokemon: dict,
    title: Optional[str] = None,
    show_xp: bool = False,
    footer: Optional[str] = None,
    description: Optional[str] = None,
) -> discord.Embed:
    shiny_tag = " ✨ **SHINY**" if pokemon.get("shiny") else ""
    nick_tag = f' "{pokemon["nickname"]}"' if pokemon.get("nickname") else ""
    embed = discord.Embed(
        title=title or f"{pokemon['displayName']}{shiny_tag}{nick_tag}",
        color=COLORS["shiny"] if pokemon.get("shiny") else COLORS["blue"],
    )
    if pokemon.get("spriteUrl"):
        embed.set_thumbnail(url=pokemon["spriteUrl"])

    embed.add_field(name="Level", value=str(pokemon["level"]), inline=True)
    embed.add_field(
        name="Type",
        value=" / ".join(type_tag(t) for t in pokemon["types"]),
        inline=True,
    )
    embed.add_field(
        name="HP",
        value=f"{pokemon['stats']['hp']}/{pokemon['stats']['maxHp']}",
        inline=True,
    )
    moves_str = " · ".join(
        m.replace("-", " ").capitalize() for m in pokemon.get("moves", [])
    ) or "None"
    embed.add_field(name="Moves", value=moves_str, inline=False)

    if show_xp:
        embed.add_field(
            name="XP",
            value=f"{pokemon['xp']}/{pokemon['xpToNext']}",
            inline=True,
        )
    if footer:
        embed.set_footer(text=footer)
    if description:
        embed.description = description
    return embed


def error_embed(message: str) -> discord.Embed:
    return discord.Embed(color=COLORS["red"], description=f"❌ {message}")


def success_embed(message: str) -> discord.Embed:
    return discord.Embed(color=COLORS["green"], description=f"✅ {message}")


def hp_bar(current: int, maximum: int) -> str:
    filled = round((current / maximum) * 10) if maximum else 0
    return "█" * filled + "░" * (10 - filled)
