"""
TCGTracker – Red-DiscordBot Cog
Tracks Pokémon TCG product drops across major retailers by UPC,
with optional in-store availability checks by ZIP code.

Admin / settings commands:
  [p]tcgset channel #channel      — set alert channel
  [p]tcgset role @role            — set ping role
  [p]tcgset bestbuy_key <key>     — set Best Buy API key (free at developer.bestbuy.com)
  [p]tcgset interval <seconds>    — set check interval (default 300, minimum 60)
  [p]tcgset zip <zip_code>        — add a ZIP code for in-store inventory checks
  [p]tcgset unzip <zip_code>      — remove a ZIP code
  [p]tcgset status                — show current configuration

Product commands (admin only):
  [p]tcgadd <upc> <msrp> <name>   — add a product to track
  [p]tcgremove <upc>              — remove a tracked product
  [p]tcglist                      — list all tracked products
  [p]tcgcheck                     — manually trigger a check right now
  [p]tcgreset <upc>               — reset alert cooldown for a product
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp
import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red

from .retailers import check_all_retailers, check_all_stores

log = logging.getLogger("red.tcgtracker")

RETAILER_EMOJIS = {
    "Best Buy":       "💛",
    "Walmart":        "💙",
    "Target":         "🎯",
    "GameStop":       "🎮",
    "Pokémon Center": "🔴",
}

CHECK_INTERVAL_DEFAULT = 300   # 5 minutes
MAX_EMBED_FIELDS       = 24    # Discord cap is 25; leave 1 for overflow notice
MAX_ZIP_CODES          = 10    # Reasonable per-guild cap


class TCGTracker(commands.Cog):
    """Tracks Pokémon TCG product restocks across major retailers by UPC."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        self._check_task: Optional[asyncio.Task] = None

        self.config = Config.get_conf(self, identifier=0x54434754524B52, force_registration=True)

        default_guild = {
            "alert_channel_id": None,       # Online restock alerts
            "store_channel_id": None,        # In-store availability alerts
            "alert_role_id": None,
            "bestbuy_key": "",
            "check_interval": CHECK_INTERVAL_DEFAULT,
            "zip_codes": [],                 # List of ZIP code strings for in-store checks
            # Dict of upc -> { "upc": str, "name": str, "msrp": float,
            #                   "added_at": float, "alerted": { retailer: bool } }
            "products": {},
        }
        self.config.register_guild(**default_guild)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        self._session = aiohttp.ClientSession()
        self._check_task = self.bot.loop.create_task(self._check_loop())

    async def cog_unload(self) -> None:
        if self._check_task:
            self._check_task.cancel()
        if self._session:
            await self._session.close()

    # ── Background loop ────────────────────────────────────────────────────────

    async def _check_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            try:
                await self._run_checks()
            except Exception:
                log.exception("Unhandled error in TCGTracker check loop")

            # Use the interval from the first guild that has products configured,
            # falling back to the default if no guilds are active.
            interval = CHECK_INTERVAL_DEFAULT
            for guild in self.bot.guilds:
                products = await self.config.guild(guild).products()
                if products:
                    interval = await self.config.guild(guild).check_interval()
                    break

            await asyncio.sleep(interval)

    async def _run_checks(self) -> None:
        """Check all guilds and all tracked products (background loop version)."""
        for guild in self.bot.guilds:
            guild_conf = await self.config.guild(guild).all()
            products   = guild_conf.get("products", {})
            if not products:
                continue

            channel_id = guild_conf.get("alert_channel_id")
            role_id    = guild_conf.get("alert_role_id")
            bby_key    = guild_conf.get("bestbuy_key", "")

            channel = guild.get_channel(channel_id) if channel_id else None
            role    = guild.get_role(role_id) if role_id else None

            for upc, product in products.items():
                results = await check_all_retailers(
                    self._session,
                    upc,
                    product["name"],
                    bestbuy_key=bby_key,
                )

                alerted: Dict[str, bool] = product.get("alerted", {})
                msrp = product.get("msrp", 0)
                changed = False

                for result in results:
                    retailer    = result["retailer"]
                    in_stock    = result["in_stock"]
                    was_alerted = alerted.get(retailer, False)

                    if in_stock and not was_alerted:
                        if channel:
                            await self._send_alert(channel, role, product, result, msrp)
                        alerted[retailer] = True
                        changed = True
                    elif not in_stock and was_alerted:
                        alerted[retailer] = False
                        changed = True

                if changed:
                    async with self.config.guild(guild).products() as saved:
                        if upc in saved:
                            saved[upc]["alerted"] = alerted

                await asyncio.sleep(2)  # Be polite between products

    # ── Shared logic for processing results ────────────────────────────────────

    async def _process_check_results(
        self,
        guild: discord.Guild,
        upc: str,
        product: dict,
        results: list,
        channel: discord.TextChannel,
        role: Optional[discord.Role],
    ) -> bool:
        """
        Shared logic for evaluating online stock results, sending alerts,
        and persisting alerted state. Returns True if any new stock was found.
        """
        alerted: Dict[str, bool] = product.get("alerted", {})
        msrp    = product.get("msrp", 0)
        changed = False
        found   = False

        for result in results:
            retailer    = result["retailer"]
            in_stock    = result["in_stock"]
            was_alerted = alerted.get(retailer, False)

            if in_stock and not was_alerted:
                await self._send_alert(channel, role, product, result, msrp)
                alerted[retailer] = True
                changed = True
                found   = True
            elif not in_stock and was_alerted:
                alerted[retailer] = False
                changed = True

        if changed:
            async with self.config.guild(guild).products() as saved:
                if upc in saved:
                    saved[upc]["alerted"] = alerted

        return found

    # ── Alert / embed helpers ─────────────────────────────────────────────────

    async def _send_alert(
        self,
        channel: discord.TextChannel,
        role: Optional[discord.Role],
        product: dict,
        result: dict,
        msrp: float,
    ) -> None:
        retailer = result["retailer"]
        emoji    = RETAILER_EMOJIS.get(retailer, "🛒")
        price    = result.get("price")
        name     = result.get("name") or product["name"]
        url      = result.get("url", "")

        if price is not None and msrp > 0:
            diff = price - msrp
            if diff <= 0:
                price_str = f"**${price:.2f}** ✅ At/below MSRP (${msrp:.2f})"
            else:
                price_str = f"**${price:.2f}** ⚠️ Above MSRP (${msrp:.2f} · +${diff:.2f})"
        elif price is not None:
            price_str = f"**${price:.2f}**"
        else:
            price_str = "_Price unavailable_"

        embed = discord.Embed(
            title=f"{emoji} {retailer} — IN STOCK!",
            description=f"**{name}**\n\n💰 Price: {price_str}\n🔗 [View Product]({url})",
            color=self._retailer_color(retailer),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="UPC",  value=product.get("upc", "N/A"), inline=True)
        embed.add_field(name="MSRP", value=f"${msrp:.2f}" if msrp else "Not set", inline=True)
        embed.set_footer(text="TCGTracker • Drop alert")

        ping = role.mention if role else ""
        await channel.send(content=ping, embed=embed)

    def _retailer_color(self, retailer: str) -> int:
        colors = {
            "Best Buy":       0xFFE000,
            "Walmart":        0x0071CE,
            "Target":         0xCC0000,
            "GameStop":       0x5C2D91,
            "Pokémon Center": 0xFF0000,
        }
        return colors.get(retailer, 0x7289DA)

    def _ok(self, msg: str) -> discord.Embed:
        return discord.Embed(color=0x2ECC71, description=f"✅ {msg}")

    def _err(self, msg: str) -> discord.Embed:
        return discord.Embed(color=0xE74C3C, description=f"❌ {msg}")

    # ══════════════════════════════════════════════════════════════════════════
    # COMMANDS
    # ══════════════════════════════════════════════════════════════════════════

    # ── Settings ───────────────────────────────────────────────────────────────

    @commands.group(name="tcgset")
    @checks.admin_or_permissions(manage_guild=True)
    async def tcgset(self, ctx: commands.Context) -> None:
        """Configure TCGTracker settings."""

    @tcgset.command(name="channel")
    async def tcgset_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the channel where online drop alerts will be posted."""
        await self.config.guild(ctx.guild).alert_channel_id.set(channel.id)
        await ctx.send(embed=self._ok(f"Online alert channel set to {channel.mention}."))

    @tcgset.command(name="storechannel")
    async def tcgset_storechannel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """
        Set the channel where in-store availability results will be posted.
        If not set, in-store results fall back to wherever tcgcheck is run.
        Usage: `tcgset storechannel #in-store-alerts`
        """
        await self.config.guild(ctx.guild).store_channel_id.set(channel.id)
        await ctx.send(embed=self._ok(f"In-store alert channel set to {channel.mention}."))

    @tcgset.command(name="clearstorechannel")
    async def tcgset_clearstorechannel(self, ctx: commands.Context) -> None:
        """Clear the in-store channel so results fall back to wherever tcgcheck is run."""
        await self.config.guild(ctx.guild).store_channel_id.set(None)
        await ctx.send(embed=self._ok("In-store channel cleared. Results will post to wherever `tcgcheck` is run."))

    @tcgset.command(name="role")
    async def tcgset_role(self, ctx: commands.Context, role: discord.Role) -> None:
        """Set the role that gets pinged on drop alerts."""
        await self.config.guild(ctx.guild).alert_role_id.set(role.id)
        await ctx.send(embed=self._ok(f"Alert role set to {role.mention}."))

    @tcgset.command(name="bestbuy_key")
    async def tcgset_bby(self, ctx: commands.Context, key: str) -> None:
        """Set your Best Buy API key (free at developer.bestbuy.com). Used for online AND in-store checks."""
        await self.config.guild(ctx.guild).bestbuy_key.set(key)
        await ctx.message.delete()
        await ctx.send(embed=self._ok("Best Buy API key saved. Message deleted for security."))

    @tcgset.command(name="interval")
    async def tcgset_interval(self, ctx: commands.Context, seconds: int) -> None:
        """Set how often to check for restocks in seconds (minimum 60, maximum 86400)."""
        seconds = max(60, min(86400, seconds))
        await self.config.guild(ctx.guild).check_interval.set(seconds)
        await ctx.send(embed=self._ok(f"Check interval set to **{seconds}s** ({seconds // 60}m {seconds % 60}s)."))

    @tcgset.command(name="zip")
    async def tcgset_zip(self, ctx: commands.Context, zip_code: str) -> None:
        """
        Add a ZIP code for in-store inventory checks.
        Stores near this ZIP will be shown when running `tcgcheck`.
        Usage: `tcgset zip 27360`
        """
        zip_code = zip_code.strip()
        if not zip_code.isdigit() or len(zip_code) != 5:
            await ctx.send(embed=self._err("ZIP code must be exactly 5 digits."))
            return

        async with self.config.guild(ctx.guild).zip_codes() as zips:
            if zip_code in zips:
                await ctx.send(embed=self._err(f"ZIP code `{zip_code}` is already added."))
                return
            if len(zips) >= MAX_ZIP_CODES:
                await ctx.send(embed=self._err(f"Maximum of {MAX_ZIP_CODES} ZIP codes allowed. Remove one first with `tcgset unzip <zip>`."))
                return
            zips.append(zip_code)

        await ctx.send(embed=self._ok(f"ZIP code `{zip_code}` added. In-store availability near this ZIP will be shown in `tcgcheck` results."))

    @tcgset.command(name="unzip")
    async def tcgset_unzip(self, ctx: commands.Context, zip_code: str) -> None:
        """
        Remove a ZIP code from in-store checks.
        Usage: `tcgset unzip 27360`
        """
        zip_code = zip_code.strip()
        async with self.config.guild(ctx.guild).zip_codes() as zips:
            if zip_code not in zips:
                await ctx.send(embed=self._err(f"ZIP code `{zip_code}` is not in the list."))
                return
            zips.remove(zip_code)

        await ctx.send(embed=self._ok(f"ZIP code `{zip_code}` removed."))

    @tcgset.command(name="status")
    async def tcgset_status(self, ctx: commands.Context) -> None:
        """Show current TCGTracker configuration."""
        conf          = await self.config.guild(ctx.guild).all()
        channel       = ctx.guild.get_channel(conf["alert_channel_id"])
        store_channel = ctx.guild.get_channel(conf["store_channel_id"]) if conf.get("store_channel_id") else None
        role          = ctx.guild.get_role(conf["alert_role_id"])
        bby           = "✅ Set" if conf.get("bestbuy_key") else "❌ Not set"
        interval      = conf.get("check_interval", CHECK_INTERVAL_DEFAULT)
        products      = conf.get("products", {})
        zip_codes     = conf.get("zip_codes", [])

        embed = discord.Embed(title="⚙️ TCGTracker Status", color=0x7289DA)
        embed.add_field(name="Online Alert Channel", value=channel.mention if channel else "❌ Not set",                                             inline=True)
        embed.add_field(name="In-Store Channel",     value=store_channel.mention if store_channel else "⚠️ Not set (falls back to command channel)", inline=True)
        embed.add_field(name="Alert Role",           value=role.mention if role else "❌ Not set",                                                   inline=True)
        embed.add_field(name="Check Interval",       value=f"{interval}s ({interval // 60}m)",                                                      inline=True)
        embed.add_field(name="Best Buy API Key",     value=bby,                                                                                     inline=True)
        embed.add_field(name="Walmart / Target / GameStop", value="✅ Scraping (no key needed)",                                                    inline=True)
        embed.add_field(name="Tracked Products",     value=str(len(products)),                                                                      inline=True)
        embed.add_field(
            name=f"ZIP Codes for In-Store ({len(zip_codes)}/{MAX_ZIP_CODES})",
            value=", ".join(f"`{z}`" for z in zip_codes) if zip_codes else "❌ None set — add with `tcgset zip <zip>`",
            inline=False,
        )
        await ctx.send(embed=embed)

    # ── Product Management ─────────────────────────────────────────────────────

    @commands.command(name="tcgadd")
    @checks.admin_or_permissions(manage_guild=True)
    async def tcgadd(self, ctx: commands.Context, upc: str, msrp: float, *, name: str) -> None:
        """
        Add a product to track by UPC.
        Usage: `tcgadd <upc> <msrp> <product name>`
        Example: `tcgadd 820650855344 44.99 Scarlet & Violet Destined Rivals ETB`
        """
        upc = upc.strip()
        if not upc.isdigit():
            await ctx.send(embed=self._err("UPC must be numbers only (no dashes or spaces)."))
            return
        if msrp <= 0:
            await ctx.send(embed=self._err("MSRP must be a positive number."))
            return

        async with self.config.guild(ctx.guild).products() as products:
            if upc in products:
                await ctx.send(embed=self._err(f"UPC `{upc}` is already being tracked. Use `tcgremove {upc}` first to replace it."))
                return
            products[upc] = {
                "upc":      upc,
                "name":     name.strip(),
                "msrp":     msrp,
                "added_at": time.time(),
                "alerted":  {},
            }

        embed = discord.Embed(
            title="✅ Product Added",
            color=0x2ECC71,
            description=f"Now tracking **{name}**",
        )
        embed.add_field(name="UPC",  value=upc,            inline=True)
        embed.add_field(name="MSRP", value=f"${msrp:.2f}", inline=True)
        embed.set_footer(text="Checks run on the configured interval across all retailers.")
        await ctx.send(embed=embed)

    @commands.command(name="tcgremove")
    @checks.admin_or_permissions(manage_guild=True)
    async def tcgremove(self, ctx: commands.Context, upc: str) -> None:
        """
        Remove a tracked product by UPC.
        Usage: `tcgremove <upc>`
        """
        upc = upc.strip()
        async with self.config.guild(ctx.guild).products() as products:
            if upc not in products:
                await ctx.send(embed=self._err(f"UPC `{upc}` is not being tracked."))
                return
            name = products[upc]["name"]
            del products[upc]

        await ctx.send(embed=self._ok(f"Stopped tracking **{name}** (`{upc}`)."))

    @commands.command(name="tcglist")
    @checks.admin_or_permissions(manage_guild=True)
    async def tcglist(self, ctx: commands.Context) -> None:
        """List all currently tracked products."""
        products = await self.config.guild(ctx.guild).products()
        if not products:
            await ctx.send(embed=self._err("No products are being tracked. Use `tcgadd <upc> <msrp> <name>` to add one."))
            return

        product_list = list(products.items())
        pages: List[discord.Embed] = []
        chunk_size = MAX_EMBED_FIELDS

        for page_start in range(0, len(product_list), chunk_size):
            chunk = product_list[page_start:page_start + chunk_size]
            page_num = (page_start // chunk_size) + 1
            total_pages = (len(product_list) + chunk_size - 1) // chunk_size

            title = f"📋 Tracked Products ({len(products)})"
            if total_pages > 1:
                title += f" — Page {page_num}/{total_pages}"

            embed = discord.Embed(title=title, color=0x3498DB)
            for upc, p in chunk:
                alerted     = p.get("alerted", {})
                in_stock_at = [r for r, v in alerted.items() if v]
                status = (
                    f"🟢 In stock at: {', '.join(in_stock_at)}"
                    if in_stock_at
                    else "🔴 Out of stock everywhere"
                )
                added = (
                    datetime.fromtimestamp(p["added_at"]).strftime("%Y-%m-%d")
                    if p.get("added_at") else "Unknown"
                )
                embed.add_field(
                    name=p["name"],
                    value=f"UPC: `{upc}` | MSRP: ${p['msrp']:.2f} | Added: {added}\n{status}",
                    inline=False,
                )
            pages.append(embed)

        for embed in pages:
            await ctx.send(embed=embed)

    @commands.command(name="tcgcheck")
    @checks.admin_or_permissions(manage_guild=True)
    async def tcgcheck(self, ctx: commands.Context) -> None:
        """
        Manually trigger an immediate check of all tracked products.
        Shows the full current status for every retailer regardless of alert state,
        and sends role-ping alerts only for genuinely new restocks.
        If ZIP codes are configured, also shows nearby in-store inventory.
        """
        if not self._session:
            await ctx.send(embed=self._err("Session not ready yet — please try again in a moment."))
            return

        products = await self.config.guild(ctx.guild).products()
        if not products:
            await ctx.send(embed=self._err("No products are being tracked."))
            return

        conf            = await self.config.guild(ctx.guild).all()
        bby_key         = conf.get("bestbuy_key", "")
        zip_codes       = conf.get("zip_codes", [])
        channel_id      = conf.get("alert_channel_id")
        store_channel_id = conf.get("store_channel_id")
        role_id         = conf.get("alert_role_id")

        # Online results → configured online channel, else fall back to where command was run
        online_channel = ctx.guild.get_channel(channel_id) if channel_id else ctx.channel
        # In-store results → configured store channel, else fall back to where command was run
        store_channel  = ctx.guild.get_channel(store_channel_id) if store_channel_id else ctx.channel
        role           = ctx.guild.get_role(role_id) if role_id else None

        store_note = f" + in-store checks for {len(zip_codes)} ZIP(s)" if zip_codes else ""
        msg = await ctx.send(embed=discord.Embed(
            description=f"🔍 Checking {len(products)} product(s) across all retailers{store_note}...",
            color=0xF1C40F,
        ))

        found_any = False

        for upc, product in products.items():
            # ── Online availability ────────────────────────────────────────
            online_results = await check_all_retailers(
                self._session, upc, product["name"], bestbuy_key=bby_key,
            )

            # Always show the full status summary in the online channel
            await self._send_manual_check_summary(online_channel, product, online_results)

            # Still fire role-ping alerts for any genuinely new restocks
            found_any = await self._process_check_results(
                ctx.guild, upc, product, online_results, online_channel, role
            ) or found_any

            # ── In-store availability (per ZIP) ───────────────────────────
            if zip_codes:
                for zip_code in zip_codes:
                    store_results = await check_all_stores(
                        self._session, upc, zip_code, online_results, bestbuy_key=bby_key,
                    )
                    if store_results:
                        await self._send_store_embed(store_channel, product, zip_code, store_results)
                    await asyncio.sleep(1)

            await asyncio.sleep(1)

        summary = (
            "✅ Check complete! Role alerts sent for new restocks."
            if found_any
            else "✅ Check complete."
        )
        await msg.edit(embed=discord.Embed(description=summary, color=0x2ECC71))

    async def _send_manual_check_summary(
        self,
        channel: discord.TextChannel,
        product: dict,
        results: list,
    ) -> None:
        """
        Send a full status snapshot for one product to the command invoker's channel.
        Shows every retailer's current in-stock state regardless of alert cooldown —
        this is purely informational and never triggers role pings.
        """
        msrp = product.get("msrp", 0)

        embed = discord.Embed(
            title=f"🔍 {product['name']}",
            description=f"UPC: `{product['upc']}` · MSRP: ${msrp:.2f}",
            color=0x3498DB,
            timestamp=datetime.now(timezone.utc),
        )

        if not results:
            embed.add_field(
                name="No Results",
                value="No retailer returned data. Check your Best Buy key or try again shortly.",
                inline=False,
            )
            await channel.send(embed=embed)
            return

        # Group results by retailer (take the first/best result per retailer)
        by_retailer: Dict[str, dict] = {}
        for r in results:
            retailer = r["retailer"]
            if retailer not in by_retailer:
                by_retailer[retailer] = r

        for retailer, r in by_retailer.items():
            emoji    = RETAILER_EMOJIS.get(retailer, "🛒")
            in_stock = r["in_stock"]
            price    = r.get("price")
            url      = r.get("url", "")

            if in_stock:
                status = "🟢 **IN STOCK**"
            else:
                status = "🔴 Out of stock"

            if price is not None and msrp > 0:
                diff = price - msrp
                if diff <= 0:
                    price_str = f"${price:.2f} ✅"
                else:
                    price_str = f"${price:.2f} ⚠️ (+${diff:.2f} over MSRP)"
            elif price is not None:
                price_str = f"${price:.2f}"
            else:
                price_str = "Price unavailable"

            value = f"{status} · {price_str}"
            if url:
                value += f"\n[View listing]({url})"

            embed.add_field(
                name=f"{emoji} {retailer}",
                value=value,
                inline=True,
            )

        embed.set_footer(text="TCGTracker • Manual check — role alerts only fire for new restocks")
        await channel.send(embed=embed)

    async def _send_store_embed(
        self,
        channel: discord.TextChannel,
        product: dict,
        zip_code: str,
        store_results: list,
    ) -> None:
        """Send a single embed listing all in-store results for one product + ZIP."""
        in_stock_stores  = [s for s in store_results if s["in_stock"]]
        oos_stores       = [s for s in store_results if not s["in_stock"]]

        embed = discord.Embed(
            title=f"🏪 In-Store Availability Near ZIP {zip_code}",
            description=f"**{product['name']}** · UPC `{product['upc']}`",
            color=0x9B59B6,
            timestamp=datetime.now(timezone.utc),
        )

        def fmt_store(s: dict) -> str:
            parts = []
            if s.get("address"):
                parts.append(s["address"])
            city_state = " ".join(filter(None, [s.get("city"), s.get("state")]))
            if city_state:
                parts.append(city_state)
            if s.get("zip"):
                parts.append(s["zip"])
            addr = ", ".join(parts) if parts else "Address unavailable"
            dist = f" · {s['distance_miles']:.1f} mi" if s.get("distance_miles") is not None else ""
            qty  = f" · Qty: {s['quantity']}" if s.get("quantity") is not None else ""
            return f"{addr}{dist}{qty}"

        # Group in-stock stores by retailer
        retailers_seen: Dict[str, List[str]] = {}
        for s in in_stock_stores:
            retailers_seen.setdefault(s["retailer"], []).append(
                f"• **{s['store_name']}** — {fmt_store(s)}"
            )

        if retailers_seen:
            for retailer, lines in retailers_seen.items():
                emoji = RETAILER_EMOJIS.get(retailer, "🛒")
                field_val = "\n".join(lines[:5])  # Cap at 5 stores per retailer per embed
                if len(lines) > 5:
                    field_val += f"\n_…and {len(lines) - 5} more_"
                embed.add_field(
                    name=f"{emoji} {retailer} — IN STOCK ({len(lines)} location{'s' if len(lines) != 1 else ''})",
                    value=field_val,
                    inline=False,
                )
        else:
            embed.add_field(
                name="🔴 No In-Store Stock Found",
                value=f"Checked {len(store_results)} location(s) — none have it in stock near ZIP {zip_code}.",
                inline=False,
            )

        if oos_stores and in_stock_stores:
            oos_summary = ", ".join(
                f"{s['retailer']} #{i + 1}" for i, s in enumerate(oos_stores[:6])
            )
            if len(oos_stores) > 6:
                oos_summary += f" +{len(oos_stores) - 6} more"
            embed.set_footer(text=f"Also checked (OOS): {oos_summary}")
        else:
            embed.set_footer(text="TCGTracker • In-store check")

        await channel.send(embed=embed)

    @commands.command(name="tcgreset")
    @checks.admin_or_permissions(manage_guild=True)
    async def tcgreset(self, ctx: commands.Context, upc: str) -> None:
        """
        Reset the alert cooldown for a product so it will alert again on next check.
        Useful if you want a fresh alert after manually confirming it went OOS.
        Usage: `tcgreset <upc>`
        """
        upc = upc.strip()
        async with self.config.guild(ctx.guild).products() as products:
            if upc not in products:
                await ctx.send(embed=self._err(f"UPC `{upc}` is not being tracked."))
                return
            products[upc]["alerted"] = {}
            name = products[upc]["name"]

        await ctx.send(embed=self._ok(
            f"Alert cooldown reset for **{name}**. It will alert again on the next check if in stock."
        ))


async def setup(bot: Red) -> None:
    await bot.add_cog(TCGTracker(bot))
