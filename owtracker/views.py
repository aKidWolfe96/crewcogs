"""Buttons and menus for the Overwatch cog."""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

import discord

if TYPE_CHECKING:
    from .overwatch import Overwatch

log = logging.getLogger("red.owtracker")

SPRAY_EDIT_ID = "owtracker:sprays:edit"
MODES = (("all", "All modes"), ("quickplay", "Quick Play"), ("competitive", "Competitive"))


def png_file(data: bytes, name: str) -> discord.File:
    ext = "jpg" if data[:3] == b"\xff\xd8\xff" else "png"
    return discord.File(io.BytesIO(data), filename=f"{name}.{ext}")


class _Expiring(discord.ui.View):
    message: discord.Message | None = None

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class ModeView(_Expiring):
    """All / Quick Play / Competitive toggle for any card.

    `render(mode)` returns (content, file); file may be None with a reason in content.
    """

    def __init__(self, render: Callable[[str], Awaitable[tuple]], mode: str = "all"):
        super().__init__(timeout=600)
        self.render, self.mode = render, mode
        self._build()

    def _build(self):
        self.clear_items()
        for key, label in MODES:
            b = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.primary if key == self.mode else discord.ButtonStyle.secondary,
            )

            async def cb(interaction: discord.Interaction, key=key):
                self.mode = key
                await interaction.response.defer()
                try:
                    content, file = await self.render(key)
                except Exception:  # noqa: BLE001
                    log.exception("card render failed")
                    return await interaction.followup.send("Couldn't load that, try again shortly.", ephemeral=True)
                self._build()
                await interaction.edit_original_response(
                    content=content, attachments=[file] if file else [], view=self
                )

            b.callback = cb
            self.add_item(b)


class RerollView(_Expiring):
    """A single Reroll button (only the person who asked can press it)."""

    def __init__(self, owner_id: int, render: Callable[[], Awaitable[tuple]], label="Reroll"):
        super().__init__(timeout=900)
        self.owner_id, self.render = owner_id, render
        b = discord.ui.Button(label=label, emoji="🎲", style=discord.ButtonStyle.primary)
        b.callback = self._reroll
        self.add_item(b)

    async def _reroll(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("Only the person who rolled can reroll.", ephemeral=True)
        content, embed = await self.render()
        await interaction.response.edit_message(content=content, embed=embed, view=self)


# ─────────────────────────────── sprays ─────────────────────────────── #
class SprayBoardView(discord.ui.View):
    """Persistent '✏️ Update my sprays' button under spray cards (survives restarts)."""

    def __init__(self, cog: "Overwatch"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Update my sprays", emoji="✏️", style=discord.ButtonStyle.success, custom_id=SPRAY_EDIT_ID)
    async def edit(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.guild is None:
            return await interaction.response.send_message("Sprays live on a server's board.", ephemeral=True)
        challenges = await self.cog.config.guild(interaction.guild).challenges()
        if not challenges:
            return await interaction.response.send_message("This server has no spray board yet.", ephemeral=True)
        editor = SprayEditor(self.cog, interaction.guild, interaction.user, challenges)
        await editor.load()
        await interaction.response.send_message(editor.caption(), view=editor, ephemeral=True)


class SprayEditor(discord.ui.View):
    """Private checklist: pick a role, tick the sprays you have. Saves instantly."""

    def __init__(self, cog: "Overwatch", guild: discord.Guild, user, challenges: dict):
        super().__init__(timeout=900)
        self.cog, self.guild, self.user, self.challenges = cog, guild, user, challenges
        self.done: set[str] = set()
        groups: dict[str, list[tuple[str, dict]]] = {}
        for cid, c in challenges.items():
            groups.setdefault(c.get("role", "Other"), []).append((cid, c))
        self.pages: list[tuple[str, list[tuple[str, dict]]]] = []
        for role in ("Tank", "Damage", "Support", "Other"):
            entries = sorted(groups.get(role, []), key=lambda kv: cog.hero_from_reward(kv[1]["reward"]).lower())
            for i in range(0, len(entries), 25):
                self.pages.append((role, entries[i:i + 25]))
        self.page = 0

    async def load(self):
        self.done = set(await self.cog.config.member_from_ids(self.guild.id, self.user.id).done())
        self._build()

    def caption(self) -> str:
        role, entries = self.pages[self.page]
        mine = sum(1 for cid in self.challenges if cid in self.done)
        got = sum(1 for cid, _ in entries if cid in self.done)
        return (
            f"🎨 **Your sprays: {mine}/{len(self.challenges)}**\n"
            f"Tick everything you've unlocked in **{role}** ({got}/{len(entries)}) and it saves straight away. "
            "Switch roles with the buttons."
        )

    def _build(self):
        self.clear_items()
        role, entries = self.pages[self.page]
        options = []
        for cid, c in entries:
            hero = self.cog.hero_from_reward(c["reward"])
            options.append(discord.SelectOption(
                label=hero[:100], value=cid,
                description=self.cog.short_condition(c["name"])[:100],
                default=cid in self.done,
            ))
        select = discord.ui.Select(
            placeholder=f"Tick your {role} sprays…", options=options,
            min_values=0, max_values=len(options), row=0,
        )
        select.callback = self._on_select
        self.add_item(select)
        seen = set()
        for i, (r, _entries) in enumerate(self.pages):
            label = r if r not in seen else f"{r} (more)"
            seen.add(r)
            b = discord.ui.Button(
                label=label, row=1,
                style=discord.ButtonStyle.primary if i == self.page else discord.ButtonStyle.secondary,
            )

            async def cb(interaction: discord.Interaction, i=i):
                self.page = i
                self._build()
                await interaction.response.edit_message(content=self.caption(), view=self)

            b.callback = cb
            self.add_item(b)

    async def _on_select(self, interaction: discord.Interaction):
        _role, entries = self.pages[self.page]
        page_ids = {cid for cid, _ in entries}
        chosen = set(interaction.data.get("values", []))
        added = chosen - self.done
        removed = (page_ids - chosen) & self.done
        async with self.cog.config.member_from_ids(self.guild.id, self.user.id).done() as done:
            for cid in added:
                if cid not in done:
                    done.append(cid)
            for cid in removed:
                if cid in done:
                    done.remove(cid)
            self.done = set(done)
        self._build()
        await interaction.response.edit_message(content=self.caption(), view=self)
        if added:
            await self.cog.spray_shoutout(self.guild, self.user, [self.challenges[c] for c in added], interaction.channel)
