"""
PokéBot – Red-DiscordBot cog
Converted from a discord.js standalone bot, then reworked around buttons.

Highlights
  • Wild spawns come with buttons: throw any ball in one tap, or press
    📖 "Do I have it?" to see privately whether the species is already in your
    Pokédex (plus your odds, berries and ball counts).
  • `heal` revives and restores your whole party (or `heal all` for the whole
    collection) in one go — and offers a one-click purchase for anything missing.
  • Battles, trades, releases, evolutions and starters use buttons / menus.
  • Every change to a trainer's save goes through a per-trainer lock, so
    simultaneous commands can no longer overwrite each other.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import math
import random
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone, timedelta
import zoneinfo
from typing import AsyncIterator, Dict, List, Optional, Tuple

import aiohttp
import discord
from redbot.core import Config, bank, commands, checks
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path
from redbot.core.errors import BalanceTooHigh

from .embeds import (
    COLORS, RARITY_BADGES, artwork_url, dex_no, error_embed, hp_bar, hp_line,
    info_embed, mon_name, pokemon_embed, pretty_move, sprite_url,
    success_embed, type_badges, type_color, type_tag, xp_bar,
)
from .healing import plan_heal
from .pokeapi import (
    MAX_POKEMON, build_pokemon_instance, calculate_type_effectiveness,
    catch_rate, clean_slug, effectiveness_label, fetch_move_data, fetch_pokemon,
    get_random_pokemon_id, set_cache_dir,
    new_uid, ensure_uids, ensure_party, party_mons, uid_index,
    estimate_hit, boss_counter_damage,
)
from .ui import (
    BattleView, ConfirmView, HealShortcutView, HealView, LinkButtonsView, MovePickerView,
    RaidJoinView, RequestView, ShopView, SpawnView, StarterView, ThrowPanelView,
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
    # ── Berries ───────────────────────────────────────────────────────────────
    {"id": "razzberry",   "name": "Razz Berry",   "emoji": "🍓", "desc": "Raises catch rate by 1.5× on next throw",          "price": 80,  "category": "berries"},
    {"id": "nanabberry",  "name": "Nanab Berry",  "emoji": "🍌", "desc": "Protects your Pokémon from catch-attempt damage",   "price": 60,  "category": "berries"},
    {"id": "pinapberry",  "name": "Pinap Berry",  "emoji": "🍍", "desc": "Doubles candy (credits) earned on a successful catch","price": 100, "category": "berries"},
]

HEAL_AMOUNTS = {"potion": 20, "superpotion": 50, "maxpotion": math.inf, "revive": None}
ITEM_NAMES   = {"potion": "🧪 Potion", "superpotion": "💊 Super Potion", "maxpotion": "💉 Max Potion", "revive": "⭐ Revive"}
BALL_NAMES   = {"pokeball": "Poké Ball", "greatball": "Great Ball", "ultraball": "Ultra Ball"}
BERRY_NAMES  = {"razzberry": "🍓 Razz Berry", "nanabberry": "🍌 Nanab Berry", "pinapberry": "🍍 Pinap Berry"}

# Berry effects applied during a catch attempt.
#   razzberry  — multiplies catch_rate by 1.5×
#   nanabberry — suppresses damage to the trainer's active Pokémon on a failed throw
#   pinapberry — doubles the credit reward on a successful catch
BERRY_EFFECTS = {
    "razzberry":  {"catch_mult": 1.5, "no_damage": False, "credit_mult": 1},
    "nanabberry": {"catch_mult": 1.0, "no_damage": True,  "credit_mult": 1},
    "pinapberry": {"catch_mult": 1.0, "no_damage": False, "credit_mult": 2},
}

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
    """Remove `amount` from the member's bank balance. Returns False if they can't afford it."""
    if amount <= 0:
        return True
    try:
        if not await bank.can_spend(member, amount):
            return False
        await bank.withdraw_credits(member, amount)
    except ValueError:  # balance changed between the check and the withdrawal
        return False
    return True

async def _currency_name(guild: discord.Guild) -> str:
    return await bank.get_currency_name(guild)


# ──────────────────────────────────────────────────────────────────────────────
# Paginated UI View
# ──────────────────────────────────────────────────────────────────────────────

class PaginatedView(discord.ui.View):
    """Generic prev/next button view for any paginated embed.

    Parameters
    ----------
    build_page : async callable(page: int) -> discord.Embed
        Called whenever the user clicks a button to get the new embed.
    total_pages : int
        Total number of pages (1-indexed).
    initial_page : int
        The page that was already sent (so buttons start in the right state).
    author_id : int
        Only this Discord user can interact with the buttons.
    timeout : float
        Seconds of inactivity before the view stops listening (buttons go grey).
    """

    def __init__(
        self,
        build_page,          # Callable[[int], Awaitable[discord.Embed]]
        total_pages: int,
        initial_page: int = 1,
        author_id: int = 0,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.build_page  = build_page
        self.total_pages = total_pages
        self.page        = initial_page
        self.author_id   = author_id
        self.message: Optional[discord.Message] = None
        self._update_buttons()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _update_buttons(self) -> None:
        self.btn_first.disabled    = self.page <= 1
        self.btn_prev.disabled     = self.page <= 1
        self.btn_next.disabled     = self.page >= self.total_pages
        self.btn_last.disabled     = self.page >= self.total_pages
        self.btn_counter.label     = f"{self.page} / {self.total_pages}"

    async def _go(self, interaction: discord.Interaction, new_page: int) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "These buttons aren't yours!", ephemeral=True
            )
            return
        self.page = max(1, min(new_page, self.total_pages))
        self._update_buttons()
        embed = await self.build_page(self.page)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        """Disable all buttons when the view times out."""
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        # message ref may not exist if the view was never attached
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    # ── buttons ───────────────────────────────────────────────────────────────

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary)
    async def btn_first(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._go(interaction, 1)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary)
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._go(interaction, self.page - 1)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.secondary, disabled=True)
    async def btn_counter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()   # non-interactive; just a label

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary)
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._go(interaction, self.page + 1)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary)
    async def btn_last(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._go(interaction, self.total_pages)


class DexShinyView(discord.ui.View):
    """A single toggle button to flip a Pokédex entry between normal and shiny art.

    Parameters
    ----------
    build_embed : callable(raw: dict, shiny: bool) -> discord.Embed
        Rebuilds the dex embed for the requested form.
    raw : dict
        The raw PokéAPI payload for the looked-up Pokémon.
    shiny : bool
        Which form is showing right now (the entry is sent in this state).
    has_shiny : bool
        Whether a shiny sprite actually exists; if not, the button is disabled.
    author_id : int
        Only this user may flip the form.
    """

    def __init__(
        self,
        build_embed,
        raw: dict,
        shiny: bool = False,
        has_shiny: bool = True,
        author_id: int = 0,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.build_embed = build_embed
        self.raw         = raw
        self.shiny       = shiny
        self.author_id   = author_id
        self.message: Optional[discord.Message] = None
        self.toggle.disabled = not has_shiny
        self._sync_label()

    def _sync_label(self) -> None:
        # Button offers the *other* form
        self.toggle.label = "Normal Form" if self.shiny else "Shiny Form"
        self.toggle.emoji = "🎨" if self.shiny else "✨"

    @discord.ui.button(label="Shiny Form", emoji="✨", style=discord.ButtonStyle.primary)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This Pokédex entry isn't yours — run `dex` yourself to flip it!", ephemeral=True
            )
            return
        self.shiny = not self.shiny
        self._sync_label()
        embed = self.build_embed(self.raw, self.shiny)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Per-trainer lock
# ──────────────────────────────────────────────────────────────────────────────

class TrainerLock:
    """An asyncio lock that the *same task* may re-enter.

    A command takes its trainer's lock for its whole run; helpers it calls can
    take the lock again without deadlocking, while other tasks (another command,
    a button press, a background raid/battle payout) wait their turn.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: Optional[asyncio.Task] = None
        self._depth = 0

    def locked(self) -> bool:
        return self._lock.locked()

    async def acquire(self, timeout: Optional[float] = None) -> bool:
        task = asyncio.current_task()
        if self._owner is task and task is not None:
            self._depth += 1
            return True
        if timeout is None:
            await self._lock.acquire()
        else:
            # Poll rather than wait_for(): cancelling a wait_for'd acquire can leak the lock.
            deadline = time.monotonic() + timeout
            while self._lock.locked():
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(0.2)
            await self._lock.acquire()
        self._owner = task
        self._depth = 1
        return True

    def release(self) -> None:
        self._depth -= 1
        if self._depth <= 0:
            self._depth = 0
            self._owner = None
            self._lock.release()


class TrainerBusy(Exception):
    """Raised when a trainer's save is held by another action for too long."""


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
        self._trades:       Dict[int, dict]           = {}  # target_user_id -> pending trade offer
        self._raids:        Dict[int, dict]           = {}  # guild_id   -> active raid
        self._raid_tasks:   Dict[int, asyncio.Task]   = {}  # guild_id   -> raid loop task
        self._locks:        Dict[Tuple[int, int], TrainerLock] = {}  # (guild, user) -> save lock
        self._spawning:     set                       = set()  # channel ids with a spawn being built
        self._evo_cache:    Dict[str, dict]           = {}  # evolution-chain url -> chain json

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
            "encounter_stats": {
                "wild_spawns": 0, "shiny_spawns": 0, "legendary_spawns": 0,
                "mythical_spawns": 0, "wild_catches": 0, "shiny_catches": 0,
                "fled": 0, "raid_wins": 0, "raid_catches": 0,
            },
        }
        # NOTE: credits field removed — balance lives in Red's bank now.
        default_member = {
            "userId":             None,
            "username":           "",
            "registeredAt":       None,
            "pokemon":            [],
            "activePokemonIndex": 0,
            "party":              [],
            "wins":               0,
            "losses":             0,
            "items": {
                "pokeball":  0,
                "greatball": 0,
                "ultraball": 0,
                "healing":   {},
                "tms":       [],
                "berries":   {},
            },
            "lastPokestop":  None,
            "pokestopStreak": 0,
            "caughtDex":     [],
        }
        self.config.register_guild(**default_guild)
        self.config.register_member(**default_member)

        # Cache PokéAPI data in Red's data folder (survives `[p]cog update`, never read-only).
        set_cache_dir(cog_data_path(self) / "pokemon_cache")

        self._session: Optional[aiohttp.ClientSession] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()
        # When the cog is (re)loaded on a running bot, on_ready never fires again —
        # start the spawn loops right away instead of waiting for a chat message.
        if self.bot.is_ready():
            for guild in self.bot.guilds:
                self._ensure_spawn_task(guild)

    async def cog_unload(self) -> None:
        for task in (*self._spawn_tasks.values(), *self._flee_tasks.values(), *self._raid_tasks.values()):
            task.cancel()
        for spawn in self._spawn_cache.values():
            view = spawn.get("view")
            if view is not None:
                view.stop()
        if self._session:
            await self._session.close()

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        """Delete a trainer's save in every server."""
        all_members = await self.config.all_members()
        for guild_id, members in all_members.items():
            if user_id in members:
                await self.config.member_from_ids(guild_id, user_id).clear()

    # ── Checks & locking ──────────────────────────────────────────────────────

    # Commands that never change a save (or manage their own locking) run unlocked.
    _UNLOCKED_COMMANDS = {
        "pokeset", "pokespawn", "pokedexsync", "raidstart", "raidcancel", "raidstatus",
        "raidswap", "raidjoin", "profile", "pokemon", "party", "dex", "pokedex", "pokeboard",
        "pokestats", "pokehelp", "tms", "berries", "inventory", "shop", "battle", "move",
        "forfeit", "trade",
    }

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage("PokéBot commands only work inside a server.")
        return True

    def _lock_for(self, guild_id: int, user_id: int) -> TrainerLock:
        key = (guild_id, user_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = TrainerLock()
        return lock

    async def cog_before_invoke(self, ctx: commands.Context) -> None:
        if ctx.guild is None or ctx.command is None:
            return
        root = ctx.command.qualified_name.split(" ")[0]
        if ctx.command.qualified_name in self._UNLOCKED_COMMANDS or root == "pokeset":
            return
        lock = self._lock_for(ctx.guild.id, ctx.author.id)
        if not await lock.acquire(timeout=8):
            raise commands.UserFeedbackCheckFailure(
                "⏳ You're still in the middle of another PokéBot action — finish it first!"
            )
        ctx.__dict__["_pokebot_lock"] = lock

    async def cog_after_invoke(self, ctx: commands.Context) -> None:
        lock = ctx.__dict__.pop("_pokebot_lock", None)
        if lock is not None:
            lock.release()

    @asynccontextmanager
    async def _trainer(self, member: discord.Member, *, timeout: Optional[float] = None) -> AsyncIterator[Optional[dict]]:
        """Lock a trainer, load a fresh copy of their save, and write it back on success.

        Yields None (and saves nothing) if the member hasn't started yet.
        Raises TrainerBusy if `timeout` elapses before the lock is free.
        """
        lock = self._lock_for(member.guild.id, member.id)
        if not await lock.acquire(timeout=timeout):
            raise TrainerBusy()
        try:
            player = await self._get_player(member)
            yield player
            if player is not None:
                await self._save_player(member, player)
        finally:
            lock.release()

    # ── Player helpers ────────────────────────────────────────────────────────

    async def _get_player(self, member: discord.Member) -> Optional[dict]:
        data = await self.config.member(member).all()
        if data["registeredAt"] is None:
            return None
        # One-time, self-healing migration: give every Pokémon a stable uid and
        # keep the party list valid. Persists only when something actually changes.
        changed = ensure_uids(data)
        changed = ensure_party(data) or changed
        if changed:
            await self._save_player(member, data)
        return data

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
            "party":              [starter["uid"]],
            "wins":               0,
            "losses":             0,
            "items": {
                "pokeball":  10,
                "greatball": 3,
                "ultraball": 1,
                "healing":   {},
                "tms":       [],
                "berries":   {},
            },
            "lastPokestop":   None,
            "pokestopStreak": 0,
            "caughtDex":      [starter["id"]],
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

    # ── XP helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _xp_for_level(level: int) -> int:
        """XP required to reach the *next* level from `level`.

        Uses a medium-speed curve that mirrors the "Medium Fast" group from
        the main series games (total XP = n³), but expressed as per-level
        cost so existing low-level Pokémon aren't instantly broken.

        Lv 1→2  :   30 XP   (a couple of catches)
        Lv 5→6  :  110 XP
        Lv 10→11:  330 XP
        Lv 20→21: 1 230 XP
        Lv 30→31: 2 730 XP  (first-stage → second-stage threshold ~Lv 16)
        Lv 50→51: 7 550 XP
        """
        return math.floor(3 * level ** 2 + 10 * level + 20)

    @staticmethod
    def _xp_bar(current: int, needed: int, length: int = 10) -> str:
        filled = round((current / needed) * length) if needed else length
        return "▰" * filled + "▱" * (length - filled)

    def _check_level_up(self, pokemon: dict) -> List[str]:
        """Apply banked XP, level the Pokémon up, grow stats, return messages."""
        # Back-fill xpToNext for Pokémon created before this formula existed
        if pokemon.get("xpToNext", 0) < 30:
            pokemon["xpToNext"] = self._xp_for_level(pokemon["level"])

        messages = []
        while pokemon["xp"] >= pokemon["xpToNext"]:
            pokemon["xp"]      -= pokemon["xpToNext"]
            pokemon["level"]   += 1
            pokemon["xpToNext"] = self._xp_for_level(pokemon["level"])

            growth = math.floor(pokemon["level"] * 0.6)
            pokemon["stats"]["maxHp"] += growth
            pokemon["stats"]["hp"]     = min(
                pokemon["stats"]["hp"] + growth, pokemon["stats"]["maxHp"]
            )
            for stat in ("attack", "defense", "speed"):
                if stat in pokemon["stats"]:
                    mult = {"attack": 0.8, "defense": 0.6, "speed": 0.7}[stat]
                    pokemon["stats"][stat] += math.floor(growth * mult)

            messages.append(
                f"⬆️ **{pokemon['displayName']}** leveled up to **Lv.{pokemon['level']}**! "
                f"_(next level in {pokemon['xpToNext']} XP)_"
            )
        return messages

    async def _fetch_evolution_target(self, pokemon: dict) -> Optional[dict]:
        """Return {name, id, min_level} of the next evolution, or None if fully evolved / no data."""
        try:
            raw      = await fetch_pokemon(self._session, pokemon["name"])
            species_url = raw["species"]["url"]
            async with self._session.get(species_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                r.raise_for_status()
                species = await r.json()

            chain_url = species["evolution_chain"]["url"]
            chain_data = self._evo_cache.get(chain_url)
            if chain_data is None:
                async with self._session.get(chain_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    r.raise_for_status()
                    chain_data = await r.json()
                self._evo_cache[chain_url] = chain_data

            # Walk the chain looking for our Pokémon, then return its next evolution
            def _find_next(node: dict, target_name: str) -> Optional[dict]:
                if node["species"]["name"] == target_name:
                    if node["evolves_to"]:
                        nxt   = node["evolves_to"][0]
                        deets = nxt["evolution_details"][0] if nxt["evolution_details"] else {}
                        return {
                            "name":      nxt["species"]["name"],
                            "min_level": deets.get("min_level"),   # None for item/trade evos
                        }
                    return None  # Already fully evolved
                for child in node["evolves_to"]:
                    result = _find_next(child, target_name)
                    if result is not None:
                        return result
                return None

            return _find_next(chain_data["chain"], pokemon["name"])
        except Exception as exc:
            log.warning(f"[PokéBot] evolution fetch failed for {pokemon['name']}: {exc}")
            return None

    def _evolution_eligible(self, pokemon: dict, evo_target: dict) -> bool:
        """True if the Pokémon meets the level threshold to evolve."""
        min_lvl = evo_target.get("min_level")
        if min_lvl is None:
            # Item/trade evolution — allow at level 20 as a reasonable proxy
            # (players can't trade items in this bot yet)
            min_lvl = 20
        return pokemon["level"] >= min_lvl

    async def _notify_evo_if_ready(
        self, ctx: commands.Context, player: dict, poke_idx: int
    ) -> None:
        """Command-path wrapper: caller already holds the trainer lock and owns `player`."""
        await self._notify_evo(ctx.channel, ctx.author, player, poke_idx, mention=False)

    async def _notify_evo(
        self,
        channel: discord.abc.Messageable,
        member: discord.Member,
        player: dict,
        poke_idx: int,
        *,
        mention: bool,
    ) -> None:
        """Send a one-time 'evolution available' notice. Caller must hold the trainer lock."""
        if not (0 <= poke_idx < len(player["pokemon"])):
            return
        poke = player["pokemon"][poke_idx]
        if poke.get("evoNotified"):
            return
        evo = await self._fetch_evolution_target(poke)
        if not (evo and self._evolution_eligible(poke, evo)):
            return
        poke["evoNotified"] = True
        await self._save_player(member, player)
        min_lvl = evo["min_level"] or 20
        embed = discord.Embed(
            title="✨ Evolution available!",
            description=(
                f"{member.mention + ' — ' if mention else ''}**{mon_name(poke)}** can evolve into "
                f"**{evo['name'].replace('-', ' ').title()}** (reached Lv.{min_lvl})!\n\n"
                f"Use `evolve {poke_idx + 1}` whenever you're ready — or keep it as it is."
            ),
            color=COLORS["yellow"],
        )
        art = artwork_url(poke)
        if art:
            embed.set_thumbnail(url=art)
        with suppress(discord.HTTPException):
            await channel.send(embed=embed)

    async def _notify_evo_background(self, channel: discord.abc.Messageable, member: discord.Member, uid: str) -> None:
        """Background-task version: takes the lock and reloads the save first."""
        try:
            async with self._trainer(member, timeout=60) as player:
                if player is None:
                    return
                idx = uid_index(player, uid)
                if idx >= 0:
                    await self._notify_evo(channel, member, player, idx, mention=True)
        except TrainerBusy:
            pass
        except Exception:
            log.exception("[PokéBot] background evolution check failed")

    def _update_dex(self, player: dict, pokemon: dict) -> bool:
        """Record a newly caught species in the player's Pokédex. Returns True if it's a new entry."""
        dex = player.setdefault("caughtDex", [])
        pid = pokemon["id"]
        if pid not in dex:
            dex.append(pid)
            return True
        return False

    def _build_battle_embed(self, battle: dict, log_lines: Optional[List[str]] = None) -> discord.Embed:
        p1, p2 = battle["player1"], battle["player2"]
        embed = discord.Embed(
            title=f"⚔️ Battle — Turn {battle['turn']}",
            color=COLORS["purple"],
        )
        for side, icon in ((p1, "🔴"), (p2, "🔵")):
            mon = side["pokemon"]
            ready = " ✅ *move locked in*" if side.get("moveUsed") else ""
            embed.add_field(
                name=f"{icon} {side['username']} — {mon_name(mon)}",
                value=f"Lv.{mon['level']} · {type_badges(mon['types'])}\n{hp_line(mon)}{ready}",
                inline=False,
            )
        art = artwork_url(p1["pokemon"])
        if art:
            embed.set_thumbnail(url=art)
        if log_lines:
            embed.add_field(name="📜 Battle Log", value="\n".join(log_lines)[-1024:], inline=False)
        embed.set_footer(text="Tap ⚔️ Choose Move (or use `move <name>`) · 3 min to act each turn")
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
        channel: Optional[discord.TextChannel] = None,
    ) -> List[str]:
        """Conclude a battle and pay out. Returns level-up announcement strings.

        Runs outside any command lock (battles involve two trainers), so each
        trainer's save is updated in turn under their own lock — never both at
        once, which rules out deadlocks between two simultaneous battles.
        """
        battle   = self._battles.pop(battle_id, None)
        lvl_msgs: List[str] = []
        if not battle:
            return lvl_msgs

        p1_is_winner = battle["player1"]["id"] == winner_id
        win_side  = battle["player1"] if p1_is_winner else battle["player2"]
        lose_side = battle["player2"] if p1_is_winner else battle["player1"]
        opp_level = lose_side["pokemon"].get("level", 1)
        turns     = battle.get("turn", 1)

        async def _apply(member_id: int, side: dict, won: bool) -> None:
            member = guild.get_member(member_id)
            if member is None:
                return
            evolve_uid = None
            try:
                async with self._trainer(member, timeout=60) as data:
                    if data is None:
                        return
                    idx = uid_index(data, side["pokemon"].get("uid", ""))
                    if idx < 0:
                        idx = data["activePokemonIndex"]
                    mon = data["pokemon"][idx]
                    if won:
                        data["wins"] = data.get("wins", 0) + 1
                        # XP = base 80 + (opponent level × 3) + small turn bonus, capped
                        xp_gain = min(80 + opp_level * 3 + turns * 2, 400)
                        mon["stats"]["hp"] = max(1, min(side["pokemon"]["stats"]["hp"], mon["stats"]["maxHp"]))
                    else:
                        data["losses"] = data.get("losses", 0) + 1
                        xp_gain = max(15, math.floor((80 + opp_level * 3) * 0.30))
                        mon["stats"]["hp"] = 0  # fainted
                    mon["xp"] = mon.get("xp", 0) + xp_gain
                    msgs = self._check_level_up(mon)
                    lvl_msgs.extend(msgs)
                    if msgs:
                        evolve_uid = mon["uid"]
                if won:
                    await _deposit(member, 100)
            except TrainerBusy:
                log.warning("[PokéBot] could not record battle result for %s (save busy)", member_id)
                return
            if channel is not None and evolve_uid:
                self.bot.loop.create_task(self._notify_evo_background(channel, member, evolve_uid))

        await _apply(winner_id, win_side, True)
        await _apply(loser_id, lose_side, False)
        return lvl_msgs

    # ── Spawn & Flee System ───────────────────────────────────────────────────

    def _spawn_embed(self, pokemon: dict, flee_at: float) -> discord.Embed:
        rarity = RARITY_BADGES.get(pokemon.get("rarity", "common"), "")
        lines = [f"{dex_no(pokemon['id'])} · Lv.**{pokemon['level']}** · {type_badges(pokemon['types'])}"]
        if pokemon.get("shiny"):
            lines.append("✨ **A SHINY Pokémon!** ✨")
        if rarity:
            lines.append(f"**{rarity} ENCOUNTER!**")
        lines.append(f"\n⏳ Flees <t:{int(flee_at)}:R> if nobody catches it.")
        embed = discord.Embed(
            title=f"🌿 A wild {pokemon['displayName']}{' ✨' if pokemon.get('shiny') else ''} appeared!",
            description="\n".join(lines),
            color=type_color(pokemon),
        )
        art = artwork_url(pokemon)
        if art:
            embed.set_image(url=art)
        embed.set_footer(text="Tap a ball to throw · 📖 Do I have it? privately checks your Pokédex, odds & berries")
        return embed

    async def _spawn_wild(self, channel: discord.TextChannel) -> None:
        """Spawn a wild Pokémon in the channel and start a flee timer."""
        if channel.id in self._spawn_cache or channel.id in self._spawning:
            return
        self._spawning.add(channel.id)
        try:
            pokemon_id = get_random_pokemon_id()
            try:
                pokemon = await build_pokemon_instance(self._session, pokemon_id)
            except Exception as exc:
                log.warning("[PokéBot] Failed to fetch Pokémon ID %s: %s", pokemon_id, exc)
                return

            async with self.config.guild(channel.guild).encounter_stats() as stats:
                stats["wild_spawns"] = stats.get("wild_spawns", 0) + 1
                if pokemon.get("shiny"):
                    stats["shiny_spawns"] = stats.get("shiny_spawns", 0) + 1
                if pokemon.get("rarity") == "legendary":
                    stats["legendary_spawns"] = stats.get("legendary_spawns", 0) + 1
                elif pokemon.get("rarity") == "mythical":
                    stats["mythical_spawns"] = stats.get("mythical_spawns", 0) + 1

            flee_timeout = await self.config.guild(channel.guild).flee_timeout()
            spawn_id = uuid.uuid4().hex
            flee_at = time.time() + flee_timeout
            view = SpawnView(self, spawn_id)
            try:
                message = await channel.send(embed=self._spawn_embed(pokemon, flee_at), view=view)
            except discord.HTTPException as exc:
                log.warning("[PokéBot] Could not post spawn in %s: %s", channel.id, exc)
                view.stop()
                return
            view.message = message
            self._spawn_cache[channel.id] = {
                "pokemon":   pokemon,
                "channelId": channel.id,
                "spawnedAt": time.time(),
                "fleeAt":    flee_at,
                "spawnId":   spawn_id,
                "message":   message,
                "view":      view,
            }
        finally:
            self._spawning.discard(channel.id)

        self._cancel_flee_task(channel.id)
        self._flee_tasks[channel.id] = self.bot.loop.create_task(
            self._flee_timer(channel, pokemon, spawn_id, flee_timeout)
        )

    async def _close_spawn_message(self, spawn: dict, note: str, color: Optional[int] = None) -> None:
        """Grey out a finished spawn's buttons and stamp what happened on it."""
        view = spawn.get("view")
        message = spawn.get("message")
        if view is not None:
            view.disable_all()
            view.stop()
        if message is None:
            return
        embed = message.embeds[0].copy() if message.embeds else discord.Embed()
        desc = (embed.description or "").split("⏳")[0].rstrip()
        embed.description = f"{desc}\n\n{note}"
        embed.set_footer(text="This encounter is over.")
        if color is not None:
            embed.color = color
        with suppress(discord.HTTPException):
            await message.edit(embed=embed, view=view)

    async def _flee_timer(self, channel: discord.TextChannel, pokemon: dict, spawn_id: str, flee_timeout: int = FLEE_TIMEOUT) -> None:
        """Wait flee_timeout seconds; if the Pokémon is still uncaught, it flees and a new one spawns after."""
        await asyncio.sleep(flee_timeout)

        # Only flee if this exact spawn (by unique ID) is still in the cache
        cached = self._spawn_cache.get(channel.id)
        if not (cached and cached.get("spawnId") == spawn_id):
            return
        self._spawn_cache.pop(channel.id, None)
        async with self.config.guild(channel.guild).encounter_stats() as stats:
            stats["fled"] = stats.get("fled", 0) + 1
        await self._close_spawn_message(cached, "🍃 **It fled into the tall grass!**", COLORS["gray"])
        with suppress(discord.HTTPException):
            embed = discord.Embed(
                description=(
                    f"🌿 The wild **{pokemon['displayName']}** got bored and fled!\n"
                    f"_A new Pokémon will appear when someone next speaks..._"
                ),
                color=COLORS["gray"],
            )
            spr = sprite_url(pokemon)
            if spr:
                embed.set_thumbnail(url=spr)
            await channel.send(embed=embed)

        # Mark this guild as waiting for activity before respawning
        self._pending_respawn[channel.guild.id] = channel

    # ── Catching (shared by buttons and the `catch` command) ─────────────────

    @staticmethod
    def _catch_chance(lead: dict, wild: dict, ball: str, berry_mult: float = 1.0) -> float:
        base_chance = catch_rate(wild, ball)
        # Being 10+ levels above the wild Pokémon makes catching easier (up to +40%),
        # 10+ below makes it harder (down to -40%).
        lvl_diff = lead["level"] - wild["level"]
        lvl_mult = 1.0 + max(-0.40, min(0.40, lvl_diff * 0.04))
        rarity_mult = {"rare": 0.85, "legendary": 0.55, "mythical": 0.45}.get(wild.get("rarity", "common"), 1.0)
        return min(base_chance * berry_mult * lvl_mult * rarity_mult, 0.95)

    @staticmethod
    def _pick_default_ball(items: dict) -> Optional[str]:
        return next((b for b in BALL_NAMES if items.get(b, 0) > 0), None)

    async def _do_catch(
        self,
        member: discord.Member,
        channel: discord.TextChannel,
        ball: Optional[str],
        berry: Optional[str],
        spawn_id: Optional[str] = None,
    ) -> dict:
        """Attempt a catch. Caller MUST hold the trainer's lock.

        Returns {"error": embed|None, "embeds": [...], "view": View|None}.
        """
        out: dict = {"error": None, "embeds": [], "view": None}
        player = await self._get_player(member)
        if not player:
            out["error"] = error_embed("Start your journey first with `start`!")
            return out

        spawn = self._spawn_cache.get(channel.id)
        if not spawn or (spawn_id and spawn["spawnId"] != spawn_id):
            out["error"] = error_embed("That Pokémon is gone — it was caught or it fled!" if spawn_id
                                       else "There's no wild Pokémon here right now!")
            return out

        active_poke = player["pokemon"][player["activePokemonIndex"]]
        if active_poke["stats"]["hp"] <= 0:
            out["error"] = error_embed(
                f"Your lead **{mon_name(active_poke)}** has fainted and can't help you catch!\n"
                f"Tap **🩹 Heal party** below, use `heal`, or switch lead with `active <slot>`."
            )
            out["view"] = HealShortcutView(self, member.id)
            return out

        items = player["items"]
        if ball is None:
            ball = self._pick_default_ball(items)
            if ball is None:
                out["error"] = error_embed("You're out of Poké Balls! Grab some from the `shop` or spin a `pokestop`.")
                return out
        ball = ball.lower().replace(" ", "").replace("-", "").replace("é", "e")
        if ball not in BALL_NAMES:
            out["error"] = error_embed("Unknown ball type. Use: `pokeball`, `greatball`, or `ultraball`.")
            return out
        if items.get(ball, 0) <= 0:
            out["error"] = error_embed(f"You don't have any {BALL_NAMES[ball]}s! Buy some with `shop`.")
            return out

        max_pokemon = await self.config.guild(channel.guild).max_pokemon()
        if len(player["pokemon"]) >= max_pokemon:
            out["error"] = error_embed(
                f"Your collection is full! (**{len(player['pokemon'])}/{max_pokemon}** Pokémon)\n"
                "Use `release <slot>` to make room before catching more."
            )
            return out

        berry_slug = (berry or "").lower().replace(" ", "").replace("-", "")
        berry_effect = {"catch_mult": 1.0, "no_damage": False, "credit_mult": 1}
        if berry_slug:
            if berry_slug not in BERRY_EFFECTS:
                out["error"] = error_embed("Unknown berry. Available: `razzberry`, `nanabberry`, `pinapberry`.")
                return out
            if items.setdefault("berries", {}).get(berry_slug, 0) <= 0:
                out["error"] = error_embed(
                    f"You don't have any {BERRY_NAMES[berry_slug]}! Get them from `pokestop` or `buy {berry_slug}`."
                )
                return out
            berry_effect = BERRY_EFFECTS[berry_slug]

        # ── Claim & roll — no awaits from here until the spawn is popped, so two
        #    trainers can never both catch the same Pokémon. ───────────────────
        if self._spawn_cache.get(channel.id) is not spawn:
            out["error"] = error_embed("Too slow — someone else just caught it!")
            return out
        pokemon = spawn["pokemon"]
        chance  = self._catch_chance(active_poke, pokemon, ball, berry_effect["catch_mult"])
        caught  = random.random() < chance
        if caught:
            self._spawn_cache.pop(channel.id, None)
            self._cancel_flee_task(channel.id)

        items[ball] -= 1
        if berry_slug:
            items["berries"][berry_slug] -= 1
        berry_tag = f"{BERRY_NAMES[berry_slug]} fed! " if berry_slug else ""
        shakes = 3 if caught else random.randint(0, 2)
        shake_text = "🔴 " * shakes if shakes else "💥 "

        # XP for the lead: full on success, 40% for the attempt.
        catch_xp_full = max(10, math.floor(pokemon["level"] * 1.5 + 5))
        catch_xp = catch_xp_full if caught else max(5, math.floor(catch_xp_full * 0.4))

        hp_note = ""
        if not caught and not berry_effect["no_damage"]:
            scratch = max(1, random.randint(math.floor(pokemon["level"] * 0.5), math.floor(pokemon["level"] * 1.5)))
            active_poke["stats"]["hp"] = max(0, active_poke["stats"]["hp"] - scratch)
            if active_poke["stats"]["hp"] <= 0:
                hp_note = f"💀 **{mon_name(active_poke)}** took {scratch} damage and fainted!"
                out["view"] = HealShortcutView(self, member.id)
            else:
                hp_note = f"🩸 {mon_name(active_poke)} took **{scratch}** damage · {hp_line(active_poke, 8)}"
        elif not caught:
            hp_note = f"🍌 The Nanab Berry kept {mon_name(active_poke)} safe!"

        active_poke["xp"] = active_poke.get("xp", 0) + catch_xp
        lvl_msgs = self._check_level_up(active_poke)
        currency = await _currency_name(channel.guild)

        if caught:
            spawn["view"].disable_all()  # visually lock the buttons right away
            base_credits   = 500 if pokemon.get("shiny") else (100 if pokemon["level"] >= 30 else 50)
            credits_earned = base_credits * berry_effect["credit_mult"]
            is_new_dex     = self._update_dex(player, pokemon)
            player["pokemon"].append({**pokemon, "caughtAt": time.time()})
            owned_same     = sum(1 for p in player["pokemon"] if p["id"] == pokemon["id"])
            await self._save_player(member, player)
            await _deposit(member, credits_earned)
            async with self.config.guild(channel.guild).encounter_stats() as stats:
                stats["wild_catches"] = stats.get("wild_catches", 0) + 1
                if pokemon.get("shiny"):
                    stats["shiny_catches"] = stats.get("shiny_catches", 0) + 1
            self.bot.loop.create_task(self._delayed_respawn(channel))
            await self._close_spawn_message(
                spawn, f"🎉 **Caught by {member.display_name}** with a {BALL_NAMES[ball]}!", COLORS["green"]
            )

            bonus = []
            if pokemon.get("shiny"):
                bonus.append("✨ shiny bonus")
            elif pokemon["level"] >= 30:
                bonus.append("💪 high-level bonus")
            if berry_effect["credit_mult"] > 1:
                bonus.append("🍍 Pinap ×2")
            embed = discord.Embed(
                title=f"🎉 Gotcha! {member.display_name} caught {pokemon['displayName']}{' ✨' if pokemon.get('shiny') else ''}!",
                description=(
                    f"{berry_tag}{shake_text}**Click!**\n"
                    f"{dex_no(pokemon['id'])} · Lv.**{pokemon['level']}** · {type_badges(pokemon['types'])}"
                ),
                color=type_color(pokemon),
            )
            embed.add_field(
                name="📖 Pokédex",
                value=(f"🆕 **New entry!** ({len(player['caughtDex'])}/{MAX_POKEMON})" if is_new_dex
                       else f"Already registered · you now own **{owned_same}**"),
                inline=True,
            )
            embed.add_field(
                name=f"💰 {currency}",
                value=f"+**{credits_earned:,}**" + (f"\n_{' · '.join(bonus)}_" if bonus else ""),
                inline=True,
            )
            need = active_poke.get("xpToNext") or 1
            embed.add_field(
                name="⭐ XP",
                value=f"+{catch_xp} → {mon_name(active_poke, shiny_mark=False)}\n{xp_bar(active_poke['xp'], need, 8)}",
                inline=True,
            )
            art = artwork_url(pokemon)
            if art:
                embed.set_thumbnail(url=art)
            embed.set_footer(text=f"Odds were {chance:.0%} · {BALL_NAMES[ball]}s left: {items[ball]}")
            out["embeds"].append(embed)
        else:
            await self._save_player(member, player)  # ball/berry spent, HP damage, XP
            embed = discord.Embed(
                title=f"💨 Oh no! {pokemon['displayName']} broke free!",
                description=(
                    f"{berry_tag}{shake_text}\n"
                    f"**{member.display_name}** threw a {BALL_NAMES[ball]} — odds were **{chance:.0%}**.\n"
                    + (f"{hp_note}\n" if hp_note else "")
                    + f"_+{catch_xp} XP to {mon_name(active_poke, shiny_mark=False)} for trying · "
                    f"{BALL_NAMES[ball]}s left: {items[ball]}_"
                ),
                color=COLORS["red"],
            )
            spr = sprite_url(pokemon)
            if spr:
                embed.set_thumbnail(url=spr)
            out["embeds"].append(embed)

        if lvl_msgs:
            out["embeds"].append(discord.Embed(description="\n".join(lvl_msgs), color=COLORS["green"]))
            await self._notify_evo(channel, member, player, player["activePokemonIndex"], mention=False)
        return out

    async def handle_throw(
        self,
        interaction: discord.Interaction,
        spawn_id: str,
        ball: Optional[str],
        berry: Optional[str],
        *,
        panel: Optional[ThrowPanelView] = None,
    ) -> None:
        """Button entry point for throwing a ball at a spawn."""
        member, channel = interaction.user, interaction.channel
        if not isinstance(member, discord.Member) or channel is None:
            return
        # Acknowledge first: Discord drops interactions not answered within 3 seconds.
        await interaction.response.defer()
        lock = self._lock_for(member.guild.id, member.id)
        if not await lock.acquire(timeout=5):
            await interaction.followup.send(
                "⏳ You're still busy with another PokéBot action — try again in a moment.", ephemeral=True
            )
            return
        try:
            out = await self._do_catch(member, channel, ball, berry, spawn_id=spawn_id)
            if out["error"] is not None:
                await interaction.followup.send(embed=out["error"], view=out["view"] or discord.utils.MISSING,
                                                ephemeral=True)
            else:
                msg = await channel.send(embeds=out["embeds"], view=out["view"] or None)
                if out["view"] is not None:
                    out["view"].message = msg
            if panel is not None:
                if self._spawn_cache.get(channel.id, {}).get("spawnId") == spawn_id:
                    new_panel = await self._build_throw_panel(member, spawn_id)
                    if new_panel is not None:
                        panel.stop()
                        with suppress(discord.HTTPException):
                            await interaction.edit_original_response(view=new_panel)
                else:
                    panel.disable_all()
                    panel.stop()
                    with suppress(discord.HTTPException):
                        await interaction.edit_original_response(view=panel)
        finally:
            lock.release()

    async def _build_throw_panel(self, member: discord.Member, spawn_id: str) -> Optional[ThrowPanelView]:
        player = await self._get_player(member)
        if not player:
            return None
        items = player.get("items", {})
        return ThrowPanelView(
            self, member.id, spawn_id,
            balls={b: items.get(b, 0) for b in BALL_NAMES},
            berries=items.get("berries", {}),
            berry_names=BERRY_NAMES,
        )

    async def spawn_dex_check(self, interaction: discord.Interaction, spawn_id: str) -> None:
        """'📖 Do I have it?' — private Pokédex status, odds and a personal throw panel."""
        member, channel = interaction.user, interaction.channel
        if not isinstance(member, discord.Member) or channel is None:
            return
        spawn = self._spawn_cache.get(channel.id)
        if not spawn or spawn["spawnId"] != spawn_id:
            await interaction.response.send_message("That Pokémon is gone — it was caught or it fled!", ephemeral=True)
            return
        player = await self._get_player(member)
        if not player:
            await interaction.response.send_message(
                "You haven't started your journey yet! Use `start` to pick a starter, then come back.",
                ephemeral=True,
            )
            return

        wild = spawn["pokemon"]
        owned = [p for p in player["pokemon"] if p["id"] == wild["id"]]
        registered = wild["id"] in player.get("caughtDex", [])
        if registered:
            best = max(owned, key=lambda p: p["level"]) if owned else None
            shinies = sum(1 for p in owned if p.get("shiny"))
            status = f"✅ **Already in your Pokédex!**\nYou own **{len(owned)}**"
            if best:
                status += f" (best Lv.{best['level']})"
            if shinies:
                status += f" · ✨ {shinies} shiny"
            if wild.get("shiny") and not shinies:
                status += "\n✨ But you don't have a **shiny** one yet!"
        else:
            status = (f"🆕 **Not in your Pokédex yet!**\nCatch it to register {dex_no(wild['id'])} "
                      f"({len(player.get('caughtDex', []))}/{MAX_POKEMON} so far).")

        lead = player["pokemon"][player["activePokemonIndex"]]
        items = player.get("items", {})
        odds_lines = []
        for b, label in BALL_NAMES.items():
            base = self._catch_chance(lead, wild, b)
            razz = self._catch_chance(lead, wild, b, BERRY_EFFECTS["razzberry"]["catch_mult"])
            emoji = {"pokeball": "🔴", "greatball": "🔵", "ultraball": "⚫"}[b]
            odds_lines.append(f"{emoji} {label} ×{items.get(b, 0)} — **{base:.0%}** _(🍓 {razz:.0%})_")
        berries = items.get("berries", {})
        berry_line = " · ".join(f"{BERRY_NAMES[k]} ×{berries.get(k, 0)}" for k in BERRY_NAMES)

        embed = discord.Embed(
            title=f"📖 {wild['displayName']}{' ✨' if wild.get('shiny') else ''} {dex_no(wild['id'])}",
            description=status,
            color=COLORS["green"] if not registered else type_color(wild),
        )
        lead_val = f"{mon_name(lead)} · Lv.{lead['level']}\n{hp_line(lead, 8)}"
        if lead["stats"]["hp"] <= 0:
            lead_val += "\n⚠️ Fainted — heal with `heal` before throwing!"
        embed.add_field(name="🎯 Your lead", value=lead_val, inline=False)
        embed.add_field(name="🎲 Your odds", value="\n".join(odds_lines), inline=False)
        embed.add_field(name="🍓 Berries", value=berry_line, inline=False)
        spr = sprite_url(wild)
        if spr:
            embed.set_thumbnail(url=spr)
        embed.set_footer(text="Only you can see this · pick a berry, then throw from here")
        panel = await self._build_throw_panel(member, spawn_id)
        await interaction.response.send_message(embed=embed, view=panel, ephemeral=True)

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
        old_spawn = self._spawn_cache.pop(channel.id, None)
        if old_spawn:
            await self._close_spawn_message(old_spawn, "🔄 Replaced by an admin spawn.", COLORS["gray"])
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
        all_members = await self.config.all_members(ctx.guild)
        for member_id, data in all_members.items():
            member = ctx.guild.get_member(int(member_id))
            if member is None or member.bot or data.get("registeredAt") is None:
                continue
            try:
                async with self._trainer(member, timeout=15) as player:
                    if not player or not player.get("pokemon"):
                        continue
                    dex = set(player.get("caughtDex", []))
                    before = len(dex)
                    dex.update(pk["id"] for pk in player["pokemon"])
                    if len(dex) > before:
                        player["caughtDex"] = sorted(dex)
                        total_new += len(dex) - before
                        synced += 1
            except TrainerBusy:
                continue

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
            await ctx.send(embed=error_embed("You already started your journey! Use `profile` or `pokemon` to see your team."))
            return

        embed = discord.Embed(
            title="🌟 Welcome to the world of Pokémon!",
            description=(
                f"Hi **{ctx.author.display_name}**! Every great trainer starts with one partner.\n\n"
                "**Pick a region from the menu below**, then choose your starter.\n"
                "_Grass, Fire or Water — there's no wrong answer!_"
            ),
            color=COLORS["yellow"],
        )
        embed.set_thumbnail(url="https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/items/poke-ball.png")
        view = StarterView(ctx.author.id, STARTERS)
        view.message = await ctx.send(embed=embed, view=view)
        await view.wait()
        if view.choice is None:
            await ctx.send(embed=error_embed("No starter chosen. Use `start` again when you're ready!"))
            return

        chosen = view.choice
        async with ctx.typing():
            try:
                pokemon = await build_pokemon_instance(self._session, chosen["id"], level=5, allow_shiny=False)
            except Exception:
                await ctx.send(embed=error_embed("Couldn't reach the Pokédex service — try `start` again in a moment."))
                return
        pokemon["stats"]["hp"] = pokemon["stats"]["maxHp"]
        if await self._get_player(ctx.author):  # double-start guard
            return
        await self._create_player(ctx.author, pokemon)

        currency = await _currency_name(ctx.guild)
        embed = discord.Embed(
            title=f"🎉 {ctx.author.display_name} chose {pokemon['displayName']}!",
            description=(
                f"Your journey begins! You received:\n"
                f"• **{pokemon['displayName']}** (Lv.5) · {type_badges(pokemon['types'])}\n"
                f"• 🔴 **10** Poké Balls · 🔵 **3** Great Balls · ⚫ **1** Ultra Ball\n"
                f"• 💰 **500 {currency}**\n\n"
                f"**Next steps**\n"
                f"• Wild Pokémon appear in the spawn channel — tap a ball to throw!\n"
                f"• `pokestop` once a day for free items · `heal` to patch up your team\n"
                f"• `pokehelp` for everything else"
            ),
            color=type_color(pokemon),
        )
        art = artwork_url(pokemon)
        if art:
            embed.set_image(url=art)
        await ctx.send(embed=embed)

    # ── Profile ───────────────────────────────────────────────────────────────

    @commands.command(name="profile", aliases=["trainer"])
    async def profile(self, ctx: commands.Context, user: Optional[discord.Member] = None) -> None:
        """View your (or another trainer's) Trainer Card."""
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

        lead     = player["pokemon"][player["activePokemonIndex"]] if player["pokemon"] else None
        shinies  = sum(1 for p in player["pokemon"] if p.get("shiny"))
        total    = player["wins"] + player["losses"]
        win_rate = f"{(player['wins'] / total * 100):.0f}%" if total else "—"
        balance  = await _get_balance(target)
        currency = await _currency_name(ctx.guild)
        dex_count = len(player.get("caughtDex", []))
        rank_label, _ = _dex_rank(dex_count)

        embed = discord.Embed(
            title=f"🪪 {target.display_name}'s Trainer Card",
            description=(
                f"{rank_label}\n{_dex_progress_bar(dex_count, MAX_POKEMON, 12)} "
                f"**{dex_count}/{MAX_POKEMON}** Pokédex ({dex_count / MAX_POKEMON:.1%})"
            ),
            color=type_color(lead) if lead else COLORS["purple"],
        )
        embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
        if lead:
            art = artwork_url(lead)
            if art:
                embed.set_thumbnail(url=art)

        embed.add_field(name=f"💰 {currency}", value=f"{balance:,}", inline=True)
        embed.add_field(name="📦 Pokémon", value=f"{len(player['pokemon'])}", inline=True)
        embed.add_field(name="✨ Shinies", value=str(shinies), inline=True)
        embed.add_field(name="⚔️ Battles", value=f"{player['wins']}W / {player['losses']}L ({win_rate})", inline=True)
        streak = player.get("pokestopStreak", 0)
        embed.add_field(name="🔥 Pokéstop streak", value=f"{streak} day{'s' if streak != 1 else ''}", inline=True)
        balls = player["items"]
        embed.add_field(
            name="🎯 Balls",
            value=f"🔴 {balls.get('pokeball', 0)} · 🔵 {balls.get('greatball', 0)} · ⚫ {balls.get('ultraball', 0)}",
            inline=True,
        )
        party_lines = []
        for i, p in enumerate(party_mons(player)):
            status = "💀" if p["stats"]["hp"] <= 0 else ("🟢" if p["stats"]["hp"] >= p["stats"]["maxHp"] else "🟡")
            crown = "👑 " if i == 0 else ""
            party_lines.append(f"{status} {crown}**{mon_name(p)}** · Lv.{p['level']}")
        embed.add_field(name="🎒 Party", value="\n".join(party_lines) or "_Empty_", inline=False)
        reg = datetime.fromtimestamp(player["registeredAt"], tz=timezone.utc).strftime("%Y-%m-%d")
        embed.set_footer(text=f"Trainer since {reg}")

        view = None
        if target == ctx.author:
            needs_heal = any(p["stats"]["hp"] < p["stats"]["maxHp"] for p in party_mons(player))
            view = LinkButtonsView(self, ctx.author.id, show_heal=needs_heal)
        msg = await ctx.send(embed=embed, view=view or None)
        if view is not None:
            view.message = msg

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

        def build_embed(pg: int) -> discord.Embed:
            pg     = max(1, min(pg, pages))
            offset = (pg - 1) * per_page
            chunk  = player["pokemon"][offset: offset + per_page]
            embed  = discord.Embed(
                title=f"📦 {target.display_name}'s Pokémon ({total})",
                color=COLORS["blue"],
            )
            embed.set_footer(
                text=f"Page {pg}/{pages} · 👑 lead · 🎒 in party · 🌟 can evolve · `pokemon <page>` to jump"
            )
            party_uids = set(player.get("party", []))
            for i, p in enumerate(chunk):
                idx     = offset + i + 1
                tags    = []
                if idx - 1 == player["activePokemonIndex"]:
                    tags.append("👑")
                elif p.get("uid") in party_uids:
                    tags.append("🎒")
                if p.get("evoNotified"):
                    tags.append("🌟")
                xp_cur  = p.get("xp", 0)
                xp_nxt  = p.get("xpToNext") or self._xp_for_level(p["level"])
                embed.add_field(
                    name=f"`{idx}` {mon_name(p)} · Lv.{p['level']} {' '.join(tags)}",
                    value=(
                        f"{type_badges(p['types'])}\n"
                        f"{hp_line(p, 8)}\n"
                        f"XP {xp_bar(xp_cur, xp_nxt, 8)} `{xp_cur}/{xp_nxt}`"
                    ),
                    inline=True,
                )
            return embed

        page  = max(1, min(page, pages))
        embed = build_embed(page)

        if pages == 1:
            await ctx.send(embed=embed)
            return

        async def async_build(pg: int) -> discord.Embed:
            return build_embed(pg)

        view = PaginatedView(async_build, pages, page, ctx.author.id)
        view.message = await ctx.send(embed=embed, view=view)

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
        ensure_party(player)  # lead becomes party[0] (added to the party if absent)
        await self._save_player(ctx.author, player)
        embed = pokemon_embed(poke, f"✅ Switched to {poke['displayName']}!", show_xp=True)
        await ctx.send(embed=embed)

    # ── Party ─────────────────────────────────────────────────────────────────

    @commands.group(name="party", invoke_without_command=True)
    async def party(self, ctx: commands.Context, user: Optional[discord.Member] = None) -> None:
        """View your battle party (up to 6 Pokémon). Subcommands: add, remove, move, clear.

        Your party is the lineup that fights in raids — when one faints, the next
        steps in. Slot 1 (the lead) is also your active Pokémon for catching and
        1v1 battles. Use `party add <slot>` with a number from your `pokemon` list.
        """
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

        embed = self._party_embed(target, player, own=target == ctx.author)
        view = None
        if target == ctx.author and any(p["stats"]["hp"] < p["stats"]["maxHp"] for p in party_mons(player)):
            view = HealShortcutView(self, ctx.author.id)
        msg = await ctx.send(embed=embed, view=view or None)
        if view is not None:
            view.message = msg

    def _party_embed(self, target: discord.abc.User, player: dict, *, own: bool = True) -> discord.Embed:
        mons = party_mons(player)
        embed = discord.Embed(
            title=f"🎒 {target.display_name}'s Party ({len(mons)}/6)",
            color=type_color(mons[0]) if mons else COLORS["purple"],
        )
        for i, p in enumerate(mons):
            crown = "👑 " if i == 0 else ""
            embed.add_field(
                name=f"{i + 1}. {crown}{mon_name(p)} · Lv.{p['level']}",
                value=f"{type_badges(p['types'])}\n{hp_line(p, 8)}",
                inline=True,
            )
        if not mons:
            embed.description = "_Empty._"
        if mons:
            art = artwork_url(mons[0])
            if art:
                embed.set_thumbnail(url=art)
        if own:
            embed.set_footer(text="party add <slot> · party remove <pos> · party move <pos> <new_pos> · party clear · heal")
        return embed

    async def party_from_interaction(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        player = await self._get_player(member) if isinstance(member, discord.Member) else None
        if not player:
            await interaction.response.send_message("Start your journey first with `start`!", ephemeral=True)
            return
        await interaction.response.send_message(embed=self._party_embed(member, player), ephemeral=True)

    async def bag_from_interaction(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        player = await self._get_player(member) if isinstance(member, discord.Member) else None
        if not player:
            await interaction.response.send_message("Start your journey first with `start`!", ephemeral=True)
            return
        await interaction.response.send_message(embed=await self._bag_embed(member, player), ephemeral=True)

    @party.command(name="add")
    async def party_add(self, ctx: commands.Context, slot: int) -> None:
        """Add a Pokémon from your collection to your party. `slot` = number from `pokemon`."""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey with `start`!"))
            return
        idx = slot - 1
        if idx < 0 or idx >= len(player["pokemon"]):
            await ctx.send(embed=error_embed(
                f"Invalid slot. You have {len(player['pokemon'])} Pokémon (slots 1–{len(player['pokemon'])})."
            ))
            return
        mon = player["pokemon"][idx]
        party = player.setdefault("party", [])
        if mon["uid"] in party:
            await ctx.send(embed=error_embed(f"**{mon['displayName']}** is already in your party!"))
            return
        if len(party) >= 6:
            await ctx.send(embed=error_embed(
                "Your party is full (6/6)! Use `party remove <pos>` to make room first."
            ))
            return
        party.append(mon["uid"])
        ensure_party(player)
        await self._save_player(ctx.author, player)
        await ctx.send(embed=success_embed(
            f"Added **{mon['displayName']}** to your party! ({len(player['party'])}/6)"
        ))

    @party.command(name="remove", aliases=["rem"])
    async def party_remove(self, ctx: commands.Context, position: int) -> None:
        """Remove a Pokémon from your party by its party position (1–6)."""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey with `start`!"))
            return
        mons = party_mons(player)
        pos = position - 1
        if pos < 0 or pos >= len(mons):
            await ctx.send(embed=error_embed(f"Invalid position. Your party has {len(mons)} Pokémon."))
            return
        if len(mons) <= 1:
            await ctx.send(embed=error_embed("You can't remove your last party Pokémon!"))
            return
        if pos == 0:
            await ctx.send(embed=error_embed(
                "That's your lead Pokémon — switch your lead first with `active <slot>`, "
                "then remove the old one."
            ))
            return
        removed = mons[pos]
        player["party"].remove(removed["uid"])
        ensure_party(player)
        await self._save_player(ctx.author, player)
        await ctx.send(embed=success_embed(
            f"Removed **{removed['displayName']}** from your party. ({len(player['party'])}/6)"
        ))

    @party.command(name="move", aliases=["swap", "order"])
    async def party_move(self, ctx: commands.Context, position: int, new_position: int) -> None:
        """Reorder your party. `party move 4 2` moves the 4th Pokémon to slot 2.

        Moving a Pokémon to position 1 makes it your lead (active) Pokémon."""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey with `start`!"))
            return
        mons = party_mons(player)
        a, b = position - 1, new_position - 1
        if a < 0 or a >= len(mons) or b < 0 or b >= len(mons):
            await ctx.send(embed=error_embed(f"Positions must be between 1 and {len(mons)}."))
            return
        party = player["party"]
        uid = party.pop(a)
        party.insert(b, uid)
        # If position 1 changed, the lead changed — sync activePokemonIndex to match
        new_lead_uid = party[0]
        player["activePokemonIndex"] = uid_index(player, new_lead_uid)
        ensure_party(player)
        await self._save_player(ctx.author, player)
        new_mons = party_mons(player)
        order = " → ".join(f"{p['displayName']}" for p in new_mons)
        await ctx.send(embed=success_embed(f"Party reordered!\n**Lineup:** {order}"))

    @party.command(name="clear")
    async def party_clear(self, ctx: commands.Context) -> None:
        """Reset your party down to just your lead Pokémon."""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey with `start`!"))
            return
        lead_idx = player.get("activePokemonIndex", 0)
        if not (0 <= lead_idx < len(player["pokemon"])):
            lead_idx = 0
        player["party"] = [player["pokemon"][lead_idx]["uid"]]
        ensure_party(player)
        await self._save_player(ctx.author, player)
        await ctx.send(embed=success_embed("Party cleared — only your lead Pokémon remains."))

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

    # ── Evolve ────────────────────────────────────────────────────────────────

    @commands.command(name="evolve")
    async def evolve(self, ctx: commands.Context, slot: int = 0) -> None:
        """Evolve a Pokémon when it's ready. Usage: `evolve [slot]` (defaults to active)."""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey with `start`!"))
            return
        if self._get_battle_by_user(ctx.author.id):
            await ctx.send(embed=error_embed("You can't evolve Pokémon during a battle!"))
            return

        idx = (slot - 1) if slot > 0 else player["activePokemonIndex"]
        if idx < 0 or idx >= len(player["pokemon"]):
            await ctx.send(embed=error_embed(
                f"Invalid slot. You have {len(player['pokemon'])} Pokémon (slots 1–{len(player['pokemon'])})."
            ))
            return

        poke = player["pokemon"][idx]

        async with ctx.typing():
            evo = await self._fetch_evolution_target(poke)

        if evo is None:
            await ctx.send(embed=error_embed(
                f"**{poke['displayName']}** is fully evolved — there's nowhere left to go!"
            ))
            return

        if not self._evolution_eligible(poke, evo):
            min_lvl = evo["min_level"] or 20
            await ctx.send(embed=error_embed(
                f"**{poke['displayName']}** can't evolve yet!\n"
                f"Reach **Lv.{min_lvl}** to unlock evolution into **{evo['name'].capitalize()}**.\n"
                f"_(Currently Lv.{poke['level']})_"
            ))
            return

        # Confirm — evolving is irreversible
        evo_name = evo["name"].replace("-", " ").title()
        nick_line = f"• Nickname **{poke['nickname']}** will be kept\n" if poke.get("nickname") else ""
        confirm_embed = discord.Embed(
            title=f"✨ Evolve {mon_name(poke)} → {evo_name}?",
            description=(
                f"• Types, sprite and stats update to the evolved form\n"
                f"{nick_line}"
                f"• This can't be undone"
            ),
            color=COLORS["yellow"],
        )
        art = artwork_url(poke)
        if art:
            confirm_embed.set_thumbnail(url=art)
        view = ConfirmView(ctx.author.id, confirm_label=f"Evolve into {evo_name}", confirm_emoji="✨",
                           cancel_label="Not now", timeout=45)
        view.message = await ctx.send(embed=confirm_embed, view=view)
        await view.wait()
        if not view.value:
            await ctx.send(embed=info_embed(f"Kept **{mon_name(poke)}** as it is — that's totally fine!", COLORS["gray"]))
            return

        # ── Perform the evolution ─────────────────────────────────────────────
        async with ctx.typing():
            try:
                new_raw = await fetch_pokemon(self._session, evo["name"])
            except Exception:
                await ctx.send(embed=error_embed(
                    f"Couldn't fetch data for **{evo_name}** right now. Try again in a moment!"
                ))
                return

        old_name   = poke["displayName"]
        new_types  = [t["type"]["name"] for t in new_raw["types"]]
        new_sprite = (
            new_raw["sprites"].get("front_shiny") or new_raw["sprites"]["front_default"]
            if poke.get("shiny")
            else new_raw["sprites"]["front_default"]
        )

        # Stat upgrade: recalculate based on new base stats at current level,
        # then add a flat bonus so evolution feels like a real power spike
        lvl = poke["level"]
        old_stats  = dict(poke["stats"])
        new_stats: Dict[str, int] = {}
        for s in new_raw["stats"]:
            base = s["base_stat"]
            name = s["stat"]["name"]
            if name == "hp":
                new_max = math.floor(((2 * base * lvl) / 100) + lvl + 10)
                # Preserve current HP ratio so the Pokémon doesn't suddenly heal
                hp_ratio = old_stats["hp"] / max(old_stats["maxHp"], 1)
                new_stats["maxHp"] = new_max
                new_stats["hp"]    = max(1, math.floor(new_max * hp_ratio))
            else:
                new_stats[name] = math.floor(((2 * base * lvl) / 100) + 5)

        # Mutate the Pokémon in-place so slot index and nickname are preserved
        poke["name"]        = new_raw["name"]
        poke["displayName"] = new_raw["name"].capitalize()
        poke["id"]          = new_raw["id"]
        poke["types"]       = new_types
        poke["stats"]       = new_stats
        poke["spriteUrl"]   = new_sprite
        poke["evoNotified"] = False   # reset so next-stage evo can notify

        # Register evolved form in dex
        self._update_dex(player, poke)
        await self._save_player(ctx.author, player)

        embed = discord.Embed(
            title=f"🎉 What? {old_name} evolved into {poke['displayName']}!",
            description=(
                f"{'✨ ' if poke.get('shiny') else ''}"
                f"**{old_name}** → **{poke['displayName']}** {dex_no(poke['id'])}\n\n"
                f"{type_badges(new_types)}\n"
                f"❤️ {hp_line(poke)} _(was {old_stats['hp']}/{old_stats['maxHp']})_"
            ),
            color=type_color(poke),
        )
        art = artwork_url(poke)
        if art:
            embed.set_image(url=art)
        embed.set_footer(text="Use 'pokemon' to see your updated collection!")
        await ctx.send(embed=embed)

        # Immediately check if the newly-evolved form can evolve again
        await self._notify_evo_if_ready(ctx, player, idx)

    # ── Dex ───────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_dex_embed(raw: dict, shiny: bool = False) -> discord.Embed:
        """Build a Pokédex entry embed for either the normal or shiny form."""
        types_str = " / ".join(type_tag(t["type"]["name"]) for t in raw["types"])
        stats_lines = []
        for s in raw["stats"]:
            name = s["stat"]["name"].replace("-", " ").title()
            bar  = "█" * round(s["base_stat"] / 15) + "░" * max(0, 10 - round(s["base_stat"] / 15))
            stats_lines.append(f"**{name}**: {bar} {s['base_stat']}")

        abilities = ", ".join(
            a["ability"]["name"].replace("-", " ").title() + (" _(hidden)_" if a["is_hidden"] else "")
            for a in raw["abilities"]
        )

        sprites  = raw["sprites"]
        official = (sprites.get("other", {}) or {}).get("official-artwork", {}) or {}
        if shiny:
            thumb = sprites.get("front_shiny") or sprites.get("front_default")
            big   = official.get("front_shiny") or official.get("front_default") or thumb
        else:
            thumb = sprites.get("front_default")
            big   = official.get("front_default") or thumb

        embed = discord.Embed(
            title=f"{dex_no(raw['id'])} — {raw['name'].replace('-', ' ').title()}{'  ✨ Shiny' if shiny else ''}",
            color=type_color({"shiny": shiny, "types": [t["type"]["name"] for t in raw["types"]]}),
        )
        if thumb:
            embed.set_thumbnail(url=thumb)
        if big:
            embed.set_image(url=big)

        embed.add_field(name="Type",            value=types_str, inline=True)
        embed.add_field(name="Height / Weight", value=f"{raw['height']/10}m / {raw['weight']/10}kg", inline=True)
        embed.add_field(name="Abilities",       value=abilities, inline=False)
        embed.add_field(name="Base Stats",      value="\n".join(stats_lines), inline=False)
        embed.set_footer(
            text="✨ Showing the shiny form — tap 🎨 for normal"
            if shiny else "Tap ✨ Shiny Form to see the shiny sprite"
        )
        return embed

    @commands.command(name="dex")
    async def dex(self, ctx: commands.Context, *, query: str) -> None:
        """Look up a Pokémon in the Pokédex. Usage: `dex <name or number> [shiny]`

        Add `shiny` (e.g. `dex charizard shiny`) to open straight to the shiny form,
        or use the button to flip between normal and shiny art."""
        # Parse an optional leading/trailing "shiny" keyword
        start_shiny = False
        q = query.strip()
        tokens = q.split()
        if tokens and tokens[-1].lower() == "shiny":
            start_shiny = True
            tokens = tokens[:-1]
        elif tokens and tokens[0].lower() == "shiny":
            start_shiny = True
            tokens = tokens[1:]
        lookup = " ".join(tokens).strip() or q  # fall back to raw query if only "shiny" given

        async with ctx.typing():
            try:
                raw = await fetch_pokemon(self._session, clean_slug(lookup))
            except Exception:
                await ctx.send(embed=error_embed(
                    f"Couldn't find a Pokémon called **{lookup}**. Check the spelling!"
                ))
                return

        sprites   = raw["sprites"]
        official  = (sprites.get("other", {}) or {}).get("official-artwork", {}) or {}
        has_shiny = bool(sprites.get("front_shiny") or official.get("front_shiny"))
        if start_shiny and not has_shiny:
            start_shiny = False  # nothing to show; open normal

        player = await self._get_player(ctx.author)
        status = None
        if player:
            owned = sum(1 for p in player["pokemon"] if p["id"] == raw["id"])
            if raw["id"] in player.get("caughtDex", []):
                status = f"✅ **Registered in your Pokédex** · you own **{owned}**"
            else:
                status = "🆕 **Not in your Pokédex yet** — catch one to register it!"

        def build(raw_: dict, shiny_: bool) -> discord.Embed:
            e = self._build_dex_embed(raw_, shiny_)
            if status:
                e.description = status
            return e

        embed = build(raw, start_shiny)
        view  = DexShinyView(
            build, raw,
            shiny=start_shiny, has_shiny=has_shiny, author_id=ctx.author.id,
        )
        view.message = await ctx.send(embed=embed, view=view)

    # ── Catch ─────────────────────────────────────────────────────────────────

    @commands.command(name="catch")
    async def catch(self, ctx: commands.Context, ball: Optional[str] = None, berry: str = "") -> None:
        """Catch the wild Pokémon in this channel. Usage: `catch [ball] [berry]`

        Leave out the ball to throw your cheapest one. Berries: `razzberry` (better odds),
        `nanabberry` (no damage on a miss), `pinapberry` (2× credits).
        Tip: the buttons under each spawn do the same thing in one tap.
        """
        if ball and ball.lower().replace(" ", "") in BERRY_EFFECTS and not berry:
            ball, berry = None, ball  # `catch razzberry` — berry with the default ball
        out = await self._do_catch(ctx.author, ctx.channel, ball, berry or None)
        if out["error"] is not None:
            msg = await ctx.send(embed=out["error"], view=out["view"] or None)
        else:
            msg = await ctx.send(embeds=out["embeds"], view=out["view"] or None)
        if out["view"] is not None:
            out["view"].message = msg

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

        confirm_embed = discord.Embed(
            title=f"👋 Release {mon_name(poke)}?",
            description=(
                f"Lv.{poke['level']} · {type_badges(poke['types'])}{shiny_tag}\n\n"
                f"**You'll receive**\n"
                f"💰 **{base_credits} {currency}**\n"
                f"{ball_emoji} **{ball_qty}\u00d7 {BALL_NAMES[ball_reward]}**"
            ),
            color=COLORS["orange"],
        )
        spr = sprite_url(poke)
        if spr:
            confirm_embed.set_thumbnail(url=spr)
        view = ConfirmView(ctx.author.id, confirm_label="Release", confirm_emoji="👋",
                           cancel_label="Keep it", danger=True, timeout=30)
        view.message = await ctx.send(embed=confirm_embed, view=view)
        await view.wait()
        if not view.value:
            await ctx.send(embed=info_embed(f"Kept **{mon_name(poke)}** — good choice!", COLORS["gray"]))
            return

        # Apply rewards and remove the Pokémon (keep the same lead, even if indexes shift)
        lead_uid = player["pokemon"][player["activePokemonIndex"]]["uid"]
        player["pokemon"].pop(idx)
        lead_idx = uid_index(player, lead_uid)
        player["activePokemonIndex"] = lead_idx if lead_idx >= 0 else 0
        ensure_party(player)  # drop the released Pokémon's uid from the party

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

        await ctx.send(embed=await self._bag_embed(ctx.author, player))

    async def _bag_embed(self, member: discord.Member, player: dict) -> discord.Embed:
        items    = player.get("items", {})
        healing  = items.get("healing", {})
        tms      = items.get("tms", [])
        berries  = items.get("berries", {})
        balance  = await _get_balance(member)
        currency = await _currency_name(member.guild)
        emoji_of = {i["id"]: i["emoji"] for i in SHOP_ITEMS}

        def lines(ids_labels, counts) -> str:
            out = []
            for item_id, label in ids_labels:
                n = counts.get(item_id, 0)
                out.append(f"{label} ×**{n}**" if n else f"{label} _×0_")
            return "\n".join(out)

        embed = discord.Embed(title=f"👜 {member.display_name}'s Bag", color=COLORS["teal"])
        embed.description = f"💰 **{balance:,} {currency}**"
        embed.add_field(name="🎯 Balls",
                        value=lines([(b, f"{emoji_of[b]} {n}") for b, n in BALL_NAMES.items()], items), inline=True)
        embed.add_field(name="💊 Medicine", value=lines(list(ITEM_NAMES.items()), healing), inline=True)
        embed.add_field(name="🍓 Berries", value=lines(list(BERRY_NAMES.items()), berries), inline=True)
        if tms:
            counts: Dict[str, int] = {}
            for slug in tms:
                counts[slug] = counts.get(slug, 0) + 1
            tm_lines = []
            for slug, n in counts.items():
                info = TM_LIST.get(slug)
                emoji = TM_TYPE_EMOJI.get(info["type"], "💿") if info else "💿"
                tm_lines.append(f"{emoji} **{info['name'] if info else slug}**{f' ×{n}' if n > 1 else ''} `{slug}`")
            embed.add_field(name="💿 TMs", value="\n".join(tm_lines)[:1024], inline=False)
        embed.set_footer(text="shop to buy · heal to patch up your team · usetm to teach moves")
        return embed

    # ── Shop ──────────────────────────────────────────────────────────────────

    async def _shop_embed(self, member: discord.Member) -> discord.Embed:
        balance  = await _get_balance(member)
        currency = await _currency_name(member.guild)
        prices   = await self._get_shop_prices(member.guild)
        embed = discord.Embed(
            title="🛒 PokéMart",
            description=f"Your balance: **💰 {balance:,} {currency}**\nPick an item below, then tap a Buy button.",
            color=COLORS["yellow"],
        )
        for title, category in (("🎯 Poké Balls", "balls"), ("💊 Medicine", "healing"), ("🍓 Berries", "berries")):
            embed.add_field(
                name=title,
                value="\n".join(
                    f"{i['emoji']} **{i['name']}** — {prices.get(i['id'], i['price']):,}\n_{i['desc']}_"
                    for i in SHOP_ITEMS if i["category"] == category
                ),
                inline=True,
            )
        embed.set_footer(text="Or type: buy <item> [amount] · TMs live in `tms`")
        return embed

    async def _buy_items(self, member: discord.Member, item: str, amount: int) -> discord.Embed:
        """Buy `amount` of a PokéMart item. Caller must hold the trainer lock."""
        player = await self._get_player(member)
        if not player:
            return error_embed("Start your journey first with `start`!")
        item = item.lower().replace(" ", "").replace("-", "").replace("é", "e")
        shop_item = next((i for i in SHOP_ITEMS if i["id"] == item), None)
        if not shop_item:
            names = ", ".join(f"`{i['id']}`" for i in SHOP_ITEMS)
            return error_embed(f"Unknown item. Available: {names}")

        amount     = max(1, min(amount, 99))
        prices     = await self._get_shop_prices(member.guild)
        total_cost = prices.get(shop_item["id"], shop_item["price"]) * amount
        currency   = await _currency_name(member.guild)
        if not await _withdraw(member, total_cost):
            balance = await _get_balance(member)
            return error_embed(
                f"You need **{total_cost:,} {currency}** for {amount}× {shop_item['name']} but only have **{balance:,}**."
            )

        if shop_item["category"] == "balls":
            player["items"][item] = player["items"].get(item, 0) + amount
            owned = player["items"][item]
        elif shop_item["category"] == "berries":
            berries = player["items"].setdefault("berries", {})
            berries[item] = berries.get(item, 0) + amount
            owned = berries[item]
        else:
            healing = player["items"].setdefault("healing", {})
            healing[item] = healing.get(item, 0) + amount
            owned = healing[item]
        await self._save_player(member, player)
        balance = await _get_balance(member)
        return success_embed(
            f"Bought **{amount}× {shop_item['emoji']} {shop_item['name']}** for **{total_cost:,} {currency}**!\n"
            f"You now have **{owned}** · Balance: **{balance:,} {currency}**"
        )

    @commands.command(name="shop", aliases=["mart", "pokemart"])
    async def shop(self, ctx: commands.Context) -> None:
        """Browse the PokéMart and buy with a couple of clicks."""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return
        prices = await self._get_shop_prices(ctx.guild)
        view = ShopView(self, ctx.author.id, SHOP_ITEMS, prices)
        view.message = await ctx.send(embed=await self._shop_embed(ctx.author), view=view)

    async def shop_purchase_interaction(self, interaction: discord.Interaction, view: ShopView, item: str, qty: int) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return
        await interaction.response.defer()
        lock = self._lock_for(member.guild.id, member.id)
        if not await lock.acquire(timeout=5):
            await interaction.followup.send("⏳ You're busy with another action — try again in a moment.", ephemeral=True)
            return
        try:
            result = await self._buy_items(member, item, qty)
        finally:
            lock.release()
        await interaction.edit_original_response(embed=await self._shop_embed(member), view=view)
        await interaction.followup.send(embed=result, ephemeral=True)

    # ── Buy ───────────────────────────────────────────────────────────────────

    @commands.command(name="buy")
    async def buy(self, ctx: commands.Context, item: str, amount: int = 1) -> None:
        """Buy items from the PokéMart. Usage: `buy <item> [amount]`"""
        await ctx.send(embed=await self._buy_items(ctx.author, item, amount))

    # ── Use ───────────────────────────────────────────────────────────────────

    @commands.command(name="use")
    async def use(self, ctx: commands.Context, item: str, slot: str = "") -> None:
        """Use a healing item. Usage: `use <item> [slot | all]`

        `use revive all` revives every fainted party Pokémon, `use potion all`
        tops up every hurt one — and offers to buy anything you're short of.
        For everything at once just use `heal`.
        """
        if slot.lower() in ("all", "party", "everyone", "team"):
            item_slug = item.lower().replace(" ", "").replace("-", "")
            if item_slug not in HEAL_AMOUNTS:
                await ctx.send(embed=error_embed(f"Unknown item. Valid: {', '.join(HEAL_AMOUNTS.keys())}"))
                return
            mode = "revive" if item_slug == "revive" else "potion"
            only = None if item_slug == "revive" else item_slug
            embed, view = await self._start_heal(ctx.author, "party", mode, only_item=only)
            msg = await ctx.send(embed=embed, view=view or None)
            if view is not None:
                view.message = msg
            return
        if slot and not slot.isdigit():
            await ctx.send(embed=error_embed("Usage: `use <item> [slot]` or `use <item> all`."))
            return
        slot = int(slot) if slot else 0
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

        desc = (
            f"⭐ **{mon_name(poke)}** was revived!\n{hp_line(poke)}"
            if item == "revive"
            else f"💊 **{mon_name(poke)}** recovered HP!\n{hp_line(poke)}"
        )
        embed = discord.Embed(title=f"Used {ITEM_NAMES[item]}!", description=desc, color=COLORS["green"])
        spr = sprite_url(poke)
        if spr:
            embed.set_thumbnail(url=spr)
        embed.set_footer(text=f"{ITEM_NAMES[item]}s remaining: {player['items']['healing'][item]}")
        await ctx.send(embed=embed)

    # ── Healing (heal everything at once) ────────────────────────────────────

    @staticmethod
    def _heal_targets(player: dict, scope: str) -> List[dict]:
        return player["pokemon"] if scope == "all" else party_mons(player)

    @staticmethod
    def _apply_heal_plan(player: dict, plan) -> None:
        by_uid = {m.get("uid"): m for m in player["pokemon"]}
        for mp in plan.mons:
            mon = by_uid.get(mp.uid)
            if mon is not None:
                mon["stats"]["hp"] = mp.hp_after
        healing = player["items"].setdefault("healing", {})
        for item, qty in plan.use.items():
            healing[item] = max(0, healing.get(item, 0) - qty)

    @staticmethod
    def _items_text(counts: Dict[str, int]) -> str:
        return ", ".join(f"{qty}× {ITEM_NAMES[item]}" for item, qty in counts.items() if qty) or "—"

    def _heal_result_embed(self, member: discord.Member, plan, *, spent: int = 0, currency: str = "") -> discord.Embed:
        lines = []
        for mp in plan.mons:
            if not mp.changed:
                lines.append(f"⚠️ **{mp.name}** — {mp.unresolved or 'unchanged'}")
                continue
            icon = "⭐" if mp.revived else "💊"
            before = "💀" if mp.hp_before <= 0 else f"{mp.hp_before}"
            after_note = f" _({mp.unresolved})_" if mp.unresolved else ""
            lines.append(
                f"{icon} **{mp.name}** {before} → {hp_bar(mp.hp_after, mp.max_hp, 8)} `{mp.hp_after}/{mp.max_hp}`{after_note}"
            )
        done = plan.complete
        embed = discord.Embed(
            title="🏥 Pokémon Center" + (" — all patched up!" if done else " — partly healed"),
            description="\n".join(lines)[:3900] or "Nothing needed healing.",
            color=COLORS["green"] if done else COLORS["orange"],
        )
        if plan.use:
            embed.add_field(name="🎒 Used from your bag", value=self._items_text(plan.use), inline=False)
        if plan.buy:
            embed.add_field(
                name="🛒 Bought & used",
                value=f"{self._items_text(plan.buy)}\n**−{spent:,} {currency}**",
                inline=False,
            )
        if not done:
            embed.set_footer(text="Run `heal` again any time to buy what's still missing.")
        return embed

    @staticmethod
    def _parse_heal_args(args: Tuple[str, ...]) -> Tuple[str, str]:
        scope, mode = "party", "all"
        for raw in args:
            word = raw.lower().strip()
            if word in ("all", "everything", "collection", "box"):
                scope = "all"
            elif word in ("party", "team"):
                scope = "party"
            elif word in ("revive", "revives", "fainted"):
                mode = "revive"
            elif word in ("potion", "potions", "hurt", "hp"):
                mode = "potion"
        return scope, mode

    async def _start_heal(
        self, member: discord.Member, scope: str, mode: str, *, only_item: Optional[str] = None
    ) -> Tuple[discord.Embed, Optional[HealView]]:
        """Heal straight away if the bag covers it; otherwise show a buy / use-what-I-have choice.

        Caller must hold the trainer lock.
        """
        player = await self._get_player(member)
        if not player:
            return error_embed("Start your journey first with `start`!"), None
        if self._get_battle_by_user(member.id):
            return error_embed("You can't heal in the middle of a battle!"), None

        targets = self._heal_targets(player, scope)
        inventory = player["items"].get("healing", {})
        prices = await self._get_shop_prices(member.guild)
        owned = plan_heal(targets, inventory, prices, mode=mode, only_item=only_item)
        where = "collection" if scope == "all" else "party"
        if owned.targets == 0:
            what = {"revive": "fainted", "potion": "hurt", "all": "fainted or hurt"}[mode]
            return info_embed(f"💚 Nobody in your {where} is {what} — everyone's ready to go!", COLORS["green"]), None

        if owned.complete:
            self._apply_heal_plan(player, owned)
            await self._save_player(member, player)
            return self._heal_result_embed(member, owned), None

        full = plan_heal(targets, inventory, prices, mode=mode, buy=True, only_item=only_item)
        balance  = await _get_balance(member)
        currency = await _currency_name(member.guild)
        lines = []
        for mp in owned.mons:
            hp_txt = "💀 Fainted" if mp.hp_before <= 0 else f"`{mp.hp_before}/{mp.max_hp}` HP"
            lines.append(f"• **{mp.name}** — {hp_txt}")
        embed = discord.Embed(
            title=f"🏥 Pokémon Center — {owned.targets} Pokémon need care",
            description="\n".join(lines)[:3000],
            color=COLORS["pink"],
        )
        embed.add_field(name="🎒 Your bag covers", value=self._items_text(owned.use) if owned.use else "Nothing yet", inline=False)
        embed.add_field(
            name="🛒 To heal everyone, buy",
            value=f"{self._items_text(full.buy)}\nCost: **{full.cost:,} {currency}** · You have **{balance:,}**",
            inline=False,
        )
        if balance < full.cost:
            embed.set_footer(text=f"You're {full.cost - balance:,} {currency} short for the full heal.")
        view = HealView(
            self, member.id, scope=scope, mode=mode,
            can_partial=owned.does_anything, cost=full.cost, currency=currency,
            affordable=balance >= full.cost,
        )
        view.only_item = only_item  # type: ignore[attr-defined]
        return embed, view

    async def execute_heal(self, interaction: discord.Interaction, view: HealView, *, buy: bool) -> None:
        """HealView button handler."""
        member = interaction.user
        if not isinstance(member, discord.Member):
            return
        await interaction.response.defer()

        async def tell(text: str) -> None:
            await interaction.followup.send(text, ephemeral=True)

        try:
            async with self._trainer(member, timeout=5) as player:
                if player is None:
                    await tell("Start your journey first with `start`!")
                    return
                if self._get_battle_by_user(member.id):
                    await tell("You can't heal in the middle of a battle!")
                    return
                targets = self._heal_targets(player, view.scope)
                prices = await self._get_shop_prices(member.guild)
                plan = plan_heal(targets, player["items"].get("healing", {}), prices, mode=view.mode,
                                 buy=buy, only_item=getattr(view, "only_item", None))
                currency = await _currency_name(member.guild)
                if buy and plan.cost > 0 and not await _withdraw(member, plan.cost):
                    balance = await _get_balance(member)
                    await tell(
                        f"💸 That costs **{plan.cost:,} {currency}** but you only have **{balance:,}**. "
                        f"Try **Use only what I have**, or earn more by catching and battling!"
                    )
                    return
                self._apply_heal_plan(player, plan)
                embed = self._heal_result_embed(member, plan, spent=plan.cost if buy else 0, currency=currency)
        except TrainerBusy:
            await tell("⏳ You're busy with another action — try again in a moment.")
            return
        view.disable_all()
        view.stop()
        await interaction.edit_original_response(embed=embed, view=view)

    async def heal_from_interaction(self, interaction: discord.Interaction, scope: str) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lock = self._lock_for(member.guild.id, member.id)
        if not await lock.acquire(timeout=5):
            await interaction.followup.send("⏳ You're busy with another action — try again in a moment.", ephemeral=True)
            return
        try:
            embed, view = await self._start_heal(member, scope, "all")
        finally:
            lock.release()
        await interaction.followup.send(embed=embed, view=view or discord.utils.MISSING, ephemeral=True)

    @commands.command(name="heal", aliases=["healall", "pokecenter"])
    async def heal(self, ctx: commands.Context, *options: str) -> None:
        """Revive and heal your whole party in one go — the Pokémon Center.

        `heal` — revive fainted party Pokémon and restore everyone to full HP
        `heal all` — do the same for your entire collection
        `heal revive` / `heal potion` — only revive, or only top up HP

        Items in your bag are used first (smartly: no Max Potions on scratches).
        If you're short, you'll get a button to buy exactly what's missing.
        """
        scope, mode = self._parse_heal_args(options)
        embed, view = await self._start_heal(ctx.author, scope, mode)
        msg = await ctx.send(embed=embed, view=view or None)
        if view is not None:
            view.message = msg

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
        if opponent.id in self._challenges or ctx.author.id in self._challenges:
            await ctx.send(embed=error_embed("One of you already has a battle challenge waiting for an answer."))
            return

        challenger_poke = player["pokemon"][player["activePokemonIndex"]]
        opp_poke        = opp_data["pokemon"][opp_data["activePokemonIndex"]]
        if challenger_poke["stats"]["hp"] <= 0:
            await ctx.send(
                embed=error_embed(f"Your lead **{mon_name(challenger_poke)}** has fainted! Heal up first."),
                view=HealShortcutView(self, ctx.author.id),
            )
            return
        if opp_poke["stats"]["hp"] <= 0:
            await ctx.send(embed=error_embed(
                f"{opponent.display_name}'s **{mon_name(opp_poke)}** has fainted — they need to `heal` first!"
            ))
            return

        embed = discord.Embed(
            title="⚔️ Battle Challenge!",
            description=(
                f"**{ctx.author.display_name}** challenges **{opponent.display_name}**!\n\n"
                f"🔴 **{mon_name(challenger_poke)}** · Lv.{challenger_poke['level']} · {type_badges(challenger_poke['types'])}\n"
                f"🔵 **{mon_name(opp_poke)}** · Lv.{opp_poke['level']} · {type_badges(opp_poke['types'])}\n\n"
                f"{opponent.mention}, do you accept? _(60 seconds)_"
            ),
            color=COLORS["orange"],
        )
        art = artwork_url(challenger_poke)
        if art:
            embed.set_thumbnail(url=art)
        view = RequestView(ctx.author.id, opponent.id, accept_label="Fight!", timeout=60)
        self._challenges[opponent.id] = {"challengerId": ctx.author.id, "expires": time.time() + 60}
        try:
            view.message = await ctx.send(content=opponent.mention, embed=embed, view=view,
                                          allowed_mentions=discord.AllowedMentions(users=[opponent]))
            await view.wait()
        finally:
            self._challenges.pop(opponent.id, None)

        if view.value != "accept":
            text = {
                "decline": f"❌ {opponent.display_name} declined the challenge.",
                "cancel":  f"↩️ {ctx.author.display_name} withdrew the challenge.",
            }.get(view.value, f"⌛ {opponent.display_name} didn't respond in time.")
            await ctx.send(embed=info_embed(text, COLORS["gray"]))
            return

        # Re-read both saves — things may have changed while we waited.
        player   = await self._get_player(ctx.author)
        opp_data = await self._get_player(opponent)
        if not player or not opp_data:
            await ctx.send(embed=error_embed("Battle cancelled — a trainer's save couldn't be loaded."))
            return
        if self._get_battle_by_user(ctx.author.id) or self._get_battle_by_user(opponent.id):
            await ctx.send(embed=error_embed("Battle cancelled — one of you joined another battle."))
            return
        p1_pokemon = copy.deepcopy(player["pokemon"][player["activePokemonIndex"]])
        p2_pokemon = copy.deepcopy(opp_data["pokemon"][opp_data["activePokemonIndex"]])
        if p1_pokemon["stats"]["hp"] <= 0 or p2_pokemon["stats"]["hp"] <= 0:
            await ctx.send(embed=error_embed("Battle cancelled — a lead Pokémon has fainted."))
            return

        battle_id = f"{ctx.author.id}_{opponent.id}_{int(time.time())}"
        battle = {
            "player1":   {"id": ctx.author.id, "username": ctx.author.display_name, "pokemon": p1_pokemon, "moveUsed": None, "lastMoveAt": time.time()},
            "player2":   {"id": opponent.id,   "username": opponent.display_name,   "pokemon": p2_pokemon, "moveUsed": None, "lastMoveAt": time.time()},
            "turn":      1,
            "log":       [],
            "status":    "active",
            "processing": False,
            "startedAt": time.time(),
            "guildId":   ctx.guild.id,
            "channelId": ctx.channel.id,
        }
        self._battles[battle_id] = battle
        self.bot.loop.create_task(self._battle_timeout_watcher(ctx.guild, battle_id, ctx.channel))

        embed = self._build_battle_embed(battle, ["The battle begins! Tap **⚔️ Choose Move** to pick your attack."])
        bview = BattleView(self, battle_id, [ctx.author.id, opponent.id])
        bview.message = await ctx.send(embed=embed, view=bview)

    async def _battle_timeout_watcher(
        self, guild: discord.Guild, battle_id: str, channel: discord.TextChannel
    ) -> None:
        """Periodically check if a battle has gone AFK and auto-forfeit if so."""
        while True:
            await asyncio.sleep(30)
            battle = self._battles.get(battle_id)
            if not battle or battle["status"] != "active":
                return
            if battle.get("processing"):
                continue

            now = time.time()
            p1, p2 = battle["player1"], battle["player2"]
            afk = other = None
            if not p1["moveUsed"] and (now - p1["lastMoveAt"]) >= BATTLE_TIMEOUT:
                afk, other = p1, p2
            elif not p2["moveUsed"] and (now - p2["lastMoveAt"]) >= BATTLE_TIMEOUT:
                afk, other = p2, p1
            if afk:
                battle["status"] = "finished"
                await self._end_battle(guild, battle_id, other["id"], afk["id"], channel)
                with suppress(discord.HTTPException):
                    await channel.send(embed=discord.Embed(
                        title="⏰ Battle timeout!",
                        description=(
                            f"**{afk['username']}** took too long and forfeited.\n"
                            f"**{other['username']}** wins and receives 100 {await _currency_name(guild)}."
                        ),
                        color=COLORS["orange"],
                    ))
                return

    async def _submit_move(
        self, member: discord.Member, channel: discord.abc.Messageable, move_name: str
    ) -> Optional[str]:
        """Lock in a move. Returns an error message, or None on success.

        When both moves are in, resolves the turn and posts the result.
        """
        result = self._get_battle_by_user(member.id)
        if not result:
            return "You're not in a battle! Use `battle @user` to start one."
        battle_id, battle = result
        if battle["status"] != "active":
            return "That battle is already over."
        move_name = move_name.lower().strip().replace(" ", "-")
        my_side = battle["player1"] if battle["player1"]["id"] == member.id else battle["player2"]
        if move_name not in my_side["pokemon"]["moves"]:
            known = ", ".join(f"`{m}`" for m in my_side["pokemon"]["moves"] if m)
            return f"{mon_name(my_side['pokemon'])} doesn't know **{move_name}**!\nKnown moves: {known}"
        if my_side["moveUsed"] or battle.get("processing"):
            return "You've already chosen a move this turn — waiting for your opponent…"

        my_side["moveUsed"]   = move_name
        my_side["lastMoveAt"] = time.time()
        both_ready = battle["player1"]["moveUsed"] and battle["player2"]["moveUsed"]
        if not both_ready:
            with suppress(discord.HTTPException):
                await channel.send(embed=info_embed(
                    f"✅ **{my_side['username']}** has locked in a move. Waiting for the opponent…",
                    COLORS["purple"],
                ))
            return None

        # Claim the turn synchronously so two simultaneous submissions can't both resolve it.
        battle["processing"] = True
        try:
            turn_result = await self._process_turn(battle_id)
        finally:
            battle["processing"] = False
        if not turn_result:
            self._battles.pop(battle_id, None)
            return "Something went wrong processing this turn — the battle was cancelled."

        updated_battle, turn_log, winner = turn_result
        embed = self._build_battle_embed(updated_battle, turn_log)
        guild = member.guild
        if winner:
            loser_id = battle["player2"]["id"] if winner["id"] == battle["player1"]["id"] else battle["player1"]["id"]
            lvl_msgs = await self._end_battle(guild, battle_id, winner["id"], loser_id, channel)
            currency = await _currency_name(guild)
            embed.color = COLORS["yellow"]
            embed.title = f"🏆 {winner['username']} wins the battle!"
            embed.set_footer(text=f"{winner['username']} earned 100 {currency} and battle XP · loser's Pokémon fainted — `heal` to recover")
            with suppress(discord.HTTPException):
                await channel.send(embed=embed)
                if lvl_msgs:
                    await channel.send(embed=discord.Embed(description="\n".join(lvl_msgs), color=COLORS["green"]))
        else:
            bview = BattleView(self, battle_id, [battle["player1"]["id"], battle["player2"]["id"]])
            with suppress(discord.HTTPException):
                bview.message = await channel.send(embed=embed, view=bview)
        return None

    async def battle_open_picker(self, interaction: discord.Interaction, battle_id: str) -> None:
        battle = self._battles.get(battle_id)
        if not battle or battle["status"] != "active":
            await interaction.response.send_message("This battle is over.", ephemeral=True)
            return
        side = battle["player1"] if battle["player1"]["id"] == interaction.user.id else battle["player2"]
        if side["moveUsed"]:
            await interaction.response.send_message(
                f"You've locked in **{pretty_move(side['moveUsed'])}** — waiting for your opponent.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        moves = []
        for slug in [m for m in side["pokemon"]["moves"] if m]:
            info = {"slug": slug, "power": None, "type": None}
            with suppress(Exception):
                md = await fetch_move_data(self._session, slug)
                info["power"] = md.get("power")
                info["type"] = (md.get("type") or {}).get("name")
            moves.append(info)
        foe = battle["player2"] if side is battle["player1"] else battle["player1"]
        embed = discord.Embed(
            title=f"⚔️ {mon_name(side['pokemon'])} — choose a move",
            description=(
                f"{hp_line(side['pokemon'], 8)}\n\nVs **{mon_name(foe['pokemon'])}** · {type_badges(foe['pokemon']['types'])}"
            ),
            color=type_color(side["pokemon"]),
        )
        view = MovePickerView(self, interaction.user.id, battle_id, moves)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    async def battle_submit_interaction(
        self, interaction: discord.Interaction, battle_id: str, slug: str, *, picker: MovePickerView
    ) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or interaction.channel is None:
            return
        picker.disable_all()
        picker.stop()
        await interaction.response.edit_message(
            content=f"✅ You chose **{pretty_move(slug)}**!", embed=None, view=picker
        )
        err = await self._submit_move(member, interaction.channel, slug)
        if err:
            await interaction.followup.send(embed=error_embed(err), ephemeral=True)

    async def battle_forfeit(self, interaction: discord.Interaction, battle_id: str) -> None:
        battle = self._battles.get(battle_id)
        if not battle or battle["status"] != "active" or battle.get("processing"):
            await interaction.response.send_message("This battle can't be forfeited right now.", ephemeral=True)
            return
        me = battle["player1"] if battle["player1"]["id"] == interaction.user.id else battle["player2"]
        other = battle["player2"] if me is battle["player1"] else battle["player1"]
        battle["status"] = "finished"
        await interaction.response.send_message(
            embed=info_embed(f"🏳️ **{me['username']}** forfeited — **{other['username']}** wins!", COLORS["orange"])
        )
        await self._end_battle(interaction.guild, battle_id, other["id"], me["id"], interaction.channel)

    @commands.command(name="move")
    async def move(self, ctx: commands.Context, *, move_name: str) -> None:
        """Use a move in your current battle. Usage: `move <move_name>` (or tap ⚔️ Choose Move)."""
        err = await self._submit_move(ctx.author, ctx.channel, move_name)
        if err:
            await ctx.send(embed=error_embed(err))

    @commands.command(name="forfeit", aliases=["surrender"])
    async def forfeit(self, ctx: commands.Context) -> None:
        """Give up your current battle."""
        result = self._get_battle_by_user(ctx.author.id)
        if not result:
            await ctx.send(embed=error_embed("You're not in a battle."))
            return
        battle_id, battle = result
        if battle.get("processing"):
            await ctx.send(embed=error_embed("A turn is resolving — try again in a second."))
            return
        me = battle["player1"] if battle["player1"]["id"] == ctx.author.id else battle["player2"]
        other = battle["player2"] if me is battle["player1"] else battle["player1"]
        battle["status"] = "finished"
        await self._end_battle(ctx.guild, battle_id, other["id"], me["id"], ctx.channel)
        await ctx.send(embed=info_embed(f"🏳️ **{me['username']}** forfeited — **{other['username']}** wins!", COLORS["orange"]))

    # ── Pokédex ───────────────────────────────────────────────────────────────

    @commands.command(name="pokedex", aliases=["pdex", "dexpage", "dp"])
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

        seen: Dict[int, str] = {}
        for pk in player["pokemon"]:
            if pk["id"] in caught_ids and pk["id"] not in seen:
                seen[pk["id"]] = pk["displayName"]

        all_entries = [(pid, seen.get(pid, f"#{pid}")) for pid in sorted(caught_ids)]
        per_page    = 30
        total_pages = max(1, math.ceil(total_caught / per_page))
        remaining   = MAX_POKEMON - total_caught
        rank_label, rank_color = _dex_rank(total_caught)
        prog_bar  = _dex_progress_bar(total_caught, MAX_POKEMON, length=20)

        def build_dex_embed(pg: int) -> discord.Embed:
            pg     = max(1, min(pg, total_pages))
            offset = (pg - 1) * per_page
            chunk  = all_entries[offset:offset + per_page]

            if pg == 1:
                if completion >= 100:   flavour = "🎉 You've caught them all! Legendary!"
                elif completion >= 75:  flavour = f"🔥 Almost there — just **{remaining}** left!"
                elif completion >= 50:  flavour = "💪 Halfway there — keep it up!"
                elif completion >= 25:  flavour = f"🌱 Making good progress — **{remaining}** still out there!"
                else:                   flavour = f"🗺️ Your journey is just beginning — **{remaining}** left to discover!"
            else:
                flavour = ""

            title = (
                f"📖 {target.display_name}'s Pokédex"
                if total_pages == 1
                else f"📖 {target.display_name}'s Pokédex — Page {pg}/{total_pages}"
            )
            embed = discord.Embed(title=title, color=rank_color)
            embed.description = (
                f"{rank_label}\n\n{prog_bar}\n"
                f"**{total_caught}** / **{MAX_POKEMON}** — {completion:.1f}% complete"
                + (f"\n\n{flavour}" if flavour else "")
            )
            col_size = 10
            cols = [chunk[i:i + col_size] for i in range(0, len(chunk), col_size)]
            for col in cols:
                val = "\n".join(f"🔵 `#{pid:04d}` {name}" for pid, name in col)
                embed.add_field(name="\u200b", value=val, inline=True)
            embed.set_footer(text=f"Page {pg}/{total_pages} · {remaining} species still to find!")
            return embed

        async def async_dex_build(pg: int) -> discord.Embed:
            return build_dex_embed(pg)

        page  = max(1, min(page, total_pages))
        embed = build_dex_embed(page)

        if total_pages == 1:
            await ctx.send(embed=embed)
            return

        view = PaginatedView(async_dex_build, total_pages, page, ctx.author.id)
        view.message = await ctx.send(embed=embed, view=view)

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
        all_members = await self.config.all_members(ctx.guild)
        for member_id, p in all_members.items():
            member = ctx.guild.get_member(int(member_id))
            if member is None or member.bot or p.get("registeredAt") is None:
                continue
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

    @commands.command(name="pokestats")
    async def pokestats(self, ctx: commands.Context) -> None:
        """Show server-wide encounter and rarity statistics."""
        st = await self.config.guild(ctx.guild).encounter_stats()
        spawns = st.get("wild_spawns", 0)
        shiny = st.get("shiny_spawns", 0)
        rate = f"1 in {spawns / shiny:.1f}" if shiny else "No recorded shinies yet"
        embed = discord.Embed(title="📊 PokéBot Server Statistics", color=COLORS["blue"])
        embed.add_field(name="Wild encounters", value=f"{spawns:,}", inline=True)
        embed.add_field(name="Wild catches", value=f"{st.get('wild_catches', 0):,}", inline=True)
        embed.add_field(name="Fled", value=f"{st.get('fled', 0):,}", inline=True)
        embed.add_field(name="Shiny spawns", value=f"{shiny:,}\n{rate}", inline=True)
        embed.add_field(name="Legendary spawns", value=f"{st.get('legendary_spawns', 0):,}", inline=True)
        embed.add_field(name="Mythical spawns", value=f"{st.get('mythical_spawns', 0):,}", inline=True)
        embed.add_field(name="Raid wins", value=f"{st.get('raid_wins', 0):,}", inline=True)
        embed.add_field(name="Raid catches", value=f"{st.get('raid_catches', 0):,}", inline=True)
        embed.set_footer(text="Statistics begin when this updated cog is installed.")
        await ctx.send(embed=embed)

    @commands.command(name="pokehelp")
    async def pokehelp(self, ctx: commands.Context) -> None:
        """Show all PokéBot commands."""
        prefix   = ctx.clean_prefix
        currency = await _currency_name(ctx.guild)

        def make_page(pg: int) -> discord.Embed:
            embed = discord.Embed(color=COLORS["blue"])
            if pg == 1:
                embed.title = "📖 PokéBot — Getting Started & Catching"
                embed.add_field(
                    name="🌟 Getting Started",
                    value="\n".join([
                        f"`{prefix}start` — Begin your journey & pick a starter",
                        f"`{prefix}profile [@user]` — View trainer profile",
                        f"`{prefix}pokehelp` — Show this help (use buttons to browse)",
                    ]),
                    inline=False,
                )
                embed.add_field(
                    name="🎯 Catching",
                    value="\n".join([
                        "• Tap **🔴 / 🔵 / ⚫** under a wild Pokémon to throw that ball",
                        "• Tap **📖 Do I have it?** to privately see if it's in your Pokédex, your odds, and to feed a berry",
                        f"`{prefix}catch [ball] [berry]` — same thing by command (ball optional)",
                        "• Higher-level lead Pokémon improve catch rate",
                        "• Wild Pokémon only deal damage on **failed** throws",
                    ]),
                    inline=False,
                )
                embed.add_field(
                    name="🏥 Healing",
                    value="\n".join([
                        f"`{prefix}heal` — revive + fully heal your whole party in one go",
                        f"`{prefix}heal all` — the same for your entire collection",
                        f"`{prefix}heal revive` · `{prefix}heal potion` — only revives / only HP",
                        f"`{prefix}use revive all` · `{prefix}use potion all` — use one item type on everyone",
                        "• Short on items? You'll get a **🛒 Buy missing & heal all** button",
                    ]),
                    inline=False,
                )
                embed.add_field(
                    name="🍓 Berries",
                    value="\n".join([
                        f"`{prefix}berries` — View your berry pouch",
                        f"`{prefix}catch <ball> <berry>` — e.g. `catch ultraball razzberry`",
                        "• 🍓 **Razz Berry** — catch rate ×1.5",
                        "• 🍌 **Nanab Berry** — no damage on a failed throw",
                        "• 🍍 **Pinap Berry** — 2× credits on a successful catch",
                        f"_Get berries from `{prefix}pokestop` or `{prefix}shop`_",
                    ]),
                    inline=False,
                )
            elif pg == 2:
                embed.title = "📖 PokéBot — Your Collection & XP"
                embed.add_field(
                    name="📦 Your Collection",
                    value="\n".join([
                        f"`{prefix}pokemon [page] [@user]` — Browse your Pokémon (shows XP bars)",
                        f"`{prefix}active <slot>` — Set your lead (party slot 1) Pokémon",
                        f"`{prefix}party [@user]` — View your 6-Pokémon battle party",
                        f"`{prefix}party add <slot>` · `remove <pos>` · `move <pos> <newpos>` · `clear`",
                        f"`{prefix}nickname <slot> <name>` — Give a Pokémon a nickname",
                        f"`{prefix}release <slot>` — Release a Pokémon for rewards",
                        f"`{prefix}dex <name or #> [shiny]` — Look up any Pokémon (✨ toggle for shiny)",
                        f"`{prefix}pokedex [page] [@user]` — View your Pokédex",
                        f"`{prefix}dex <name>` also tells you if it's already registered",
                    ]),
                    inline=False,
                )
                embed.add_field(
                    name="⭐ XP & Levelling",
                    value="\n".join([
                        "• Catch XP: `wild_level × 1.5 + 5` (40% on failed throws)",
                        "• Win XP: `80 + opp_level × 3 + turns × 2` (max 400)",
                        "• Loss XP: 30% consolation — losing isn't wasted!",
                        "• Level-ups grow HP, Atk, Def and Speed automatically",
                    ]),
                    inline=False,
                )
                embed.add_field(
                    name="✨ Evolution",
                    value="\n".join([
                        f"`{prefix}evolve [slot]` — Evolve a Pokémon (defaults to active)",
                        "• Evolution is **optional** — you'll get a notification when ready",
                        "• The Pokémon's nickname, shiny status and slot are preserved",
                        "• Stats are recalculated from the evolved form's base stats",
                    ]),
                    inline=False,
                )
            elif pg == 3:
                embed.title = "📖 PokéBot — Battling, Raids & Trading"
                embed.add_field(
                    name="⚔️ Battling",
                    value="\n".join([
                        f"`{prefix}battle @user` — Challenge someone (they tap **Fight!**)",
                        "• Tap **⚔️ Choose Move** for a private move menu (or `move <name>`)",
                        f"`{prefix}forfeit` — give up the current battle",
                        f"• Winner earns 100 {currency} + full XP",
                        "• Loser earns 30% consolation XP · 3-min AFK timeout",
                    ]),
                    inline=False,
                )
                embed.add_field(
                    name="🔴 Raids",
                    value="\n".join([
                        f"**⚔️ Join Raid** button / `{prefix}raidjoin` — Join with your **whole party**",
                        f"`{prefix}raidswap <pos>` — Bring a party member to the front",
                        f"`{prefix}raidheal <item>` — Heal your active Pokémon (every 2 turns)",
                        f"`{prefix}raidstatus` — Check boss HP, party, and turn count",
                        f"`{prefix}raidstart [pokemon]` — *(Admin)* Start a raid boss",
                        f"`{prefix}raidcancel` — *(Admin)* Cancel the active raid",
                        "• Co-op: each trainer's party fights; when a mon faints the next steps in",
                        "• You're only out when your **entire party** faints — bring all 6!",
                        "• Boss HP scales to your team's strength; boss damage can't one-shot",
                        "• Victory: Raid Ball throws (more for survivors), XP & credits by damage",
                        "• ⭐–⭐⭐⭐⭐⭐ tiers: higher = tougher boss, better rewards",
                        "• Shiny bosses boost catch chance for everyone",
                    ]),
                    inline=False,
                )
                embed.add_field(
                    name="🔄 Trading",
                    value="\n".join([
                        f"`{prefix}trade @user <your_slot> <their_slot>` — Offer a trade",
                        "• The other trainer taps **Accept trade** or **Decline**",
                        "• Both trainers' Pokédexes update after a successful trade",
                        "• Cannot trade during battles or your last Pokémon",
                    ]),
                    inline=False,
                )
                embed.add_field(
                    name="🏆 Leaderboard",
                    value=f"`{prefix}pokeboard [wins|caught|shinies|balance]` — Server rankings",
                    inline=False,
                )
            elif pg == 4:
                embed.title = "📖 PokéBot — Shop, TMs & Daily"
                embed.add_field(
                    name="🛒 Shop",
                    value="\n".join([
                        f"`{prefix}shop` — PokéMart: pick an item, tap **Buy ×1 / ×5 / ×10**",
                        f"`{prefix}buy <item> [amount]` — Buy by command",
                        f"`{prefix}use <item> [slot|all]` — Use a healing item",
                        f"`{prefix}bag` — View your bag & {currency} balance",
                    ]),
                    inline=False,
                )
                embed.add_field(
                    name="💿 TMs",
                    value="\n".join([
                        f"`{prefix}tms [page]` — Browse TM Shop (25 moves, 4 pages)",
                        f"`{prefix}buytm <move>` — Buy a TM  e.g. `buytm thunderbolt`",
                        f"`{prefix}usetm <move> <poke_slot> <move_slot>` — Teach the TM",
                        "• TMs are one-use — choose carefully!",
                    ]),
                    inline=False,
                )
                embed.add_field(
                    name="🏪 Daily Pokéstop",
                    value="\n".join([
                        f"`{prefix}pokestop` — Spin for free daily items + streak bonuses",
                        "• 🔥 **Day 3+:** +1 Great Ball, +1 Razz Berry every spin",
                        "• 🌟 **Every 7th day:** +2 Ultra Balls, +2 Pinap Berries, +300 credits",
                        "• Miss a day and your streak resets — spin every day!",
                    ]),
                    inline=False,
                )
            elif pg == 5:
                embed.title = "📖 PokéBot — Admin & Tips"
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
                        "• Uncaught Pokémon **flee** after a configurable timeout (default 4 h)",
                        "• Shiny Pokémon are **1/256** — extremely rare!",
                        "• Your active Pokémon's level affects catch rate vs the wild Pokémon",
                        "• Catch XP is awarded even on a failed throw",
                        "• Check your `pokedex` — 1,025 species to complete!",
                        f"• All earnings go straight to your server {currency} balance",
                    ]),
                    inline=False,
                )
            embed.set_footer(text=f"Page {pg}/5 · PokéBot — Good luck on your journey, Trainer!")
            return embed

        async def async_help_build(pg: int) -> discord.Embed:
            return make_page(pg)

        embed = make_page(1)
        view  = PaginatedView(async_help_build, 5, 1, ctx.author.id)
        view.message = await ctx.send(embed=embed, view=view)


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

        all_tms     = list(TM_LIST.items())
        per_page    = 8
        total_pages = max(1, math.ceil(len(all_tms) / per_page))

        def build_tms_embed(pg: int) -> discord.Embed:
            pg     = max(1, min(pg, total_pages))
            offset = (pg - 1) * per_page
            chunk  = all_tms[offset:offset + per_page]
            embed  = discord.Embed(
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
            embed.set_footer(text=f"Page {pg}/{total_pages} · {len(all_tms)} TMs total")
            return embed

        async def async_tms_build(pg: int) -> discord.Embed:
            return build_tms_embed(pg)

        page  = max(1, min(page, total_pages))
        embed = build_tms_embed(page)

        if total_pages == 1:
            await ctx.send(embed=embed)
            return

        view = PaginatedView(async_tms_build, total_pages, page, ctx.author.id)
        view.message = await ctx.send(embed=embed, view=view)

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
        """Spin a Pokéstop for free daily items and streak bonuses! Resets at midnight ET."""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return

        # ── Streak / cooldown logic ───────────────────────────────────────────
        EASTERN   = zoneinfo.ZoneInfo("America/New_York")
        now       = datetime.now(tz=EASTERN)
        today_str = now.strftime("%Y-%m-%d")
        last_stop = player.get("lastPokestop")
        streak    = player.get("pokestopStreak", 0)

        if last_stop == today_str:
            midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            delta    = midnight - now
            hours, remainder = divmod(int(delta.total_seconds()), 3600)
            minutes          = remainder // 60
            await ctx.send(embed=error_embed(
                f"You already spun this Pokéstop today!\n"
                f"Come back in **{hours}h {minutes}m** when it resets at midnight Eastern.\n"
                f"🔥 Current streak: **{streak} day{'s' if streak != 1 else ''}**"
            ))
            return

        # Check if streak is still alive (must spin on consecutive days)
        if last_stop:
            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            if last_stop == yesterday:
                streak += 1   # consecutive day — keep the streak going
            else:
                streak = 1    # missed a day — reset
        else:
            streak = 1        # first ever spin

        # ── Build reward bundle ───────────────────────────────────────────────
        reward_balls   = {}
        reward_healing = {}
        reward_berries = {}
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

        # Berries — always a small chance; better chance with higher streaks
        berry_base = 0.40 + min(streak - 1, 6) * 0.05   # up to 70% at streak 7+
        if random.random() < berry_base:
            razz = random.randint(1, 2)
            reward_berries["razzberry"] = razz
            lines.append(f"🍓 {razz}× Razz Berry")
        if random.random() < (berry_base * 0.6):
            nanab = random.randint(1, 2)
            reward_berries["nanabberry"] = nanab
            lines.append(f"🍌 {nanab}× Nanab Berry")
        if random.random() < (berry_base * 0.4):
            pinap = 1
            reward_berries["pinapberry"] = pinap
            lines.append(f"🍍 {pinap}× Pinap Berry")

        # Small currency bonus
        if random.random() < 0.6:
            reward_credits = random.randint(25, 150)
            lines.append(f"💰 {reward_credits} {currency}")

        # ── Streak bonuses (on top of normal rewards) ─────────────────────────
        streak_bonus_lines = []
        if streak >= 7 and streak % 7 == 0:
            # Weekly milestone: a guaranteed Ultra Ball + Pinap Berry + big credit bump
            reward_balls["ultraball"]    = reward_balls.get("ultraball", 0) + 2
            reward_berries["pinapberry"] = reward_berries.get("pinapberry", 0) + 2
            reward_credits              += 300
            streak_bonus_lines.append("🌟 **Weekly milestone bonus!** +2 Ultra Balls, +2 Pinap Berries, +300 credits!")
        elif streak >= 3:
            # 3-day+ bonus: extra ball and berry
            reward_balls["greatball"]  = reward_balls.get("greatball", 0) + 1
            reward_berries["razzberry"] = reward_berries.get("razzberry", 0) + 1
            streak_bonus_lines.append("✨ **Streak bonus!** +1 Great Ball, +1 Razz Berry")

        # ── Apply rewards ─────────────────────────────────────────────────────
        items   = player.setdefault("items", {"pokeball": 0, "greatball": 0, "ultraball": 0, "healing": {}, "tms": [], "berries": {}})
        healing = items.setdefault("healing", {})
        berries = items.setdefault("berries", {})

        for ball, qty in reward_balls.items():
            items[ball] = items.get(ball, 0) + qty
        for item, qty in reward_healing.items():
            healing[item] = healing.get(item, 0) + qty
        for berry, qty in reward_berries.items():
            berries[berry] = berries.get(berry, 0) + qty

        player["lastPokestop"]   = today_str
        player["pokestopStreak"] = streak
        await self._save_player(ctx.author, player)

        if reward_credits:
            await _deposit(ctx.author, reward_credits)

        # ── Streak display ────────────────────────────────────────────────────
        streak_flames = "🔥" * min(streak, 7)
        streak_header = f"{streak_flames} **Day {streak} streak!**"
        if streak == 1:
            streak_header = "🗺️ Spin #1 — start your streak!"

        embed = discord.Embed(
            title="🏪 Pokéstop",
            description=(
                f"**{ctx.author.display_name}** spun a Pokéstop!\n"
                f"{streak_header}\n\n"
                + "\n".join(lines)
                + ("\n\n" + "\n".join(streak_bonus_lines) if streak_bonus_lines else "")
                + "\n\n_Come back tomorrow to keep your streak alive!_"
            ),
            color=COLORS["blue"],
        )
        next_milestone = 7 - (streak % 7)
        if next_milestone < 7:
            embed.set_footer(text=f"🌟 Weekly bonus in {next_milestone} day{'s' if next_milestone != 1 else ''} · Resets daily at midnight Eastern (ET)")
        else:
            embed.set_footer(text="Resets daily at midnight Eastern (ET)")
        await ctx.send(embed=embed)


    # ── Berries ───────────────────────────────────────────────────────────────

    @commands.command(name="berries", aliases=["myberries"])
    async def berries(self, ctx: commands.Context) -> None:
        """View your berry inventory."""
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey with `start`!"))
            return
        bag = player.get("items", {}).get("berries", {})
        lines = []
        for slug, label in BERRY_NAMES.items():
            count = bag.get(slug, 0)
            effect = {
                "razzberry":  "Catch rate ×1.5",
                "nanabberry": "No damage on fail",
                "pinapberry": "2× catch credits",
            }[slug]
            lines.append(f"{label} ×**{count}** — _{effect}_")
        embed = discord.Embed(
            title=f"🍓 {ctx.author.display_name}'s Berry Pouch",
            description="\n".join(lines) or "_No berries — spin a `pokestop` or buy some with `buy`!_",
            color=COLORS["green"],
        )
        embed.set_footer(text="Use berries: catch <ball> <berry>  e.g.  catch ultraball razzberry")
        await ctx.send(embed=embed)

    # ── Trade ─────────────────────────────────────────────────────────────────

    @commands.command(name="trade")
    async def trade(self, ctx: commands.Context, target: discord.Member, your_slot: int, their_slot: int) -> None:
        """Offer a Pokémon trade. Usage: `trade @user <your_slot> <their_slot>`

        The other trainer gets Accept / Decline buttons. The exact Pokémon shown are
        the ones that get swapped — even if either collection is reordered meanwhile.
        """
        if target == ctx.author:
            await ctx.send(embed=error_embed("You can't trade with yourself!"))
            return
        if target.bot:
            await ctx.send(embed=error_embed("You can't trade with a bot!"))
            return
        player = await self._get_player(ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey with `start`!"))
            return
        target_data = await self._get_player(target)
        if not target_data:
            await ctx.send(embed=error_embed(f"{target.display_name} hasn't started their journey yet!"))
            return
        if self._get_battle_by_user(ctx.author.id) or self._get_battle_by_user(target.id):
            await ctx.send(embed=error_embed("Can't trade while either trainer is in a battle!"))
            return
        if target.id in self._trades or ctx.author.id in self._trades:
            await ctx.send(embed=error_embed("One of you already has a trade offer waiting — finish that first."))
            return

        your_idx, their_idx = your_slot - 1, their_slot - 1
        if not (0 <= your_idx < len(player["pokemon"])):
            await ctx.send(embed=error_embed(f"You don't have a Pokémon in slot {your_slot}. You have {len(player['pokemon'])}."))
            return
        if not (0 <= their_idx < len(target_data["pokemon"])):
            await ctx.send(embed=error_embed(
                f"{target.display_name} doesn't have a Pokémon in slot {their_slot}. They have {len(target_data['pokemon'])}."
            ))
            return
        if len(player["pokemon"]) <= 1 or len(target_data["pokemon"]) <= 1:
            await ctx.send(embed=error_embed("Neither trainer can trade away their last Pokémon!"))
            return

        your_poke, their_poke = player["pokemon"][your_idx], target_data["pokemon"][their_idx]
        your_uid, their_uid = your_poke["uid"], their_poke["uid"]

        def side(p: dict) -> str:
            return f"**{mon_name(p)}** · Lv.{p['level']}\n{dex_no(p['id'])} · {type_badges(p['types'])}"

        embed = discord.Embed(
            title="🔄 Trade Offer",
            description=f"{target.mention}, **{ctx.author.display_name}** wants to trade with you! _(2 minutes)_",
            color=COLORS["orange"],
        )
        embed.add_field(name=f"📤 {ctx.author.display_name} gives", value=side(your_poke), inline=True)
        embed.add_field(name=f"📥 {target.display_name} gives", value=side(their_poke), inline=True)
        art = artwork_url(your_poke)
        if art:
            embed.set_thumbnail(url=art)
        art2 = artwork_url(their_poke)
        if art2:
            embed.set_image(url=art2)

        view = RequestView(ctx.author.id, target.id, accept_label="Accept trade", timeout=120)
        self._trades[target.id] = {"offerer_id": ctx.author.id}
        try:
            view.message = await ctx.send(content=target.mention, embed=embed, view=view,
                                          allowed_mentions=discord.AllowedMentions(users=[target]))
            await view.wait()
        finally:
            self._trades.pop(target.id, None)

        if view.value != "accept":
            text = {
                "decline": f"❌ {target.display_name} declined the trade.",
                "cancel":  f"↩️ {ctx.author.display_name} withdrew the offer.",
            }.get(view.value, f"⌛ {target.display_name} didn't respond — trade expired.")
            await ctx.send(embed=info_embed(text, COLORS["gray"]))
            return

        # ── Execute: lock both trainers (in a fixed order to avoid deadlocks) ──
        first, second = sorted((ctx.author, target), key=lambda m: m.id)
        lock_a = self._lock_for(ctx.guild.id, first.id)
        lock_b = self._lock_for(ctx.guild.id, second.id)
        if not await lock_a.acquire(timeout=10):
            await ctx.send(embed=error_embed("Trade failed — a trainer is busy. Try again."))
            return
        try:
            if not await lock_b.acquire(timeout=10):
                await ctx.send(embed=error_embed("Trade failed — a trainer is busy. Try again."))
                return
            try:
                player      = await self._get_player(ctx.author)
                target_data = await self._get_player(target)
                if not player or not target_data:
                    await ctx.send(embed=error_embed("Trade failed — a trainer's save couldn't be loaded."))
                    return
                if self._get_battle_by_user(ctx.author.id) or self._get_battle_by_user(target.id):
                    await ctx.send(embed=error_embed("Trade failed — one of you entered a battle."))
                    return
                yi, ti = uid_index(player, your_uid), uid_index(target_data, their_uid)
                if yi < 0 or ti < 0:
                    await ctx.send(embed=error_embed("Trade failed — one of those Pokémon is no longer available."))
                    return
                poke_y = player["pokemon"][yi]
                poke_t = target_data["pokemon"][ti]
                player["pokemon"][yi]      = poke_t
                target_data["pokemon"][ti] = poke_y
                poke_t["evoNotified"] = False
                poke_y["evoNotified"] = False
                self._update_dex(player, poke_t)
                self._update_dex(target_data, poke_y)
                ensure_party(player)
                ensure_party(target_data)
                await self._save_player(ctx.author, player)
                await self._save_player(target, target_data)
            finally:
                lock_b.release()
        finally:
            lock_a.release()

        done = discord.Embed(
            title="🔄 Trade complete!",
            description=(
                f"**{ctx.author.display_name}** received **{mon_name(poke_t)}** (Lv.{poke_t['level']})\n"
                f"**{target.display_name}** received **{mon_name(poke_y)}** (Lv.{poke_y['level']})"
            ),
            color=COLORS["green"],
        )
        art = artwork_url(poke_t)
        if art:
            done.set_thumbnail(url=art)
        await ctx.send(embed=done)

    # ══════════════════════════════════════════════════════════════════════════
    # RAID SYSTEM  (party-based, rebalanced)
    # ══════════════════════════════════════════════════════════════════════════
    #
    # How a raid works now:
    #   • Each trainer joins with their whole PARTY (up to 6 Pokémon) as a bench.
    #   • Every turn, each trainer's current active mon attacks the boss. When a
    #     mon faints, the next non-fainted party member auto-steps in — a trainer
    #     is only knocked out once their ENTIRE party faints.
    #   • Boss HP is sized to the team's actual damage output so the fight lasts a
    #     target number of turns regardless of player level (no more "can't dent
    #     the boss"), and boss damage is capped as a % of each mon's max HP so it
    #     can never one-shot low-level Pokémon (no more instant wipes).
    #
    # ── Tuning knobs (tweak these to rebalance) ───────────────────────────────
    #   RAID_TARGET_TURNS  — roughly how many turns of clean hitting a boss lasts
    #   RAID_BOSS_DMG_FRAC — boss hit = this fraction of the target's max HP (±15%)
    #   RAID_MIN_BOSS_HP   — floor so low tiers aren't trivially short
    RAID_TARGET_TURNS  = {1: 6, 2: 8,  3: 10, 4: 12, 5: 14}
    RAID_BOSS_DMG_FRAC = {1: 0.09, 2: 0.12, 3: 0.15, 4: 0.18, 5: 0.21}
    RAID_MIN_BOSS_HP   = 120
    RAID_MAX_TURNS     = 40
    RAID_TURN_DELAY    = 4   # seconds between turns so the log reads naturally

    # ── Legendary / rare boss pool ────────────────────────────────────────────
    # Names are lowercase PokéAPI slugs (case-sensitive endpoint). Display names
    # are derived automatically from the API response.
    RAID_BOSS_POOL: List[Dict] = [
        # ── Legendary birds / beasts / titans (frequent) ──────────────────────
        {"id": 144, "name": "articuno",    "tier": "legendary"},
        {"id": 145, "name": "zapdos",      "tier": "legendary"},
        {"id": 146, "name": "moltres",     "tier": "legendary"},
        {"id": 243, "name": "raikou",      "tier": "legendary"},
        {"id": 244, "name": "entei",       "tier": "legendary"},
        {"id": 245, "name": "suicune",     "tier": "legendary"},
        {"id": 377, "name": "regirock",    "tier": "legendary"},
        {"id": 378, "name": "regice",      "tier": "legendary"},
        {"id": 379, "name": "registeel",   "tier": "legendary"},
        {"id": 380, "name": "latias",      "tier": "legendary"},
        {"id": 381, "name": "latios",      "tier": "legendary"},
        {"id": 480, "name": "uxie",        "tier": "legendary"},
        {"id": 481, "name": "mesprit",     "tier": "legendary"},
        {"id": 482, "name": "azelf",       "tier": "legendary"},
        {"id": 638, "name": "cobalion",    "tier": "legendary"},
        {"id": 639, "name": "terrakion",   "tier": "legendary"},
        {"id": 640, "name": "virizion",    "tier": "legendary"},
        {"id": 716, "name": "xerneas",     "tier": "legendary"},
        {"id": 717, "name": "yveltal",     "tier": "legendary"},
        {"id": 772, "name": "type-null",   "tier": "legendary"},
        {"id": 785, "name": "tapu-koko",   "tier": "legendary"},
        {"id": 786, "name": "tapu-lele",   "tier": "legendary"},
        {"id": 787, "name": "tapu-bulu",   "tier": "legendary"},
        {"id": 788, "name": "tapu-fini",   "tier": "legendary"},
        # ── Greater legendaries (medium frequency) ────────────────────────────
        {"id": 150, "name": "mewtwo",      "tier": "greater"},
        {"id": 249, "name": "lugia",       "tier": "greater"},
        {"id": 250, "name": "ho-oh",       "tier": "greater"},
        {"id": 382, "name": "kyogre",      "tier": "greater"},
        {"id": 383, "name": "groudon",     "tier": "greater"},
        {"id": 384, "name": "rayquaza",    "tier": "greater"},
        {"id": 483, "name": "dialga",      "tier": "greater"},
        {"id": 484, "name": "palkia",      "tier": "greater"},
        {"id": 485, "name": "heatran",     "tier": "greater"},
        {"id": 488, "name": "cresselia",   "tier": "greater"},
        {"id": 643, "name": "reshiram",    "tier": "greater"},
        {"id": 644, "name": "zekrom",      "tier": "greater"},
        {"id": 718, "name": "zygarde",     "tier": "greater"},
        {"id": 800, "name": "necrozma",    "tier": "greater"},
        {"id": 888, "name": "zacian",      "tier": "greater"},
        {"id": 889, "name": "zamazenta",   "tier": "greater"},
        {"id": 890, "name": "eternatus",   "tier": "greater"},
        {"id": 905, "name": "enamorus-incarnate", "tier": "greater"},
        # ── Mythicals (rare) ──────────────────────────────────────────────────
        {"id": 151, "name": "mew",         "tier": "mythical"},
        {"id": 251, "name": "celebi",      "tier": "mythical"},
        {"id": 385, "name": "jirachi",     "tier": "mythical"},
        {"id": 491, "name": "darkrai",     "tier": "mythical"},
        {"id": 492, "name": "shaymin",     "tier": "mythical"},
        {"id": 494, "name": "victini",     "tier": "mythical"},
        {"id": 647, "name": "keldeo",      "tier": "mythical"},
        {"id": 720, "name": "hoopa",       "tier": "mythical"},
        {"id": 801, "name": "magearna",    "tier": "mythical"},
        {"id": 802, "name": "marshadow",   "tier": "mythical"},
        {"id": 807, "name": "zeraora",     "tier": "mythical"},
        {"id": 898, "name": "calyrex",     "tier": "mythical"},
        # ── Pseudo-legendaries (common, give variety) ─────────────────────────
        {"id": 149, "name": "dragonite",   "tier": "pseudo"},
        {"id": 248, "name": "tyranitar",   "tier": "pseudo"},
        {"id": 373, "name": "salamence",   "tier": "pseudo"},
        {"id": 376, "name": "metagross",   "tier": "pseudo"},
        {"id": 445, "name": "garchomp",    "tier": "pseudo"},
        {"id": 635, "name": "hydreigon",   "tier": "pseudo"},
        {"id": 681, "name": "aegislash",   "tier": "pseudo"},
        {"id": 784, "name": "kommo-o",     "tier": "pseudo"},
        {"id": 887, "name": "dragapult",   "tier": "pseudo"},
        {"id": 996, "name": "baxcalibur",  "tier": "pseudo"},
    ]

    _RAID_TIER_WEIGHTS = {"pseudo": 35, "legendary": 40, "greater": 18, "mythical": 7}

    def _pick_random_raid_boss(self) -> Dict:
        """Pick a tier first, then a boss, so tier odds are not distorted by pool size."""
        tiers = list(self._RAID_TIER_WEIGHTS)
        weights = [self._RAID_TIER_WEIGHTS[t] for t in tiers]
        chosen_tier = random.choices(tiers, weights=weights, k=1)[0]
        return random.choice([e for e in self.RAID_BOSS_POOL if e["tier"] == chosen_tier])

    # ── Raid helpers ──────────────────────────────────────────────────────────

    def _raid_star_tier(self, level: int) -> Tuple[int, str]:
        """Return (stars, label) based on boss level."""
        if level >= 90: return 5, "⭐⭐⭐⭐⭐"
        if level >= 75: return 4, "⭐⭐⭐⭐"
        if level >= 60: return 3, "⭐⭐⭐"
        if level >= 50: return 2, "⭐⭐"
        return 1, "⭐"

    def _raid_balls_awarded(self, stars: int, alive: bool) -> int:
        """Number of Raid Balls given on victory. Survivors get +1."""
        base = {1: 2, 2: 3, 3: 4, 4: 5, 5: 6}[stars]
        return base + (1 if alive else 0)

    @staticmethod
    def _raid_active_mon(participant: dict) -> Optional[dict]:
        """The participant's currently-fighting bench Pokémon (or None if all fainted)."""
        bench = participant.get("bench", [])
        idx   = participant.get("active_idx", 0)
        if 0 <= idx < len(bench) and bench[idx]["stats"]["hp"] > 0:
            return bench[idx]
        # Auto-advance to the next non-fainted party member
        for i, mon in enumerate(bench):
            if mon["stats"]["hp"] > 0:
                participant["active_idx"] = i
                return mon
        return None

    @staticmethod
    def _raid_party_alive(participant: dict) -> bool:
        return any(m["stats"]["hp"] > 0 for m in participant.get("bench", []))

    @staticmethod
    def _raid_party_status(participant: dict) -> Tuple[int, int]:
        """(alive_count, total_count) of a participant's bench."""
        bench = participant.get("bench", [])
        return sum(1 for m in bench if m["stats"]["hp"] > 0), len(bench)

    def _build_raid_embed(self, raid: dict, extra_lines: Optional[List[str]] = None) -> discord.Embed:
        boss              = raid["boss"]
        stars, star_label = self._raid_star_tier(boss["level"])
        hp_current        = raid["boss_hp"]
        hp_max            = raid["boss_hp_max"]
        bar               = hp_bar(hp_current, hp_max)
        participants      = raid["participants"]

        color_map = {1: COLORS["green"], 2: COLORS["blue"], 3: COLORS["purple"],
                     4: COLORS["orange"], 5: COLORS["shiny"]}
        embed = discord.Embed(
            title=f"{'✨ ' if boss.get('shiny') else ''}🔴 RAID BOSS — {boss['displayName']} {star_label}",
            color=color_map.get(stars, COLORS["red"]),
        )
        art = artwork_url(boss)
        if art:
            embed.set_thumbnail(url=art)

        embed.add_field(name="Boss HP", value=f"{bar}  **{hp_current:,} / {hp_max:,}**", inline=False)
        embed.add_field(
            name="Level / Type",
            value=f"Lv.{boss['level']} | {' / '.join(t.capitalize() for t in boss['types'])}",
            inline=True,
        )
        embed.add_field(name="Turn", value=str(raid.get("turn", 0)), inline=True)
        embed.add_field(name="Trainers", value=str(len(participants)), inline=True)

        if participants:
            p_lines = []
            for p in participants:
                active = self._raid_active_mon(p)
                alive_n, total_n = self._raid_party_status(p)
                dmg = p.get("damage_dealt", 0)
                heal_cd = " _(heal ready)_" if p.get("heal_cooldown", 0) == 0 else ""
                if active:
                    poke   = active
                    status = f"{poke['displayName']} {poke['stats']['hp']}/{poke['stats']['maxHp']} HP"
                    icon   = "✅"
                else:
                    status = "all fainted"
                    icon   = "❌"
                p_lines.append(
                    f"{icon} **{p['username']}** [{alive_n}/{total_n}] — {status} | 💥 {dmg:,} dmg{heal_cd}"
                )
            embed.add_field(name="👥 Raid Party", value="\n".join(p_lines), inline=False)

        if extra_lines:
            embed.add_field(name="📋 Turn Log", value="\n".join(extra_lines[-8:]), inline=False)

        return embed

    async def _run_raid(self, guild: discord.Guild, channel: discord.TextChannel) -> None:
        """Main raid loop — join window, HP sizing, then the auto-battle."""
        raid = self._raids.get(guild.id)
        if not raid:
            return

        # ── Join window ───────────────────────────────────────────────────────
        await asyncio.sleep(120)
        raid = self._raids.get(guild.id)
        if not raid:
            return   # cancelled

        if not raid["participants"]:
            self._raids.pop(guild.id, None)
            try:
                await channel.send(embed=discord.Embed(
                    color=COLORS["gray"],
                    description=f"🏃 Nobody joined the raid in time — **{raid['boss']['displayName']}** fled!",
                ))
            except discord.HTTPException:
                pass
            return

        boss       = raid["boss"]
        stars, _   = self._raid_star_tier(boss["level"])
        n_trainers = len(raid["participants"])

        # ── Size boss HP to the team's actual output ──────────────────────────
        # Each trainer's lead does roughly estimate_hit() per turn; summing the
        # leads gives expected team DPS. Boss HP = team DPS × target turns, so the
        # fight always lasts about RAID_TARGET_TURNS no matter the players' level.
        boss_def  = raid["boss_defense"]
        team_dps  = sum(
            estimate_hit(p["bench"][0], boss_def)
            for p in raid["participants"] if p.get("bench")
        )
        team_dps  = max(team_dps, 1)
        boss_hp_max = max(
            math.floor(team_dps * self.RAID_TARGET_TURNS[stars]),
            self.RAID_MIN_BOSS_HP,
        )
        raid["boss_hp"]     = boss_hp_max
        raid["boss_hp_max"] = boss_hp_max

        total_party = sum(len(p["bench"]) for p in raid["participants"])
        await channel.send(embed=discord.Embed(
            color=COLORS["red"],
            description=(
                f"⚔️ **The raid battle begins!** {n_trainers} trainer(s) "
                f"({total_party} Pokémon) vs **{boss['displayName']}** (Lv.{boss['level']})!\n\n"
                f"Boss HP scaled to your team: **{boss_hp_max:,}**\n\n"
                f"Each turn your active Pokémon attacks; the next steps in when one faints. "
                f"Use `raidswap <pos>` to pick who's in front, or `raidheal <item>` to heal."
            ),
        ))

        # ── Battle loop ───────────────────────────────────────────────────────
        for turn in range(1, self.RAID_MAX_TURNS + 1):
            await asyncio.sleep(self.RAID_TURN_DELAY)
            raid = self._raids.get(guild.id)
            if not raid:
                return

            turn_log: List[str] = [f"**— Turn {turn} —**"]
            raid["turn"] = turn

            # Tick heal cooldowns down each turn
            for p in raid["participants"]:
                if p.get("heal_cooldown", 0) > 0:
                    p["heal_cooldown"] -= 1

            # Trainers with at least one healthy party member
            active_trainers = [p for p in raid["participants"] if self._raid_party_alive(p)]
            if not active_trainers:
                break

            # ── Each active trainer's front Pokémon attacks ────────────────────
            for participant in active_trainers:
                poke = self._raid_active_mon(participant)
                if poke is None:
                    continue
                moves = [m for m in poke.get("moves", []) if m]
                if not moves:
                    turn_log.append(f"⚠️ {participant['username']}'s {poke['displayName']} has no usable moves!")
                    continue

                # Cache the best move per *bench slot* to avoid re-fetching each turn
                cache_key = f"best_move_{participant['active_idx']}"
                if not participant.get(cache_key):
                    best_move, best_power = moves[0], 0
                    for mv in moves:
                        try:
                            md = await fetch_move_data(self._session, mv)
                            pw = md.get("power") or 0
                            if pw > best_power:
                                best_power, best_move = pw, mv
                        except Exception:
                            pass
                    participant[cache_key] = best_move

                move_name = participant[cache_key]
                try:
                    move_data = await fetch_move_data(self._session, move_name)
                except Exception:
                    turn_log.append(f"⚠️ {poke['displayName']}'s {move_name} failed!")
                    continue

                power     = move_data.get("power") or 40
                move_type = move_data["type"]["name"]
                accuracy  = move_data.get("accuracy") or 100

                if random.random() * 100 > accuracy:
                    turn_log.append(f"💨 {poke['displayName']} used **{move_name.replace('-', ' ')}** — missed!")
                    continue

                atk      = poke["stats"].get("attack") or poke["stats"].get("special-attack") or 50
                lvl      = poke["level"]
                type_eff = calculate_type_effectiveness(move_type, boss["types"])
                stab     = 1.5 if move_type in poke["types"] else 1.0
                crit     = 1.5 if random.random() < 0.0625 else 1.0
                rand_m   = 0.85 + random.random() * 0.15
                boss_def = min(raid["boss_defense"], 80)

                damage = max(1, math.floor(
                    (((2 * lvl / 5 + 2) * power * atk / boss_def) / 50 + 2)
                    * stab * type_eff * rand_m * crit
                ))
                raid["boss_hp"] = max(0, raid["boss_hp"] - damage)
                participant["damage_dealt"] = participant.get("damage_dealt", 0) + damage

                eff_txt  = effectiveness_label(type_eff)
                crit_txt = " ⚡ Crit!" if crit > 1 else ""
                turn_log.append(
                    f"🎯 **{participant['username']}**'s {poke['displayName']} → "
                    f"**{move_name.replace('-', ' ')}**{crit_txt} {eff_txt} **{damage:,}** dmg"
                )
                if raid["boss_hp"] <= 0:
                    break

            # ── Victory check ──────────────────────────────────────────────────
            if raid["boss_hp"] <= 0:
                embed = self._build_raid_embed(raid, turn_log)
                embed.color = COLORS["yellow"]
                embed.title = f"🏆 RAID VICTORY — {boss['displayName']} defeated!"
                await channel.send(embed=embed)
                await self._resolve_raid_victory(guild, channel, raid)
                self._raids.pop(guild.id, None)
                return

            # ── Boss counterattack (capped by target's max HP) ─────────────────
            # Pick a random active trainer so damage spreads instead of focusing.
            target_p    = random.choice(active_trainers)
            target_poke = self._raid_active_mon(target_p)
            if target_poke is not None:
                frac        = self.RAID_BOSS_DMG_FRAC[stars]
                boss_damage = boss_counter_damage(target_poke["stats"]["maxHp"], frac, random.random())
                target_poke["stats"]["hp"] = max(0, target_poke["stats"]["hp"] - boss_damage)
                if target_poke["stats"]["hp"] <= 0:
                    nxt = self._raid_active_mon(target_p)  # auto-advance
                    if nxt is not None:
                        fainted_str = (
                            f" 💀 **{target_poke['displayName']} fainted!** "
                            f"{nxt['displayName']} stepped in."
                        )
                    else:
                        fainted_str = f" 💀 **{target_p['username']}'s whole party fainted!**"
                else:
                    fainted_str = ""
                turn_log.append(
                    f"🔴 **{boss['displayName']}** attacks "
                    f"**{target_p['username']}'s {target_poke['displayName']}** — "
                    f"**{boss_damage:,}** dmg!{fainted_str}"
                )

            embed = self._build_raid_embed(raid, turn_log)
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass

            # Defeat check — every trainer's entire party down
            if all(not self._raid_party_alive(p) for p in raid["participants"]):
                break

        # ── Defeat / timeout ──────────────────────────────────────────────────
        raid = self._raids.pop(guild.id, None)
        if not raid:
            return

        total_dmg = sum(p.get("damage_dealt", 0) for p in raid["participants"])
        pct       = (total_dmg / max(raid["boss_hp_max"], 1)) * 100
        timed_out = raid["boss_hp"] > 0 and any(self._raid_party_alive(p) for p in raid["participants"])
        headline  = (
            f"⏱️ **Time's up!** {boss['displayName']} held on through {self.RAID_MAX_TURNS} turns."
            if timed_out else
            f"All trainers' parties fainted! **{boss['displayName']}** escapes."
        )
        await channel.send(embed=discord.Embed(
            color=COLORS["red"],
            title=f"💀 RAID OVER — {boss['displayName']} survives!",
            description=(
                f"{headline}\n\n"
                f"**Total damage:** {total_dmg:,} / {raid['boss_hp_max']:,} HP ({pct:.1f}%)\n\n"
                f"Everyone earns consolation XP — train up your party and try again!"
            ),
        ))
        for participant in raid["participants"]:
            member = guild.get_member(participant["member_id"])
            if not member:
                continue
            try:
                async with self._trainer(member, timeout=60) as p_data:
                    if not p_data:
                        continue
                    poke = p_data["pokemon"][p_data["activePokemonIndex"]]
                    poke["xp"] = poke.get("xp", 0) + max(30, math.floor(participant.get("damage_dealt", 0) / 10))
                    self._check_level_up(poke)
            except TrainerBusy:
                log.warning("[PokéBot] skipped raid consolation XP for %s (save busy)", member.id)

    async def _resolve_raid_victory(
        self, guild: discord.Guild, channel: discord.TextChannel, raid: dict
    ) -> None:
        """Award rewards — multiple Raid Ball throws — to every participant."""
        boss      = raid["boss"]
        currency  = await bank.get_currency_name(guild)
        total_dmg = max(1, sum(p.get("damage_dealt", 0) for p in raid["participants"]))
        stars, _  = self._raid_star_tier(boss["level"])

        # Shiny reveal on victory
        shiny_chance  = {1: 1/100, 2: 1/75, 3: 1/50, 4: 1/30, 5: 1/20}[stars]
        boss_is_shiny = random.random() < shiny_chance
        if boss_is_shiny:
            boss["shiny"] = True
            boss["spriteUrl"] = boss.get("shinySpriteUrl") or boss.get("spriteUrl")

        async with self.config.guild(guild).encounter_stats() as raid_stats:
            raid_stats["raid_wins"] = raid_stats.get("raid_wins", 0) + 1

        lines = []
        max_pokemon = await self.config.guild(guild).max_pokemon()
        for participant in raid["participants"]:
            member = guild.get_member(participant["member_id"])
            if not member:
                continue
            alive   = self._raid_party_alive(participant)
            contrib = participant.get("damage_dealt", 0) / total_dmg
            xp_gain = math.floor(100 + boss["level"] * 2 + contrib * 300)
            credits = stars * 200 + math.floor(contrib * stars * 400)

            # Multiple Raid Ball throws — rolled up front, applied under the lock below.
            n_balls     = self._raid_balls_awarded(stars, alive)
            per_ball    = min(0.55 + contrib * 0.20 + (0.05 if boss_is_shiny else 0), 0.85)
            caught      = False
            throw_lines = []
            for throw_num in range(1, n_balls + 1):
                if random.random() < per_ball:
                    caught = True
                    throw_lines.append(f"  🔵 Ball {throw_num}: ✅ **Caught!**")
                    break
                throw_lines.append(f"  🔵 Ball {throw_num}: 💨 Broke free...")

            actually_added = 0
            full_box = False
            lvl_msgs: List[str] = []
            try:
                async with self._trainer(member, timeout=60) as p_data:
                    if not p_data:
                        continue
                    poke = p_data["pokemon"][p_data["activePokemonIndex"]]
                    poke["xp"] = poke.get("xp", 0) + xp_gain
                    lvl_msgs = self._check_level_up(poke)
                    if caught:
                        if len(p_data["pokemon"]) < max_pokemon:
                            boss_copy                = copy.deepcopy(boss)
                            boss_copy["uid"]         = new_uid()   # each caught copy is its own Pokémon
                            boss_copy["shiny"]       = boss_is_shiny
                            boss_copy["stats"]["hp"] = boss_copy["stats"]["maxHp"]
                            boss_copy["caughtAt"]    = time.time()
                            boss_copy["nickname"]    = None
                            p_data["pokemon"].append(boss_copy)
                            self._update_dex(p_data, boss_copy)
                            actually_added = 1
                        else:
                            full_box = True
            except TrainerBusy:
                log.warning("[PokéBot] skipped raid rewards for %s (save busy)", member.id)
                continue
            with suppress(Exception):
                await _deposit(member, credits)
            if actually_added:
                async with self.config.guild(guild).encounter_stats() as raid_stats:
                    raid_stats["raid_catches"] = raid_stats.get("raid_catches", 0) + actually_added

            if actually_added:
                caught_str = f"🎉 **Caught {boss['displayName']}{'  ✨' if boss_is_shiny else ''}!**"
            elif full_box:
                caught_str = "📦 Caught it — but your collection is full, so it was released."
            else:
                caught_str = f"😔 {boss['displayName']} wasn't caught this time."
            lvl_str = f"\n  ⬆️ {lvl_msgs[0]}" if lvl_msgs else ""
            lines.append(
                f"{'✅' if alive else '💀'} **{participant['username']}** — "
                f"+{xp_gain} XP · +{credits} {currency} · {n_balls} Raid Ball(s)\n"
                + "\n".join(throw_lines)
                + f"\n  {caught_str}{lvl_str}"
            )
            if len(lvl_msgs) > 1:
                with suppress(discord.HTTPException):
                    await channel.send(embed=discord.Embed(description="\n".join(lvl_msgs[1:]), color=COLORS["green"]))

        shiny_announce = "\n\n✨ **The boss was SHINY — boosted catch chances for everyone!**" if boss_is_shiny else ""
        embed = discord.Embed(
            title="🏆 Raid Rewards",
            description=("\n\n".join(lines) + shiny_announce)[:4000],
            color=COLORS["yellow"],
        )
        art = artwork_url(boss)
        if art:
            embed.set_thumbnail(url=art)
        with suppress(discord.HTTPException):
            await channel.send(embed=embed)

    # ── Raid commands ─────────────────────────────────────────────────────────

    @commands.command(name="raidstart", aliases=["startraid"])
    @checks.admin_or_permissions(manage_guild=True)
    async def raidstart(self, ctx: commands.Context, *, pokemon_name: str = "") -> None:
        """(Admin) Start a raid in the spawn channel. Usage: `raidstart [pokemon name]`
        Omit the name for a random legendary or rare boss."""
        if ctx.guild.id in self._raids:
            await ctx.send(embed=error_embed("A raid is already active! Use `raidstatus` to check on it."))
            return

        channel_id = await self.config.guild(ctx.guild).spawn_channel_id()
        if not channel_id:
            await ctx.send(embed=error_embed("No spawn channel set! Use `pokeset spawnchannel #channel` first."))
            return
        channel = ctx.guild.get_channel(channel_id)
        if not channel:
            await ctx.send(embed=error_embed("Spawn channel not found — reconfigure with `pokeset spawnchannel`."))
            return

        async with ctx.typing():
            if pokemon_name.strip():
                slug = pokemon_name.lower().strip().replace(" ", "-")
                try:
                    boss = await build_pokemon_instance(self._session, slug, level=random.randint(60, 100), allow_shiny=False)
                except Exception:
                    await ctx.send(embed=error_embed(f"Couldn't find **{pokemon_name}**. Check the spelling!"))
                    return
            else:
                pick  = self._pick_random_raid_boss()
                slug  = str(pick.get("name", pick["id"])).lower()
                level = random.randint(70, 100) if pick["tier"] in ("greater", "mythical") else random.randint(55, 90)
                try:
                    boss = await build_pokemon_instance(self._session, slug, level=level, allow_shiny=False)
                except Exception:
                    boss = await build_pokemon_instance(
                        self._session, get_random_pokemon_id(), level=random.randint(55, 100), allow_shiny=False
                    )

        stars, star_label = self._raid_star_tier(boss["level"])
        tier_label = {
            "pseudo": "Rare", "legendary": "Legendary",
            "greater": "Greater Legendary", "mythical": "Mythical",
        }.get(next((e["tier"] for e in self.RAID_BOSS_POOL if e.get("name") == slug), ""), "Boss")

        boss_attack  = boss["stats"].get("attack") or boss["stats"].get("special-attack") or 80
        boss_defense = boss["stats"].get("defense") or 60

        # HP here is a placeholder; it's re-sized to the team once the battle starts.
        placeholder_hp = max(boss["stats"]["maxHp"] * 4, self.RAID_MIN_BOSS_HP)

        raid = {
            "boss":         boss,
            "boss_hp":      placeholder_hp,
            "boss_hp_max":  placeholder_hp,
            "boss_attack":  boss_attack,
            "boss_defense": boss_defense,
            "participants": [],
            "turn":         0,
            "channel_id":   channel.id,
            "started_at":   time.time(),
        }
        self._raids[ctx.guild.id] = raid

        balls_preview = self._raid_balls_awarded(stars, alive=True)
        embed = discord.Embed(
            title=f"🔴 RAID BATTLE — {boss['displayName']} appeared! {star_label}",
            description=(
                f"**{tier_label}** · Lv.{boss['level']}\n"
                f"Type: {' / '.join(t.capitalize() for t in boss['types'])}\n\n"
                f"⏳ **Join window: 2 minutes** — tap **⚔️ Join Raid** (or type `raidjoin`) to enter with your party!\n\n"
                f"**On victory:** {balls_preview - 1}–{balls_preview} Raid Balls per trainer\n"
                f"_Boss HP scales to your team — bring a full party (up to 6) for the tough ones!_"
            ),
            color={1: COLORS["green"], 2: COLORS["blue"], 3: COLORS["purple"],
                   4: COLORS["orange"], 5: COLORS["shiny"]}[stars],
        )
        art = artwork_url(boss)
        if art:
            embed.set_image(url=art)
        embed.set_footer(text=f"{star_label} · Higher stars = tougher boss & better rewards!")
        join_view = RaidJoinView(self, ctx.guild.id, timeout=120)
        join_view.message = await channel.send(embed=embed, view=join_view)

        if channel != ctx.channel:
            await ctx.send(embed=success_embed(f"Raid started in {channel.mention}!"))

        task = self.bot.loop.create_task(self._run_raid(ctx.guild, channel))
        self._raid_tasks[ctx.guild.id] = task

    async def _raid_join(self, member: discord.Member) -> Tuple[bool, discord.Embed]:
        raid = self._raids.get(member.guild.id)
        if not raid:
            return False, error_embed("There's no active raid right now!")
        if raid["turn"] > 0:
            return False, error_embed("The raid has already started — you can only join during the 2-minute window!")
        player = await self._get_player(member)
        if not player:
            return False, error_embed("Start your journey with `start` before joining raids!")
        if any(p["member_id"] == member.id for p in raid["participants"]):
            return False, error_embed("You've already joined this raid!")

        # Snapshot the party (deep copies so raid damage never touches stored HP)
        roster = party_mons(player)
        if not roster:
            return False, error_embed("Your party is empty! Add Pokémon with `party add <slot>`.")
        bench = [copy.deepcopy(m) for m in roster]
        if all(m["stats"]["hp"] <= 0 for m in bench):
            return False, error_embed("Every Pokémon in your party has fainted! Use `heal` before joining.")

        start_idx = next((i for i, m in enumerate(bench) if m["stats"]["hp"] > 0), 0)
        raid["participants"].append({
            "member_id":     member.id,
            "username":      member.display_name,
            "bench":         bench,
            "active_idx":    start_idx,
            "damage_dealt":  0,
            "heal_cooldown": 0,
        })
        alive_n, total_n = self._raid_party_status(raid["participants"][-1])
        lead = bench[start_idx]
        hurt = sum(1 for m in bench if 0 < m["stats"]["hp"] < m["stats"]["maxHp"])
        tip = f"\n_Tip: {hurt + total_n - alive_n} party member(s) aren't at full HP — `heal` before the fight starts!_" \
            if (hurt or alive_n < total_n) else ""
        return True, discord.Embed(
            color=COLORS["green"],
            description=(
                f"✅ **{member.display_name}** joined with a party of **{total_n}** "
                f"({alive_n} healthy)! Leading with **{mon_name(lead)}** (Lv.{lead['level']}).\n"
                f"_{len(raid['participants'])} trainer(s) ready._{tip}"
            ),
        )

    async def raid_join_interaction(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return
        ok, embed = await self._raid_join(member)
        if ok:
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.command(name="raidjoin", aliases=["joinraid"])
    async def raidjoin(self, ctx: commands.Context) -> None:
        """Join the active raid with your whole party during the 2-minute window."""
        _, embed = await self._raid_join(ctx.author)
        await ctx.send(embed=embed)

    @commands.command(name="raidswap")
    async def raidswap(self, ctx: commands.Context, position: int) -> None:
        """Bring a specific party member to the front mid-raid. Usage: `raidswap <position>`
        Position is the slot in your party (see `raidstatus`/`party`)."""
        raid = self._raids.get(ctx.guild.id)
        if not raid or raid["turn"] == 0:
            await ctx.send(embed=error_embed("No active raid battle right now."))
            return

        participant = next((p for p in raid["participants"] if p["member_id"] == ctx.author.id), None)
        if not participant:
            await ctx.send(embed=error_embed("You're not in this raid!"))
            return

        bench = participant["bench"]
        idx   = position - 1
        if idx < 0 or idx >= len(bench):
            await ctx.send(embed=error_embed(f"Invalid position. Your party has {len(bench)} Pokémon."))
            return
        new_poke = bench[idx]
        if new_poke["stats"]["hp"] <= 0:
            await ctx.send(embed=error_embed(f"**{new_poke['displayName']}** has fainted — pick a healthy one."))
            return
        if participant["active_idx"] == idx:
            await ctx.send(embed=error_embed(f"**{new_poke['displayName']}** is already in front!"))
            return

        old_poke = bench[participant["active_idx"]] if 0 <= participant["active_idx"] < len(bench) else None
        old_name = old_poke["displayName"] if old_poke else "your Pokémon"
        participant["active_idx"] = idx
        await ctx.send(embed=discord.Embed(
            color=COLORS["blue"],
            description=(
                f"🔄 **{ctx.author.display_name}** sent out **{new_poke['displayName']}** "
                f"(Lv.{new_poke['level']}, HP {new_poke['stats']['hp']}/{new_poke['stats']['maxHp']}) "
                f"in place of **{old_name}**!"
            ),
        ))

    @commands.command(name="raidheal")
    async def raidheal(self, ctx: commands.Context, item: str = "potion") -> None:
        """Use a healing item on your active raid Pokémon. Usage: `raidheal [item]`
        Heals the Pokémon currently in front without touching your stored HP.
        Usable once every 2 turns per trainer."""
        raid = self._raids.get(ctx.guild.id)
        if not raid or raid["turn"] == 0:
            await ctx.send(embed=error_embed("No active raid battle right now."))
            return

        participant = next((p for p in raid["participants"] if p["member_id"] == ctx.author.id), None)
        if not participant:
            await ctx.send(embed=error_embed("You're not in this raid!"))
            return

        poke = self._raid_active_mon(participant)
        if poke is None:
            await ctx.send(embed=error_embed("Your whole party has fainted — nothing left to heal!"))
            return

        cooldown = participant.get("heal_cooldown", 0)
        if cooldown > 0:
            await ctx.send(embed=error_embed(f"Healing is on cooldown for **{cooldown}** more turn(s)!"))
            return

        item_slug = item.lower().replace(" ", "").replace("-", "")
        if item_slug not in HEAL_AMOUNTS:
            await ctx.send(embed=error_embed(f"Unknown item. Valid: {', '.join(HEAL_AMOUNTS.keys())}"))
            return
        if item_slug == "revive":
            await ctx.send(embed=error_embed("Revives can't be used mid-raid — swap in another party member instead."))
            return

        player = await self._get_player(ctx.author)
        if not player:
            return
        count = player["items"].get("healing", {}).get(item_slug, 0)
        if count <= 0:
            await ctx.send(embed=error_embed(f"You don't have any {ITEM_NAMES[item_slug]}! Buy some with `shop`."))
            return

        heal_amt = HEAL_AMOUNTS[item_slug]
        old_hp   = poke["stats"]["hp"]
        if math.isinf(heal_amt):
            poke["stats"]["hp"] = poke["stats"]["maxHp"]
        else:
            poke["stats"]["hp"] = min(poke["stats"]["maxHp"], poke["stats"]["hp"] + int(heal_amt))
        healed = poke["stats"]["hp"] - old_hp

        player["items"]["healing"][item_slug] -= 1
        await self._save_player(ctx.author, player)
        participant["heal_cooldown"] = 2

        bar = hp_bar(poke["stats"]["hp"], poke["stats"]["maxHp"])
        await ctx.send(embed=discord.Embed(
            color=COLORS["green"],
            description=(
                f"💊 **{ctx.author.display_name}** used {ITEM_NAMES[item_slug]} on "
                f"**{poke['displayName']}** — restored **{healed} HP**!\n"
                f"HP: {bar} {poke['stats']['hp']}/{poke['stats']['maxHp']}\n"
                f"_(Healing available again in 2 turns)_"
            ),
        ))

    @commands.command(name="raidstatus", aliases=["raidinfo"])
    async def raidstatus(self, ctx: commands.Context) -> None:
        """Check the current raid — boss HP, party status, turn count."""
        raid = self._raids.get(ctx.guild.id)
        if not raid:
            await ctx.send(embed=error_embed("No raid is active right now."))
            return
        embed = self._build_raid_embed(raid)
        if raid["turn"] == 0:
            embed.set_footer(text="⏳ Waiting for trainers — type `raidjoin` to enter!")
        await ctx.send(embed=embed)

    @commands.command(name="raidcancel")
    @checks.admin_or_permissions(manage_guild=True)
    async def raidcancel(self, ctx: commands.Context) -> None:
        """(Admin) Cancel the current raid immediately."""
        if ctx.guild.id not in self._raids:
            await ctx.send(embed=error_embed("No raid is active right now."))
            return
        self._raids.pop(ctx.guild.id, None)
        task = self._raid_tasks.pop(ctx.guild.id, None)
        if task and not task.done():
            task.cancel()
        await ctx.send(embed=success_embed("Raid cancelled."))


async def setup(bot: Red) -> None:
    await bot.add_cog(PokéBot(bot))
