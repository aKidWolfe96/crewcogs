"""RestockMonitor — free multi-retailer restock alerts (built for Pokémon TCG drops).

Watch individual product links at Target, Best Buy, Walmart, GameStop, Pokémon
Center, Amazon, any Shopify card shop, or any site that publishes standard
product data — plus whole Shopify stores for new / restocked items matching
keywords. Alerts fire only when something goes from unavailable → available.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import tasks
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import humanize_timedelta

from .retailers import (
    AVAILABLE, BLOCKED, ERROR, IN_STOCK, PREORDER, RETAILERS, STATUS_EMOJI, STATUS_LABEL, UNKNOWN,
    CheckContext, CheckResult, FetchError, check_product, detect_retailer, fetch_shopify_catalog,
    normalize_url, shopify_base,
)

log = logging.getLogger("red.crewcogs.restockmonitor")

MIN_INTERVAL = 2            # minutes
DEFAULT_INTERVAL = 5        # minutes
HOST_GAP = 4.0              # seconds between requests to the same store
ALERT_COOLDOWN = 10 * 60    # don't re-alert the same product within 10 minutes
FAIL_WARN_AT = 6            # post one "having trouble" notice after this many failures in a row
MAX_PRODUCTS = 60
MAX_STORES = 10
MAX_STORE_ALERTS = 8        # per check, per store (the rest are summarised)
DEFAULT_KEYWORDS = ["pokemon"]


def _fmt_price(price: Optional[float]) -> str:
    return f"${price:,.2f}" if price else "—"


def _ago(ts: Optional[float]) -> str:
    return f"<t:{int(ts)}:R>" if ts else "never"


class LinkView(discord.ui.View):
    """A single URL button (link buttons need no callbacks and never expire)."""

    def __init__(self, url: str, label: str) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label=label[:80], url=url, emoji="🛒"))


class PageView(discord.ui.View):
    def __init__(self, pages: List[discord.Embed], owner_id: int) -> None:
        super().__init__(timeout=180)
        self.pages, self.owner_id, self.index = pages, owner_id, 0
        self.message: Optional[discord.Message] = None
        self._sync()

    def _sync(self) -> None:
        self.prev.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages) - 1
        self.counter.label = f"{self.index + 1}/{len(self.pages)}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Run the command yourself to page through.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index += 1
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class RestockMonitor(commands.Cog):
    """Free restock alerts for Pokémon cards (and anything else) across many stores."""

    __version__ = "2.0.0"

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=2611190811, force_registration=True)
        self.config.register_global(migrated_from_bestbuy=False)
        self.config.register_guild(
            channel=None,
            ping=None,               # None | "everyone" | "here" | role id
            interval=DEFAULT_INTERVAL,
            zip=None,
            allow_third_party=False,
            enabled=True,
            next_id=1,
            products={},             # id -> product dict
            stores={},               # id -> watched Shopify store dict
        )
        self.session: Optional[aiohttp.ClientSession] = None
        self._due: Dict[Tuple[int, str, str], float] = {}
        self._host_locks: Dict[str, asyncio.Lock] = {}
        self._host_last: Dict[str, float] = {}
        self._target_cache: Dict[str, str] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
        try:
            await self._migrate_from_bestbuymonitor()
        except Exception:
            log.exception("Could not import data from the old BestBuyMonitor cog")
        self.poller.start()

    async def cog_unload(self) -> None:
        self.poller.cancel()
        if self.session:
            await self.session.close()

    async def red_delete_data_for_user(self, *, requester: str, user_id: int) -> None:
        all_guilds = await self.config.all_guilds()
        for gid, data in all_guilds.items():
            if any(p.get("added_by") == user_id for p in data.get("products", {}).values()):
                async with self.config.guild_from_id(gid).products() as prods:
                    for p in prods.values():
                        if p.get("added_by") == user_id:
                            p["added_by"] = None

    async def _migrate_from_bestbuymonitor(self) -> None:
        """One-time import of channel / ping / products saved by the old BestBuyMonitor cog."""
        if await self.config.migrated_from_bestbuy():
            return
        old = Config.get_conf(None, identifier=8675309420, cog_name="BestBuyMonitor")
        old_guilds = await old.all_guilds()
        imported = 0
        for gid, data in old_guilds.items():
            products = data.get("products") or {}
            gconf = self.config.guild_from_id(gid)
            if await gconf.products():
                continue
            if data.get("alert_channel"):
                await gconf.channel.set(data["alert_channel"])
            if data.get("ping_target") is not None:
                await gconf.ping.set(data["ping_target"])
            if data.get("check_interval"):
                await gconf.interval.set(max(MIN_INTERVAL, int(data["check_interval"]) // 60))
            new: Dict[str, dict] = {}
            for n, (sku, info) in enumerate(products.items(), start=1):
                url = info.get("url") or f"https://www.bestbuy.com/site/{sku}.p?skuId={sku}"
                new[str(n)] = self._new_product(url, "bestbuy", None)
                new[str(n)]["name"] = info.get("name")
                imported += 1
            if new:
                await gconf.products.set(new)
                await gconf.next_id.set(len(new) + 1)
        await self.config.migrated_from_bestbuy.set(True)
        if imported:
            log.info("Imported %s products from BestBuyMonitor", imported)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _new_product(url: str, retailer: str, added_by: Optional[int]) -> dict:
        return {
            "url": url, "retailer": retailer, "name": None, "custom_name": None, "image": None,
            "price": None, "status": None, "detail": None, "third_party": False,
            "last_checked": None, "last_change": None, "last_alert": None, "alert_msg": None,
            "fails": 0, "paused": False, "maxprice": None, "added_by": added_by, "added_at": time.time(),
        }

    async def _ctx_for(self, guild: discord.Guild) -> CheckContext:
        assert self.session is not None
        tokens = await self.bot.get_shared_api_tokens("bestbuy")
        return CheckContext(
            session=self.session,
            zip_code=await self.config.guild(guild).zip(),
            bestbuy_key=tokens.get("api_key"),
            target_store_cache=self._target_cache,
        )

    async def _throttle(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = HOST_GAP - (time.monotonic() - self._host_last.get(host, 0.0))
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0, 1.5))
            self._host_last[host] = time.monotonic()

    async def _check(self, guild: discord.Guild, url: str, retailer: str) -> CheckResult:
        await self._throttle(urlparse(url).hostname or url)
        return await check_product(url, retailer, await self._ctx_for(guild))

    def _schedule(self, key: Tuple[int, str, str], interval_min: int, fails: int) -> None:
        base = max(MIN_INTERVAL, interval_min) * 60
        backoff = min(2 ** max(0, fails - 1), 8) if fails else 1
        self._due[key] = time.time() + base * backoff + random.uniform(0, base * 0.2)

    @staticmethod
    def _display_name(p: dict) -> str:
        return p.get("custom_name") or p.get("name") or urlparse(p["url"]).netloc

    def _status_line(self, p: dict) -> str:
        if p.get("paused"):
            return "⏸️ Paused"
        status = p.get("status")
        if p.get("fails") and p.get("last_error"):
            last = STATUS_LABEL.get(status, "not checked yet") if status else "not checked yet"
            return f"{STATUS_EMOJI[p['last_error']]} {STATUS_LABEL[p['last_error']]} · last known: {last}"
        if not status:
            return "⚪ Not checked yet"
        return f"{STATUS_EMOJI.get(status, '⚪')} {STATUS_LABEL.get(status, status)}"

    def _product_embed(self, pid: str, p: dict, *, title_prefix: str = "") -> discord.Embed:
        r = RETAILERS.get(p["retailer"], RETAILERS["generic"])
        status = p.get("status")
        color = 0x2ECC71 if status in AVAILABLE else (0xE74C3C if status else r.color)
        embed = discord.Embed(
            title=f"{title_prefix}{self._display_name(p)}"[:256],
            url=p["url"],
            color=color,
        )
        embed.add_field(name="Status", value=self._status_line(p), inline=True)
        embed.add_field(name="Price", value=_fmt_price(p.get("price")), inline=True)
        embed.add_field(name="Store", value=f"{r.emoji} {r.name}", inline=True)
        extras = []
        if p.get("detail"):
            extras.append(p["detail"])
        if p.get("maxprice"):
            extras.append(f"Alerts only at or under {_fmt_price(p['maxprice'])}")
        if extras:
            embed.add_field(name="Notes", value="\n".join(extras)[:1024], inline=False)
        if p.get("image"):
            embed.set_thumbnail(url=p["image"])
        embed.set_footer(text=f"ID #{pid} · last checked")
        if p.get("last_checked"):
            embed.timestamp = datetime.fromtimestamp(p["last_checked"], tz=timezone.utc)
        return embed

    def _alert_embed(self, pid: str, p: dict, *, sold_out_after: Optional[float] = None) -> discord.Embed:
        r = RETAILERS.get(p["retailer"], RETAILERS["generic"])
        preorder = p.get("status") == PREORDER
        if sold_out_after is not None:
            headline = f"❌ Sold out again after {humanize_timedelta(seconds=max(60, int(sold_out_after)))}"
            color = 0x95A5A6
        elif preorder:
            headline = "🟣 PRE-ORDERS ARE OPEN"
            color = 0x9B59B6
        else:
            headline = "🟢 BACK IN STOCK"
            color = 0x2ECC71
        embed = discord.Embed(
            title=f"{r.emoji} {self._display_name(p)}"[:256],
            url=p["url"],
            description=f"**{headline}** at **{r.name}**",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Price", value=_fmt_price(p.get("price")), inline=True)
        embed.add_field(name="Store", value=f"{r.emoji} {r.name}", inline=True)
        if p.get("detail"):
            embed.add_field(name="Details", value=p["detail"][:1024], inline=False)
        if p.get("image"):
            embed.set_thumbnail(url=p["image"])
        embed.set_footer(text=f"Restock Monitor · #{pid}")
        return embed

    async def _ping(self, guild: discord.Guild) -> Tuple[Optional[str], discord.AllowedMentions]:
        target = await self.config.guild(guild).ping()
        if target == "everyone":
            return "@everyone", discord.AllowedMentions(everyone=True)
        if target == "here":
            return "@here", discord.AllowedMentions(everyone=True)
        if target:
            role = guild.get_role(int(target))
            if role:
                return role.mention, discord.AllowedMentions(roles=[role])
        return None, discord.AllowedMentions.none()

    def _alert_channel(self, guild: discord.Guild, channel_id: Optional[int]) -> Optional[discord.abc.Messageable]:
        if not channel_id:
            return None
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            return None
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links):
            return None
        return channel

    # ── Applying results ──────────────────────────────────────────────────────

    async def _apply_result(self, guild: discord.Guild, pid: str, result: CheckResult) -> Optional[str]:
        """Store a check result and send any alert. Returns the event fired, if any."""
        gconf = self.config.guild(guild)
        allow_third_party = await gconf.allow_third_party()
        now = time.time()
        event: Optional[str] = None
        async with gconf.products() as prods:
            p = prods.get(pid)
            if p is None:
                return None
            p["last_checked"] = now
            if result.status in (ERROR, BLOCKED):
                p["fails"] = p.get("fails", 0) + 1
                p["last_error"] = result.status
                p["error_detail"] = result.detail
                if p["fails"] == FAIL_WARN_AT:
                    event = "warn"
            else:
                p["fails"] = 0
                p.pop("last_error", None)
                p.pop("error_detail", None)
                if result.name:
                    p["name"] = result.name
                if result.image:
                    p["image"] = result.image
                if result.price:
                    p["price"] = result.price
                p["detail"] = result.detail
                p["third_party"] = result.third_party
                if result.status != UNKNOWN:
                    prev = p.get("status")
                    was = prev in AVAILABLE
                    now_avail = result.status in AVAILABLE
                    if result.status != prev:
                        p["last_change"] = now
                    p["status"] = result.status
                    if now_avail and not was and prev is not None:
                        too_pricey = p.get("maxprice") and result.price and result.price > p["maxprice"]
                        scalper = result.third_party and not allow_third_party
                        cooling = p.get("last_alert") and now - p["last_alert"] < ALERT_COOLDOWN
                        if too_pricey:
                            p["detail"] = f"In stock at {_fmt_price(result.price)} — above your {_fmt_price(p['maxprice'])} limit"
                        elif scalper:
                            p["detail"] = (result.detail or "Third-party seller") + " — alert skipped (third-party)"
                        elif not cooling:
                            event = "restock"
                            p["last_alert"] = now
                            p["available_since"] = now
                    elif was and not now_avail:
                        event = "soldout"
            snapshot = dict(p)

        channel = self._alert_channel(guild, await gconf.channel())
        if event == "restock" and channel is not None:
            content, mentions = await self._ping(guild)
            r = RETAILERS.get(snapshot["retailer"], RETAILERS["generic"])
            try:
                msg = await channel.send(
                    content=content, embed=self._alert_embed(pid, snapshot),
                    view=LinkView(snapshot["url"], f"Buy at {r.name}"), allowed_mentions=mentions,
                )
                async with gconf.products() as prods:
                    if pid in prods:
                        prods[pid]["alert_msg"] = [msg.channel.id, msg.id]
            except discord.HTTPException as exc:
                log.warning("Could not send restock alert in %s: %s", guild.id, exc)
        elif event == "soldout" and snapshot.get("alert_msg"):
            ch_id, msg_id = snapshot["alert_msg"]
            ch = guild.get_channel_or_thread(ch_id)
            if ch is not None:
                since = snapshot.get("available_since") or snapshot.get("last_alert") or now
                try:
                    await ch.get_partial_message(msg_id).edit(
                        embed=self._alert_embed(pid, snapshot, sold_out_after=now - since)
                    )
                except discord.HTTPException:
                    pass
        elif event == "warn" and channel is not None:
            r = RETAILERS.get(snapshot["retailer"], RETAILERS["generic"])
            try:
                await channel.send(embed=discord.Embed(
                    color=0xE67E22,
                    description=(
                        f"⚠️ I've had trouble checking **{self._display_name(snapshot)}** ({r.name}) "
                        f"{FAIL_WARN_AT} times in a row: {snapshot.get('error_detail') or 'unknown error'}\n"
                        f"I'll keep trying, but less often. `restock debug <link>` shows what the store returns."
                    ),
                ))
            except discord.HTTPException:
                pass
        return event

    async def _apply_store(self, guild: discord.Guild, sid: str, items: Optional[List[dict]],
                           error: Optional[FetchError]) -> None:
        gconf = self.config.guild(guild)
        now = time.time()
        alerts: List[Tuple[str, dict]] = []
        async with gconf.stores() as stores:
            st = stores.get(sid)
            if st is None:
                return
            st["last_checked"] = now
            if error is not None:
                st["fails"] = st.get("fails", 0) + 1
                st["error_detail"] = error.detail
                return
            st["fails"] = 0
            st.pop("error_detail", None)
            known: Dict[str, bool] = st.setdefault("known", {})
            maxprice = st.get("maxprice")
            for item in items or []:
                prev = known.get(item["id"])
                if st.get("seeded"):
                    price_ok = not (maxprice and item["price"] and item["price"] > maxprice)
                    if prev is None and price_ok:
                        alerts.append(("new", item))
                    elif prev is False and item["available"] and price_ok:
                        alerts.append(("restock", item))
                known[item["id"]] = item["available"]
            st["matches"] = len(items or [])
            st["seeded"] = True
            if len(known) > 3000:
                for key in list(known)[: len(known) - 3000]:
                    known.pop(key, None)
            snapshot = dict(st)

        channel = self._alert_channel(guild, await gconf.channel())
        if not alerts or channel is None:
            return
        content, mentions = await self._ping(guild)
        shop = snapshot.get("name") or urlparse(snapshot["url"]).netloc
        for kind, item in alerts[:MAX_STORE_ALERTS]:
            if kind == "restock":
                head, color = "🟢 BACK IN STOCK", 0x2ECC71
            elif item["available"]:
                head, color = "🆕 NEW & AVAILABLE", 0x2ECC71
            else:
                head, color = "🆕 New listing (not purchasable yet)", 0x3498DB
            embed = discord.Embed(
                title=f"🛍️ {item['title']}"[:256], url=item["url"],
                description=f"**{head}** at **{shop}**", color=color,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Price", value=_fmt_price(item.get("price")), inline=True)
            embed.add_field(name="Watching for", value=", ".join(snapshot.get("keywords") or ["everything"])[:1024], inline=True)
            if item.get("image"):
                embed.set_thumbnail(url=item["image"])
            embed.set_footer(text=f"Restock Monitor · store #{sid}")
            try:
                await channel.send(content=content if item["available"] else None, embed=embed,
                                   view=LinkView(item["url"], f"Open at {shop}"), allowed_mentions=mentions)
            except discord.HTTPException:
                break
        if len(alerts) > MAX_STORE_ALERTS:
            try:
                await channel.send(embed=discord.Embed(
                    color=0x3498DB,
                    description=f"…and **{len(alerts) - MAX_STORE_ALERTS}** more matching changes at **{shop}**: {snapshot['url']}",
                ))
            except discord.HTTPException:
                pass

    # ── Poller ────────────────────────────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def poller(self) -> None:
        try:
            await self._poll_once()
        except Exception:
            log.exception("Restock poll failed")

    @poller.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_red_ready()

    async def _poll_once(self) -> None:
        now = time.time()
        all_guilds = await self.config.all_guilds()
        # (url, retailer) -> [(guild, pid, interval)] so one fetch serves every server watching it
        product_jobs: Dict[Tuple[str, str], List[Tuple[discord.Guild, str, int]]] = {}
        store_jobs: List[Tuple[discord.Guild, str, dict, int]] = []
        for gid, g in all_guilds.items():
            guild = self.bot.get_guild(gid)
            if guild is None or not g.get("channel") or not g.get("enabled", True):
                continue
            interval = max(MIN_INTERVAL, int(g.get("interval") or DEFAULT_INTERVAL))
            for pid, p in (g.get("products") or {}).items():
                if p.get("paused"):
                    continue
                if now >= self._due.get((gid, "p", pid), 0):
                    product_jobs.setdefault((p["url"], p["retailer"]), []).append((guild, pid, interval))
            for sid, st in (g.get("stores") or {}).items():
                if not st.get("paused") and now >= self._due.get((gid, "s", sid), 0):
                    store_jobs.append((guild, sid, st, interval))

        by_host: Dict[str, List[Any]] = {}
        for (url, retailer), targets in product_jobs.items():
            by_host.setdefault(urlparse(url).hostname or url, []).append(("p", url, retailer, targets))
        for job in store_jobs:
            by_host.setdefault(urlparse(job[2]["url"]).hostname or job[2]["url"], []).append(("s", job))
        if by_host:
            await asyncio.gather(*(self._run_host_jobs(jobs) for jobs in by_host.values()))

    async def _run_host_jobs(self, jobs: List[Any]) -> None:
        for job in jobs:
            try:
                if job[0] == "p":
                    _, url, retailer, targets = job
                    result = await self._check(targets[0][0], url, retailer)
                    for guild, pid, interval in targets:
                        await self._apply_result(guild, pid, result)
                        p = (await self.config.guild(guild).products()).get(pid) or {}
                        self._schedule((guild.id, "p", pid), interval, p.get("fails", 0))
                else:
                    guild, sid, st, interval = job[1]
                    await self._run_store(guild, sid, st)
                    fresh = (await self.config.guild(guild).stores()).get(sid) or {}
                    self._schedule((guild.id, "s", sid), interval, fresh.get("fails", 0))
            except Exception:
                log.exception("Restock job failed")

    async def _run_store(self, guild: discord.Guild, sid: str, st: dict) -> None:
        await self._throttle(urlparse(st["url"]).hostname or st["url"])
        assert self.session is not None
        try:
            items = await fetch_shopify_catalog(self.session, st["url"], st.get("keywords") or [])
            await self._apply_store(guild, sid, items, None)
        except FetchError as exc:
            await self._apply_store(guild, sid, None, exc)

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.group(name="restock", aliases=["tcg", "tcgc", "restocks"], invoke_without_command=True)
    @commands.guild_only()
    async def restock(self, ctx: commands.Context) -> None:
        """Free restock alerts for Pokémon cards across many stores.

        Quick start:
        1. `[p]restock channel #restocks`
        2. `[p]restock ping @Collectors` (or `everyone`, `here`, `none`)
        3. `[p]restock add <product link>` — Target, Best Buy, Walmart, GameStop,
           Pokémon Center, Amazon, any Shopify card shop, and most other stores
        4. `[p]restock store add <shop link> [keywords]` to watch a whole Shopify shop
        """
        await ctx.send_help()

    @restock.command(name="add", aliases=["watch"])
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_add(self, ctx: commands.Context, url: str, max_price: Optional[float] = None) -> None:
        """Watch a product link. Optionally only alert at or under a price.

        Examples:
        `[p]restock add https://www.target.com/p/pokemon-.../-/A-12345678`
        `[p]restock add https://shop.example.com/products/surging-sparks-etb 59.99`
        """
        url = normalize_url(url)
        if not urlparse(url).hostname:
            await ctx.send("❌ That doesn't look like a link.")
            return
        products = await self.config.guild(ctx.guild).products()
        if len(products) >= MAX_PRODUCTS:
            await ctx.send(f"❌ You're watching the maximum of {MAX_PRODUCTS} products. Remove some first.")
            return
        if any(p["url"] == url for p in products.values()):
            await ctx.send("ℹ️ That link is already being watched — see `restock list`.")
            return
        retailer = detect_retailer(url)
        async with ctx.typing():
            result = await self._check(ctx.guild, url, retailer)

        pid = str(await self.config.guild(ctx.guild).next_id())
        p = self._new_product(url, retailer, ctx.author.id)
        p.update(
            name=result.name, image=result.image, price=result.price, detail=result.detail,
            third_party=result.third_party, last_checked=time.time(), maxprice=max_price,
        )
        if result.status in (ERROR, BLOCKED):
            p["fails"], p["last_error"], p["error_detail"] = 1, result.status, result.detail
        elif result.status != UNKNOWN:
            p["status"] = result.status
            p["last_change"] = time.time()
        async with self.config.guild(ctx.guild).products() as prods:
            prods[pid] = p
        await self.config.guild(ctx.guild).next_id.set(int(pid) + 1)
        interval = await self.config.guild(ctx.guild).interval()
        self._schedule((ctx.guild.id, "p", pid), interval, 0)

        embed = self._product_embed(pid, p, title_prefix="👀 Now watching: ")
        r = RETAILERS[retailer]
        tips = []
        if result.status == BLOCKED:
            tips.append(f"⚠️ {r.name} blocked this first check ({result.detail}). I'll keep trying on schedule.")
        elif result.status == ERROR:
            tips.append(f"⚠️ First check failed: {result.detail} I'll keep trying.")
        elif result.status == UNKNOWN:
            tips.append("⚠️ I couldn't read stock info from this page. Try `restock debug <link>` — "
                        "this store may not publish product data.")
        if result.available:
            tips.append("It's available **right now** — you'll be alerted the next time it comes back after selling out.")
        else:
            tips.append("You'll get an alert as soon as it's available.")
        if r.reliability == "best-effort":
            tips.append(f"ℹ️ {r.note}")
        if not await self.config.guild(ctx.guild).channel():
            tips.append(f"❗ No alert channel set — use `{ctx.clean_prefix}restock channel #channel`.")
        embed.description = "\n".join(tips)
        await ctx.send(embed=embed, view=LinkView(url, f"Open at {r.name}"))

    @restock.command(name="remove", aliases=["delete", "del", "unwatch"])
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_remove(self, ctx: commands.Context, product_id: str) -> None:
        """Stop watching a product (see IDs in `restock list`)."""
        async with self.config.guild(ctx.guild).products() as prods:
            p = prods.pop(product_id.lstrip("#"), None)
        if p is None:
            await ctx.send("❌ No product with that ID. Check `restock list`.")
            return
        self._due.pop((ctx.guild.id, "p", product_id.lstrip("#")), None)
        await ctx.send(f"🗑️ Stopped watching **{self._display_name(p)}**.")

    async def _edit_product(self, ctx: commands.Context, product_id: str, **changes: Any) -> Optional[dict]:
        async with self.config.guild(ctx.guild).products() as prods:
            p = prods.get(product_id.lstrip("#"))
            if p is None:
                await ctx.send("❌ No product with that ID. Check `restock list`.")
                return None
            p.update(changes)
            return dict(p)

    @restock.command(name="pause")
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_pause(self, ctx: commands.Context, product_id: str) -> None:
        """Pause checks for one product."""
        p = await self._edit_product(ctx, product_id, paused=True)
        if p:
            await ctx.send(f"⏸️ Paused **{self._display_name(p)}**.")

    @restock.command(name="resume", aliases=["unpause"])
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_resume(self, ctx: commands.Context, product_id: str) -> None:
        """Resume checks for a paused product."""
        p = await self._edit_product(ctx, product_id, paused=False)
        if p:
            self._due.pop((ctx.guild.id, "p", product_id.lstrip("#")), None)
            await ctx.send(f"▶️ Resumed **{self._display_name(p)}**.")

    @restock.command(name="rename")
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_rename(self, ctx: commands.Context, product_id: str, *, name: str) -> None:
        """Give a product a short name for alerts (use `reset` to go back to the store's name)."""
        custom = None if name.lower() == "reset" else name[:100]
        p = await self._edit_product(ctx, product_id, custom_name=custom)
        if p:
            await ctx.send(f"✏️ Now shown as **{self._display_name(p)}**.")

    @restock.command(name="maxprice", aliases=["limit"])
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_maxprice(self, ctx: commands.Context, product_id: str, price: str) -> None:
        """Only alert when the price is at or under this (use `none` to clear). Great against scalpers."""
        value = None if price.lower() in ("none", "off", "clear") else _parse_price(price)
        if price.lower() not in ("none", "off", "clear") and value is None:
            await ctx.send("❌ Give a price like `54.99`, or `none`.")
            return
        p = await self._edit_product(ctx, product_id, maxprice=value)
        if p:
            await ctx.send(f"💲 {'No price limit' if value is None else f'Alerts only at or under {_fmt_price(value)}'} "
                           f"for **{self._display_name(p)}**.")

    @restock.command(name="list", aliases=["status", "board"])
    async def restock_list(self, ctx: commands.Context) -> None:
        """Show everything being watched and its current status."""
        g = await self.config.guild(ctx.guild).all()
        products, stores = g["products"], g["stores"]
        if not products and not stores:
            await ctx.send(f"Nothing watched yet — add a product with `{ctx.clean_prefix}restock add <link>`.")
            return
        lines = []
        order = {IN_STOCK: 0, PREORDER: 1}
        for pid, p in sorted(products.items(), key=lambda kv: (order.get(kv[1].get("status"), 2), int(kv[0]))):
            r = RETAILERS.get(p["retailer"], RETAILERS["generic"])
            name = self._display_name(p)
            name = name if len(name) <= 60 else name[:57] + "…"
            extra = f" · ≤{_fmt_price(p['maxprice'])}" if p.get("maxprice") else ""
            lines.append(
                f"`#{pid}` {r.emoji} [{name}]({p['url']})\n"
                f"    {self._status_line(p)} · {_fmt_price(p.get('price'))}{extra} · checked {_ago(p.get('last_checked'))}"
            )
        for sid, st in stores.items():
            kw = ", ".join(st.get("keywords") or ["everything"])
            err = f" · ⚠️ {st['error_detail']}" if st.get("fails") else ""
            lines.append(
                f"`S{sid}` 🛍️ [{st.get('name') or urlparse(st['url']).netloc}]({st['url']}) — whole store\n"
                f"    watching **{kw}** · {st.get('matches', 0)} matching items · checked {_ago(st.get('last_checked'))}{err}"
            )
        channel = ctx.guild.get_channel(g["channel"]) if g["channel"] else None
        header = (f"Alerts → {channel.mention if channel else '❗ no channel set'} · every {g['interval']} min"
                  f"{' · ⏸️ monitoring paused' if not g['enabled'] else ''}")
        pages: List[discord.Embed] = []
        for i in range(0, len(lines), 8):
            embed = discord.Embed(title="🛒 Restock Watchlist", description=header + "\n\n" + "\n".join(lines[i:i + 8]),
                                  color=0x3498DB)
            embed.set_footer(text=f"{len(products)} products · {len(stores)} stores · 🟢 in stock 🟣 pre-order 🔴 sold out 🟠 blocked")
            pages.append(embed)
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            view = PageView(pages, ctx.author.id)
            view.message = await ctx.send(embed=pages[0], view=view)

    @restock.command(name="check", aliases=["now"])
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_check(self, ctx: commands.Context, product_id: Optional[str] = None) -> None:
        """Check one product (or everything) right now. Alerts fire normally if something restocked."""
        products = await self.config.guild(ctx.guild).products()
        targets = {product_id.lstrip("#"): products.get(product_id.lstrip("#"))} if product_id else products
        if product_id and targets.get(product_id.lstrip("#")) is None:
            await ctx.send("❌ No product with that ID. Check `restock list`.")
            return
        if not targets:
            await ctx.send("Nothing to check yet.")
            return
        interval = await self.config.guild(ctx.guild).interval()
        async with ctx.typing():
            for pid, p in targets.items():
                result = await self._check(ctx.guild, p["url"], p["retailer"])
                await self._apply_result(ctx.guild, pid, result)
                fresh = (await self.config.guild(ctx.guild).products()).get(pid) or {}
                self._schedule((ctx.guild.id, "p", pid), interval, fresh.get("fails", 0))
        products = await self.config.guild(ctx.guild).products()
        if product_id:
            pid = product_id.lstrip("#")
            p = products[pid]
            r = RETAILERS.get(p["retailer"], RETAILERS["generic"])
            embed = self._product_embed(pid, p)
            if p.get("error_detail"):
                embed.description = f"⚠️ {p['error_detail']}"
            await ctx.send(embed=embed, view=LinkView(p["url"], f"Open at {r.name}"))
        else:
            counts: Dict[str, int] = {}
            for p in products.values():
                key = p.get("last_error") or p.get("status") or UNKNOWN
                counts[key] = counts.get(key, 0) + 1
            summary = " · ".join(f"{STATUS_EMOJI[k]} {v} {STATUS_LABEL[k].lower()}" for k, v in counts.items())
            await ctx.send(f"✅ Checked {len(targets)} product(s): {summary}\nSee `{ctx.clean_prefix}restock list` for details.")

    @restock.command(name="debug", aliases=["test-link", "try"])
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_debug(self, ctx: commands.Context, url: str) -> None:
        """Show exactly what the checker reads from a link — without saving it."""
        url = normalize_url(url)
        retailer = detect_retailer(url)
        async with ctx.typing():
            result = await self._check(ctx.guild, url, retailer)
        r = RETAILERS[retailer]
        embed = discord.Embed(title=f"🔍 {r.emoji} {r.name} check", url=url,
                              color=0x2ECC71 if result.available else 0x95A5A6)
        embed.add_field(name="Status", value=f"{STATUS_EMOJI[result.status]} {STATUS_LABEL[result.status]}", inline=True)
        embed.add_field(name="Price", value=_fmt_price(result.price), inline=True)
        embed.add_field(name="Checker", value=f"`{retailer}` ({r.reliability})", inline=True)
        embed.add_field(name="Name", value=result.name or "—", inline=False)
        if result.detail:
            embed.add_field(name="Details", value=result.detail[:1024], inline=False)
        if result.third_party:
            embed.add_field(name="Seller", value="Third-party marketplace seller", inline=False)
        if result.image:
            embed.set_thumbnail(url=result.image)
        await ctx.send(embed=embed)

    @restock.command(name="channel")
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_channel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        """Set the channel restock alerts are posted in (defaults to this one)."""
        channel = channel or ctx.channel
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await ctx.send(f"❌ I need **Send Messages** and **Embed Links** in {channel.mention}.")
            return
        await self.config.guild(ctx.guild).channel.set(channel.id)
        await ctx.send(f"✅ Restock alerts will be posted in {channel.mention}.")

    @restock.command(name="ping")
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_ping(self, ctx: commands.Context, *, target: str) -> None:
        """Who to ping on alerts: `everyone`, `here`, a role, or `none`."""
        value = target.strip().lower()
        if value in ("none", "off", "nobody"):
            await self.config.guild(ctx.guild).ping.set(None)
            await ctx.send("✅ Alerts will post without a ping.")
            return
        if value in ("everyone", "@everyone", "here", "@here"):
            value = value.lstrip("@")
            await self.config.guild(ctx.guild).ping.set(value)
            await ctx.send(f"✅ Alerts will ping @{value}.")
            return
        try:
            role = await commands.RoleConverter().convert(ctx, target)
        except commands.BadArgument:
            await ctx.send("❌ Give a role, `everyone`, `here`, or `none`.")
            return
        await self.config.guild(ctx.guild).ping.set(role.id)
        warn = "" if role.mentionable or ctx.guild.me.guild_permissions.mention_everyone else \
            "\n⚠️ That role isn't mentionable and I lack **Mention Everyone**, so the ping won't notify anyone."
        await ctx.send(f"✅ Alerts will ping **{role.name}**.{warn}", allowed_mentions=discord.AllowedMentions.none())

    @restock.command(name="interval")
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_interval(self, ctx: commands.Context, minutes: int) -> None:
        """How often to check each product, in minutes (minimum 2)."""
        minutes = max(MIN_INTERVAL, minutes)
        await self.config.guild(ctx.guild).interval.set(minutes)
        await ctx.send(f"✅ Checking every **{minutes} minute(s)** (with a little random jitter so stores don't block us).")

    @restock.command(name="zip", aliases=["location"])
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_zip(self, ctx: commands.Context, zip_code: str) -> None:
        """Set your ZIP code (used for Target store-pickup info). Use `none` to clear."""
        if zip_code.lower() == "none":
            await self.config.guild(ctx.guild).zip.set(None)
            await ctx.send("✅ ZIP cleared.")
            return
        if not zip_code.isdigit() or len(zip_code) != 5:
            await ctx.send("❌ Give a 5-digit US ZIP code.")
            return
        await self.config.guild(ctx.guild).zip.set(zip_code)
        await ctx.send(f"✅ ZIP set to **{zip_code}** — Target checks will mention pickup near you.")

    @restock.command(name="thirdparty", aliases=["marketplace"])
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_thirdparty(self, ctx: commands.Context, on_off: bool) -> None:
        """Alert on third-party marketplace sellers too? (Off by default — often scalper prices.)"""
        await self.config.guild(ctx.guild).allow_third_party.set(on_off)
        await ctx.send(f"✅ Third-party seller alerts are now **{'on' if on_off else 'off'}**.")

    @restock.command(name="toggle")
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_toggle(self, ctx: commands.Context) -> None:
        """Pause or resume all monitoring in this server."""
        enabled = not await self.config.guild(ctx.guild).enabled()
        await self.config.guild(ctx.guild).enabled.set(enabled)
        await ctx.send("▶️ Monitoring resumed." if enabled else "⏸️ All monitoring paused.")

    @restock.command(name="test")
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_test(self, ctx: commands.Context) -> None:
        """Send a sample alert to the alert channel (with your ping) to check everything works."""
        channel = self._alert_channel(ctx.guild, await self.config.guild(ctx.guild).channel())
        if channel is None:
            await ctx.send("❌ Set an alert channel I can post in first: `restock channel #channel`.")
            return
        sample = self._new_product("https://www.pokemoncenter.com/", "pokemoncenter", None)
        sample.update(name="TEST — Pokémon TCG Elite Trainer Box", price=49.99, status=IN_STOCK,
                      detail="This is a test alert. Real alerts look exactly like this.")
        content, mentions = await self._ping(ctx.guild)
        await channel.send(content=content, embed=self._alert_embed("test", sample),
                           view=LinkView(sample["url"], "Buy at Pokémon Center"), allowed_mentions=mentions)
        if channel != ctx.channel:
            await ctx.send(f"✅ Test alert sent to {channel.mention}.")

    @restock.command(name="retailers", aliases=["stores", "supported"])
    async def restock_retailers(self, ctx: commands.Context) -> None:
        """Which stores are supported and how reliable each checker is."""
        badge = {"good": "✅ reliable", "ok": "🟡 usually works", "best-effort": "🟠 best effort"}
        lines = [f"{r.emoji} **{r.name}** — {badge[r.reliability]}\n    _{r.note}_" for r in RETAILERS.values()]
        tokens = await self.bot.get_shared_api_tokens("bestbuy")
        bb = "✅ set" if tokens.get("api_key") else f"not set — the bot owner can add a free key with `{ctx.clean_prefix}set api bestbuy api_key,YOUR_KEY` (get one at developer.bestbuy.com)"
        embed = discord.Embed(
            title="🛒 Supported stores (all free)",
            description="\n".join(lines) + f"\n\n**Best Buy developer key:** {bb}",
            color=0x3498DB,
        )
        embed.set_footer(text="Any link works — unknown sites are read via the standard product data most shops publish.")
        await ctx.send(embed=embed)

    @restock.command(name="settings")
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_settings(self, ctx: commands.Context) -> None:
        """Show this server's monitor settings."""
        g = await self.config.guild(ctx.guild).all()
        channel = ctx.guild.get_channel(g["channel"]) if g["channel"] else None
        ping = g["ping"]
        if isinstance(ping, int):
            role = ctx.guild.get_role(ping)
            ping = role.mention if role else "deleted role"
        embed = discord.Embed(title="⚙️ Restock Monitor settings", color=0x3498DB)
        embed.add_field(name="Alert channel", value=channel.mention if channel else "❗ not set", inline=True)
        embed.add_field(name="Ping", value=f"@{ping}" if ping in ("everyone", "here") else (ping or "none"), inline=True)
        embed.add_field(name="Interval", value=f"every {g['interval']} min", inline=True)
        embed.add_field(name="ZIP", value=g["zip"] or "not set", inline=True)
        embed.add_field(name="Third-party sellers", value="alert" if g["allow_third_party"] else "skip", inline=True)
        embed.add_field(name="Monitoring", value="on" if g["enabled"] else "⏸️ paused", inline=True)
        embed.add_field(name="Watching", value=f"{len(g['products'])} products · {len(g['stores'])} stores", inline=False)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ── Whole-store watching (Shopify) ────────────────────────────────────────

    @restock.group(name="store", aliases=["shop"], invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def restock_store(self, ctx: commands.Context) -> None:
        """Watch a whole Shopify card shop for new or restocked items matching keywords."""
        await ctx.send_help()

    @restock_store.command(name="add")
    async def store_add(self, ctx: commands.Context, url: str, *, keywords: str = "pokemon") -> None:
        """Watch a Shopify shop. Keywords are comma-separated; each can be several words.

        Example: `[p]restock store add https://mycardshop.com pokemon booster box, pokemon etb`
        The first check quietly learns what's already listed; after that you get
        alerts for brand-new listings and for items that come back in stock.
        """
        base = shopify_base(url)
        stores = await self.config.guild(ctx.guild).stores()
        if len(stores) >= MAX_STORES:
            await ctx.send(f"❌ You're watching the maximum of {MAX_STORES} stores.")
            return
        if any(s["url"] == base for s in stores.values()):
            await ctx.send("ℹ️ That store is already being watched — use `restock store keywords` to change what it looks for.")
            return
        kw = [k.strip() for k in keywords.split(",") if k.strip()] or DEFAULT_KEYWORDS
        assert self.session is not None
        async with ctx.typing():
            await self._throttle(urlparse(base).hostname or base)
            try:
                items = await fetch_shopify_catalog(self.session, base, kw)
            except FetchError as exc:
                await ctx.send(f"❌ Couldn't read that store's catalog: {exc.detail}\n"
                               "Whole-store watching only works with Shopify shops. "
                               "You can still watch single products with `restock add`.")
                return
        sid = str(await self.config.guild(ctx.guild).next_id())
        await self.config.guild(ctx.guild).next_id.set(int(sid) + 1)
        async with self.config.guild(ctx.guild).stores() as st:
            st[sid] = {
                "url": base, "name": urlparse(base).netloc, "keywords": kw, "known": {}, "seeded": False,
                "matches": 0, "fails": 0, "last_checked": None, "maxprice": None, "added_by": ctx.author.id,
            }
        await self._apply_store(ctx.guild, sid, items, None)  # seeds silently
        interval = await self.config.guild(ctx.guild).interval()
        self._schedule((ctx.guild.id, "s", sid), interval, 0)
        in_stock = [i for i in items if i["available"]]
        sample = "\n".join(f"• [{i['title'][:70]}]({i['url']}) — {_fmt_price(i['price'])}" for i in in_stock[:5])
        await ctx.send(embed=discord.Embed(
            title=f"🛍️ Now watching {urlparse(base).netloc}",
            description=(
                f"Looking for: **{', '.join(kw)}**\n"
                f"Found **{len(items)}** matching items ({len(in_stock)} available right now).\n"
                f"You'll be alerted about **new listings** and **restocks** from now on.\n\n"
                + (f"**Available now:**\n{sample}" if sample else "")
            )[:4000],
            color=0x95BF47,
        ).set_footer(text=f"Store ID S{sid}"))

    @restock_store.command(name="remove", aliases=["delete", "del"])
    async def store_remove(self, ctx: commands.Context, store_id: str) -> None:
        """Stop watching a store (IDs look like S3 in `restock list`)."""
        sid = store_id.upper().lstrip("S#")
        async with self.config.guild(ctx.guild).stores() as st:
            removed = st.pop(sid, None)
        if removed is None:
            await ctx.send("❌ No store with that ID. Check `restock list`.")
            return
        self._due.pop((ctx.guild.id, "s", sid), None)
        await ctx.send(f"🗑️ Stopped watching **{removed.get('name') or removed['url']}**.")

    @restock_store.command(name="keywords", aliases=["kw"])
    async def store_keywords(self, ctx: commands.Context, store_id: str, *, keywords: str) -> None:
        """Change what a watched store looks for (comma-separated). Re-learns the catalog quietly."""
        sid = store_id.upper().lstrip("S#")
        kw = [k.strip() for k in keywords.split(",") if k.strip()]
        async with self.config.guild(ctx.guild).stores() as st:
            if sid not in st:
                await ctx.send("❌ No store with that ID. Check `restock list`.")
                return
            st[sid].update(keywords=kw, known={}, seeded=False)
        self._due.pop((ctx.guild.id, "s", sid), None)
        await ctx.send(f"✅ Now looking for **{', '.join(kw) or 'everything'}** — I'll learn the current catalog on the next check.")

    @restock_store.command(name="maxprice", aliases=["limit"])
    async def store_maxprice(self, ctx: commands.Context, store_id: str, price: str) -> None:
        """Only alert for items at or under this price at a watched store (`none` to clear)."""
        sid = store_id.upper().lstrip("S#")
        value = None if price.lower() in ("none", "off", "clear") else _parse_price(price)
        async with self.config.guild(ctx.guild).stores() as st:
            if sid not in st:
                await ctx.send("❌ No store with that ID.")
                return
            st[sid]["maxprice"] = value
        await ctx.send(f"💲 {'No price limit' if value is None else f'Only items at or under {_fmt_price(value)}'}.")


def _parse_price(text: str) -> Optional[float]:
    try:
        value = float(text.replace("$", "").replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None
