"""One Daily Rewards panel: stipend (with streak), daily spin, scratch ticket, bailout."""
from __future__ import annotations

import asyncio
import random
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import discord
from redbot.core import bank

from .casino_core import CONFIG, currency, safe_deposit, settle_game
from .ui import OwnedView

DICE_PATH = Path(__file__).parent / "dice"
SPIN_COOLDOWN = 86400
SCRATCH_PRIZES = [0, 250, 500, 1000, 2500, 5000, 10000]
SCRATCH_WEIGHTS = [3500, 3200, 2000, 900, 300, 90, 10]
SCRATCH_SYMBOLS = {0: "🍋", 250: "🍒", 500: "🔔", 1000: "⭐", 2500: "💎", 5000: "👑", 10000: "7️⃣"}
STREAK_STEP = 0.10      # +10% stipend per consecutive day…
STREAK_MAX = 0.70       # …up to +70% (day 8+)
FREEBIE_DEFAULTS = {
    "daily_enabled": True, "daily_min": 750, "daily_max": 1250, "daily_cooldown": 86400,
    "claim_enabled": True, "claim_threshold": 250, "claim_amount": 500, "claim_cooldown": 43200,
    "scratch_enabled": True, "scratch_cooldown": 86400, "spin_enabled": True,
}
_locks: Dict[Tuple[int, int], asyncio.Lock] = {}


def _lock(member: discord.Member) -> asyncio.Lock:
    return _locks.setdefault((member.guild.id, member.id), asyncio.Lock())


async def freebie_settings(guild: discord.Guild) -> dict:
    merged = dict(FREEBIE_DEFAULTS)
    merged.update(await CONFIG.guild(guild).freebies() or {})
    return merged


def _remaining(last: float, cooldown: int, now: float) -> int:
    return int(cooldown - (now - float(last or 0)))


def streak_bonus(streak: int) -> float:
    return min(STREAK_MAX, STREAK_STEP * max(0, streak - 1))


async def status(member: discord.Member) -> Dict[str, dict]:
    """{reward: {"ready": bool, "enabled": bool, "wait": seconds, "note": str}}"""
    s = await freebie_settings(member.guild)
    data = await CONFIG.member(member).all()
    now = time.time()
    balance = await bank.get_balance(member)
    out = {}
    for key, last_key, cooldown, enabled in (
        ("stipend", "daily_stipend_at", int(s["daily_cooldown"]), bool(s["daily_enabled"])),
        ("spin", "daily_spin_at", SPIN_COOLDOWN, bool(s.get("spin_enabled", True))),
        ("scratch", "scratch_claimed_at", int(s["scratch_cooldown"]), bool(s["scratch_enabled"])),
        ("bailout", "bailout_claimed_at", int(s["claim_cooldown"]), bool(s["claim_enabled"])),
    ):
        wait = _remaining(data.get(last_key, 0), cooldown, now)
        out[key] = {"enabled": enabled, "wait": max(0, wait), "ready": enabled and wait <= 0, "note": ""}
    threshold = int(s["claim_threshold"])
    if out["bailout"]["ready"] and balance >= threshold:
        out["bailout"]["ready"] = False
        out["bailout"]["note"] = f"only below {threshold:,}"
    return out


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Individual claims (caller holds the member's lock) ──────────────────────

async def claim_stipend(member: discord.Member) -> Optional[str]:
    st = (await status(member))["stipend"]
    if not st["ready"]:
        return None
    s = await freebie_settings(member.guild)
    conf = CONFIG.member(member)
    data = await conf.all()
    today = _today()
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    streak = data.get("login_streak", 0) + 1 if data.get("login_streak_day") == yesterday else 1
    if data.get("login_streak_day") == today:
        streak = data.get("login_streak", 1)
    low = max(0, int(s["daily_min"]))
    base = random.randint(low, max(low, int(s["daily_max"])))
    bonus = int(base * streak_bonus(streak))
    deposited = await safe_deposit(member, base + bonus)
    await conf.daily_stipend_at.set(time.time())
    await conf.login_streak.set(streak)
    await conf.login_streak_day.set(today)
    cur = await currency(member.guild)
    extra = f" (+{bonus:,} streak bonus)" if bonus else ""
    return f"🎁 Stipend: **+{deposited:,} {cur}**{extra} · 🔥 day {streak}"


async def claim_scratch(member: discord.Member) -> Optional[Tuple[str, List[str]]]:
    if not (await status(member))["scratch"]["ready"]:
        return None
    prize = random.choices(SCRATCH_PRIZES, weights=SCRATCH_WEIGHTS, k=1)[0]
    deposited = await safe_deposit(member, prize) if prize else 0
    await CONFIG.member(member).scratch_claimed_at.set(time.time())
    cur = await currency(member.guild)
    if prize:
        symbols = [SCRATCH_SYMBOLS[prize]] * 3
        text = f"🎟️ Scratch: {' '.join(symbols)} **+{deposited:,} {cur}**" + (" 🎉 **BIG WINNER!**" if prize >= 5000 else "")
    else:
        pool = [v for k, v in SCRATCH_SYMBOLS.items() if k]
        symbols = random.sample(pool, 3)
        text = f"🎟️ Scratch: {' '.join(symbols)} no match — better luck tomorrow"
    return text, symbols


async def claim_bailout(member: discord.Member) -> Optional[str]:
    if not (await status(member))["bailout"]["ready"]:
        return None
    s = await freebie_settings(member.guild)
    deposited = await safe_deposit(member, max(0, int(s["claim_amount"])))
    await CONFIG.member(member).bailout_claimed_at.set(time.time())
    return f"🎩 Bailout: the dealer slides you **+{deposited:,} {await currency(member.guild)}**"


async def start_spin(member: discord.Member) -> Optional[int]:
    if not (await status(member))["spin"]["ready"]:
        return None
    await CONFIG.member(member).daily_spin_at.set(time.time())
    return random.randint(100, 1000)


async def take_spin(member: discord.Member, amount: int) -> str:
    s = await settle_game(member, "dailyspin", 0, amount, "push", include_economy=False)
    return f"🎲 Daily Spin: **+{s.deposited:,} {await currency(member.guild)}**"


# ── Panel ────────────────────────────────────────────────────────────────────

LABELS = {"stipend": ("Stipend", "🎁"), "spin": ("Daily Spin", "🎲"), "scratch": ("Scratch", "🎟️"), "bailout": ("Bailout", "🎩")}


async def panel_embed(member: discord.Member, log: List[str]) -> discord.Embed:
    st = await status(member)
    data = await CONFIG.member(member).all()
    balance = await bank.get_balance(member)
    cur = await currency(member.guild)
    now = time.time()
    lines = []
    for key, (label, emoji) in LABELS.items():
        info = st[key]
        if not info["enabled"]:
            lines.append(f"⚫ {emoji} **{label}** — off")
        elif info["ready"]:
            lines.append(f"✅ {emoji} **{label}** — ready!")
        elif info["note"]:
            lines.append(f"🔒 {emoji} **{label}** — {info['note']}")
        else:
            lines.append(f"⏳ {emoji} **{label}** — <t:{int(now + info['wait'])}:R>")
    streak = data.get("login_streak", 0)
    alive = data.get("login_streak_day") in (_today(), (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d"))
    streak_line = (f"🔥 **{streak}-day streak** · next stipend +{int(streak_bonus(streak + 1) * 100)}%"
                   if streak and alive else "🔥 Claim your stipend daily to build a streak (up to +70%)")
    embed = discord.Embed(
        title=f"🎁 Daily Rewards — {member.display_name}",
        description=f"💰 **{balance:,} {cur}**\n{streak_line}\n\n" + "\n".join(lines),
        color=0xF1C40F,
    )
    if log:
        embed.add_field(name="Just now", value="\n".join(log[-5:]), inline=False)
    now_utc = datetime.now(timezone.utc)
    next_daily = int((now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    embed.set_footer(text="Daily challenges reset at 00:00 UTC · see `casino challenges`")
    embed.timestamp = datetime.fromtimestamp(next_daily, tz=timezone.utc)
    return embed


class RewardsPanel(OwnedView):
    def __init__(self, member: discord.Member) -> None:
        super().__init__(member.id, timeout=300)
        self.member = member
        self.log: List[str] = []
        self.pending_spin: Optional[int] = None
        self.first_die: Optional[int] = None

    async def build(self) -> "RewardsPanel":
        self.clear_items()
        st = await status(self.member)
        any_ready = False
        for key, (label, emoji) in LABELS.items():
            btn = discord.ui.Button(label=label, emoji=emoji, disabled=not st[key]["ready"],
                                    style=discord.ButtonStyle.primary if st[key]["ready"] else discord.ButtonStyle.secondary)
            btn.callback = self._make(key)
            self.add_item(btn)
            any_ready = any_ready or st[key]["ready"]
        claim_all = discord.ui.Button(label="Claim all", emoji="⚡", style=discord.ButtonStyle.success,
                                      disabled=not any_ready, row=1)
        claim_all.callback = self._claim_all
        self.add_item(claim_all)
        return self

    async def _refresh(self, interaction: discord.Interaction, attachments=None) -> None:
        await self.build()
        kwargs = {"embed": await panel_embed(self.member, self.log), "view": self}
        kwargs["attachments"] = attachments or []
        await interaction.response.edit_message(**kwargs)

    def _make(self, key: str):
        async def go(interaction: discord.Interaction) -> None:
            async with _lock(self.member):
                if key == "spin":
                    amount = await start_spin(self.member)
                    if amount is None:
                        await self._refresh(interaction)
                        return
                    self.pending_spin = amount
                    await self._show_spin_offer(interaction, amount)
                    return
                if key == "scratch":
                    res = await claim_scratch(self.member)
                    if res:
                        self.log.append(res[0])
                elif key == "stipend":
                    res = await claim_stipend(self.member)
                    if res:
                        self.log.append(res)
                elif key == "bailout":
                    res = await claim_bailout(self.member)
                    if res:
                        self.log.append(res)
                await self._refresh(interaction)
        return go

    async def _claim_all(self, interaction: discord.Interaction) -> None:
        async with _lock(self.member):
            # Bailout first: it depends on the balance *before* today's other rewards land.
            for claim in (claim_bailout, claim_stipend):
                res = await claim(self.member)
                if res:
                    self.log.append(res)
            scratch = await claim_scratch(self.member)
            if scratch:
                self.log.append(scratch[0])
            amount = await start_spin(self.member)
            if amount is not None:
                self.log.append(await take_spin(self.member, amount))
            await self._refresh(interaction)

    # ── Daily Spin: take it or risk it on higher / lower dice ───────────────

    async def _show_spin_offer(self, interaction: discord.Interaction, amount: int) -> None:
        self.clear_items()
        cur = await currency(self.member.guild)
        take = discord.ui.Button(label=f"Take {amount:,}", emoji="💰", style=discord.ButtonStyle.success)
        risk = discord.ui.Button(label=f"Risk it for {amount * 2:,}", emoji="🎲", style=discord.ButtonStyle.danger)
        take.callback = self._take
        risk.callback = self._risk
        self.add_item(take)
        self.add_item(risk)
        embed = discord.Embed(
            title="🎲 Daily Spin",
            description=(f"You spun **{amount:,} {cur}**!\n\nTake it, or **risk it**: guess whether the next die "
                         f"rolls higher or lower. Right = **double**, tie = keep it, wrong = lose it."),
            color=0x9B59B6,
        )
        await interaction.response.edit_message(embed=embed, view=self, attachments=[])

    async def _take(self, interaction: discord.Interaction) -> None:
        async with _lock(self.member):
            if self.pending_spin is None:
                await interaction.response.defer()
                return
            amount, self.pending_spin = self.pending_spin, None
            self.log.append(await take_spin(self.member, amount))
            await self._refresh(interaction)

    async def _risk(self, interaction: discord.Interaction) -> None:
        if self.pending_spin is None:
            await interaction.response.defer()
            return
        self.first_die = random.randint(1, 6)
        self.clear_items()
        hi = discord.ui.Button(label="Higher", emoji="⬆️", style=discord.ButtonStyle.primary,
                               disabled=self.first_die == 6)
        lo = discord.ui.Button(label="Lower", emoji="⬇️", style=discord.ButtonStyle.primary,
                               disabled=self.first_die == 1)
        hi.callback = self._guess("higher")
        lo.callback = self._guess("lower")
        self.add_item(hi)
        self.add_item(lo)
        embed = discord.Embed(title="🎲 Daily Spin — higher or lower?",
                              description=f"First roll: **{self.first_die}**\nWill the next die be higher or lower?",
                              color=0x9B59B6)
        embed.set_thumbnail(url="attachment://first.png")
        await interaction.response.edit_message(
            embed=embed, view=self, attachments=[discord.File(DICE_PATH / f"{self.first_die}.png", filename="first.png")]
        )

    def _guess(self, guess: str):
        async def go(interaction: discord.Interaction) -> None:
            async with _lock(self.member):
                if self.pending_spin is None or self.first_die is None:
                    await interaction.response.defer()
                    return
                amount, self.pending_spin = self.pending_spin, None
                second = random.randint(1, 6)
                first = self.first_die
                cur = await currency(self.member.guild)
                if second == first:
                    s = await settle_game(self.member, "dailyspin", amount, amount, "push", include_economy=False)
                    self.log.append(f"🎲 Spin gamble: {first} → {second}, a tie — kept **{s.deposited:,} {cur}**")
                elif (guess == "higher") == (second > first):
                    s = await settle_game(self.member, "dailyspin", amount, amount * 2, "win", include_economy=False)
                    self.log.append(f"🎲 Spin gamble: {first} → {second} 🔥 doubled to **{s.deposited:,} {cur}**!")
                else:
                    await settle_game(self.member, "dailyspin", amount, 0, "loss", include_economy=False)
                    self.log.append(f"🎲 Spin gamble: {first} → {second} 💀 lost the {amount:,}")
                self.first_die = None
                await self.build()
                embed = await panel_embed(self.member, self.log)
                embed.set_thumbnail(url="attachment://second.png")
                await interaction.response.edit_message(
                    embed=embed, view=self,
                    attachments=[discord.File(DICE_PATH / f"{second}.png", filename="second.png")],
                )
        return go

    async def on_timeout(self) -> None:
        # Never let a spin offer vanish: an unanswered offer is taken automatically.
        async with _lock(self.member):
            if self.pending_spin is not None:
                amount, self.pending_spin = self.pending_spin, None
                with suppress(Exception):
                    await take_spin(self.member, amount)
        await super().on_timeout()


async def open_panel(ctx) -> None:
    panel = await RewardsPanel(ctx.author).build()
    panel.message = await ctx.send(embed=await panel_embed(ctx.author, []), view=panel)
