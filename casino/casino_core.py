from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import discord
from redbot.core import Config, bank
from redbot.core.errors import BalanceTooHigh

CASINO_CONFIG_ID = 2468135790
ACTIVE_GAMES = ("blackjack", "coinflip", "slots", "roulette")
DEFAULT_GAME_SETTINGS = {
    "blackjack": {"enabled": True, "min_bet": 1, "max_bet": 0, "cooldown": 3},
    "coinflip": {"enabled": True, "min_bet": 1, "max_bet": 0, "cooldown": 3},
    "slots": {"enabled": True, "min_bet": 1, "max_bet": 0, "cooldown": 3},
    "roulette": {"enabled": True, "min_bet": 1, "max_bet": 0, "cooldown": 5},
}

CONFIG = Config.get_conf(None, identifier=CASINO_CONFIG_ID, force_registration=True)
CONFIG.register_guild(
    games=DEFAULT_GAME_SETTINGS,
    total_wagered=0,
    total_paid=0,
    total_games=0,
    total_wins=0,
    total_losses=0,
    total_pushes=0,
    biggest_payout=0,
    biggest_payout_user=0,
    biggest_payout_game="",
)
CONFIG.register_member(
    total_wagered=0,
    total_paid=0,
    total_games=0,
    wins=0,
    losses=0,
    pushes=0,
    biggest_bet=0,
    biggest_payout=0,
    games={},
)

LOG = logging.getLogger("red.crewcogs.casino")
_LAST_PLAY: Dict[Tuple[int, int, str], float] = {}


@dataclass(frozen=True)
class SettlementResult:
    requested_payout: int
    deposited: int
    outcome: str
    stats_recorded: bool = True

    @property
    def capped(self) -> bool:
        return self.deposited < self.requested_payout


def normalize_game(game: str) -> str:
    return game.lower().strip()


async def get_game_settings(guild: discord.Guild, game: str) -> dict:
    game = normalize_game(game)
    games = await CONFIG.guild(guild).games()
    defaults = DEFAULT_GAME_SETTINGS.get(
        game, {"enabled": True, "min_bet": 1, "max_bet": 0, "cooldown": 0}
    )
    merged = dict(defaults)
    merged.update(games.get(game, {}))
    return merged


async def validate_bet(ctx, game: str, bet: int) -> Optional[str]:
    if ctx.guild is None:
        return "Casino games can only be played in a server."
    if bet <= 0:
        return "Your wager must be greater than zero."

    settings = await get_game_settings(ctx.guild, game)
    if not settings["enabled"]:
        return f"{game.title()} is currently disabled."
    if bet < settings["min_bet"]:
        return f"The minimum {game} bet is **{settings['min_bet']:,}**."
    if settings["max_bet"] > 0 and bet > settings["max_bet"]:
        return f"The maximum {game} bet is **{settings['max_bet']:,}**."

    balance = await bank.get_balance(ctx.author)
    if bet > balance:
        currency = await bank.get_currency_name(ctx.guild)
        return f"You do not have enough {currency}. Your balance is **{balance:,}**."

    cooldown = max(0, int(settings["cooldown"]))
    key = (ctx.guild.id, ctx.author.id, normalize_game(game))
    retry = cooldown - (time.monotonic() - _LAST_PLAY.get(key, 0.0))
    if retry > 0:
        return f"Please wait **{retry:.1f}s** before playing {game} again."
    return None


def mark_played(guild_id: int, user_id: int, game: str) -> None:
    _LAST_PLAY[(guild_id, user_id, normalize_game(game))] = time.monotonic()


async def safe_deposit(member: discord.Member, amount: int) -> int:
    """Deposit as much as the bank cap allows and return the actual amount deposited."""
    if amount <= 0:
        return 0
    try:
        await bank.deposit_credits(member, amount)
        return amount
    except BalanceTooHigh as exc:
        current = await bank.get_balance(member)
        room = max(0, exc.max_balance - current)
        if room:
            await bank.deposit_credits(member, room)
        return room


async def refund_wager(member: discord.Member, game: str, wager: int, *, reason: str) -> int:
    """Refund an unsettled wager and log any bank-cap shortfall."""
    deposited = await safe_deposit(member, wager)
    log = LOG.warning if deposited < wager else LOG.info
    log(
        "Casino wager refund: user=%s guild=%s game=%s wager=%s refunded=%s reason=%s",
        member.id,
        member.guild.id if member.guild else None,
        normalize_game(game),
        wager,
        deposited,
        reason,
    )
    return deposited


async def record_game(
    member: discord.Member,
    game: str,
    wager: int,
    payout: int,
    outcome: str,
    *,
    include_economy: bool = True,
) -> None:
    """Record one completed result in member, per-game, and guild totals."""
    if member.guild is None:
        return

    game = normalize_game(game)
    outcome = outcome.lower().strip()
    outcome_key = {"win": "wins", "loss": "losses", "push": "pushes"}.get(outcome)
    if outcome_key is None:
        raise ValueError("outcome must be win, loss, or push")
    if wager < 0 or payout < 0:
        raise ValueError("wager and payout cannot be negative")

    member_conf = CONFIG.member(member)
    guild_conf = CONFIG.guild(member.guild)

    async with member_conf.all() as data:
        if include_economy:
            data["total_wagered"] += wager
            data["total_paid"] += payout
            data["biggest_bet"] = max(data["biggest_bet"], wager)
            data["biggest_payout"] = max(data["biggest_payout"], payout)
        data["total_games"] += 1
        data[outcome_key] += 1

        game_data = data["games"].setdefault(
            game,
            {
                "wagered": 0,
                "paid": 0,
                "games": 0,
                "wins": 0,
                "losses": 0,
                "pushes": 0,
                "biggest_bet": 0,
                "biggest_payout": 0,
            },
        )
        # Per-game fields remain useful for Daily Spin boards even when its
        # free reward is excluded from overall casino economics.
        game_data["wagered"] += wager
        game_data["paid"] += payout
        game_data["games"] += 1
        game_data[outcome_key] += 1
        game_data["biggest_bet"] = max(game_data["biggest_bet"], wager)
        game_data["biggest_payout"] = max(game_data["biggest_payout"], payout)

    async with guild_conf.all() as data:
        if include_economy:
            data["total_wagered"] += wager
            data["total_paid"] += payout
        data["total_games"] += 1
        data[f"total_{outcome_key}"] += 1
        if include_economy and payout > data["biggest_payout"]:
            data["biggest_payout"] = payout
            data["biggest_payout_user"] = member.id
            data["biggest_payout_game"] = game


async def settle_game(
    member: discord.Member,
    game: str,
    wager: int,
    payout: int,
    outcome: str,
    *,
    include_economy: bool = True,
) -> SettlementResult:
    """Deposit a result and record it through one shared settlement path.

    Call this exactly once after a wager has been withdrawn and the outcome is final.
    A payout may be reduced by Red's maximum-balance cap; stats record the amount
    actually deposited so analytics match real balance movement.
    """
    game = normalize_game(game)
    outcome = outcome.lower().strip()
    if outcome not in {"win", "loss", "push"}:
        raise ValueError("outcome must be win, loss, or push")
    if wager < 0 or payout < 0:
        raise ValueError("wager and payout cannot be negative")

    deposited = await safe_deposit(member, payout)
    stats_recorded = True
    try:
        await record_game(
            member,
            game,
            wager,
            deposited,
            outcome,
            include_economy=include_economy,
        )
    except Exception:
        stats_recorded = False
        LOG.exception(
            "Casino stats failed after payout: user=%s guild=%s game=%s wager=%s "
            "requested_payout=%s deposited=%s outcome=%s",
            member.id,
            member.guild.id if member.guild else None,
            game,
            wager,
            payout,
            deposited,
            outcome,
        )

    LOG.info(
        "Casino settled: user=%s guild=%s game=%s wager=%s requested_payout=%s "
        "deposited=%s outcome=%s",
        member.id,
        member.guild.id if member.guild else None,
        game,
        wager,
        payout,
        deposited,
        outcome,
    )
    return SettlementResult(payout, deposited, outcome, stats_recorded)
