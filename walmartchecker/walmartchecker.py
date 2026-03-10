import discord
from redbot.core import commands
from redbot.core.bot import Red
import aiohttp
import asyncio
import re
import json
from typing import Optional


class WalmartChecker(commands.Cog):
    """Check Walmart item stock by item number or search by name."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_json(
        self, session: aiohttp.ClientSession, url: str, params: dict = None
    ) -> Optional[dict]:
        try:
            async with session.get(
                url,
                params=params,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
        except Exception:
            pass
        return None

    async def _scrape_next_data(
        self, session: aiohttp.ClientSession, url: str
    ) -> Optional[dict]:
        """Fetch a Walmart page and extract the embedded __NEXT_DATA__ JSON."""
        try:
            async with session.get(
                url,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    match = re.search(
                        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                        html,
                        re.DOTALL,
                    )
                    if match:
                        return json.loads(match.group(1))
        except Exception:
            pass
        return None

    def _parse_product(self, product: dict) -> Optional[dict]:
        """Normalise a raw Walmart product dict into a clean dict."""
        if not product:
            return None
        try:
            item_id = str(product.get("usItemId") or product.get("id") or "")
            name = product.get("name") or product.get("title") or "Unknown"

            price_raw = (
                product.get("priceInfo", {}).get("currentPrice", {}).get("price")
                or product.get("price")
            )
            if isinstance(price_raw, (int, float)):
                price = f"${price_raw:.2f}"
            elif price_raw:
                price = str(price_raw)
            else:
                price = "N/A"

            avail = str(
                product.get("availabilityStatus")
                or product.get("fulfillmentStatus")
                or ""
            ).upper()

            if avail == "IN_STOCK":
                in_stock = True
                avail_text = "✅ In Stock"
            elif avail == "LIMITED_STOCK":
                in_stock = True
                avail_text = "⚠️ Limited Stock"
            else:
                in_stock = False
                avail_text = "❌ Out of Stock"

            image_url = None
            imgs = product.get("imageInfo", {})
            if imgs:
                image_url = imgs.get("thumbnailUrl") or (
                    imgs.get("allImages", [{}])[0].get("url") if imgs.get("allImages") else None
                )
            if not image_url:
                image_url = product.get("image") or product.get("imageUrl")

            url = (
                f"https://www.walmart.com/ip/{item_id}"
                if item_id
                else "https://www.walmart.com"
            )

            return {
                "id": item_id,
                "name": name[:200],
                "price": price,
                "in_stock": in_stock,
                "avail": avail_text,
                "image_url": image_url,
                "url": url,
            }
        except Exception:
            return None

    async def _lookup_by_id(self, item_id: str) -> Optional[dict]:
        """Look up a single Walmart item by its numeric item ID."""
        api_url = (
            f"https://www.walmart.com/orchestra/home/pages/item?itemId={item_id}"
        )
        page_url = f"https://www.walmart.com/ip/{item_id}"

        async with aiohttp.ClientSession() as session:
            data = await self._get_json(session, api_url)
            if data:
                try:
                    product = data["props"]["pageProps"]["initialData"]["data"]["product"]
                    return self._parse_product(product)
                except (KeyError, TypeError):
                    pass

            raw = await self._scrape_next_data(session, page_url)
            if raw:
                try:
                    product = raw["props"]["pageProps"]["initialData"]["data"]["product"]
                    return self._parse_product(product)
                except (KeyError, TypeError):
                    pass

        return None

    async def _search(self, query: str, limit: int = 5) -> list:
        """Search Walmart by keyword and return a list of parsed products."""
        url = "https://www.walmart.com/search"
        params = {"q": query, "affinityOverride": "default"}

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(
                    url,
                    params=params,
                    headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        match = re.search(
                            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                            html,
                            re.DOTALL,
                        )
                        if match:
                            raw = json.loads(match.group(1))
                            items = (
                                raw.get("props", {})
                                .get("pageProps", {})
                                .get("initialData", {})
                                .get("searchResult", {})
                                .get("itemStacks", [{}])[0]
                                .get("items", [])
                            )
                            results = []
                            for item in items[:limit]:
                                parsed = self._parse_product(item)
                                if parsed:
                                    results.append(parsed)
                            return results
            except Exception:
                pass

        return []

    def _build_embed(self, product: dict) -> discord.Embed:
        """Build a Discord embed for a single product."""
        color = discord.Color.green() if product["in_stock"] else discord.Color.red()
        embed = discord.Embed(
            title=product["name"],
            url=product["url"],
            color=color,
        )
        embed.add_field(name="Price", value=product["price"], inline=True)
        embed.add_field(name="Availability", value=product["avail"], inline=True)
        embed.add_field(name="Item #", value=product["id"] or "N/A", inline=True)
        if product.get("image_url"):
            embed.set_thumbnail(url=product["image_url"])
        embed.set_footer(text="Walmart Stock Checker • Data may be delayed")
        return embed

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.group(name="walmart", invoke_without_command=True)
    async def walmart(self, ctx: commands.Context):
        """Walmart stock checker commands."""
        await ctx.send_help(ctx.command)

    @walmart.command(name="item")
    async def walmart_item(self, ctx: commands.Context, item_number: str):
        """Look up a Walmart item by its item number.

        Example:
            `[p]walmart item 483990820`
        """
        item_number = re.sub(r"\D", "", item_number)
        if not item_number:
            return await ctx.send("❌ Please provide a valid numeric item number.")

        async with ctx.typing():
            product = await self._lookup_by_id(item_number)

        if not product:
            return await ctx.send(
                f"❌ Could not find item **#{item_number}** on Walmart. "
                "The item may not exist or Walmart may be rate limiting requests."
            )

        await ctx.send(embed=self._build_embed(product))

    @walmart.command(name="search")
    async def walmart_search(self, ctx: commands.Context, *, query: str):
        """Search Walmart by item name and show stock status of top results.

        Example:
            `[p]walmart search pokemon cards`
            `[p]walmart search pokemon scarlet violet booster pack`
        """
        async with ctx.typing():
            results = await self._search(query, limit=5)

        if not results:
            return await ctx.send(
                f"❌ No results found for **{query}**. "
                "Walmart may be rate limiting requests."
            )

        if len(results) == 1:
            return await ctx.send(embed=self._build_embed(results[0]))

        embed = discord.Embed(
            title=f"🛒 Walmart Search: {query}",
            description="Here are the top results:",
            color=discord.Color.blue(),
        )
        for i, p in enumerate(results, 1):
            stock_icon = "✅" if p["in_stock"] else "❌"
            embed.add_field(
                name=f"{i}. {p['name'][:80]}",
                value=(
                    f"{stock_icon} {p['avail']}  |  💲{p['price']}\n"
                    f"Item # `{p['id']}`  •  [View on Walmart]({p['url']})"
                ),
                inline=False,
            )
        embed.set_footer(
            text="Use `[p]walmart item <item #>` for full details on any item."
        )
        await ctx.send(embed=embed)

    @walmart.command(name="track")
    async def walmart_track(self, ctx: commands.Context, item_number: str):
        """Track a Walmart item and alert you when it comes in stock.

        Checks every 5 minutes for up to 1 hour.

        Example:
            `[p]walmart track 483990820`
        """
        item_number = re.sub(r"\D", "", item_number)
        if not item_number:
            return await ctx.send("❌ Please provide a valid numeric item number.")

        async with ctx.typing():
            product = await self._lookup_by_id(item_number)

        if not product:
            return await ctx.send(
                f"❌ Could not find item **#{item_number}** on Walmart."
            )

        if product["in_stock"]:
            embed = self._build_embed(product)
            embed.description = "🎉 This item is **already in stock**!"
            return await ctx.send(embed=embed)

        await ctx.send(
            f"👀 Now tracking **{product['name'][:80]}** (#{item_number}).\n"
            "I'll ping you here if it comes in stock within the next hour "
            "(checks every 5 minutes)."
        )

        for _ in range(12):
            await asyncio.sleep(300)
            updated = await self._lookup_by_id(item_number)
            if updated and updated["in_stock"]:
                embed = self._build_embed(updated)
                embed.description = "🎉 **Now in Stock!**"
                return await ctx.send(
                    f"{ctx.author.mention} Alert! Item is now in stock!",
                    embed=embed,
                )

        await ctx.send(
            f"⏰ Tracking ended for **{product['name'][:80]}** — "
            "it did not come in stock within 1 hour."
        )
