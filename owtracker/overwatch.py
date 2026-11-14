"""Overwatch stats, rank-up alerts, party tools and a spray-challenge board for Red.

Stats come from the unofficial OverFast API, which reads Blizzard's public career
profiles. Spray/challenge progress isn't exposed by any API, so that half is a
board members tick off themselves (now with buttons).
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import random
import unicodedata
from typing import Optional

import discord
from discord.ext import tasks
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, pagify

from . import api as ofapi
from . import render
from .api import OverFast, OverFastError
from .cute_sprays import CUTE_SPRAYS
from .views import ModeView, RerollView, SprayBoardView, png_file

__version__ = "2.0.0"
log = logging.getLogger("red.owtracker")

MAX_EMBED_FIELDS = 25
REWARD_MAXLEN = 100
CONDITION_MAXLEN = 300
RANK_CHECK_HOURS = 3
LB_LIMIT = 30

ROLE_EMOJI = {"tank": "🛡️", "damage": "⚔️", "support": "✚", "open": "🔀"}
SPRAY = "🎨"
MODE_LABELS = {"all": "All modes", "quickplay": "Quick Play", "competitive": "Competitive"}
MODE_ALIASES = {"qp": "quickplay", "quick": "quickplay", "quickplay": "quickplay", "unranked": "quickplay",
                "comp": "competitive", "ranked": "competitive", "competitive": "competitive", "all": "all"}
ROLE_ALIASES = {"tank": "tank", "tanks": "tank", "dps": "damage", "damage": "damage", "dmg": "damage",
                "support": "support", "supp": "support", "heal": "support", "healer": "support", "healers": "support"}

BOARD_ROLE_ORDER = ("Tank", "Damage", "Support", "Other")
BOARD_ROLE_META = {"Tank": ("🛡️", 0x3B82F6), "Damage": ("⚔️", 0xEF4444), "Support": ("✚", 0x22C55E), "Other": (SPRAY, 0x9CA3AF)}
VALID_BOARD_ROLES = {"tank": "Tank", "damage": "Damage", "support": "Support"}

HERO_ALIASES = {
    "76": "soldier-76", "soldier": "soldier-76", "mccree": "cassidy", "cass": "cassidy",
    "ball": "wrecking-ball", "hammond": "wrecking-ball", "hamster": "wrecking-ball",
    "jq": "junker-queen", "queen": "junker-queen", "rein": "reinhardt", "hog": "roadhog",
    "torb": "torbjorn", "zen": "zenyatta", "bap": "baptiste", "brig": "brigitte", "widow": "widowmaker",
    "sym": "symmetra", "rama": "ramattra", "lw": "lifeweaver", "kiri": "kiriko", "doom": "doomfist",
    "dva": "dva", "lucio": "lucio", "sombra": "sombra", "cat": "jetpack-cat",
}
LB_STATS = {
    "winrate": ("Win rate", lambda g: g.get("winrate") or 0, lambda g: f"{g.get('winrate') or 0:.1f}%"),
    "kda": ("KDA", lambda g: g.get("kda") or 0, lambda g: f"{g.get('kda') or 0:.2f} KDA"),
    "hours": ("Time played", lambda g: g.get("time_played") or 0, lambda g: render.hours(g.get("time_played"))),
    "games": ("Games", lambda g: g.get("games_played") or 0, lambda g: f"{g.get('games_played') or 0:,} games"),
    "damage": ("Damage / game", lambda g: (g.get("average") or {}).get("damage") or 0,
               lambda g: f"{(g.get('average') or {}).get('damage') or 0:,.0f} dmg"),
    "healing": ("Healing / game", lambda g: (g.get("average") or {}).get("healing") or 0,
                lambda g: f"{(g.get('average') or {}).get('healing') or 0:,.0f} heals"),
    "elims": ("Elims / game", lambda g: (g.get("average") or {}).get("eliminations") or 0,
              lambda g: f"{(g.get('average') or {}).get('eliminations') or 0:.1f} elims"),
}
LB_ALIASES = {"wr": "winrate", "win": "winrate", "time": "hours", "playtime": "hours", "matches": "games",
              "dmg": "damage", "heals": "healing", "heal": "healing", "kills": "elims", "elim": "elims"}
MEDALS = ("🥇", "🥈", "🥉")


class LookupError_(Exception):
    pass


class LinkPicker(discord.ui.View):
    """Dropdown to pick yourself from a name search (no BattleTag number needed)."""

    def __init__(self, cog: "Overwatch", ctx: commands.Context, results: list[dict]):
        super().__init__(timeout=300)
        self.cog, self.ctx, self.results = cog, ctx, {r["player_id"]: r for r in results}
        self.message = None
        select = discord.ui.Select(
            placeholder="Pick your profile",
            options=[
                discord.SelectOption(label=r["name"][:100], value=r["player_id"][:100],
                                     description=(r.get("title") or "No title")[:100])
                for r in results
            ],
        )
        select.callback = self._pick
        self.add_item(select)

    async def _pick(self, interaction: discord.Interaction):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("This picker is for someone else.", ephemeral=True)
        r = self.results[interaction.data["values"][0]]
        await interaction.response.edit_message(content=f"Linking **{r['name']}**…", view=None)
        await self.cog._save_link(self.ctx, r["name"], r["player_id"], {"competitive": True}, user=interaction.user,
                                  send=lambda text: interaction.edit_original_response(content=text))
        self.stop()


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(ch for ch in text.lower() if ch.isalnum())


class Overwatch(commands.Cog):
    """Overwatch stat cards, rank-up alerts, party tools and the spray-challenge board."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=2026061501, force_registration=True)
        # Spray progress (`done`) stays per server: the board itself is per server.
        # Links moved to per-user in 2.0 so they work everywhere; old ones migrate on use.
        self.config.register_member(battletag=None, player_id=None, done=[])
        self.config.register_user(battletag=None, player_id=None, ranks={})
        self.config.register_guild(
            challenges={}, next_id=1,
            announce_channel=None, announce_demotions=False, spray_shoutouts=True, region="americas",
        )
        self.api = OverFast(f"RedDiscordBot-Overwatch-Cog/{__version__}")
        self._board_view: SprayBoardView | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def cog_load(self):
        self._board_view = SprayBoardView(self)
        self.bot.add_view(self._board_view)
        self.rank_watch.start()

    async def cog_unload(self):
        self.rank_watch.cancel()
        if self._board_view:
            self._board_view.stop()
        await self.api.close()

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        await self.config.user_from_id(user_id).clear()
        for guild_id, members in (await self.config.all_members()).items():
            if user_id in members:
                await self.config.member_from_ids(guild_id, user_id).clear()

    def format_help_for_context(self, ctx):
        return f"{super().format_help_for_context(ctx)}\n\nCog Version: {__version__}"

    # ------------------------------------------------------------------ #
    # Small helpers (kept public: the views use them)
    # ------------------------------------------------------------------ #
    @staticmethod
    def short_condition(text: str) -> str:
        """Drop a trailing '(Achievement Name)' so lists scan cleanly."""
        if text.endswith(")") and " (" in text:
            return text[: text.rfind(" (")].rstrip()
        return text

    @staticmethod
    def hero_from_reward(reward: str) -> str:
        """'Cute D.Va Spray' -> 'D.Va'; anything else passes through."""
        if reward.startswith("Cute ") and reward.endswith(" Spray"):
            return reward[5:-6].strip() or reward
        return reward

    @staticmethod
    def _bar(done: int, total: int, segments: int = 10) -> str:
        if total <= 0:
            return ""
        filled = round(segments * done / total)
        return "▰" * filled + "▱" * (segments - filled)

    @staticmethod
    def _done_on_board(done: list, challenges: dict) -> list:
        return [cid for cid in done if cid in challenges]

    def api_error(self, exc: Exception) -> str:
        if isinstance(exc, OverFastError) and exc.status == 429:
            return "⏳ The stats service is rate-limiting us. Try again in a minute."
        return "⚠️ The Overwatch stats service is having trouble right now. Try again shortly."

    PRIVATE_HELP = (
        "In Overwatch: **Options → Social → Career Profile Visibility → Public**. "
        "It can take a little while to show up after you change it."
    )

    # ------------------------------------------------------------------ #
    # Links
    # ------------------------------------------------------------------ #
    async def get_link(self, user, guild: Optional[discord.Guild] = None) -> dict:
        data = await self.config.user(user).all()
        if data["player_id"] or guild is None:
            return data
        legacy = await self.config.member_from_ids(guild.id, user.id).all()
        if legacy.get("player_id"):
            async with self.config.user(user).all() as conf:
                conf.update(battletag=legacy["battletag"], player_id=legacy["player_id"])
            # keep `done` (spray progress); only the link moves
            async with self.config.member_from_ids(guild.id, user.id).all() as m:
                m.update(battletag=None, player_id=None)
            data = await self.config.user(user).all()
        return data

    async def guild_links(self, guild: discord.Guild) -> list[tuple[discord.Member, dict]]:
        found = {}
        for uid, data in (await self.config.all_members(guild)).items():
            if data.get("player_id"):
                found[uid] = data
        for uid, data in (await self.config.all_users()).items():
            if data.get("player_id"):
                found[uid] = data
        return [(m, d) for uid, d in found.items() if (m := guild.get_member(uid))]

    async def _target(self, ctx: commands.Context, arg: Optional[str]) -> tuple[str, str]:
        """Command argument -> (player_id, display label). Raises LookupError_."""
        if arg is None:
            data = await self.get_link(ctx.author, ctx.guild)
            if not data["player_id"]:
                raise LookupError_(f"Link your BattleTag first: `{ctx.clean_prefix}ow link Name#1234`.")
            return data["player_id"], data["battletag"]
        if "#" in arg or "-" in arg and arg.rsplit("-", 1)[-1].isdigit():
            return ofapi.to_player_id(arg), arg
        if ctx.guild:
            try:
                member = await commands.MemberConverter().convert(ctx, arg)
            except commands.BadArgument:
                member = None
            if member is not None:
                data = await self.get_link(member, ctx.guild)
                if not data["player_id"]:
                    raise LookupError_(f"**{member.display_name}** hasn't linked a BattleTag yet.")
                return data["player_id"], data["battletag"]
        raise LookupError_("Give me a BattleTag like `Name#1234`, or @mention someone who has linked.")

    @commands.group(name="ow")
    async def ow(self, ctx: commands.Context):
        """Overwatch stats, party tools and the server's spray-challenge board."""

    @ow.command(name="link")
    async def ow_link(self, ctx: commands.Context, *, battletag: str):
        """Link your BattleTag, e.g. `[p]ow link Aaron#1234`.

        Your career profile must be **Public** in-game or stats come back empty.
        Don't know your number? `[p]ow link Aaron` searches for you.
        """
        battletag = battletag.strip()
        async with ctx.typing():
            if "#" not in battletag:
                try:
                    results = await self.api.search(battletag)
                except OverFastError as exc:
                    return await ctx.send(self.api_error(exc))
                results = [r for r in results if r.get("is_public", True)]
                if not results:
                    return await ctx.send(
                        f"No public profiles named **{battletag}**. Use your full `Name#1234`, "
                        f"and make sure your career is public. {self.PRIVATE_HELP}"
                    )
                if len(results) == 1:
                    r = results[0]
                    return await self._save_link(ctx, r["name"], r["player_id"], {"competitive": True})
                picker = LinkPicker(self, ctx, results[:25])
                picker.message = await ctx.send(
                    f"Found {len(results)} public players named **{battletag}**. Which one is you?", view=picker
                )
                return
            player_id = ofapi.to_player_id(battletag)
            try:
                summary = await self.api.summary(player_id)
            except OverFastError as exc:
                return await ctx.send(self.api_error(exc))
        if summary is None:
            return await ctx.send(
                f"Couldn't find **{battletag}**. Check the spelling and number (it's case-sensitive). "
                f"New or recently-public profiles can take a bit to appear."
            )
        await self._save_link(ctx, battletag, player_id, summary)

    async def _save_link(self, ctx, battletag: str, player_id: str, summary: dict, user=None, send=None):
        user = user or ctx.author
        async with self.config.user(user).all() as conf:
            conf.update(battletag=battletag, player_id=player_id, ranks={})
        if ctx.guild:
            async with self.config.member_from_ids(ctx.guild.id, user.id).all() as m:
                m.update(battletag=None, player_id=None)
        name = summary.get("username", battletag)
        extra = "" if summary.get("competitive") else (
            f"\n-# No ranks showing yet: play some competitive, or make your career public. {self.PRIVATE_HELP}"
        )
        text = f"✅ Linked **{user.display_name}** → **{name}**. Try `{ctx.clean_prefix}ow profile`.{extra}"
        if send:
            return await send(text)
        await ctx.send(text)

    @ow.command(name="unlink")
    async def ow_unlink(self, ctx: commands.Context):
        """Unlink your BattleTag (your spray progress is kept)."""
        async with self.config.user(ctx.author).all() as conf:
            conf.update(battletag=None, player_id=None, ranks={})
        if ctx.guild:
            async with self.config.member(ctx.author).all() as m:
                m.update(battletag=None, player_id=None)
        await ctx.send("Unlinked your BattleTag. Spray progress is untouched.")

    # ------------------------------------------------------------------ #
    # Data builders
    # ------------------------------------------------------------------ #
    async def hero_index(self) -> dict[str, dict]:
        heroes = await self.api.heroes()
        return {h["key"]: h for h in heroes}

    async def find_hero(self, text: str) -> Optional[dict]:
        heroes = await self.hero_index()
        q = _norm(text)
        if not q:
            return None
        if q in HERO_ALIASES and HERO_ALIASES[q] in heroes:
            return heroes[HERO_ALIASES[q]]
        by_name = {_norm(h["name"]): h for h in heroes.values()}
        by_key = {_norm(k): h for k, h in heroes.items()}
        if q in by_name:
            return by_name[q]
        if q in by_key:
            return by_key[q]
        starts = [h for n, h in by_name.items() if n.startswith(q)]
        if len(starts) == 1:
            return starts[0]
        close = difflib.get_close_matches(q, list(by_name), n=1, cutoff=0.7)
        return by_name[close[0]] if close else None

    @staticmethod
    def _played(stats: Optional[dict]) -> bool:
        g = (stats or {}).get("general") or {}
        return bool(g.get("games_played") or g.get("time_played"))

    async def _fetch(self, player_id: str, mode: str):
        summary, stats = await asyncio.gather(
            self.api.summary(player_id),
            self.api.stats(player_id, gamemode=None if mode == "all" else mode),
        )
        return summary, stats

    def _ranks(self, summary) -> tuple[list, Optional[int]]:
        platforms = ofapi.ranks_by_platform(summary)
        both = len(platforms) > 1
        out, season = [], None
        for platform, block in platforms.items():
            season = season or block.get("season")
            for role in ofapi.ROLE_ORDER:
                rank = block.get(role)
                if rank:
                    out.append((role, {**rank, "platform": platform if both else None}))
        return out, season

    async def profile_card(self, player_id: str, label: str, mode: str):
        try:
            summary, stats = await self._fetch(player_id, mode)
        except OverFastError as exc:
            return self.api_error(exc), None
        if summary is None:
            return f"Couldn't find **{label}** anymore. Did the BattleTag change? Re-link with `ow link`.", None
        name = summary.get("username") or label
        if not self._played(stats):
            if mode != "all":
                return f"**{name}** has no {MODE_LABELS[mode]} games on record.", None
            return f"🔒 **{name}**'s career profile is private or empty. {self.PRIVATE_HELP}", None
        heroes_idx = await self.hero_index()
        top = sorted((stats.get("heroes") or {}).items(), key=lambda kv: kv[1].get("time_played") or 0, reverse=True)
        heroes = []
        for key, s in top[:3]:
            info = heroes_idx.get(key, {})
            heroes.append({"name": info.get("name", key.title()), "role": info.get("role"),
                           "portrait": info.get("portrait"), "stats": s})
        ranks, season = self._ranks(summary)
        p = {
            "name": name, "title": summary.get("title"), "avatar": summary.get("avatar"),
            "namecard": summary.get("namecard"), "endorsement": (summary.get("endorsement") or {}).get("level"),
            "mode_label": MODE_LABELS[mode], "ranks": ranks, "season": season,
            "general": stats.get("general"), "roles": stats.get("roles") or {}, "heroes": heroes,
        }
        urls = [p["avatar"], p["namecard"]] + [r.get("rank_icon") for _, r in ranks] + [h["portrait"] for h in heroes]
        images = await self.api.images(urls)
        png = await asyncio.get_running_loop().run_in_executor(None, render.render_profile, p, images)
        return None, png_file(png, f"ow-{mode}")

    async def hero_card(self, player_id: str, label: str, hero: dict, mode: str, region: str):
        try:
            summary, stats = await self._fetch(player_id, mode)
        except OverFastError as exc:
            return self.api_error(exc), None
        if summary is None:
            return f"Couldn't find **{label}**.", None
        name = summary.get("username") or label
        hs = ((stats or {}).get("heroes") or {}).get(hero["key"])
        if not hs or not (hs.get("games_played") or hs.get("time_played")):
            if not self._played(stats):
                return f"🔒 **{name}**'s career profile is private or empty. {self.PRIVATE_HELP}", None
            where = "" if mode == "all" else f" in {MODE_LABELS[mode]}"
            return f"**{name}** hasn't played **{hero['name']}**{where}.", None
        meta = None
        try:
            meta_mode = "competitive" if mode == "competitive" else "quickplay"
            rows = await self.api.meta(meta_mode, region)
            meta = next((r for r in rows if r.get("hero") == hero["key"]), None)
        except OverFastError:
            pass
        p = {"player": name, "hero": hero, "stats": hs, "mode_label": MODE_LABELS[mode], "meta": meta}
        images = await self.api.images([hero.get("portrait")])
        png = await asyncio.get_running_loop().run_in_executor(None, render.render_hero, p, images)
        return None, png_file(png, f"ow-{hero['key']}")

    async def compare_card(self, a: tuple, b: tuple, mode: str):
        sides = []
        for player_id, label in (a, b):
            try:
                summary, stats = await self._fetch(player_id, mode)
            except OverFastError as exc:
                return self.api_error(exc), None
            if summary is None:
                return f"Couldn't find **{label}**.", None
            name = summary.get("username") or label
            if not self._played(stats):
                return f"🔒 **{name}** has no public {MODE_LABELS[mode]} stats.", None
            general = dict(stats["general"])
            ranks, _ = self._ranks(summary)
            best = max(ranks, key=lambda r: ofapi.rank_value(r[1]), default=None)
            general["_best_rank"] = ofapi.rank_value(best[1]) if best else 0
            general["_best_rank_label"] = f"{ofapi.rank_label(best[1])} {best[0].title()}" if best else "Unranked"
            sides.append({"name": name, "avatar": summary.get("avatar"), "general": general})
        images = await self.api.images([s["avatar"] for s in sides])
        png = await asyncio.get_running_loop().run_in_executor(
            None, render.render_compare, sides[0], sides[1], MODE_LABELS[mode], images
        )
        return None, png_file(png, "ow-compare")

    async def _send_card(self, ctx, render_fn, mode="all"):
        async with ctx.typing():
            content, file = await render_fn(mode)
        if file is None:
            return await ctx.send(content)
        view = ModeView(render_fn, mode)
        view.message = await ctx.send(file=file, view=view)

    @staticmethod
    def _split_mode(text: Optional[str]) -> tuple[Optional[str], str]:
        """'@friend comp' -> ('@friend', 'competitive')."""
        if not text:
            return None, "all"
        parts = text.rsplit(maxsplit=1)
        if parts[-1].lower() in MODE_ALIASES:
            return (parts[0] if len(parts) == 2 else None), MODE_ALIASES[parts[-1].lower()]
        return text, "all"

    # ------------------------------------------------------------------ #
    # Stat commands
    # ------------------------------------------------------------------ #
    @ow.command(name="profile", aliases=["p", "stats", "card"])
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def ow_profile(self, ctx: commands.Context, *, player: Optional[str] = None):
        """Your profile card, or someone else's.

        Buttons switch between All / Quick Play / Competitive.
        Examples: `[p]ow profile`, `[p]ow profile @friend`, `[p]ow profile Name#1234 comp`
        """
        player, mode = self._split_mode(player)
        try:
            target = await self._target(ctx, player)
        except LookupError_ as exc:
            return await ctx.send(str(exc))
        await self._send_card(ctx, lambda m: self.profile_card(*target, m), mode)

    @ow.command(name="hero", aliases=["h"])
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def ow_hero(self, ctx: commands.Context, hero: str, *, player: Optional[str] = None):
        """Your stats on one hero, compared with everyone else's.

        Examples: `[p]ow hero ana`, `[p]ow hero "soldier 76" @friend`, `[p]ow hero kiri comp`
        """
        player, mode = self._split_mode(player)
        info = await self._hero_or_say(ctx, hero)
        if not info:
            return
        try:
            target = await self._target(ctx, player)
        except LookupError_ as exc:
            return await ctx.send(str(exc))
        region = await self.config.guild(ctx.guild).region() if ctx.guild else "americas"
        await self._send_card(ctx, lambda m: self.hero_card(*target, info, m, region), mode)

    async def _hero_or_say(self, ctx, text) -> Optional[dict]:
        try:
            info = await self.find_hero(text)
        except OverFastError as exc:
            await ctx.send(self.api_error(exc))
            return None
        if info is None:
            await ctx.send(f"I don't know a hero called **{text}**.")
        return info

    @ow.command(name="mains", aliases=["heroes"])
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def ow_mains(self, ctx: commands.Context, *, player: Optional[str] = None):
        """Most-played heroes with win rate and KDA. Add `comp` or `qp` to filter."""
        player, mode = self._split_mode(player)
        try:
            player_id, label = await self._target(ctx, player)
        except LookupError_ as exc:
            return await ctx.send(str(exc))
        async with ctx.typing():
            try:
                summary, stats = await self._fetch(player_id, mode)
                heroes_idx = await self.hero_index()
            except OverFastError as exc:
                return await ctx.send(self.api_error(exc))
        if not self._played(stats):
            return await ctx.send(f"🔒 No public stats for **{label}**. {self.PRIVATE_HELP}")
        name = (summary or {}).get("username") or label
        top = sorted((stats.get("heroes") or {}).items(), key=lambda kv: kv[1].get("time_played") or 0, reverse=True)[:10]
        lines = []
        for i, (key, s) in enumerate(top):
            info = heroes_idx.get(key, {})
            emoji = ROLE_EMOJI.get(info.get("role"), "•")
            place = MEDALS[i] if i < 3 else f"`{i + 1:>2}.`"
            lines.append(
                f"{place} {emoji} **{info.get('name', key)}** · {render.hours(s.get('time_played'))}\n"
                f"╰ {s.get('winrate') or 0:.0f}% WR · {s.get('kda') or 0:.2f} KDA · {s.get('games_played') or 0:,} games"
            )
        embed = discord.Embed(title=f"🎮 {name}'s mains · {MODE_LABELS[mode]}", description="\n".join(lines), color=0xF99E1A)
        if summary and summary.get("avatar"):
            embed.set_thumbnail(url=summary["avatar"])
        await ctx.send(embed=embed)

    @ow.command(name="compare", aliases=["vs"])
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def ow_compare(self, ctx: commands.Context, player_one: str, player_two: Optional[str] = None):
        """Head-to-head card. `[p]ow compare @friend` (you vs them) or `[p]ow compare @a @b`."""
        try:
            if player_two is None:
                a, b = await self._target(ctx, None), await self._target(ctx, player_one)
            else:
                a, b = await self._target(ctx, player_one), await self._target(ctx, player_two)
        except LookupError_ as exc:
            return await ctx.send(str(exc))
        await self._send_card(ctx, lambda m: self.compare_card(a, b, m))

    @ow.command(name="leaderboard", aliases=["lb", "top"])
    @commands.guild_only()
    @commands.cooldown(1, 20, commands.BucketType.guild)
    async def ow_leaderboard(self, ctx: commands.Context, stat: str = "rank", mode: str = "all"):
        """Rank everyone in the server who linked.

        Stats: `rank` (best role), `tank` `damage` `support` ranks, `winrate`, `kda`, `hours`,
        `games`, `damage`, `healing`, `elims`, or a **hero name** for hours on that hero.
        Add `comp` or `qp`: `[p]ow lb kda comp`, `[p]ow lb kiriko`
        """
        stat_l = LB_ALIASES.get(stat.lower(), stat.lower())
        mode = MODE_ALIASES.get(mode.lower(), "all")
        links = await self.guild_links(ctx.guild)
        if not links:
            return await ctx.send(f"Nobody's linked yet. Start with `{ctx.clean_prefix}ow link Name#1234`.")
        links = links[:LB_LIMIT]

        rank_role = None
        if stat_l in ("rank", "ranks", "sr"):
            rank_role = "best"
        elif stat_l in ROLE_ALIASES and stat_l not in LB_STATS:
            rank_role = ROLE_ALIASES[stat_l]
        hero = None
        if rank_role is None and stat_l not in LB_STATS:
            hero = await self._hero_or_say(ctx, stat)
            if hero is None:
                return

        async def one(member, data):
            try:
                if rank_role:
                    summary = await self.api.summary(data["player_id"])
                    return member, summary, None
                return member, None, await self.api.stats(data["player_id"], gamemode=None if mode == "all" else mode)
            except OverFastError:
                return member, None, None

        async with ctx.typing():
            results = await asyncio.gather(*(one(m, d) for m, d in links))

        rows = []
        for member, summary, stats in results:
            if rank_role:
                ranks, _ = self._ranks(summary)
                pool = [r for r in ranks if rank_role == "best" or r[0] == rank_role]
                best = max(pool, key=lambda r: ofapi.rank_value(r[1]), default=None)
                if best:
                    role_txt = f" {ROLE_EMOJI.get(best[0], '')}" if rank_role == "best" else ""
                    rows.append((ofapi.rank_value(best[1]), member, f"{ofapi.rank_label(best[1])}{role_txt}"))
            elif hero:
                hs = ((stats or {}).get("heroes") or {}).get(hero["key"])
                if hs and hs.get("time_played"):
                    rows.append((hs["time_played"], member,
                                 f"{render.hours(hs['time_played'])} · {hs.get('winrate') or 0:.0f}% WR"))
            elif self._played(stats):
                label, key_fn, fmt = LB_STATS[stat_l]
                g = stats["general"]
                rows.append((key_fn(g), member, fmt(g)))
        rows.sort(key=lambda r: r[0], reverse=True)

        if rank_role:
            title = "Competitive rank" if rank_role == "best" else f"{rank_role.title()} rank"
        elif hero:
            title = f"{hero['name']} time played"
        else:
            title = LB_STATS[stat_l][0]
        lines = [f"{MEDALS[i] if i < 3 else f'`{i + 1:>2}.`'} **{m.display_name}** — {txt}" for i, (_, m, txt) in enumerate(rows)]
        embed = discord.Embed(
            title=f"🏆 Overwatch · {title}" + ("" if rank_role else f" · {MODE_LABELS[mode]}"),
            description="\n".join(lines) or "Nobody has public data for this yet.",
            color=0xF99E1A,
        )
        missing = len(links) - len(rows)
        if missing:
            embed.set_footer(text=f"{missing} linked player{'s' if missing != 1 else ''} with no public data for this")
        await ctx.send(embed=embed)

    @ow.command(name="tracker")
    @commands.guild_only()
    async def ow_tracker(self, ctx: commands.Context):
        """Everyone who linked: ranks, quick stats and spray progress."""
        links = await self.guild_links(ctx.guild)
        if not links:
            return await ctx.send(f"Nobody's linked yet. Start with `{ctx.clean_prefix}ow link`.")
        challenges = await self.config.guild(ctx.guild).challenges()
        members_conf = await self.config.all_members(ctx.guild)
        total = len(challenges)

        async def build(member, data):
            try:
                summary, stats = await self._fetch(data["player_id"], "all")
            except OverFastError:
                summary = stats = None
            done = len(self._done_on_board(members_conf.get(member.id, {}).get("done", []), challenges))
            return member, data, summary, stats, done

        async with ctx.typing():
            rows = await asyncio.gather(*(build(m, d) for m, d in links))
        rows.sort(key=lambda x: (-x[4], x[0].display_name.lower()))
        emb = discord.Embed(title=f"Overwatch Tracker — {ctx.guild.name}", color=0xF99E1A)
        for member, data, summary, stats, done in rows[:MAX_EMBED_FIELDS]:
            name = (summary or {}).get("username") or data.get("battletag") or member.display_name
            ranks, _ = self._ranks(summary)
            rank_txt = " | ".join(f"{ROLE_EMOJI.get(r, '')} {ofapi.rank_label(x)}" for r, x in ranks) or "Unranked / private"
            g = (stats or {}).get("general") or {}
            stat_txt = (f"{g.get('winrate') or 0:.0f}% WR · {g.get('kda') or 0:.2f} KDA · {render.hours(g.get('time_played'))}"
                        if self._played(stats) else "No public stats")
            spray = f" — {SPRAY} {done}/{total}" if total else ""
            emb.add_field(name=f"{member.display_name} ({name})", value=f"{rank_txt}\n{stat_txt}{spray}", inline=False)
        footer = [f"{SPRAY} = spray challenges done · {total} on the board"] if total else []
        if len(rows) > MAX_EMBED_FIELDS:
            footer.append(f"showing {MAX_EMBED_FIELDS} of {len(rows)} linked")
        if footer:
            emb.set_footer(text=" · ".join(footer))
        await ctx.send(embed=emb)

    # ------------------------------------------------------------------ #
    # Party tools
    # ------------------------------------------------------------------ #
    @ow.command(name="roll", aliases=["random", "pick"])
    @commands.cooldown(3, 10, commands.BucketType.user)
    async def ow_roll(self, ctx: commands.Context, role: Optional[str] = None):
        """Random heroes. In a voice channel, rolls a role + hero for everyone in it.

        `[p]ow roll` · `[p]ow roll support` (just you, one role)
        """
        role = ROLE_ALIASES.get(role.lower()) if role else None
        try:
            heroes = list((await self.hero_index()).values())
        except OverFastError as exc:
            return await ctx.send(self.api_error(exc))
        voice = getattr(getattr(ctx.author, "voice", None), "channel", None)
        party = [m for m in voice.members if not m.bot] if voice and role is None else []

        def roll():
            if len(party) >= 2:
                players = party[:6]
                pool = ["tank", "tank", "damage", "damage", "support", "support"] if len(players) == 6 else \
                    ["tank", "damage", "damage", "support", "support"]
                random.shuffle(pool)
                taken, lines = set(), []
                for member, r in sorted(zip(players, pool), key=lambda x: ("tank", "damage", "support").index(x[1])):
                    choices = [h for h in heroes if h["role"] == r and h["key"] not in taken]
                    h = random.choice(choices)
                    taken.add(h["key"])
                    lines.append(f"{ROLE_EMOJI[r]} **{member.display_name}** → **{h['name']}**")
                embed = discord.Embed(title=f"🎲 Team roll · {voice.name}", description="\n".join(lines), color=0xF99E1A)
                if len(party) > 6:
                    embed.set_footer(text=f"Rolled the first 6 of {len(party)} in voice")
                return None, embed
            pool = [h for h in heroes if not role or h["role"] == role]
            h = random.choice(pool)
            embed = discord.Embed(
                title=f"🎲 {ctx.author.display_name}, you're playing **{h['name']}**",
                description=f"{ROLE_EMOJI.get(h['role'], '')} {h['role'].title()}", color=0xF99E1A,
            )
            embed.set_thumbnail(url=h.get("portrait"))
            return None, embed

        content, embed = roll()
        view = RerollView(ctx.author.id, lambda: asyncio.sleep(0, result=roll()))
        view.message = await ctx.send(content=content, embed=embed, view=view)

    @ow.command(name="map")
    @commands.cooldown(3, 10, commands.BucketType.user)
    async def ow_map(self, ctx: commands.Context, *, mode: Optional[str] = None):
        """Random map for customs. Optionally a mode: `push`, `control`, `escort`, `hybrid`, `flashpoint`…"""
        try:
            maps = await self.api.maps()
        except OverFastError as exc:
            return await ctx.send(self.api_error(exc))
        maps = [m for m in maps if "standard" in (m.get("uses") or ["standard"])
                and not set(m.get("gamemodes") or []) & {"practice-range", "workshop"}]
        if mode:
            want = _norm(mode)
            maps = [m for m in maps if any(_norm(g) == want or _norm(g).startswith(want) for g in m.get("gamemodes") or [])]
        if not maps:
            return await ctx.send("No maps match that mode.")

        def roll():
            m = random.choice(maps)
            modes = ", ".join(g.replace("-", " ").title() for g in m.get("gamemodes") or [])
            embed = discord.Embed(title=f"🗺️ {m['name']}", description=f"{modes} · {m.get('location', '')}", color=0xF99E1A)
            if m.get("screenshot"):
                embed.set_image(url=m["screenshot"])
            return None, embed

        content, embed = roll()
        view = RerollView(ctx.author.id, lambda: asyncio.sleep(0, result=roll()))
        view.message = await ctx.send(embed=embed, view=view)

    @ow.command(name="meta")
    @commands.cooldown(2, 15, commands.BucketType.channel)
    async def ow_meta(self, ctx: commands.Context, *filters: str):
        """Most-picked and best-winning heroes right now.

        Mix and match: a role (`tank` `dps` `support`), a rank (`gold`, `diamond`…), and `qp`/`comp`.
        `[p]ow meta` · `[p]ow meta support` · `[p]ow meta dps diamond comp`
        """
        role = division = None
        mode = "competitive"
        for f in filters:
            f = f.lower()
            if f in ROLE_ALIASES:
                role = ROLE_ALIASES[f]
            elif f in ofapi.DIVISIONS:
                division = "grandmaster" if f == "champion" else f
            elif f in MODE_ALIASES and MODE_ALIASES[f] != "all":
                mode = MODE_ALIASES[f]
        region = await self.config.guild(ctx.guild).region() if ctx.guild else "americas"
        async with ctx.typing():
            try:
                rows = await self.api.meta(mode, region, role, division)
                heroes = await self.hero_index()
            except OverFastError as exc:
                return await ctx.send(self.api_error(exc))
        if not rows:
            return await ctx.send("No meta data for that combination right now.")

        def name(r):
            h = heroes.get(r["hero"], {})
            return f"{ROLE_EMOJI.get(h.get('role'), '')} {h.get('name', r['hero'])}"

        picks = sorted(rows, key=lambda r: r.get("pickrate") or 0, reverse=True)[:8]
        wins = sorted((r for r in rows if (r.get("pickrate") or 0) >= 2), key=lambda r: r.get("winrate") or 0, reverse=True)[:8]
        bans = sorted((r for r in rows if r.get("banrate") is not None), key=lambda r: r["banrate"], reverse=True)[:5]
        bits = [MODE_LABELS[mode], region.title()]
        if role:
            bits.append(role.title())
        if division:
            bits.append(division.title())
        embed = discord.Embed(title="📈 Overwatch meta", description=" · ".join(bits), color=0xF99E1A)
        embed.add_field(name="Most picked", value="\n".join(f"{name(r)} — {r['pickrate']:.1f}%" for r in picks), inline=True)
        embed.add_field(name="Best win rate", value="\n".join(f"{name(r)} — {r['winrate']:.1f}%" for r in wins) or "—", inline=True)
        if bans and mode == "competitive":
            embed.add_field(name="Most banned", value="\n".join(f"{name(r)} — {r['banrate']:.1f}%" for r in bans), inline=False)
        embed.set_footer(text="Best win rate only counts heroes picked at least 2% of the time · data via OverFast")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    @commands.group(name="owset")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def owset(self, ctx: commands.Context):
        """Overwatch announcements and settings."""

    @owset.command(name="channel")
    async def owset_channel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Where rank-ups and spray unlocks are announced. Leave empty to turn off."""
        await self.config.guild(ctx.guild).announce_channel.set(channel.id if channel else None)
        await ctx.send(f"Announcements go to {channel.mention}." if channel else "Announcements turned off.")

    @owset.command(name="demotions")
    async def owset_demotions(self, ctx: commands.Context, on: bool):
        """Also announce rank drops (`on`/`off`). Off by default, no shame here."""
        await self.config.guild(ctx.guild).announce_demotions.set(on)
        await ctx.send(f"Rank drops will {'now' if on else 'not'} be announced.")

    @owset.command(name="shoutouts")
    async def owset_shoutouts(self, ctx: commands.Context, on: bool):
        """Announce when someone unlocks a spray (`on`/`off`)."""
        await self.config.guild(ctx.guild).spray_shoutouts.set(on)
        await ctx.send(f"Spray shout-outs {'on' if on else 'off'}.")

    @owset.command(name="region")
    async def owset_region(self, ctx: commands.Context, region: str):
        """Region for meta stats: `americas`, `europe` or `asia`."""
        region = region.lower()
        if region not in ("americas", "europe", "asia"):
            return await ctx.send("Pick `americas`, `europe` or `asia`.")
        await self.config.guild(ctx.guild).region.set(region)
        await ctx.send(f"Meta stats now use **{region.title()}**.")

    @owset.command(name="checkranks")
    async def owset_checkranks(self, ctx: commands.Context):
        """Check everyone's ranks right now instead of waiting for the next automatic check."""
        async with ctx.typing():
            changes = await self.check_ranks()
        await ctx.send(f"Checked. {changes} rank change{'s' if changes != 1 else ''} found.")

    @owset.command(name="settings")
    async def owset_settings(self, ctx: commands.Context):
        """Show this server's Overwatch settings."""
        g = await self.config.guild(ctx.guild).all()
        ch = ctx.guild.get_channel(g["announce_channel"]) if g["announce_channel"] else None
        await ctx.send(box(
            f"Announcements : {'#' + ch.name if ch else 'off'}\n"
            f"Rank drops    : {'announced' if g['announce_demotions'] else 'quiet'}\n"
            f"Spray shouts  : {'on' if g['spray_shoutouts'] else 'off'}\n"
            f"Meta region   : {g['region']}\n"
            f"Linked players: {len(await self.guild_links(ctx.guild))}\n"
            f"Board sprays  : {len(g['challenges'])}",
            lang="yaml",
        ))

    # ------------------------------------------------------------------ #
    # Rank-up alerts
    # ------------------------------------------------------------------ #
    @tasks.loop(hours=RANK_CHECK_HOURS)
    async def rank_watch(self):
        await self.bot.wait_until_red_ready()
        try:
            await self.check_ranks()
        except Exception:  # noqa: BLE001
            log.exception("rank check failed")

    async def check_ranks(self) -> int:
        guilds = {gid: d for gid, d in (await self.config.all_guilds()).items() if d.get("announce_channel")}
        # migrate any old per-server links so everyone is watched
        for gid in guilds:
            guild = self.bot.get_guild(gid)
            if guild:
                for uid, data in (await self.config.all_members(guild)).items():
                    if data.get("player_id") and (m := guild.get_member(uid)):
                        await self.get_link(m, guild)
        changes = 0
        for uid, data in (await self.config.all_users()).items():
            if not data.get("player_id"):
                continue
            try:
                summary = await self.api.summary(data["player_id"])
            except OverFastError:
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(1.5)  # be gentle with OverFast
            if summary is None:
                continue
            old = data.get("ranks") or {}
            new = {}
            events = []
            for platform, block in ofapi.ranks_by_platform(summary).items():
                season = block.get("season")
                for role in ofapi.ROLE_ORDER:
                    rank = block.get(role)
                    if not rank:
                        continue
                    key = f"{platform}:{role}"
                    value = ofapi.rank_value(rank)
                    new[key] = {"value": value, "label": ofapi.rank_label(rank), "season": season,
                                "icon": rank.get("rank_icon")}
                    prev = old.get(key)
                    if prev and prev.get("season") == season and prev["value"] != value:
                        events.append((role, platform, prev, new[key]))
            if not new and old:
                new = old  # profile went private / API blip: keep the last known ranks
            await self.config.user_from_id(uid).ranks.set(new)
            if not events:
                continue
            changes += len(events)
            await self._announce_ranks(uid, summary.get("username") or data.get("battletag"), events, guilds)
        return changes

    async def _announce_ranks(self, uid: int, name: str, events, guilds: dict):
        for gid, g in guilds.items():
            guild = self.bot.get_guild(gid)
            member = guild.get_member(uid) if guild else None
            channel = guild.get_channel(g["announce_channel"]) if member else None
            if channel is None:
                continue
            for role, platform, prev, cur in events:
                up = cur["value"] > prev["value"]
                if not up and not g.get("announce_demotions"):
                    continue
                emoji = ROLE_EMOJI.get(role, "")
                if up:
                    title = f"🎉 {member.display_name} ranked up!"
                    desc = f"{emoji} **{role.title()}**: {prev['label']} → **{cur['label']}**"
                    color = 0x48D678
                else:
                    title = f"📉 {member.display_name} dropped a rank"
                    desc = f"{emoji} **{role.title()}**: {prev['label']} → {cur['label']}\nYou'll get it back 💪"
                    color = 0xEE545C
                embed = discord.Embed(title=title, description=desc, color=color)
                if cur.get("icon"):
                    embed.set_thumbnail(url=cur["icon"])
                embed.set_footer(text=f"{name}" + (f" · {platform.upper()}" if platform == "console" else ""))
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    log.warning("couldn't post rank alert in %s", gid)

    # ------------------------------------------------------------------ #
    # Spray board
    # ------------------------------------------------------------------ #
    async def spray_shoutout(self, guild: discord.Guild, user, unlocked: list[dict], fallback_channel=None):
        g = await self.config.guild(guild).all()
        if not g["spray_shoutouts"]:
            return
        channel = guild.get_channel(g["announce_channel"]) if g["announce_channel"] else fallback_channel
        if channel is None:
            return
        done = self._done_on_board(await self.config.member_from_ids(guild.id, user.id).done(), g["challenges"])
        total = len(g["challenges"])
        names = ", ".join(f"**{c['reward']}**" for c in unlocked[:5]) + (f" +{len(unlocked) - 5} more" if len(unlocked) > 5 else "")
        name = getattr(user, "display_name", str(user))
        text = f"{SPRAY} **{name}** unlocked {names}! ({len(done)}/{total})"
        if total and len(done) == total:
            text += f"\n🏆 **{name} has completed the entire spray board!**"
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    async def spray_card(self, guild: discord.Guild, member) -> Optional[discord.File]:
        challenges = await self.config.guild(guild).challenges()
        done = set(await self.config.member_from_ids(guild.id, member.id).done())
        try:
            heroes = await self.hero_index()
        except OverFastError:
            heroes = {}
        by_name = {_norm(h["name"]): h for h in heroes.values()}
        groups: dict[str, list] = {}
        for cid, c in challenges.items():
            hero = self.hero_from_reward(c["reward"])
            info = by_name.get(_norm(hero), {})
            groups.setdefault(c.get("role", "Other"), []).append((hero, info.get("portrait"), cid in done))
        ordered = [(r, sorted(groups[r], key=lambda e: e[0].lower())) for r in BOARD_ROLE_ORDER if r in groups]
        link = await self.get_link(member, guild)
        avatar = None
        if link.get("player_id"):
            try:
                avatar = ((await self.api.summary(link["player_id"])) or {}).get("avatar")
            except OverFastError:
                pass
        avatar = avatar or member.display_avatar.replace(size=128, format="png").url
        p = {"name": member.display_name, "avatar": avatar, "done": len(done & set(challenges)),
             "total": len(challenges), "groups": ordered}
        images = await self.api.images([avatar] + [e[1] for _, es in ordered for e in es])
        png = await asyncio.get_running_loop().run_in_executor(None, render.render_sprays, p, images)
        return png_file(png, "ow-sprays")

    @ow.command(name="sprays", aliases=["spray", "board"])
    @commands.guild_only()
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def ow_sprays(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Your spray collection as a picture. Tap ✏️ to tick off what you've unlocked."""
        member = member or ctx.author
        challenges = await self.config.guild(ctx.guild).challenges()
        if not challenges:
            return await ctx.send(
                f"No spray board yet. Admins: `{ctx.clean_prefix}ow challenge seedcute` loads all the cute sprays."
            )
        async with ctx.typing():
            file = await self.spray_card(ctx.guild, member)
        await ctx.send(file=file, view=self._board_view)

    # ── Original text commands (unchanged behaviour) ───────────────────
    @ow.group(name="challenge", aliases=["challenges", "c"])
    @commands.guild_only()
    async def ow_challenge(self, ctx: commands.Context):
        """Manage and check off spray challenges. Tip: `[p]ow sprays` has buttons."""

    async def _send_paged(self, ctx: commands.Context, title: str, body: str):
        pages = list(pagify(body, delims=["\n"], page_length=3900)) or [""]
        for i, page in enumerate(pages, 1):
            suffix = f" ({i}/{len(pages)})" if len(pages) > 1 else ""
            await ctx.send(embed=discord.Embed(title=title + suffix, description=page, color=await ctx.embed_color()))

    @ow_challenge.command(name="list")
    async def challenge_list(self, ctx: commands.Context):
        """Show the board grouped by role, with how many people have each spray."""
        challenges = await self.config.guild(ctx.guild).challenges()
        if not challenges:
            return await ctx.send(
                f"No challenges yet. Admins can bulk-load the cute sprays with "
                f"`{ctx.clean_prefix}ow challenge seedcute`, or add one with `{ctx.clean_prefix}ow challenge add`."
            )
        members = await self.config.all_members(ctx.guild)
        by_role: dict[str, list] = {}
        for cid, c in challenges.items():
            by_role.setdefault(c.get("role", "Other"), []).append((cid, c))
        for role in BOARD_ROLE_ORDER:
            entries = by_role.get(role)
            if not entries:
                continue
            emoji, color = BOARD_ROLE_META[role]
            entries.sort(key=lambda kv: self.hero_from_reward(kv[1]["reward"]).lower())
            lines = []
            for cid, c in entries:
                count = sum(1 for d in members.values() if cid in d.get("done", []))
                badge = f"  `✅ {count}`" if count else ""
                lines.append(f"`#{cid}` **{self.hero_from_reward(c['reward'])}** — {self.short_condition(c['name'])}{badge}")
            pages = list(pagify("\n".join(lines), delims=["\n"], page_length=3900)) or [""]
            for i, page in enumerate(pages, 1):
                suffix = f" ({i}/{len(pages)})" if len(pages) > 1 else ""
                await ctx.send(embed=discord.Embed(
                    title=f"{emoji} {role} Sprays ({len(entries)}){suffix}", description=page, color=discord.Color(color),
                ))

    @ow_challenge.command(name="mine")
    async def challenge_mine(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Show which challenges you (or another member) have completed."""
        member = member or ctx.author
        challenges = await self.config.guild(ctx.guild).challenges()
        if not challenges:
            return await ctx.send("No challenges on the board yet.")
        done = self._done_on_board(await self.config.member(member).done(), challenges)
        total = len(challenges)
        header = f"**{member.display_name}** — {len(done)}/{total}  {self._bar(len(done), total)}\n\n"
        if not done:
            return await ctx.send(header + "*Nothing checked off yet.*")
        body = "\n".join(f"✅ **{challenges[cid]['reward']}** — {challenges[cid]['name']}" for cid in sorted(done, key=int))
        await self._send_paged(ctx, f"{SPRAY} {member.display_name}'s Sprays", header + body)

    @ow_challenge.command(name="who")
    async def challenge_who(self, ctx: commands.Context, challenge_id: str):
        """Show exactly who has completed one challenge."""
        challenges = await self.config.guild(ctx.guild).challenges()
        if challenge_id not in challenges:
            return await ctx.send(f"No challenge with that ID. See `{ctx.clean_prefix}ow challenge list`.")
        members = await self.config.all_members(ctx.guild)
        doers = [m.display_name for uid, d in members.items()
                 if challenge_id in d.get("done", []) and (m := ctx.guild.get_member(uid))]
        c = challenges[challenge_id]
        await ctx.send(f"**{c['reward']}** — {c['name']}\nDone by: {', '.join(sorted(doers, key=str.lower)) if doers else 'Nobody yet'}")

    @ow_challenge.command(name="done")
    async def challenge_done(self, ctx: commands.Context, challenge_id: str):
        """Mark a challenge complete for yourself, e.g. `[p]ow challenge done 3`."""
        challenges = await self.config.guild(ctx.guild).challenges()
        if challenge_id not in challenges:
            return await ctx.send(f"No challenge with that ID. See `{ctx.clean_prefix}ow challenge list`.")
        async with self.config.member(ctx.author).done() as done:
            if challenge_id in done:
                return await ctx.send(f"You've already got **{challenges[challenge_id]['reward']}**.")
            done.append(challenge_id)
        await ctx.send(f"Marked **{challenges[challenge_id]['reward']}** done for you. {SPRAY}")
        g = await self.config.guild(ctx.guild).all()
        if g["announce_channel"] and g["announce_channel"] != ctx.channel.id:
            await self.spray_shoutout(ctx.guild, ctx.author, [challenges[challenge_id]])

    @ow_challenge.command(name="undo")
    async def challenge_undo(self, ctx: commands.Context, challenge_id: str):
        """Un-mark a challenge for yourself."""
        async with self.config.member(ctx.author).done() as done:
            if challenge_id not in done:
                return await ctx.send("That one wasn't marked.")
            done.remove(challenge_id)
        await ctx.send("Updated.")

    @commands.admin_or_permissions(manage_guild=True)
    @ow_challenge.command(name="add")
    async def challenge_add(self, ctx: commands.Context, *, text: str):
        """Add a challenge. Format: `reward | condition` or `reward | condition | role`.

        e.g. `[p]ow challenge add Cute Shion Spray | <condition> | Damage`
        """
        if "|" not in text:
            return await ctx.send("Use the format `reward | condition` (optionally `| role`).")
        parts = [p.strip() for p in text.split("|")]
        reward = parts[0][:REWARD_MAXLEN]
        name = parts[1][:CONDITION_MAXLEN] if len(parts) > 1 else ""
        role = VALID_BOARD_ROLES.get(parts[2].lower(), "Other") if len(parts) > 2 and parts[2] else "Other"
        if not reward or not name:
            return await ctx.send("Both a reward and a condition are required.")
        next_id = await self.config.guild(ctx.guild).next_id()
        async with self.config.guild(ctx.guild).challenges() as challenges:
            challenges[str(next_id)] = {"name": name, "reward": reward, "role": role}
        await self.config.guild(ctx.guild).next_id.set(next_id + 1)
        await ctx.send(f"Added **#{next_id}** to *{role}*: {reward} — {name}")

    @commands.admin_or_permissions(manage_guild=True)
    @ow_challenge.command(name="remove")
    async def challenge_remove(self, ctx: commands.Context, challenge_id: str):
        """Remove a challenge from the board."""
        async with self.config.guild(ctx.guild).challenges() as challenges:
            removed = challenges.pop(challenge_id, None)
        if removed is None:
            return await ctx.send("No such challenge.")
        await ctx.send(f"Removed **{removed['reward']}**.")

    @commands.admin_or_permissions(manage_guild=True)
    @ow_challenge.command(name="clear")
    async def challenge_clear(self, ctx: commands.Context, confirmation: str = ""):
        """Wipe the entire challenge board. Re-run with `confirm` to proceed."""
        if confirmation.lower() != "confirm":
            return await ctx.send(
                f"This wipes **all** challenges on the board. Re-run `{ctx.clean_prefix}ow challenge clear confirm` to proceed."
            )
        await self.config.guild(ctx.guild).challenges.set({})
        await self.config.guild(ctx.guild).next_id.set(1)
        await ctx.send("Cleared the challenge board. (Members' progress entries are kept but hidden.)")

    @commands.admin_or_permissions(manage_guild=True)
    @ow_challenge.command(name="seedcute")
    async def challenge_seedcute(self, ctx: commands.Context):
        """Bulk-load the cute-spray challenges. Skips ones already on the board, so it's safe to re-run."""
        next_id = await self.config.guild(ctx.guild).next_id()
        added = 0
        async with self.config.guild(ctx.guild).challenges() as challenges:
            existing = {c["reward"] for c in challenges.values()}
            for hero, condition, role in CUTE_SPRAYS:
                reward = f"Cute {hero} Spray"
                if reward in existing:
                    continue
                challenges[str(next_id)] = {"name": condition, "reward": reward, "role": role}
                existing.add(reward)
                next_id += 1
                added += 1
        await self.config.guild(ctx.guild).next_id.set(next_id)
        await ctx.send(
            f"Seeded **{added}** cute-spray challenges. See them with `{ctx.clean_prefix}ow sprays`.\n"
            f"Note: Shion has no cute spray yet — add it with `{ctx.clean_prefix}ow challenge add` once it appears in-game."
        )
