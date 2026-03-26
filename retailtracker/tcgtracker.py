"""
TCGTracker – Red-DiscordBot Cog
Tracks Pokémon TCG product drops across major retailers by UPC.

Admin commands:
  [p]tcgset channel #channel    — set alert channel
  [p]tcgset role @role          — set ping role
  [p]tcgset bestbuy_key <key>   — set Best Buy API key
  [p]tcgset walmart_key <key>   — set Walmart API key
  [p]tcgset interval <seconds>  — set check interval (default 300)

Product commands (admin only):
  [p]tcgadd <upc> <msrp> <name> — add a product to track
  [p]tcgremove <upc>            — remove a tracked product
  [p]tcglist                    — list all tracked products
  [p]tcgcheck                   — manually trigger a check right now
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Dict, Optional

import aiohttp
import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red

from .retailers import check_all_retailers

RETAILER_EMOJIS = {
    "Best Buy":       "💛",
    "Walmart":        "💙",
    "Target":         "🎯",
    "GameStop":       "🎮",
    "Pokémon Center": "🔴",
}

CHECK_INTERVAL_DEFAULT = 300  # 5 minutes


class TCGTracker(commands.Cog):
    """Tracks Pokémon TCG product restocks across major retailers by UPC."""

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        self._check_task: Optional[asyncio.Task] = None

        self.config = Config.get_conf(self, identifier=0x54434754524B52, force_registration=True)

        default_guild = {
            "alert_channel_id": None,
            "alert_role_id": None,
            "bestbuy_key": "",
            "walmart_key": "",
            "check_interval": CHECK_INTERVAL_DEFAULT,
            # Dict of upc -> { "name": str, "msrp": float, "added_at": float,
            #                   "alerted": { retailer: bool } }
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
            except Exception as e:
                pass  # Keep loop alive regardless
            # Re-read interval in case it was changed
            for guild in self.bot.guilds:
                interval = await self.config.guild(guild).check_interval()
                await asyncio.sleep(interval)
                break
            else:
                await asyncio.sleep(CHECK_INTERVAL_DEFAULT)

    async def _run_checks(self) -> None:
        """Check all guilds and all tracked products."""
        for guild in self.bot.guilds:
            guild_conf = await self.config.guild(guild).all()
            products   = guild_conf.get("products", {})
            if not products:
                continue

            channel_id = guild_conf.get("alert_channel_id")
            role_id    = guild_conf.get("alert_role_id")
            bby_key    = guild_conf.get("bestbuy_key", "")
            wmt_key    = guild_conf.get("walmart_key", "")

            channel = guild.get_channel(channel_id) if channel_id else None
            role    = guild.get_role(role_id) if role_id else None

            for upc, product in products.items():
                results = await check_all_retailers(
                    self._session,
                    upc,
                    product["name"],
                    bestbuy_key=bby_key,
                    walmart_key=wmt_key,
                )

                alerted: Dict[str, bool] = product.get("alerted", {})
                msrp = product.get("msrp", 0)
                changed = False

                for result in results:
                    retailer  = result["retailer"]
                    in_stock  = result["in_stock"]
                    was_alerted = alerted.get(retailer, False)

                    if in_stock and not was_alerted:
                        # New restock — fire alert
                        if channel:
                            await self._send_alert(channel, role, product, result, msrp)
                        alerted[retailer] = True
                        changed = True
                    elif not in_stock and was_alerted:
                        # Back out of stock — reset cooldown so next restock alerts again
                        alerted[retailer] = False
                        changed = True

                if changed:
                    async with self.config.guild(guild).products() as saved:
                        if upc in saved:
                            saved[upc]["alerted"] = alerted

                # Small delay between products to be polite to retailers
                await asyncio.sleep(2)

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

        # Price comparison
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
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="UPC", value=product.get("upc", "N/A"), inline=True)
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
        """Set the channel where drop alerts will be posted."""
        await self.config.guild(ctx.guild).alert_channel_id.set(channel.id)
        await ctx.send(embed=self._ok(f"Alert channel set to {channel.mention}."))

    @tcgset.command(name="role")
    async def tcgset_role(self, ctx: commands.Context, role: discord.Role) -> None:
        """Set the role that gets pinged on drop alerts."""
        await self.config.guild(ctx.guild).alert_role_id.set(role.id)
        await ctx.send(embed=self._ok(f"Alert role set to {role.mention}."))

    @tcgset.command(name="bestbuy_key")
    async def tcgset_bby(self, ctx: commands.Context, key: str) -> None:
        """Set your Best Buy API key (free at developer.bestbuy.com)."""
        await self.config.guild(ctx.guild).bestbuy_key.set(key)
        await ctx.message.delete()  # Delete to hide key from chat
        await ctx.send(embed=self._ok("Best Buy API key saved. Message deleted for security."))

    @tcgset.command(name="walmart_key")
    async def tcgset_wmt(self, ctx: commands.Context, key: str) -> None:
        """Set your Walmart API key (free at developer.walmartlabs.com)."""
        await self.config.guild(ctx.guild).walmart_key.set(key)
        await ctx.message.delete()
        await ctx.send(embed=self._ok("Walmart API key saved. Message deleted for security."))

    @tcgset.command(name="interval")
    async def tcgset_interval(self, ctx: commands.Context, seconds: int) -> None:
        """Set how often to check for restocks in seconds (minimum 60)."""
        seconds = max(60, seconds)
        await self.config.guild(ctx.guild).check_interval.set(seconds)
        await ctx.send(embed=self._ok(f"Check interval set to **{seconds}s** ({seconds//60}m {seconds%60}s)."))

    @tcgset.command(name="status")
    async def tcgset_status(self, ctx: commands.Context) -> None:
        """Show current TCGTracker configuration."""
        conf = await self.config.guild(ctx.guild).all()
        channel = ctx.guild.get_channel(conf["alert_channel_id"])
        role    = ctx.guild.get_role(conf["alert_role_id"])
        bby     = "✅ Set" if conf.get("bestbuy_key") else "❌ Not set"
        wmt     = "✅ Set" if conf.get("walmart_key") else "❌ Not set"
        interval = conf.get("check_interval", CHECK_INTERVAL_DEFAULT)
        products = conf.get("products", {})

        embed = discord.Embed(title="⚙️ TCGTracker Status", color=0x7289DA)
        embed.add_field(name="Alert Channel",    value=channel.mention if channel else "❌ Not set", inline=True)
        embed.add_field(name="Alert Role",       value=role.mention if role else "❌ Not set",       inline=True)
        embed.add_field(name="Check Interval",   value=f"{interval}s ({interval//60}m)",             inline=True)
        embed.add_field(name="Best Buy API Key", value=bby, inline=True)
        embed.add_field(name="Walmart",          value="✅ Scraping (no key needed)", inline=True)
        embed.add_field(name="Tracked Products", value=str(len(products)), inline=True)
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
                "upc": upc,
                "name": name.strip(),
                "msrp": msrp,
                "added_at": time.time(),
                "alerted": {},
            }

        embed = discord.Embed(
            title="✅ Product Added",
            color=0x2ECC71,
            description=f"Now tracking **{name}**",
        )
        embed.add_field(name="UPC",  value=upc,            inline=True)
        embed.add_field(name="MSRP", value=f"${msrp:.2f}", inline=True)
        embed.set_footer(text="Checks run every 5 minutes across all retailers.")
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

        embed = discord.Embed(
            title=f"📋 Tracked Products ({len(products)})",
            color=0x3498DB,
        )
        for upc, p in products.items():
            alerted    = p.get("alerted", {})
            in_stock_at = [r for r, v in alerted.items() if v]
            status = (
                f"🟢 In stock at: {', '.join(in_stock_at)}"
                if in_stock_at
                else "🔴 Out of stock everywhere"
            )
            added = datetime.fromtimestamp(p["added_at"]).strftime("%Y-%m-%d") if p.get("added_at") else "Unknown"
            embed.add_field(
                name=f"{p['name']}",
                value=f"UPC: `{upc}` | MSRP: ${p['msrp']:.2f} | Added: {added}\n{status}",
                inline=False,
            )
        await ctx.send(embed=embed)

    @commands.command(name="tcgcheck")
    @checks.admin_or_permissions(manage_guild=True)
    async def tcgcheck(self, ctx: commands.Context) -> None:
        """Manually trigger an immediate check of all tracked products."""
        products = await self.config.guild(ctx.guild).products()
        if not products:
            await ctx.send(embed=self._err("No products are being tracked."))
            return

        msg = await ctx.send(embed=discord.Embed(
            description=f"🔍 Checking {len(products)} product(s) across all retailers...",
            color=0xF1C40F,
        ))

        conf    = await self.config.guild(ctx.guild).all()
        bby_key = conf.get("bestbuy_key", "")
        wmt_key = conf.get("walmart_key", "")
        channel_id = conf.get("alert_channel_id")
        role_id    = conf.get("alert_role_id")
        channel = ctx.guild.get_channel(channel_id) if channel_id else ctx.channel
        role    = ctx.guild.get_role(role_id) if role_id else None

        found_any = False
        for upc, product in products.items():
            results = await check_all_retailers(
                self._session, upc, product["name"],
                bestbuy_key=bby_key, walmart_key=wmt_key,
            )
            alerted: dict = product.get("alerted", {})
            changed = False

            for result in results:
                retailer = result["retailer"]
                in_stock = result["in_stock"]
                was_alerted = alerted.get(retailer, False)

                if in_stock and not was_alerted:
                    await self._send_alert(channel, role, product, result, product["msrp"])
                    alerted[retailer] = True
                    changed = True
                    found_any = True
                elif not in_stock and was_alerted:
                    alerted[retailer] = False
                    changed = True

            if changed:
                async with self.config.guild(ctx.guild).products() as saved:
                    if upc in saved:
                        saved[upc]["alerted"] = alerted

            await asyncio.sleep(1)

        summary = "✅ Check complete! Alerts sent for new restocks." if found_any else "✅ Check complete. Nothing new in stock right now."
        await msg.edit(embed=discord.Embed(description=summary, color=0x2ECC71))

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

        await ctx.send(embed=self._ok(f"Alert cooldown reset for **{name}**. It will alert again on the next check if in stock."))

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _ok(self, msg: str) -> discord.Embed:
        return discord.Embed(color=0x2ECC71, description=f"✅ {msg}")

    def _err(self, msg: str) -> discord.Embed:
        return discord.Embed(color=0xE74C3C, description=f"❌ {msg}")


async def setup(bot: Red) -> None:
    await bot.add_cog(TCGTracker(bot))
