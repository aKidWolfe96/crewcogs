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

async def fetch_product(session: aiohttp.ClientSession, sku: str) -> str:
    """
    Check Best Buy online stock status for a SKU.
    Uses the internal tcfb buttonstate endpoint with correct URL format,
    then falls back to scraping the product page if that fails.
    Returns a status string: ADD_TO_CART, SOLD_OUT, COMING_SOON, or UNKNOWN.
    """
    # Primary: internal buttonstate endpoint (correct unencoded format)
    try:
        import urllib.parse
        paths = [[
            "shop", "buttonstate", "v5", "item", "skus",
            int(sku), "conditions", "NONE",
            "destinationZipCode", "55423",
            "storeId", " ",
            "context", "cyp",
            "addAll", "false"
        ]]
        params = urllib.parse.urlencode({
            "paths": str(paths).replace("'", '"'),
            "method": "get"
        })
        url = f"https://www.bestbuy.com/api/tcfb/model.json?{params}"
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                # Walk the nested jsonGraph response
                skus_node = (
                    data.get("jsonGraph", {})
                    .get("shop", {})
                    .get("buttonstate", {})
                    .get("v5", {})
                    .get("item", {})
                    .get("skus", {})
                )
                # SKU key may be int or string in response
                sku_node = skus_node.get(sku) or skus_node.get(int(sku)) or {}
                raw = str(sku_node)
                match = re.search(r"buttonState[^:]*:\s*[^A-Z]*([A-Z_]+)", raw, re.IGNORECASE)
                if match:
                    return match.group(1).upper()
    except Exception:
        pass

    # Fallback: scrape the product page directly
    try:
        page_url = f"https://www.bestbuy.com/site/product/{sku}.p"
        async with session.get(page_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            text = await resp.text()
            # Look for fulfillment/button state signals in page JSON
            if '"buttonState":"ADD_TO_CART"' in text or '"fulfillmentCode":"ADD_TO_CART"' in text:
                return "ADD_TO_CART"
            if '"buttonState":"SOLD_OUT"' in text or '"isOutOfStock":true' in text:
                return "SOLD_OUT"
            if '"buttonState":"COMING_SOON"' in text:
                return "COMING_SOON"
            if '"buttonState":"PRE_ORDER"' in text:
                return "PRE_ORDER"
            # Broader search across all JSON blobs on the page
            match = re.search(r'"buttonState"\s*:\s*"([A-Z_]+)"', text)
            if match:
                return match.group(1).upper()
            # Last resort: check if add to cart button exists in HTML
            if 'data-button-state="ADD_TO_CART"' in text:
                return "ADD_TO_CART"
            if 'data-button-state="SOLD_OUT"' in text:
                return "SOLD_OUT"
    except Exception:
        pass

    return "UNKNOWN"

async def fetch_product_info(session: aiohttp.ClientSession, sku: str) -> tuple:
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


def build_alert_embed(name, sku, status, url, image_url=None):
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
        current_status = await fetch_product(self._session, sku)
        if not current_status:
            current_status = "UNKNOWN"

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
            embed = build_alert_embed(name, sku, current_status, url, image_url)
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
            name, image_url = await fetch_product_info(self._session, sku)
            current_status = await fetch_product(self._session, sku)

        async with self.config.guild(ctx.guild).products() as products:
            products[sku] = {
                "url": url,
                "name": name,
                "image_url": image_url,
                "last_status": current_status,
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
        results = []
        for sku, info in products.items():
            status = await fetch_product(self._session, sku)
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

    # --- Debug ---

    @tcgc.command(name="debug")
    async def tcgc_debug(self, ctx: commands.Context, sku: str):
        """Show raw response from Best Buy for a SKU to diagnose issues.

        Example: `[p]tcgc debug 6562319`
        """
        import urllib.parse
        import traceback
        import ssl

        await ctx.send(f"Running debug check on SKU {sku}...")

        def safe(text, limit=300):
            # Strip backticks to prevent Discord formatting issues
            return text.replace("`", "'")[:limit]

        # Test 0: basic connectivity
        try:
            async with self._session.get("https://httpbin.org/get", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                await ctx.send(f"CONNECTIVITY: OK (status {resp.status})")
        except Exception as e:
            tb = safe(traceback.format_exc())
            await ctx.send("CONNECTIVITY FAILED\nType: " + type(e).__name__ + "\nMsg: " + safe(str(e)) + "\nTrace:\n" + tb)

        # Test 1: tcfb endpoint
        try:
            paths = [[
                "shop", "buttonstate", "v5", "item", "skus",
                int(sku), "conditions", "NONE",
                "destinationZipCode", "55423",
                "storeId", " ",
                "context", "cyp",
                "addAll", "false"
            ]]
            params = urllib.parse.urlencode({
                "paths": json.dumps(paths),
                "method": "get"
            })
            url = f"https://www.bestbuy.com/api/tcfb/model.json?{params}"
            async with self._session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                text = await resp.text()
                await ctx.send(f"TCFB ENDPOINT: status={resp.status} body={safe(text)}")
        except Exception as e:
            tb = safe(traceback.format_exc())
            await ctx.send("TCFB FAILED\nType: " + type(e).__name__ + "\nMsg: " + safe(str(e)) + "\nTrace:\n" + tb)

        # Test 2: product page, ssl relaxed
        try:
            page_url = f"https://www.bestbuy.com/site/searchpage.jsp?st={sku}"
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            async with self._session.get(page_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20), ssl=ssl_ctx) as resp:
                text = await resp.text()
                btn_matches = re.findall(r'"buttonState"\s*:\s*"([^"]+)"', text)
                msg = ("PRODUCT PAGE: status=" + str(resp.status) +
                       " length=" + str(len(text)) + "\n" +
                       "buttonState matches: " + str(btn_matches[:5] if btn_matches else "none") + "\n" +
                       "First 300 chars: " + safe(text))
                await ctx.send(msg[:1990])
        except Exception as e:
            tb = safe(traceback.format_exc())
            await ctx.send("PRODUCT PAGE FAILED\nType: " + type(e).__name__ + "\nMsg: " + safe(str(e)) + "\nTrace:\n" + tb)
