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
from redbot.core import commands, Config, checks, bank
from redbot.core.bot import Red

from .api import (
    get_upcoming_event, get_recent_event,
    get_event_on_date, get_event_by_id, get_fighter,
)
from . import embeds

MAX_BETS = 3   # max active bets per user per card


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


def _score_bets(bets_dict: dict, results_event: dict):
    """
    Score {fight_key: {uid: {"pick": name, "amount": int}}} against results.
    Returns (payouts, outcomes, resolved):
        payouts  = {uid: credits_to_deposit}            # stake*2 for a win
        outcomes = {uid: {"net": int, "won": int, "lost": int}}
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

    payouts, outcomes, resolved = {}, {}, []
    for fight_key, ub in bets_dict.items():
        for uid, bet in ub.items():
            picked, amount = bet.get("pick", ""), bet.get("amount", 0)
            rf = find_result(picked)
            if not rf or not rf.get("winner"):
                continue
            resolved.append((fight_key, uid))
            o = outcomes.setdefault(uid, {"net": 0, "won": 0, "lost": 0})
            if _norm(rf["winner"]) == _norm(picked):
                payouts[uid] = payouts.get(uid, 0) + amount * 2
                o["net"] += amount
                o["won"] += 1
            else:
                o["net"] -= amount
                o["lost"] += 1
    return payouts, outcomes, resolved


def _merge_payouts(total: dict, part: dict):
    for uid, amt in part.items():
        total[uid] = total.get(uid, 0) + amt


def _merge_outcomes(total: dict, part: dict):
    for uid, o in part.items():
        s = total.setdefault(uid, {"net": 0, "won": 0, "lost": 0})
        s["net"]  += o["net"]
        s["won"]  += o["won"]
        s["lost"] += o["lost"]


class UFC(commands.Cog):
    """UFC fight cards, results, fighter stats, and a server pick-em game."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=7833209, force_registration=True)
        self.config.register_guild(
            events={},      # { eid: {"meta": {...},
                            #         "picks": {"Red|Blue": {uid: name}},
                            #         "bets":  {"Red|Blue": {uid: {"pick": name, "amount": int}}} } }
            picks={},       # legacy flat picks (pre-event-scoping) — still settle-able
            standings={},   # { uid: {"correct": int, "total": int} }
            betting={},     # { uid: {"net": int, "won": int, "lost": int} }
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

    @staticmethod
    def _match_fighter(event: dict, fighter_name: str):
        """Find (fight, picked, opponent) for a fighter name on the card, or (None, None, None)."""
        q = _norm(fighter_name)
        for fight in event.get("fights", []):
            if q in _norm(fight["red"]):
                return fight, fight["red"], fight["blue"]
            if q in _norm(fight["blue"]):
                return fight, fight["blue"], fight["red"]
        return None, None, None

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
            f"`{p}ufc bet <amount> <name>` — bet credits (win pays 2×)\n"
            f"`{p}ufc unbet <name>` — cancel a bet & refund\n"
            f"`{p}ufc bets` — your active bets\n"
            f"`{p}ufc standings` — leaderboard (picks + money)\n"
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

        matched, picked, opponent = self._match_fighter(event, fighter_name)

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

    # ── bet ────────────────────────────────────────────────────────────────-

    @ufc.command(name="bet")
    async def ufc_bet(self, ctx, amount: int, *, fighter_name: str):
        """Bet credits on a fighter (1:1, win pays double). Also sets your pick.

        Example: !ufc bet 100 Jon Jones
        Max 3 active bets per card. Re-betting the same fight adjusts your stake.
        """
        if amount <= 0:
            return await ctx.send(embed=embeds.error_embed("Bet amount must be positive."))

        async with ctx.typing():
            event = await get_upcoming_event(self.session)
        if not event:
            return await ctx.send(embed=embeds.error_embed(
                "Couldn't fetch the upcoming card to match your bet."))

        matched, picked, opponent = self._match_fighter(event, fighter_name)
        if not matched:
            return await ctx.send(embed=embeds.error_embed(
                f"**{fighter_name}** isn't on the upcoming card.\n"
                f"Use `{ctx.clean_prefix}ufc card` to see current matchups."))
        if matched.get("completed") or matched.get("winner"):
            return await ctx.send(embed=embeds.error_embed(
                f"That fight (**{matched['red']}** vs **{matched['blue']}**) has "
                "already happened — betting is locked."))

        fight_key = f"{matched['red']}|{matched['blue']}"
        eid = event["id"]
        uid = str(ctx.author.id)
        currency = await bank.get_currency_name(ctx.guild)

        evs = await self.config.guild(ctx.guild).events()
        bucket = evs.get(eid, {})
        bets = bucket.get("bets", {})

        existing = bets.get(fight_key, {}).get(uid)
        prev_amount = existing["amount"] if existing else 0

        distinct_fights = {fk for fk, ub in bets.items() if uid in ub}
        if fight_key not in distinct_fights and len(distinct_fights) >= MAX_BETS:
            return await ctx.send(embed=embeds.error_embed(
                f"You're at the **{MAX_BETS}-bet max** for this card.\n"
                f"Use `{ctx.clean_prefix}ufc unbet <fighter>` to free a slot first."))

        delta = amount - prev_amount
        if delta > 0:
            if not await bank.can_spend(ctx.author, delta):
                bal = await bank.get_balance(ctx.author)
                return await ctx.send(embed=embeds.error_embed(
                    f"Not enough {currency}. You have **{bal:,}**, "
                    f"need **{delta:,}** more for this bet."))
            await bank.withdraw_credits(ctx.author, delta)
        elif delta < 0:
            await bank.deposit_credits(ctx.author, -delta)

        async with self.config.guild(ctx.guild).events() as evs_w:
            b = evs_w.setdefault(eid, {"meta": {}, "picks": {}, "bets": {}})
            b["meta"] = {
                "id": eid, "name": event["name"], "shortname": event["shortname"],
                "date": event.get("date", ""), "date_compact": event.get("date_compact", ""),
            }
            b.setdefault("bets", {}).setdefault(fight_key, {})[uid] = {
                "pick": picked, "amount": amount,
            }
            b.setdefault("picks", {}).setdefault(fight_key, {})[uid] = picked

        cur_bets = (await self.config.guild(ctx.guild).events())[eid]["bets"]
        slots_left = MAX_BETS - len({fk for fk, ub in cur_bets.items() if uid in ub})

        await ctx.send(embed=embeds.bet_confirm_embed(
            ctx.author, picked, opponent, amount, event["shortname"],
            currency=currency, slots_left=slots_left,
            changed_from=prev_amount if existing else None))

    # ── unbet ──────────────────────────────────────────────────────────────-

    @ufc.command(name="unbet")
    async def ufc_unbet(self, ctx, *, fighter_name: str):
        """Cancel a bet, get refunded, and free the slot. Example: !ufc unbet Jon Jones"""
        async with ctx.typing():
            event = await get_upcoming_event(self.session)
        if not event:
            return await ctx.send(embed=embeds.error_embed(
                "Couldn't fetch the upcoming card."))

        matched, picked, _ = self._match_fighter(event, fighter_name)
        if not matched:
            return await ctx.send(embed=embeds.error_embed(
                f"**{fighter_name}** isn't on the upcoming card."))
        if matched.get("completed") or matched.get("winner"):
            return await ctx.send(embed=embeds.error_embed(
                "That fight already happened — the bet can't be cancelled."))

        fight_key = f"{matched['red']}|{matched['blue']}"
        eid = event["id"]
        uid = str(ctx.author.id)
        currency = await bank.get_currency_name(ctx.guild)

        evs = await self.config.guild(ctx.guild).events()
        bet = evs.get(eid, {}).get("bets", {}).get(fight_key, {}).get(uid)
        if not bet:
            return await ctx.send(embed=embeds.error_embed(
                "You don't have a bet on that fight."))

        refund = bet["amount"]
        await bank.deposit_credits(ctx.author, refund)

        async with self.config.guild(ctx.guild).events() as evs_w:
            b = evs_w.get(eid, {})
            if fight_key in b.get("bets", {}) and uid in b["bets"][fight_key]:
                del b["bets"][fight_key][uid]
                if not b["bets"][fight_key]:
                    del b["bets"][fight_key]
            if fight_key in b.get("picks", {}) and uid in b["picks"][fight_key]:
                del b["picks"][fight_key][uid]
                if not b["picks"][fight_key]:
                    del b["picks"][fight_key]

        used = len({fk for fk, ub in
               (await self.config.guild(ctx.guild).events()).get(eid, {}).get("bets", {}).items()
               if uid in ub})
        await ctx.send(embed=embeds.unbet_embed(
            ctx.author, picked, refund, currency=currency, slots_left=MAX_BETS - used))

    # ── bets ───────────────────────────────────────────────────────────────-

    @ufc.command(name="bets")
    async def ufc_bets(self, ctx):
        """Show your active bets for the upcoming card."""
        async with ctx.typing():
            event = await get_upcoming_event(self.session)
            evs = await self.config.guild(ctx.guild).events()
        currency = await bank.get_currency_name(ctx.guild)
        uid = str(ctx.author.id)

        my_bets = []
        if event:
            bets = evs.get(event["id"], {}).get("bets", {})
            for fight_key, ub in bets.items():
                if uid in ub:
                    red, blue = fight_key.split("|", 1)
                    pick = ub[uid]["pick"]
                    opp = blue if pick == red else red
                    my_bets.append({"fighter": pick, "opponent": opp,
                                    "amount": ub[uid]["amount"]})
        await ctx.send(embed=embeds.bets_embed(
            ctx.author, my_bets, currency=currency, max_bets=MAX_BETS))

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
        betting = await self.config.guild(ctx.guild).betting()
        pending = sum(len(fp) for b in evs.values() for fp in b.get("picks", {}).values())
        pending += sum(len(v) for v in (legacy or {}).values())
        await ctx.send(embed=embeds.standings_embed(
            standings, ctx.guild, pending, betting=betting))

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
            total_payouts = {}        # uid -> credits to deposit
            bet_outcomes = {}         # uid -> {net, won, lost}
            resolved_bets_by_event = {}

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
                    p, o, br = _score_bets(bucket.get("bets", {}), results)
                    _merge_payouts(total_payouts, p)
                    _merge_outcomes(bet_outcomes, o)
                    resolved_bets_by_event.setdefault(eid, []).extend(br)
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
                    p, o, br = _score_bets(bucket.get("bets", {}), results)
                    _merge_payouts(total_payouts, p)
                    _merge_outcomes(bet_outcomes, o)
                    resolved_bets_by_event.setdefault(eid, []).extend(br)
                if legacy:
                    results = await get_recent_event(self.session)
                    if results:
                        d, r = _score_picks(legacy, results)
                        if r:
                            settled_names.append(results["shortname"])
                        _merge_deltas(total_deltas, d)
                        resolved_legacy.extend(r)

        if not total_deltas and not bet_outcomes:
            return await ctx.send(embed=embeds.error_embed(
                "No finished fights matched the locked-in picks yet.\n"
                "Run `!ufc settle` once results are posted, or settle a specific "
                "card with `!ufc settle YYYY-MM-DD`."))

        # pick-em standings
        if total_deltas:
            async with self.config.guild(guild).standings() as standings:
                for uid, d in total_deltas.items():
                    s = standings.setdefault(uid, {"correct": 0, "total": 0})
                    s["correct"] += d["correct"]
                    s["total"]   += d["total"]

        # pay out winning bets via Red bank
        for uid, amt in total_payouts.items():
            if amt <= 0:
                continue
            member = guild.get_member(int(uid))
            if member:
                try:
                    await bank.deposit_credits(member, amt)
                except Exception:
                    pass  # member may be unreachable; stats still recorded below

        # lifetime betting stats (net P/L and W-L)
        if bet_outcomes:
            async with self.config.guild(guild).betting() as betting:
                for uid, o in bet_outcomes.items():
                    s = betting.setdefault(uid, {"net": 0, "won": 0, "lost": 0})
                    s["net"]  += o["net"]
                    s["won"]  += o["won"]
                    s["lost"] += o["lost"]

        # remove resolved picks AND bets; drop an event only when both are empty
        async with self.config.guild(guild).events() as evs_w:
            touched = set(resolved_by_event) | set(resolved_bets_by_event)
            for eid in touched:
                if eid not in evs_w:
                    continue
                pmap = evs_w[eid].get("picks", {})
                for fight_key, uid in resolved_by_event.get(eid, []):
                    if fight_key in pmap and uid in pmap[fight_key]:
                        del pmap[fight_key][uid]
                    if fight_key in pmap and not pmap[fight_key]:
                        del pmap[fight_key]
                bmap = evs_w[eid].get("bets", {})
                for fight_key, uid in resolved_bets_by_event.get(eid, []):
                    if fight_key in bmap and uid in bmap[fight_key]:
                        del bmap[fight_key][uid]
                    if fight_key in bmap and not bmap[fight_key]:
                        del bmap[fight_key]
                if not pmap and not bmap:
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

        # money summary for users who had bets resolve
        if bet_outcomes:
            currency = await bank.get_currency_name(guild)
            lines.append("\n**💰 Betting:**")
            for uid, o in sorted(bet_outcomes.items(), key=lambda x: -x[1]["net"]):
                m = guild.get_member(int(uid))
                disp = m.display_name if m else f"<@{uid}>"
                net = o["net"]
                sign = "+" if net >= 0 else "−"
                emoji = "💸" if net >= 0 else "📉"
                lines.append(f"{emoji} **{disp}**: {sign}{abs(net):,} {currency} "
                             f"({o['won']}W-{o['lost']}L)")

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
        """[Admin] Clear all picks and bets (every event), refunding active bets."""
        evs = await self.config.guild(ctx.guild).events()
        refunded_total = refunded_users = 0
        for bucket in evs.values():
            for ub in bucket.get("bets", {}).values():
                for uid, bet in ub.items():
                    member = ctx.guild.get_member(int(uid))
                    amt = bet.get("amount", 0)
                    if member and amt > 0:
                        try:
                            await bank.deposit_credits(member, amt)
                            refunded_total += amt
                            refunded_users += 1
                        except Exception:
                            pass
        await self.config.guild(ctx.guild).events.set({})
        await self.config.guild(ctx.guild).picks.set({})
        msg = "✅ All picks and bets cleared."
        if refunded_total:
            currency = await bank.get_currency_name(ctx.guild)
            msg += f" Refunded **{refunded_total:,} {currency}** across {refunded_users} bet(s)."
        await ctx.send(msg)

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
