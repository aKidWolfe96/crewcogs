"""
FortniteStats — a self-hosted Fortnite cog for Red-DiscordBot.

Powered by the official fortnite-api.com Python library (https://fortnite-api.com).
Uses only public data through a documented API: no Epic logins or locker scraping.

Player commands ([p]fn …):
    stats / season      custom stat card with Lifetime/Season + input toggles
    compare             head-to-head card
    leaderboard         server ranking of everyone who linked
    link / unlink       link your Epic, PlayStation or Xbox name
    shop                today's shop cover + private section-by-section browser
    find                item lookup: in today's shop? when was it last seen?
    wishlist            get pinged in the daily shop post when an item returns
    news                in-game news

Setup:
    1. Grab a free API key at https://dash.fortnite-api.com (Discord login).
       Only stats need it; shop/news/items work without one.
    2. [p]fnset apikey <key>      (owner only, stored globally; run it in DMs)
    3. [p]fnset shopchannel #ch   (per-server daily shop post at ~00:05 UTC)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from dataclasses import dataclass, field

import discord
from discord.ext import tasks
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, pagify

import fortnite_api
from fortnite_api import AccountType, MatchMethod, ResponseFlags, TimeWindow

from . import shoprender as sr
from . import statsrender as st
from .views import CompareView, ShopBrowser, ShopPostView, StatsView, WishlistButton, png_file

log = logging.getLogger("red.fortnitestats")

# Item shop rotates at 00:00 UTC. Post a few minutes after so the API has refreshed.
SHOP_POST_TIME = dt.time(hour=0, minute=5, tzinfo=dt.timezone.utc)
SHOP_REFRESH_SECONDS = 300
STATS_CACHE_SECONDS = 600
LEADERBOARD_LIMIT = 25
WISHLIST_LIMIT = 25

PLATFORMS = {
    "epic": AccountType.EPIC, "pc": AccountType.EPIC,
    "psn": AccountType.PSN, "ps": AccountType.PSN, "ps4": AccountType.PSN, "ps5": AccountType.PSN,
    "playstation": AccountType.PSN,
    "xbox": AccountType.XBL, "xbl": AccountType.XBL, "xb": AccountType.XBL,
}
PLATFORM_LABELS = {"epic": "Epic", "psn": "PlayStation", "xbl": "Xbox"}
WINDOWS = {"lifetime": TimeWindow.LIFETIME, "season": TimeWindow.SEASON}
WINDOW_LABELS = {"lifetime": "Lifetime", "season": "This Season"}

LEADERBOARD_STATS = {
    "wins": ("Wins", lambda o: o["wins"], lambda o: f"{o['wins']:,} wins"),
    "kd": ("K/D", lambda o: o["kd"], lambda o: f"{o['kd']:.2f} K/D"),
    "winrate": ("Win rate", lambda o: o["win_rate"], lambda o: f"{o['win_rate']:.1f}% win rate"),
    "kills": ("Kills", lambda o: o["kills"], lambda o: f"{o['kills']:,} kills"),
    "matches": ("Matches", lambda o: o["matches"], lambda o: f"{o['matches']:,} matches"),
    "top10": ("Top 10s", lambda o: o["top10"] or 0, lambda o: f"{o['top10'] or 0:,} top 10s"),
}
LB_ALIASES = {"win": "wins", "k/d": "kd", "kdr": "kd", "wr": "winrate", "win%": "winrate",
              "kill": "kills", "games": "matches", "match": "matches", "top": "top10"}
MEDALS = ("🥇", "🥈", "🥉")

RARITY_COLORS = {
    "common": 0xB1B1B1, "uncommon": 0x60B932, "rare": 0x3675D6, "epic": 0xB13AF0,
    "legendary": 0xE2843C, "mythic": 0xE5C13B, "icon": 0x1FC9C3, "marvel": 0xC53D3E,
    "dc": 0x4F5DD6, "starwars": 0x1A1A1A, "gaminglegends": 0x5C2D91, "slurp": 0x2FD9D2,
    "lava": 0xD45A22, "frozen": 0x6FCBE3, "shadow": 0x3A3A3A, "dark": 0x8B2FB0,
}


@dataclass
class ShopState:
    """One shop rotation: parsed model, downloaded art and rendered images."""

    model: sr.ShopModel
    art: sr.ArtCache = field(default_factory=sr.ArtCache)
    cover: bytes | None = None
    pages: dict = field(default_factory=dict)
    fetched: float = field(default_factory=time.monotonic)


class StatsLookupError(Exception):
    pass


def _next_reset() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    nxt = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(nxt.timestamp())


class FortniteStats(commands.Cog):
    """Fortnite stat cards, a browsable item shop, wishlists, news and item lookups."""

    __version__ = "2.0.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=874203991, force_registration=True)
        self.config.register_global(api_key=None)
        self.config.register_guild(shop_channel=None, last_shop_hash=None)
        # Links used to be per-server (member); they're per-user now so they work
        # everywhere, including DMs. Old member links are migrated on first use.
        self.config.register_member(epic_name=None, account_id=None, platform="epic")
        self.config.register_user(epic_name=None, account_id=None, platform="epic", wishlist=[])
        self._client: fortnite_api.Client | None = None
        self._shop: ShopState | None = None
        self._shop_lock = asyncio.Lock()
        self._stats_cache: dict[tuple, tuple[float, object]] = {}
        self._post_view: ShopPostView | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def cog_load(self):
        self._post_view = ShopPostView(self)
        self.bot.add_view(self._post_view)
        self.auto_shop.start()

    async def cog_unload(self):
        self.auto_shop.cancel()
        if self._post_view is not None:
            self._post_view.stop()
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        await self.config.user_from_id(user_id).clear()
        for guild_id in (await self.config.all_members()).keys():
            await self.config.member_from_ids(guild_id, user_id).clear()

    async def _get_client(self) -> fortnite_api.Client:
        """Lazily build (and reuse) one async client for the cog's lifetime."""
        if self._client is None:
            api_key = await self.config.api_key()
            self._client = fortnite_api.Client(api_key=api_key)
            await self._client.__aenter__()
        return self._client

    async def get_link(self, user, guild: discord.Guild | None = None) -> dict:
        """A user's linked account, migrating an old per-server link if needed."""
        data = await self.config.user(user).all()
        if data["epic_name"] or guild is None:
            return data
        legacy = await self.config.member_from_ids(guild.id, user.id).all()
        if legacy.get("epic_name"):
            async with self.config.user(user).all() as conf:
                conf.update({k: legacy.get(k) for k in ("epic_name", "account_id", "platform")})
            await self.config.member_from_ids(guild.id, user.id).clear()
            data = await self.config.user(user).all()
        return data

    async def set_link(self, user, epic_name, account_id, platform):
        async with self.config.user(user).all() as conf:
            conf.update({"epic_name": epic_name, "account_id": account_id, "platform": platform})

    async def guild_links(self, guild: discord.Guild) -> list[tuple[discord.Member, dict]]:
        """Every member of `guild` with a linked account (new and legacy links)."""
        found: dict[int, dict] = {}
        for member_id, data in (await self.config.all_members(guild)).items():
            if data.get("epic_name"):
                found[member_id] = data
        for user_id, data in (await self.config.all_users()).items():
            if data.get("epic_name"):
                found[user_id] = data
        out = []
        for uid, data in found.items():
            member = guild.get_member(uid)
            if member is not None:
                out.append((member, data))
        return out

    def format_help_for_context(self, ctx):
        pre = super().format_help_for_context(ctx)
        return f"{pre}\n\nCog Version: {self.__version__}"

    # ------------------------------------------------------------------ #
    # Owner / admin config
    # ------------------------------------------------------------------ #
    @commands.group(name="fnset")
    async def fnset(self, ctx: commands.Context):
        """Configure the FortniteStats cog."""

    @fnset.command(name="apikey")
    @commands.is_owner()
    async def fnset_apikey(self, ctx: commands.Context, *, key: str):
        """Set the fortnite-api.com API key (needed for player stats).

        Get one free at https://dash.fortnite-api.com (log in with Discord).
        Run this in DMs to keep the key out of your channel history.
        """
        await self.config.api_key.set(key)
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        await ctx.send("✅ API key saved. (Your message was removed if I had permission.)")

    @fnset.command(name="shopchannel")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def fnset_shopchannel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Set (or clear) the channel for the daily shop post.

        Leave the channel out to turn the daily post off.
        """
        if channel is None:
            await self.config.guild(ctx.guild).shop_channel.clear()
            return await ctx.send("Daily shop post turned off.")
        await self.config.guild(ctx.guild).shop_channel.set(channel.id)
        await ctx.send(
            f"🛒 The item shop will post in {channel.mention} every day at <t:{_next_reset() + 300}:t> "
            "(5 minutes after it resets), with wishlist pings."
        )

    @fnset.command(name="postshop")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def fnset_postshop(self, ctx: commands.Context):
        """Post today's shop to the shop channel right now (with wishlist pings)."""
        channel_id = await self.config.guild(ctx.guild).shop_channel()
        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return await ctx.send(f"Set a shop channel first with `{ctx.clean_prefix}fnset shopchannel #channel`.")
        async with ctx.typing():
            state = await self.shop_state(force=True)
            ok = await self._post_shop(ctx.guild, channel, state)
        await ctx.send(f"Posted in {channel.mention}." if ok else "I couldn't post there. Check my permissions.")

    @fnset.command(name="settings")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def fnset_settings(self, ctx: commands.Context):
        """Show current settings for this server."""
        has_key = bool(await self.config.api_key())
        ch_id = await self.config.guild(ctx.guild).shop_channel()
        ch = ctx.guild.get_channel(ch_id) if ch_id else None
        linked = len(await self.guild_links(ctx.guild))
        await ctx.send(box(
            f"API key set   : {'yes' if has_key else 'no (stats are off)'}\n"
            f"Daily shop    : {'#' + ch.name if ch else 'off'}\n"
            f"Linked players: {linked}",
            lang="yaml",
        ))

    # ------------------------------------------------------------------ #
    # Accounts
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="fn")
    async def fn(self, ctx: commands.Context):
        """Fortnite stats, item shop, wishlists and news."""

    @staticmethod
    def _split_platform(text: str) -> tuple[str, str]:
        """'psn Some Name' -> ('psn', 'Some Name'); defaults to epic."""
        parts = text.strip().split(maxsplit=1)
        if len(parts) == 2 and parts[0].lower() in PLATFORMS:
            return PLATFORMS[parts[0].lower()].value, parts[1].strip()
        return "epic", text.strip()

    @fn.command(name="link")
    async def fn_link(self, ctx: commands.Context, *, name: str):
        """Link your Fortnite account so stat commands work without a name.

        Examples:
        `[p]fn link KrustyKrabby` (Epic name, works for every platform)
        `[p]fn link psn KrustyKrabby` (PlayStation name)
        `[p]fn link xbox KrustyKrabby` (Xbox gamertag)
        """
        platform, name = self._split_platform(name)
        if not await self.config.api_key():
            await self.set_link(ctx.author, name, None, platform)
            return await ctx.send(f"🔗 Linked you to **{name}** ({PLATFORM_LABELS[platform]}).")

        async with ctx.typing():
            lookup = {"name": name, "platform": platform}
            try:
                stats = await self.fetch_stats(lookup, "lifetime")
            except fortnite_api.Forbidden:
                await self.set_link(ctx.author, name, None, platform)
                return await ctx.send(
                    f"🔗 Linked you to **{name}**, but that account's stats are **private** so nothing will show yet.\n"
                    "Fix it in Fortnite: **Settings → Account and Privacy → Show on Career Leaderboard → On**."
                )
            except Exception as exc:  # noqa: BLE001
                return await ctx.send(self.stats_error(exc, lookup))

        await self.set_link(ctx.author, stats.user.name, stats.user.id, platform)
        await ctx.send(
            f"🔗 Linked you to **{stats.user.name}** ({PLATFORM_LABELS[platform]}). "
            f"Try `{ctx.clean_prefix}fn stats`. Your link keeps working even if you change your name."
        )

    @fn.command(name="unlink")
    async def fn_unlink(self, ctx: commands.Context):
        """Remove your linked Fortnite account."""
        async with self.config.user(ctx.author).all() as conf:
            conf.update({"epic_name": None, "account_id": None, "platform": "epic"})
        if ctx.guild:
            await self.config.member(ctx.author).clear()
        await ctx.send("Unlinked.")

    async def _resolve(self, ctx: commands.Context, target: str | None) -> tuple[dict, str]:
        """Turn a command argument into (lookup, display label). Raises StatsLookupError."""
        if target is None:
            data = await self.get_link(ctx.author, ctx.guild)
            if not data["epic_name"]:
                raise StatsLookupError(
                    f"Give me a name, or link yours once with `{ctx.clean_prefix}fn link <name>` "
                    f"(console names: `{ctx.clean_prefix}fn link psn <name>`)."
                )
            return self._lookup_from(data), PLATFORM_LABELS.get(data["platform"], "Epic")

        if ctx.guild:
            try:
                member = await commands.MemberConverter().convert(ctx, target)
            except commands.BadArgument:
                member = None
            if member is not None:
                data = await self.get_link(member, ctx.guild)
                if data["epic_name"]:
                    return self._lookup_from(data), PLATFORM_LABELS.get(data["platform"], "Epic")
                if target.startswith("<@"):
                    raise StatsLookupError(f"**{member.display_name}** hasn't linked a Fortnite account yet.")

        platform, name = self._split_platform(target)
        return {"name": name, "platform": platform}, PLATFORM_LABELS[platform]

    @staticmethod
    def _lookup_from(data: dict) -> dict:
        if data.get("account_id"):
            return {"account_id": data["account_id"], "name": data["epic_name"], "platform": data.get("platform", "epic")}
        return {"name": data["epic_name"], "platform": data.get("platform", "epic")}

    # ------------------------------------------------------------------ #
    # Stats plumbing
    # ------------------------------------------------------------------ #
    async def fetch_stats(self, lookup: dict, window: str):
        key = (lookup.get("account_id") or f"{lookup.get('platform')}:{lookup['name'].lower()}", window)
        hit = self._stats_cache.get(key)
        if hit and time.monotonic() - hit[0] < STATS_CACHE_SECONDS:
            return hit[1]
        client = await self._get_client()
        if lookup.get("account_id"):
            stats = await client.fetch_br_stats(account_id=lookup["account_id"], time_window=WINDOWS[window])
        else:
            stats = await client.fetch_br_stats(
                name=lookup["name"], type=AccountType(lookup.get("platform", "epic")), time_window=WINDOWS[window]
            )
        self._stats_cache[key] = (time.monotonic(), stats)
        if len(self._stats_cache) > 500:
            cutoff = time.monotonic() - STATS_CACHE_SECONDS
            self._stats_cache = {k: v for k, v in self._stats_cache.items() if v[0] > cutoff}
        return stats

    def stats_error(self, exc: Exception, lookup: dict) -> str:
        name = lookup.get("name", "that player")
        if isinstance(exc, fortnite_api.Forbidden):
            return (
                f"🔒 **{name}** has their stats set to private.\n"
                "Fix it in Fortnite: **Settings → Account and Privacy → Show on Career Leaderboard → On**."
            )
        if isinstance(exc, fortnite_api.NotFound):
            where = PLATFORM_LABELS.get(lookup.get("platform", "epic"), "Epic")
            tip = "" if lookup.get("platform", "epic") != "epic" else (
                "\nOn console? Try `fn stats psn <name>` or `fn stats xbox <name>`."
            )
            return f"❓ Couldn't find **{name}** on {where}.{tip}"
        if isinstance(exc, (fortnite_api.Unauthorized, fortnite_api.MissingAPIKey)):
            return "The bot owner's fortnite-api.com key is missing or invalid (`fnset apikey`)."
        if isinstance(exc, fortnite_api.RateLimited):
            return "⏳ Too many lookups at once. Try again in a few seconds."
        if isinstance(exc, fortnite_api.ServiceUnavailable):
            return "Fortnite's stats service is down right now. Try again later."
        log.exception("stats lookup failed", exc_info=exc)
        return f"Lookup failed: `{type(exc).__name__}`."

    async def stats_card(self, stats, window: str, input_key: str, platform_label: str):
        """Returns (content, file) for a stat card, or a text-only reason."""
        data = st.summarize(stats, input_key)
        if data is None:
            what = "this season" if window == "season" else "on record"
            inp = "" if input_key == "all" else f" on {st.INPUTS[input_key]}"
            return f"**{stats.user.name}** has no matches {what}{inp} yet.", None
        png = await asyncio.get_running_loop().run_in_executor(
            None, st.render_card, data, WINDOW_LABELS[window], platform_label
        )
        return None, png_file(png, f"fortnite-{window}")

    async def compare_card(self, a: tuple, b: tuple, window: str, cache: dict):
        sides = []
        for lookup, _label in (a, b):
            key = (str(lookup), window)
            try:
                if key not in cache:
                    cache[key] = await self.fetch_stats(lookup, window)
            except Exception as exc:  # noqa: BLE001
                return self.stats_error(exc, lookup), None
            data = st.summarize(cache[key])
            if data is None:
                return f"**{cache[key].user.name}** has no matches {'this season' if window == 'season' else 'yet'}.", None
            sides.append(data)
        png = await asyncio.get_running_loop().run_in_executor(None, st.render_compare, sides[0], sides[1], WINDOW_LABELS[window])
        return None, png_file(png, "fortnite-compare")

    # ------------------------------------------------------------------ #
    # Stats commands
    # ------------------------------------------------------------------ #
    async def _need_key(self, ctx) -> bool:
        if await self.config.api_key():
            return False
        await ctx.send(
            "Stats aren't set up yet. The bot owner needs a free key from https://dash.fortnite-api.com "
            f"and `{ctx.clean_prefix}fnset apikey <key>`."
        )
        return True

    async def _send_stats(self, ctx: commands.Context, target, window: str):
        if await self._need_key(ctx):
            return
        try:
            lookup, label = await self._resolve(ctx, target)
        except StatsLookupError as exc:
            return await ctx.send(str(exc))
        async with ctx.typing():
            try:
                stats = await self.fetch_stats(lookup, window)
            except Exception as exc:  # noqa: BLE001
                return await ctx.send(self.stats_error(exc, lookup))
            content, file = await self.stats_card(stats, window, "all", label)
        view = StatsView(self, lookup, label, window, {window: stats})
        view.message = await ctx.send(content=content, file=file, view=view) if file else await ctx.send(content, view=view)

    @fn.command(name="stats", aliases=["s", "lifetime"])
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def fn_stats(self, ctx: commands.Context, *, player: str = None):
        """Your stat card, or someone else's.

        Use the buttons to switch Lifetime/Season and input type.

        Examples:
        `[p]fn stats` (your linked account)
        `[p]fn stats @friend` (their linked account)
        `[p]fn stats SomeEpicName`
        `[p]fn stats psn SomeName` or `[p]fn stats xbox SomeName`
        """
        await self._send_stats(ctx, player, "lifetime")

    @fn.command(name="season")
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def fn_season(self, ctx: commands.Context, *, player: str = None):
        """Current-season stat card. Same options as `[p]fn stats`."""
        await self._send_stats(ctx, player, "season")

    @fn.command(name="compare", aliases=["vs"])
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def fn_compare(self, ctx: commands.Context, player_one: str, player_two: str = None):
        """Head-to-head stat comparison.

        Examples:
        `[p]fn compare @friend` (you vs them)
        `[p]fn compare @friend @other`
        `[p]fn compare "Name One" "Name Two"` (quote names with spaces)
        """
        if await self._need_key(ctx):
            return
        try:
            if player_two is None:
                a, b = await self._resolve(ctx, None), await self._resolve(ctx, player_one)
            else:
                a, b = await self._resolve(ctx, player_one), await self._resolve(ctx, player_two)
        except StatsLookupError as exc:
            return await ctx.send(str(exc))
        cache: dict = {}
        async with ctx.typing():
            content, file = await self.compare_card(a, b, "lifetime", cache)
        if file is None:
            return await ctx.send(content)
        view = CompareView(self, a, b, "lifetime", cache)
        view.message = await ctx.send(file=file, view=view)

    @fn.command(name="leaderboard", aliases=["lb", "top"])
    @commands.guild_only()
    @commands.cooldown(1, 30, commands.BucketType.guild)
    async def fn_leaderboard(self, ctx: commands.Context, stat: str = "wins", window: str = "lifetime"):
        """Rank everyone in this server who linked an account.

        Stats: `wins`, `kd`, `winrate`, `kills`, `matches`, `top10`
        Add `season` for this season only: `[p]fn lb kd season`
        """
        if await self._need_key(ctx):
            return
        stat = LB_ALIASES.get(stat.lower(), stat.lower())
        if stat in ("season", "lifetime"):
            stat, window = "wins", stat
        window = "season" if window.lower().startswith("s") else "lifetime"
        if stat not in LEADERBOARD_STATS:
            return await ctx.send(f"Pick one of: {', '.join(f'`{s}`' for s in LEADERBOARD_STATS)}.")

        members = await self.guild_links(ctx.guild)
        if not members:
            return await ctx.send(f"Nobody has linked yet. Start with `{ctx.clean_prefix}fn link <name>`.")

        rows, private, missing = [], 0, 0
        async with ctx.typing():
            for member, data in members[:LEADERBOARD_LIMIT]:
                lookup = self._lookup_from(data)
                try:
                    stats = await self.fetch_stats(lookup, window)
                except fortnite_api.Forbidden:
                    private += 1
                    continue
                except Exception:  # noqa: BLE001
                    missing += 1
                    continue
                summary = st.summarize(stats)
                if summary:
                    rows.append((member, summary))
                await asyncio.sleep(0.35)  # stay well under the API's 3 req/s

        label, key_fn, fmt = LEADERBOARD_STATS[stat]
        rows.sort(key=lambda r: key_fn(r[1]["overall"]), reverse=True)
        lines = []
        for i, (member, summary) in enumerate(rows):
            place = MEDALS[i] if i < 3 else f"`{i + 1:>2}.`"
            o = summary["overall"]
            extra = f"{o['wins']:,} W · {o['kd']:.2f} K/D" if stat not in ("wins", "kd") else f"{o['matches']:,} matches"
            lines.append(f"{place} **{member.display_name}** — {fmt(o)}\n╰ *{summary['name']}* · {extra}")
        embed = discord.Embed(
            title=f"🏆 Fortnite {label} — {WINDOW_LABELS[window]}",
            description="\n".join(lines) or "No one has matches for this yet.",
            color=0xFFD640,
        )
        notes = []
        if private:
            notes.append(f"{private} private")
        if missing:
            notes.append(f"{missing} not found")
        if len(members) > LEADERBOARD_LIMIT:
            notes.append(f"showing first {LEADERBOARD_LIMIT} linked")
        embed.set_footer(text=" · ".join(notes + [f"Link yours: {ctx.clean_prefix}fn link <name>"]))
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ #
    # Shop plumbing
    # ------------------------------------------------------------------ #
    async def shop_state(self, force: bool = False) -> ShopState:
        async with self._shop_lock:
            fresh = self._shop and time.monotonic() - self._shop.fetched < SHOP_REFRESH_SECONDS
            if fresh and not force:
                return self._shop
            client = await self._get_client()
            shop = await client.fetch_shop()
            if self._shop and shop.hash and shop.hash == self._shop.model.hash:
                self._shop.fetched = time.monotonic()
                return self._shop
            self._shop = ShopState(model=sr.build_model(shop))
            return self._shop

    async def shop_cover_png(self, state: ShopState) -> bytes:
        if state.cover is None:
            state.cover = await sr.cover_png(asyncio.get_running_loop(), state.model, state.art)
        return state.cover

    async def shop_page_png(self, state: ShopState, index: int) -> bytes:
        if index not in state.pages:
            state.pages[index] = await sr.page_png(asyncio.get_running_loop(), state.model, index, state.art)
        return state.pages[index]

    def shop_caption(self, state: ShopState) -> str:
        s = state.model.stats()
        bits = [f"{s['items']} items"]
        if s["new"]:
            bits.append(f"🆕 {s['new']} new")
        if s["sale"]:
            bits.append(f"💸 {s['sale']} on sale")
        if s["leaving"]:
            bits.append(f"⌛ {s['leaving']} leaving")
        return (
            f"## 🛒 Item Shop · {state.model.date_label}\n"
            f"{' · '.join(bits)} · resets <t:{_next_reset()}:R>\n"
            "-# Tap **Browse the shop** for every item, section by section."
        )

    async def open_browser(self, interaction: discord.Interaction, start: int = 0):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            state = await self.shop_state()
            browser = ShopBrowser(self, state, start)
            file = await browser.render()
        except Exception:  # noqa: BLE001
            log.exception("shop browser failed")
            return await interaction.followup.send("The shop is having trouble loading. Try again shortly.", ephemeral=True)
        await interaction.followup.send(browser.caption(), file=file, view=browser, ephemeral=True)

    # ------------------------------------------------------------------ #
    # Shop commands
    # ------------------------------------------------------------------ #
    @fn.command(name="shop")
    @commands.cooldown(1, 15, commands.BucketType.channel)
    async def fn_shop(self, ctx: commands.Context):
        """Today's item shop. Tap **Browse the shop** to page through every section."""
        async with ctx.typing():
            try:
                state = await self.shop_state()
                png = await self.shop_cover_png(state)
            except Exception as exc:  # noqa: BLE001
                log.exception("shop failed")
                return await ctx.send(f"Couldn't load the shop: `{type(exc).__name__}`.")
        await ctx.send(self.shop_caption(state), file=png_file(png, "item-shop"), view=self._post_view)

    @fn.command(name="shoptext")
    @commands.cooldown(1, 15, commands.BucketType.channel)
    async def fn_shoptext(self, ctx: commands.Context):
        """Today's item shop as a quick text list."""
        async with ctx.typing():
            try:
                state = await self.shop_state()
            except Exception as exc:  # noqa: BLE001
                return await ctx.send(f"Couldn't load the shop: `{type(exc).__name__}`.")
        lines = [f"# 🛒 Item Shop · {state.model.date_label}"]
        for name, cards in state.model.sections:
            if name == sr.JAM_SECTION:
                lines.append(f"\n**🎵 Jam Tracks** · {len(cards)} songs")
                continue
            lines.append(f"\n**{name}**")
            for c in cards:
                tags = "".join((" 🆕" if c.new else "", " 💸" if c.on_sale else "", " ⌛" if c.last_day else ""))
                lines.append(f"• {c.name} — {c.price:,} V{tags}" if c.price is not None else f"• {c.name}{tags}")
        lines.append("\n-# 🆕 new · 💸 on sale · ⌛ last day")
        for page in pagify("\n".join(lines), page_length=1900, delims=["\n\n", "\n"]):
            await ctx.send(page)

    @fn.command(name="find", aliases=["item", "cosmetic", "search"])
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def fn_find(self, ctx: commands.Context, *, item: str):
        """Look up an item: is it in today's shop, and when was it last seen?"""
        async with ctx.typing():
            client = await self._get_client()
            try:
                state = await self.shop_state()
            except Exception:  # noqa: BLE001
                state = None
            stocked = state.model.cards if state else []
            in_shop = state.model.find(item) if state else []
            try:
                results = await client.search_br_cosmetics(
                    multiple=True, name=item, match_method=MatchMethod.CONTAINS,
                    response_flags=ResponseFlags.INCLUDE_SHOP_HISTORY,
                )
            except fortnite_api.NotFound:
                results = []
            except Exception:  # noqa: BLE001
                log.exception("cosmetic search failed")
                results = []

        q = item.lower().strip()

        def stocked_card(cosmetic_id):
            # Prefer the item's own listing over a bundle that happens to contain it.
            hits = [c for c in stocked if cosmetic_id in c.ids]
            return min(hits, key=lambda c: len(c.ids)) if hits else None

        cosmetic = next((c for c in results if c.name and c.name.lower() == q), None)
        exact_card = next((c for c in in_shop if c.name.lower() == q), None)
        if cosmetic is not None:
            card = stocked_card(cosmetic.id)
        elif exact_card is not None:
            # e.g. a Jam Track or car: exact shop match beats a partial cosmetic match
            card = exact_card
            cosmetic = next((c for c in results if c.id in exact_card.ids), None)
        else:
            cosmetic = min(results, key=lambda c: len(c.name or "x" * 99)) if results else None
            card = stocked_card(cosmetic.id) if cosmetic is not None else (in_shop[0] if in_shop else None)

        if cosmetic is None and card is None:
            return await ctx.send(f"No item found matching **{item}**.")

        embed = self._item_embed(cosmetic, card, await ctx.embed_color())
        view = WishlistButton(self, cosmetic.id, cosmetic.name) if cosmetic is not None else None
        await ctx.send(embed=embed, view=view)

    def _item_embed(self, cosmetic, card, default_color) -> discord.Embed:
        name = cosmetic.name if cosmetic is not None else card.name
        color = default_color
        if cosmetic is not None:
            color = RARITY_COLORS.get(sr._rarity_key(cosmetic) or "", default_color)
        embed = discord.Embed(title=name, color=color)
        if cosmetic is not None:
            if cosmetic.description:
                embed.description = f"*{cosmetic.description}*"
            type_ = getattr(cosmetic.type, "display_value", None) if cosmetic.type else None
            rarity = getattr(cosmetic.rarity, "display_value", None) if cosmetic.rarity else None
            series = getattr(cosmetic.series, "value", None) if cosmetic.series else None
            if type_:
                embed.add_field(name="Type", value=type_)
            if series or rarity:
                embed.add_field(name="Rarity", value=series or rarity)
            if cosmetic.set and getattr(cosmetic.set, "value", None):
                embed.add_field(name="Set", value=cosmetic.set.value)
            intro = getattr(cosmetic.introduction, "text", None) if cosmetic.introduction else None
            if intro:
                embed.add_field(name="Introduced", value=intro, inline=False)
            images = cosmetic.images
            pic = (images.featured or images.icon) if images else None
            if pic:
                embed.set_thumbnail(url=pic.url)

        if card is not None:
            price = f"**{card.price:,} V-Bucks**" if card.price is not None else "in the shop"
            if card.on_sale:
                price += f" ~~{card.regular:,}~~"
            leaves = f" · leaves <t:{int(card.out_date.timestamp())}:R>" if card.out_date else ""
            embed.add_field(name="🛒 In today's shop!", value=f"{price}{leaves}\nSection: {card.section}", inline=False)
            if cosmetic is None and card.art_url:
                embed.set_thumbnail(url=card.art_url)
        elif cosmetic is not None:
            history = sorted(getattr(cosmetic, "shop_history", None) or [])
            if history:
                last = history[-1]
                embed.add_field(
                    name="📅 Last in the shop",
                    value=f"<t:{int(last.timestamp())}:D> (<t:{int(last.timestamp())}:R>) · sold {len(history)} time{'s' if len(history) != 1 else ''}",
                    inline=False,
                )
            else:
                embed.add_field(name="📅 Shop history", value="Never sold in the item shop (pass or reward item).", inline=False)
        embed.set_footer(text="⭐ Add it to your wishlist to get pinged when it's back")
        return embed

    # ------------------------------------------------------------------ #
    # Wishlist
    # ------------------------------------------------------------------ #
    async def wishlist_add(self, user: discord.abc.User, item_id: str, item_name: str) -> str:
        async with self.config.user(user).wishlist() as wl:
            if any(w["id"] == item_id for w in wl):
                return f"**{item_name}** is already on your wishlist."
            if len(wl) >= WISHLIST_LIMIT:
                return f"Your wishlist is full ({WISHLIST_LIMIT} items). Remove something first."
            wl.append({"id": item_id, "name": item_name})
        return f"⭐ Added **{item_name}**. I'll ping you in the daily shop post when it's in stock."

    @fn.group(name="wishlist", aliases=["wl", "wish"], invoke_without_command=True, fallback="show")
    async def fn_wishlist(self, ctx: commands.Context):
        """Your wishlist. You get pinged in the daily shop post when an item is in stock."""
        wl = await self.config.user(ctx.author).wishlist()
        if not wl:
            return await ctx.send(
                f"Your wishlist is empty. Add items with `{ctx.clean_prefix}fn wishlist add <item>` "
                f"or the ⭐ button on `{ctx.clean_prefix}fn find`."
            )
        in_stock = set()
        try:
            state = await self.shop_state()
            stocked_ids = {i for c in state.model.cards for i in c.ids}
            in_stock = {w["id"] for w in wl if w["id"] in stocked_ids}
        except Exception:  # noqa: BLE001
            pass
        lines = [f"{'🛒' if w['id'] in in_stock else '⭐'} {w['name']}" for w in wl]
        embed = discord.Embed(title=f"⭐ {ctx.author.display_name}'s wishlist", description="\n".join(lines), color=0xFFD640)
        embed.set_footer(text=f"{len(wl)}/{WISHLIST_LIMIT} · 🛒 = in the shop today")
        await ctx.send(embed=embed)

    @fn_wishlist.command(name="add")
    async def fn_wishlist_add(self, ctx: commands.Context, *, item: str):
        """Add an item to your wishlist."""
        client = await self._get_client()
        try:
            results = await client.search_br_cosmetics(multiple=True, name=item, match_method=MatchMethod.CONTAINS)
        except fortnite_api.NotFound:
            results = []
        if not results:
            return await ctx.send(f"No item found matching **{item}**.")
        q = item.lower().strip()
        pick = next((c for c in results if c.name and c.name.lower() == q), None) or min(
            results, key=lambda c: len(c.name or "x" * 99)
        )
        await ctx.send(await self.wishlist_add(ctx.author, pick.id, pick.name))

    @fn_wishlist.command(name="remove", aliases=["del", "delete"])
    async def fn_wishlist_remove(self, ctx: commands.Context, *, item: str):
        """Remove an item from your wishlist."""
        q = item.lower().strip()
        async with self.config.user(ctx.author).wishlist() as wl:
            match = next((w for w in wl if w["name"].lower() == q), None) or next(
                (w for w in wl if q in w["name"].lower()), None
            )
            if match:
                wl.remove(match)
        await ctx.send(f"Removed **{match['name']}**." if match else f"**{item}** isn't on your wishlist.")

    @fn_wishlist.command(name="clear")
    async def fn_wishlist_clear(self, ctx: commands.Context):
        """Empty your wishlist."""
        await self.config.user(ctx.author).wishlist.clear()
        await ctx.send("Wishlist cleared.")

    # ------------------------------------------------------------------ #
    # News
    # ------------------------------------------------------------------ #
    @fn.command(name="news")
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def fn_news(self, ctx: commands.Context, mode: str = "br"):
        """Current in-game news. `mode` can be `br` or `stw`."""
        client = await self._get_client()
        async with ctx.typing():
            try:
                news = await client.fetch_news()
            except Exception as exc:  # noqa: BLE001
                log.exception("news fetch failed")
                return await ctx.send(f"News fetch failed: `{type(exc).__name__}`.")
        section = news.stw if mode.lower() == "stw" else news.br
        if not section or not section.image:
            return await ctx.send("No news right now.")
        embed = discord.Embed(title=f"📰 Fortnite {'Save the World' if mode.lower() == 'stw' else 'Battle Royale'} News",
                              color=await ctx.embed_color())
        embed.set_image(url=section.image.url)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------ #
    # Daily auto-shop
    # ------------------------------------------------------------------ #
    async def _wishlist_pings(self, guild: discord.Guild, state: ShopState) -> list[str]:
        by_id: dict[str, sr.ShopCard] = {}
        for card in state.model.cards:
            for i in card.ids:
                # prefer the item's own listing over a bundle that contains it
                if i not in by_id or len(card.ids) < len(by_id[i].ids):
                    by_id[i] = card
        lines = []
        for user_id, data in (await self.config.all_users()).items():
            member = guild.get_member(user_id)
            if member is None:
                continue
            hits = [by_id[w["id"]] for w in data.get("wishlist", []) if w["id"] in by_id]
            if hits:
                items = ", ".join(f"**{c.name}** ({c.price:,} V)" if c.price is not None else f"**{c.name}**" for c in hits)
                lines.append(f"{member.mention} {items}")
        return lines

    async def _post_shop(self, guild: discord.Guild, channel: discord.TextChannel, state: ShopState) -> bool:
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.attach_files):
            return False
        try:
            png = await self.shop_cover_png(state)
            await channel.send(self.shop_caption(state), file=png_file(png, "item-shop"), view=self._post_view)
            pings = await self._wishlist_pings(guild, state)
            if pings:
                for page in pagify("## ⭐ Wishlist alert!\n" + "\n".join(pings), page_length=1900):
                    await channel.send(page, allowed_mentions=discord.AllowedMentions(users=True))
            await self.config.guild(guild).last_shop_hash.set(state.model.hash)
            return True
        except discord.HTTPException:
            log.warning("Failed to post shop in %s/%s", guild.id, channel.id, exc_info=True)
            return False

    @tasks.loop(time=SHOP_POST_TIME)
    async def auto_shop(self):
        await self.bot.wait_until_red_ready()
        all_guilds = await self.config.all_guilds()
        if not any(d.get("shop_channel") for d in all_guilds.values()):
            return
        state = None
        for attempt in range(6):  # the API can lag the reset by a few minutes
            try:
                state = await self.shop_state(force=True)
            except Exception:  # noqa: BLE001
                log.exception("auto-shop fetch failed")
                state = None
            if state and state.model.date and state.model.date.date() >= dt.datetime.now(dt.timezone.utc).date():
                break
            await asyncio.sleep(120)
        if state is None:
            return
        for guild_id, data in all_guilds.items():
            channel_id = data.get("shop_channel")
            guild = self.bot.get_guild(guild_id)
            channel = guild.get_channel(channel_id) if guild and channel_id else None
            if channel is None or data.get("last_shop_hash") == state.model.hash:
                continue
            await self._post_shop(guild, channel, state)
