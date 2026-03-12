import discord
from redbot.core import commands, Config
from redbot.core.bot import Red

import aiohttp
import asyncio
import re
import json


class RetailTracker(commands.Cog):
    """
    Universal Retail Product Tracker (UPC based)
    Currently supports Walmart.
    Designed for multi-store expansion.
    """

    def __init__(self, bot: Red):

        self.bot = bot

        self.config = Config.get_conf(self, identifier=7788994455)

        self.config.register_global(
            products={}
        )

        self.headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html"
        }

        self.monitor_task = bot.loop.create_task(self.monitor_loop())

    def cog_unload(self):
        self.monitor_task.cancel()

    # ----------------------------------------------------
    # Walmart helpers
    # ----------------------------------------------------

    async def walmart_lookup(self, item_id):

        url = f"https://www.walmart.com/ip/{item_id}"

        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(timeout=timeout) as session:

            async with session.get(url, headers=self.headers, ssl=False) as r:

                if r.status != 200:
                    return None

                text = await r.text()

        match = re.search(
            r'window\.__WML_REDUX_INITIAL_STATE__\s*=\s*(\{.*?\});',
            text
        )

        if not match:
            return None

        try:

            data = json.loads(match.group(1))

            product = data["product"]["products"][item_id]

            name = product.get("productName")

            upc = product.get("upc")

            price = None

            if product.get("price"):
                price = product["price"].get("price")

            availability = product.get("availabilityStatus")

            in_stock = availability == "IN_STOCK"

            image = None

            if product.get("imageInfo"):
                image = product["imageInfo"].get("thumbnailUrl")

            return {
                "name": name,
                "upc": str(upc),
                "item_id": str(item_id),
                "price": float(price) if price else 0.0,
                "in_stock": in_stock,
                "image": image,
                "url": url
            }

        except Exception:
            return None

    # ----------------------------------------------------
    # Monitor loop
    # ----------------------------------------------------

    async def monitor_loop(self):

        await self.bot.wait_until_ready()

        while True:

            products = await self.config.products()

            if not products:
                await asyncio.sleep(300)
                continue

            for upc, data in products.items():

                walmart_id = data["stores"].get("walmart")

                if not walmart_id:
                    continue

                product = await self.walmart_lookup(walmart_id)

                if not product:
                    continue

                last_stock = data.get("last_stock")

                last_price = data.get("last_price")

                new_stock = product["in_stock"]

                new_price = product["price"]

                notify = False
                message = None

                if new_stock and not last_stock:
                    notify = True
                    message = "🟢 Item is now **IN STOCK**"

                if last_price and new_price < last_price:
                    notify = True
                    message = f"💰 Price dropped to **${new_price}**"

                if notify:

                    for uid in data["watchers"]:

                        user = self.bot.get_user(uid)

                        if user:

                            try:

                                embed = discord.Embed(
                                    title=product["name"],
                                    url=product["url"],
                                    color=discord.Color.green()
                                )

                                embed.description = message

                                embed.add_field(
                                    name="Price",
                                    value=f"${new_price}"
                                )

                                if product["image"]:
                                    embed.set_thumbnail(url=product["image"])

                                await user.send(embed=embed)

                            except Exception:
                                pass

                data["last_stock"] = new_stock
                data["last_price"] = new_price

            await self.config.products.set(products)

            await asyncio.sleep(120)

    # ----------------------------------------------------
    # Commands
    # ----------------------------------------------------

    @commands.group()
    async def retail(self, ctx):
        """Retail tracker commands"""
        pass

    # ----------------------------------------------------
    # Track command
    # ----------------------------------------------------

    @retail.command()
    async def track(self, ctx, input_value: str):
        """
        Track a product.

        Accepts:
        - Walmart URL
        - Walmart Item ID
        """

        item_id = None

        if "walmart.com" in input_value:

            match = re.search(r"/ip/(?:[^/]+/)?(\d+)", input_value)

            if match:
                item_id = match.group(1)

        else:

            item_id = re.sub(r"\D", "", input_value)

        if not item_id:
            return await ctx.send("Invalid Walmart item.")

        async with ctx.typing():
            product = await self.walmart_lookup(item_id)

        if not product:
            return await ctx.send("Product not found.")

        upc = product["upc"]

        products = await self.config.products()

        if upc not in products:

            products[upc] = {
                "name": product["name"],
                "stores": {
                    "walmart": item_id
                },
                "watchers": [],
                "last_stock": product["in_stock"],
                "last_price": product["price"]
            }

        if ctx.author.id not in products[upc]["watchers"]:
            products[upc]["watchers"].append(ctx.author.id)

        await self.config.products.set(products)

        await ctx.send(
            f"Tracking **{product['name']}**\nUPC: `{upc}`"
        )

    # ----------------------------------------------------
    # Stop tracking
    # ----------------------------------------------------

    @retail.command()
    async def untrack(self, ctx, upc: str):

        products = await self.config.products()

        if upc not in products:
            return await ctx.send("UPC not tracked.")

        if ctx.author.id in products[upc]["watchers"]:
            products[upc]["watchers"].remove(ctx.author.id)

        if not products[upc]["watchers"]:
            del products[upc]

        await self.config.products.set(products)

        await ctx.send("Stopped tracking.")

    # ----------------------------------------------------
    # List tracked
    # ----------------------------------------------------

    @retail.command()
    async def tracked(self, ctx):

        products = await self.config.products()

        if not products:
            return await ctx.send("No tracked products.")

        embed = discord.Embed(
            title="Tracked Products",
            color=discord.Color.blue()
        )

        for upc, data in products.items():

            embed.add_field(
                name=data["name"],
                value=f"UPC `{upc}`\nWatchers: {len(data['watchers'])}",
                inline=False
            )

        await ctx.send(embed=embed)

    # ----------------------------------------------------
    # Lookup command
    # ----------------------------------------------------

    @retail.command()
    async def lookup(self, ctx, item_id: str):

        item_id = re.sub(r"\D", "", item_id)

        async with ctx.typing():
            product = await self.walmart_lookup(item_id)

        if not product:
            return await ctx.send("Product not found.")

        embed = discord.Embed(
            title=product["name"],
            url=product["url"],
            color=discord.Color.green() if product["in_stock"] else discord.Color.red()
        )

        embed.add_field(name="UPC", value=product["upc"])

        embed.add_field(name="Price", value=f"${product['price']}")

        embed.add_field(
            name="Stock",
            value="In Stock" if product["in_stock"] else "Out of Stock"
        )

        if product["image"]:
            embed.set_thumbnail(url=product["image"])

        await ctx.send(embed=embed)
