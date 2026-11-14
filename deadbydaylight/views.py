"""Buttons and menus for the Dead by Daylight cog."""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

if TYPE_CHECKING:
    from .dbd import DeadByDaylight

log = logging.getLogger("red.deadbydaylight")

P100_EDIT_ID = "deadbydaylight:p100:edit"


def png_file(data: bytes, name: str) -> discord.File:
    return discord.File(io.BytesIO(data), filename=f"{name}.png")


class RerollView(discord.ui.View):
    """🎲 Reroll for whoever asked. `render()` returns (content, file)."""

    def __init__(self, owner_id: int, render: Callable[[], Awaitable[tuple]]):
        super().__init__(timeout=900)
        self.owner_id, self.render = owner_id, render
        self.message: discord.Message | None = None

    @discord.ui.button(label="Reroll", emoji="🎲", style=discord.ButtonStyle.danger)
    async def reroll(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("Only the person who rolled can reroll.", ephemeral=True)
        await interaction.response.defer()
        content, file = await self.render()
        await interaction.edit_original_response(content=content, attachments=[file] if file else [], view=self)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class P100BoardView(discord.ui.View):
    """Persistent 'Update my P100s' button under prestige boards (survives restarts)."""

    def __init__(self, cog: "DeadByDaylight"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Update my P100s", emoji="🩸", style=discord.ButtonStyle.danger, custom_id=P100_EDIT_ID)
    async def edit(self, interaction: discord.Interaction, _button: discord.ui.Button):
        try:
            await self.cog.data.load()
        except Exception:  # noqa: BLE001
            return await interaction.response.send_message("Couldn't load the character list, try again shortly.", ephemeral=True)
        editor = P100Editor(self.cog, interaction.user, interaction.guild, interaction.channel)
        await editor.load()
        await interaction.response.send_message(editor.caption(), view=editor, ephemeral=True)


def _chunks(items: list, size: int = 25) -> list[list]:
    """Split into even pages of at most `size` (44 -> 22+22, 54 -> 18+18+18)."""
    if not items:
        return []
    pages = (len(items) + size - 1) // size
    per = (len(items) + pages - 1) // pages
    return [items[i:i + per] for i in range(0, len(items), per)]


class P100Editor(discord.ui.View):
    """Private tick-list of every character. Saves as soon as you change a menu."""

    def __init__(self, cog: "DeadByDaylight", user, guild, channel):
        super().__init__(timeout=900)
        self.cog, self.user, self.guild, self.channel = cog, user, guild, channel
        self.done: set[str] = set()
        self.pages: list[tuple[str, list]] = []
        for role, title in (("killer", "Killers"), ("survivor", "Survivors")):
            chunks = _chunks(cog.data.roster(role))
            for i, chunk in enumerate(chunks):
                label = title if len(chunks) == 1 else f"{title} {chunk[0].short[0]}–{chunk[-1].short[0]}"
                self.pages.append((label, chunk))
        self.page = 0

    async def load(self):
        self.done = set(await self.cog.config.user(self.user).p100())
        self._build()

    def caption(self) -> str:
        label, chars = self.pages[self.page]
        total = sum(len(c) for _, c in self.pages)
        mine = sum(1 for _, cs in self.pages for c in cs if c.id in self.done)
        here = sum(1 for c in chars if c.id in self.done)
        return (
            f"🩸 **Your Prestige 100s: {mine}/{total}**\n"
            f"Tick every character you've maxed in **{label}** ({here}/{len(chars)}). It saves straight away."
        )

    def _build(self):
        self.clear_items()
        label, chars = self.pages[self.page]
        select = discord.ui.Select(
            placeholder=f"Tick your P100 {label.lower()}…",
            min_values=0, max_values=len(chars), row=0,
            options=[discord.SelectOption(label=c.name[:100], value=c.id[:100], default=c.id in self.done) for c in chars],
        )
        select.callback = self._on_select
        self.add_item(select)
        for i, (lab, _chars) in enumerate(self.pages):
            b = discord.ui.Button(
                label=lab, row=1 + i // 5,
                style=discord.ButtonStyle.danger if i == self.page else discord.ButtonStyle.secondary,
            )

            async def cb(interaction: discord.Interaction, i=i):
                self.page = i
                self._build()
                await interaction.response.edit_message(content=self.caption(), view=self)

            b.callback = cb
            self.add_item(b)

    async def _on_select(self, interaction: discord.Interaction):
        _label, chars = self.pages[self.page]
        page_ids = {c.id for c in chars}
        chosen = set(interaction.data.get("values", []))
        added = chosen - self.done
        removed = (page_ids - chosen) & self.done
        async with self.cog.config.user(self.user).p100() as p100:
            for cid in added:
                if cid not in p100:
                    p100.append(cid)
            for cid in removed:
                if cid in p100:
                    p100.remove(cid)
            self.done = set(p100)
        self._build()
        await interaction.response.edit_message(content=self.caption(), view=self)
        if added and self.guild is not None:
            names = sorted(self.cog.data.characters[c].name for c in added if c in self.cog.data.characters)
            await self.cog.p100_shoutout(self.guild, self.user, names, len(self.done), self.channel)
