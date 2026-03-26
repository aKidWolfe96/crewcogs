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
  [p]catch        – Catch a wild Pokémon
  [p]shop         – Browse PokéMart
  [p]buy          – Buy items in bulk
  [p]use          – Use a healing item
  [p]battle       – Challenge another trainer
  [p]move         – Use a move in battle
  [p]leaderboard  – Server rankings
  [p]pokespawn    – (Admin) Force a spawn
  [p]pokeset      – (Admin) Bot settings
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
import discord
from redbot.core import Config, commands, checks
from redbot.core.bot import Red

from .embeds import (
    COLORS, TYPE_EMOJIS, error_embed, hp_bar, pokemon_embed,
    success_embed, type_tag,
)
from .pokeapi import (
    MAX_POKEMON, build_pokemon_instance, calculate_type_effectiveness,
    catch_rate, effectiveness_label, fetch_move_data, fetch_pokemon,
    get_random_pokemon_id, set_cache_dir,
)

# ──────────────────────────────────────────────────────────────────────────────
# Starter list (same as original)
# ──────────────────────────────────────────────────────────────────────────────
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
    {"id": "pokeball",    "name": "Poké Ball",    "emoji": "🔴", "desc": "Standard catch ball",          "price": 50,  "category": "balls"},
    {"id": "greatball",   "name": "Great Ball",   "emoji": "🔵", "desc": "Better catch rate (1.5×)",     "price": 150, "category": "balls"},
    {"id": "ultraball",   "name": "Ultra Ball",   "emoji": "⚫", "desc": "Best catch rate (2×)",         "price": 300, "category": "balls"},
    {"id": "potion",      "name": "Potion",       "emoji": "🧪", "desc": "Heals 20 HP",                  "price": 100, "category": "healing"},
    {"id": "superpotion", "name": "Super Potion", "emoji": "💊", "desc": "Heals 50 HP",                  "price": 200, "category": "healing"},
    {"id": "maxpotion",   "name": "Max Potion",   "emoji": "💉", "desc": "Fully restores HP",            "price": 500, "category": "healing"},
    {"id": "revive",      "name": "Revive",       "emoji": "⭐", "desc": "Revives fainted Pokémon to ½ HP", "price": 400, "category": "healing"},
]

HEAL_AMOUNTS = {"potion": 20, "superpotion": 50, "maxpotion": math.inf, "revive": None}
ITEM_NAMES   = {"potion": "🧪 Potion", "superpotion": "💊 Super Potion", "maxpotion": "💉 Max Potion", "revive": "⭐ Revive"}
BALL_NAMES   = {"pokeball": "Poké Ball", "greatball": "Great Ball", "ultraball": "Ultra Ball"}


# ──────────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────────
class PokéBot(commands.Cog):
    """Full-featured Pokémon catching and battling cog."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot

        # In-memory battle/challenge state (mirrors JS Maps)
        self._battles: Dict[str, dict] = {}          # battle_id -> battle
        self._challenges: Dict[int, dict] = {}        # challenged_user_id -> challenge
        self._spawn_cache: Dict[int, dict] = {}       # channel_id -> spawn
        self._msg_counts: Dict[int, int] = {}         # channel_id -> count
        self._spawn_tasks: Dict[int, asyncio.Task] = {}

        self.config = Config.get_conf(self, identifier=0x504F4B45424F54, force_registration=True)

        default_guild = {
            "spawn_channel_id": None,
            "spawn_interval": 300,
        }
        default_member = {
            "userId": None,
            "username": "",
            "registeredAt": None,
            "credits": 0,
            "pokemon": [],
            "activePokemonIndex": 0,
            "wins": 0,
            "losses": 0,
            "items": {
                "pokeball": 0,
                "greatball": 0,
                "ultraball": 0,
                "healing": {},
            },
        }
        self.config.register_guild(**default_guild)
        self.config.register_member(**default_member)

        # Set up file cache for PokéAPI responses
        cache_path = Path(__file__).parent / "data" / "pokemon_cache"
        set_cache_dir(cache_path)

        self._session: Optional[aiohttp.ClientSession] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        for task in self._spawn_tasks.values():
            task.cancel()
        if self._session:
            await self._session.close()

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _get_player(self, guild: discord.Guild, user: discord.Member) -> Optional[dict]:
        data = await self.config.member(user).all()
        if data["registeredAt"] is None:
            return None
        return data

    async def _save_player(self, user: discord.Member, data: dict) -> None:
        await self.config.member(user).set(data)

    async def _create_player(
        self, user: discord.Member, starter: dict
    ) -> dict:
        player = {
            "userId": user.id,
            "username": user.display_name,
            "registeredAt": time.time(),
            "credits": 500,
            "pokemon": [starter],
            "activePokemonIndex": 0,
            "wins": 0,
            "losses": 0,
            "items": {
                "pokeball": 10,
                "greatball": 3,
                "ultraball": 1,
                "healing": {},
            },
        }
        await self._save_player(user, player)
        return player

    def _get_battle_by_user(self, user_id: int) -> Optional[Tuple[str, dict]]:
        for bid, battle in self._battles.items():
            if battle["player1"]["id"] == user_id or battle["player2"]["id"] == user_id:
                return bid, battle
        return None

    def _check_level_up(self, pokemon: dict) -> List[str]:
        messages = []
        while pokemon["xp"] >= pokemon["xpToNext"]:
            pokemon["xp"] -= pokemon["xpToNext"]
            pokemon["level"] += 1
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

    def _build_battle_embed(self, battle: dict, log_lines: List[str] = []) -> discord.Embed:
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

        power    = move_data.get("power") or 0
        move_type = move_data["type"]["name"]
        accuracy = move_data.get("accuracy") or 100

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
            first  = (p1, p2, p1["moveUsed"])
            second = (p2, p1, p2["moveUsed"])
        else:
            first  = (p2, p1, p2["moveUsed"])
            second = (p1, p2, p1["moveUsed"])

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
        p1["moveUsed"] = None
        p2["moveUsed"] = None

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
            w = await self._get_player(guild, winner_member)
            if w:
                w["wins"] = w.get("wins", 0) + 1
                w["credits"] = w.get("credits", 0) + 100
                wp = w["pokemon"][w["activePokemonIndex"]]
                wp["xp"] = wp.get("xp", 0) + 50 * (battle["turn"] if battle else 1)
                self._check_level_up(wp)
                await self._save_player(winner_member, w)

        if loser_member:
            l = await self._get_player(guild, loser_member)
            if l:
                l["losses"] = l.get("losses", 0) + 1
                await self._save_player(loser_member, l)

    # ── Spawn System ──────────────────────────────────────────────────────────

    async def _spawn_wild(self, channel: discord.TextChannel) -> None:
        if channel.id in self._spawn_cache:
            return
        pokemon_id = get_random_pokemon_id()
        try:
            pokemon = await build_pokemon_instance(self._session, pokemon_id)
        except Exception as e:
            return

        self._spawn_cache[channel.id] = {"pokemon": pokemon, "channelId": channel.id, "spawnedAt": time.time()}

        shiny_text = "\n✨ **A SHINY Pokémon appeared!** ✨" if pokemon["shiny"] else ""
        embed = discord.Embed(
            title=f"A wild {pokemon['displayName']} appeared!{'  ✨' if pokemon['shiny'] else ''}",
            description=(
                f"**Level {pokemon['level']}** | Type: {' / '.join(t.capitalize() for t in pokemon['types'])}"
                + shiny_text
            ),
            color=COLORS["shiny"] if pokemon["shiny"] else COLORS["green"],
        )
        if pokemon.get("spriteUrl"):
            embed.set_image(url=pokemon["spriteUrl"])
        embed.set_footer(text="Use `catch <ball>` to try to catch it!")
        await channel.send(embed=embed)

    async def _spawn_loop(self, guild: discord.Guild) -> None:
        await self.bot.wait_until_ready()
        while True:
            try:
                channel_id = await self.config.guild(guild).spawn_channel_id()
                interval   = await self.config.guild(guild).spawn_interval()
                if channel_id:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        await self._spawn_wild(channel)
            except Exception:
                pass
            jitter = random.randint(-60, 60)
            await asyncio.sleep(max(60, (interval or 300) + jitter))

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        self._ensure_spawn_task(guild)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            self._ensure_spawn_task(guild)

    def _ensure_spawn_task(self, guild: discord.Guild) -> None:
        if guild.id not in self._spawn_tasks or self._spawn_tasks[guild.id].done():
            self._spawn_tasks[guild.id] = self.bot.loop.create_task(self._spawn_loop(guild))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
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

    @commands.command(name="pokespawn")
    @checks.admin_or_permissions(manage_guild=True)
    async def pokespawn(self, ctx: commands.Context) -> None:
        """(Admin) Force spawn a wild Pokémon in this channel."""
        await ctx.typing()
        self._spawn_cache.pop(ctx.channel.id, None)  # allow re-spawn
        await self._spawn_wild(ctx.channel)

    # ── Start ─────────────────────────────────────────────────────────────────

    @commands.command(name="start")
    async def start(self, ctx: commands.Context) -> None:
        """Begin your Pokémon journey and choose a starter!"""
        player = await self._get_player(ctx.guild, ctx.author)
        if player:
            await ctx.send(embed=error_embed("You already started your journey! Use `pokemon` to see your team."))
            return

        lines = []
        for gen in range(1, 10):
            trio = STARTERS[(gen - 1) * 3: gen * 3]
            lines.append(f"**Gen {gen}:** {', '.join(s['name'] for s in trio)}")

        embed = discord.Embed(
            title="🌟 Welcome to your Pokémon journey!",
            description=(
                "Choose your starter by typing its name below!\n\n"
                + "\n".join(lines)
            ),
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

        embed = discord.Embed(
            title=f"🎉 You chose {pokemon['displayName']}!",
            description=(
                f"Welcome, **{ctx.author.display_name}**! Your journey begins!\n\n"
                f"You received:\n"
                f"• **{pokemon['displayName']}** (Lv.5)\n"
                f"• **10 Poké Balls**, 3 Great Balls, 1 Ultra Ball\n"
                f"• **500 credits**\n\n"
                f"Use `help` to see all commands. Good luck!"
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
        player = await self._get_player(ctx.guild, target)
        if not player:
            msg = "You haven't started yet! Use `start`." if target == ctx.author else f"{target.display_name} hasn't started their journey yet."
            await ctx.send(embed=error_embed(msg))
            return

        active = player["pokemon"][player["activePokemonIndex"]] if player["pokemon"] else None
        shinies = sum(1 for p in player["pokemon"] if p.get("shiny"))
        total   = player["wins"] + player["losses"]
        win_rate = f"{(player['wins'] / total * 100):.1f}" if total else "0.0"

        embed = discord.Embed(
            title=f"🎒 {target.display_name}'s Trainer Profile",
            color=COLORS["purple"],
        )
        if active and active.get("spriteUrl"):
            embed.set_thumbnail(url=active["spriteUrl"])

        embed.add_field(name="💰 Credits", value=str(player.get("credits", 0)), inline=True)
        embed.add_field(name="📦 Pokémon", value=str(len(player["pokemon"])), inline=True)
        embed.add_field(name="✨ Shinies", value=str(shinies), inline=True)
        embed.add_field(name="⚔️ Battles", value=f"{player['wins']}W / {player['losses']}L ({win_rate}%)", inline=True)
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
        reg = datetime.fromtimestamp(player["registeredAt"]).strftime("%Y-%m-%d")
        embed.set_footer(text=f"Trainer since {reg}")
        await ctx.send(embed=embed)

    # ── Pokemon list ──────────────────────────────────────────────────────────

    @commands.command(name="pokemon")
    async def pokemon_list(self, ctx: commands.Context, page: int = 1, user: Optional[discord.Member] = None) -> None:
        """View your Pokémon collection. Usage: `pokemon [page] [@user]`"""
        target = user or ctx.author
        player = await self._get_player(ctx.guild, target)
        if not player:
            msg = "You haven't started your journey yet! Use `start`." if target == ctx.author else f"{target.display_name} hasn't started their journey yet."
            await ctx.send(embed=error_embed(msg))
            return

        per_page = 6
        total = len(player["pokemon"])
        pages = max(1, math.ceil(total / per_page))
        page  = max(1, min(page, pages))
        offset = (page - 1) * per_page
        chunk  = player["pokemon"][offset: offset + per_page]

        embed = discord.Embed(
            title=f"{target.display_name}'s Pokémon ({total} total)",
            color=COLORS["blue"],
        )
        embed.set_footer(text=f"Page {page}/{pages} · Active: #{player['activePokemonIndex'] + 1} | Use `pokemon <page>` to navigate")

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
        player = await self._get_player(ctx.guild, ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey with `start`!"))
            return
        if self._get_battle_by_user(ctx.author.id):
            await ctx.send(embed=error_embed("You can't switch Pokémon during a battle!"))
            return

        idx = slot - 1
        if idx < 0 or idx >= len(player["pokemon"]):
            await ctx.send(embed=error_embed(f"Invalid slot. You have {len(player['pokemon'])} Pokémon (slots 1–{len(player['pokemon'])})."))
            return

        player["activePokemonIndex"] = idx
        await self._save_player(ctx.author, player)
        poke = player["pokemon"][idx]
        embed = pokemon_embed(poke, f"✅ Switched to {poke['displayName']}!", show_xp=True)
        await ctx.send(embed=embed)

    # ── Nickname ──────────────────────────────────────────────────────────────

    @commands.command(name="nickname")
    async def nickname(self, ctx: commands.Context, slot: int, *, name: str) -> None:
        """Give a Pokémon a nickname. Usage: `nickname <slot> <name>`"""
        player = await self._get_player(ctx.guild, ctx.author)
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
        await ctx.send(embed=success_embed(f"{player['pokemon'][idx]['displayName']} is now nicknamed **{name}**!"))

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

        embed = discord.Embed(
            title=f"#{raw['id']} — {raw['name'].capitalize()}",
            color=COLORS["blue"],
        )
        if raw["sprites"]["front_default"]:
            embed.set_thumbnail(url=raw["sprites"]["front_default"])
        official = raw["sprites"].get("other", {}).get("official-artwork", {})
        if official and official.get("front_default"):
            embed.set_image(url=official["front_default"])

        embed.add_field(name="Type", value=types_str, inline=True)
        embed.add_field(name="Height / Weight", value=f"{raw['height']/10}m / {raw['weight']/10}kg", inline=True)
        embed.add_field(name="Abilities", value=abilities, inline=False)
        embed.add_field(name="Base Stats", value="\n".join(stats_lines), inline=False)
        embed.set_footer(text="Shiny sprite available in-game ✨")
        await ctx.send(embed=embed)

    # ── Catch ─────────────────────────────────────────────────────────────────

    @commands.command(name="catch")
    async def catch(self, ctx: commands.Context, ball: str = "pokeball") -> None:
        """Catch a wild Pokémon! Usage: `catch [pokeball|greatball|ultraball]`"""
        player = await self._get_player(ctx.guild, ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return

        spawn = self._spawn_cache.get(ctx.channel.id)
        if not spawn:
            await ctx.send(embed=error_embed("There's no wild Pokémon here right now!"))
            return

        ball = ball.lower().replace(" ", "").replace("-", "")
        if ball not in BALL_NAMES:
            await ctx.send(embed=error_embed(f"Unknown ball type. Use: `pokeball`, `greatball`, or `ultraball`."))
            return

        ball_count = player["items"].get(ball, 0)
        if ball_count <= 0:
            await ctx.send(embed=error_embed(f"You don't have any {BALL_NAMES[ball]}s! Buy some with `shop`."))
            return

        player["items"][ball] -= 1
        pokemon = spawn["pokemon"]
        chance  = catch_rate(pokemon, ball)
        caught  = random.random() < chance

        shakes = 3 if caught else random.randint(0, 2)
        shake_text = "🔴 *shake*... " * shakes

        async with ctx.typing():
            await asyncio.sleep(1.5)

        if caught:
            player["pokemon"].append({**pokemon, "caughtAt": time.time()})
            self._spawn_cache.pop(ctx.channel.id, None)

            credits_earned = 500 if pokemon.get("shiny") else (100 if pokemon["level"] >= 30 else 50)
            player["credits"] = player.get("credits", 0) + credits_earned
            await self._save_player(ctx.author, player)

            bonus_tag = " ✨ Shiny bonus!" if pokemon.get("shiny") else (" 💪 High level bonus!" if pokemon["level"] >= 30 else "")
            embed = pokemon_embed(
                pokemon,
                f"{ctx.author.display_name} caught {pokemon['displayName']}{'  ✨' if pokemon.get('shiny') else ''}!",
                footer=f"{shake_text}Gotcha! Added to your collection.",
            )
            embed.color = COLORS["shiny"] if pokemon.get("shiny") else COLORS["green"]
            embed.add_field(
                name="💰 Credits Earned",
                value=f"+{credits_earned}{bonus_tag} (Total: {player['credits']})",
                inline=True,
            )
        else:
            await self._save_player(ctx.author, player)
            embed = discord.Embed(
                title=f"Oh no! {pokemon['displayName']} broke free!",
                description=f"{shake_text}💨 {pokemon['displayName']} escaped!\n\n_{BALL_NAMES[ball]}s remaining: {player['items'][ball]}_",
                color=COLORS["red"],
            )
            if pokemon.get("spriteUrl"):
                embed.set_thumbnail(url=pokemon["spriteUrl"])

        await ctx.send(embed=embed)

    # ── Shop ──────────────────────────────────────────────────────────────────

    @commands.command(name="shop")
    async def shop(self, ctx: commands.Context) -> None:
        """Browse the PokéMart."""
        player = await self._get_player(ctx.guild, ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return

        balls_str = "\n\n".join(
            f"{i['emoji']} **{i['name']}** — {i['price']} credits\n_{i['desc']}_"
            for i in SHOP_ITEMS if i["category"] == "balls"
        )
        heal_str = "\n\n".join(
            f"{i['emoji']} **{i['name']}** — {i['price']} credits\n_{i['desc']}_"
            for i in SHOP_ITEMS if i["category"] == "healing"
        )
        embed = discord.Embed(
            title="🛒 PokéMart",
            description=f"Your credits: **💰 {player.get('credits', 0)}**\n\nUse `buy <item> [amount]` to purchase.",
            color=COLORS["yellow"],
        )
        embed.add_field(name="🎯 Poké Balls", value=balls_str, inline=False)
        embed.add_field(name="💊 Healing Items", value=heal_str, inline=False)
        embed.set_footer(text="Win battles and catch Pokémon to earn more credits!")
        await ctx.send(embed=embed)

    # ── Buy ───────────────────────────────────────────────────────────────────

    @commands.command(name="buy")
    async def buy(self, ctx: commands.Context, item: str, amount: int = 1) -> None:
        """Buy items from the PokéMart. Usage: `buy <item> [amount]`"""
        player = await self._get_player(ctx.guild, ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return

        item = item.lower().replace(" ", "").replace("-", "")
        shop_item = next((i for i in SHOP_ITEMS if i["id"] == item), None)
        if not shop_item:
            names = ", ".join(f"`{i['id']}`" for i in SHOP_ITEMS)
            await ctx.send(embed=error_embed(f"Unknown item. Available: {names}"))
            return

        amount = max(1, min(amount, 99))
        total_cost = shop_item["price"] * amount
        if player.get("credits", 0) < total_cost:
            await ctx.send(embed=error_embed(
                f"You need **{total_cost} credits** for {amount}× {shop_item['name']} "
                f"but only have **{player.get('credits', 0)}**."
            ))
            return

        player["credits"] -= total_cost
        if shop_item["category"] == "balls":
            player["items"][item] = player["items"].get(item, 0) + amount
        else:
            if "healing" not in player["items"]:
                player["items"]["healing"] = {}
            player["items"]["healing"][item] = player["items"]["healing"].get(item, 0) + amount

        await self._save_player(ctx.author, player)
        await ctx.send(embed=success_embed(
            f"Bought **{amount}× {shop_item['emoji']} {shop_item['name']}** for **{total_cost} credits**!\n"
            f"Remaining credits: **{player['credits']}**"
        ))

    # ── Use ───────────────────────────────────────────────────────────────────

    @commands.command(name="use")
    async def use(self, ctx: commands.Context, item: str, slot: int = 0) -> None:
        """Use a healing item. Usage: `use <item> [slot]`"""
        player = await self._get_player(ctx.guild, ctx.author)
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
                await ctx.send(embed=error_embed(f"{poke['displayName']} hasn't fainted — Revive only works on fainted Pokémon!"))
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
            poke["stats"]["hp"] = poke["stats"]["maxHp"] if math.isinf(heal) else min(poke["stats"]["maxHp"], poke["stats"]["hp"] + heal)

        player["items"]["healing"][item] -= 1
        await self._save_player(ctx.author, player)

        bar = hp_bar(poke["stats"]["hp"], poke["stats"]["maxHp"])
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
        player = await self._get_player(ctx.guild, ctx.author)
        if not player:
            await ctx.send(embed=error_embed("Start your journey first with `start`!"))
            return
        if opponent == ctx.author:
            await ctx.send(embed=error_embed("You can't battle yourself!"))
            return
        if opponent.bot:
            await ctx.send(embed=error_embed("You can't battle a bot!"))
            return

        opp_data = await self._get_player(ctx.guild, opponent)
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
            "challengerId": ctx.author.id,
            "challengerName": ctx.author.display_name,
            "channelId": ctx.channel.id,
            "expires": time.time() + 60,
        }

        challenger_poke = player["pokemon"][player["activePokemonIndex"]]
        opp_poke        = opp_data["pokemon"][opp_data["activePokemonIndex"]]

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
            embed = discord.Embed(color=COLORS["gray"], description=f"❌ {opponent.display_name} declined the battle challenge.")
            await ctx.send(embed=embed)
            return

        # Start battle
        import copy
        p1_pokemon = copy.deepcopy(player["pokemon"][player["activePokemonIndex"]])
        p2_pokemon = copy.deepcopy(opp_data["pokemon"][opp_data["activePokemonIndex"]])

        battle_id = f"{ctx.author.id}_{opponent.id}_{int(time.time())}"
        battle = {
            "player1": {"id": ctx.author.id, "username": ctx.author.display_name, "pokemon": p1_pokemon, "moveUsed": None},
            "player2": {"id": opponent.id, "username": opponent.display_name, "pokemon": p2_pokemon, "moveUsed": None},
            "turn": 1,
            "log": [],
            "status": "active",
            "startedAt": time.time(),
            "guildId": ctx.guild.id,
            "channelId": ctx.channel.id,
        }
        self._battles[battle_id] = battle

        embed = self._build_battle_embed(battle, ["The battle begins! Both trainers, use `move <move_name>` to fight!"])
        moves1 = " · ".join(m.replace("-", " ").capitalize() for m in p1_pokemon["moves"])
        moves2 = " · ".join(m.replace("-", " ").capitalize() for m in p2_pokemon["moves"])
        await ctx.send(
            content=(
                f"{ctx.author.mention}'s moves: {moves1}\n"
                f"{opponent.mention}'s moves: {moves2}"
            ),
            embed=embed,
        )

    # ── Move ──────────────────────────────────────────────────────────────────

    @commands.command(name="move")
    async def move(self, ctx: commands.Context, *, move_name: str) -> None:
        """Use a move in your current battle. Usage: `move <move_name>`"""
        player = await self._get_player(ctx.guild, ctx.author)
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

        my_side["moveUsed"] = move_name
        await ctx.send(embed=discord.Embed(
            color=COLORS["purple"],
            description=f"⚔️ **{my_side['pokemon']['displayName']}** is ready to use **{move_name.replace('-', ' ')}**! Waiting for opponent...",
        ))

        if battle["player1"]["moveUsed"] and battle["player2"]["moveUsed"]:
            turn_result = await self._process_turn(battle_id)
            if not turn_result:
                return

            updated_battle, turn_log, winner = turn_result
            embed = self._build_battle_embed(updated_battle, turn_log)

            if winner:
                loser_id = battle["player2"]["id"] if winner["id"] == battle["player1"]["id"] else battle["player1"]["id"]
                await self._end_battle(ctx.guild, battle_id, winner["id"], loser_id)
                winner_member = ctx.guild.get_member(winner["id"])
                embed.color = COLORS["yellow"]
                embed.title = f"🏆 {winner['username']} wins the battle!"
                embed.set_footer(text=f"{winner['username']} earned 100 credits and battle XP!")

            await ctx.send(embed=embed)

    # ── Leaderboard ───────────────────────────────────────────────────────────

    @commands.command(name="leaderboard", aliases=["lb"])
    async def leaderboard(self, ctx: commands.Context, category: str = "wins") -> None:
        """View the server leaderboard. Categories: wins, caught, shinies, credits"""
        category = category.lower()
        valid = {"wins", "caught", "shinies", "credits"}
        if category not in valid:
            await ctx.send(embed=error_embed(f"Valid categories: {', '.join(valid)}"))
            return

        all_members = ctx.guild.members
        entries = []
        for member in all_members:
            if member.bot:
                continue
            p = await self._get_player(ctx.guild, member)
            if p:
                entries.append((member, p))

        if category == "wins":
            entries.sort(key=lambda x: x[1].get("wins", 0), reverse=True)
            title = "🏆 Battle Leaderboard"
            get_val = lambda p: f"{p.get('wins', 0)} wins"
        elif category == "caught":
            entries.sort(key=lambda x: len(x[1]["pokemon"]), reverse=True)
            title = "📦 Most Pokémon Caught"
            get_val = lambda p: f"{len(p['pokemon'])} Pokémon"
        elif category == "shinies":
            entries.sort(key=lambda x: sum(1 for pk in x[1]["pokemon"] if pk.get("shiny")), reverse=True)
            title = "✨ Shiny Hunters"
            get_val = lambda p: f"{sum(1 for pk in p['pokemon'] if pk.get('shiny'))} shinies"
        else:
            entries.sort(key=lambda x: x[1].get("credits", 0), reverse=True)
            title = "💰 Richest Trainers"
            get_val = lambda p: f"{p.get('credits', 0)} credits"

        medals = ["🥇", "🥈", "🥉"]
        top10  = entries[:10]
        lines  = [
            f"{medals[i] if i < 3 else f'**{i+1}.**'} **{m.display_name}** — {get_val(p)}"
            for i, (m, p) in enumerate(top10)
        ] or ["_No trainers yet! Be the first with `start`._"]

        embed = discord.Embed(title=title, description="\n".join(lines), color=COLORS["yellow"])
        embed.timestamp = datetime.utcnow()

        caller_rank = next((i for i, (m, _) in enumerate(entries) if m == ctx.author), -1)
        if caller_rank >= 10:
            _, caller_p = entries[caller_rank]
            embed.set_footer(text=f"Your rank: #{caller_rank + 1} — {get_val(caller_p)}")

        await ctx.send(embed=embed)

    # ── Help ──────────────────────────────────────────────────────────────────

    @commands.command(name="pokehelp")
    async def pokehelp(self, ctx: commands.Context) -> None:
        """Show all PokéBot commands."""
        prefix = ctx.clean_prefix
        embed = discord.Embed(
            title="📖 PokéBot — Command Reference",
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
                f"`{prefix}dex <name or #>` — Look up any Pokémon",
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
            ]),
            inline=False,
        )
        embed.add_field(
            name="🏆 Leaderboard",
            value=f"`{prefix}leaderboard [wins|caught|shinies|credits]` — Server rankings",
            inline=False,
        )
        embed.add_field(
            name="⚙️ Admin",
            value="\n".join([
                f"`{prefix}pokeset spawnchannel #channel` — Set spawn channel",
                f"`{prefix}pokeset spawninterval <seconds>` — Set spawn timer",
                f"`{prefix}pokespawn` — Force a spawn now",
            ]),
            inline=False,
        )
        embed.add_field(
            name="💡 Tips",
            value="\n".join([
                "• Wild Pokémon spawn automatically on a timer or every ~15 messages",
                "• Shiny Pokémon are **1/512** — extremely rare!",
                "• Winning battles earns XP and 100 credits",
                "• Higher-level balls have better catch rates",
                "• All 1,025 Pokémon (Gen 1–9) can appear",
            ]),
            inline=False,
        )
        embed.set_footer(text="Good luck on your journey, Trainer!")
        await ctx.send(embed=embed)


async def setup(bot: Red) -> None:
    await bot.add_cog(PokéBot(bot))
