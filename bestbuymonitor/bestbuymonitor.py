import discord
import aiohttp
import asyncio
import re
import json
from datetime import datetime
from redbot.core import commands, Config
from redbot.core.bot import Red

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

def extract_sku(url: str) -> str | None:
    """Pull the SKU from a Best Buy product URL."""
    match = re.search(r"/(\d{7,})/", url)
    return match.group(1) if match else None

async def fetch_product(session: aiohttp.ClientSession, sku: str) -> dict | None:
    """
    Hit Best Buy's internal fulfillment API (no key required) to get
    stock + product info for a given SKU.
    """
    url = (
        f"https://www.bestbuy.com/api/tcfb/model.json"
        f"?paths=%5B%5B%22shop%22%2C%22buttonstate%22%2C%22v5%22%2C%22item%22%2C%22skus%22%2C{sku}%2C%22conditions%22%2C%22NONE%22%2C%22destinationZipCode%22%2C%2255423%22%2C%22storeId%22%2C%22+%22%2C%22context%22%2C%22cyp%22%2C%22addAll%22%2C%22false%22%5D%5D"
        f"&method=get"
    )
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            btn = (
                data.get("jsonGraph", {})
                .get("shop", {})
                .get("buttonstate", {})
                .get("v5", {})
                .get("item", {})
                .get("skus", {})
                .get(sku, {})
                .get("conditions", {})
                .get("NONE", {})
            )
            # Walk through nested $type/value structure
            def unwrap(node):
                if isinstance(node, dict):
                    if "$type" in node:
                        return node.get("value")
                    return {k: unwrap(v) for k, v in node.items()}
                return node

            btn = unwrap(btn)
            if not btn:
                return None
            return btn
    except Exception:
        return None

async def fetch_product_name(session: aiohttp.ClientSession, sku: str) -> str:
    """Scrape the product name from the Best Buy product page."""
    url = f"https://www.bestbuy.com/site/searchpage.jsp?st={sku}"
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
            match = re.search(r'"name"\s*:\s*"([^"]{5,})"', text)
            if match:
                return match.group(1)
    except Exception:
        pass
    return f"SKU {sku}"

def build_alert_embed(name: str, sku: str, status: str, url: str) -> discord.Embed:
    in_stock = status.lower() in ("add_to_cart", "available", "purchasable")
    color = discord.Color.green() if in_stock else discord.Color.red()
    emoji = "🟢" if in_stock else "🔴"
    embed = discord.Embed(
        title=f"{emoji} Best Buy Restock Alert",
        description=f"**[{name}]({url})**",
        color=color,
        timestamp=datetime.utcnow(),
    )
    embed.add_field(name="Status", value="✅ In Stock — Buy Now!" if in_stock else "❌ Out of Stock", inline=True)
    embed.add_field(name="SKU", value=sku, inline=True)
    embed.add_field(name="Link", value=f"[Open on Best Buy]({url})", inline=False)
    embed.set_footer(text="Best Buy Monitor • bestbuy.com")
    return embed


class BestBuyMonitor(commands.Cog):
    """Monitor Best Buy product pages for restock alerts."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=8675309420, force_registration=True)
        self.config.register_guild(
            alert_channel=None,
            ping_target=None,       # None | "everyone" | "here" | role_id (int)
            products={},            # { sku: { "url": str, "name": str, "last_status": str } }
            check_interval=300,     # seconds between checks (default 5 min)
        )
        self._task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._monitor_loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------ #
    #  Background loop                                                     #
    # ------------------------------------------------------------------ #

    async def _monitor_loop(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                await self._check_all_guilds()
            except Exception:
                pass
            # Use the shortest interval across all guilds so we don't miss anything
            all_intervals = []
            all_guilds = await self.config.all_guilds()
            for gdata in all_guilds.values():
                all_intervals.append(gdata.get("check_interval", 300))
            interval = min(all_intervals) if all_intervals else 300
            await asyncio.sleep(interval)

    async def _check_all_guilds(self):
        all_guilds = await self.config.all_guilds()
        for guild_id, gdata in all_guilds.items():
            channel_id = gdata.get("alert_channel")
            products = gdata.get("products", {})
            ping_target = gdata.get("ping_target")
            if not channel_id or not products:
                continue
            channel = self.bot.get_channel(channel_id)
            if not channel:
                continue
            for sku, info in products.items():
                await self._check_product(guild_id, channel, sku, info, ping_target)
                await asyncio.sleep(2)  # small delay between SKU checks

    async def _check_product(self, guild_id, channel, sku, info, ping_target):
        btn = await fetch_product(self._session, sku)
        if not btn:
            return

        # Best Buy button state key tells us availability
        button_state = btn.get("destinationZipCode", {})
        # Flatten — look for buttonState key anywhere in the response
        raw = json.dumps(btn)
        state_match = re.search(r'"buttonState"\s*:\s*"([^"]+)"', raw)
        current_status = state_match.group(1) if state_match else "UNKNOWN"

        last_status = info.get("last_status", "")
        url = info.get("url", f"https://www.bestbuy.com/site/{sku}.p")
        name = info.get("name", f"SKU {sku}")

        # Only alert when status CHANGES to in-stock
        in_stock_states = {"ADD_TO_CART", "PURCHASABLE", "AVAILABLE"}
        just_restocked = (
            current_status in in_stock_states
            and last_status not in in_stock_states
            and last_status != ""
        )

        if just_restocked:
            ping = self._build_ping(channel.guild, ping_target)
            embed = build_alert_embed(name, sku, current_status, url)
            try:
                await channel.send(content=ping, embed=embed)
            except discord.Forbidden:
                pass

        # Save updated status
        async with self.config.guild_from_id(guild_id).products() as p:
            if sku in p:
                p[sku]["last_status"] = current_status

    def _build_ping(self, guild: discord.Guild, ping_target) -> str | None:
        if ping_target is None:
            return None
        if ping_target == "everyone":
            return "@everyone"
        if ping_target == "here":
            return "@here"
        # It's a role ID
        role = guild.get_role(int(ping_target))
        return role.mention if role else None

    # ------------------------------------------------------------------ #
    #  Commands                                                            #
    # ------------------------------------------------------------------ #

    @commands.group(name="tcgc", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def tcgc(self, ctx: commands.Context):
        """TCG Checker — monitor Best Buy for Pokémon card restocks."""
        await ctx.send_help()

    # --- Channel setup ---

    @tcgc.command(name="setchannel")
    async def tcgc_setchannel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Set the channel where restock alerts will be posted.

        Leave blank to use the current channel.
        Example: `[p]tcgc setchannel #restock-alerts`
        """
        target = channel or ctx.channel
        await self.config.guild(ctx.guild).alert_channel.set(target.id)
        await ctx.send(f"✅ Restock alerts will be posted in {target.mention}.")

    # --- Ping setup ---

    @tcgc.command(name="setping")
    async def tcgc_setping(self, ctx: commands.Context, *, target: str = None):
        """Set who gets pinged on a restock alert.

        Options:
        • `everyone` — pings @everyone
        • `here`     — pings @here
        • `@RoleName` or a role ID — pings that role
        • `none`     — no ping, just posts the embed

        Example: `[p]tcgc setping everyone`
        Example: `[p]tcgc setping @Collectors`
        """
        if target is None or target.lower() == "none":
            await self.config.guild(ctx.guild).ping_target.set(None)
            return await ctx.send("✅ Ping disabled — alerts will post silently.")

        if target.lower() == "everyone":
            await self.config.guild(ctx.guild).ping_target.set("everyone")
            return await ctx.send("✅ Will ping @everyone on restocks.")

        if target.lower() == "here":
            await self.config.guild(ctx.guild).ping_target.set("here")
            return await ctx.send("✅ Will ping @here on restocks.")

        # Try to resolve a role
        role = None
        role_id_match = re.search(r"\d+", target)
        if role_id_match:
            role = ctx.guild.get_role(int(role_id_match.group()))
        if not role:
            role = discord.utils.find(lambda r: r.name.lower() == target.lower(), ctx.guild.roles)

        if role:
            await self.config.guild(ctx.guild).ping_target.set(role.id)
            return await ctx.send(f"✅ Will ping {role.mention} on restocks.")

        await ctx.send("❌ Couldn't find that role. Try `everyone`, `here`, `none`, or a valid role name/mention.")

    # --- Add product ---

    @tcgc.command(name="add")
    async def tcgc_add(self, ctx: commands.Context, url: str):
        """Add a Best Buy product URL to monitor.

        Example: `[p]tcgc add https://www.bestbuy.com/site/pokemon.../1234567.p`
        """
        sku = extract_sku(url)
        if not sku:
            return await ctx.send("❌ Couldn't extract a SKU from that URL. Make sure it's a valid Best Buy product link.")

        async with ctx.typing():
            msg = await ctx.send(f"🔍 Looking up SKU `{sku}`...")
            name = await fetch_product_name(self._session, sku)
            btn = await fetch_product(self._session, sku)

        raw = json.dumps(btn) if btn else ""
        state_match = re.search(r'"buttonState"\s*:\s*"([^"]+)"', raw)
        current_status = state_match.group(1) if state_match else "UNKNOWN"

        async with self.config.guild(ctx.guild).products() as products:
            products[sku] = {
                "url": url,
                "name": name,
                "last_status": current_status,
            }

        in_stock = current_status in {"ADD_TO_CART", "PURCHASABLE", "AVAILABLE"}
        status_str = "✅ Currently IN STOCK" if in_stock else "❌ Currently out of stock"

        embed = discord.Embed(
            title="📦 Product Added to Monitor",
            description=f"**[{name}]({url})**",
            color=discord.Color.blue(),
        )
        embed.add_field(name="SKU", value=sku, inline=True)
        embed.add_field(name="Current Status", value=status_str, inline=True)
        embed.set_footer(text="You'll be alerted when this item restocks.")
        await msg.edit(content=None, embed=embed)

    # --- Remove product ---

    @tcgc.command(name="remove")
    async def tcgc_remove(self, ctx: commands.Context, sku: str):
        """Remove a product from the monitor by SKU.

        Example: `[p]tcgc remove 1234567`
        """
        async with self.config.guild(ctx.guild).products() as products:
            if sku not in products:
                return await ctx.send(f"❌ SKU `{sku}` isn't being monitored.")
            name = products[sku].get("name", sku)
            del products[sku]

        await ctx.send(f"✅ Removed **{name}** (`{sku}`) from the monitor.")

    # --- List products ---

    @tcgc.command(name="list")
    async def tcgc_list(self, ctx: commands.Context):
        """Show all products currently being monitored."""
        products = await self.config.guild(ctx.guild).products()
        channel_id = await self.config.guild(ctx.guild).alert_channel()
        ping_target = await self.config.guild(ctx.guild).ping_target()
        interval = await self.config.guild(ctx.guild).check_interval()

        channel_str = f"<#{channel_id}>" if channel_id else "❌ Not set — use `[p]tcgc setchannel`"
        if ping_target is None:
            ping_str = "None (silent)"
        elif ping_target in ("everyone", "here"):
            ping_str = f"@{ping_target}"
        else:
            role = ctx.guild.get_role(int(ping_target))
            ping_str = role.mention if role else f"Unknown role ({ping_target})"

        embed = discord.Embed(
            title="🛒 TCG Checker — Active Watchlist",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Alert Channel", value=channel_str, inline=True)
        embed.add_field(name="Ping", value=ping_str, inline=True)
        embed.add_field(name="Check Interval", value=f"Every {interval // 60} min", inline=True)

        if not products:
            embed.description = "No products being monitored. Use `[p]tcgc add <url>` to add one."
        else:
            lines = []
            for sku, info in products.items():
                name = info.get("name", sku)
                status = info.get("last_status", "UNKNOWN")
                in_stock = status in {"ADD_TO_CART", "PURCHASABLE", "AVAILABLE"}
                emoji = "🟢" if in_stock else "🔴"
                lines.append(f"{emoji} **[{name}]({info.get('url', '')})**\nSKU: `{sku}` • Status: `{status}`")
            embed.description = "\n\n".join(lines)

        await ctx.send(embed=embed)

    # --- Check now ---

    @tcgc.command(name="check")
    async def tcgc_check(self, ctx: commands.Context):
        """Manually trigger an immediate stock check for all monitored products."""
        products = await self.config.guild(ctx.guild).products()
        if not products:
            return await ctx.send("No products being monitored.")

        msg = await ctx.send(f"🔄 Checking {len(products)} product(s)...")
        results = []
        for sku, info in products.items():
            btn = await fetch_product(self._session, sku)
            raw = json.dumps(btn) if btn else ""
            state_match = re.search(r'"buttonState"\s*:\s*"([^"]+)"', raw)
            status = state_match.group(1) if state_match else "UNKNOWN"
            in_stock = status in {"ADD_TO_CART", "PURCHASABLE", "AVAILABLE"}
            emoji = "🟢" if in_stock else "🔴"
            name = info.get("name", sku)
            results.append(f"{emoji} **{name}** — `{status}`")

            async with self.config.guild(ctx.guild).products() as p:
                if sku in p:
                    p[sku]["last_status"] = status
            await asyncio.sleep(1)

        embed = discord.Embed(
            title="📊 Manual Stock Check Results",
            description="\n".join(results),
            color=discord.Color.blue(),
            timestamp=datetime.utcnow(),
        )
        await msg.edit(content=None, embed=embed)

    # --- Set interval ---

    @tcgc.command(name="setinterval")
    async def tcgc_setinterval(self, ctx: commands.Context, minutes: int):
        """Set how often (in minutes) Best Buy is checked.

        Minimum: 1 minute. Default: 5 minutes.
        Example: `[p]tcgc setinterval 3`
        """
        if minutes < 1:
            return await ctx.send("❌ Minimum interval is 1 minute.")
        await self.config.guild(ctx.guild).check_interval.set(minutes * 60)
        await ctx.send(f"✅ Check interval set to every **{minutes} minute(s)**.")

    # --- Status / settings summary ---

    @tcgc.command(name="settings")
    async def tcgc_settings(self, ctx: commands.Context):
        """Show current monitor settings."""
        await self.tcgc_list(ctx)
