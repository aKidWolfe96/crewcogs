import discord
import aiohttp
import asyncio
import re
import json
import requests
import functools
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
    """Pull the SKU from a Best Buy product URL.

    Handles multiple formats:
    - /site/product-name/1234567.p        (standard)
    - /site/product-name/1234567.p?...    (with query string)
    - /sku/1234567                        (sku/ path)
    - /product/product-name/SKU/1234567  (product/ path)
    - bare SKU passed directly            (just digits)
    """
    # If it's just a plain number, use it directly
    if re.fullmatch(r"\d{6,}", url.strip()):
        return url.strip()

    patterns = [
        r"/sku/(\d{6,})",           # /sku/12513873
        r"/(\d{6,})\.p",            # /1234567.p
        r"/(\d{6,})\?",             # /1234567?...
        r"/(\d{6,})$",              # ends with digits
        r"[=/](\d{6,})",            # fallback: = or / followed by digits
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

REQUESTS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


def _sync_fetch_status(sku: str) -> dict:
    """
    Synchronous Best Buy stock + price check using requests.
    Returns dict with keys: status, price
    """
    import urllib.parse

    session = requests.Session()
    session.headers.update(REQUESTS_HEADERS)

    status = "UNKNOWN"
    price = None

    try:
        page_url = f"https://www.bestbuy.com/site/searchpage.jsp?st={sku}"
        resp = session.get(page_url, timeout=45)
        text = resp.text

        # Extract status
        if '"buttonState":"ADD_TO_CART"' in text or '"fulfillmentCode":"ADD_TO_CART"' in text:
            status = "ADD_TO_CART"
        elif '"buttonState":"SOLD_OUT"' in text or '"isOutOfStock":true' in text:
            status = "SOLD_OUT"
        elif '"buttonState":"COMING_SOON"' in text:
            status = "COMING_SOON"
        elif '"buttonState":"PRE_ORDER"' in text:
            status = "PRE_ORDER"
        else:
            m = re.search(r'"buttonState"\s*:\s*"([A-Z_]+)"', text)
            if m:
                status = m.group(1).upper()
            elif 'data-button-state="ADD_TO_CART"' in text:
                status = "ADD_TO_CART"
            elif 'data-button-state="SOLD_OUT"' in text:
                status = "SOLD_OUT"

        # Extract price
        price_match = re.search(r'"currentPrice"\s*:\s*([0-9]+\.?[0-9]*)', text)
        if price_match:
            price = float(price_match.group(1))
        if price is None:
            price_match = re.search(r'"salePrice"\s*:\s*([0-9]+\.?[0-9]*)', text)
            if price_match:
                price = float(price_match.group(1))
    except Exception:
        pass

    # Fallback: tcfb endpoint for status only
    if status == "UNKNOWN":
        try:
            paths = json.dumps([[
                "shop", "buttonstate", "v5", "item", "skus",
                int(sku), "conditions", "NONE",
                "destinationZipCode", "55423",
                "storeId", " ", "context", "cyp", "addAll", "false"
            ]])
            params = urllib.parse.urlencode({"paths": paths, "method": "get"})
            url = f"https://www.bestbuy.com/api/tcfb/model.json?{params}"
            resp = session.get(url, timeout=45, headers={**REQUESTS_HEADERS, "Accept": "application/json"})
            if resp.status_code == 200:
                data = resp.json()
                skus_node = (
                    data.get("jsonGraph", {})
                    .get("shop", {}).get("buttonstate", {})
                    .get("v5", {}).get("item", {}).get("skus", {})
                )
                sku_node = skus_node.get(sku) or skus_node.get(int(sku)) or {}
                raw = str(sku_node)
                m = re.search(r"buttonState[^:]*:[^A-Z]*([A-Z_]+)", raw, re.IGNORECASE)
                if m:
                    status = m.group(1).upper()
        except Exception:
            pass

    return {"status": status, "price": price}


async def fetch_product(sku: str) -> dict:
    """Async wrapper — runs the sync requests call in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(_sync_fetch_status, sku))


def _sync_fetch_info(sku: str) -> tuple:
    """Synchronous product name + image fetch using requests."""
    name = f"SKU {sku}"
    image_url = f"https://pisces.bbystatic.com/image2/BestBuy_US/images/products/{sku[:4]}/{sku}_sd.jpg"

    session = requests.Session()
    session.headers.update(REQUESTS_HEADERS)
    try:
        url = f"https://www.bestbuy.com/site/searchpage.jsp?st={sku}"
        resp = session.get(url, timeout=15)
        text = resp.text
        name_match = re.search(r'"name"\s*:\s*"([^"]{5,})"', text)
        if name_match:
            name = name_match.group(1)
        img_match = re.search(r'"href"\s*:\s*"(https://pisces\.bbystatic\.com[^"]+)"', text)
        if img_match:
            image_url = img_match.group(1)
    except Exception:
        pass

    return name, image_url


async def fetch_product_info(sku: str) -> tuple:
    """Async wrapper — runs the sync requests call in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(_sync_fetch_info, sku))

async def fetch_product_info(sku: str) -> tuple:
    """
    Scrape the product name and image URL from the Best Buy product page.
    Returns (name, image_url) -- image_url may be None if not found.
    """
    name = f"SKU {sku}"
    image_url = None

    # Best Buy CDN image URL pattern
    cdn_image = f"https://pisces.bbystatic.com/image2/BestBuy_US/images/products/{sku[:4]}/{sku}_sd.jpg"

    url = f"https://www.bestbuy.com/site/searchpage.jsp?st={sku}"
    try:
        async with _make_session() as session:
         async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
            name_match = re.search(r'"name"\s*:\s*"([^"]{5,})"', text)
            if name_match:
                name = name_match.group(1)
            img_match = re.search(r'"href"\s*:\s*"(https://pisces\.bbystatic\.com[^"]+)"', text)
            if img_match:
                image_url = img_match.group(1)
    except Exception:
        pass

    if not image_url:
        image_url = cdn_image

    return name, image_url


def build_alert_embed(name, sku, status, url, image_url=None, price=None):
    in_stock = status.lower() in ("add_to_cart", "available", "purchasable")
    color = discord.Color.green() if in_stock else discord.Color.red()
    emoji = "\U0001f7e2" if in_stock else "\U0001f534"
    embed = discord.Embed(
        title=f"{emoji} Best Buy Restock Alert",
        description=f"**[{name}]({url})**",
        color=color,
        timestamp=datetime.utcnow(),
    )
    embed.add_field(name="Status", value="\u2705 In Stock \u2014 Buy Now!" if in_stock else "\u274c Out of Stock", inline=True)
    embed.add_field(name="Price", value=f"${price:.2f}" if price else "N/A", inline=True)
    embed.add_field(name="SKU", value=sku, inline=True)
    embed.add_field(name="Link", value=f"[Open on Best Buy]({url})", inline=False)
    embed.set_footer(text="Best Buy Monitor \u2022 bestbuy.com")
    if image_url:
        embed.set_thumbnail(url=image_url)
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

    async def cog_load(self):
        self._task = asyncio.create_task(self._monitor_loop())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

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
        result = await fetch_product(sku)
        current_status = result.get("status", "UNKNOWN")
        current_price = result.get("price")

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
            image_url = info.get("image_url")
            embed = build_alert_embed(name, sku, current_status, url, image_url, current_price)
            try:
                await channel.send(content=ping, embed=embed)
            except discord.Forbidden:
                pass

        # Save updated status
        async with self.config.guild_from_id(guild_id).products() as p:
            if sku in p:
                p[sku]["last_status"] = current_status
                if current_price is not None:
                    p[sku]["last_price"] = current_price

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
            name, image_url = await fetch_product_info(sku)
            result = await fetch_product(sku)
            current_status = result.get("status", "UNKNOWN")
            current_price = result.get("price")

        async with self.config.guild(ctx.guild).products() as products:
            products[sku] = {
                "url": url,
                "name": name,
                "image_url": image_url,
                "last_status": current_status,
                "last_price": current_price,
            }

        in_stock = current_status in {"ADD_TO_CART", "PURCHASABLE", "AVAILABLE"}
        status_str = "\u2705 Currently IN STOCK" if in_stock else "\u274c Currently out of stock"

        embed = discord.Embed(
            title="\U0001f4e6 Product Added to Monitor",
            description=f"**[{name}]({url})**",
            color=discord.Color.blue(),
        )
        embed.add_field(name="SKU", value=sku, inline=True)
        embed.add_field(name="Current Status", value=status_str, inline=True)
        embed.set_footer(text="You'll be alerted when this item restocks.")
        if image_url:
            embed.set_thumbnail(url=image_url)
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
        lines = []
        for sku, info in products.items():
            result = await fetch_product(sku)
            status = result.get("status", "UNKNOWN")
            price = result.get("price") or info.get("last_price")
            name = info.get("name", f"SKU {sku}")
            url = info.get("url", f"https://www.bestbuy.com/site/{sku}.p")
            image_url = info.get("image_url")
            in_stock = status in {"ADD_TO_CART", "PURCHASABLE", "AVAILABLE"}

            async with self.config.guild(ctx.guild).products() as p:
                if sku in p:
                    p[sku]["last_status"] = status
                    if price is not None:
                        p[sku]["last_price"] = price

            if status == "COMING_SOON":
                emoji = "\U0001f7e0"
                status_str = "Coming Soon"
            elif status == "PRE_ORDER":
                emoji = "\U0001f7e3"
                status_str = "Pre-Order"
            elif in_stock:
                emoji = "\U0001f7e2"
                status_str = "In Stock"
            else:
                emoji = "\U0001f534"
                status_str = "Out of Stock"

            price_str = f"${price:.2f}" if price else "N/A"
            lines.append(f"{emoji} **[{name}]({url})**\n\u00a0\u00a0\u00a0Status: {status_str} • Price: {price_str} • SKU: `{sku}`")
            await asyncio.sleep(1)

        embed = discord.Embed(
            title="📊 Stock Check Results",
            description="\n\n".join(lines),
            color=discord.Color.blue(),
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(text="Best Buy Monitor \u2022 bestbuy.com")
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

    # --- Debug ---

    @tcgc.command(name="debug")
    async def tcgc_debug(self, ctx: commands.Context, sku: str):
        """Show raw response from Best Buy for a SKU to diagnose issues.

        Example: `[p]tcgc debug 6562319`
        """
        import urllib.parse
        import traceback

        await ctx.send(f"Running debug check on SKU {sku}...")

        def safe(text, limit=400):
            return str(text).replace("`", "'")[:limit]

        def sync_debug(sku):
            import urllib.parse
            results = []

            # Test 0: basic connectivity
            try:
                r = requests.get("https://httpbin.org/get", timeout=10, headers=REQUESTS_HEADERS)
                results.append(f"CONNECTIVITY: OK status={r.status_code}")
            except Exception as e:
                results.append(f"CONNECTIVITY FAILED: {type(e).__name__}: {safe(str(e))}")

            # Test 1: Homepage cookies
            try:
                session = requests.Session()
                session.headers.update(REQUESTS_HEADERS)
                r = session.get("https://www.bestbuy.com/", timeout=15)
                cookies = list(session.cookies.keys())
                results.append(f"HOMEPAGE: status={r.status_code} cookies={cookies} length={len(r.text)}")
            except Exception as e:
                results.append(f"HOMEPAGE FAILED: {type(e).__name__}: {safe(str(e))}")

            # Test 2: tcfb endpoint
            try:
                paths = json.dumps([[
                    "shop", "buttonstate", "v5", "item", "skus",
                    int(sku), "conditions", "NONE",
                    "destinationZipCode", "55423",
                    "storeId", " ", "context", "cyp", "addAll", "false"
                ]])
                params = urllib.parse.urlencode({"paths": paths, "method": "get"})
                url = f"https://www.bestbuy.com/api/tcfb/model.json?{params}"
                r = session.get(url, timeout=15)
                results.append(f"TCFB: status={r.status_code} body={safe(r.text)}")
            except Exception as e:
                results.append(f"TCFB FAILED: {type(e).__name__}: {safe(str(e))}")

            # Test 3: product search page
            try:
                page_url = f"https://www.bestbuy.com/site/searchpage.jsp?st={sku}"
                r = session.get(page_url, timeout=20)
                text = r.text
                btn_matches = re.findall(r'"buttonState"\s*:\s*"([^"]+)"', text)
                results.append(
                    f"PRODUCT PAGE: status={r.status_code} length={len(text)}\n"
                    f"buttonState matches: {btn_matches[:5] if btn_matches else 'none'}\n"
                    f"First 300 chars: {safe(text, 300)}"
                )
            except Exception as e:
                results.append(f"PRODUCT PAGE FAILED: {type(e).__name__}: {safe(str(e))}")

            return results

        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, functools.partial(sync_debug, sku))
        for r in results:
            await ctx.send(r[:1990])
