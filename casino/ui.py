"""Shared buttons for the casino: Play again / 2× / ½, and the quick bet picker."""
from __future__ import annotations

import logging
from contextlib import suppress
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

import discord

log = logging.getLogger("red.crewcogs.casino.ui")

Replay = Callable[[discord.Interaction, int], Awaitable[None]]
Extra = Tuple[str, Optional[str], discord.ButtonStyle, Callable[[discord.Interaction], Awaitable[None]]]


def short(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n / 1_000:.0f}K"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K".replace(".0K", "K")
    return str(n)


class OwnedView(discord.ui.View):
    def __init__(self, owner_id: int, *, timeout: Optional[float] = 120) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("That's someone else's game — start your own!", ephemeral=True)
            return False
        return True

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)) and not getattr(item, "url", None):
                item.disabled = True

    async def on_timeout(self) -> None:
        self.disable_all()
        if self.message is not None:
            with suppress(discord.HTTPException):
                await self.message.edit(view=self)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("Casino view error", exc_info=error)
        with suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send("⚠️ Something went wrong. Your wager is safe — try again.", ephemeral=True)
            else:
                await interaction.response.send_message("⚠️ Something went wrong. Try again.", ephemeral=True)


class GameOverView(OwnedView):
    """Under every result: replay instantly with the same, double, or half bet."""

    def __init__(self, owner_id: int, bet: int, replay: Replay, *, extras: Sequence[Extra] = (),
                 again_label: str = "Again", row: Optional[int] = None) -> None:
        super().__init__(owner_id, timeout=180)
        self.bet = bet
        self.replay = replay
        options = [
            (f"{again_label} · {short(bet)}", "🔁", discord.ButtonStyle.success, bet),
            (f"2× · {short(bet * 2)}", "⏫", discord.ButtonStyle.primary, bet * 2),
        ]
        if bet >= 2:
            options.append((f"½ · {short(bet // 2)}", "⏬", discord.ButtonStyle.secondary, bet // 2))
        for label, emoji, style, amount in options:
            btn = discord.ui.Button(label=label, emoji=emoji, style=style, row=row)
            btn.callback = self._make_replay(amount)
            self.add_item(btn)
        for label, emoji, style, callback in extras:
            btn = discord.ui.Button(label=label, emoji=emoji, style=style, row=row)
            btn.callback = self._wrap(callback)
            self.add_item(btn)

    async def _close(self, interaction: discord.Interaction) -> None:
        self.disable_all()
        self.stop()
        await interaction.response.edit_message(view=self)

    def _make_replay(self, amount: int):
        async def _go(interaction: discord.Interaction) -> None:
            await self._close(interaction)
            await self.replay(interaction, amount)
        return _go

    def _wrap(self, callback):
        async def _go(interaction: discord.Interaction) -> None:
            await self._close(interaction)
            await callback(interaction)
        return _go


class CustomBetModal(discord.ui.Modal, title="Custom bet"):
    amount = discord.ui.TextInput(label="How much?", placeholder="e.g. 750, 2.5k, half, all", max_length=20)

    def __init__(self, on_bet: Callable[[discord.Interaction, str], Awaitable[None]]) -> None:
        super().__init__()
        self.on_bet = on_bet

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.on_bet(interaction, str(self.amount.value))


class BetPanelView(OwnedView):
    """Pick a stake with one tap (or type a custom one)."""

    AMOUNTS = (100, 500, 1_000, 5_000, 25_000)

    def __init__(self, owner_id: int, on_bet: Callable[[discord.Interaction, str], Awaitable[None]],
                 *, max_bet: int = 0) -> None:
        super().__init__(owner_id, timeout=300)
        self.on_bet = on_bet
        for amount in self.AMOUNTS:
            if max_bet and amount > max_bet:
                continue
            self._add(f"{short(amount)}", None, discord.ButtonStyle.secondary, str(amount), row=0)
        self._add("Half", "🌗", discord.ButtonStyle.primary, "half", row=1)
        self._add("All in", "🔥", discord.ButtonStyle.danger, "all", row=1)
        custom = discord.ui.Button(label="Custom…", emoji="✏️", style=discord.ButtonStyle.success, row=1)
        custom.callback = self._custom
        self.add_item(custom)

    def _add(self, label: str, emoji: Optional[str], style: discord.ButtonStyle, raw: str, row: int) -> None:
        btn = discord.ui.Button(label=label, emoji=emoji, style=style, row=row)

        async def _go(interaction: discord.Interaction) -> None:
            await self.on_bet(interaction, raw)
        btn.callback = _go
        self.add_item(btn)

    async def _custom(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CustomBetModal(self.on_bet))


class ChoiceView(OwnedView):
    """A row of buttons for picking an option (heads/tails, red/black, …)."""

    def __init__(self, owner_id: int, choices: List[Tuple[str, Optional[str], discord.ButtonStyle, str]],
                 on_choice: Callable[[discord.Interaction, str], Awaitable[None]], *, timeout: float = 60) -> None:
        super().__init__(owner_id, timeout=timeout)
        self.chosen: Optional[str] = None
        for i, (label, emoji, style, value) in enumerate(choices):
            btn = discord.ui.Button(label=label, emoji=emoji, style=style, row=i // 5)

            async def _go(interaction: discord.Interaction, value: str = value) -> None:
                self.chosen = value
                self.disable_all()
                self.stop()
                await interaction.response.edit_message(view=self)
                await on_choice(interaction, value)
            btn.callback = _go
            self.add_item(btn)
