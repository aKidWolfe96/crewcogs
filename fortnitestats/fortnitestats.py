"""
FortniteStats — a self-hosted Fortnite cog for Red-DiscordBot.

Powered by the official fortnite-api.com Python library (https://fortnite-api.com).
Covers the clean, ToS-safe surface: graphical player stats, the daily item shop
(with optional auto-posting), in-game news, and cosmetic lookups.

Deliberately does NOT implement account login / locker valuation. That feature
requires holding an Epic OAuth token and hitting undocumented endpoints with real
account credentials, which is against Epic's ToS. Everything in here uses only
public data through a documented API.

Setup:
    1. pip install fortnite-api   (or let Red install it via info.json requirements)
    2. Grab a free API key at https://dash.fortnite-api.com  (Discord login)
       -- only the stats endpoint needs it; shop/news/cosmetics work without one.
    3. [p]fnset apikey <key>      (owner only, stored globally)
    4. [p]fnset shopchannel #ch   (per-guild, enables the daily auto-shop)
"""

import io
import logging
from datetime import time, timezone

import discord
from discord.ext import tasks
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, humanize_number

import fortnite_api
from fortnite_api import StatsImageType, TimeWindow

from .shoprender import render_shop_image

log = logging.getLogger("red.fortnitestats")

# Item shop rotates at 00:00 UTC. Post a few minutes after so the API has refreshed.
SHOP_POST_TIME = time(hour=0, minute=5, tzinfo=timezone.utc)

RARITY_COLORS = {
    "common": 0xB1B1B1,
    "uncommon": 0x60B932,
    "rare": 0x3675D6,
    "epic": 0xB13AF0,
    "legendary": 0xE2843C,
    "mythic": 0xE5C13B,
    "icon_series": 0x1FC9C3,
    "marvel": 0xC53D3E,
    "dc": 0x4F5DD6,
    "starwars": 0x1A1A1A,
    "gaming_legends": 0x5C2D91,
    "slurp": 0x2FD9D2,
    "lava": 0xD45A22,
    "frozen": 0x6FCBE3,
    "shadow": 0x3A3A3A,
    "dark": 0x8B2FB0,
}


class FortniteStats(commands.Cog):
    """Graphical Fortnite stats, item shop, news and cosmetics."""

    __version__ = "1.5.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=874203991, force_registration=True)
        self.config.register_global(api_key=None)
        self.config.register_guild(shop_channel=None)
        self.config.register_member(epic_name=None)
        self._client: fortnite_api.Client | None = None
        self._cosmetic_cache: list | None = None
        # rendered shop PNG cached by shop hash → bytes (keep only the latest day)
        self._shop_image_cache: dict[str, bytes] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def cog_load(self):
        self.auto_shop.start()

    async def cog_unload(self):
        self.auto_shop.cancel()
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None

    async def _get_client(self) -> fortnite_api.Client:
        """Lazily build (and reuse) one async client for the cog's lifetime."""
        if self._client is None:
            api_key = await self.config.api_key()
            self._client = fortnite_api.Client(api_key=api_key)
            await self._client.__aenter__()
        return self._client

    def format_help_for_context(self, ctx):
        pre = super().format_help_for_context(ctx)
        return f"{pre}\n\nCog Version: {self.__version__}"

    # ------------------------------------------------------------------ #
    # Owner / admin config
    # ------------------------------------------------------------------ #
    @commands.group(name="fnset")
    async def fnset(self, ctx: commands.Context):
        """Configure the FortniteStats cog."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @fnset.command(name="apikey")
    @commands.is_owner()
    async def fnset_apikey(self, ctx: commands.Context, *, key: str):
        """Set the fortnite-api.com API key (required for player stats).

        Get one free at https://dash.fortnite-api.com — log in with Discord.
        Run this in DM to keep the key out of your channel history.
        """
        await self.config.api_key.set(key)
        # Rebuild the client so it picks up the new key.
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
            self._client = None
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        await ctx.send("API key set. (Your message was removed if I had permission.)")

    @fnset.command(name="shopchannel")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def fnset_shopchannel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Set (or clear) the channel for the daily auto-shop post.

        Omit the channel to disable auto-posting in this server.
        """
        if channel is None:
            await self.config.guild(ctx.guild).shop_channel.clear()
            return await ctx.send("Daily auto-shop disabled.")
        await self.config.guild(ctx.guild).shop_channel.set(channel.id)
        await ctx.send(f"Daily item shop will post in {channel.mention} at ~00:05 UTC.")

    @fnset.command(name="settings")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def fnset_settings(self, ctx: commands.Context):
        """Show current settings for this server."""
        has_key = bool(await self.config.api_key())
        ch_id = await self.config.guild(ctx.guild).shop_channel()
        ch = ctx.guild.get_channel(ch_id) if ch_id else None
        await ctx.send(
            box(
                f"API key set : {'yes' if has_key else 'no'}\n"
                f"Auto-shop    : {ch.mention if ch else 'disabled'}",
                lang="ini",
            )
        )

    # ------------------------------------------------------------------ #
    # Player-facing commands
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(name="fn")
    async def fn(self, ctx: commands.Context):
        """Fortnite stats, shop, news and cosmetics."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @fn.command(name="link")
    async def fn_link(self, ctx: commands.Context, *, epic_name: str):
        """Link your Epic display name so `[p]fn stats` works with no argument."""
        await self.config.member(ctx.author).epic_name.set(epic_name)
        await ctx.send(f"Linked you to Epic account **{epic_name}**.")

    @fn.command(name="unlink")
    async def fn_unlink(self, ctx: commands.Context):
        """Remove your linked Epic display name."""
        await self.config.member(ctx.author).epic_name.clear()
        await ctx.send("Unlinked.")

    @fn.command(name="stats")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def fn_stats(self, ctx: commands.Context, *, name: str = None):
        """Graphical lifetime Battle Royale stats. Uses your linked name if omitted."""
        await self._send_stats(ctx, name, TimeWindow.LIFETIME)

    @fn.command(name="season")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def fn_season(self, ctx: commands.Context, *, name: str = None):
        """Graphical current-season Battle Royale stats. Uses your linked name if omitted."""
        await self._send_stats(ctx, name, TimeWindow.SEASON)

    async def _send_stats(self, ctx: commands.Context, name, window):
        """Shared stats fetch + render for the stats/season commands."""
        if not await self.config.api_key():
            return await ctx.send(
                "No API key configured. The bot owner needs to run "
                f"`{ctx.clean_prefix}fnset apikey <key>` first "
                "(free at https://dash.fortnite-api.com)."
            )

        if name is None:
            name = await self.config.member(ctx.author).epic_name()
        if not name:
            return await ctx.send(
                f"Give me an Epic name, or link yours with `{ctx.clean_prefix}fn link <name>`."
            )

        is_season = window == TimeWindow.SEASON
        client = await self._get_client()

        async with ctx.typing():
            try:
                stats = await client.fetch_br_stats(
                    name=name, time_window=window, image=StatsImageType.ALL
                )
            except fortnite_api.NotFound:
                return await ctx.send(
                    f"No account found for **{name}**, or its stats are set to private."
                )
            except fortnite_api.Forbidden:
                return await ctx.send("That account has its stats set to private.")
            except Exception as exc:  # noqa: BLE001
                log.exception("stats lookup failed")
                return await ctx.send(f"Lookup failed: `{type(exc).__name__}`.")

        # The API renders the stat card for us — just hand back the image.
        if stats.image and stats.image.url:
            embed = discord.Embed(
                title=f"{stats.user.name} — {'Season' if is_season else 'Lifetime'} Stats",
                color=await ctx.embed_color(),
            )
            embed.set_image(url=stats.image.url)
            if stats.battle_pass:
                embed.set_footer(text=f"Battle Pass Level {stats.battle_pass.level}")
            return await ctx.send(embed=embed)

        # Fallback to a text embed if image generation was unavailable.
        overall = stats.inputs.all.overall if stats.inputs and stats.inputs.all else None
        if not overall:
            return await ctx.send("No stats available for that account.")
        embed = discord.Embed(title=f"{stats.user.name}", color=await ctx.embed_color())
        embed.add_field(name="Wins", value=humanize_number(overall.wins))
        embed.add_field(name="K/D", value=f"{overall.kd:.2f}")
        embed.add_field(name="Win %", value=f"{overall.win_rate:.1f}%")
        embed.add_field(name="Kills", value=humanize_number(overall.kills))
        embed.add_field(name="Matches", value=humanize_number(overall.matches))
        embed.add_field(name="Top 10", value=humanize_number(overall.top10))
        await ctx.send(embed=embed)

    @fn.command(name="shop")
    @commands.cooldown(1, 10, commands.BucketType.guild)
    async def fn_shop(self, ctx: commands.Context):
        """Show today's item shop as a rendered image."""
        client = await self._get_client()
        async with ctx.typing():
            try:
                shop = await client.fetch_shop()
            except Exception as exc:  # noqa: BLE001
                log.exception("shop fetch failed")
                return await ctx.send(f"Shop fetch failed: `{type(exc).__name__}`.")

            png = await self._get_shop_image(shop)

        if png is not None:
            date = shop.date.strftime("%Y-%m-%d") if shop.date else "today"
            return await ctx.send(file=discord.File(io.BytesIO(png), filename=f"shop-{date}.png"))
        # Renderer failed — fall back to the text embed.
        await ctx.send(embed=self._build_shop_embed(shop, await ctx.embed_color()))

    @fn.command(name="shoptext")
    @commands.cooldown(1, 10, commands.BucketType.guild)
    async def fn_shoptext(self, ctx: commands.Context):
        """Show today's item shop as a text list (lighter than the image)."""
        client = await self._get_client()
        async with ctx.typing():
            try:
                shop = await client.fetch_shop()
            except Exception as exc:  # noqa: BLE001
                log.exception("shop fetch failed")
                return await ctx.send(f"Shop fetch failed: `{type(exc).__name__}`.")
        await ctx.send(embed=self._build_shop_embed(shop, await ctx.embed_color()))

    @fn.command(name="news")
    @commands.cooldown(1, 10, commands.BucketType.guild)
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
            return await ctx.send("No news image available right now.")
        embed = discord.Embed(
            title=f"Fortnite {mode.upper()} News", color=await ctx.embed_color()
        )
        embed.set_image(url=section.image.url)
        await ctx.send(embed=embed)

    @fn.command(name="cosmetic")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def fn_cosmetic(self, ctx: commands.Context, *, query: str):
        """Look up a Battle Royale cosmetic by name."""
        client = await self._get_client()
        async with ctx.typing():
            if self._cosmetic_cache is None:
                try:
                    self._cosmetic_cache = await client.fetch_cosmetics_br()
                except Exception as exc:  # noqa: BLE001
                    log.exception("cosmetic fetch failed")
                    return await ctx.send(f"Cosmetic fetch failed: `{type(exc).__name__}`.")

        q = query.lower()
        match = next(
            (c for c in self._cosmetic_cache if c.name and c.name.lower() == q), None
        ) or next(
            (c for c in self._cosmetic_cache if c.name and q in c.name.lower()), None
        )
        if match is None:
            return await ctx.send(f"No cosmetic found matching **{query}**.")

        rarity_enum = match.rarity
        rarity_key = rarity_enum.value.lower() if rarity_enum else ""
        embed = discord.Embed(
            title=match.name,
            description=match.description or "",
            color=RARITY_COLORS.get(rarity_key, await ctx.embed_color()),
        )
        if match.type:
            embed.add_field(name="Type", value=self._pretty_enum(match.type.value))
        if rarity_enum:
            embed.add_field(name="Rarity", value=self._pretty_enum(rarity_enum.value))
        if match.set and getattr(match.set, "value", None):
            embed.add_field(name="Set", value=match.set.value)
        intro_text = getattr(match.introduction, "text", None) if match.introduction else None
        if intro_text:
            embed.add_field(name="Introduced", value=intro_text)
        icon = None
        if match.images:
            icon = match.images.featured or match.images.icon
        if icon:
            embed.set_thumbnail(url=icon.url)
        await ctx.send(embed=embed)

    @staticmethod
    def _pretty_enum(value: str) -> str:
        """'icon_series' -> 'Icon Series'."""
        return value.replace("_", " ").title() if value else "—"

    # ------------------------------------------------------------------ #
    # Daily auto-shop loop
    # ------------------------------------------------------------------ #
    @tasks.loop(time=SHOP_POST_TIME)
    async def auto_shop(self):
        await self.bot.wait_until_red_ready()
        self._cosmetic_cache = None  # shop rotated; invalidate cosmetic cache too
        try:
            client = await self._get_client()
            shop = await client.fetch_shop()
        except Exception:  # noqa: BLE001
            log.exception("auto-shop fetch failed")
            return

        all_guilds = await self.config.all_guilds()
        if not all_guilds:
            return

        png = await self._get_shop_image(shop)
        date = shop.date.strftime("%Y-%m-%d") if shop.date else "today"
        embed_fallback = self._build_shop_embed(shop, discord.Color.blurple())

        for guild_id, data in all_guilds.items():
            channel_id = data.get("shop_channel")
            if not channel_id:
                continue
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            channel = guild.get_channel(channel_id)
            if channel is None:
                continue
            perms = channel.permissions_for(guild.me)
            if not (perms.send_messages and perms.embed_links):
                continue
            try:
                if png is not None and perms.attach_files:
                    await channel.send(
                        file=discord.File(io.BytesIO(png), filename=f"shop-{date}.png")
                    )
                else:
                    color = await self.bot.get_embed_color(channel)
                    embed_fallback.color = color
                    await channel.send(embed=embed_fallback)
            except discord.HTTPException:
                log.warning("Failed to post auto-shop in %s/%s", guild_id, channel_id)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _get_shop_image(self, shop) -> bytes | None:
        """Render (or return cached) shop PNG bytes. Cached per shop hash."""
        key = shop.hash or (shop.date.isoformat() if shop.date else "current")
        if key in self._shop_image_cache:
            return self._shop_image_cache[key]
        try:
            png = await render_shop_image(self.bot.loop, shop)
        except Exception:  # noqa: BLE001
            log.exception("shop render failed")
            return None
        if png:
            # keep only the latest day's render in memory
            self._shop_image_cache = {key: png}
        return png

    def _build_shop_embed(self, shop, color) -> discord.Embed:
        date = shop.date.strftime("%B %d, %Y") if shop.date else "today"
        embed = discord.Embed(
            title=f"Fortnite Item Shop — {date}",
            color=color,
            url="https://fortnite-api.com",
        )

        # Group entries by their layout/section name.
        sections: dict[str, list] = {}
        for entry in shop.entries:
            if entry.bundle:  # skip raw bundle duplicates for a cleaner list
                continue
            section = entry.layout.name if entry.layout and entry.layout.name else "Featured"
            sections.setdefault(section, [])
            names = [c.name for c in (entry.br or []) if c.name]
            label = names[0] if names else (entry.dev_name or "Unknown")
            price = entry.final_price
            sections[section].append((label, price))

        shown = 0
        for section, items in list(sections.items())[:8]:
            lines = []
            for label, price in items[:8]:
                lines.append(f"• {label} — {humanize_number(price)} V")
                shown += 1
            if lines:
                embed.add_field(name=section, value="\n".join(lines), inline=False)

        total = sum(len(v) for v in sections.values())
        embed.set_footer(
            text=f"Showing {shown} of {total} offers · prices in V-Bucks · data via fortnite-api.com"
        )
        return embed
