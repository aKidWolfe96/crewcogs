"""Interactive views (buttons / select menus) for PokéBot.

Views stay thin: they hold just enough state to know *what* was clicked and
hand the real work back to the cog, which owns locking and persistence.
"""
from __future__ import annotations

import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import discord

from .embeds import ARTWORK_BASE, COLORS, TYPE_COLORS, TYPE_EMOJIS, pretty_move

if TYPE_CHECKING:  # pragma: no cover
    from .pokebot import PokéBot

log = logging.getLogger("red.pokebot.ui")

BALL_BUTTONS = (
    ("pokeball",  "Poké Ball",  "🔴", discord.ButtonStyle.danger),
    ("greatball", "Great Ball", "🔵", discord.ButtonStyle.primary),
    ("ultraball", "Ultra Ball", "⚫", discord.ButtonStyle.secondary),
)


async def _safe_ephemeral(interaction: discord.Interaction, content: str) -> None:
    with suppress(discord.HTTPException):
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)


class BaseView(discord.ui.View):
    """View with sane timeout handling (buttons grey out) and error reporting."""

    def __init__(self, *, timeout: Optional[float] = 120.0) -> None:
        super().__init__(timeout=timeout)
        self.message: Optional[discord.Message] = None

    def disable_all(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]

    async def on_timeout(self) -> None:
        self.disable_all()
        if self.message is not None:
            with suppress(discord.HTTPException):
                await self.message.edit(view=self)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("PokéBot view error", exc_info=error)
        await _safe_ephemeral(interaction, "⚠️ Something went wrong handling that. Please try again.")


class OwnerView(BaseView):
    """Only `owner_id` may press the buttons."""

    def __init__(self, owner_id: int, *, timeout: Optional[float] = 120.0) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("These buttons aren't for you!", ephemeral=True)
            return False
        return True


# ──────────────────────────────────────────────────────────────────────────────
# Generic confirm / accept views
# ──────────────────────────────────────────────────────────────────────────────

class ConfirmView(OwnerView):
    """Yes / No buttons. `await view.wait()` then read `view.value` (True/False/None)."""

    def __init__(self, owner_id: int, *, confirm_label: str = "Confirm", confirm_emoji: str = "✅",
                 cancel_label: str = "Cancel", danger: bool = False, timeout: float = 45.0) -> None:
        super().__init__(owner_id, timeout=timeout)
        self.value: Optional[bool] = None
        self.confirm.label = confirm_label
        self.confirm.emoji = confirm_emoji
        self.confirm.style = discord.ButtonStyle.danger if danger else discord.ButtonStyle.success
        self.cancel.label = cancel_label

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = True
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.value = False
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()


class RequestView(BaseView):
    """Accept / Decline for a request sent to `target_id` (the requester may withdraw it).

    `value` ends as "accept", "decline", "cancel" or None (timed out).
    """

    def __init__(self, requester_id: int, target_id: int, *, accept_label: str = "Accept",
                 timeout: float = 90.0) -> None:
        super().__init__(timeout=timeout)
        self.requester_id = requester_id
        self.target_id = target_id
        self.value: Optional[str] = None
        self.accept.label = accept_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in (self.requester_id, self.target_id):
            await interaction.response.send_message("This request isn't for you!", ephemeral=True)
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, value: str) -> None:
        self.value = value
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Accept", emoji="✅", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("Only the other trainer can accept.", ephemeral=True)
            return
        await self._finish(interaction, "accept")

    @discord.ui.button(label="Decline", emoji="✖️", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, "decline" if interaction.user.id == self.target_id else "cancel")


# ──────────────────────────────────────────────────────────────────────────────
# Starter picker
# ──────────────────────────────────────────────────────────────────────────────

STARTER_TYPES = ("grass", "fire", "water")


def starter_embeds(trio: List[dict], gen: int) -> List[discord.Embed]:
    embeds = []
    for starter, ptype in zip(trio, STARTER_TYPES):
        e = discord.Embed(
            title=f"{TYPE_EMOJIS[ptype]} {starter['name']}",
            description=f"#{starter['id']:04d} · Gen {gen} · {ptype.capitalize()} type",
            color=TYPE_COLORS[ptype],
        )
        e.set_thumbnail(url=f"{ARTWORK_BASE}/{starter['id']}.png")
        embeds.append(e)
    return embeds


class StarterView(OwnerView):
    """Pick a generation from the menu, then tap one of its three starters."""

    def __init__(self, owner_id: int, starters: List[dict]) -> None:
        super().__init__(owner_id, timeout=120.0)
        self.starters = starters
        self.choice: Optional[dict] = None
        self.gen_select = discord.ui.Select(
            placeholder="🌍 Choose a region / generation…",
            options=[
                discord.SelectOption(
                    label=f"Generation {g}",
                    description=" · ".join(s["name"] for s in starters[(g - 1) * 3: g * 3]),
                    value=str(g),
                )
                for g in range(1, len(starters) // 3 + 1)
            ],
        )
        self.gen_select.callback = self._on_gen
        self.add_item(self.gen_select)

    async def _on_gen(self, interaction: discord.Interaction) -> None:
        gen = int(self.gen_select.values[0])
        trio = self.starters[(gen - 1) * 3: gen * 3]
        for item in [c for c in self.children if isinstance(c, discord.ui.Button)]:
            self.remove_item(item)
        styles = (discord.ButtonStyle.success, discord.ButtonStyle.danger, discord.ButtonStyle.primary)
        for starter, ptype, style in zip(trio, STARTER_TYPES, styles):
            btn = discord.ui.Button(label=f"Choose {starter['name']}", emoji=TYPE_EMOJIS[ptype], style=style)
            btn.callback = self._make_pick(starter)
            self.add_item(btn)
        await interaction.response.edit_message(embeds=starter_embeds(trio, gen), view=self)

    def _make_pick(self, starter: dict):
        async def _pick(interaction: discord.Interaction) -> None:
            self.choice = starter
            self.disable_all()
            await interaction.response.edit_message(view=self)
            self.stop()
        return _pick


# ──────────────────────────────────────────────────────────────────────────────
# Wild spawn
# ──────────────────────────────────────────────────────────────────────────────

class SpawnView(BaseView):
    """Buttons under a wild Pokémon: quick-throw each ball, or open the trainer panel."""

    def __init__(self, cog: "PokéBot", spawn_id: str) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.spawn_id = spawn_id
        for ball, label, emoji, style in BALL_BUTTONS:
            btn = discord.ui.Button(label=label, emoji=emoji, style=style, row=0)
            btn.callback = self._make_throw(ball)
            self.add_item(btn)
        check = discord.ui.Button(label="Do I have it?", emoji="📖", style=discord.ButtonStyle.success, row=1)
        check.callback = self._dex_check
        self.add_item(check)

    def _make_throw(self, ball: str):
        async def _throw(interaction: discord.Interaction) -> None:
            await self.cog.handle_throw(interaction, self.spawn_id, ball, None)
        return _throw

    async def _dex_check(self, interaction: discord.Interaction) -> None:
        await self.cog.spawn_dex_check(interaction, self.spawn_id)


class ThrowPanelView(OwnerView):
    """Ephemeral, per-trainer panel: pick a berry and throw any ball."""

    def __init__(self, cog: "PokéBot", owner_id: int, spawn_id: str,
                 balls: Dict[str, int], berries: Dict[str, int], berry_names: Dict[str, str]) -> None:
        super().__init__(owner_id, timeout=180.0)
        self.cog = cog
        self.spawn_id = spawn_id
        self.berry: Optional[str] = None

        options = [discord.SelectOption(label="No berry", value="none", emoji="🚫", default=True)]
        for slug, label in berry_names.items():
            count = berries.get(slug, 0)
            if count > 0:
                emoji, _, name = label.partition(" ")
                options.append(discord.SelectOption(label=f"{name} ×{count}", value=slug, emoji=emoji))
        self.berry_select = discord.ui.Select(placeholder="🍓 Feed a berry first (optional)", options=options, row=0)
        self.berry_select.callback = self._on_berry
        self.add_item(self.berry_select)

        for ball, label, emoji, style in BALL_BUTTONS:
            count = balls.get(ball, 0)
            btn = discord.ui.Button(label=f"{label} ×{count}", emoji=emoji, style=style,
                                    disabled=count <= 0, row=1)
            btn.callback = self._make_throw(ball)
            self.add_item(btn)

    async def _on_berry(self, interaction: discord.Interaction) -> None:
        value = self.berry_select.values[0]
        self.berry = None if value == "none" else value
        for opt in self.berry_select.options:
            opt.default = opt.value == value
        await interaction.response.edit_message(view=self)

    def _make_throw(self, ball: str):
        async def _throw(interaction: discord.Interaction) -> None:
            await self.cog.handle_throw(interaction, self.spawn_id, ball, self.berry, panel=self)
        return _throw


# ──────────────────────────────────────────────────────────────────────────────
# Healing
# ──────────────────────────────────────────────────────────────────────────────

class HealView(OwnerView):
    """Shown when the bag can't cover everything: heal with what you have, or buy the rest."""

    def __init__(self, cog: "PokéBot", owner_id: int, *, scope: str, mode: str,
                 can_partial: bool, cost: int, currency: str, affordable: bool) -> None:
        super().__init__(owner_id, timeout=120.0)
        self.cog = cog
        self.scope = scope
        self.mode = mode
        self.use_owned.disabled = not can_partial
        self.buy.label = f"Buy missing & heal all ({cost:,} {currency})"
        if not affordable:
            self.buy.style = discord.ButtonStyle.secondary

    @discord.ui.button(label="Buy missing & heal all", emoji="🛒", style=discord.ButtonStyle.success)
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.execute_heal(interaction, self, buy=True)

    @discord.ui.button(label="Use only what I have", emoji="🎒", style=discord.ButtonStyle.primary)
    async def use_owned(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.execute_heal(interaction, self, buy=False)

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()


class HealShortcutView(OwnerView):
    """A single 'Heal' button bolted onto party / profile / failed-catch messages."""

    def __init__(self, cog: "PokéBot", owner_id: int, *, scope: str = "party", label: str = "Heal party") -> None:
        super().__init__(owner_id, timeout=180.0)
        self.cog = cog
        self.scope = scope
        self.heal.label = label

    @discord.ui.button(label="Heal party", emoji="🩹", style=discord.ButtonStyle.success)
    async def heal(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.heal_from_interaction(interaction, self.scope)


# ──────────────────────────────────────────────────────────────────────────────
# Battles
# ──────────────────────────────────────────────────────────────────────────────

class BattleView(BaseView):
    """Attached to every battle board: open your private move picker, or forfeit."""

    def __init__(self, cog: "PokéBot", battle_id: str, player_ids: List[int]) -> None:
        super().__init__(timeout=600.0)
        self.cog = cog
        self.battle_id = battle_id
        self.player_ids = player_ids

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self.player_ids:
            await interaction.response.send_message("You're not in this battle!", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Choose Move", emoji="⚔️", style=discord.ButtonStyle.danger)
    async def choose(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.battle_open_picker(interaction, self.battle_id)

    @discord.ui.button(label="Forfeit", emoji="🏳️", style=discord.ButtonStyle.secondary)
    async def forfeit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.battle_forfeit(interaction, self.battle_id)


class MovePickerView(OwnerView):
    """Ephemeral list of your Pokémon's moves."""

    def __init__(self, cog: "PokéBot", owner_id: int, battle_id: str, moves: List[Dict[str, Any]]) -> None:
        super().__init__(owner_id, timeout=180.0)
        self.cog = cog
        self.battle_id = battle_id
        for mv in moves:
            power = f" · {mv['power']}" if mv.get("power") else ""
            btn = discord.ui.Button(
                label=f"{pretty_move(mv['slug'])}{power}"[:80],
                emoji=TYPE_EMOJIS.get(mv.get("type") or "", "🎯"),
                style=discord.ButtonStyle.primary,
            )
            btn.callback = self._make_pick(mv["slug"])
            self.add_item(btn)

    def _make_pick(self, slug: str):
        async def _pick(interaction: discord.Interaction) -> None:
            await self.cog.battle_submit_interaction(interaction, self.battle_id, slug, picker=self)
        return _pick


# ──────────────────────────────────────────────────────────────────────────────
# Raids
# ──────────────────────────────────────────────────────────────────────────────

class RaidJoinView(BaseView):
    def __init__(self, cog: "PokéBot", guild_id: int, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.guild_id = guild_id

    @discord.ui.button(label="Join Raid", emoji="⚔️", style=discord.ButtonStyle.danger)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.raid_join_interaction(interaction)


# ──────────────────────────────────────────────────────────────────────────────
# Shop
# ──────────────────────────────────────────────────────────────────────────────

class ShopView(OwnerView):
    """Pick an item from the menu, then buy ×1 / ×5 / ×10 with one click."""

    def __init__(self, cog: "PokéBot", owner_id: int, items: List[dict], prices: Dict[str, int]) -> None:
        super().__init__(owner_id, timeout=180.0)
        self.cog = cog
        self.selected: Optional[str] = None
        self.select = discord.ui.Select(
            placeholder="🛒 Pick an item to buy…",
            options=[
                discord.SelectOption(
                    label=f"{i['name']} — {prices.get(i['id'], i['price']):,}",
                    description=i["desc"][:100], value=i["id"], emoji=i["emoji"],
                )
                for i in items
            ],
            row=0,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)
        for qty in (1, 5, 10):
            btn = discord.ui.Button(label=f"Buy ×{qty}", style=discord.ButtonStyle.success, disabled=True, row=1)
            btn.callback = self._make_buy(qty)
            self.add_item(btn)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.selected = self.select.values[0]
        for opt in self.select.options:
            opt.default = opt.value == self.selected
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = False
        await interaction.response.edit_message(view=self)

    def _make_buy(self, qty: int):
        async def _buy(interaction: discord.Interaction) -> None:
            if not self.selected:
                await interaction.response.send_message("Pick an item first!", ephemeral=True)
                return
            await self.cog.shop_purchase_interaction(interaction, self, self.selected, qty)
        return _buy


class LinkButtonsView(OwnerView):
    """Profile / party shortcuts."""

    def __init__(self, cog: "PokéBot", owner_id: int, *, show_heal: bool, show_party: bool = True) -> None:
        super().__init__(owner_id, timeout=180.0)
        self.cog = cog
        if not show_heal:
            self.remove_item(self.heal)
        if not show_party:
            self.remove_item(self.party)

    @discord.ui.button(label="Party", emoji="🎒", style=discord.ButtonStyle.primary)
    async def party(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.party_from_interaction(interaction)

    @discord.ui.button(label="Heal party", emoji="🩹", style=discord.ButtonStyle.success)
    async def heal(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.heal_from_interaction(interaction, "party")

    @discord.ui.button(label="Bag", emoji="👜", style=discord.ButtonStyle.secondary)
    async def bag(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.bag_from_interaction(interaction)


__all__ = [
    "BaseView", "OwnerView", "ConfirmView", "RequestView", "StarterView", "starter_embeds",
    "SpawnView", "ThrowPanelView", "HealView", "HealShortcutView", "BattleView",
    "MovePickerView", "RaidJoinView", "ShopView", "LinkButtonsView", "COLORS",
]
