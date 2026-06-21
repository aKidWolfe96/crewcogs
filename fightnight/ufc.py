"""
UFC Cog for Red-DiscordBot v3.

Picks are tied to the EVENT they were made for, and `settle` re-fetches that
specific event's results by date — so it always settles the right card even
after ESPN's scoreboard has rolled over to a new event.

Commands
  !ufc card                 upcoming fight card
  !ufc results              most recent event results
  !ufc fighter <name>       fighter stats + recent fights
  !ufc pick <name>          lock in a pick for the upcoming card
  !ufc picks                show server picks for the upcoming card
  !ufc standings            pick-em leaderboard
  !ufc settle [YYYY-MM-DD]  [admin] score picks vs results, update standings
  !ufc clearpicks           [admin] clear all picks without scoring
  !ufc resetstandings       [admin] wipe standings (confirmation required)
"""
import asyncio
import aiohttp
import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red

from .api import (
    get_upcoming_event, get_recent_event,
    get_event_on_date, get_event_by_id, get_fighter,
)
from . import embeds


def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def _merge_deltas(total: dict, part: dict):
    for uid, d in part.items():
        t = total.setdefault(uid, {"correct": 0, "total": 0})
        t["correct"] += d["correct"]
        t["total"]   += d["total"]


def _score_picks(picks_dict: dict, results_event: dict):
    """
    Score {fight_key: {uid: picked_name}} against a results event.
    Matches by FIGHTER NAME (order-independent), robust to card changes.
    Returns (deltas, resolved):
        deltas   = {uid: {"correct": int, "total": int}}
        resolved = [(fight_key, uid), ...]
    """
    result_fights = results_event.get("fights", []) if results_event else []

    def find_result(picked: str):
        pn = _norm(picked)
        for rf in result_fights:
            r, b = _norm(rf["red"]), _norm(rf["blue"])
            if pn == r or pn == b or pn in r or pn in b:
                return rf
        return None

    deltas, resolved = {}, []
    for fight_key, fp in picks_dict.items():
        for uid, picked in fp.items():
            rf = find_result(picked)
            if not rf or not rf.get("winner"):
                continue
            resolved.append((fight_key, uid))
            d = deltas.setdefault(uid, {"correct": 0, "total": 0})
            d["total"] += 1
            if _norm(rf["winner"]) == _norm(picked):
                d["correct"] += 1
    return deltas, resolved


class UFC(commands.Cog):
    """UFC fight cards, results, fighter stats, and a server pick-em game."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=7833209, force_registration=True)
        self.config.register_guild(
            events={},      # { eid: {"meta": {...}, "picks": {"Red|Blue": {uid: name}}} }
            picks={},       # legacy flat picks (pre-event-scoping) — still settle-able
            standings={},   # { uid: {"correct": int, "total": int} }
        )
        self._session: aiohttp.ClientSession = None

    async def cog_load(self):
        self._session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ── group ──────────────────────────────────────────────────────────────-

    @commands.group(name="ufc", invoke_without_command=True)
    async def ufc(self, ctx):
        p = ctx.clean_prefix
        e = discord.Embed(title="🥊  UFC Bot", color=0xD20A0A, description=(
            f"`{p}ufc card` — upcoming fight card\n"
            f"`{p}ufc results` — recent event results\n"
            f"`{p}ufc fighter <name>` — fighter stats\n"
            f"`{p}ufc pick <name>` — lock in your pick\n"
            f"`{p}ufc picks` — server picks\n"
            f"`{p}ufc standings` — pick-em leaderboard\n"
        ))
        await ctx.send(embed=e)

    # ── card / results ───────────────────────────────────────────────────────

    @ufc.command(name="card")
    async def ufc_card(self, ctx):
        async with ctx.typing():
            event = await get_upcoming_event(self.session)
        if not event:
            return await ctx.send(embed=embeds.error_embed(
                "Couldn't fetch the upcoming card right now. Try again shortly."))
        await ctx.send(embed=embeds.card_embed(event))

    @ufc.command(name="results")
    async def ufc_results(self, ctx):
        async with ctx.typing():
            event = await get_recent_event(self.session)
        if not event:
            return await ctx.send(embed=embeds.error_embed(
                "Couldn't fetch recent results right now. Try again shortly."))
        await ctx.send(embed=embeds.results_embed(event))

    # ── fighter ────────────────────────────────────────────────────────────-

    @ufc.command(name="fighter")
    async def ufc_fighter(self, ctx, *, name: str):
        """Look up a fighter's stats and recent fights. Example: !ufc fighter Jon Jones"""
        async with ctx.typing():
            f = await get_fighter(self.session, name)
        if not f:
            return await ctx.send(embed=embeds.error_embed(
                f"Couldn't find **{name}**.\n\n"
                "• Use their full name (`Jon Jones`, not `Jones`)\n"
                "• Check the spelling / use their official fight name"))
        await ctx.send(embed=embeds.fighter_embed(f))

    # ── pick ───────────────────────────────────────────────────────────────-

    @ufc.command(name="pick")
    async def ufc_pick(self, ctx, *, fighter_name: str):
        """Lock in your pick for a fight on the upcoming card. Example: !ufc pick Jon Jones"""
        async with ctx.typing():
            event = await get_upcoming_event(self.session)
        if not event:
            return await ctx.send(embed=embeds.error_embed(
                "Couldn't fetch the upcoming card to match your pick."))

        q = _norm(fighter_name)
        matched = picked = opponent = None
        for fight in event.get("fights", []):
            if q in _norm(fight["red"]):
                matched, picked, opponent = fight, fight["red"], fight["blue"]; break
            if q in _norm(fight["blue"]):
                matched, picked, opponent = fight, fight["blue"], fight["red"]; break

        if not matched:
            return await ctx.send(embed=embeds.error_embed(
                f"**{fighter_name}** isn't on the upcoming card.\n"
                f"Use `{ctx.clean_prefix}ufc card` to see current matchups."))

        if matched.get("completed") or matched.get("winner"):
            return await ctx.send(embed=embeds.error_embed(
                f"That fight (**{matched['red']}** vs **{matched['blue']}**) has "
                "already happened — picks for it are locked."))

        fight_key = f"{matched['red']}|{matched['blue']}"
        eid = event["id"]
        uid = str(ctx.author.id)

        async with self.config.guild(ctx.guild).events() as evs:
            bucket = evs.setdefault(eid, {"meta": {}, "picks": {}})
            bucket["meta"] = {
                "id": eid,
                "name": event["name"],
                "shortname": event["shortname"],
                "date": event.get("date", ""),
                "date_compact": event.get("date_compact", ""),
            }
            bucket["picks"].setdefault(fight_key, {})
            old = bucket["picks"][fight_key].get(uid)
            bucket["picks"][fight_key][uid] = picked

        if old and old != picked:
            await ctx.send(f"🔄 {ctx.author.mention} changed pick: **{old}** → **{picked}**")
        else:
            await ctx.send(embed=embeds.pick_confirm_embed(
                ctx.author, picked, opponent, event["shortname"]))

    # ── picks ──────────────────────────────────────────────────────────────-

    @ufc.command(name="picks")
    async def ufc_picks(self, ctx):
        """Show everyone's picks for the upcoming event."""
        async with ctx.typing():
            event = await get_upcoming_event(self.session)
            evs = await self.config.guild(ctx.guild).events()
            legacy = await self.config.guild(ctx.guild).picks()

        # If ESPN is unreachable, fall back to the most recent stored event
        # bucket so picks still display from saved data.
        if not event:
            if evs:
                eid = max(evs, key=lambda k: evs[k].get("meta", {}).get("date_compact", ""))
                meta = evs[eid].get("meta", {})
                event = {"shortname": meta.get("shortname", "Event"),
                         "name": meta.get("name", "Event"),
                         "date": meta.get("date", "TBD"), "fights": [], "id": eid}
            else:
                event = {"shortname": "Upcoming Event", "name": "Upcoming Event",
                         "date": "TBD", "fights": [], "id": ""}

        bucket = evs.get(event.get("id", ""), {})
        picks = dict(bucket.get("picks", {}))
        for k, v in (legacy or {}).items():
            picks.setdefault(k, {}).update(v)

        await ctx.send(embed=embeds.picks_embed(event, picks, ctx.guild))

    # ── standings ────────────────────────────────────────────────────────────

    @ufc.command(name="standings")
    async def ufc_standings(self, ctx):
        standings = await self.config.guild(ctx.guild).standings()
        evs = await self.config.guild(ctx.guild).events()
        legacy = await self.config.guild(ctx.guild).picks()
        pending = sum(len(fp) for b in evs.values() for fp in b.get("picks", {}).values())
        pending += sum(len(v) for v in (legacy or {}).values())
        await ctx.send(embed=embeds.standings_embed(standings, ctx.guild, pending))

    # ── settle ─────────────────────────────────────────────────────────────-

    @ufc.command(name="settle")
    @checks.admin_or_permissions(manage_guild=True)
    async def ufc_settle(self, ctx, date: str = None):
        """[Admin] Score picks against results and update standings.

        Normally just run `!ufc settle` after an event — it settles each event
        you have picks for, fetching that exact card's results by date.

        To force-settle everything against a specific past card, pass its date:
            !ufc settle 2024-04-13
        """
        guild = ctx.guild

        async with ctx.typing():
            evs = await self.config.guild(guild).events()
            legacy = await self.config.guild(guild).picks()

            if not evs and not legacy:
                return await ctx.send("No picks to settle.")

            total_deltas = {}
            settled_names = []
            resolved_by_event = {}
            resolved_legacy = []

            if date:
                ymd = date.replace("-", "").replace("/", "")
                results = await get_event_on_date(self.session, ymd)
                if not results:
                    return await ctx.send(embed=embeds.error_embed(
                        f"No UFC event found on **{date}**. "
                        "Use the event date as `YYYY-MM-DD`."))
                settled_names.append(results["shortname"])
                for eid, bucket in evs.items():
                    d, r = _score_picks(bucket.get("picks", {}), results)
                    _merge_deltas(total_deltas, d)
                    resolved_by_event.setdefault(eid, []).extend(r)
                if legacy:
                    d, r = _score_picks(legacy, results)
                    _merge_deltas(total_deltas, d)
                    resolved_legacy.extend(r)
            else:
                for eid, bucket in evs.items():
                    meta = bucket.get("meta", {})
                    ymd = meta.get("date_compact", "")
                    results = (await get_event_by_id(self.session, eid, ymd)
                               or await get_event_on_date(self.session, ymd))
                    if not results:
                        continue
                    d, r = _score_picks(bucket.get("picks", {}), results)
                    if r:
                        settled_names.append(meta.get("shortname", results["shortname"]))
                    _merge_deltas(total_deltas, d)
                    resolved_by_event.setdefault(eid, []).extend(r)
                if legacy:
                    results = await get_recent_event(self.session)
                    if results:
                        d, r = _score_picks(legacy, results)
                        if r:
                            settled_names.append(results["shortname"])
                        _merge_deltas(total_deltas, d)
                        resolved_legacy.extend(r)

        if not total_deltas:
            return await ctx.send(embed=embeds.error_embed(
                "No finished fights matched the locked-in picks yet.\n"
                "Run `!ufc settle` once results are posted, or settle a specific "
                "card with `!ufc settle YYYY-MM-DD`."))

        async with self.config.guild(guild).standings() as standings:
            for uid, d in total_deltas.items():
                s = standings.setdefault(uid, {"correct": 0, "total": 0})
                s["correct"] += d["correct"]
                s["total"]   += d["total"]

        async with self.config.guild(guild).events() as evs_w:
            for eid, pairs in resolved_by_event.items():
                if eid not in evs_w:
                    continue
                pmap = evs_w[eid].get("picks", {})
                for fight_key, uid in pairs:
                    if fight_key in pmap and uid in pmap[fight_key]:
                        del pmap[fight_key][uid]
                    if fight_key in pmap and not pmap[fight_key]:
                        del pmap[fight_key]
                if not pmap:
                    del evs_w[eid]
        if resolved_legacy:
            async with self.config.guild(guild).picks() as legacy_w:
                for fight_key, uid in resolved_legacy:
                    if fight_key in legacy_w and uid in legacy_w[fight_key]:
                        del legacy_w[fight_key][uid]
                    if fight_key in legacy_w and not legacy_w[fight_key]:
                        del legacy_w[fight_key]

        scored = sum(d["total"] for d in total_deltas.values())
        lines = [f"Scored **{scored}** pick(s):\n"]
        for uid, d in sorted(total_deltas.items(), key=lambda x: -x[1]["correct"]):
            m = guild.get_member(int(uid))
            disp = m.display_name if m else f"<@{uid}>"
            c, t = d["correct"], d["total"]
            pct = round(c / t * 100) if t else 0
            icon = "🔥" if c == t else ("✅" if c else "❌")
            lines.append(f"{icon} **{disp}**: {c}/{t} ({pct}%)")

        leftover = sum(len(fp) for b in (await self.config.guild(guild).events()).values()
                       for fp in b.get("picks", {}).values())
        leftover += sum(len(v) for v in (await self.config.guild(guild).picks()).values())
        if leftover:
            lines.append(f"\n*{leftover} pick(s) not yet scored (fights pending).*")
        lines.append(f"\nUse `{ctx.clean_prefix}ufc standings` to see the leaderboard.")

        label = ", ".join(dict.fromkeys(settled_names)) or "UFC"
        await ctx.send(embed=embeds.settle_embed(label, lines))

    # ── clearpicks / resetstandings ───────────────────────────────────────────

    @ufc.command(name="clearpicks")
    @checks.admin_or_permissions(manage_guild=True)
    async def ufc_clearpicks(self, ctx):
        """[Admin] Clear all picks (every event) without scoring."""
        await self.config.guild(ctx.guild).events.set({})
        await self.config.guild(ctx.guild).picks.set({})
        await ctx.send("✅ All picks cleared.")

    @ufc.command(name="resetstandings")
    @checks.admin_or_permissions(administrator=True)
    async def ufc_resetstandings(self, ctx):
        """[Admin] Permanently wipe standings (asks for confirmation)."""
        await ctx.send("⚠️ This permanently deletes all standings. "
                       "Type `confirm` to proceed, or anything else to cancel.")

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=30)
        except asyncio.TimeoutError:
            return await ctx.send("Timed out — standings unchanged.")
        if _norm(msg.content) == "confirm":
            await self.config.guild(ctx.guild).standings.set({})
            await ctx.send("✅ Standings reset.")
        else:
            await ctx.send("Cancelled — standings unchanged.")


async def setup(bot: Red):
    await bot.add_cog(UFC(bot))
