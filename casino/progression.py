from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import discord
from redbot.core import bank

from .casino_core import CONFIG, safe_deposit

# Five early/mid-game title achievements award credits. Five prestige titles are
# cosmetic bragging rights. The other ten achievements award permanent badges.
ACHIEVEMENTS: Tuple[dict, ...] = (
    {"id":"first_steps","emoji":"🎲","name":"First Steps","description":"Play 50 casino games.","stat":"total_games","goal":50,"reward":5000,"title":"🎲 Regular"},
    {"id":"high_roller","emoji":"💵","name":"High Roller","description":"Wager 100,000 total.","stat":"total_wagered","goal":100000,"reward":10000,"title":"💵 High Roller"},
    {"id":"jackpot_hunter","emoji":"🎰","name":"Jackpot Hunter","description":"Record a single payout of 25,000 or more.","stat":"biggest_payout","goal":25000,"reward":15000,"title":"🎰 Jackpot Hunter"},
    {"id":"card_shark","emoji":"🃏","name":"Card Shark","description":"Reach seven correct guesses in High/Low.","stat":"best_highlow_streak","goal":7,"reward":20000,"title":"🃏 Card Shark"},
    {"id":"all_around","emoji":"🌟","name":"All-Around Gambler","description":"Win 25 games in Blackjack, Coin Flip, Slots, Roulette, and High/Low.","stat":"all_game_wins","goal":25,"reward":25000,"title":"🌟 All-Around"},
    {"id":"on_fire","emoji":"🔥","name":"On Fire","description":"Win 10 settled games in a row.","stat":"longest_win_streak","goal":10,"reward":0,"title":"🔥 On Fire"},
    {"id":"whale","emoji":"🐋","name":"Whale","description":"Wager 1,000,000 total.","stat":"total_wagered","goal":1000000,"reward":0,"title":"🐋 Whale"},
    {"id":"blackjack_pro","emoji":"♠️","name":"Blackjack Pro","description":"Win 100 Blackjack hands.","stat":"games.blackjack.wins","goal":100,"reward":0,"title":"♠️ Blackjack Pro"},
    {"id":"coin_king","emoji":"🪙","name":"Coin King","description":"Win 100 Coin Flip games.","stat":"games.coinflip.wins","goal":100,"reward":0,"title":"🪙 Coin King"},
    {"id":"casino_legend","emoji":"👑","name":"Casino Legend","description":"Unlock the other 19 achievements.","stat":"achievement_count","goal":19,"reward":0,"title":"👑 Casino Legend"},
    {"id":"seasoned_player","emoji":"🏦","name":"Seasoned Player","description":"Play 500 casino games.","stat":"total_games","goal":500,"reward":0},
    {"id":"lucky_devil","emoji":"🍀","name":"Lucky Devil","description":"Win 250 casino games.","stat":"wins","goal":250,"reward":0},
    {"id":"wheel_regular","emoji":"🎡","name":"Wheel Regular","description":"Win 75 Roulette games.","stat":"games.roulette.wins","goal":75,"reward":0},
    {"id":"slot_regular","emoji":"💎","name":"Reel Regular","description":"Win 75 Slots games.","stat":"games.slots.wins","goal":75,"reward":0},
    {"id":"highlow_regular","emoji":"🂡","name":"Higher or Lower","description":"Win 50 High/Low games.","stat":"games.highlow.wins","goal":50,"reward":0},
    {"id":"daily_regular","emoji":"☀️","name":"Daily Regular","description":"Complete Daily Spin 30 times.","stat":"games.dailyspin.games","goal":30,"reward":0},
    {"id":"daily_dedication","emoji":"📅","name":"Daily Dedication","description":"Complete 30 daily challenges.","stat":"daily_completed","goal":30,"reward":0},
    {"id":"weekly_warrior","emoji":"🗓️","name":"Weekly Warrior","description":"Complete 12 weekly challenges.","stat":"weekly_completed","goal":12,"reward":0},
    {"id":"big_bankroll","emoji":"💰","name":"Big Bankroll","description":"Receive 1,000,000 total in casino returns.","stat":"total_paid","goal":1000000,"reward":0},
    {"id":"iron_will","emoji":"🛡️","name":"Iron Will","description":"Play 100 games after losing streaks without quitting.","stat":"losses","goal":100,"reward":0},
)
ACHIEVEMENT_MAP = {a["id"]: a for a in ACHIEVEMENTS}

DAILY_POOL: Tuple[dict, ...] = (
    {"id":"d_play_12","category":"play","name":"Table Hopper","description":"Play 12 casino games.","metric":"games","goal":12,"reward":2500},
    {"id":"d_blackjack_5","category":"play","name":"Blackjack Session","description":"Play 5 Blackjack hands.","metric":"game.blackjack.games","goal":5,"reward":2500},
    {"id":"d_roulette_5","category":"play","name":"Spin the Wheel","description":"Play 5 Roulette games.","metric":"game.roulette.games","goal":5,"reward":2500},
    {"id":"d_win_6","category":"win","name":"Winning Ways","description":"Win 6 casino games.","metric":"wins","goal":6,"reward":3500},
    {"id":"d_coinflip_3","category":"win","name":"Heads or Tails","description":"Win 3 Coin Flip games.","metric":"game.coinflip.wins","goal":3,"reward":3000},
    {"id":"d_slots_3","category":"win","name":"Reel Results","description":"Win 3 Slots games.","metric":"game.slots.wins","goal":3,"reward":3000},
    {"id":"d_wager_15000","category":"economy","name":"Put It on the Table","description":"Wager 15,000 total today.","metric":"wagered","goal":15000,"reward":4000},
    {"id":"d_paid_20000","category":"economy","name":"Cash Flow","description":"Receive 20,000 in returns today.","metric":"paid","goal":20000,"reward":4000},
    {"id":"d_highlow_3","category":"special","name":"Read the Cards","description":"Reach a 3-card High/Low streak.","metric":"highlow_streak","goal":3,"reward":4000},
    {"id":"d_dailyspin","category":"special","name":"Daily Ritual","description":"Complete Daily Spin once.","metric":"game.dailyspin.games","goal":1,"reward":2000},
)

WEEKLY_POOL: Tuple[dict, ...] = (
    {"id":"w_play_100","category":"play","name":"Casino Marathon","description":"Play 100 casino games.","metric":"games","goal":100,"reward":15000},
    {"id":"w_blackjack_30","category":"play","name":"Blackjack Week","description":"Play 30 Blackjack hands.","metric":"game.blackjack.games","goal":30,"reward":12000},
    {"id":"w_all_games","category":"play","name":"Full Circuit","description":"Play each wagered casino game at least 10 times.","metric":"all_games_played","goal":10,"reward":18000},
    {"id":"w_win_40","category":"win","name":"Winning Week","description":"Win 40 casino games.","metric":"wins","goal":40,"reward":18000},
    {"id":"w_coinflip_15","category":"win","name":"Coin Collector","description":"Win 15 Coin Flip games.","metric":"game.coinflip.wins","goal":15,"reward":14000},
    {"id":"w_roulette_12","category":"win","name":"Wheel Work","description":"Win 12 Roulette games.","metric":"game.roulette.wins","goal":12,"reward":14000},
    {"id":"w_wager_200000","category":"economy","name":"High Stakes","description":"Wager 200,000 this week.","metric":"wagered","goal":200000,"reward":22000},
    {"id":"w_paid_250000","category":"economy","name":"Big Returns","description":"Receive 250,000 in returns this week.","metric":"paid","goal":250000,"reward":22000},
    {"id":"w_highlow_5","category":"special","name":"Card Reader","description":"Reach a 5-card High/Low streak three times.","metric":"highlow_5_runs","goal":3,"reward":18000},
    {"id":"w_dailyspin_5","category":"special","name":"Five-Day Habit","description":"Complete Daily Spin five times.","metric":"game.dailyspin.games","goal":5,"reward":12000},
)


def daily_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def weekly_key() -> str:
    now = datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def _choose(pool: Iterable[dict], previous: Iterable[str]) -> List[str]:
    previous = set(previous)
    available = [c for c in pool if c["id"] not in previous]
    categories: Dict[str, List[dict]] = {}
    for item in available:
        categories.setdefault(item["category"], []).append(item)
    chosen: List[dict] = []
    # Ensure variety: one play, one win, one economy/special.
    for category in ("play", "win"):
        if categories.get(category):
            chosen.append(random.choice(categories[category]))
    tail = [x for x in available if x["category"] in {"economy", "special"} and x not in chosen]
    if tail:
        chosen.append(random.choice(tail))
    leftovers = [x for x in available if x not in chosen]
    while len(chosen) < 3 and leftovers:
        pick = random.choice(leftovers); leftovers.remove(pick); chosen.append(pick)
    return [x["id"] for x in chosen]


async def ensure_rotations(guild: discord.Guild) -> Tuple[List[str], List[str]]:
    conf = CONFIG.guild(guild)
    async with conf.progression() as state:
        dk, wk = daily_key(), weekly_key()
        if state.get("daily_key") != dk:
            previous = state.get("daily_ids", [])
            state["previous_daily_ids"] = previous
            state["daily_ids"] = _choose(DAILY_POOL, previous)
            state["daily_key"] = dk
        if state.get("weekly_key") != wk:
            previous = state.get("weekly_ids", [])
            state["previous_weekly_ids"] = previous
            state["weekly_ids"] = _choose(WEEKLY_POOL, previous)
            state["weekly_key"] = wk
        return list(state["daily_ids"]), list(state["weekly_ids"])


def stat_value(data: dict, stat: str) -> int:
    if stat == "achievement_count":
        return len([x for x in data.get("achievements", []) if x != "casino_legend"])
    if stat == "all_game_wins":
        games = data.get("games", {})
        return min((games.get(g, {}).get("wins", 0) for g in ("blackjack","coinflip","slots","roulette","highlow")), default=0)
    cur: Any = data
    for part in stat.split("."):
        if not isinstance(cur, dict): return 0
        cur = cur.get(part, 0)
    return int(cur or 0)


def _metric_increment(metric: str, game: str, wager: int, paid: int, outcome: str, metadata: dict) -> int:
    if metric == "games": return 1
    if metric == "wins": return 1 if outcome == "win" else 0
    if metric == "wagered": return wager
    if metric == "paid": return paid
    if metric == "highlow_streak": return int(metadata.get("highlow_streak", 0))
    if metric == "highlow_5_runs": return 1 if int(metadata.get("highlow_streak", 0)) >= 5 else 0
    if metric.startswith("game."):
        _, wanted, field = metric.split(".")
        if game != wanted: return 0
        if field == "games": return 1
        if field == "wins": return 1 if outcome == "win" else 0
    return 0


async def process_progress(member: discord.Member, game: str, wager: int, paid: int, outcome: str, *, metadata: Optional[dict]=None, channel=None) -> None:
    metadata = metadata or {}
    daily_ids, weekly_ids = await ensure_rotations(member.guild)
    daily_defs = {x["id"]:x for x in DAILY_POOL}; weekly_defs = {x["id"]:x for x in WEEKLY_POOL}
    member_conf = CONFIG.member(member)
    notices: List[str] = []
    reward_total = 0
    async with member_conf.all() as data:
        # Streak and game-specific durable stats.
        if outcome == "win":
            data["current_win_streak"] = data.get("current_win_streak", 0) + 1
            data["longest_win_streak"] = max(data.get("longest_win_streak", 0), data["current_win_streak"])
        else:
            data["current_win_streak"] = 0
        hs = int(metadata.get("highlow_streak", 0))
        data["best_highlow_streak"] = max(data.get("best_highlow_streak", 0), hs)

        periods = (("daily", daily_key(), daily_ids, daily_defs), ("weekly", weekly_key(), weekly_ids, weekly_defs))
        for kind, key, ids, defs in periods:
            state = data.setdefault(f"{kind}_state", {})
            if state.get("key") != key:
                state.clear(); state.update({"key":key,"progress":{},"claimed":[]})
            for cid in ids:
                definition = defs[cid]
                metric = definition["metric"]
                if metric == "all_games_played":
                    counts = state.setdefault("game_counts", {}).setdefault(cid, {})
                    if game in {"blackjack", "coinflip", "slots", "roulette", "highlow"}:
                        counts[game] = int(counts.get(game, 0)) + 1
                    new = min((int(counts.get(g, 0)) for g in ("blackjack", "coinflip", "slots", "roulette", "highlow")), default=0)
                else:
                    old = int(state["progress"].get(cid, 0))
                    inc = _metric_increment(metric, game, wager, paid, outcome, metadata)
                    # streak challenges use max, not sum
                    new = max(old, inc) if metric == "highlow_streak" else old + inc
                state["progress"][cid] = new
                if new >= definition["goal"] and cid not in state["claimed"]:
                    state["claimed"].append(cid)
                    reward_total += definition["reward"]
                    data[f"{kind}_completed"] = data.get(f"{kind}_completed", 0) + 1
                    notices.append(f"✅ **{kind.title()} challenge complete:** {definition['name']} (+{definition['reward']:,})")

        unlocked = set(data.get("achievements", []))
        # Two passes allow Casino Legend to unlock on the same settlement.
        for _ in range(2):
            for achievement in ACHIEVEMENTS:
                aid = achievement["id"]
                if aid in unlocked: continue
                if stat_value(data, achievement["stat"]) >= achievement["goal"]:
                    unlocked.add(aid); data["achievements"] = list(unlocked)
                    reward_total += achievement["reward"]
                    title = f" Title unlocked: **{achievement['title']}**" if achievement.get("title") else ""
                    reward = f" (+{achievement['reward']:,})" if achievement["reward"] else ""
                    notices.append(f"🏆 **Achievement unlocked:** {achievement['emoji']} {achievement['name']}{reward}.{title}")
        data["achievements"] = sorted(unlocked)

    deposited = await safe_deposit(member, reward_total)
    if notices and channel is not None:
        if deposited < reward_total:
            notices.append(f"⚠️ Bank cap allowed only **{deposited:,}** of **{reward_total:,}** reward credits.")
        try:
            await channel.send("\n".join(notices))
        except (discord.Forbidden, discord.HTTPException):
            pass


def challenge_definitions(ids: Iterable[str], weekly: bool=False) -> List[dict]:
    lookup = {x["id"]:x for x in (WEEKLY_POOL if weekly else DAILY_POOL)}
    return [lookup[x] for x in ids if x in lookup]
