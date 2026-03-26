"""
TCGTracker – Red-DiscordBot Cog
Tracks Pokémon TCG product drops at Best Buy by UPC using their official API.

Settings commands (admin only):
  [p]tcgset bestbuy_key <key>     — set Best Buy API key (free at developer.bestbuy.com)
  [p]tcgset channel #channel      — set channel for online restock alerts
  [p]tcgset storechannel #channel — set channel for in-store availability results
  [p]tcgset clearstorechannel     — remove dedicated in-store channel
  [p]tcgset role @role            — set role to ping on restock alerts
  [p]tcgset interval <seconds>    — set check interval (default 300, min 60, max 86400)
  [p]tcgset zip <zip>             — add a ZIP code for in-store checks
  [p]tcgset unzip <zip>           — remove a ZIP code
  [p]tcgset status                — show current configuration

Product commands (admin only):
  [p]tcgadd <upc> <msrp> <name>  — add a product to track
  [p]tcgremove <upc>              — stop tracking a product
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

from .retailers import check_bestbuy, check_bestbuy_stores

log = logging.getLogger("red.tcgtracker")

CHECK_INTERVAL_DEFAULT = 300  # 5 minutes
MAX_EMBED_FIELDS       = 24   # Discord cap is 25; keep one spare
MAX_ZIP_CODES          = 10


class TCGTracker(commands.Cog):
    """Tracks Pokémon TCG product restocks at Best Buy by UPC."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        self._check_task: Optional[asyncio.Task] = None

        self.config = Config.get_conf(self, identifier=0x54434754524B52, force_registration=True)
        self.config.register_guild(
            alert_channel_id=None,       # Online restock alerts
            store_channel_id=None,       # In-store availability results
            alert_role_id=None,
            bestbuy_key="",
            check_interval=CHECK_INTERVAL_DEFAULT,
            zip_codes=[],                # ZIP codes for in-store checks
            # upc -> { upc, name, msrp, added_at, alerted: { retailer: bool } }
            products={},
        )

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        # ssl=False handles networks where antivirus or VPN SSL inspection presents
        # a self-signed certificate that aiohttp's verifier rejects by default.
        connector = aiohttp.TCPConnector(ssl=False)
        self._session = aiohttp.ClientSession(connector=connector)
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

            interval = CHECK_INTERVAL_DEFAULT
            for guild in self.bot.guilds:
                if await self.config.guild(guild).products():
                    interval = await self.config.guild(guild).check_interval()
                    break
            await asyncio.sleep(interval)

    async def _run_checks(self) -> None:
        """Background loop: check all guilds and products, fire alerts for new restocks."""
        for guild in self.bot.guilds:
            conf      = await self.config.guild(guild).all()
            products  = conf.get("products", {})
            if not products:
                continue

            channel   = guild.get_channel(conf["alert_channel_id"]) if conf["alert_channel_id"] else None
            role      = guild.get_role(conf["alert_role_id"]) if conf["alert_role_id"] else None
            bby_key   = conf.get("bestbuy_key", "")

            for upc, product in products.items():
                results = await check_bestbuy(self._session, upc, bby_key)
                await self._process_results(guild, upc, product, results, channel, role)
                await asyncio.sleep(1)

    # ── Shared result processor ────────────────────────────────────────────────

    async def _process_results(
        self,
        guild: discord.Guild,
        upc: str,
        product: dict,
        results: list,
        channel: Optional[discord.TextChannel],
        role: Optional[discord.Role],
    ) -> bool:
        """
        Evaluate stock results, send role-ping alerts for new restocks, and
        persist alerted state. Returns True if any new stock was found.
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
                if channel:
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

    # ── Alert embeds ───────────────────────────────────────────────────────────

    async def _send_alert(
        self,
        channel: discord.TextChannel,
        role: Optional[discord.Role],
        product: dict,
        result: dict,
        msrp: float,
    ) -> None:
        """Send a role-ping restock alert embed."""
        price = result.get("price")
        url   = result.get("url", "")
        name  = result.get("name") or product["name"]

        if price is not None and msrp > 0:
            diff = price - msrp
            price_str = (
                f"**${price:.2f}** ✅ At/below MSRP (${msrp:.2f})"
                if diff <= 0
                else f"**${price:.2f}** ⚠️ Above MSRP (${msrp:.2f} · +${diff:.2f})"
            )
        elif price is not None:
            price_str = f"**${price:.2f}**"
        else:
            price_str = "_Price unavailable_"

        embed = discord.Embed(
            title="💛 Best Buy — IN STOCK!",
            description=f"**{name}**\n\n💰 Price: {price_str}\n🔗 [View Product]({url})",
            color=0xFFE000,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="UPC",  value=product.get("upc", "N/A"), inline=True)
        embed.add_field(name="MSRP", value=f"${msrp:.2f}" if msrp else "Not set", inline=True)
        embed.set_footer(text="TCGTracker • Restock alert")

        await channel.send(content=role.mention if role else "", embed=embed)

    async def _send_manual_summary(
        self,
        channel: discord.TextChannel,
        product: dict,
        results: list,
    ) -> None:
        """
        Send a full status snapshot for one product.
        Shows current stock state regardless of alert cooldown — never pings.
        """
        msrp  = product.get("msrp", 0)
        embed = discord.Embed(
            title=f"🔍 {product['name']}",
            description=f"UPC: `{product['upc']}` · MSRP: ${msrp:.2f}",
            color=0x3498DB,
            timestamp=datetime.now(timezone.utc),
        )

        if not results:
            embed.add_field(
                name="⚪ Best Buy",
                value="Not found in Best Buy's catalog for this UPC.",
                inline=False,
            )
            embed.set_footer(text="TCGTracker • Manual check")
            await channel.send(embed=embed)
            return

        for result in results:
            price    = result.get("price")
            url      = result.get("url", "")
            in_stock = result["in_stock"]

            status = "🟢 **IN STOCK**" if in_stock else "🔴 Out of stock"

            if price is not None and msrp > 0:
                diff = price - msrp
                price_str = f"${price:.2f} ✅" if diff <= 0 else f"${price:.2f} ⚠️ (+${diff:.2f} over MSRP)"
            elif price is not None:
                price_str = f"${price:.2f}"
            else:
                price_str = "Price unavailable"

            value = f"{status} · {price_str}"
            if url:
                value += f"\n[View listing]({url})"

            embed.add_field(name="💛 Best Buy", value=value, inline=True)

        embed.set_footer(text="TCGTracker • Manual check — role alerts only fire for new restocks")
        await channel.send(embed=embed)

    async def _send_store_embed(
        self,
        channel: discord.TextChannel,
        product: dict,
        zip_code: str,
        store_results: list,
    ) -> None:
        """Send in-store availability results for one product + ZIP."""
        in_stock_stores = [s for s in store_results if s["in_stock"]]
        oos_stores      = [s for s in store_results if not s["in_stock"]]

        embed = discord.Embed(
            title=f"🏪 Best Buy In-Store — ZIP {zip_code}",
            description=f"**{product['name']}** · UPC `{product['upc']}`",
            color=0xFFE000,
            timestamp=datetime.now(timezone.utc),
        )

        def fmt_store(s: dict) -> str:
            parts    = [p for p in [s.get("address"), s.get("city"), s.get("state"), s.get("zip")] if p]
            addr     = ", ".join(parts) if parts else "Address unavailable"
            dist     = f" · {s['distance_miles']:.1f} mi" if s.get("distance_miles") is not None else ""
            return f"{addr}{dist}"

        if in_stock_stores:
            lines = [f"• **{s['store_name']}** — {fmt_store(s)}" for s in in_stock_stores[:8]]
            if len(in_stock_stores) > 8:
                lines.append(f"_…and {len(in_stock_stores) - 8} more_")
            embed.add_field(
                name=f"🟢 In Stock ({len(in_stock_stores)} location{'s' if len(in_stock_stores) != 1 else ''})",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="🔴 No In-Store Stock Found",
                value=f"Checked {len(store_results)} Best Buy location(s) within 25 miles — none have it in stock.",
                inline=False,
            )

        if oos_stores and in_stock_stores:
            embed.set_footer(text=f"Also checked (OOS): {len(oos_stores)} location(s) · TCGTracker")
        else:
            embed.set_footer(text="TCGTracker • In-store check")

        await channel.send(embed=embed)

    # ══════════════════════════════════════════════════════════════════════════
    # COMMANDS
    # ══════════════════════════════════════════════════════════════════════════

    # ── Settings ───────────────────────────────────────────────────────────────

    @commands.group(name="tcgset")
    @checks.admin_or_permissions(manage_guild=True)
    async def tcgset(self, ctx: commands.Context) -> None:
        """Configure TCGTracker settings."""

    @tcgset.command(name="bestbuy_key")
    async def tcgset_bby(self, ctx: commands.Context, key: str) -> None:
        """Set your Best Buy API key (free at developer.bestbuy.com)."""
        await self.config.guild(ctx.guild).bestbuy_key.set(key)
        await ctx.message.delete()
        await ctx.send(embed=self._ok("Best Buy API key saved. Message deleted for security."))

    @tcgset.command(name="channel")
    async def tcgset_channel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the channel for online restock alerts."""
        await self.config.guild(ctx.guild).alert_channel_id.set(channel.id)
        await ctx.send(embed=self._ok(f"Online alert channel set to {channel.mention}."))

    @tcgset.command(name="storechannel")
    async def tcgset_storechannel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the channel for in-store availability results from tcgcheck."""
        await self.config.guild(ctx.guild).store_channel_id.set(channel.id)
        await ctx.send(embed=self._ok(f"In-store channel set to {channel.mention}."))

    @tcgset.command(name="clearstorechannel")
    async def tcgset_clearstorechannel(self, ctx: commands.Context) -> None:
        """Remove the dedicated in-store channel (results will post to wherever tcgcheck is run)."""
        await self.config.guild(ctx.guild).store_channel_id.set(None)
        await ctx.send(embed=self._ok("In-store channel cleared. Results will post to the command channel."))

    @tcgset.command(name="role")
    async def tcgset_role(self, ctx: commands.Context, role: discord.Role) -> None:
        """Set the role that gets pinged on restock alerts."""
        await self.config.guild(ctx.guild).alert_role_id.set(role.id)
        await ctx.send(embed=self._ok(f"Alert role set to {role.mention}."))

    @tcgset.command(name="interval")
    async def tcgset_interval(self, ctx: commands.Context, seconds: int) -> None:
        """Set how often to check for restocks in seconds (min 60, max 86400)."""
        seconds = max(60, min(86400, seconds))
        await self.config.guild(ctx.guild).check_interval.set(seconds)
        await ctx.send(embed=self._ok(f"Check interval set to **{seconds}s** ({seconds // 60}m {seconds % 60}s)."))

    @tcgset.command(name="zip")
    async def tcgset_zip(self, ctx: commands.Context, zip_code: str) -> None:
        """Add a ZIP code for in-store inventory checks (up to 10)."""
        zip_code = zip_code.strip()
        if not zip_code.isdigit() or len(zip_code) != 5:
            await ctx.send(embed=self._err("ZIP code must be exactly 5 digits."))
            return
        async with self.config.guild(ctx.guild).zip_codes() as zips:
            if zip_code in zips:
                await ctx.send(embed=self._err(f"ZIP `{zip_code}` is already added."))
                return
            if len(zips) >= MAX_ZIP_CODES:
                await ctx.send(embed=self._err(f"Maximum of {MAX_ZIP_CODES} ZIP codes allowed. Remove one first."))
                return
            zips.append(zip_code)
        await ctx.send(embed=self._ok(f"ZIP `{zip_code}` added for in-store checks."))

    @tcgset.command(name="unzip")
    async def tcgset_unzip(self, ctx: commands.Context, zip_code: str) -> None:
        """Remove a ZIP code from in-store checks."""
        zip_code = zip_code.strip()
        async with self.config.guild(ctx.guild).zip_codes() as zips:
            if zip_code not in zips:
                await ctx.send(embed=self._err(f"ZIP `{zip_code}` is not in the list."))
                return
            zips.remove(zip_code)
        await ctx.send(embed=self._ok(f"ZIP `{zip_code}` removed."))

    @tcgset.command(name="status")
    async def tcgset_status(self, ctx: commands.Context) -> None:
        """Show current TCGTracker configuration."""
        conf          = await self.config.guild(ctx.guild).all()
        channel       = ctx.guild.get_channel(conf["alert_channel_id"]) if conf["alert_channel_id"] else None
        store_channel = ctx.guild.get_channel(conf["store_channel_id"]) if conf.get("store_channel_id") else None
        role          = ctx.guild.get_role(conf["alert_role_id"]) if conf["alert_role_id"] else None
        interval      = conf.get("check_interval", CHECK_INTERVAL_DEFAULT)
        zip_codes     = conf.get("zip_codes", [])
        products      = conf.get("products", {})

        embed = discord.Embed(title="⚙️ TCGTracker Status", color=0x7289DA)
        embed.add_field(name="Online Alert Channel", value=channel.mention if channel else "❌ Not set",                                                     inline=True)
        embed.add_field(name="In-Store Channel",     value=store_channel.mention if store_channel else "⚠️ Not set (uses command channel)",                  inline=True)
        embed.add_field(name="Alert Role",           value=role.mention if role else "❌ Not set",                                                            inline=True)
        embed.add_field(name="Best Buy API Key",     value="✅ Set" if conf.get("bestbuy_key") else "❌ Not set",                                             inline=True)
        embed.add_field(name="Check Interval",       value=f"{interval}s ({interval // 60}m)",                                                               inline=True)
        embed.add_field(name="Tracked Products",     value=str(len(products)),                                                                               inline=True)
        embed.add_field(
            name=f"ZIP Codes ({len(zip_codes)}/{MAX_ZIP_CODES})",
            value=", ".join(f"`{z}`" for z in zip_codes) if zip_codes else "None — add with `tcgset zip <zip>`",
            inline=False,
        )
        await ctx.send(embed=embed)

    # ── Product management ─────────────────────────────────────────────────────

    @commands.command(name="tcgadd")
    @checks.admin_or_permissions(manage_guild=True)
    async def tcgadd(self, ctx: commands.Context, upc: str, msrp: float, *, name: str) -> None:
        """
        Add a product to track by UPC.
        Usage: tcgadd <upc> <msrp> <product name>
        Example: tcgadd 074427166076 44.99 Elite Gengar 9-Pocket PRO-Binder
        """
        upc = upc.strip()
        if not upc.isdigit():
            await ctx.send(embed=self._err("UPC must be numbers only."))
            return
        if msrp <= 0:
            await ctx.send(embed=self._err("MSRP must be a positive number."))
            return

        async with self.config.guild(ctx.guild).products() as products:
            if upc in products:
                await ctx.send(embed=self._err(f"UPC `{upc}` is already tracked. Use `tcgremove {upc}` first."))
                return
            products[upc] = {
                "upc":      upc,
                "name":     name.strip(),
                "msrp":     msrp,
                "added_at": time.time(),
                "alerted":  {},
            }

        embed = discord.Embed(title="✅ Product Added", color=0x2ECC71, description=f"Now tracking **{name}**")
        embed.add_field(name="UPC",  value=upc,            inline=True)
        embed.add_field(name="MSRP", value=f"${msrp:.2f}", inline=True)
        await ctx.send(embed=embed)

    @commands.command(name="tcgremove")
    @checks.admin_or_permissions(manage_guild=True)
    async def tcgremove(self, ctx: commands.Context, upc: str) -> None:
        """Remove a tracked product by UPC."""
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
            await ctx.send(embed=self._err("No products tracked. Use `tcgadd <upc> <msrp> <name>` to add one."))
            return

        product_list = list(products.items())
        for page_start in range(0, len(product_list), MAX_EMBED_FIELDS):
            chunk      = product_list[page_start:page_start + MAX_EMBED_FIELDS]
            page_num   = (page_start // MAX_EMBED_FIELDS) + 1
            total_pages = (len(product_list) + MAX_EMBED_FIELDS - 1) // MAX_EMBED_FIELDS
            title      = f"📋 Tracked Products ({len(products)})"
            if total_pages > 1:
                title += f" — Page {page_num}/{total_pages}"

            embed = discord.Embed(title=title, color=0x3498DB)
            for upc, p in chunk:
                alerted     = p.get("alerted", {})
                in_stock_at = [r for r, v in alerted.items() if v]
                status      = f"🟢 In stock at: {', '.join(in_stock_at)}" if in_stock_at else "🔴 Out of stock"
                added       = datetime.fromtimestamp(p["added_at"]).strftime("%Y-%m-%d") if p.get("added_at") else "Unknown"
                embed.add_field(
                    name=p["name"],
                    value=f"UPC: `{upc}` | MSRP: ${p['msrp']:.2f} | Added: {added}\n{status}",
                    inline=False,
                )
            await ctx.send(embed=embed)

    @commands.command(name="tcgcheck")
    @checks.admin_or_permissions(manage_guild=True)
    async def tcgcheck(self, ctx: commands.Context) -> None:
        """
        Manually check all tracked products right now.
        Shows full current status for every product. Role alerts only fire for new restocks.
        If ZIP codes are configured, also shows nearby Best Buy store inventory.
        """
        if not self._session:
            await ctx.send(embed=self._err("Session not ready — try again in a moment."))
            return

        products = await self.config.guild(ctx.guild).products()
        if not products:
            await ctx.send(embed=self._err("No products tracked."))
            return

        conf            = await self.config.guild(ctx.guild).all()
        bby_key         = conf.get("bestbuy_key", "")
        zip_codes       = conf.get("zip_codes", [])
        channel_id      = conf.get("alert_channel_id")
        store_channel_id = conf.get("store_channel_id")
        role_id         = conf.get("alert_role_id")

        online_channel = ctx.guild.get_channel(channel_id) if channel_id else ctx.channel
        store_channel  = ctx.guild.get_channel(store_channel_id) if store_channel_id else ctx.channel
        role           = ctx.guild.get_role(role_id) if role_id else None

        store_note = f" + in-store checks for {len(zip_codes)} ZIP(s)" if zip_codes else ""
        msg = await ctx.send(embed=discord.Embed(
            description=f"🔍 Checking {len(products)} product(s){store_note}...",
            color=0xF1C40F,
        ))

        found_any = False

        for upc, product in products.items():
            results = await check_bestbuy(self._session, upc, bby_key)

            # Full status summary → online channel
            await self._send_manual_summary(online_channel, product, results)

            # Role-ping alerts for new restocks only
            found_any = await self._process_results(
                ctx.guild, upc, product, results, online_channel, role
            ) or found_any

            # In-store checks per ZIP → store channel
            if zip_codes:
                sku = next((r.get("sku", "") for r in results if r.get("sku")), "")
                for zip_code in zip_codes:
                    store_results = await check_bestbuy_stores(
                        self._session, sku, zip_code, bby_key
                    )
                    if store_results:
                        await self._send_store_embed(store_channel, product, zip_code, store_results)
                    await asyncio.sleep(0.5)

            await asyncio.sleep(1)

        summary = "✅ Done! Role alerts sent for new restocks." if found_any else "✅ Check complete."
        await msg.edit(embed=discord.Embed(description=summary, color=0x2ECC71))

    @commands.command(name="tcgreset")
    @checks.admin_or_permissions(manage_guild=True)
    async def tcgreset(self, ctx: commands.Context, upc: str) -> None:
        """Reset the alert cooldown for a product so it will alert again on next check."""
        upc = upc.strip()
        async with self.config.guild(ctx.guild).products() as products:
            if upc not in products:
                await ctx.send(embed=self._err(f"UPC `{upc}` is not being tracked."))
                return
            products[upc]["alerted"] = {}
            name = products[upc]["name"]
        await ctx.send(embed=self._ok(f"Cooldown reset for **{name}**. Will alert again on next check if in stock."))

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _ok(self, msg: str) -> discord.Embed:
        return discord.Embed(color=0x2ECC71, description=f"✅ {msg}")

    def _err(self, msg: str) -> discord.Embed:
        return discord.Embed(color=0xE74C3C, description=f"❌ {msg}")


async def setup(bot: Red) -> None:
    await bot.add_cog(TCGTracker(bot))
