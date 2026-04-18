"""
PokéBot – Red-DiscordBot cog
Converted from a discord.js standalone bot.

Commands (all prefixed, no slash commands):
  [p]start        – Begin journey / pick starter
  [p]profile      – View trainer profile
  [p]pokemon      – Browse collection
  [p]active       – Switch active Pokémon
  [p]nickname     – Nickname a Pokémon
  [p]dex          – Pokédex lookup
  [p]pokedex      – Your Pokédex (caught species)
  [p]dexpage      – Browse Pokédex by page
  [p]catch        – Catch a wild Pokémon
  [p]shop         – Browse PokéMart
  [p]buy          – Buy items in bulk
  [p]use          – Use a healing item
  [p]inventory    – View your bag
  [p]battle       – Challenge another trainer
  [p]move         – Use a move in battle
  [p]tms         – Browse TM shop
  [p]buytm       – Buy a TM
  [p]usetm       – Teach a TM move to a Pokémon
"""
from __future__ import annotations

import asyncio
import copy
import logging
import math
import random
import time
import uuid
from datetime import datetime, timezone, timedelta
import zoneinfo
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
import discord
from redbot.core import Config, bank, commands, checks
from redbot.core.bot import Red
from redbot.core.errors import BalanceTooHigh

from .embeds import (
    COLORS, TYPE_EMOJIS, error_embed, hp_bar, pokemon_embed,
    success_embed, type_tag,
)
from .pokeapi import (
    MAX_POKEMON, build_pokemon_instance, calculate_type_effectiveness,
    catch_rate, effectiveness_label, fetch_move_data, fetch_pokemon,
    get_random_pokemon_id, resolve_pokemon_id, set_cache_dir,
)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

log = logging.getLogger("red.pokebot")

FLEE_TIMEOUT   = 4 * 60 * 60   # 4 hours — how long before an uncaught spawn flees
BATTLE_TIMEOUT = 3 * 60        # 3 minutes — auto-forfeit if a player goes AFK in battle

STARTERS = [
    # Gen 1
    {"id": 1,   "name": "Bulbasaur"},  {"id": 4,   "name": "Charmander"}, {"id": 7,   "name": "Squirtle"},
    # Gen 2
    {"id": 152, "name": "Chikorita"}, {"id": 155, "name": "Cyndaquil"},  {"id": 158, "name": "Totodile"},
    # Gen 3
    {"id": 252, "name": "Treecko"},   {"id": 255, "name": "Torchic"},    {"id": 258, "name": "Mudkip"},
    # Gen 4
    {"id": 387, "name": "Turtwig"},   {"id": 390, "name": "Chimchar"},   {"id": 393, "name": "Piplup"},
    # Gen 5
    {"id": 495, "name": "Snivy"},     {"id": 498, "name": "Tepig"},      {"id": 501, "name": "Oshawott"},
    # Gen 6
    {"id": 650, "name": "Chespin"},   {"id": 653, "name": "Fennekin"},   {"id": 656, "name": "Froakie"},
    # Gen 7
    {"id": 722, "name": "Rowlet"},    {"id": 725, "name": "Litten"},     {"id": 728, "name": "Popplio"},
    # Gen 8
    {"id": 810, "name": "Grookey"},   {"id": 813, "name": "Scorbunny"},  {"id": 816, "name": "Sobble"},
    # Gen 9
    {"id": 906, "name": "Sprigatito"},{"id": 909, "name": "Fuecoco"},    {"id": 912, "name": "Quaxly"},
]

SHOP_ITEMS = [
    {"id": "pokeball",    "name": "Poké Ball",    "emoji": "🔴", "desc": "Standard catch ball",             "price": 50,  "category": "balls"},
    {"id": "greatball",   "name": "Great Ball",   "emoji": "🔵", "desc": "Better catch rate (1.5×)",        "price": 150, "category": "balls"},
    {"id": "ultraball",   "name": "Ultra Ball",   "emoji": "⚫", "desc": "Best catch rate (2×)",            "price": 300, "category": "balls"},
    {"id": "potion",      "name": "Potion",       "emoji": "🧪", "desc": "Heals 20 HP",                     "price": 100, "category": "healing"},
    {"id": "superpotion", "name": "Super Potion", "emoji": "💊", "desc": "Heals 50 HP",                     "price": 200, "category": "healing"},
    {"id": "maxpotion",   "name": "Max Potion",   "emoji": "💉", "desc": "Fully restores HP",               "price": 500, "category": "healing"},
    {"id": "revive",      "name": "Revive",       "emoji": "⭐", "desc": "Revives fainted Pokémon to ½ HP", "price": 400, "category": "healing"},
]

HEAL_AMOUNTS = {"potion": 20, "superpotion": 50, "maxpotion": math.inf, "revive": None}
ITEM_NAMES   = {"potion": "🧪 Potion", "superpotion": "💊 Super Potion", "maxpotion": "💉 Max Potion", "revive": "⭐ Revive"}
BALL_NAMES   = {"pokeball": "Poké Ball", "greatball": "Great Ball", "ultraball": "Ultra Ball"}

# TM list — move name (PokéAPI slug) mapped to display info and price
# Prices are tiered 300–1000 by move power/utility (least → best):
#   300  — Basic utility moves
#   450  — Solid mid-tier moves
#   600  — Strong standard moves
#   750  — Premium coverage moves
#   900  — High-power moves
#  1000  — Top-tier / signature moves
TM_LIST: Dict[str, dict] = {
    # ── 300 — Basic ───────────────────────────────────────────────────────────
    "aerial-ace":       {"name": "Aerial Ace",        "type": "flying",   "price": 300,   "desc": "An incredibly fast and accurate attack."},
    "brick-break":      {"name": "Brick Break",       "type": "fighting", "price": 300,   "desc": "A chop that smashes barriers."},
    "x-scissor":        {"name": "X-Scissor",         "type": "bug",      "price": 300,   "desc": "Slashes with crossed scythes."},
    "rock-slide":       {"name": "Rock Slide",        "type": "rock",     "price": 300,   "desc": "Large rocks are hurled at the foe."},
    "giga-drain":       {"name": "Giga Drain",        "type": "grass",    "price": 300,   "desc": "Drains HP from the foe."},
    # ── 450 — Mid-tier ────────────────────────────────────────────────────────
    "sludge-bomb":      {"name": "Sludge Bomb",       "type": "poison",   "price": 450,   "desc": "Hurls toxic sludge at the target."},
    "energy-ball":      {"name": "Energy Ball",       "type": "grass",    "price": 450,   "desc": "Fires a green orb of nature energy."},
    "surf":             {"name": "Surf",              "type": "water",    "price": 450,   "desc": "A surging wave attack."},
    "shadow-ball":      {"name": "Shadow Ball",       "type": "ghost",    "price": 450,   "desc": "A shadowy blob that may lower Sp. Def."},
    "dark-pulse":       {"name": "Dark Pulse",        "type": "dark",     "price": 450,   "desc": "Emanates a horrible aura of fear."},
    "dazzling-gleam":   {"name": "Dazzling Gleam",    "type": "fairy",    "price": 450,   "desc": "Dazes the foe with a powerful flash."},
    "flash-cannon":     {"name": "Flash Cannon",      "type": "steel",    "price": 450,   "desc": "Fires a beam of light energy."},
    "iron-head":        {"name": "Iron Head",         "type": "steel",    "price": 450,   "desc": "Slams with a steel-hard head."},
    # ── 600 — Strong ──────────────────────────────────────────────────────────
    "flamethrower":     {"name": "Flamethrower",      "type": "fire",     "price": 600,   "desc": "A powerful Fire-type blast."},
    "ice-beam":         {"name": "Ice Beam",          "type": "ice",      "price": 600,   "desc": "An icy beam that may freeze."},
    "thunderbolt":      {"name": "Thunderbolt",       "type": "electric", "price": 600,   "desc": "A strong electric attack."},
    "psychic":          {"name": "Psychic",           "type": "psychic",  "price": 600,   "desc": "A strong psychic attack."},
    "dragon-pulse":     {"name": "Dragon Pulse",      "type": "dragon",   "price": 600,   "desc": "A shock wave of pure draconic energy."},
    # ── 750 — Premium ─────────────────────────────────────────────────────────
    "focus-blast":      {"name": "Focus Blast",       "type": "fighting", "price": 750,   "desc": "A powerful, fully focused punch."},
    "earthquake":       {"name": "Earthquake",        "type": "ground",   "price": 750,   "desc": "Shakes the ground for big damage."},
    # ── 900 — High-power ──────────────────────────────────────────────────────
    "blizzard":         {"name": "Blizzard",          "type": "ice",      "price": 900,   "desc": "A howling blizzard that may freeze."},
    "fire-blast":       {"name": "Fire Blast",        "type": "fire",     "price": 900,   "desc": "An inferno that may burn."},
    "thunder":          {"name": "Thunder",           "type": "electric", "price": 900,   "desc": "A huge lightning bolt."},
    "solar-beam":       {"name": "Solar Beam",        "type": "grass",    "price": 900,   "desc": "A two-turn beam of solar energy."},
    # ── 1000 — Top-tier ───────────────────────────────────────────────────────
    "hyper-beam":       {"name": "Hyper Beam",        "type": "normal",   "price": 1000,  "desc": "The strongest Normal-type attack."},
}

TM_TYPE_EMOJI: Dict[str, str] = {
    "fire": "🔥", "ice": "🧊", "electric": "⚡", "ground": "🟤",
    "psychic": "🔮", "water": "💧", "ghost": "👻", "grass": "🌿",
    "dragon": "🐉", "fighting": "🥊", "poison": "☠️", "rock": "🪨",
    "steel": "⚙️", "bug": "🐛", "flying": "🌬️", "normal": "⬜",
    "dark": "🌑", "fairy": "✨",
}


# ──────────────────────────────────────────────────────────────────────────────
# Pokédex helpers
# ──────────────────────────────────────────────────────────────────────────────

def _dex_progress_bar(caught: int, total: int, length: int = 20) -> str:
    """Coloured block progress bar using Discord emoji squares."""
    filled   = round((caught / total) * length) if total else 0
    empty    = length - filled
    pct      = (caught / total) * 100 if total else 0
    # Colour tier: red → orange → yellow → green → gold
    if pct >= 100:
        block = "🟨"
    elif pct >= 75:
        block = "🟩"
    elif pct >= 50:
        block = "🟦"
    elif pct >= 25:
        block = "🟧"
    else:
        block = "🟥"
    return block * filled + "⬛" * empty


DEX_RANK_TIERS = [
    (1025, "🏆 **Pokémon Master**",   0xFFD700),
    (900,  "🌟 **Champion**",          0xFFD700),
    (750,  "💎 **Elite Trainer**",     0x9B59B6),
    (500,  "🔵 **Ace Trainer**",       0x3498DB),
    (250,  "🌿 **Rising Trainer**",    0x2ECC71),
    (100,  "🔰 **Rookie**",            0xE67E22),
    (0,    "🥚 **Beginner**",          0x95A5A6),
]

def _dex_rank(caught: int):
    """Return (rank_label, embed_color) based on dex completion."""
    for threshold, label, color in DEX_RANK_TIERS:
        if caught >= threshold:
            return label, color


# ──────────────────────────────────────────────────────────────────────────────
# Bank helpers  (Red economy — guild-scoped or global depending on bank setting)
# ──────────────────────────────────────────────────────────────────────────────

async def _get_balance(member: discord.Member) -> int:
    return await bank.get_balance(member)

async def _deposit(member: discord.Member, amount: int) -> None:
    """Add `amount` to the member's bank balance, capping at the bank max."""
    try:
        await bank.deposit_credits(member, amount)
    except BalanceTooHigh as e:
        # Just silently cap — the player still gets as much as possible
        await bank.set_balance(member, e.max_balance)

async def _withdraw(member: discord.Member, amount: int) -> bool:
    """Remove `amount` from the member's bank balance. Returns False if insufficient funds."""
    balance = await bank.get_balance(member)
    if balance < amount:
        return False
    await bank.withdraw_credits(member, amount)
    return True

async def _currency_name(guild: discord.Guild) -> str:
    return await bank.get_currency_name(guild)


# ──────────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────────

class PokéBot(commands.Cog):
    """Full-featured Pokémon catching and battling cog."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot

        # In-memory state
        self._battles:      Dict[str, dict]          = {}  # battle_id  -> battle
        self._challenges:   Dict[int, dict]           = {}  # challenged_user_id -> challenge
        self._spawn_cache:  Dict[int, dict]           = {}  # channel_id -> spawn
        self._spawn_tasks:  Dict[int, asyncio.Task]   = {}  # guild_id   -> loop task
        self._flee_tasks:   Dict[int, asyncio.Task]   = {}  # channel_id -> flee timer task
        self._pending_respawn: Dict[int, discord.TextChannel] = {}  # guild_id -> channel waiting for activity
        self._msg_counts:   Dict[int, int]            = {}  # channel_id -> message count

        self.config = Config.get_conf(self, identifier=0x504F4B45424F54, force_registration=True)

        # Default shop prices — mirrors SHOP_ITEMS prices; admins can override per-guild
        _default_shop_prices = {i["id"]: i["price"] for i in SHOP_ITEMS}

        _default_tm_prices = {slug: info["price"] for slug, info in TM_LIST.items()}

        default_guild = {
            "spawn_channel_id": None,
            "spawn_interval":   300,
            "flee_timeout":     14400,   # seconds — default 4 hours
            "max_pokemon":      500,     # collection cap per trainer
            "shop_prices":      _default_shop_prices,
            "tm_prices":        _default_tm_prices,
        }
        # NOTE: credits field removed — balance lives in Red's bank now.
        default_member = {
            "userId":             None,
            "username":           "",
            "registeredAt":       None,
            "pokemon":            [],
            "activePokemonIndex": 0,
            "wins":               0,
            "losses":             0,
            "items": {
                "pokeball":  0,
                "greatball": 0,
                "ultraball": 0,
                "healing":   {},
                "tms":       [],
            },
            "lastPokestop": None,
            "caughtDex":    [],
        }
        self.config.register_guild(**default_guild)
        self.config.register_member(**default_member)

        cache_path = Path(__file__).parent / "data" / "pokemon_cache"
        set_cache_dir(cache_path)

        self._session: Optional[aiohttp.ClientSession] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        for task in self._spawn_tasks.values():
            task.cancel()
        for task in self._flee_tasks.values():
            task.cancel()
        if self._session:
            await self._session.close()

    # ── Player helpers ────────────────────────────────────────────────────────

    async def _get_player(self, member: discord.Member) -> Optional[dict]:
        data = await self.config.member(member).all()
        return data if data["registeredAt"] is not None else None

    async def _save_player(self, member: discord.Member, data: dict) -> None:
        await self.config.member(member).set(data)

    async def _get_shop_prices(self, guild: discord.Guild) -> dict:
        """Return the guild's current shop prices, falling back to defaults."""
        stored = await self.config.guild(guild).shop_prices()
        defaults = {i["id"]: i["price"] for i in SHOP_ITEMS}
        return {**defaults, **stored}

    async def _get_tm_prices(self, guild: discord.Guild) -> dict:
        """Return the guild's current TM prices, falling back to defaults."""
        stored = await self.config.guild(guild).tm_prices()
        defaults = {slug: info["price"] for slug, info in TM_LIST.items()}
        return {**defaults, **stored}


    async def _create_player(self, member: discord.Member, starter: dict) -> dict:
        player = {
            "userId":             member.id,
            "username":           member.display_name,
            "registeredAt":       time.time(),
            "pokemon":            [starter],
            "activePokemonIndex": 0,
            "wins":               0,
            "losses":             0,
            "items": {
                "pokeball":  10,
                "greatball": 3,
                "ultraball": 1,
                "healing":   {},
                "tms":       [],
            },
            "lastPokestop": None,
            "caughtDex":    [starter["id"]],
        }
        await self._save_player(member, player)
        # Give starter credits via bank
        await _deposit(member, 500)
        return player

    # ── Battle helpers ────────────────────────────────────────────────────────

    def _get_battle_by_user(self, user_id: int) -> Optional[Tuple[str, dict]]:
        for bid, battle in self._battles.items():
            if battle["player1"]["id"] == user_id or battle["player2"]["id"] == user_id:
                return bid, battle
        return None

    def _check_level_up(self, pokemon: dict) -> List[str]:
        messages = []
        while pokemon["xp"] >= pokemon["xpToNext"]:
            pokemon["xp"]      -= pokemon["xpToNext"]
            pokemon["level"]   += 1
            pokemon["xpToNext"] = pokemon["level"] ** 2 * 10
            growth = math.floor(pokemon["level"] * 0.5)
            pokemon["stats"]["maxHp"] += growth
            pokemon["stats"]["hp"] = min(pokemon["stats"]["hp"] + growth, pokemon["stats"]["maxHp"])
            for stat in ("attack", "defense", "speed"):
                if stat in pokemon["stats"]:
                    mult = {"attack": 0.8, "defense": 0.6, "speed": 0.7}[stat]
                    pokemon["stats"][stat] += math.floor(growth * mult)
            messages.append(f"⬆️ **{pokemon['displayName']}** leveled up to **Lv.{pokemon['level']}**!")
        return messages

    def _update_dex(self, player: dict, pokemon: dict) -> bool:
        """Record a newly caught species in the player's Pokédex. Returns True if it's a new entry."""
        dex = player.setdefault("caughtDex", [])
        pid = pokemon["id"]
        if pid not in dex:
            dex.append(pid)
            return True
        return False

    def _build_battle_embed(self, battle: dict, log_lines: Optional[List[str]] = None) -> discord.Embed:
        if log_lines is None:
            log_lines = []
        p1, p2 = battle["player1"], battle["player2"]
        embed = discord.Embed(
            title=f"⚔️ Pokémon Battle — Turn {battle['turn']}",
            color=COLORS["purple"],
        )
        embed.add_field(
            name=f"{p1['username']}'s {p1['pokemon']['displayName']}{'  ✨' if p1['pokemon'].get('shiny') else ''}",
            value=f"Lv.{p1['pokemon']['level']} | HP: {hp_bar(p1['pokemon']['stats']['hp'], p1['pokemon']['stats']['maxHp'])} ({p1['pokemon']['stats']['hp']}/{p1['pokemon']['stats']['maxHp']})",
            inline=False,
        )
        embed.add_field(
            name=f"{p2['username']}'s {p2['pokemon']['displayName']}{'  ✨' if p2['pokemon'].get('shiny') else ''}",
            value=f"Lv.{p2['pokemon']['level']} | HP: {hp_bar(p2['pokemon']['stats']['hp'], p2['pokemon']['stats']['maxHp'])} ({p2['pokemon']['stats']['hp']}/{p2['pokemon']['stats']['maxHp']})",
            inline=False,
        )
        if log_lines:
            embed.add_field(name="Battle Log", value="\n".join(log_lines), inline=False)
        embed.set_footer(text="Use `move <move_name>` to attack!")
        return embed

    async def _resolve_move(
        self, attacker: dict, defender: dict, move_name: str
    ) -> Tuple[List[str], bool]:
        log = []
        try:
            move_data = await fetch_move_data(self._session, move_name)
        except Exception:
            log.append(f"⚠️ {attacker['pokemon']['displayName']} tried {move_name} but it failed!")
            return log, False

        power     = move_data.get("power") or 0
        move_type = move_data["type"]["name"]
        accuracy  = move_data.get("accuracy") or 100

        if random.random() * 100 > accuracy:
            log.append(f"💨 {attacker['pokemon']['displayName']} used **{move_name.replace('-', ' ')}** but missed!")
            return log, False

        log.append(f"🎯 {attacker['pokemon']['displayName']} used **{move_name.replace('-', ' ')}**!")

        if power == 0:
            log.append(f"_{move_name.replace('-', ' ')} had no damage effect._")
            return log, False

        atk  = attacker["pokemon"]["stats"].get("attack") or attacker["pokemon"]["stats"].get("special-attack") or 50
        dfs  = defender["pokemon"]["stats"].get("defense") or defender["pokemon"]["stats"].get("special-defense") or 50
        lvl  = attacker["pokemon"]["level"]

        type_eff  = calculate_type_effectiveness(move_type, defender["pokemon"]["types"])
        stab      = 1.5 if move_type in attacker["pokemon"]["types"] else 1.0
        rand_mult = 0.85 + random.random() * 0.15
        critical  = 1.5 if random.random() < 0.0625 else 1.0

        damage = max(1, math.floor(
            (((2 * lvl / 5 + 2) * power * atk / dfs) / 50 + 2)
            * stab * type_eff * rand_mult * critical
        ))

        defender["pokemon"]["stats"]["hp"] = max(0, defender["pokemon"]["stats"]["hp"] - damage)

        if critical > 1:
            log.append("⚡ A critical hit!")
        eff_txt = effectiveness_label(type_eff)
        if eff_txt:
            log.append(eff_txt)
        log.append(
            f"💥 Dealt **{damage}** damage! "
            f"({defender['pokemon']['displayName']} HP: "
            f"{defender['pokemon']['stats']['hp']}/{defender['pokemon']['stats']['maxHp']})"
        )

        battle_over = defender["pokemon"]["stats"]["hp"] <= 0
        if battle_over:
            log.append(f"💀 {defender['pokemon']['displayName']} fainted!")

        return log, battle_over

    async def _process_turn(self, battle_id: str) -> Optional[Tuple[dict, List[str], Optional[dict]]]:
        battle = self._battles.get(battle_id)
        if not battle:
            return None
        p1, p2 = battle["player1"], battle["player2"]
        if not p1.get("moveUsed") or not p2.get("moveUsed"):
            return None

        turn_log = [f"**— Turn {battle['turn']} —**"]

        spd1 = p1["pokemon"]["stats"].get("speed", 50)
        spd2 = p2["pokemon"]["stats"].get("speed", 50)
        if spd1 >= spd2:
            first, second = (p1, p2, p1["moveUsed"]), (p2, p1, p2["moveUsed"])
        else:
            first, second = (p2, p1, p2["moveUsed"]), (p1, p2, p1["moveUsed"])

        log1, over1 = await self._resolve_move(first[0], first[1], first[2])
        turn_log.extend(log1)
        winner = None
        if over1:
            winner = first[0]
            battle["status"] = "finished"
        else:
            log2, over2 = await self._resolve_move(second[0], second[1], second[2])
            turn_log.extend(log2)
            if over2:
                winner = second[0]
                battle["status"] = "finished"

        battle["turn"] += 1
        now = time.time()
        p1["moveUsed"]   = None
        p2["moveUsed"]   = None
        p1["lastMoveAt"] = now   # reset AFK timer for the new turn
        p2["lastMoveAt"] = now

        return battle, turn_log, winner

    async def _end_battle(
        self,
        guild: discord.Guild,
        battle_id: str,
        winner_id: int,
        loser_id: int,
    ) -> None:
        battle = self._battles.pop(battle_id, None)

        winner_member = guild.get_member(winner_id)
        loser_member  = guild.get_member(loser_id)

        if winner_member:
            winner_data = await self._get_player(winner_member)
            if winner_data:
                winner_data["wins"] = winner_data.get("wins", 0) + 1
                wp = winner_data["pokemon"][winner_data["activePokemonIndex"]]
                wp["xp"] = wp.get("xp", 0) + 50 * (battle["turn"] if battle else 1)
                self._check_level_up(wp)
                # Write battle damage back — winner keeps whatever HP they have left
                if battle:
                    p1_is_winner = battle["player1"]["id"] == winner_id
                    winner_battle_poke = battle["player1"]["pokemon"] if p1_is_winner else battle["player2"]["pokemon"]
                    wp["stats"]["hp"] = max(1, winner_battle_poke["stats"]["hp"])  # winner survives with at least 1 HP
                await self._save_player(winner_member, winner_data)
                await _deposit(winner_member, 100)

        if loser_member:
            loser_data = await self._get_player(loser_member)
            if loser_data:
                loser_data["losses"] = loser_data.get("losses", 0) + 1
                # Write battle damage back — loser's pokemon faints at 0 HP
                if battle:
                    p1_is_loser = battle["player1"]["id"] == loser_id
                    loser_battle_poke = battle["player1"]["pokemon"] if p1_is_loser else battle["player2"]["pokemon"]
                    lp = loser_data["pokemon"][loser_data["activePokemonIndex"]]
                    lp["stats"]["hp"] = 0  # fainted
                await self._save_player(loser_member, loser_data)

    # ── Spawn & Flee System ───────────────────────────────────────────────────

    async def _spawn_wild(self, channel: discord.TextChannel) -> None:
        """Spawn a wild Pokémon in the channel and start a flee timer."""
        if channel.id in self._spawn_cache:
            return

        pokemon_id = get_random_pokemon_id()
        try:
            pokemon = await build_pokemon_instance(self._session, pokemon_id)
        except Exception as exc:
            log.warning(f"[PokéBot] Failed to fetch Pokémon ID {pokemon_id}: {exc}")
            return

        spawn_id = str(uuid.uuid4())
        self._spawn_cache[channel.id] = {
            "pokemon":   pokemon,
            "channelId": channel.id,
            "spawnedAt": time.time(),
            "spawnId":   spawn_id,
        }

        shiny_text = "\n✨ **A SHINY Pokémon appeared!** ✨" if pokemon["shiny"] else ""
        embed = discord.Embed(
            title=f"A wild {pokemon['displayName']} appeared!{'  ✨' if pokemon['shiny'] else ''}",
            description=(
                f"**Level {pokemon['level']}** | "
                f"Type: {' / '.join(t.capitalize() for t in pokemon['types'])}"
                + shiny_text
            ),
            color=COLORS["shiny"] if pokemon["shiny"] else COLORS["green"],
        )
        if pokemon.get("spriteUrl"):
            embed.set_image(url=pokemon["spriteUrl"])
        embed.set_footer(text=f"Use `catch <ball>` to catch it! It will flee in 4 hours if ignored.")
        await channel.send(embed=embed)

        # Cancel any existing flee timer for this channel then start a fresh one
        flee_timeout = await self.config.guild(channel.guild).flee_timeout()
        self._cancel_flee_task(channel.id)
        self._flee_tasks[channel.id] = self.bot.loop.create_task(
            self._flee_timer(channel, pokemon, spawn_id, flee_timeout)
        )

    async def _flee_timer(self, channel: discord.TextChannel, pokemon: dict, spawn_id: str, flee_timeout: int = FLEE_TIMEOUT) -> None:
        """Wait flee_timeout seconds; if the Pokémon is still uncaught, it flees and a new one spawns shortly after."""
        await asyncio.sleep(flee_timeout)

        # Only flee if this exact spawn (by unique ID) is still in the cache
        cached = self._spawn_cache.get(channel.id)
        if cached and cached.get("spawnId") == spawn_id:
            self._spawn_cache.pop(channel.id, None)
            try:
                embed = discord.Embed(
                    description=(
                        f"🌿 The wild **{pokemon['displayName']}** got bored and fled into the tall grass!\n"
                        f"_A new Pokémon will appear when someone next speaks..._"
                    ),
                    color=COLORS["gray"],
                )
                if pokemon.get("spriteUrl"):
                    embed.set_thumbnail(url=pokemon["spriteUrl"])
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass

            # Mark this guild as waiting for activity before respawning
            self._pending_respawn[channel.guild.id] = channel

    def _cancel_flee_task(self, channel_id: int) -> None:
        task = self._flee_tasks.pop(channel_id, None)
        if task and not task.done():
            task.cancel()

    async def _delayed_respawn(self, channel: discord.TextChannel) -> None:
        """Wait a short random delay then spawn a new Pokémon after a flee."""
        await asyncio.sleep(random.randint(10, 60))
        await self._spawn_wild(channel)

    async def _spawn_loop(self, guild: discord.Guild) -> None:
        log = logging.getLogger("red.pokebot")
        await self.bot.wait_until_ready()
        while True:
            try:
                interval   = await self.config.guild(guild).spawn_interval()
                channel_id = await self.config.guild(guild).spawn_channel_id()
                jitter     = random.randint(-60, 60)
                await asyncio.sleep(max(60, (interval or 300) + jitter))
                if channel_id:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        await self._spawn_wild(channel)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.exception(f"[PokéBot] spawn loop error in {guild.name}: {exc}")
                await asyncio.sleep(60)

    def _ensure_spawn_task(self, guild: discord.Guild) -> None:
        task = self._spawn_tasks.get(guild.id)
        if task is None or task.done():
            if task is not None and task.done() and not task.cancelled():
                # Log if the task died unexpectedly rather than being cancelled
                exc = task.exception() if not task.cancelled() else None
                if exc:
                    logging.getLogger("red.pokebot").error(
                        f"[PokéBot] spawn task for {guild.name} died: {exc} — restarting"
                    )
            self._spawn_tasks[guild.id] = self.bot.loop.create_task(self._spawn_loop(guild))

    # ── Listeners ─────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        self._ensure_spawn_task(guild)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            self._ensure_spawn_task(guild)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        spawn_channel_id = await self.config.guild(message.guild).spawn_channel_id()
        if not spawn_channel_id:
            return
        # Watchdog — restart the spawn loop if it has silently died
        self._ensure_spawn_task(message.guild)

        # Any message anywhere in the server triggers a pending post-flee respawn
        if message.guild.id in self._pending_respawn:
            channel = self._pending_respawn.pop(message.guild.id)
            self.bot.loop.create_task(self._delayed_respawn(channel))

        # Message-count trigger only watches the spawn channel
        if message.channel.id != spawn_channel_id:
            return
        key = message.channel.id
        self._msg_counts[key] = self._msg_counts.get(key, 0) + 1
        if self._msg_counts[key] >= 15 and random.random() < 0.4:
            self._msg_counts[key] = 0
            if key not in self._spawn_cache:
                await self._spawn_wild(message.channel)

    # ══════════════════════════════════════════════════════════════════════════
    # COMMANDS
    # ══════════════════════════════════════════════════════════════════════════

    # ── Admin ─────────────────────────────────────────────────────────────────

    @commands.group(name="pokeset")
    @checks.admin_or_permissions(manage_guild=True)
    async def pokeset(self, ctx: commands.Context) -> None:
        """PokéBot admin settings."""

    @pokeset.command(name="spawnchannel")
    async def pokeset_spawnchannel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the channel where wild Pokémon will spawn."""
        await self.config.guild(ctx.guild).spawn_channel_id.set(channel.id)
        self._ensure_spawn_task(ctx.guild)
        await ctx.send(embed=success_embed(f"Spawn channel set to {channel.mention}!"))

    @pokeset.command(name="spawninterval")
    async def pokeset_spawninterval(self, ctx: commands.Context, seconds: int) -> None:
        """Set the automatic spawn interval in seconds (minimum 60)."""
        seconds = max(60, seconds)
        await self.config.guild(ctx.guild).spawn_interval.set(seconds)
        await ctx.send(embed=success_embed(f"Spawn interval set to {seconds}s."))

    @pokeset.command(name="fleetimeout")
    async def pokeset_fleetimeout(self, ctx: commands.Context, minutes: int) -> None:
        """Set how long a wild Pokémon stays before fleeing (minimum 5 minutes)."""
        minutes = max(5, minutes)
        seconds = minutes * 60
        await self.config.guild(ctx.guild).flee_timeout.set(seconds)
        await ctx.send(embed=success_embed(f"Flee timeout set to **{minutes} minutes**."))

    @pokeset.command(name="maxpokemon")
    async def pokeset_maxpokemon(self, ctx: commands.Context, limit: int) -> None:
        """Set the max Pokémon a trainer can hold (minimum 10, maximum 2000)."""
        limit = max(10, min(limit, 2000))
        await self.config.guild(ctx.guild).max_pokemon.set(limit)
        await ctx.send(embed=success_embed(f"Trainer collection limit set to **{limit} Pokémon**."))

    @pokeset.command(name="setprice")
    async def pokeset_setprice(self, ctx: commands.Context, item: str, price: int) -> None:
        """Set a custom shop price for an item. Usage: `pokeset setprice <item> <price>`
        Example: `pokeset setprice pokeball 75`"""
        item = item.lower().replace(" ", "").replace("-", "")
        shop_item = next((i for i in SHOP_ITEMS if i["id"] == item), None)
        if not shop_item:
            names = ", ".join(f"`{i['id']}`" for i in SHOP_ITEMS)
            await ctx.send(embed=error_embed(f"Unknown item. Available: {names}"))
            return
        price = max(1, price)
        prices = await self._get_shop_prices(ctx.guild)
        prices[item] = price
        await self.config.guild(ctx.guild).shop_prices.set(prices)
        currency = await _currency_name(ctx.guild)
        await ctx.send(embed=success_embed(
            f"Price for **{shop_item['emoji']} {shop_item['name']}** set to **{price}** {currency}."
        ))

    @pokeset.command(name="resetprices")
    async def pokeset_resetprices(self, ctx: commands.Context) -> None:
        """Reset all shop prices back to their defaults."""
        defaults = {i["id"]: i["price"] for i in SHOP_ITEMS}
        await self.config.guild(ctx.guild).shop_prices.set(defaults)
        await ctx.send(embed=success_embed("All shop prices have been reset to defaults."))

    @pokeset.command(name="showprices")
    async def pokeset_showprices(self, ctx: commands.Context) -> None:
        """Show current shop prices for this server."""
        prices   = await self._get_shop_prices(ctx.guild)
        currency = await _currency_name(ctx.guild)
        defaults = {i["id"]: i["price"] for i in SHOP_ITEMS}
        lines = []
        for item in SHOP_ITEMS:
            iid      = item["id"]
            current  = prices.get(iid, item["price"])
            modified = " ✏️" if current != defaults[iid] else ""
            lines.append(f"{item['emoji']} **{item['name']}** — {current} {currency}{modified}")
        embed = discord.Embed(
            title="🛒 Current Shop Prices",
            description="\n".join(lines) + "\n\n_✏️ = modified from default_",
            color=COLORS["blue"],
        )
        embed.set_footer(text="Use `pokeset setprice <item> <price>` to change · `pokeset resetprices` to reset all")
        await ctx.send(embed=embed)

    @pokeset.command(name="settmprice")
    async def pokeset_settmprice(self, ctx: commands.Context, tm: str, price: int) -> None:
        """Set a custom price for a TM. Usage: `pokeset settmprice <tm_slug> <price>`
        Example: `pokeset settmprice flamethrower 2000`
        Use `showtmprices` to see all TM slugs."""
        slug = tm.lower().strip()
        # Accept both slug and display name
        tm_info = TM_LIST.get(slug) or next(
            (info for s, info in TM_LIST.items() if info["name"].lower() == slug or s == slug),
            None,
        )
        if not tm_info:
            # Re-resolve slug from display name match
            slug = next(
                (s for s, info in TM_LIST.items() if info["name"].lower() == tm.lower()),
                None,
            )
            if slug:
                tm_info = TM_LIST[slug]
        if not tm_info or not slug:
            await ctx.send(embed=error_embed(
                f"Unknown TM **{tm}**. Use `pokeset showtmprices` to see valid TM slugs."
            ))
            return
        price = max(1, price)
        tm_prices = await self._get_tm_prices(ctx.guild)
        tm_prices[slug] = price
        await self.config.guild(ctx.guild).tm_prices.set(tm_prices)
        emoji = TM_TYPE_EMOJI.get(tm_info["type"], "💿")
        currency = await _currency_name(ctx.guild)
        await ctx.send(embed=success_embed(
            f"Price for {emoji} **TM {tm_info['name']}** set to **{price}** {currency}."
        ))

    @pokeset.command(name="resettmprices")
    async def pokeset_resettmprices(self, ctx: commands.Context) -> None:
        """Reset all TM prices back to their defaults."""
        defaults = {slug: info["price"] for slug, info in TM_LIST.items()}
        await self.config.guild(ctx.guild).tm_prices.set(defaults)
        await ctx.send(embed=success_embed("All TM prices have been reset to defaults."))

    @pokeset.command(name="showtmprices")
    async def pokeset_showtmprices(self, ctx: commands.Context) -> None:
        """Show current TM prices for this server."""
        tm_prices = await self._get_tm_prices(ctx.guild)
        defaults  = {slug: info["price"] for slug, info in TM_LIST.items()}
        currency  = await _currency_name(ctx.guild)
        lines = []
        for slug, info in TM_LIST.items():
            emoji    = TM_TYPE_EMOJI.get(info["type"], "💿")
            current  = tm_prices.get(slug, info["price"])
            modified = " ✏️" if current != defaults[slug] else ""
            lines.append(f"{emoji} **{info['name']}** (`{slug}`) — {current} {currency}{modified}")
        # Split into pages of 10 to avoid embed length limits
        page_size = 10
        pages = [lines[i:i+page_size] for i in range(0, len(lines), page_size)]
        for idx, page in enumerate(pages, 1):
            embed = discord.Embed(
                title=f"💿 TM Prices ({idx}/{len(pages)})",
                description="\n".join(page) + "\n\n_✏️ = modified from default_",
                color=COLORS["blue"],
            )
            embed.set_footer(text="Use `pokeset settmprice <slug> <price>` to change · `pokeset resettmprices` to reset all")
            await ctx.send(embed=embed)

    @commands.command(name="pokespawn")
    @checks.admin_or_permissions(manage_guild=True)
    async def pokespawn(self, ctx: commands.Context) -> None:
        """(Admin) Force spawn a wild Pokémon in the configured spawn channel."""
        channel_id = await self.config.guild(ctx.guild).spawn_channel_id()
        if not channel_id:
            await ctx.send(embed=error_embed("No spawn channel set! Use `pokeset spawnchannel #channel` first."))
            return
        channel = ctx.guild.get_channel(channel_id)
        if not channel:
            await ctx.send(embed=error_embed("Spawn channel not found — it may have been deleted. Use `pokeset spawnchannel` to set a new one."))
            return
        self._cancel_flee_task(channel.id)
        self._spawn_cache.pop(channel.id, None)
        await self._spawn_wild(channel)
        if channel != ctx.channel:
            await ctx.send(embed=success_embed(f"Spawned a wild Pokémon in {channel.mention}!"))


    @commands.command(name="pokedexsync")
    @checks.admin_or_permissions(manage_guild=True)
    async def pokedexsync(self, ctx: commands.Context) -> None:
        """(Admin) One-time migration: populate every trainer's Pokédex from their existing collection."""
        await ctx.send(embed=discord.Embed(
            color=COLORS["yellow"],
            description="⏳ Syncing Pokédex for all trainers... this may take a moment.",
        ))

        synced    = 0
        total_new = 0

        # Collect all members and their data first, then batch-save concurrently
        save_tasks = []
        for member in ctx.guild.members:
            if member.bot:
                continue
            player = await self._get_player(member)
            if not player or not player.get("pokemon"):
                continue

            dex     = set(player.get("caughtDex", []))
            before  = len(dex)

            for pk in player["pokemon"]:
                dex.add(pk["id"])

            new_entries = len(dex) - before
            if new_entries > 0:
                player["caughtDex"] = sorted(dex)
                save_tasks.append(self._save_player(member, player))
                total_new += new_entries
                synced    += 1

        if save_tasks:
            await asyncio.gather(*save_tasks)

        await ctx.send(embed=success_embed(
            f"Pokédex sync complete!\n"
            f"**{synced}** trainer(s) updated · **{total_new}** total new entries registered."
        ))

    # ── Start ─────────────────────────────────────────────────────────────────

    @commands.command(name="start")
    async def start(self, ctx: commands.Context) -> None:
        """Begin your Pokémon journey and choose a starter!"""
        player = await self._get_player(ctx.author)
        if player:
            await ctx.send(embed=error_embed("You already started your journey! Use `pokemon` to see your team."))
            return

        lines = []
        for gen in range(1, 10):
            trio = STARTERS[(gen - 1) * 3: gen * 3]
            lines.append(f"**Gen {gen}:** {', '.join(s['name'] for s in trio)}")

        embed = discord.Embed(
            title="🌟 Welcome to your Pokémon journey!",
            description="Choose your starter by typing its name below!\n\n" + "\n".join(lines),
            color=COLORS["yellow"],
        )
        await ctx.send(embed=embed)

        def check(m: discord.Message) -> bool:
            return (
                m.author == ctx.author
                and m.channel == ctx.channel
                and any(m.content.strip().lower() == s["name"].lower() for s in STARTERS)
            )

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60.0)
        except asyncio.TimeoutError:
            await ctx.send(embed=error_embed("Selection timed out. Use `start` again when ready!"))
            return

        chosen = next(s for s in STARTERS if s["name"].lower() == msg.content.strip().lower())
        async with ctx.typing():
            pokemon = await build_pokemon_instance(self._session, chosen["id"], level=5)
        pokemon["stats"]["hp"] = pokemon["stats"]["maxHp"]
        await self._create_player(ctx.author, pokemon)

        currency = await _currency_name(ctx.guild)
        embed = discord.Embed(
            title=f"🎉 You chose {pokemon['displayName']}!",
            description=(
                f"Welcome, **{ctx.author.display_name}**! Your journey begins!\n\n"
                f"You received:\n"
                f"• **{pokemon['displayName']}** (Lv.5)\n"
                f"• **10 Poké Balls**, 3 Great Balls, 1 Ultra Ball\n"
                f"• **500 {currency}** added to your bank\n\n"
                f"Use `pokehelp` to see all commands. Good luck!"
            ),
            color=COLORS["green"],
        )
        if pokemon.get("spriteUrl"):
            embed.set_image(url=pokemon["spriteUrl"])
        await ctx.send(embed=embed)

    # ── Profile ───────────────────────────────────────────────────────────────

    @commands.command(name="profile")
    async def profile(self, ctx: commands.Context, user: Optional[discord.Member] = None) -> None:
        """View your (or another trainer's) profile."""
        target = user or ctx.author
        player = await self._get_player(target)
        if not player:
            msg = (
                "You haven't started yet! Use `start`."
                if target == ctx.author
                else f"{target.display_name} hasn't started their journey yet."
            )
            await ctx.send(embed=error_embed(msg))
            return

        active   = player["pokemon"][player["activePokemonIndex"]] if player["pokemon"] else None
        shinies  = sum(1 for p in player["pokemon"] if p.get("shiny"))
        total    = player["wins"] + player["losses"]
        win_rate = f"{(player['wins'] / total * 100):.1f}" if total else "0.0"
        balance  = await _get_balance(target)
        currency = await _currency_name(ctx.guild)

        embed = discord.Embed(
            title=f"🎒 {target.display_name}'s Trainer Profile",
            color=COLORS["purple"],
        )
        if active and active.get("spriteUrl"):
            embed.set_thumbnail(url=active["spriteUrl"])

        embed.add_field(name=f"💰 {currency}", value=str(balance), inline=True)
        dex_count = len(player.get("caughtDex", []))
        embed.add_field(name="📦 Pokémon",     value=str(len(player["pokemon"])), inline=True)
        embed.add_field(name="📖 Pokédex",     value=f"{dex_count}/{MAX_POKEMON}", inline=True)
        embed.add_field(name="✨ Shinies",      value=str(shinies), inline=True)
        embed.add_field(name="⚔️ Battles",     value=f"{player['wins']}W / {player['losses']}L ({win_rate}%)", inline=True)
        embed.add_field(
            name="🎯 Active Pokémon",
            value=(
                f"{active['displayName']}{'  ✨' if active.get('shiny') else ''} (Lv.{active['level']})"
                if active else "None"
            ),
            inline=True,
        )
        balls = player["items"]
        embed.add_field(
            name="🎒 Balls",
            value=f"Poké: {balls.get('pokeball', 0)} · Great: {balls.get('greatball', 0)} · Ultra: {balls.get('ultraball', 0)}",
            inline=False,
        )
        reg = datetime.fromtimestamp(player["registeredAt"], tz=timezone.utc).strftime("%Y-%m-%d")
        embed.set_footer(text=f"Trainer since {reg}")
        await ctx.send(embed=embed)

    # ── Pokemon list ──────────────────────────────────────────────────────────

    @commands.command(name="pokemon")
    async def pokemon_list(self, ctx: commands.Context, page: int = 1, user: Optional[discord.Member] = None) -> None:
        """View your Pokémon collection. Usage: `pokemon [page] [@user]`"""
        target = user or ctx.author
        player = await self._get_player(target)
        if not player:
            msg = (
                "You haven't started your journey yet! Use `start`."
                if target == ctx.author
                else f"{target.display_name} hasn't started their journey yet."
            )
            await ctx.send(embed=error_embed(msg))
            return

        per_page = 6
        total    = len(player["pokemon"])
        pages    = max(1, math.ceil(total / per_page))
        page     = max(1, min(page, pages))
        offset   = (page - 1) * per_page
        chunk    = player["pokemon"][offset: offset + per_page]

        embed = discord.Embed(
            title=f"{target.display_name}'s Pokémon ({total} total)",
            color=COLORS["blue"],
        )
        embed.set_footer(
            text=f"Page {page}/{pages} · Active: #{player['activePokemonIndex'] + 1} | Use `pokemon <page>` to navigate"
        )

        for i, p in enumerate(chunk):
            idx    = offset + i + 1
            active = " ⬅ Active" if idx - 1 == player["activePokemonIndex"] else ""
            shiny  = " ✨" if p.get("shiny") else ""
            nick   = f' "{p["nickname"]}"' if p.get("nickname") else ""
            embed.add_field(
                name=f"#{idx} {p['displayName']}{shiny}{nick}{active}",
                value=f"Lv.{p['level']} | {' / '.join(type_tag(t) for t in p['types'])} | HP: {p['stats']['hp']}/{p['stats']['maxHp']}",
                inline=False,
            )

        await ctx.send(embed=embed)

    # ── Active ────────────────────────────────────────────────────────────────

    @commands.command(name="active")
    async def active(self, ctx: commands.Context, slot: int) -> None:
        """Switch your active Pokémon for battles. Slot number from `pokemon` list."""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey with `start`!"))
            return
        if self._get_battle_by_user(ctx.author.id):
            await ctx.send(embed=error_embed("You can't switch Pokémon during a battle!"))
            return

        idx = slot - 1
        if idx < 0 or idx >= len(player["pokemon"]):
            await ctx.send(embed=error_embed(
                f"Invalid slot. You have {len(player['pokemon'])} Pokémon (slots 1–{len(player['pokemon'])})."
            ))
            return

        poke = player["pokemon"][idx]
        if poke["stats"]["hp"] <= 0:
            await ctx.send(embed=error_embed(
                f"**{poke['displayName']}** has fainted and can't battle! "
                f"Use `use revive {idx + 1}` to revive it first."
            ))
            return
        player["activePokemonIndex"] = idx
        await self._save_player(ctx.author, player)
        embed = pokemon_embed(poke, f"✅ Switched to {poke['displayName']}!", show_xp=True)
        await ctx.send(embed=embed)

    # ── Nickname ──────────────────────────────────────────────────────────────

    @commands.command(name="nickname")
    async def nickname(self, ctx: commands.Context, slot: int, *, name: str) -> None:
        """Give a Pokémon a nickname. Usage: `nickname <slot> <name>`"""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey with `start`!"))
            return
        idx = slot - 1
        if idx < 0 or idx >= len(player["pokemon"]):
            await ctx.send(embed=error_embed(f"Invalid slot. You have {len(player['pokemon'])} Pokémon."))
            return
        name = name.strip()[:20]
        player["pokemon"][idx]["nickname"] = name
        await self._save_player(ctx.author, player)
        await ctx.send(embed=success_embed(
            f"{player['pokemon'][idx]['displayName']} is now nicknamed **{name}**!"
        ))

    # ── Dex ───────────────────────────────────────────────────────────────────

    @commands.command(name="dex")
    async def dex(self, ctx: commands.Context, *, query: str) -> None:
        """Look up a Pokémon in the Pokédex. Usage: `dex <name or number>`"""
        async with ctx.typing():
            try:
                raw = await fetch_pokemon(self._session, query.lower().strip())
            except Exception:
                await ctx.send(embed=error_embed(f"Couldn't find a Pokémon called **{query}**. Check the spelling!"))
                return

        types_str   = " / ".join(type_tag(t["type"]["name"]) for t in raw["types"])
        stats_lines = []
        for s in raw["stats"]:
            name = s["stat"]["name"].replace("-", " ").title()
            bar  = "█" * round(s["base_stat"] / 15) + "░" * max(0, 10 - round(s["base_stat"] / 15))
            stats_lines.append(f"**{name}**: {bar} {s['base_stat']}")

        abilities = ", ".join(
            a["ability"]["name"].replace("-", " ").title() + (" _(hidden)_" if a["is_hidden"] else "")
            for a in raw["abilities"]
        )

        embed = discord.Embed(
            title=f"#{raw['id']} — {raw['name'].capitalize()}",
            color=COLORS["blue"],
        )
        if raw["sprites"]["front_default"]:
            embed.set_thumbnail(url=raw["sprites"]["front_default"])
        official = raw["sprites"].get("other", {}).get("official-artwork", {})
        if official and official.get("front_default"):
            embed.set_image(url=official["front_default"])

        embed.add_field(name="Type",           value=types_str, inline=True)
        embed.add_field(name="Height / Weight", value=f"{raw['height']/10}m / {raw['weight']/10}kg", inline=True)
        embed.add_field(name="Abilities",       value=abilities, inline=False)
        embed.add_field(name="Base Stats",      value="\n".join(stats_lines), inline=False)
        embed.set_footer(text="Shiny sprite available in-game ✨")
        await ctx.send(embed=embed)

    # ── Catch ─────────────────────────────────────────────────────────────────

    @commands.command(name="catch")
    async def catch(self, ctx: commands.Context, ball: str = "pokeball") -> None:
        """Catch a wild Pokémon! Usage: `catch [pokeball|greatball|ultraball]`"""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return

        spawn = self._spawn_cache.get(ctx.channel.id)
        if not spawn:
            await ctx.send(embed=error_embed("There's no wild Pokémon here right now!"))
            return

        ball = ball.lower().replace(" ", "").replace("-", "")
        if ball not in BALL_NAMES:
            await ctx.send(embed=error_embed("Unknown ball type. Use: `pokeball`, `greatball`, or `ultraball`."))
            return

        ball_count = player["items"].get(ball, 0)
        if ball_count <= 0:
            await ctx.send(embed=error_embed(f"You don't have any {BALL_NAMES[ball]}s! Buy some with `shop`."))
            return

        # Check collection cap before consuming the ball
        max_pokemon = await self.config.guild(ctx.guild).max_pokemon()
        if len(player["pokemon"]) >= max_pokemon:
            await ctx.send(embed=error_embed(
                f"Your collection is full! (**{len(player['pokemon'])}/{max_pokemon}** Pokémon)\n"
                "Use `release <slot>` to release one before catching more."
            ))
            return

        player["items"][ball] -= 1
        pokemon = spawn["pokemon"]
        chance  = catch_rate(pokemon, ball)
        caught  = random.random() < chance

        shakes     = 3 if caught else random.randint(0, 2)
        shake_text = "🔴 *shake*... " * shakes

        # Wild Pokémon fights back — deals damage to the active Pokémon
        active_poke = player["pokemon"][player["activePokemonIndex"]]
        wild_level  = pokemon["level"]
        scratch_dmg = max(1, random.randint(
            math.floor(wild_level * 0.5),
            math.floor(wild_level * 1.5)
        ))
        active_poke["stats"]["hp"] = max(0, active_poke["stats"]["hp"] - scratch_dmg)
        fainted_from_catch = active_poke["stats"]["hp"] <= 0

        async with ctx.typing():
            await asyncio.sleep(1.5)

        currency = await _currency_name(ctx.guild)

        if caught:
            # Pokémon caught — cancel flee timer and remove from spawn cache
            self._cancel_flee_task(ctx.channel.id)
            self._spawn_cache.pop(ctx.channel.id, None)
            # Schedule a fresh spawn after a short delay so the channel never goes dead
            self.bot.loop.create_task(self._delayed_respawn(ctx.channel))

            credits_earned = 500 if pokemon.get("shiny") else (100 if pokemon["level"] >= 30 else 50)
            is_new_dex = self._update_dex(player, pokemon)
            player["pokemon"].append({**pokemon, "caughtAt": time.time()})
            await self._save_player(ctx.author, player)
            await _deposit(ctx.author, credits_earned)

            bonus_tag = (
                " ✨ Shiny bonus!" if pokemon.get("shiny")
                else (" 💪 High level bonus!" if pokemon["level"] >= 30 else "")
            )
            hp_note = (
                f" | ⚠️ {active_poke['displayName']} fainted! Use a Revive."
                if fainted_from_catch
                else f" | {active_poke['displayName']} took {scratch_dmg} dmg ({active_poke['stats']['hp']}/{active_poke['stats']['maxHp']} HP)"
            )
            embed = pokemon_embed(
                pokemon,
                f"{ctx.author.display_name} caught {pokemon['displayName']}{'  ✨' if pokemon.get('shiny') else ''}!",
                footer=f"{shake_text}Gotcha! Added to your collection.{hp_note}",
            )
            embed.color = COLORS["shiny"] if pokemon.get("shiny") else COLORS["green"]
            embed.add_field(
                name=f"💰 {currency} Earned",
                value=f"+{credits_earned}{bonus_tag}",
                inline=True,
            )
            if is_new_dex:
                dex_count = len(player.get("caughtDex", []))
                embed.add_field(
                    name="📖 Pokédex",
                    value=f"New entry! ({dex_count}/{MAX_POKEMON} caught)",
                    inline=True,
                )
        else:
            await self._save_player(ctx.author, player)  # saves HP damage from wild pokemon
            hp_note = (
                f"\n⚠️ **{active_poke['displayName']}** fainted! Use `use revive` before battling."
                if fainted_from_catch
                else f"\n{active_poke['displayName']} took **{scratch_dmg} damage** ({active_poke['stats']['hp']}/{active_poke['stats']['maxHp']} HP remaining)"
            )
            embed = discord.Embed(
                title=f"Oh no! {pokemon['displayName']} broke free!",
                description=(
                    f"{shake_text}💨 {pokemon['displayName']} escaped!\n"
                    f"{hp_note}\n\n"
                    f"_{BALL_NAMES[ball]}s remaining: {player['items'][ball]}_"
                ),
                color=COLORS["red"],
            )
            if pokemon.get("spriteUrl"):
                embed.set_thumbnail(url=pokemon["spriteUrl"])

        await ctx.send(embed=embed)


    # ── Release ───────────────────────────────────────────────────────────────

    @commands.command(name="release")
    async def release(self, ctx: commands.Context, slot: int) -> None:
        """Release a Pokémon from your collection for a reward. Usage: `release <slot>`"""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey with `start`!"))
            return
        if self._get_battle_by_user(ctx.author.id):
            await ctx.send(embed=error_embed("You can't release Pokémon during a battle!"))
            return

        idx = slot - 1
        if idx < 0 or idx >= len(player["pokemon"]):
            await ctx.send(embed=error_embed(
                f"Invalid slot. You have {len(player['pokemon'])} Pokémon "
                f"(slots 1\u2013{len(player['pokemon'])})."
            ))
            return
        if len(player["pokemon"]) <= 1:
            await ctx.send(embed=error_embed("You can't release your last Pokémon!"))
            return

        poke     = player["pokemon"][idx]
        currency = await _currency_name(ctx.guild)

        # Rewards scale with level; shinies get a 3× bonus
        base_credits = poke["level"] * 10
        if poke.get("shiny"):
            base_credits = int(base_credits * 3)

        # Random ball reward — better balls are rarer
        ball_chance = random.random()
        if ball_chance < 0.10:
            ball_reward = "ultraball"
        elif ball_chance < 0.35:
            ball_reward = "greatball"
        else:
            ball_reward = "pokeball"
        ball_qty = random.randint(1, 3)

        ball_emoji = {"pokeball": "🔴", "greatball": "🔵", "ultraball": "⚫"}[ball_reward]
        shiny_tag  = " ✨ SHINY" if poke.get("shiny") else ""
        nick_tag   = f' "{poke["nickname"]}"' if poke.get("nickname") else ""

        confirm_embed = discord.Embed(
            title=f"❓ Release {poke['displayName']}{shiny_tag}{nick_tag}?",
            description=(
                f"Are you sure you want to release **{poke['displayName']}** "
                f"(Lv.{poke['level']}){shiny_tag}?\n\n"
                f"You will receive:\n"
                f"💰 **{base_credits} {currency}**\n"
                f"{ball_emoji} **{ball_qty}\u00d7 {BALL_NAMES[ball_reward]}**\n\n"
                "Type `yes` to confirm or `no` to cancel."
            ),
            color=COLORS["orange"],
        )
        if poke.get("spriteUrl"):
            confirm_embed.set_thumbnail(url=poke["spriteUrl"])
        await ctx.send(embed=confirm_embed)

        def check(m: discord.Message) -> bool:
            return (
                m.author == ctx.author
                and m.channel == ctx.channel
                and m.content.lower().strip() in ("yes", "no")
            )

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=30.0)
        except asyncio.TimeoutError:
            await ctx.send(embed=error_embed("Release cancelled \u2014 no response in 30 seconds."))
            return

        if msg.content.lower().strip() == "no":
            await ctx.send(embed=discord.Embed(
                color=COLORS["gray"],
                description=f"Kept **{poke['displayName']}** \u2014 good choice!",
            ))
            return

        # Apply rewards and remove the Pokémon
        player["pokemon"].pop(idx)

        # Fix active index if it now points past the end or at the released slot
        active = player["activePokemonIndex"]
        if active >= len(player["pokemon"]) or active == idx:
            player["activePokemonIndex"] = 0

        items = player.setdefault("items", {"pokeball": 0, "greatball": 0, "ultraball": 0, "healing": {}})
        items[ball_reward] = items.get(ball_reward, 0) + ball_qty
        await self._save_player(ctx.author, player)
        await _deposit(ctx.author, base_credits)

        embed = discord.Embed(
            title=f"👋 {poke['displayName']} was released!",
            description=(
                f"**{poke['displayName']}** was set free into the wild.\n\n"
                f"**Rewards received:**\n"
                f"💰 **+{base_credits} {currency}**\n"
                f"{ball_emoji} **+{ball_qty}\u00d7 {BALL_NAMES[ball_reward]}**"
                + ("\n\n✨ _Shiny bonus applied!_" if poke.get("shiny") else "")
            ),
            color=COLORS["green"],
        )
        if poke.get("spriteUrl"):
            embed.set_thumbnail(url=poke["spriteUrl"])
        embed.set_footer(text=f"Collection: {len(player['pokemon'])} Pokémon remaining")
        await ctx.send(embed=embed)

    # ── Inventory ─────────────────────────────────────────────────────────────

    @commands.command(name="inventory", aliases=["inv", "bag"])
    async def inventory(self, ctx: commands.Context) -> None:
        """View your bag — balls, healing items, and bank balance."""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return

        items   = player.get("items", {})
        healing = items.get("healing", {})
        tms     = items.get("tms", [])
        balance  = await _get_balance(ctx.author)
        currency = await _currency_name(ctx.guild)

        balls_lines = []
        for ball_id, label in BALL_NAMES.items():
            count = items.get(ball_id, 0)
            emoji = next((i["emoji"] for i in SHOP_ITEMS if i["id"] == ball_id), "🔴")
            balls_lines.append(f"{emoji} **{label}** — {count}")

        heal_lines = []
        for item_id, label in ITEM_NAMES.items():
            count = healing.get(item_id, 0)
            heal_lines.append(f"{label} — {count}")

        embed = discord.Embed(
            title=f"🎒 {ctx.author.display_name}'s Bag",
            color=COLORS["blue"],
        )
        embed.add_field(name=f"💰 {currency}", value=str(balance), inline=False)
        embed.add_field(name="🎯 Poké Balls",   value="\n".join(balls_lines), inline=True)
        embed.add_field(name="💊 Healing Items", value="\n".join(heal_lines), inline=True)

        if tms:
            tm_lines = []
            for slug in tms:
                tm_info = TM_LIST.get(slug)
                if tm_info:
                    emoji = TM_TYPE_EMOJI.get(tm_info["type"], "💿")
                    tm_lines.append(f"{emoji} **{tm_info['name']}** `{slug}`")
                else:
                    tm_lines.append(f"💿 `{slug}`")
            embed.add_field(name="💿 TMs", value="\n".join(tm_lines), inline=False)
        else:
            embed.add_field(name="💿 TMs", value="_None — buy TMs with `buytm <move>`_", inline=False)

        embed.set_footer(text="Use `shop` to buy items • `tms` to browse TMs • `usetm` to teach moves")
        await ctx.send(embed=embed)

    # ── Shop ──────────────────────────────────────────────────────────────────

    @commands.command(name="shop")
    async def shop(self, ctx: commands.Context) -> None:
        """Browse the PokéMart."""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return

        balance  = await _get_balance(ctx.author)
        currency = await _currency_name(ctx.guild)
        prices   = await self._get_shop_prices(ctx.guild)

        balls_str = "\n\n".join(
            f"{i['emoji']} **{i['name']}** — {prices.get(i['id'], i['price'])} {currency}\n_{i['desc']}_"
            for i in SHOP_ITEMS if i["category"] == "balls"
        )
        heal_str = "\n\n".join(
            f"{i['emoji']} **{i['name']}** — {prices.get(i['id'], i['price'])} {currency}\n_{i['desc']}_"
            for i in SHOP_ITEMS if i["category"] == "healing"
        )
        embed = discord.Embed(
            title="🛒 PokéMart",
            description=f"Your balance: **💰 {balance} {currency}**\n\nUse `buy <item> [amount]` to purchase.",
            color=COLORS["yellow"],
        )
        embed.add_field(name="🎯 Poké Balls",   value=balls_str, inline=False)
        embed.add_field(name="💊 Healing Items", value=heal_str,  inline=False)
        embed.set_footer(text="Win battles and catch Pokémon to earn more!")
        await ctx.send(embed=embed)

    # ── Buy ───────────────────────────────────────────────────────────────────

    @commands.command(name="buy")
    async def buy(self, ctx: commands.Context, item: str, amount: int = 1) -> None:
        """Buy items from the PokéMart. Usage: `buy <item> [amount]`"""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return

        item = item.lower().replace(" ", "").replace("-", "")
        shop_item = next((i for i in SHOP_ITEMS if i["id"] == item), None)
        if not shop_item:
            names = ", ".join(f"`{i['id']}`" for i in SHOP_ITEMS)
            await ctx.send(embed=error_embed(f"Unknown item. Available: {names}"))
            return

        amount     = max(1, min(amount, 99))
        prices     = await self._get_shop_prices(ctx.guild)
        item_price = prices.get(shop_item["id"], shop_item["price"])
        total_cost = item_price * amount
        currency   = await _currency_name(ctx.guild)

        success = await _withdraw(ctx.author, total_cost)
        if not success:
            balance = await _get_balance(ctx.author)
            await ctx.send(embed=error_embed(
                f"You need **{total_cost} {currency}** for {amount}× {shop_item['name']} "
                f"but only have **{balance}**."
            ))
            return

        if shop_item["category"] == "balls":
            player["items"][item] = player["items"].get(item, 0) + amount
        else:
            if "healing" not in player["items"]:
                player["items"]["healing"] = {}
            player["items"]["healing"][item] = player["items"]["healing"].get(item, 0) + amount

        await self._save_player(ctx.author, player)
        balance = await _get_balance(ctx.author)
        await ctx.send(embed=success_embed(
            f"Bought **{amount}× {shop_item['emoji']} {shop_item['name']}** "
            f"for **{total_cost} {currency}**!\n"
            f"Remaining balance: **{balance} {currency}**"
        ))

    # ── Use ───────────────────────────────────────────────────────────────────

    @commands.command(name="use")
    async def use(self, ctx: commands.Context, item: str, slot: int = 0) -> None:
        """Use a healing item. Usage: `use <item> [slot]`"""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return
        if self._get_battle_by_user(ctx.author.id):
            await ctx.send(embed=error_embed("You can't use items during a battle!"))
            return

        item = item.lower().replace(" ", "").replace("-", "")
        if item not in HEAL_AMOUNTS:
            await ctx.send(embed=error_embed(f"Unknown item. Valid: {', '.join(HEAL_AMOUNTS.keys())}"))
            return

        idx = (slot - 1) if slot > 0 else player["activePokemonIndex"]
        if idx < 0 or idx >= len(player["pokemon"]):
            await ctx.send(embed=error_embed(f"Invalid slot. You have {len(player['pokemon'])} Pokémon."))
            return

        count = player["items"].get("healing", {}).get(item, 0)
        if count <= 0:
            await ctx.send(embed=error_embed(f"You don't have any {ITEM_NAMES[item]}s! Buy some with `shop`."))
            return

        poke = player["pokemon"][idx]

        if item == "revive":
            if poke["stats"]["hp"] > 0:
                await ctx.send(embed=error_embed(
                    f"{poke['displayName']} hasn't fainted — Revive only works on fainted Pokémon!"
                ))
                return
            poke["stats"]["hp"] = math.floor(poke["stats"]["maxHp"] / 2)
        else:
            if poke["stats"]["hp"] <= 0:
                await ctx.send(embed=error_embed(f"{poke['displayName']} has fainted! Use a ⭐ Revive first."))
                return
            if poke["stats"]["hp"] >= poke["stats"]["maxHp"]:
                await ctx.send(embed=error_embed(f"{poke['displayName']} is already at full HP!"))
                return
            heal = HEAL_AMOUNTS[item]
            poke["stats"]["hp"] = (
                poke["stats"]["maxHp"]
                if math.isinf(heal)
                else min(poke["stats"]["maxHp"], poke["stats"]["hp"] + heal)
            )

        player["items"]["healing"][item] -= 1
        await self._save_player(ctx.author, player)

        bar  = hp_bar(poke["stats"]["hp"], poke["stats"]["maxHp"])
        desc = (
            f"⭐ **{poke['displayName']}** was revived!\nHP: {bar} {poke['stats']['hp']}/{poke['stats']['maxHp']}"
            if item == "revive"
            else f"💊 **{poke['displayName']}** recovered HP!\nHP: {bar} {poke['stats']['hp']}/{poke['stats']['maxHp']}"
        )
        embed = discord.Embed(title=f"Used {ITEM_NAMES[item]}!", description=desc, color=COLORS["green"])
        if poke.get("spriteUrl"):
            embed.set_thumbnail(url=poke["spriteUrl"])
        embed.set_footer(text=f"{ITEM_NAMES[item]}s remaining: {player['items']['healing'][item]}")
        await ctx.send(embed=embed)

    # ── Battle ────────────────────────────────────────────────────────────────

    @commands.command(name="battle")
    async def battle(self, ctx: commands.Context, opponent: discord.Member) -> None:
        """Challenge another trainer to a Pokémon battle! Usage: `battle @user`"""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return
        if opponent == ctx.author:
            await ctx.send(embed=error_embed("You can't battle yourself!"))
            return
        if opponent.bot:
            await ctx.send(embed=error_embed("You can't battle a bot!"))
            return

        opp_data = await self._get_player(opponent)
        if not opp_data:
            await ctx.send(embed=error_embed(f"{opponent.display_name} hasn't started their journey yet!"))
            return
        if self._get_battle_by_user(ctx.author.id):
            await ctx.send(embed=error_embed("You're already in a battle!"))
            return
        if self._get_battle_by_user(opponent.id):
            await ctx.send(embed=error_embed(f"{opponent.display_name} is already in a battle!"))
            return

        self._challenges[opponent.id] = {
            "challengerId":   ctx.author.id,
            "challengerName": ctx.author.display_name,
            "channelId":      ctx.channel.id,
            "expires":        time.time() + 60,
        }

        challenger_poke = player["pokemon"][player["activePokemonIndex"]]
        opp_poke        = opp_data["pokemon"][opp_data["activePokemonIndex"]]

        if challenger_poke["stats"]["hp"] <= 0:
            await ctx.send(embed=error_embed(
                f"**{challenger_poke['displayName']}** has fainted! Heal it or switch active Pokémon before battling."
            ))
            return
        if opp_poke["stats"]["hp"] <= 0:
            await ctx.send(embed=error_embed(
                f"{opponent.display_name}'s **{opp_poke['displayName']}** has fainted — they need to heal up first!"
            ))
            return

        embed = discord.Embed(
            title="⚔️ Battle Challenge!",
            description=(
                f"**{ctx.author.display_name}** challenges **{opponent.display_name}** to a battle!\n\n"
                f"🔴 {ctx.author.display_name}'s **{challenger_poke['displayName']}** (Lv.{challenger_poke['level']})\n"
                f"🔵 {opponent.display_name}'s **{opp_poke['displayName']}** (Lv.{opp_poke['level']})\n\n"
                f"{opponent.mention}, type `accept` or `decline` to respond!"
            ),
            color=COLORS["orange"],
        )
        await ctx.send(embed=embed)

        def check(m: discord.Message) -> bool:
            return (
                m.author == opponent
                and m.channel == ctx.channel
                and m.content.lower().strip() in ("accept", "decline")
            )

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=60.0)
        except asyncio.TimeoutError:
            self._challenges.pop(opponent.id, None)
            await ctx.send(embed=error_embed(f"{opponent.display_name} didn't respond in time. Challenge expired."))
            return

        challenge = self._challenges.pop(opponent.id, None)
        if not challenge or time.time() > challenge["expires"]:
            await ctx.send(embed=error_embed("Challenge expired."))
            return

        if msg.content.lower().strip() == "decline":
            await ctx.send(embed=discord.Embed(
                color=COLORS["gray"],
                description=f"❌ {opponent.display_name} declined the battle challenge.",
            ))
            return

        # Build the battle
        p1_pokemon = copy.deepcopy(player["pokemon"][player["activePokemonIndex"]])
        p2_pokemon = copy.deepcopy(opp_data["pokemon"][opp_data["activePokemonIndex"]])

        battle_id = f"{ctx.author.id}_{opponent.id}_{int(time.time())}"
        battle = {
            "player1":   {"id": ctx.author.id, "username": ctx.author.display_name, "pokemon": p1_pokemon, "moveUsed": None, "lastMoveAt": time.time()},
            "player2":   {"id": opponent.id,   "username": opponent.display_name,   "pokemon": p2_pokemon, "moveUsed": None, "lastMoveAt": time.time()},
            "turn":      1,
            "log":       [],
            "status":    "active",
            "startedAt": time.time(),
            "guildId":   ctx.guild.id,
            "channelId": ctx.channel.id,
        }
        self._battles[battle_id] = battle

        # Start the AFK timeout watcher
        self.bot.loop.create_task(self._battle_timeout_watcher(ctx.guild, battle_id, ctx.channel))

        embed  = self._build_battle_embed(battle, ["The battle begins! Both trainers, use `move <move_name>` to fight!"])
        moves1 = " · ".join(m.replace("-", " ").capitalize() for m in p1_pokemon["moves"])
        moves2 = " · ".join(m.replace("-", " ").capitalize() for m in p2_pokemon["moves"])
        await ctx.send(
            content=(
                f"{ctx.author.mention}'s moves: {moves1}\n"
                f"{opponent.mention}'s moves: {moves2}"
            ),
            embed=embed,
        )

    async def _battle_timeout_watcher(
        self, guild: discord.Guild, battle_id: str, channel: discord.TextChannel
    ) -> None:
        """Periodically check if a battle has gone AFK and auto-forfeit if so."""
        while True:
            await asyncio.sleep(60)  # Check every minute
            battle = self._battles.get(battle_id)
            if not battle or battle["status"] != "active":
                return

            now  = time.time()
            p1   = battle["player1"]
            p2   = battle["player2"]
            # Check whichever player has been idle longest
            afk  = None
            other = None
            if not p1["moveUsed"] and (now - p1["lastMoveAt"]) >= BATTLE_TIMEOUT:
                afk, other = p1, p2
            elif not p2["moveUsed"] and (now - p2["lastMoveAt"]) >= BATTLE_TIMEOUT:
                afk, other = p2, p1

            if afk:
                battle["status"] = "finished"
                await self._end_battle(guild, battle_id, other["id"], afk["id"])
                try:
                    embed = discord.Embed(
                        title="⏰ Battle Timeout!",
                        description=(
                            f"**{afk['username']}** took too long to respond and forfeited!\n"
                            f"**{other['username']}** wins by default and receives 100 {await _currency_name(guild)}."
                        ),
                        color=COLORS["orange"],
                    )
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    pass
                return

    # ── Move ──────────────────────────────────────────────────────────────────

    @commands.command(name="move")
    async def move(self, ctx: commands.Context, *, move_name: str) -> None:
        """Use a move in your current battle. Usage: `move <move_name>`"""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey with `start`!"))
            return

        result = self._get_battle_by_user(ctx.author.id)
        if not result:
            await ctx.send(embed=error_embed("You're not in a battle! Use `battle @user` to start one."))
            return

        battle_id, battle = result
        move_name = move_name.lower().replace(" ", "-")
        is_p1     = battle["player1"]["id"] == ctx.author.id
        my_side   = battle["player1"] if is_p1 else battle["player2"]

        if move_name not in my_side["pokemon"]["moves"]:
            move_list = ", ".join(f"`{m}`" for m in my_side["pokemon"]["moves"])
            await ctx.send(embed=error_embed(
                f"{my_side['pokemon']['displayName']} doesn't know **{move_name}**!\nKnown moves: {move_list}"
            ))
            return

        if my_side["moveUsed"]:
            await ctx.send(embed=error_embed("You already chose a move this turn! Waiting for your opponent..."))
            return

        my_side["moveUsed"]    = move_name
        my_side["lastMoveAt"]  = time.time()   # Reset AFK timer on move submission
        await ctx.send(embed=discord.Embed(
            color=COLORS["purple"],
            description=f"⚔️ **{my_side['pokemon']['displayName']}** is ready to use **{move_name.replace('-', ' ')}**! Waiting for opponent...",
        ))

        if battle["player1"]["moveUsed"] and battle["player2"]["moveUsed"]:
            turn_result = await self._process_turn(battle_id)
            if not turn_result:
                log.error(
                    "[PokéBot] _process_turn returned None for battle %s — "
                    "p1.moveUsed=%s p2.moveUsed=%s",
                    battle_id,
                    battle["player1"].get("moveUsed"),
                    battle["player2"].get("moveUsed"),
                )
                await ctx.send(embed=error_embed(
                    "Something went wrong processing this turn. "
                    "The battle has been cancelled — please start a new one."
                ))
                self._battles.pop(battle_id, None)
                return

            updated_battle, turn_log, winner = turn_result
            embed = self._build_battle_embed(updated_battle, turn_log)

            if winner:
                loser_id = (
                    battle["player2"]["id"]
                    if winner["id"] == battle["player1"]["id"]
                    else battle["player1"]["id"]
                )
                await self._end_battle(ctx.guild, battle_id, winner["id"], loser_id)
                currency       = await _currency_name(ctx.guild)
                embed.color    = COLORS["yellow"]
                embed.title    = f"🏆 {winner['username']} wins the battle!"
                embed.set_footer(text=f"{winner['username']} earned 100 {currency} and battle XP!")

            await ctx.send(embed=embed)

    # ── Pokédex ───────────────────────────────────────────────────────────────

    @commands.command(name="pokedex", aliases=["pdex"])
    async def pokedex(self, ctx: commands.Context, page: int = 1, user: Optional[discord.Member] = None) -> None:
        """View your Pokédex — species you've caught. Usage: `pokedex [page] [@user]`"""
        target = user or ctx.author
        player = await self._get_player(target)
        if not player:
            msg = (
                "You haven't started your journey yet! Use `start`."
                if target == ctx.author
                else f"{target.display_name} hasn't started their journey yet."
            )
            await ctx.send(embed=error_embed(msg))
            return

        caught_ids   = set(player.get("caughtDex", []))
        total_caught = len(caught_ids)
        completion   = (total_caught / MAX_POKEMON) * 100

        if not caught_ids:
            await ctx.send(embed=discord.Embed(
                color=COLORS["orange"],
                description="📭 No Pokémon in your Pokédex yet!\nHead out and start catching to fill it up.",
            ))
            return

        # Build name map from the player's current collection
        seen: Dict[int, str] = {}
        for pk in player["pokemon"]:
            if pk["id"] in caught_ids and pk["id"] not in seen:
                seen[pk["id"]] = pk["displayName"]

        all_entries = [(pid, seen.get(pid, f"#{pid}")) for pid in sorted(caught_ids)]
        per_page    = 30
        total_pages = max(1, math.ceil(total_caught / per_page))
        page        = max(1, min(page, total_pages))
        offset      = (page - 1) * per_page
        chunk       = all_entries[offset:offset + per_page]

        rank_label, rank_color = _dex_rank(total_caught)
        prog_bar  = _dex_progress_bar(total_caught, MAX_POKEMON, length=20)
        remaining = MAX_POKEMON - total_caught

        # Milestone flavour text (only on page 1)
        if page == 1:
            if completion >= 100:
                flavour = "🎉 You've caught them all! Legendary!"
            elif completion >= 75:
                flavour = f"🔥 Almost there — just **{remaining}** left!"
            elif completion >= 50:
                flavour = "💪 Halfway there — keep it up!"
            elif completion >= 25:
                flavour = f"🌱 Making good progress — **{remaining}** still out there!"
            else:
                flavour = f"🗺️ Your journey is just beginning — **{remaining}** left to discover!"
        else:
            flavour = ""

        title = (
            f"📖 {target.display_name}'s Pokédex"
            if total_pages == 1
            else f"📖 {target.display_name}'s Pokédex — Page {page}/{total_pages}"
        )
        embed = discord.Embed(title=title, color=rank_color)
        embed.description = (
            f"{rank_label}\n\n"
            f"{prog_bar}\n"
            f"**{total_caught}** / **{MAX_POKEMON}** — {completion:.1f}% complete"
            + (f"\n\n{flavour}" if flavour else "")
        )

        # Pokémon entries in up to 3 inline columns of 10
        col_size = 10
        cols = [chunk[i:i + col_size] for i in range(0, len(chunk), col_size)]
        for col in cols:
            val = "\n".join(f"🔵 `#{pid:04d}` {name}" for pid, name in col)
            embed.add_field(name="\u200b", value=val, inline=True)

        footer = f"Page {page}/{total_pages} · pokedex <page> to navigate · {remaining} species still to find!"
        embed.set_footer(text=footer)
        await ctx.send(embed=embed)

    @commands.command(name="dexpage", aliases=["dp"])
    async def dexpage(self, ctx: commands.Context, page: int = 1, user: Optional[discord.Member] = None) -> None:
        """Browse your Pokédex by page. Usage: `dexpage [page] [@user]`"""
        target = user or ctx.author
        player = await self._get_player(target)
        if not player:
            await ctx.send(embed=error_embed("That trainer hasn't started yet!"))
            return

        caught_ids   = set(player.get("caughtDex", []))
        total_caught = len(caught_ids)
        if not caught_ids:
            await ctx.send(embed=discord.Embed(
                color=COLORS["orange"],
                description="📭 No Pokémon in your Pokédex yet — go catch some!",
            ))
            return

        seen: Dict[int, str] = {}
        for pk in player["pokemon"]:
            if pk["id"] in caught_ids:
                seen[pk["id"]] = pk["displayName"]

        all_entries = [(pid, seen.get(pid, f"#{pid}")) for pid in sorted(caught_ids)]
        per_page    = 30
        total_pages = max(1, math.ceil(total_caught / per_page))
        page        = max(1, min(page, total_pages))
        offset      = (page - 1) * per_page
        chunk       = all_entries[offset:offset + per_page]
        remaining   = MAX_POKEMON - total_caught
        completion  = (total_caught / MAX_POKEMON) * 100

        rank_label, rank_color = _dex_rank(total_caught)
        prog_bar = _dex_progress_bar(total_caught, MAX_POKEMON, length=20)

        embed = discord.Embed(
            title=f"📖 {target.display_name}'s Pokédex — Page {page} of {total_pages}",
            color=rank_color,
        )
        embed.description = (
            f"{rank_label}\n"
            f"{prog_bar}\n"
            f"**{total_caught}** / **{MAX_POKEMON}** — {completion:.1f}%"
        )

        col_size = 10
        cols = [chunk[i:i + col_size] for i in range(0, len(chunk), col_size)]
        for col in cols:
            val = "\n".join(f"🔵 `#{pid:04d}` {name}" for pid, name in col)
            embed.add_field(name="\u200b", value=val, inline=True)

        embed.set_footer(text=f"Page {page}/{total_pages} · dexpage <page> to navigate · {remaining} species still to find!")
        await ctx.send(embed=embed)

        # ── Leaderboard ───────────────────────────────────────────────────────────

    @commands.command(name="pokeboard", aliases=["pb"])
    async def leaderboard(self, ctx: commands.Context, category: str = "wins") -> None:
        """View the server leaderboard. Categories: wins, caught, shinies, balance"""
        category = category.lower()
        # Swap "credits" alias to "balance" for clarity with bank integration
        if category == "credits":
            category = "balance"
        valid = {"wins", "caught", "shinies", "balance"}
        if category not in valid:
            await ctx.send(embed=error_embed(f"Valid categories: {', '.join(valid)}"))
            return

        entries = []
        for member in ctx.guild.members:
            if member.bot:
                continue
            p = await self._get_player(member)
            if p:
                entries.append((member, p))

        currency = await _currency_name(ctx.guild)

        # For balance we need an async lookup; build the map first
        balance_map: Dict[int, int] = {}
        if category == "balance":
            for member, _ in entries:
                balance_map[member.id] = await _get_balance(member)

        # Per-category config: (sort_key, title, value_formatter)
        BOARD_CONFIG = {
            "wins":    (
                lambda x: x[1].get("wins", 0),
                "🏆 Battle Leaderboard",
                lambda m, p: f"{p.get('wins', 0)} wins",
            ),
            "caught":  (
                lambda x: len(x[1]["pokemon"]),
                "📦 Most Pokémon Caught",
                lambda m, p: f"{len(p['pokemon'])} Pokémon",
            ),
            "shinies": (
                lambda x: sum(1 for pk in x[1]["pokemon"] if pk.get("shiny")),
                "✨ Shiny Hunters",
                lambda m, p: f"{sum(1 for pk in p['pokemon'] if pk.get('shiny'))} shinies",
            ),
            "balance": (
                lambda x: balance_map.get(x[0].id, 0),
                "💰 Richest Trainers",
                lambda m, p: f"{balance_map.get(m.id, 0)} {currency}",
            ),
        }

        sort_key, title, get_val = BOARD_CONFIG[category]
        entries.sort(key=sort_key, reverse=True)

        medals = ["🥇", "🥈", "🥉"]
        top10  = entries[:10]
        lines  = [
            f"{medals[i] if i < 3 else f'**{i+1}.**'} **{m.display_name}** — {get_val(m, p)}"
            for i, (m, p) in enumerate(top10)
        ] or ["_No trainers yet! Be the first with `start`._"]

        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            color=COLORS["yellow"],
        )
        embed.timestamp = datetime.now(tz=timezone.utc)

        caller_rank = next((i for i, (m, _) in enumerate(entries) if m == ctx.author), -1)
        if caller_rank >= 10:
            _, caller_p = entries[caller_rank]
            embed.set_footer(text=f"Your rank: #{caller_rank + 1} — {get_val(ctx.author, caller_p)}")

        await ctx.send(embed=embed)

    # ── Help ──────────────────────────────────────────────────────────────────

    @commands.command(name="pokehelp")
    async def pokehelp(self, ctx: commands.Context) -> None:
        """Show all PokéBot commands."""
        prefix   = ctx.clean_prefix
        currency = await _currency_name(ctx.guild)
        embed    = discord.Embed(
            title=f"📖 PokéBot — Command Reference",
            color=COLORS["blue"],
        )
        embed.add_field(
            name="🌟 Getting Started",
            value="\n".join([
                f"`{prefix}start` — Begin your journey & pick a starter",
                f"`{prefix}profile [@user]` — View trainer profile",
                f"`{prefix}pokehelp` — Show this message",
            ]),
            inline=False,
        )
        embed.add_field(
            name="🎯 Catching",
            value="\n".join([
                f"`{prefix}catch [ball]` — Catch a wild Pokémon",
                f"`{prefix}pokespawn` — *(Admin)* Force spawn a Pokémon",
            ]),
            inline=False,
        )
        embed.add_field(
            name="📦 Your Collection",
            value="\n".join([
                f"`{prefix}pokemon [page] [@user]` — Browse your Pokémon",
                f"`{prefix}active <slot>` — Set your battle Pokémon",
                f"`{prefix}nickname <slot> <name>` — Give a Pokémon a nickname",
                f"`{prefix}release <slot>` — Release a Pokémon for rewards",
                f"`{prefix}dex <name or #>` — Look up any Pokémon",
                f"`{prefix}pokedex [page] [@user]` — View your Pokédex",
                f"`{prefix}dexpage <page> [@user]` — Browse Pokédex pages",
            ]),
            inline=False,
        )
        embed.add_field(
            name="⚔️ Battling",
            value="\n".join([
                f"`{prefix}battle @user` — Challenge someone to a battle",
                f"`{prefix}move <move_name>` — Use a move in your current battle",
            ]),
            inline=False,
        )
        embed.add_field(
            name="🛒 Shop",
            value="\n".join([
                f"`{prefix}shop` — Browse the PokéMart",
                f"`{prefix}buy <item> [amount]` — Buy items",
                f"`{prefix}use <item> [slot]` — Use a healing item",
                f"`{prefix}inventory` — View your bag & {currency} balance",
                f"`{prefix}pokestop` — Spin for free daily items",
            ]),
            inline=False,
        )
        embed.add_field(
            name="💿 TMs",
            value="\n".join([
                f"`{prefix}tms [page]` — Browse TM Shop (25 moves)",
                f"`{prefix}buytm <move>` — Buy a TM (e.g. `buytm thunderbolt`)",
                f"`{prefix}usetm <move> <pokemon_slot> <move_slot>` — Teach the TM",
            ]),
            inline=False,
        )
        embed.add_field(
            name="🏆 Leaderboard",
            value=f"`{prefix}pokeboard [wins|caught|shinies|balance]` — Server rankings",
            inline=False,
        )
        embed.add_field(
            name="⚙️ Admin",
            value="\n".join([
                f"`{prefix}pokeset spawnchannel #channel` — Set spawn channel",
                f"`{prefix}pokeset spawninterval <seconds>` — Set spawn timer",
                f"`{prefix}pokeset fleetimeout <minutes>` — Set flee timer",
                f"`{prefix}pokeset maxpokemon <limit>` — Set collection cap",
                f"`{prefix}pokeset setprice <item> <price>` — Set item price",
                f"`{prefix}pokeset showprices` — View current prices",
                f"`{prefix}pokeset resetprices` — Reset prices to defaults",
                f"`{prefix}pokespawn` — Force a spawn now",
            ]),
            inline=False,
        )
        embed.add_field(
            name="💡 Tips",
            value="\n".join([
                "• Wild Pokémon spawn on a timer or every ~15 messages",
                "• Uncaught Pokémon **flee after a configurable timeout** (default 4 hours)",
                "• Shiny Pokémon are **1/512** — extremely rare!",
                "• Winning battles earns XP and 100 " + currency,
                f"• All earnings go straight to your server {currency} balance",
                "• Higher-level balls have better catch rates",
                "• Spin `pokestop` every day for free balls and items",
                "• Check your `pokedex` — 1,025 species to complete!",
                "• All 1,025 Pokémon (Gen 1–9) can appear",
                "• TMs are one-use — buy with `buytm`, teach with `usetm <move> <poke_slot> <move_slot>`",
            ]),
            inline=False,
        )
        embed.set_footer(text="Good luck on your journey, Trainer!")
        await ctx.send(embed=embed)


    # ── TMs ───────────────────────────────────────────────────────────────────

    @commands.command(name="tms", aliases=["tmshop", "tmlist"])
    async def tms(self, ctx: commands.Context, page: int = 1) -> None:
        """Browse available TMs. Usage: `tms [page]`"""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return

        currency  = await _currency_name(ctx.guild)
        balance   = await _get_balance(ctx.author)
        owned_tms = player.get("items", {}).get("tms", [])
        tm_prices = await self._get_tm_prices(ctx.guild)

        all_tms   = list(TM_LIST.items())
        per_page  = 8
        total_pages = max(1, math.ceil(len(all_tms) / per_page))
        page      = max(1, min(page, total_pages))
        offset    = (page - 1) * per_page
        chunk     = all_tms[offset:offset + per_page]

        embed = discord.Embed(
            title="💿 TM Shop",
            description=(
                f"Balance: **💰 {balance} {currency}**\n"
                f"Use `buytm <move>` to purchase · `usetm <move> <slot> <move_slot>` to teach\n\u200b"
            ),
            color=COLORS["blue"],
        )
        for slug, info in chunk:
            owned_tag = " ✅ **Owned**" if slug in owned_tms else ""
            emoji     = TM_TYPE_EMOJI.get(info["type"], "💿")
            price     = tm_prices.get(slug, info["price"])
            embed.add_field(
                name=f"{emoji} **{info['name']}** — {price} {currency}{owned_tag}",
                value=f"_{info['desc']}_  ·  Type: {info['type'].capitalize()}  ·  `{slug}`",
                inline=False,
            )
        embed.set_footer(text=f"Page {page}/{total_pages} · tms <page> to navigate · {len(all_tms)} TMs total")
        await ctx.send(embed=embed)

    @commands.command(name="buytm")
    async def buytm(self, ctx: commands.Context, *, move: str) -> None:
        """Buy a TM from the shop. Usage: `buytm <move_name>`
        Example: `buytm thunderbolt` or `buytm ice beam`"""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return

        # Normalise input to PokéAPI slug format
        slug = move.lower().strip().replace(" ", "-")
        tm_info = TM_LIST.get(slug)
        if not tm_info:
            # Try matching display name case-insensitively
            slug = next(
                (s for s, info in TM_LIST.items() if info["name"].lower() == move.lower().strip()),
                None,
            )
            if slug:
                tm_info = TM_LIST[slug]
            else:
                names = ", ".join(f"`{s}`" for s in TM_LIST)
                await ctx.send(embed=error_embed(
                    f"Unknown TM **{move}**.\nAvailable: {names}\nUse `tms` to browse."
                ))
                return

        currency = await _currency_name(ctx.guild)
        tm_prices = await self._get_tm_prices(ctx.guild)
        price = tm_prices.get(slug, tm_info["price"])

        # TMs are single-use consumables — allow buying duplicates (one per use)
        success = await _withdraw(ctx.author, price)
        if not success:
            balance = await _get_balance(ctx.author)
            await ctx.send(embed=error_embed(
                f"You need **{price} {currency}** for TM {tm_info['name']} "
                f"but only have **{balance}**."
            ))
            return

        items = player.setdefault("items", {})
        tms   = items.setdefault("tms", [])
        tms.append(slug)
        await self._save_player(ctx.author, player)

        balance = await _get_balance(ctx.author)
        emoji   = TM_TYPE_EMOJI.get(tm_info["type"], "💿")
        embed = discord.Embed(
            title=f"💿 Bought TM: {tm_info['name']}!",
            description=(
                f"{emoji} **{tm_info['name']}** added to your bag!\n"
                f"_{tm_info['desc']}_\n\n"
                f"Use `usetm {slug} <pokemon_slot> <move_slot>` to teach it.\n"
                f"Remaining balance: **{balance} {currency}**"
            ),
            color=COLORS["green"],
        )
        await ctx.send(embed=embed)

    @commands.command(name="usetm")
    async def usetm(self, ctx: commands.Context, move: str, pokemon_slot: int, move_slot: int) -> None:
        """Teach a TM move to one of your Pokémon.
        Usage: `usetm <move> <pokemon_slot> <move_slot>`
        Example: `usetm thunderbolt 1 3` — teach Thunderbolt to Pokémon #1, replacing move slot 3.
        Move slots are 1–4. Use `pokemon` to see Pokémon slots."""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return
        if self._get_battle_by_user(ctx.author.id):
            await ctx.send(embed=error_embed("You can't use TMs during a battle!"))
            return

        slug = move.lower().strip().replace(" ", "-")
        tm_info = TM_LIST.get(slug)
        if not tm_info:
            slug = next(
                (s for s, info in TM_LIST.items() if info["name"].lower() == move.lower().strip()),
                None,
            )
            if slug:
                tm_info = TM_LIST[slug]
            else:
                await ctx.send(embed=error_embed(
                    f"Unknown TM **{move}**. Use `tms` to browse available TMs."
                ))
                return

        items = player.get("items", {})
        tms   = items.get("tms", [])
        if slug not in tms:
            await ctx.send(embed=error_embed(
                f"You don't have TM **{tm_info['name']}** in your bag!\n"
                f"Buy it with `buytm {slug}`."
            ))
            return

        poke_idx = pokemon_slot - 1
        if poke_idx < 0 or poke_idx >= len(player["pokemon"]):
            await ctx.send(embed=error_embed(
                f"Invalid Pokémon slot. You have {len(player['pokemon'])} Pokémon (slots 1–{len(player['pokemon'])})."
            ))
            return

        move_idx = move_slot - 1
        poke     = player["pokemon"][poke_idx]
        moves    = poke.get("moves", [])

        if move_idx < 0 or move_idx >= 4:
            await ctx.send(embed=error_embed("Move slot must be 1–4."))
            return
        # Pad moves list to 4 if the Pokémon has fewer (edge case)
        while len(moves) < 4:
            moves.append(None)

        # Check if already knows the move
        if slug in moves:
            await ctx.send(embed=error_embed(
                f"**{poke['displayName']}** already knows **{tm_info['name']}**!"
            ))
            return

        old_move = moves[move_idx]
        old_move_name = old_move.replace("-", " ").capitalize() if old_move else "—"

        # Teach the move — consume the TM
        moves[move_idx] = slug
        poke["moves"]   = moves
        tms.remove(slug)
        await self._save_player(ctx.author, player)

        emoji  = TM_TYPE_EMOJI.get(tm_info["type"], "💿")
        embed  = discord.Embed(
            title=f"✅ {poke['displayName']} learned {tm_info['name']}!",
            description=(
                f"{emoji} **{tm_info['name']}** was taught to **{poke['displayName']}**!\n"
                f"Replaced move slot {move_slot}: ~~{old_move_name}~~ → **{tm_info['name']}**\n\n"
                f"Current moves:\n"
                + "\n".join(
                    f"{i+1}. {(m.replace('-', ' ').capitalize() if m else '—')}"
                    for i, m in enumerate(poke['moves'])
                )
            ),
            color=COLORS["green"],
        )
        if poke.get("spriteUrl"):
            embed.set_thumbnail(url=poke["spriteUrl"])
        await ctx.send(embed=embed)

    # ── Pokéstop ──────────────────────────────────────────────────────────────

    @commands.command(name="pokestop")
    async def pokestop(self, ctx: commands.Context) -> None:
        """Spin a Pokéstop for free daily items! Resets at midnight each day."""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return

        # Check if already claimed today (reset at midnight US Eastern)
        EASTERN   = zoneinfo.ZoneInfo("America/New_York")
        now       = datetime.now(tz=EASTERN)
        today_str = now.strftime("%Y-%m-%d")
        last_stop = player.get("lastPokestop")

        if last_stop == today_str:
            # Calculate time until next midnight Eastern
            midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            delta    = midnight - now
            hours, remainder = divmod(int(delta.total_seconds()), 3600)
            minutes          = remainder // 60
            await ctx.send(embed=error_embed(
                f"You already spun this Pokéstop today!\n"
                f"Come back in **{hours}h {minutes}m** when it resets at midnight Eastern."
            ))
            return

        # Build a random reward bundle
        reward_balls   = {}
        reward_healing = {}
        reward_credits = 0
        lines          = []
        currency       = await _currency_name(ctx.guild)

        # Always give at least a handful of Pokéballs
        pb = random.randint(3, 8)
        reward_balls["pokeball"] = pb
        lines.append(f"🔴 {pb}× Poké Ball")

        # Chance for better balls
        if random.random() < 0.4:
            gb = random.randint(1, 3)
            reward_balls["greatball"] = gb
            lines.append(f"🔵 {gb}× Great Ball")
        if random.random() < 0.15:
            ub = random.randint(1, 2)
            reward_balls["ultraball"] = ub
            lines.append(f"⚫ {ub}× Ultra Ball")

        # Chance for healing items
        if random.random() < 0.5:
            potions = random.randint(1, 3)
            reward_healing["potion"] = potions
            lines.append(f"🧪 {potions}× Potion")
        if random.random() < 0.25:
            superpotions = random.randint(1, 2)
            reward_healing["superpotion"] = superpotions
            lines.append(f"💊 {superpotions}× Super Potion")
        if random.random() < 0.1:
            reward_healing["maxpotion"] = 1
            lines.append("💉 1× Max Potion")
        if random.random() < 0.08:
            reward_healing["revive"] = 1
            lines.append("⭐ 1× Revive")

        # Small currency bonus
        if random.random() < 0.6:
            reward_credits = random.randint(25, 150)
            lines.append(f"💰 {reward_credits} {currency}")

        # Apply rewards to player
        items = player.setdefault("items", {"pokeball": 0, "greatball": 0, "ultraball": 0, "healing": {}})
        healing = items.setdefault("healing", {})

        for ball, qty in reward_balls.items():
            items[ball] = items.get(ball, 0) + qty
        for item, qty in reward_healing.items():
            healing[item] = healing.get(item, 0) + qty

        player["lastPokestop"] = today_str
        await self._save_player(ctx.author, player)

        if reward_credits:
            await _deposit(ctx.author, reward_credits)

        embed = discord.Embed(
            title="🏪 Pokéstop",
            description=(
                f"**{ctx.author.display_name}** spun a Pokéstop!\n\n"
                + "\n".join(lines)
                + "\n\n_Come back tomorrow for more!_"
            ),
            color=COLORS["blue"],
        )
        embed.set_footer(text="Resets daily at midnight Eastern (ET)")
        await ctx.send(embed=embed)


async def setup(bot: Red) -> None:
    await bot.add_cog(PokéBot(bot))
