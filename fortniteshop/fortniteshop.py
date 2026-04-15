"""
FortniteShop – Red-DiscordBot cog
Fetches the daily Fortnite item shop and posts it to a configured channel
when the shop resets at 00:00 UTC (midnight).

API used: https://fortnite-api.com  (free, no key required)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

log = logging.getLogger("red.FortniteShop")

# ── Fortnite API endpoint ──────────────────────────────────────────────────────
SHOP_URL = "https://fortnite-api.com/v2/shop"

# Rarity → embed colour mapping
RARITY_COLOURS = {
    "common":    0x9D9D9D,
    "uncommon":  0x1ECA25,
    "rare":      0x2097F3,
    "epic":      0xB045F3,
    "legendary": 0xF3A21A,
    "mythic":    0xF7E63E,
    "icon":      0x18D3E6,
    "marvel":    0xED1D24,
    "dc":        0x0075F5,
    "gaming legends": 0x7B2FBE,
    "shadow":    0x454545,
    "slurp":     0x00FFFF,
    "dark":      0x8000FF,
    "frozen":    0xADD8E6,
    "lava":      0xFF4500,
    "star wars": 0xFFE81F,
}

# ── Cog ───────────────────────────────────────────────────────────────────────
class FortniteShop(commands.Cog):
    """Daily Fortnite item-shop announcements for Red-DiscordBot."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0x464E5348, force_registration=True
        )
        # Per-guild defaults
        self.config.register_guild(
            channel_id=None,      # channel to post in
            role_ping=None,       # optional role ID to ping
            show_prices=True,     # show V-Buck prices under each item
            max_items=20,         # cap how many items appear in the embed
        )
        self._tasks: dict[int, asyncio.Task] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        """Start the daily loop for every guild that has a channel set."""
        all_guilds = await self.config.all_guilds()
        for guild_id, data in all_guilds.items():
            if data.get("channel_id"):
                self._ensure_loop(guild_id)

    async def cog_unload(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _ensure_loop(self, guild_id: int) -> None:
        """Create (or replace) the daily-post task for a guild."""
        existing = self._tasks.get(guild_id)
        if existing and not existing.done():
            return
        self._tasks[guild_id] = self.bot.loop.create_task(
            self._daily_loop(guild_id), name=f"fnshop-{guild_id}"
        )

    def _cancel_loop(self, guild_id: int) -> None:
        task = self._tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

    @staticmethod
    async def _fetch_shop() -> Optional[dict]:
        """Pull the current shop from fortnite-api.com. Returns the JSON dict or None."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(SHOP_URL, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status != 200:
                        log.warning("Fortnite API returned HTTP %s", r.status)
                        return None
                    return await r.json()
        except Exception as exc:
            log.error("Error fetching Fortnite shop: %s", exc)
            return None

    @staticmethod
    def _seconds_until_reset() -> float:
        """Seconds until the next midnight UTC (when the Fortnite shop resets)."""
        now = datetime.now(tz=timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return (tomorrow - now).total_seconds()

    # ── Daily loop ─────────────────────────────────────────────────────────────

    async def _daily_loop(self, guild_id: int) -> None:
        await self.bot.wait_until_ready()
        while True:
            sleep_secs = self._seconds_until_reset()
            log.debug(
                "Guild %s: next shop post in %.0f s (%.1f h)",
                guild_id, sleep_secs, sleep_secs / 3600,
            )
            await asyncio.sleep(sleep_secs)

            try:
                await self._post_shop(guild_id)
            except Exception as exc:
                log.error("Guild %s: failed to post shop – %s", guild_id, exc)

    async def _post_shop(self, guild_id: int, channel: discord.TextChannel = None) -> bool:
        """
        Fetch the shop and post embeds in the configured channel.
        Pass ``channel`` to override the config (used by the manual command).
        Returns True on success.
        """
        cfg = await self.config.guild_from_id(guild_id).all()

        if channel is None:
            ch_id = cfg["channel_id"]
            if not ch_id:
                return False
            channel = self.bot.get_channel(ch_id)
            if channel is None:
                log.warning("Guild %s: shop channel %s not found", guild_id, ch_id)
                return False

        data = await self._fetch_shop()
        if not data or data.get("status") != 200:
            await channel.send("⚠️ Could not fetch the Fortnite item shop right now. Try again later.")
            return False

        shop = data["data"]
        all_entries = shop.get("featured", {}).get("entries", []) + \
                      shop.get("daily", {}).get("entries", [])

        if not all_entries:
            await channel.send("The Fortnite shop appears to be empty right now.")
            return False

        show_prices = cfg["show_prices"]
        max_items   = cfg["max_items"]
        role_id     = cfg["role_ping"]

        # ── Header embed ───────────────────────────────────────────────────────
        date_str = datetime.now(tz=timezone.utc).strftime("%B %d, %Y")
        header = discord.Embed(
            title="🛒  Fortnite Item Shop",
            description=f"**{date_str}**  •  Shop has reset!\n\n"
                        f"Showing {min(len(all_entries), max_items)} of {len(all_entries)} items.",
            colour=0x00D4FF,
        )
        header.set_thumbnail(
            url="https://fortnite-api.com/images/cosmetics/br/cid_a_272_athena_commando_f_prime/smallicon.png"
        )
        header.set_footer(text="Prices in V-Bucks  •  fortnite-api.com")

        ping_content = f"<@&{role_id}>" if role_id else None
        await channel.send(content=ping_content, embed=header)

        # ── One embed per item (up to max_items) ──────────────────────────────
        for entry in all_entries[:max_items]:
            items = entry.get("items", [])
            if not items:
                continue
            item = items[0]

            name     = item.get("name", "Unknown Item")
            rarity   = (item.get("rarity", {}).get("value") or "common").lower()
            colour   = RARITY_COLOURS.get(rarity, 0x2097F3)
            final_price = entry.get("finalPrice", 0)
            regular_price = entry.get("regularPrice", final_price)

            em = discord.Embed(title=name, colour=colour)

            # Description / type
            desc_parts = []
            item_type = item.get("type", {}).get("displayValue", "")
            if item_type:
                desc_parts.append(f"*{item_type}*")
            desc_text = item.get("description", "")
            if desc_text:
                desc_parts.append(desc_text)
            if desc_parts:
                em.description = "\n".join(desc_parts)

            # Rarity field
            rarity_display = item.get("rarity", {}).get("displayValue", rarity.title())
            em.add_field(name="Rarity", value=rarity_display, inline=True)

            # Price field
            if show_prices:
                if regular_price and regular_price != final_price:
                    price_str = f"~~{regular_price:,}~~ **{final_price:,}** V-Bucks"
                else:
                    price_str = f"**{final_price:,}** V-Bucks"
                em.add_field(name="Price", value=price_str, inline=True)

            # Bundle field
            if len(items) > 1:
                bundle_name = entry.get("bundle", {}).get("name", "Bundle")
                em.add_field(
                    name="Bundle",
                    value=f"*{bundle_name}* — {len(items)} items",
                    inline=True,
                )

            # Thumbnail (item icon)
            images = item.get("images", {})
            icon = images.get("smallIcon") or images.get("icon")
            if icon:
                em.set_thumbnail(url=icon)

            await channel.send(embed=em)
            await asyncio.sleep(0.3)   # stay well under rate limits

        return True

    # ── Commands ───────────────────────────────────────────────────────────────

    @commands.group(name="fnshop", invoke_without_command=True)
    @commands.guild_only()
    async def fnshop(self, ctx: commands.Context) -> None:
        """Fortnite daily item-shop commands."""
        await ctx.send_help()

    # -- Setup group -----------------------------------------------------------

    @fnshop.group(name="set")
    @commands.admin_or_permissions(manage_guild=True)
    async def fnshop_set(self, ctx: commands.Context) -> None:
        """Configure the FortniteShop cog."""

    @fnshop_set.command(name="channel")
    async def fnshop_set_channel(
        self,
        ctx: commands.Context,
        channel: discord.TextChannel,
    ) -> None:
        """Set the channel where daily shop updates will be posted."""
        await self.config.guild(ctx.guild).channel_id.set(channel.id)
        self._ensure_loop(ctx.guild.id)
        await ctx.send(
            f"✅ Shop updates will be posted in {channel.mention} every day at midnight UTC."
        )

    @fnshop_set.command(name="ping")
    async def fnshop_set_ping(
        self,
        ctx: commands.Context,
        role: Optional[discord.Role] = None,
    ) -> None:
        """Set a role to ping with each shop update. Leave blank to disable."""
        if role:
            await self.config.guild(ctx.guild).role_ping.set(role.id)
            await ctx.send(f"✅ Will ping {role.mention} with each shop update.")
        else:
            await self.config.guild(ctx.guild).role_ping.set(None)
            await ctx.send("✅ Role ping disabled.")

    @fnshop_set.command(name="prices")
    async def fnshop_set_prices(self, ctx: commands.Context, enabled: bool) -> None:
        """Toggle whether V-Buck prices are shown. (True/False)"""
        await self.config.guild(ctx.guild).show_prices.set(enabled)
        state = "shown" if enabled else "hidden"
        await ctx.send(f"✅ V-Buck prices will be **{state}** in shop embeds.")

    @fnshop_set.command(name="maxitems")
    async def fnshop_set_maxitems(self, ctx: commands.Context, amount: int) -> None:
        """Set how many items to show per update (1–40)."""
        amount = max(1, min(40, amount))
        await self.config.guild(ctx.guild).max_items.set(amount)
        await ctx.send(f"✅ Will show up to **{amount}** items per shop update.")

    @fnshop_set.command(name="disable")
    async def fnshop_set_disable(self, ctx: commands.Context) -> None:
        """Stop posting daily shop updates in this server."""
        await self.config.guild(ctx.guild).channel_id.set(None)
        self._cancel_loop(ctx.guild.id)
        await ctx.send("✅ Daily shop updates disabled.")

    # -- Info / manual post ----------------------------------------------------

    @fnshop.command(name="settings")
    @commands.admin_or_permissions(manage_guild=True)
    async def fnshop_settings(self, ctx: commands.Context) -> None:
        """Show the current FortniteShop settings for this server."""
        cfg = await self.config.guild(ctx.guild).all()
        ch = ctx.guild.get_channel(cfg["channel_id"]) if cfg["channel_id"] else None
        role = ctx.guild.get_role(cfg["role_ping"]) if cfg["role_ping"] else None

        secs = self._seconds_until_reset()
        hours, rem = divmod(int(secs), 3600)
        mins = rem // 60

        em = discord.Embed(title="FortniteShop Settings", colour=0x00D4FF)
        em.add_field(name="Channel",    value=ch.mention if ch else "Not set",       inline=False)
        em.add_field(name="Role ping",  value=role.mention if role else "Disabled",  inline=True)
        em.add_field(name="Show prices",value="Yes" if cfg["show_prices"] else "No", inline=True)
        em.add_field(name="Max items",  value=str(cfg["max_items"]),                 inline=True)
        em.add_field(
            name="Next reset",
            value=f"≈ {hours}h {mins}m  (midnight UTC)",
            inline=False,
        )
        await ctx.send(embed=em)

    @fnshop.command(name="now")
    @commands.admin_or_permissions(manage_guild=True)
    async def fnshop_now(
        self,
        ctx: commands.Context,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        """
        Manually post the current Fortnite shop right now.
        Defaults to the configured shop channel; pass a channel to override.
        """
        target = channel or (
            ctx.guild.get_channel(
                await self.config.guild(ctx.guild).channel_id()
            )
        )
        if target is None:
            await ctx.send(
                "❌ No shop channel set. Use `fnshop set channel #channel` first, "
                "or pass a channel to this command."
            )
            return

        async with ctx.typing():
            success = await self._post_shop(ctx.guild.id, channel=target)

        if not success:
            await ctx.send("❌ Failed to fetch or post the shop. Check logs for details.")

    @fnshop.command(name="help")
    async def fnshop_help(self, ctx: commands.Context) -> None:
        """Show FortniteShop command reference."""
        prefix = ctx.clean_prefix
        em = discord.Embed(
            title="🛒  FortniteShop — Help",
            colour=0x00D4FF,
            description=(
                "Automatically posts the Fortnite item shop every day "
                "when the shop resets at **midnight UTC**."
            ),
        )
        em.add_field(
            name="Setup (admin only)",
            value=(
                f"`{prefix}fnshop set channel #channel` — set the post channel\n"
                f"`{prefix}fnshop set ping @role` — role to ping (leave blank to clear)\n"
                f"`{prefix}fnshop set prices true/false` — show V-Buck prices\n"
                f"`{prefix}fnshop set maxitems <1-40>` — how many items to display\n"
                f"`{prefix}fnshop set disable` — turn off auto-posting"
            ),
            inline=False,
        )
        em.add_field(
            name="Utility",
            value=(
                f"`{prefix}fnshop now [#channel]` — post the shop right now\n"
                f"`{prefix}fnshop settings` — view current config\n"
                f"`{prefix}fnshop help` — show this message"
            ),
            inline=False,
        )
        em.set_footer(text="Powered by fortnite-api.com")
        await ctx.send(embed=em)


# ── Red entrypoint ─────────────────────────────────────────────────────────────
async def setup(bot: Red) -> None:
    await bot.add_cog(FortniteShop(bot))
