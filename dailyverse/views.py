"""Buttons and menus for verse cards.

* VerseView sits under every public verse card. The public copy is persistent
  (it keeps working after restarts; its verse is looked up by message ID), and
  everything it answers with is private ("only you can see this").
* The private copies carry their own state and edit themselves in place.
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Optional

import discord

from .bible import KJV, Passage
from .sources import ORDER, TRANSLATIONS

if TYPE_CHECKING:
    from .dailyverse import DailyVerse

log = logging.getLogger("red.dailyverse.views")


def theme_options(cog: "DailyVerse") -> list[discord.SelectOption]:
    return [discord.SelectOption(label=t["name"], value=k, emoji=t["emoji"]) for k, t in cog.themes.items()][:25]


def translation_options(current: Optional[str] = None) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(label=TRANSLATIONS[k].name, value=k, description=TRANSLATIONS[k].description[:100],
                             default=(k == current))
        for k in ORDER
    ]


class VerseView(discord.ui.View):
    def __init__(self, cog: "DailyVerse", state: Optional[dict] = None):
        super().__init__(timeout=None if state is None else 900)
        self.cog = cog
        self.state = state
        self.persistent = state is None
        cid = (lambda name: f"dailyverse:v:{name}") if self.persistent else (lambda name: None)

        def add(item, callback):
            item.callback = callback
            self.add_item(item)

        def kw(name):
            c = cid(name)
            return {"custom_id": c} if c else {}

        add(discord.ui.Select(placeholder="✨ Get a verse about…", options=theme_options(cog), row=0, **kw("theme")),
            self.on_theme)
        add(discord.ui.Select(placeholder="📖 Read in another translation…",
                              options=translation_options(None if self.persistent else state.get("t")), row=1,
                              **kw("translation")),
            self.on_translation)
        add(discord.ui.Button(label="Compare translations", emoji="⚖️", style=discord.ButtonStyle.primary, row=2,
                              **kw("compare")), self.on_compare)
        add(discord.ui.Button(label="Read in context", emoji="📜", style=discord.ButtonStyle.secondary, row=2,
                              **kw("context")), self.on_context)
        add(discord.ui.Button(label="Another verse", emoji="🔄", style=discord.ButtonStyle.secondary, row=2,
                              **kw("another")), self.on_another)
        add(discord.ui.Button(label="Show as text", emoji="📋", style=discord.ButtonStyle.secondary, row=3,
                              **kw("text")), self.on_text)
        add(discord.ui.Button(label="Save", emoji="⭐", style=discord.ButtonStyle.secondary, row=3,
                              **kw("save")), self.on_save)
        add(discord.ui.Button(label="Send to me", emoji="✉️", style=discord.ButtonStyle.secondary, row=3,
                              **kw("dm")), self.on_dm)
        if self.persistent:
            add(discord.ui.Button(label="Prayer thread", emoji="🙏", style=discord.ButtonStyle.secondary, row=3,
                                  custom_id="dailyverse:v:thread"), self.on_thread)
        else:
            add(discord.ui.Button(label="Share to channel", emoji="📣", style=discord.ButtonStyle.success, row=3),
                self.on_share)

    # ── helpers ──────────────────────────────────────────────────────
    async def _state(self, interaction: discord.Interaction) -> Optional[dict]:
        if self.state is not None:
            return self.state
        state = await self.cog.load_state(interaction.message.id) if interaction.message else None
        if not state or not state.get("p"):
            await interaction.response.send_message(
                "I don't have this verse on record anymore. Try `verse today` or `verse random`.", ephemeral=True)
            return None
        return state

    async def _show(self, interaction: discord.Interaction, passage: Passage, tr_key: str,
                    theme: Optional[str], header: Optional[str] = None):
        """Persistent card → new private card. Private card → update in place."""
        if self.persistent:
            await interaction.response.defer(ephemeral=True, thinking=True)
        else:
            await interaction.response.defer()
        payload = await self.cog.card_payload(passage, tr_key, guild=interaction.guild, theme=theme, header=header)
        view = VerseView(self.cog, payload.state)
        if self.persistent:
            await interaction.followup.send(payload.content, files=payload.files(), view=view, ephemeral=True)
        else:
            await interaction.edit_original_response(content=payload.content, attachments=payload.files(), view=view)

    # ── callbacks ────────────────────────────────────────────────────
    async def on_theme(self, interaction: discord.Interaction):
        theme = interaction.data.get("values", [None])[0]
        state = self.state or {}
        passage = self.cog.random_passage(theme)
        await self._show(interaction, passage, state.get("t") or await self.cog.default_translation(interaction.guild), theme)

    async def on_translation(self, interaction: discord.Interaction):
        state = await self._state(interaction)
        if state is None:
            return
        tr_key = interaction.data.get("values", [None])[0]
        await self._show(interaction, Passage.from_key(state["p"]), tr_key, state.get("th"), state.get("h"))

    async def on_compare(self, interaction: discord.Interaction):
        state = await self._state(interaction)
        if state is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        content, files = await self.cog.compare_payload(Passage.from_key(state["p"]), interaction.guild)
        await interaction.followup.send(content, files=files, ephemeral=True)

    async def on_context(self, interaction: discord.Interaction):
        state = await self._state(interaction)
        if state is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        p = Passage.from_key(state["p"])
        reader = ReaderView(self.cog, interaction.user.id, p.book, p.c1, state.get("t") or "kjv",
                            highlight={(c, v) for c, v in p.verse_ids()})
        embed = await reader.build(interaction.guild)
        await interaction.followup.send(embed=embed, view=reader, ephemeral=True)

    async def on_another(self, interaction: discord.Interaction):
        state = await self._state(interaction)
        if state is None:
            return
        passage = self.cog.random_passage(state.get("th"), avoid=state.get("p"))
        await self._show(interaction, passage, state.get("t") or "kjv", state.get("th"))

    async def on_text(self, interaction: discord.Interaction):
        state = await self._state(interaction)
        if state is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        pages = await self.cog.text_pages(Passage.from_key(state["p"]), state.get("t") or "kjv")
        for page in pages[:5]:
            await interaction.followup.send(page, ephemeral=True)

    async def on_save(self, interaction: discord.Interaction):
        state = await self._state(interaction)
        if state is None:
            return
        msg = await self.cog.add_favorite(interaction.user, Passage.from_key(state["p"]))
        await interaction.response.send_message(msg, ephemeral=True)

    async def on_dm(self, interaction: discord.Interaction):
        state = await self._state(interaction)
        if state is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        payload = await self.cog.card_payload(Passage.from_key(state["p"]), state.get("t") or "kjv",
                                              guild=interaction.guild, theme=state.get("th"), header=state.get("h"))
        try:
            await interaction.user.send(payload.content, files=payload.files())
            await interaction.followup.send("✉️ Sent to your DMs.", ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send("I couldn't DM you. Check that DMs from server members are allowed.",
                                            ephemeral=True)

    async def on_thread(self, interaction: discord.Interaction):
        state = await self._state(interaction)
        if state is None:
            return
        msg = interaction.message
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Prayer threads only work in regular text channels.", ephemeral=True)
        existing = channel.get_thread(msg.id)
        if existing:
            return await interaction.response.send_message(f"🙏 Join the conversation in {existing.mention}.", ephemeral=True)
        if not channel.permissions_for(interaction.guild.me).create_public_threads:
            return await interaction.response.send_message("I don't have permission to create threads here.", ephemeral=True)
        label = Passage.from_key(state["p"]).label
        try:
            thread = await msg.create_thread(name=f"🙏 {label} · prayer & reflection"[:100], auto_archive_duration=1440)
            await thread.send(f"{interaction.user.mention} opened this thread. Share a prayer request, "
                              f"a reflection on **{label}**, or what it means to you.",
                              allowed_mentions=discord.AllowedMentions.none())
            await interaction.response.send_message(f"🙏 Thread started: {thread.mention}", ephemeral=True)
        except discord.HTTPException:
            existing = channel.get_thread(msg.id)
            text = f"🙏 Join the conversation in {existing.mention}." if existing else "I couldn't start a thread here."
            await interaction.response.send_message(text, ephemeral=True)

    async def on_share(self, interaction: discord.Interaction):
        state = self.state
        channel = interaction.channel
        if interaction.guild is None or channel is None:
            return await interaction.response.send_message("Sharing only works in a server channel.", ephemeral=True)
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.attach_files):
            return await interaction.response.send_message("I can't post images in this channel.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        payload = await self.cog.card_payload(Passage.from_key(state["p"]), state.get("t") or "kjv",
                                              guild=interaction.guild, theme=state.get("th"), header=state.get("h"))
        content = f"-# Shared by {interaction.user.mention}\n{payload.content}"
        sent = await channel.send(content, files=payload.files(), view=self.cog.verse_view,
                                  allowed_mentions=discord.AllowedMentions.none())
        await self.cog.remember(sent, payload.state)
        await interaction.followup.send("📣 Shared!", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        log.exception("verse button failed", exc_info=error)
        text = "Something went wrong with that button. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


class ThemePickerView(discord.ui.View):
    """`[p]verse pick`: a persistent menu anyone can use for a private verse."""

    def __init__(self, cog: "DailyVerse"):
        super().__init__(timeout=None)
        self.cog = cog
        select = discord.ui.Select(placeholder="✨ What do you need today?", options=theme_options(cog),
                                   custom_id="dailyverse:pick:theme")
        select.callback = self.on_pick
        self.add_item(select)

    async def on_pick(self, interaction: discord.Interaction):
        theme = interaction.data.get("values", [None])[0]
        await interaction.response.defer(ephemeral=True, thinking=True)
        payload = await self.cog.card_payload(self.cog.random_passage(theme),
                                              await self.cog.default_translation(interaction.guild),
                                              guild=interaction.guild, theme=theme)
        await interaction.followup.send(payload.content, files=payload.files(),
                                        view=VerseView(self.cog, payload.state), ephemeral=True)


class ReaderView(discord.ui.View):
    """Read a chapter: page through it, jump chapters, switch translations."""

    def __init__(self, cog: "DailyVerse", owner_id: int, book: int, chapter: int, tr_key: str,
                 highlight: Optional[set] = None, page: Optional[int] = None):
        super().__init__(timeout=900)
        self.cog, self.owner_id = cog, owner_id
        self.book, self.chapter, self.tr_key = book, chapter, tr_key
        self.highlight = highlight or set()
        self.page = page
        self.pages = 1
        self.select = discord.ui.Select(placeholder="📖 Translation", options=translation_options(tr_key), row=1)
        self.select.callback = self.on_translation
        self.add_item(self.select)

    async def build(self, guild) -> discord.Embed:
        embed, self.pages, page, used = await self.cog.reader_page(
            self.book, self.chapter, self.tr_key, self.highlight, self.page, guild)
        self.page = page
        self.tr_key = used
        self.select.options = translation_options(used)
        self.prev_ch.disabled = self.book == 0 and self.chapter == 1
        self.next_ch.disabled = self.book == 65 and self.chapter == KJV.chapter_count(65)
        self.prev_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.pages - 1
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Open your own reader with `verse read <book chapter>`.", ephemeral=True)
        return False

    async def _refresh(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = await self.build(interaction.guild)
        await interaction.edit_original_response(embed=embed, view=self)

    def _move_chapter(self, step: int):
        book, ch = self.book, self.chapter + step
        if ch < 1:
            book -= 1
            ch = KJV.chapter_count(book)
        elif ch > KJV.chapter_count(book):
            book += 1
            ch = 1
        self.book, self.chapter, self.page, self.highlight = book, ch, 0, set()

    @discord.ui.button(emoji="⏮️", label="Chapter", style=discord.ButtonStyle.secondary, row=0)
    async def prev_ch(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._move_chapter(-1)
        await self._refresh(interaction)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.primary, row=0)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary, row=0)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._refresh(interaction)

    @discord.ui.button(emoji="⏭️", label="Chapter", style=discord.ButtonStyle.secondary, row=0)
    async def next_ch(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._move_chapter(1)
        await self._refresh(interaction)

    async def on_translation(self, interaction: discord.Interaction):
        self.tr_key = interaction.data.get("values", [self.tr_key])[0]
        await self._refresh(interaction)


def jpg_file(data: bytes, name: str, alt: str = "") -> discord.File:
    return discord.File(io.BytesIO(data), filename=f"{name}.jpg", description=(alt or None) and alt[:1000])
