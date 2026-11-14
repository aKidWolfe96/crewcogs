"""
UFC Cog for Red-DiscordBot v3 — fight night, done properly.

  • Picture cards: fight-card poster, Tale of the Tape, fighter cards, leaderboards
  • Tap-to-pick: a dropdown per fight (winner + optional method/round bonus)
  • Every fight locks automatically when its part of the card starts
  • Bets pay at the DraftKings moneyline locked in when you bet
  • A watcher posts the card before the event, creates a Discord event, posts
    each result live, settles picks/bets on the spot and posts a recap

Commands live under `[p]ufc`; settings under `[p]ufcset`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import discord
from discord.ext import tasks
from redbot.core import Config, bank, checks, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box

from . import embeds
from . import render
from . import scoring as sc
from .api import (attach_odds, espn_api_status, get_event_by_id, get_event_on_date, get_fighter,
                  get_recent_event, get_upcoming_event)
from .views import CardView, PickEditor, is_locked, png_file

log = logging.getLogger("red.akidwolfe.fightnight")

__version__ = "6.0.0"
MAX_BETS = 3
STALE_EVENT_GRACE = timedelta(hours=36)
EVENT_TTL_IDLE = 900
EVENT_TTL_LIVE = 120
MEDALS = ("🥇", "🥈", "🥉")
METHOD_WORDS = {"ko": "ko", "tko": "ko", "knockout": "ko", "sub": "sub", "submission": "sub",
                "dec": "dec", "decision": "dec", "points": "dec"}


def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def _event_is_stale(meta: dict) -> bool:
    compact = str(meta.get("date_compact", "") or "")
    if len(compact) == 8 and compact.isdigit():
        try:
            start = datetime.strptime(compact, "%Y%m%d").replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) >= start + timedelta(days=1) + STALE_EVENT_GRACE
        except ValueError:
            return False
    return False


def _legacy_score(picks_dict: dict, results_event: dict):
    """Score the pre-v5 flat picks table (kept so nothing old is lost)."""
    fights = results_event.get("fights", []) if results_event else []
    deltas, resolved = {}, []
    for key, fp in picks_dict.items():
        for uid, picked in fp.items():
            pn = _norm(picked)
            rf = next((f for f in fights if pn in (_norm(f["red"]), _norm(f["blue"]))), None)
            if not rf or not rf.get("winner"):
                continue
            resolved.append((key, uid))
            d = deltas.setdefault(uid, {"correct": 0, "total": 0})
            d["total"] += 1
            d["correct"] += int(_norm(rf["winner"]) == pn)
    return deltas, resolved


def _first_start(event: dict) -> Optional[int]:
    starts = [f["start"] for f in event.get("fights", []) if f.get("start")]
    return min(starts) if starts else event.get("timestamp")


def _parse_pick(text: str):
    """'Jon Jones ko 2' -> ('Jon Jones', 'ko:2'); 'Jones dec' -> ('Jones', 'dec')."""
    words = text.split()
    method = rnd = None
    if words and words[-1].lstrip("r").isdigit() and len(words) > 1:
        rnd = words[-1].lstrip("r")
        words = words[:-1]
    if words and words[-1].lower() in METHOD_WORDS and len(words) > 1:
        method = METHOD_WORDS[words[-1].lower()]
        words = words[:-1]
    if method == "dec":
        value = "dec"
    elif method and rnd:
        value = f"{method}:{rnd}"
    else:
        value = None
    return " ".join(words), value


class UFC(commands.Cog):
    """UFC fight cards, results, fighter cards, live pick'em and betting."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=7833209, force_registration=True)
        self.config.register_guild(
            events={},      # {eid: {meta, picks, methods, bets, odds, settled, scores, posted}}
            picks={},       # legacy flat picks (pre-event-scoping) — still settle-able
            standings={},   # {uid: {correct, total, bonus, best_upset}}
            betting={},     # {uid: {net, won, lost}}
            periods={},     # {"2026-11": {uid: {...}}, "2026": {...}}
            last_card={},   # {"name", "scores"} for `standings card`
            channel=None, ping_role=None, hype_hours=6, live=True,
            sched_events=False, sched_created={},
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self._event: Optional[dict] = None
        self._event_at = 0.0
        self._event_lock = asyncio.Lock()
        self._images: dict[str, Optional[bytes]] = {}
        self._card_view: Optional[CardView] = None

    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        self._card_view = CardView(self)
        self.bot.add_view(self._card_view)
        self.watch.start()

    async def cog_unload(self):
        self.watch.cancel()
        if self._card_view:
            self._card_view.stop()
        if self._session and not self._session.closed:
            await self._session.close()

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        uid = str(user_id)
        for gid, g in (await self.config.all_guilds()).items():
            conf = self.config.guild_from_id(gid)
            async with conf.standings() as s:
                s.pop(uid, None)
            async with conf.betting() as b:
                b.pop(uid, None)

    @property
    def session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ------------------------------------------------------------------ #
    # Data helpers
    # ------------------------------------------------------------------ #
    async def get_event(self, force: bool = False) -> Optional[dict]:
        """Current/next card with odds, cached (2 min near fight time, 15 min otherwise)."""
        async with self._event_lock:
            ev = self._event
            live = ev and (_first_start(ev) or 0) - 3600 <= time.time() <= (_first_start(ev) or 0) + 12 * 3600
            ttl = EVENT_TTL_LIVE if live else EVENT_TTL_IDLE
            if ev and not force and time.time() - self._event_at < ttl:
                return ev
            fresh = await get_upcoming_event(self.session)
            if fresh:
                try:
                    await attach_odds(self.session, fresh)
                except Exception:  # noqa: BLE001
                    log.debug("odds fetch failed", exc_info=True)
                # keep last-known odds for fights whose line was pulled at lock
                if ev and ev.get("id") == fresh.get("id"):
                    old = {sc.fight_key(f): f for f in ev.get("fights", [])}
                    for f in fresh.get("fights", []):
                        o = old.get(sc.fight_key(f)) or {}
                        for side in ("red_odds", "blue_odds"):
                            if f.get(side) is None and o.get(side) is not None:
                                f[side] = o[side]
                self._event, self._event_at = fresh, time.time()
            return self._event

    async def images(self, urls) -> dict:
        need = [u for u in dict.fromkeys(urls) if u and u not in self._images]

        async def one(u):
            try:
                async with self.session.get(u, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    self._images[u] = await r.read() if r.status == 200 else None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                self._images[u] = None

        await asyncio.gather(*(one(u) for u in need))
        if len(self._images) > 600:
            self._images = {k: self._images[k] for k in list(self._images)[-300:]}
        return {u: self._images.get(u) for u in urls if u}

    async def _png(self, fn, *args) -> bytes:
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    @staticmethod
    def _meta(event: dict) -> dict:
        return {"id": event["id"], "name": event["name"], "shortname": event["shortname"],
                "date": event.get("date", ""), "date_compact": event.get("date_compact", "")}

    @staticmethod
    def _match_fighter(event: dict, name: str):
        q = _norm(name)
        if not q:
            return None, None, None
        for f in event.get("fights", []):
            for side, other in (("red", "blue"), ("blue", "red")):
                n = _norm(f[side])
                if q == n or q in n or n.split()[-1] == q:
                    return f, f[side], f[other]
        return None, None, None

    def _snapshot_odds(self, bucket: dict, fight: dict):
        key = sc.fight_key(fight)
        if fight.get("red_odds") is None and fight.get("blue_odds") is None:
            return
        bucket.setdefault("odds", {})[key] = {_norm(fight["red"]): fight.get("red_odds"),
                                              _norm(fight["blue"]): fight.get("blue_odds")}

    async def save_pick(self, guild, event, fight, user, picked: str):
        key, uid = sc.fight_key(fight), str(user.id)
        async with self.config.guild(guild).events() as evs:
            b = evs.setdefault(event["id"], {"meta": {}, "picks": {}})
            b["meta"] = self._meta(event)
            b.setdefault("picks", {}).setdefault(key, {})[uid] = picked
            self._snapshot_odds(b, fight)

    async def save_method(self, guild, event, fight, user, method: Optional[str]):
        key, uid = sc.fight_key(fight), str(user.id)
        async with self.config.guild(guild).events() as evs:
            b = evs.setdefault(event["id"], {"meta": self._meta(event), "picks": {}})
            m = b.setdefault("methods", {}).setdefault(key, {})
            if method is None:
                m.pop(uid, None)
            else:
                method_key, _, rnd = method.partition(":")
                m[uid] = {"method": method_key, "round": rnd or None}

    async def _my_picks(self, guild, event, uid: str) -> dict:
        b = (await self.config.guild(guild).events()).get(event["id"], {})
        out = {}
        for f in event.get("fights", []):
            key = sc.fight_key(f)
            pick = (b.get("picks", {}).get(key) or {}).get(uid)
            m = (b.get("methods", {}).get(key) or {}).get(uid)
            method = None
            if m:
                method = m["method"] if m["method"] == "dec" else f"{m['method']}:{m.get('round')}"
            if pick:
                out[key] = {"pick": pick, "method": method}
        return out

    async def _splits(self, guild, event) -> dict:
        b = (await self.config.guild(guild).events()).get(event["id"], {})
        out = {}
        for f in event.get("fights", []):
            fp = b.get("picks", {}).get(sc.fight_key(f)) or {}
            r = sum(1 for v in fp.values() if _norm(v) == _norm(f["red"]))
            out[sc.fight_key(f)] = (r, len(fp) - r)
        return out

    # ------------------------------------------------------------------ #
    # Interactions
    # ------------------------------------------------------------------ #
    async def open_pick_editor(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("Picks live on a server.", ephemeral=True)
        event = await self.get_event()
        if not event:
            return await interaction.response.send_message("Couldn't load the card right now.", ephemeral=True)
        mine = await self._my_picks(interaction.guild, event, str(interaction.user.id))
        editor = PickEditor(self, interaction.guild, interaction.user, event, mine)
        if not editor.open_fights:
            return await interaction.response.send_message("⏱️ Every fight on this card is locked.", ephemeral=True)
        await interaction.response.send_message(editor.caption(), view=editor, ephemeral=True)

    async def show_my_picks(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message("Picks live on a server.", ephemeral=True)
        event = await self.get_event()
        if not event:
            return await interaction.response.send_message("Couldn't load the card right now.", ephemeral=True)
        await interaction.response.send_message(
            embed=await self._mine_embed(interaction.guild, event, interaction.user), ephemeral=True)

    async def _mine_embed(self, guild, event, user) -> discord.Embed:
        uid = str(user.id)
        mine = await self._my_picks(guild, event, uid)
        b = (await self.config.guild(guild).events()).get(event["id"], {})
        lines = []
        for f in event.get("fights", []):
            key = sc.fight_key(f)
            m = mine.get(key)
            lock = "🔒 " if is_locked(f) else ""
            if m:
                how = f" · {sc.method_choice_label(m['method'])}" if m.get("method") else ""
                bet = (b.get("bets", {}).get(key) or {}).get(uid)
                bet_txt = f" · 💰 {bet['amount']:,} @ {sc.fmt_odds(bet.get('odds'))}" if bet else ""
                lines.append(f"{lock}✅ **{m['pick']}**{how}{bet_txt}\n╰ vs {f['blue'] if m['pick'] == f['red'] else f['red']}")
            else:
                lines.append(f"{lock}⬜ {f['red']} vs {f['blue']}")
        e = discord.Embed(title=f"📋 {user.display_name}'s picks · {event['shortname']}",
                          description="\n".join(lines)[:4000] or "No fights.", color=embeds.UFC_BLUE)
        e.set_footer(text="🔒 = locked · tap 'Make my picks' to change open fights")
        return e

    # ------------------------------------------------------------------ #
    # Commands: info
    # ------------------------------------------------------------------ #
    @commands.group(name="ufc", invoke_without_command=True)
    async def ufc(self, ctx: commands.Context):
        """UFC cards, picks, betting and standings."""
        p = ctx.clean_prefix
        e = discord.Embed(title="🥊  UFC Fight Night", color=embeds.UFC_RED, description=(
            f"`{p}ufc card` — the card, with a **Make my picks** button\n"
            f"`{p}ufc tape [fighter]` — Tale of the Tape (main event by default)\n"
            f"`{p}ufc results` — last event's results\n"
            f"`{p}ufc fighter <name>` — fighter card\n"
            f"`{p}ufc pick <name> [ko 2|sub 1|dec]` — pick by typing instead\n"
            f"`{p}ufc mypicks` · `{p}ufc picks` — yours / everyone's\n"
            f"`{p}ufc bet <amount> <name>` — bet at the real odds · `{p}ufc unbet` · `{p}ufc bets`\n"
            f"`{p}ufc standings [card|month|year|all]` — leaderboard\n\n"
            "Fights lock when their part of the card starts. Results, scoring and payouts are automatic."
        ))
        await ctx.send(embed=e)

    async def _send_card(self, destination, event: dict, guild: Optional[discord.Guild], content: str = None,
                         ping=None):
        splits = await self._splits(guild, event) if guild else None
        imgs = await self.images(render.card_image_urls(event))
        png = await self._png(render.render_card, event, imgs, splits)
        segs = []
        for seg, label in (("prelims2", "Early prelims"), ("prelims1", "Prelims"), ("main", "Main card")):
            starts = [f["start"] for f in event.get("fights", []) if f.get("segment") == seg and f.get("start")]
            if starts:
                segs.append(f"{label} <t:{min(starts)}:t>")
        text = content or f"## 🥊 {event['name']}"
        if segs:
            text += "\n" + " · ".join(segs) + f" · <t:{_first_start(event)}:R>"
        text += "\n-# Tap **Make my picks**: winner + optional method & round. Each fight locks when its segment starts."
        if ping:
            text = f"{ping.mention}\n{text}"
        return await destination.send(
            text, file=png_file(png, "ufc-card"), view=self._card_view,
            allowed_mentions=discord.AllowedMentions(roles=True) if ping else discord.AllowedMentions.none(),
        )

    @ufc.command(name="card")
    @commands.cooldown(1, 15, commands.BucketType.channel)
    async def ufc_card(self, ctx: commands.Context):
        """The upcoming card as a poster, with buttons to make your picks."""
        async with ctx.typing():
            event = await self.get_event()
            if not event:
                return await ctx.send(embed=embeds.error_embed(
                    f"Couldn't fetch the upcoming card from ESPN. `{ctx.clean_prefix}ufc apistatus` shows why."))
            try:
                await self._send_card(ctx, event, ctx.guild)
            except Exception:  # noqa: BLE001
                log.exception("card render failed")
                await ctx.send(embed=embeds.card_embed(event), view=self._card_view)

    @ufc.command(name="tape", aliases=["taleofthetape", "tott"])
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def ufc_tape(self, ctx: commands.Context, *, fighter: str = None):
        """Tale of the Tape for the main event, or the fight with this fighter."""
        async with ctx.typing():
            event = await self.get_event()
            if not event or not event.get("fights"):
                return await ctx.send(embed=embeds.error_embed("No card available right now."))
            fight = event["fights"][0]
            if fighter:
                fight, _, _ = self._match_fighter(event, fighter)
                if not fight:
                    return await ctx.send(embed=embeds.error_embed(f"**{fighter}** isn't on this card."))
            imgs = await self.images(render.tape_image_urls(fight))
            png = await self._png(render.render_tape, fight, event["shortname"], imgs)
        await ctx.send(file=png_file(png, "tale-of-the-tape"))

    @ufc.command(name="results")
    @commands.cooldown(1, 15, commands.BucketType.channel)
    async def ufc_results(self, ctx: commands.Context, date: str = None):
        """Results of the most recent event (or `YYYY-MM-DD`)."""
        async with ctx.typing():
            if date:
                event = await get_event_on_date(self.session, date.replace("-", "").replace("/", ""))
            else:
                event = await get_recent_event(self.session)
            if not event:
                return await ctx.send(embed=embeds.error_embed("Couldn't fetch results right now."))
            try:
                imgs = await self.images(render.card_image_urls(event))
                png = await self._png(render.render_card, event, imgs, None)
                await ctx.send(f"## 📊 Results · {event['name']}", file=png_file(png, "ufc-results"))
            except Exception:  # noqa: BLE001
                log.exception("results render failed")
                await ctx.send(embed=embeds.results_embed(event))

    @ufc.command(name="fighter")
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def ufc_fighter(self, ctx: commands.Context, *, name: str):
        """A fighter card: record, bio and recent fights."""
        async with ctx.typing():
            f = await get_fighter(self.session, name)
            if not f:
                return await ctx.send(embed=embeds.error_embed(
                    f"Couldn't find **{name}**. Try their full fight name, e.g. `Jon Jones`."))
            try:
                imgs = await self.images([f.get("headshot")])
                png = await self._png(render.render_fighter, f, imgs)
                await ctx.send(file=png_file(png, "fighter"))
            except Exception:  # noqa: BLE001
                log.exception("fighter render failed")
                await ctx.send(embed=embeds.fighter_embed(f))

    @ufc.command(name="apistatus")
    @checks.admin_or_permissions(administrator=True)
    async def ufc_apistatus(self, ctx: commands.Context):
        """Test the ESPN endpoints and report failures."""
        async with ctx.typing():
            results = await espn_api_status(self.session)
        lines = [f"aiohttp: {getattr(aiohttp, '__version__', 'unknown')}"]
        healthy = True
        for label, ok, detail in results:
            state = "OK" if ok else ("WARN" if "(legacy)" in label else "FAIL")
            healthy &= ok or "(legacy)" in label
            lines.append(f"{state} {label}: {detail}")
        await ctx.send(embed=discord.Embed(title="🥊 UFC API Status", description=box("\n".join(lines)[:3800]),
                                           color=0x2ECC71 if healthy else 0xE67E22))

    # ------------------------------------------------------------------ #
    # Commands: picks & bets
    # ------------------------------------------------------------------ #
    @ufc.command(name="pick")
    @commands.guild_only()
    async def ufc_pick(self, ctx: commands.Context, *, pick: str):
        """Pick a fighter by typing. Add a bonus: `ko 2`, `sub 1` or `dec`.

        Examples: `[p]ufc pick Gane` · `[p]ufc pick Gane ko 2` · `[p]ufc pick Harrison dec`
        """
        name, method = _parse_pick(pick)
        event = await self.get_event()
        if not event:
            return await ctx.send(embed=embeds.error_embed("Couldn't fetch the upcoming card."))
        fight, picked, opponent = self._match_fighter(event, name)
        if not fight:
            return await ctx.send(embed=embeds.error_embed(
                f"**{name}** isn't on the upcoming card. See `{ctx.clean_prefix}ufc card`."))
        if is_locked(fight):
            return await ctx.send(embed=embeds.error_embed(f"⏱️ **{fight['red']} vs {fight['blue']}** has started, picks are locked."))
        if method and method != "dec" and int(method.split(":")[1]) > (fight.get("rounds") or 3):
            return await ctx.send(embed=embeds.error_embed(f"That fight is only {fight.get('rounds') or 3} rounds."))
        await self.save_pick(ctx.guild, event, fight, ctx.author, picked)
        if method:
            await self.save_method(ctx.guild, event, fight, ctx.author, method)
        how = f" by **{sc.method_choice_label(method)}**" if method else ""
        odds = fight.get("red_odds" if picked == fight["red"] else "blue_odds")
        await ctx.send(f"✅ {ctx.author.mention} picked **{picked}**{how} over {opponent}"
                       + (f" ({sc.fmt_odds(odds)})" if odds is not None else ""),
                       allowed_mentions=discord.AllowedMentions.none())

    @ufc.command(name="mypicks")
    @commands.guild_only()
    async def ufc_mypicks(self, ctx: commands.Context):
        """Your picks for the upcoming card."""
        event = await self.get_event()
        if not event:
            return await ctx.send(embed=embeds.error_embed("Couldn't fetch the upcoming card."))
        await ctx.send(embed=await self._mine_embed(ctx.guild, event, ctx.author), view=self._card_view)

    @ufc.command(name="picks")
    @commands.guild_only()
    async def ufc_picks(self, ctx: commands.Context):
        """Everyone's picks for the upcoming card."""
        event = await self.get_event()
        evs = await self.config.guild(ctx.guild).events()
        if not event:
            return await ctx.send(embed=embeds.error_embed("Couldn't fetch the upcoming card."))
        picks = dict(evs.get(event["id"], {}).get("picks", {}))
        for k, v in (await self.config.guild(ctx.guild).picks() or {}).items():
            picks.setdefault(k, {}).update(v)
        await ctx.send(embed=embeds.picks_embed(event, picks, ctx.guild), view=self._card_view)

    @ufc.command(name="bet")
    @commands.guild_only()
    async def ufc_bet(self, ctx: commands.Context, amount: int, *, fighter_name: str):
        """Bet credits on a fighter at the current odds (also sets your pick).

        `+250` underdog: 100 returns 350 · `-300` favourite: 100 returns 133.
        Max 3 bets per card. Betting the same fight again changes your stake.
        """
        if amount <= 0:
            return await ctx.send(embed=embeds.error_embed("Bet amount must be positive."))
        event = await self.get_event()
        if not event:
            return await ctx.send(embed=embeds.error_embed("Couldn't fetch the upcoming card."))
        fight, picked, opponent = self._match_fighter(event, fighter_name)
        if not fight:
            return await ctx.send(embed=embeds.error_embed(f"**{fighter_name}** isn't on the upcoming card."))
        if is_locked(fight):
            return await ctx.send(embed=embeds.error_embed("⏱️ That fight has started, betting is locked."))
        key, eid, uid = sc.fight_key(fight), event["id"], str(ctx.author.id)
        odds = fight.get("red_odds" if picked == fight["red"] else "blue_odds")
        currency = await bank.get_currency_name(ctx.guild)
        bets = (await self.config.guild(ctx.guild).events()).get(eid, {}).get("bets", {})
        existing = (bets.get(key) or {}).get(uid)
        prev = existing["amount"] if existing else 0
        slots = {k for k, ub in bets.items() if uid in ub}
        if key not in slots and len(slots) >= MAX_BETS:
            return await ctx.send(embed=embeds.error_embed(
                f"You're at the **{MAX_BETS}-bet max** for this card. `{ctx.clean_prefix}ufc unbet <fighter>` frees a slot."))
        delta = amount - prev
        if delta > 0:
            if not await bank.can_spend(ctx.author, delta):
                bal = await bank.get_balance(ctx.author)
                return await ctx.send(embed=embeds.error_embed(
                    f"Not enough {currency}. You have **{bal:,}**, need **{delta:,}** more."))
            await bank.withdraw_credits(ctx.author, delta)
        elif delta < 0:
            await bank.deposit_credits(ctx.author, -delta)
        async with self.config.guild(ctx.guild).events() as evs:
            b = evs.setdefault(eid, {"meta": {}, "picks": {}})
            b["meta"] = self._meta(event)
            b.setdefault("bets", {}).setdefault(key, {})[uid] = {"pick": picked, "amount": amount, "odds": odds}
            b.setdefault("picks", {}).setdefault(key, {})[uid] = picked
            self._snapshot_odds(b, fight)
        ret = sc.payout(amount, odds)
        e = discord.Embed(title=f"💰 Bet locked: {picked}", color=embeds.GREEN, description=(
            f"**{amount:,} {currency}** on **{picked}** over {opponent}\n"
            f"Odds **{sc.fmt_odds(odds)}** → a win returns **{ret:,} {currency}** (+{ret - amount:,})"
            + (f"\n-# Changed from {prev:,}" if existing else "")
            + ("" if odds is not None else "\n-# No line yet, so this pays even money (2×).")
        ))
        e.set_footer(text=f"{MAX_BETS - len(slots | {key})} bet slot(s) left on {event['shortname']}")
        await ctx.send(embed=e)

    @ufc.command(name="unbet")
    @commands.guild_only()
    async def ufc_unbet(self, ctx: commands.Context, *, fighter_name: str):
        """Cancel a bet before the fight starts and get refunded."""
        event = await self.get_event()
        if not event:
            return await ctx.send(embed=embeds.error_embed("Couldn't fetch the upcoming card."))
        fight, _, _ = self._match_fighter(event, fighter_name)
        if not fight:
            return await ctx.send(embed=embeds.error_embed(f"**{fighter_name}** isn't on the upcoming card."))
        if is_locked(fight):
            return await ctx.send(embed=embeds.error_embed("⏱️ That fight has started, the bet stands."))
        key, eid, uid = sc.fight_key(fight), event["id"], str(ctx.author.id)
        async with self.config.guild(ctx.guild).events() as evs:
            ub = evs.get(eid, {}).get("bets", {}).get(key) or {}
            bet = ub.pop(uid, None)
        if not bet:
            return await ctx.send(embed=embeds.error_embed("You don't have a bet on that fight."))
        await bank.deposit_credits(ctx.author, bet["amount"])
        currency = await bank.get_currency_name(ctx.guild)
        await ctx.send(f"↩️ Cancelled your bet on **{bet['pick']}** and refunded **{bet['amount']:,} {currency}**. "
                       "Your pick still stands.")

    @ufc.command(name="bets")
    @commands.guild_only()
    async def ufc_bets(self, ctx: commands.Context):
        """Your open bets and what they'd pay."""
        event = await self.get_event()
        evs = await self.config.guild(ctx.guild).events()
        currency = await bank.get_currency_name(ctx.guild)
        uid = str(ctx.author.id)
        lines = []
        for eid, b in evs.items():
            for key, ub in (b.get("bets") or {}).items():
                bet = ub.get(uid)
                if bet:
                    ret = sc.payout(bet["amount"], bet.get("odds"))
                    lines.append(f"💰 **{bet['pick']}** · {bet['amount']:,} @ {sc.fmt_odds(bet.get('odds'))} "
                                 f"→ returns **{ret:,}**\n╰ {b.get('meta', {}).get('shortname', '')}")
        e = discord.Embed(title=f"💰 {ctx.author.display_name}'s bets", color=embeds.UFC_GOLD,
                          description="\n".join(lines) or "No open bets.")
        if event:
            e.set_footer(text=f"Max {MAX_BETS} per card · amounts in {currency}")
        await ctx.send(embed=e)

    # ------------------------------------------------------------------ #
    # Standings
    # ------------------------------------------------------------------ #
    @ufc.command(name="standings", aliases=["leaderboard", "lb"])
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def ufc_standings(self, ctx: commands.Context, period: str = "all"):
        """Pick'em leaderboard: `card` (last event), `month`, `year` or `all`."""
        g = await self.config.guild(ctx.guild).all()
        now = datetime.now(timezone.utc)
        period = period.lower()
        betting = {}
        if period in ("card", "last", "event"):
            table = (g["last_card"] or {}).get("scores", {})
            title, sub = "Last card", (g["last_card"] or {}).get("name", "No finished card yet")
            betting = table
        elif period in ("month", "monthly"):
            table, title, sub = g["periods"].get(now.strftime("%Y-%m"), {}), "Monthly standings", now.strftime("%B %Y")
            betting = table
        elif period in ("year", "season", "yearly"):
            table, title, sub = g["periods"].get(now.strftime("%Y"), {}), "Season standings", now.strftime("%Y season")
            betting = table
        else:
            table, title, sub = g["standings"], "All-time standings", "Every card since the start"
            betting = g["betting"]
        rows = []
        for uid, s in table.items():
            if not s.get("total"):
                continue
            m = ctx.guild.get_member(int(uid))
            up = s.get("best_upset")
            rows.append({
                "name": m.display_name if m else f"User {uid[-4:]}",
                "points": sc.points(s), "correct": s.get("correct", 0), "total": s.get("total", 0),
                "bonus": s.get("bonus", 0), "net": (betting.get(uid) or {}).get("net", 0),
                "best_upset": f"Best upset: {up['fighter']} {sc.fmt_odds(up['ml'])}" if up else "",
            })
        rows.sort(key=lambda r: (-r["points"], -r["correct"], r["name"].lower()))
        currency = await bank.get_currency_name(ctx.guild)
        async with ctx.typing():
            png = await self._png(render.render_standings, title, f"{sub} · {len(rows)} player(s)", rows[:20], currency)
        await ctx.send(file=png_file(png, "standings"))

    # ------------------------------------------------------------------ #
    # Settling (automatic + manual)
    # ------------------------------------------------------------------ #
    async def process_event(self, guild: discord.Guild, event: dict, announce: bool) -> list:
        """Settle every newly-finished fight of `event` for this guild. Returns summaries."""
        eid = event["id"]
        summaries = []
        async with self.config.guild(guild).events() as evs:
            bucket = evs.get(eid)
            if bucket is None:
                if not announce:
                    return []
                bucket = evs.setdefault(eid, {"meta": self._meta(event), "picks": {}})
            settled = set(bucket.get("settled", []))
            for f in event.get("fights", []):
                if not f.get("completed") or sc.fight_key(f) in settled:
                    continue
                s = sc.settle_fight(bucket, f)
                if s is not None and (s["total"] or s["bets"] or s["refunds"] or announce):
                    summaries.append(s)
            for f in event.get("fights", []):
                if not is_locked(f):
                    self._snapshot_odds(bucket, f)
        if summaries:
            await self._apply(guild, event, summaries)
        return summaries

    async def _apply(self, guild, event, summaries):
        conf = self.config.guild(guild)
        month = (event.get("date_compact") or datetime.now(timezone.utc).strftime("%Y%m%d"))
        keys = (f"{month[:4]}-{month[4:6]}", month[:4])
        async with conf.standings() as st, conf.betting() as bt, conf.periods() as per:
            for s in summaries:
                sc.merge_into(st, s["deltas"])
                for k in keys:
                    sc.merge_into(per.setdefault(k, {}), s["deltas"], keys=("correct", "total", "bonus", "net"))
                for uid, d in s["deltas"].items():
                    if d.get("won") or d.get("lost"):
                        b = bt.setdefault(uid, {"net": 0, "won": 0, "lost": 0})
                        b["net"] += d["net"]
                        b["won"] += d["won"]
                        b["lost"] += d["lost"]
        for s in summaries:
            for uid, _pick, amount, paid, won, _odds in s["bets"]:
                if won and paid:
                    await self._deposit(guild, uid, paid)
            for uid, amount in s["refunds"]:
                await self._deposit(guild, uid, amount)

    async def _deposit(self, guild, uid, amount):
        member = guild.get_member(int(uid))
        if member and amount > 0:
            try:
                await bank.deposit_credits(member, amount)
            except Exception:  # noqa: BLE001
                log.warning("couldn't pay %s %s in %s", amount, uid, guild.id)

    def _name(self, guild, uid):
        m = guild.get_member(int(uid))
        return m.display_name if m else f"User {uid[-4:]}"

    async def result_embed(self, guild, s: dict) -> discord.Embed:
        f = s["fight"]
        currency = await bank.get_currency_name(guild)
        if s["void"]:
            e = discord.Embed(title=f"⚪ {f['red']} vs {f['blue']}: {f.get('method') or 'No decision'}",
                              description="No winner, so picks are voided and bets refunded.", color=embeds.GREY)
            return e
        loser = f["blue"] if f["winner"] == f["red"] else f["red"]
        how = f.get("method") or "Final"
        if f.get("method_detail"):
            how += f" ({f['method_detail']})"
        if f.get("method_key") in ("ko", "sub"):
            how += f" · R{f.get('round')} {f.get('time', '')}"
        e = discord.Embed(title=f"🥊 {f['winner']} def. {loser}", description=how, color=embeds.UFC_RED)
        info = f.get("red_info") if f["winner"] == f["red"] else f.get("blue_info")
        if info and info.get("headshot"):
            e.set_thumbnail(url=info["headshot"])
        if s["total"]:
            right = [self._name(guild, u) + (" 🎯" * b) for u, _p, ok, b in s["pickers"] if ok]
            text = f"**{s['called']}/{s['total']}** called it"
            if right:
                text += ": " + ", ".join(right[:12]) + (" …" if len(right) > 12 else "")
            e.add_field(name="🗳️ Pick'em", value=text, inline=False)
        if s["upsets"]:
            e.add_field(name="🐶 Upset called!", value=", ".join(
                f"{self._name(guild, u)} ({sc.fmt_odds(ml)})" for u, _p, ml in s["upsets"]), inline=False)
        if s["bets"]:
            e.add_field(name="💰 Bets", value="\n".join(
                f"{'💸' if won else '📉'} {self._name(guild, u)}: "
                + (f"+{paid - amt:,}" if won else f"−{amt:,}") + f" {currency}"
                for u, _p, amt, paid, won, _o in s["bets"]), inline=False)
        e.set_footer(text="🎯 = nailed the method/round bonus")
        return e

    async def recap_embed(self, guild, event, bucket) -> Optional[discord.Embed]:
        scores = bucket.get("scores") or {}
        if not scores:
            return None
        ranked = sorted(scores.items(), key=lambda kv: (-sc.points(kv[1]), -kv[1].get("correct", 0)))
        e = discord.Embed(title=f"🏆 {event['shortname']} pick'em results", color=embeds.UFC_GOLD)
        lines = []
        for i, (uid, s) in enumerate(ranked[:10]):
            place = MEDALS[i] if i < 3 else f"`{i + 1}.`"
            net = s.get("net", 0)
            money = f" · {'+' if net >= 0 else '−'}{abs(net):,}" if (s.get("won") or s.get("lost")) else ""
            bonus = f", +{s['bonus']} bonus" if s.get("bonus") else ""
            lines.append(f"{place} **{self._name(guild, uid)}** — {sc.points(s)} pts "
                         f"({s.get('correct', 0)}/{s.get('total', 0)}{bonus}){money}")
        e.description = "\n".join(lines)
        winner_uid, best = ranked[0]
        e.add_field(name="👑 Card champion", value=f"**{self._name(guild, winner_uid)}** with {sc.points(best)} points",
                    inline=False)
        upsets = [(uid, s["best_upset"]) for uid, s in scores.items() if s.get("best_upset")]
        if upsets:
            uid, up = max(upsets, key=lambda x: x[1]["ml"])
            e.add_field(name="🐶 Best upset call", value=f"{self._name(guild, uid)}: **{up['fighter']}** ({sc.fmt_odds(up['ml'])})",
                        inline=False)
        e.set_footer(text="See the full table with `ufc standings card`")
        return e

    @ufc.command(name="settle")
    @checks.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def ufc_settle(self, ctx: commands.Context, date: str = None):
        """[Admin] Settle finished fights now (normally automatic).

        Also clears out cards that are long finished and refunds any bets on fights that never happened.
        Pass a date (`YYYY-MM-DD`) to settle that day's card.
        """
        guild = ctx.guild
        async with ctx.typing():
            evs = await self.config.guild(guild).events()
            if date:
                ev = await get_event_on_date(self.session, date.replace("-", "").replace("/", ""))
                if not ev:
                    return await ctx.send(embed=embeds.error_embed(f"No UFC event found on **{date}**."))
                targets = [ev]
            else:
                targets = []
                for eid, b in evs.items():
                    ev = await get_event_by_id(self.session, eid, b.get("meta", {}).get("date_compact", ""))
                    if ev:
                        targets.append(ev)
            summaries = []
            for ev in targets:
                summaries += await self.process_event(guild, ev, announce=False)
            voided, refunded = await self._cleanup_stale(guild)
            legacy = await self.config.guild(guild).picks()
            legacy_scored = 0
            if legacy:
                recent = await get_recent_event(self.session)
                if recent:
                    deltas, resolved = _legacy_score(legacy, recent)
                    async with self.config.guild(guild).standings() as st:
                        for uid, d in deltas.items():
                            t = st.setdefault(uid, {"correct": 0, "total": 0})
                            t["correct"] += d["correct"]
                            t["total"] += d["total"]
                            legacy_scored += d["total"]
                    async with self.config.guild(guild).picks() as lw:
                        for key, uid in resolved:
                            (lw.get(key) or {}).pop(uid, None)
        scored = sum(s["total"] for s in summaries) + legacy_scored
        bets = sum(len(s["bets"]) for s in summaries)
        msg = f"✅ Settled **{len(summaries)}** fight(s): **{scored}** pick(s) scored, **{bets}** bet(s) paid out."
        if voided or refunded:
            currency = await bank.get_currency_name(guild)
            msg += f"\n🧹 Cleared {voided} pick(s) from finished cards and refunded {refunded:,} {currency} on fights that never happened."
        await ctx.send(msg)

    async def _cleanup_stale(self, guild) -> tuple[int, int]:
        voided = refunded = 0
        refunds: dict[str, int] = {}
        async with self.config.guild(guild).events() as evs:
            for eid in list(evs):
                b = evs[eid]
                if not _event_is_stale(b.get("meta", {})):
                    continue
                voided += sum(len(v) for v in (b.get("picks") or {}).values())
                for ub in (b.get("bets") or {}).values():
                    for uid, bet in ub.items():
                        refunds[uid] = refunds.get(uid, 0) + int(bet.get("amount", 0) or 0)
                del evs[eid]
        for uid, amt in refunds.items():
            await self._deposit(guild, uid, amt)
            refunded += amt
        return voided, refunded

    @ufc.command(name="clearpicks")
    @checks.admin_or_permissions(manage_guild=True)
    @commands.guild_only()
    async def ufc_clearpicks(self, ctx: commands.Context):
        """[Admin] Clear all open picks and bets (bets are refunded)."""
        evs = await self.config.guild(ctx.guild).events()
        total = 0
        for b in evs.values():
            for ub in (b.get("bets") or {}).values():
                for uid, bet in ub.items():
                    await self._deposit(ctx.guild, uid, int(bet.get("amount", 0) or 0))
                    total += int(bet.get("amount", 0) or 0)
        await self.config.guild(ctx.guild).events.set({})
        await self.config.guild(ctx.guild).picks.set({})
        await ctx.send("✅ All picks and bets cleared." + (f" Refunded {total:,}." if total else ""))

    @ufc.command(name="resetstandings")
    @checks.admin_or_permissions(administrator=True)
    @commands.guild_only()
    async def ufc_resetstandings(self, ctx: commands.Context):
        """[Admin] Wipe all standings (asks for confirmation)."""
        await ctx.send("⚠️ This deletes **all** standings, monthly/season tables and betting stats. Type `confirm`.")
        try:
            msg = await self.bot.wait_for("message", timeout=30,
                                          check=lambda m: m.author == ctx.author and m.channel == ctx.channel)
        except asyncio.TimeoutError:
            return await ctx.send("Timed out, nothing changed.")
        if _norm(msg.content) != "confirm":
            return await ctx.send("Cancelled.")
        conf = self.config.guild(ctx.guild)
        for item in (conf.standings, conf.betting, conf.periods, conf.last_card):
            await item.set({})
        await ctx.send("✅ Standings reset.")

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    @commands.group(name="ufcset")
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def ufcset(self, ctx: commands.Context):
        """Fight-night automation settings."""

    @ufcset.command(name="channel")
    async def ufcset_channel(self, ctx, channel: Optional[discord.TextChannel] = None):
        """Fight-night channel: card post, live results and recap. Leave empty to turn off."""
        await self.config.guild(ctx.guild).channel.set(channel.id if channel else None)
        await ctx.send(f"🥊 Fight night posts go to {channel.mention}." if channel else "Fight-night posts turned off.")

    @ufcset.command(name="role")
    async def ufcset_role(self, ctx, role: Optional[discord.Role] = None):
        """Role to ping when the card is posted. Leave empty for no ping."""
        await self.config.guild(ctx.guild).ping_role.set(role.id if role else None)
        await ctx.send(f"Will ping {role.mention}." if role else "No role ping.",
                       allowed_mentions=discord.AllowedMentions.none())

    @ufcset.command(name="hype")
    async def ufcset_hype(self, ctx, hours: int):
        """Post the card this many hours before the first fight (0 = don't auto-post)."""
        await self.config.guild(ctx.guild).hype_hours.set(max(0, min(hours, 168)))
        await ctx.send(f"Card auto-post: {f'{hours}h before the first fight' if hours else 'off'}.")

    @ufcset.command(name="live")
    async def ufcset_live(self, ctx, on: bool):
        """Post each result as it happens (`on`/`off`). Scoring is automatic either way."""
        await self.config.guild(ctx.guild).live.set(on)
        await ctx.send(f"Live results {'on' if on else 'off'}.")

    @ufcset.command(name="events")
    async def ufcset_events(self, ctx, on: bool):
        """Create a Discord scheduled event for each card (`on`/`off`). Needs Manage Events."""
        if on and not ctx.guild.me.guild_permissions.manage_events:
            return await ctx.send("I need the **Manage Events** permission for that.")
        await self.config.guild(ctx.guild).sched_events.set(on)
        await ctx.send(f"Discord events for each card: {'on' if on else 'off'}.")

    @ufcset.command(name="post")
    async def ufcset_post(self, ctx):
        """Post the card to the fight-night channel now (with the role ping)."""
        g = await self.config.guild(ctx.guild).all()
        channel = ctx.guild.get_channel(g["channel"]) if g["channel"] else None
        if channel is None:
            return await ctx.send(f"Set a channel first: `{ctx.clean_prefix}ufcset channel #channel`.")
        event = await self.get_event()
        if not event:
            return await ctx.send("Couldn't fetch the card.")
        async with ctx.typing():
            await self._send_card(channel, event, ctx.guild, ping=ctx.guild.get_role(g["ping_role"]) if g["ping_role"] else None)
        async with self.config.guild(ctx.guild).events() as evs:
            b = evs.setdefault(event["id"], {"meta": self._meta(event), "picks": {}})
            b.setdefault("posted", {})["hype"] = True
        await ctx.tick()

    @ufcset.command(name="settings")
    async def ufcset_settings(self, ctx):
        """Show fight-night settings."""
        g = await self.config.guild(ctx.guild).all()
        ch = ctx.guild.get_channel(g["channel"]) if g["channel"] else None
        role = ctx.guild.get_role(g["ping_role"]) if g["ping_role"] else None
        await ctx.send(box(
            f"Channel        : {'#' + ch.name if ch else 'off'}\n"
            f"Ping role      : {'@' + role.name if role else 'none'}\n"
            f"Card auto-post : {str(g['hype_hours']) + 'h before' if g['hype_hours'] else 'off'}\n"
            f"Live results   : {'on' if g['live'] else 'off'}\n"
            f"Discord events : {'on' if g['sched_events'] else 'off'}",
            lang="yaml"))

    # ------------------------------------------------------------------ #
    # The watcher
    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=3)
    async def watch(self):
        await self.bot.wait_until_red_ready()
        try:
            await self._tick()
        except Exception:  # noqa: BLE001
            log.exception("fight-night watcher failed")

    async def _tick(self):
        guilds = await self.config.all_guilds()
        if not guilds:
            return
        event = await self.get_event()
        now = time.time()
        for gid, g in guilds.items():
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            channel = guild.get_channel(g["channel"]) if g.get("channel") else None
            if event:
                await self._maybe_schedule(guild, g, event)
                await self._maybe_hype(guild, g, event, channel, now)
                summaries = await self.process_event(guild, event, announce=bool(channel and g.get("live")))
                if channel and g.get("live"):
                    for s in summaries:
                        try:
                            await channel.send(embed=await self.result_embed(guild, s))
                        except discord.HTTPException:
                            pass
                await self._maybe_recap(guild, event, channel)
            # older cards that still hold picks (e.g. the bot was offline on fight night)
            for eid, b in (g.get("events") or {}).items():
                if event and eid == event.get("id"):
                    continue
                if not (b.get("picks") or b.get("bets")):
                    continue
                old = await get_event_by_id(self.session, eid, b.get("meta", {}).get("date_compact", ""))
                if old:
                    await self.process_event(guild, old, announce=False)
                    await self._maybe_recap(guild, old, channel)
            await self._cleanup_stale(guild)

    async def _maybe_schedule(self, guild, g, event):
        if not g.get("sched_events") or event["id"] in (g.get("sched_created") or {}):
            return
        start = _first_start(event)
        if not start or start < time.time() + 600 or start > time.time() + 14 * 86400:
            return
        if not guild.me.guild_permissions.manage_events:
            return
        main = event["fights"][0] if event.get("fights") else None
        desc = f"Main event: {main['red']} vs {main['blue']} ({main.get('weight_class', '')})" if main else ""
        try:
            se = await guild.create_scheduled_event(
                name=event["name"][:100], description=(desc + "\nMake your picks with the UFC bot!")[:1000],
                start_time=datetime.fromtimestamp(start, timezone.utc),
                end_time=datetime.fromtimestamp(start + 7 * 3600, timezone.utc),
                entity_type=discord.EntityType.external, location=(event.get("location") or "UFC")[:100],
                privacy_level=discord.PrivacyLevel.guild_only,
            )
        except discord.HTTPException:
            log.warning("couldn't create scheduled event in %s", guild.id, exc_info=True)
            return
        async with self.config.guild(guild).sched_created() as sc_map:
            sc_map[event["id"]] = se.id

    async def _maybe_hype(self, guild, g, event, channel, now):
        hours = g.get("hype_hours") or 0
        start = _first_start(event)
        if not channel or not hours or not start:
            return
        if not (start - hours * 3600 <= now < start + 3600):
            return
        bucket = (g.get("events") or {}).get(event["id"]) or {}
        if (bucket.get("posted") or {}).get("hype"):
            return
        role = guild.get_role(g["ping_role"]) if g.get("ping_role") else None
        try:
            await self._send_card(channel, event, guild, content=f"## 🥊 It's fight night: {event['name']}", ping=role)
        except discord.HTTPException:
            return
        async with self.config.guild(guild).events() as evs:
            b = evs.setdefault(event["id"], {"meta": self._meta(event), "picks": {}})
            b.setdefault("posted", {})["hype"] = True

    async def _maybe_recap(self, guild, event, channel):
        fights = event.get("fights", [])
        if not fights or not all(f.get("completed") for f in fights):
            return
        evs = await self.config.guild(guild).events()
        bucket = evs.get(event["id"])
        if not bucket or (bucket.get("posted") or {}).get("recap"):
            return
        embed = await self.recap_embed(guild, event, bucket)
        if embed and channel:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                pass
        async with self.config.guild(guild).events() as evs_w:
            b = evs_w.get(event["id"])
            if b is not None:
                b.setdefault("posted", {})["recap"] = True
        if bucket.get("scores"):
            await self.config.guild(guild).last_card.set({"name": event["name"], "scores": bucket["scores"]})


async def setup(bot: Red):
    await bot.add_cog(UFC(bot))
