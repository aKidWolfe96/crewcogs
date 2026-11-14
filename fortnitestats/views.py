"""Discord UI for FortniteStats: shop browser, stat-card toggles, wishlist button."""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import discord

from . import statsrender as st

if TYPE_CHECKING:
    from .fortnitestats import FortniteStats, ShopState

log = logging.getLogger("red.fortnitestats")

BROWSE_ID = "fortnitestats:shop:browse"


def png_file(data: bytes, name: str) -> discord.File:
    ext = "jpg" if data[:3] == b"\xff\xd8\xff" else "png"
    return discord.File(io.BytesIO(data), filename=f"{name}.{ext}")


# ─────────────────────────────── shop ─────────────────────────────── #
class ShopPostView(discord.ui.View):
    """Persistent 'Browse the shop' button under every shop post (survives restarts)."""

    def __init__(self, cog: "FortniteStats"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Browse the shop", emoji="🛒", style=discord.ButtonStyle.success, custom_id=BROWSE_ID)
    async def browse(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.open_browser(interaction)


class SectionSelect(discord.ui.Select):
    def __init__(self, state: "ShopState", current: str):
        options = []
        for name, cards in state.model.sections[:25]:
            options.append(discord.SelectOption(
                label=name[:100], value=name[:100],
                description=f"{len(cards)} item{'s' if len(cards) != 1 else ''}",
                default=name[:100] == current[:100],
            ))
        super().__init__(placeholder="Jump to a section…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        view: ShopBrowser = self.view
        target = self.values[0]
        for i, (name, *_rest) in enumerate(view.state.model.pages):
            if name[:100] == target:
                await view.show(interaction, i)
                return


class ShopBrowser(discord.ui.View):
    """Private (ephemeral) page-by-page shop browser."""

    def __init__(self, cog: "FortniteStats", state: "ShopState", index: int = 0):
        super().__init__(timeout=900)
        self.cog, self.state, self.index = cog, state, index
        self._rebuild()

    @property
    def total(self) -> int:
        return len(self.state.model.pages)

    def _rebuild(self):
        self.clear_items()
        section = self.state.model.pages[self.index][0]
        self.add_item(SectionSelect(self.state, section))
        prev_btn = discord.ui.Button(emoji="◀️", style=discord.ButtonStyle.secondary, row=1, disabled=self.index == 0)
        count = discord.ui.Button(label=f"{self.index + 1} / {self.total}", style=discord.ButtonStyle.secondary, row=1, disabled=True)
        next_btn = discord.ui.Button(emoji="▶️", style=discord.ButtonStyle.secondary, row=1, disabled=self.index >= self.total - 1)
        prev_btn.callback = lambda i: self.show(i, self.index - 1)
        next_btn.callback = lambda i: self.show(i, self.index + 1)
        for b in (prev_btn, count, next_btn):
            self.add_item(b)

    def caption(self) -> str:
        name, num, total, cards = self.state.model.pages[self.index]
        part = f" · page {num}/{total}" if total > 1 else ""
        return f"🛒 **{name}**{part} · {len(cards)} item{'s' if len(cards) != 1 else ''}"

    async def render(self) -> discord.File:
        png = await self.cog.shop_page_png(self.state, self.index)
        return png_file(png, f"shop-{self.index + 1}")

    async def show(self, interaction: discord.Interaction, index: int):
        self.index = max(0, min(index, self.total - 1))
        await interaction.response.defer()
        try:
            file = await self.render()
        except Exception:  # noqa: BLE001
            log.exception("shop page render failed")
            await interaction.followup.send("Couldn't draw that page, try again in a moment.", ephemeral=True)
            return
        self._rebuild()
        await interaction.edit_original_response(content=self.caption(), attachments=[file], view=self)


# ─────────────────────────────── stats ─────────────────────────────── #
class InputSelect(discord.ui.Select):
    def __init__(self, current: str, available: set[str]):
        options = [
            discord.SelectOption(label=label, value=key, default=key == current)
            for key, label in st.INPUTS.items()
            if key == "all" or key in available
        ]
        super().__init__(placeholder="Input type", options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        self.view.input_key = self.values[0]
        await self.view.refresh(interaction)


class StatsView(discord.ui.View):
    """Lifetime / Season toggle + input picker for a stat card."""

    def __init__(self, cog: "FortniteStats", lookup: dict, platform_label: str, window: str, stats: dict):
        super().__init__(timeout=600)
        self.cog, self.lookup, self.platform_label = cog, lookup, platform_label
        self.window = window
        self.input_key = "all"
        self.stats = stats          # window -> BrPlayerStats (filled lazily)
        self.message: discord.Message | None = None
        self._rebuild()

    def _available_inputs(self) -> set[str]:
        s = self.stats.get(self.window)
        inputs = getattr(s, "inputs", None)
        out = set()
        for key in ("keyboard_mouse", "gamepad", "touch"):
            block = getattr(inputs, key, None) if inputs else None
            if block and block.overall and block.overall.matches:
                out.add(key)
        return out

    def _rebuild(self):
        self.clear_items()
        for window, label in (("lifetime", "Lifetime"), ("season", "This Season")):
            b = discord.ui.Button(
                label=label, row=0,
                style=discord.ButtonStyle.primary if window == self.window else discord.ButtonStyle.secondary,
            )
            b.callback = self._window_cb(window)
            self.add_item(b)
        self.add_item(InputSelect(self.input_key, self._available_inputs()))

    def _window_cb(self, window):
        async def cb(interaction: discord.Interaction):
            self.window = window
            await self.refresh(interaction)
        return cb

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if self.window not in self.stats:
            try:
                self.stats[self.window] = await self.cog.fetch_stats(self.lookup, self.window)
            except Exception as exc:  # noqa: BLE001
                await interaction.followup.send(self.cog.stats_error(exc, self.lookup), ephemeral=True)
                return
        if self.input_key != "all" and self.input_key not in self._available_inputs():
            self.input_key = "all"
        content, file = await self.cog.stats_card(self.stats[self.window], self.window, self.input_key, self.platform_label)
        self._rebuild()
        await interaction.edit_original_response(content=content, attachments=[file] if file else [], view=self)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class CompareView(discord.ui.View):
    def __init__(self, cog: "FortniteStats", a: tuple, b: tuple, window: str, cache: dict):
        super().__init__(timeout=600)
        self.cog, self.a, self.b, self.window, self.cache = cog, a, b, window, cache
        self.message: discord.Message | None = None
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        for window, label in (("lifetime", "Lifetime"), ("season", "This Season")):
            b = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if window == self.window else discord.ButtonStyle.secondary,
            )

            async def cb(interaction: discord.Interaction, window=window):
                self.window = window
                await interaction.response.defer()
                content, file = await self.cog.compare_card(self.a, self.b, window, self.cache)
                self._rebuild()
                await interaction.edit_original_response(content=content, attachments=[file] if file else [], view=self)

            b.callback = cb
            self.add_item(b)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


# ─────────────────────────────── wishlist ─────────────────────────────── #
class WishlistButton(discord.ui.View):
    def __init__(self, cog: "FortniteStats", item_id: str, item_name: str):
        super().__init__(timeout=300)
        self.cog, self.item_id, self.item_name = cog, item_id, item_name

    @discord.ui.button(label="Add to my wishlist", emoji="⭐", style=discord.ButtonStyle.primary)
    async def add(self, interaction: discord.Interaction, _button: discord.ui.Button):
        msg = await self.cog.wishlist_add(interaction.user, self.item_id, self.item_name)
        await interaction.response.send_message(msg, ephemeral=True)
