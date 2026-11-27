"""Daily Verse for Red: a beautiful, inspirational Bible verse every day.

Posts a verse card on a schedule, looks up any verse, compares translations
side by side, and lets people read whole chapters, all with full translation
names and the publishers' copyright notices.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import random
import re
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import tasks
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import humanize_list, pagify
from redbot.core.utils.menus import menu

from . import render
from .bible import BOOKS, KJV, Passage, ReferenceError_, parse_reference
from .sources import (COMPARE_DEFAULT, ORDER, TRANSLATIONS, PassageText, SourceError, Sources,
                      resolve_translation, split_translation)
from .views import ReaderView, ThemePickerView, VerseView, jpg_file

__version__ = "1.0.0"
log = logging.getLogger("red.dailyverse")

DATA = json.loads((Path(__file__).parent / "data" / "verses.json").read_text(encoding="utf-8"))
FAVORITES_LIMIT = 50
STATE_DAYS = 120  # how long buttons under old cards keep working
TZ_SHORTCUTS = {
    "eastern": "America/New_York", "est": "America/New_York", "edt": "America/New_York", "et": "America/New_York",
    "central": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago", "ct": "America/Chicago",
    "mountain": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver", "mt": "America/Denver",
    "arizona": "America/Phoenix",
    "pacific": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles", "pt": "America/Los_Angeles",
    "alaska": "America/Anchorage", "hawaii": "Pacific/Honolulu",
    "utc": "UTC", "gmt": "UTC", "uk": "Europe/London", "london": "Europe/London",
}


def easter(year: int) -> dt.date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(year, month, day)


def holiday_for(day: dt.date) -> Optional[str]:
    if (day.month, day.day) == (1, 1):
        return "new_year"
    if (day.month, day.day) == (12, 25):
        return "christmas"
    if day == easter(day.year):
        return "easter"
    if day.month == 11 and day.weekday() == 3 and 22 <= day.day <= 28:
        return "thanksgiving"  # United States: fourth Thursday of November
    return None


def parse_time(text: str) -> Optional[tuple[int, int]]:
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*", text.lower())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap:
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if ap.startswith("p") else 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h, mi


def fmt_time(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.time(h, m).strftime("%I:%M %p").lstrip("0")


@dataclass
class Payload:
    content: str
    images: list[bytes]
    alt: str
    state: dict = field(default_factory=dict)

    def files(self) -> list[discord.File]:
        return [jpg_file(img, f"verse_{i + 1}", self.alt) for i, img in enumerate(self.images)]


class DailyVerse(commands.Cog):
    """A beautiful, inspirational Bible verse every day, plus lookups and side-by-side translations."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=2026112701, force_registration=True)
        self.config.register_guild(
            channel=None, time="08:00", timezone="America/New_York", translation="nkjv",
            compare=list(COMPARE_DEFAULT), style="rotate", weekday_themes=True, holidays=True,
            reactions=["🙏", "❤️"], thread=False, role=None, last_date=None, used=[], today={},
            reaction_counts={},
        )
        self.config.register_user(favorites=[], dm_daily=False, last_dm=None)
        self.config.init_custom("VERSEMSG", 1)
        self.config.register_custom("VERSEMSG", p=None, t=None, th=None, h=None, ts=0, g=None, daily=False)
        self.sources = Sources(bot)
        self.themes: dict[str, dict] = DATA["themes"]
        self.holidays: dict[str, dict] = DATA["holidays"]
        self.weekdays: dict[str, dict] = DATA["weekdays"]
        self._theme_passages = {k: [parse_reference(r) for r in t["verses"]]
                                for k, t in {**self.themes, **self.holidays}.items()}
        self.verse_view: Optional[VerseView] = None
        self.picker_view: Optional[ThemePickerView] = None
        self._daily_messages: set[int] = set()
        self._post_lock = asyncio.Lock()
        self._last_prune = 0.0

    # ── lifecycle ─────────────────────────────────────────────────────
    async def cog_load(self):
        await self.sources.start()
        self.verse_view = VerseView(self)
        self.picker_view = ThemePickerView(self)
        self.bot.add_view(self.verse_view)
        self.bot.add_view(self.picker_view)
        for mid, data in (await self.config.custom("VERSEMSG").all()).items():
            if data.get("daily"):
                self._daily_messages.add(int(mid))
        self.daily_loop.start()

    async def cog_unload(self):
        self.daily_loop.cancel()
        for v in (self.verse_view, self.picker_view):
            if v:
                v.stop()
        await self.sources.close()

    async def red_delete_data_for_user(self, *, requester, user_id: int):
        await self.config.user_from_id(user_id).clear()

    def format_help_for_context(self, ctx):
        return f"{super().format_help_for_context(ctx)}\n\nCog Version: {__version__}"

    # ── shared helpers (also used by the buttons) ─────────────────────
    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    async def _local_today(self, guild: Optional[discord.Guild]) -> dt.date:
        if guild is None:
            return dt.date.today()
        try:
            tz = ZoneInfo(await self.config.guild(guild).timezone())
        except (ZoneInfoNotFoundError, ValueError):
            tz = ZoneInfo("UTC")
        return dt.datetime.now(tz).date()

    async def _style(self, guild: Optional[discord.Guild]) -> render.Style:
        setting = await self.config.guild(guild).style() if guild else "rotate"
        return render.style_for(await self._local_today(guild), setting)

    async def default_translation(self, guild: Optional[discord.Guild]) -> str:
        if guild is None:
            return "nkjv"
        return await self.config.guild(guild).translation()

    async def fetch(self, passage: Passage, tr_key: str) -> tuple[PassageText, Optional[str]]:
        """The passage in the requested translation, or the King James Version with a note why not."""
        try:
            return await self.sources.get(passage, tr_key), None
        except SourceError as exc:
            if tr_key == "kjv":
                raise
            note = f"⚠️ {exc} Showing the **King James Version** instead."
            return await self.sources.get(passage, "kjv"), note

    def random_passage(self, theme: Optional[str] = None, avoid: Optional[str] = None) -> Passage:
        if theme not in self._theme_passages:
            theme = random.choice(list(self.themes))
        pool = [p for p in self._theme_passages[theme] if p.key != avoid] or self._theme_passages[theme]
        return random.choice(pool)

    def theme_name(self, theme: Optional[str]) -> Optional[str]:
        t = self.themes.get(theme) or self.holidays.get(theme)
        return t["name"] if t else None

    def _resolve_theme(self, text: str) -> Optional[str]:
        t = re.sub(r"[^a-z]", "", (text or "").lower())
        if not t:
            return None
        for k, v in {**self.themes, **self.holidays}.items():
            name = re.sub(r"[^a-z]", "", v["name"].lower())
            if t in (k, name) or name.startswith(t) or k.startswith(t) or (len(t) >= 4 and t in name):
                return k
        return None

    async def card_payload(self, passage: Passage, tr_key: str, *, guild: Optional[discord.Guild],
                           theme: Optional[str] = None, header: Optional[str] = None,
                           title: Optional[str] = None) -> Payload:
        text, note = await self.fetch(passage, tr_key)
        tr = text.translation
        header = header or self.theme_name(theme) or "Scripture"
        style = await self._style(guild)
        images = await self._run(render.render_passage, [t for _, _, t in text.verses], passage.label,
                                 tr.card_name, tr.card_notice, header, style)
        lines = []
        if title:
            lines.append(title)
        lines.append(f"📖 **{passage.label}** · {tr.name}")
        if note:
            lines.append(note)
        if text.notice:
            lines.append(f"-# {text.notice}")
        alt = f"{passage.label} ({tr.name}): {text.text}"
        state = {"p": passage.key, "t": tr.key, "th": theme, "h": header}
        return Payload("\n".join(lines), images, alt, state)

    async def compare_payload(self, passage: Passage, guild: Optional[discord.Guild]) -> tuple[str, list[discord.File]]:
        keys = await self.config.guild(guild).compare() if guild else list(COMPARE_DEFAULT)
        style = await self._style(guild)
        files, names, notices, problems = [], [], [], []
        for key in keys:
            try:
                text = await self.sources.get(passage, key)
            except SourceError as exc:
                problems.append(f"⚠️ {exc}")
                continue
            tr = text.translation
            images = await self._run(render.render_passage, [t for _, _, t in text.verses], passage.label,
                                     tr.card_name, tr.card_notice, tr.name, style)
            for i, img in enumerate(images[: max(1, 10 // max(1, len(keys)))]):
                files.append(jpg_file(img, f"{tr.key}_{i + 1}", f"{passage.label} ({tr.name}): {text.text}"))
            names.append(tr.name)
            if text.notice:
                notices.append(f"-# {text.notice}")
        if not files:  # nothing loaded: fall back to the built-in King James Version
            text = await self.sources.get(passage, "kjv")
            images = await self._run(render.render_passage, [t for _, _, t in text.verses], passage.label,
                                     text.translation.card_name, text.translation.card_notice,
                                     text.translation.name, style)
            files = [jpg_file(img, f"kjv_{i + 1}", text.text) for i, img in enumerate(images)]
            names.append(text.translation.name)
        content = "\n".join([f"⚖️ **{passage.label}** · {humanize_list(names)}", *problems, *notices])
        return content[:2000], files[:10]

    async def text_pages(self, passage: Passage, tr_key: str) -> list[str]:
        text, note = await self.fetch(passage, tr_key)
        lines = [f"📖 **{passage.label}** · {text.translation.name}"]
        if note:
            lines.append(note)
        multi = len(text.verses) > 1
        lines += [f"**{v}** {t}" if multi else f"> {t}" for _, v, t in text.verses]
        if text.notice:
            lines.append(f"-# {text.notice}")
        return list(pagify("\n".join(lines), page_length=1900))

    async def reader_page(self, book: int, chapter: int, tr_key: str, highlight: set, page: Optional[int],
                          guild: Optional[discord.Guild]):
        passage = Passage.chapter(book, chapter)
        text, note = await self.fetch(passage, tr_key)
        lines = []
        for c, v, t in text.verses:
            lines.append(f"**{v}** __{t}__" if (c, v) in highlight else f"**{v}** {t}")
        pages, cur = [], ""
        for line in lines:
            if cur and len(cur) + len(line) + 1 > 3600:
                pages.append(cur)
                cur = line
            else:
                cur = f"{cur}\n{line}" if cur else line
        pages.append(cur or "*(empty)*")
        if page is None:  # open on the page with the highlighted verse
            first = min(highlight)[1] if highlight else 1
            page = next((i for i, p in enumerate(pages) if f"**{first}** __" in p), 0)
        page = max(0, min(page, len(pages) - 1))
        style = await self._style(guild)
        name = "Psalm" if book == 18 else BOOKS[book][0]
        embed = discord.Embed(title=f"📜 {name} {chapter}", description=pages[page], color=style.color)
        embed.set_author(name=text.translation.name)
        footer = f"Page {page + 1} of {len(pages)}"
        if text.notice:
            footer += f" · {text.notice.replace('<', '').replace('>', '')}"
        embed.set_footer(text=footer[:2048])
        if note:
            embed.add_field(name="Note", value=note[:1024], inline=False)
        return embed, len(pages), page, text.translation.key

    async def add_favorite(self, user: discord.abc.User, passage: Passage) -> str:
        async with self.config.user(user).favorites() as favs:
            if passage.key in favs:
                return f"⭐ **{passage.label}** is already in your favorites."
            if len(favs) >= FAVORITES_LIMIT:
                return f"You have {FAVORITES_LIMIT} favorites already. Remove one with `verse unsave`."
            favs.append(passage.key)
            return f"⭐ Saved **{passage.label}**. See them all with `verse favorites`."

    async def remember(self, message: discord.Message, state: dict, daily: bool = False):
        await self.config.custom("VERSEMSG", str(message.id)).set({
            **state, "ts": int(_time.time()), "g": message.guild.id if message.guild else None, "daily": daily})
        if daily:
            self._daily_messages.add(message.id)

    async def load_state(self, message_id: int) -> Optional[dict]:
        data = await self.config.custom("VERSEMSG", str(message_id)).all()
        return data if data.get("p") else None

    async def _prune_states(self):
        if _time.time() - self._last_prune < 86400:
            return
        self._last_prune = _time.time()
        cutoff = _time.time() - STATE_DAYS * 86400
        group = self.config.custom("VERSEMSG")
        for mid, data in (await group.all()).items():
            if data.get("ts", 0) < cutoff:
                await group.clear_raw(mid)
                self._daily_messages.discard(int(mid))

    async def _send_card(self, ctx: commands.Context, payload: Payload) -> discord.Message:
        msg = await ctx.send(payload.content, files=payload.files(), view=self.verse_view,
                             allowed_mentions=discord.AllowedMentions.none())
        await self.remember(msg, payload.state)
        return msg

    # ── the daily verse ───────────────────────────────────────────────
    async def _choose_daily(self, guild: discord.Guild, day: dt.date) -> tuple[Passage, str, str, str]:
        """(passage, theme key, header, emoji) for a guild's verse of the day."""
        conf = await self.config.guild(guild).all()
        holiday = holiday_for(day) if conf["holidays"] else None
        if holiday:
            theme, header = holiday, self.holidays[holiday]["name"]
        elif conf["weekday_themes"]:
            wd = self.weekdays[str(day.weekday())]
            theme = wd["theme"]
            header = f"{wd['label']} · {self.themes[theme]['name']}"
        else:
            theme = random.choice(list(self.themes))
            header = f"Daily Verse · {self.themes[theme]['name']}"
        used = set(conf["used"])
        pool = self._theme_passages[theme]
        fresh = [p for p in pool if p.key not in used]
        if not fresh:  # every verse in this theme has been used: start the theme over
            async with self.config.guild(guild).used() as u:
                u[:] = [k for k in u if k not in {p.key for p in pool}]
            fresh = pool
        passage = random.choice(fresh)
        async with self.config.guild(guild).used() as u:
            u.append(passage.key)
            del u[:-1500]
        emoji = (self.holidays.get(theme) or self.themes.get(theme))["emoji"]
        return passage, theme, header, emoji

    async def post_daily(self, guild: discord.Guild, channel: discord.abc.Messageable) -> Optional[discord.Message]:
        conf = await self.config.guild(guild).all()
        day = await self._local_today(guild)
        passage, theme, header, emoji = await self._choose_daily(guild, day)
        payload = await self.card_payload(passage, conf["translation"], guild=guild, theme=theme, header=header,
                                          title=f"## {emoji} {header}")
        role = guild.get_role(conf["role"]) if conf["role"] else None
        content = f"{role.mention}\n{payload.content}" if role else payload.content
        msg = await channel.send(content[:2000], files=payload.files(), view=self.verse_view,
                                 allowed_mentions=discord.AllowedMentions(roles=[role] if role else False,
                                                                          users=False, everyone=False))
        await self.remember(msg, payload.state, daily=True)
        await self.config.guild(guild).today.set({**payload.state, "date": day.isoformat(), "mid": msg.id,
                                                  "cid": msg.channel.id})
        perms = channel.permissions_for(guild.me)
        if perms.add_reactions:
            for emo in conf["reactions"]:
                try:
                    await msg.add_reaction(emo)
                except discord.HTTPException:
                    pass
        if conf["thread"] and isinstance(channel, discord.TextChannel) and perms.create_public_threads:
            try:
                await msg.create_thread(name=f"🙏 {passage.label} · prayer & reflection"[:100],
                                        auto_archive_duration=1440)
            except discord.HTTPException:
                log.info("couldn't create daily thread in %s", guild.id)
        await self._send_dms(guild, payload)
        return msg

    async def _send_dms(self, guild: discord.Guild, payload: Payload):
        today = dt.date.today().isoformat()
        for uid, data in (await self.config.all_users()).items():
            if not data.get("dm_daily") or data.get("last_dm") == today:
                continue
            member = guild.get_member(uid)
            if member is None:
                continue
            await self.config.user_from_id(uid).last_dm.set(today)
            try:
                await member.send(f"-# Your daily verse from **{guild.name}**\n{payload.content}"[:2000],
                                  files=payload.files())
            except discord.HTTPException:
                pass
            await asyncio.sleep(1)

    @tasks.loop(minutes=1)
    async def daily_loop(self):
        await self.bot.wait_until_red_ready()
        async with self._post_lock:
            for gid, conf in (await self.config.all_guilds()).items():
                if not conf.get("channel"):
                    continue
                guild = self.bot.get_guild(gid)
                if guild is None:
                    continue
                try:
                    tz = ZoneInfo(conf.get("timezone") or "UTC")
                except (ZoneInfoNotFoundError, ValueError):
                    tz = ZoneInfo("UTC")
                now = dt.datetime.now(tz)
                today = now.date().isoformat()
                if conf.get("last_date") == today:
                    continue
                h, m = (int(x) for x in (conf.get("time") or "08:00").split(":"))
                if now.time() < dt.time(h, m):
                    continue
                channel = guild.get_channel(conf["channel"])
                await self.config.guild(guild).last_date.set(today)  # set first: never double-post
                if channel is None:
                    continue
                perms = channel.permissions_for(guild.me)
                if not (perms.send_messages and perms.attach_files):
                    log.info("missing permissions for daily verse in %s", gid)
                    continue
                try:
                    await self.post_daily(guild, channel)
                except Exception:
                    log.exception("daily verse failed in %s", gid)
            try:
                await self._prune_states()
            except Exception:
                log.exception("pruning verse states failed")

    @daily_loop.before_loop
    async def _before_daily(self):
        await self.bot.wait_until_red_ready()

    # ── reaction tally for "most loved" ───────────────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._tally(payload, 1)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._tally(payload, -1)

    async def _tally(self, payload: discord.RawReactionActionEvent, step: int):
        if payload.message_id not in self._daily_messages or payload.guild_id is None:
            return
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        state = await self.load_state(payload.message_id)
        if not state:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        async with self.config.guild(guild).reaction_counts() as counts:
            counts[state["p"]] = max(0, counts.get(state["p"], 0) + step)

    # ═════════════════════════════════════════════════════════════════
    # Commands for everyone
    # ═════════════════════════════════════════════════════════════════
    @commands.group(name="verse", aliases=["bible"], invoke_without_command=True)
    @commands.bot_has_permissions(attach_files=True)
    @commands.cooldown(3, 15, commands.BucketType.user)
    async def verse(self, ctx: commands.Context, *, reference: Optional[str] = None):
        """Look up a verse as a card: `[p]verse John 3:16` or `[p]verse Psalm 23 niv`.

        Add a translation at the end to choose one: New King James Version, New International Version,
        New American Standard Bible (1995) or King James Version (short names like `nkjv` work too).
        With nothing after it, shows today's verse.
        """
        if not reference:
            return await ctx.invoke(self.verse_today)
        ref, tr_key = split_translation(reference)
        try:
            passage = parse_reference(ref)
        except ReferenceError_ as exc:
            return await ctx.send(str(exc))
        if len(passage) > 60:
            return await ctx.send(f"That's a long passage ({len(passage)} verses). Use `{ctx.clean_prefix}verse read {ref}` "
                                  "to read it page by page.")
        async with ctx.typing():
            payload = await self.card_payload(passage, tr_key or await self.default_translation(ctx.guild), guild=ctx.guild)
        await self._send_card(ctx, payload)

    @verse.command(name="compare", aliases=["all", "versions-of"])
    @commands.bot_has_permissions(attach_files=True)
    @commands.cooldown(2, 20, commands.BucketType.user)
    async def verse_compare(self, ctx: commands.Context, *, reference: str):
        """See a verse in every translation side by side: `[p]verse compare John 3:16`."""
        ref, _ = split_translation(reference)
        try:
            passage = parse_reference(ref)
        except ReferenceError_ as exc:
            return await ctx.send(str(exc))
        if len(passage) > 12:
            return await ctx.send("Comparisons work best for up to 12 verses. Try a shorter range.")
        async with ctx.typing():
            content, files = await self.compare_payload(passage, ctx.guild)
        await ctx.send(content, files=files)

    @verse.command(name="read", aliases=["chapter", "context"])
    @commands.bot_has_permissions(embed_links=True)
    async def verse_read(self, ctx: commands.Context, *, reference: str):
        """Read a whole chapter page by page: `[p]verse read John 3` or `[p]verse read Romans 8:28 niv`."""
        ref, tr_key = split_translation(reference)
        try:
            passage = parse_reference(ref)
        except ReferenceError_ as exc:
            return await ctx.send(str(exc))
        highlight = set() if passage.whole_chapters else set(passage.verse_ids())
        reader = ReaderView(self, ctx.author.id, passage.book, passage.c1,
                            tr_key or await self.default_translation(ctx.guild), highlight=highlight)
        async with ctx.typing():
            embed = await reader.build(ctx.guild)
        await ctx.send(embed=embed, view=reader)

    @verse.command(name="today", aliases=["daily"])
    @commands.bot_has_permissions(attach_files=True)
    async def verse_today(self, ctx: commands.Context):
        """Today's verse for this server (or a random one if none has posted yet)."""
        today = await self.config.guild(ctx.guild).today() if ctx.guild else {}
        day = (await self._local_today(ctx.guild)).isoformat()
        async with ctx.typing():
            if today.get("p") and today.get("date") == day:
                payload = await self.card_payload(Passage.from_key(today["p"]), today.get("t") or "kjv",
                                                  guild=ctx.guild, theme=today.get("th"), header=today.get("h"))
                channel = ctx.guild.get_channel(today.get("cid") or 0)
                if channel and today.get("mid"):
                    payload.content += f"\n-# [Today's post]({channel.get_partial_message(today['mid']).jump_url})"
            else:
                payload = await self.card_payload(self.random_passage(), await self.default_translation(ctx.guild),
                                                  guild=ctx.guild)
        await self._send_card(ctx, payload)

    @verse.command(name="random", aliases=["about", "theme"])
    @commands.bot_has_permissions(attach_files=True)
    @commands.cooldown(3, 15, commands.BucketType.user)
    async def verse_random(self, ctx: commands.Context, *, theme: Optional[str] = None):
        """A random inspirational verse, optionally about a theme: `[p]verse random hope`.

        Themes: Hope, Strength, Peace, Love, Courage, Comfort, Faith, Gratitude, Wisdom,
        Forgiveness, Anxiety & Worry, Hard Times.
        """
        key = None
        if theme:
            key = self._resolve_theme(theme)
            if key is None:
                names = ", ".join(t["name"] for t in self.themes.values())
                return await ctx.send(f"I don't have a theme called **{theme}**. Try one of: {names}.")
        async with ctx.typing():
            payload = await self.card_payload(self.random_passage(key), await self.default_translation(ctx.guild),
                                              guild=ctx.guild, theme=key)
        await self._send_card(ctx, payload)

    @verse.command(name="pick", aliases=["menu", "themes"])
    async def verse_pick(self, ctx: commands.Context):
        """Post a menu anyone can use to get a private verse for how they're feeling."""
        embed = discord.Embed(
            title="✨ What do you need today?",
            description="Pick a theme below and I'll show you a verse. Only you will see it, "
                        "and you can share it to the channel if you'd like.",
            color=(await self._style(ctx.guild)).color,
        )
        await ctx.send(embed=embed, view=self.picker_view)

    @verse.command(name="search", aliases=["find"])
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def verse_search(self, ctx: commands.Context, *, words: str):
        """Find verses containing words, or an exact phrase in quotes: `[p]verse search "be strong"`.

        Search uses the built-in King James Version, since the other translations can't be stored.
        """
        results = await self._run(KJV.search, words)
        if not results:
            return await ctx.send(f"No verses in the King James Version contain **{words}**.")
        lines = []
        for b, c, v, text in results:
            name = "Psalm" if b == 18 else BOOKS[b][0]
            short = text if len(text) <= 180 else text[:177].rsplit(" ", 1)[0] + "…"
            lines.append(f"**{name} {c}:{v}**: {short}")
        pages, chunk = [], []
        for i, line in enumerate(lines, 1):
            chunk.append(line)
            if len(chunk) == 10 or i == len(lines):
                embed = discord.Embed(title=f"🔎 “{words}” in the King James Version",
                                      description="\n".join(chunk), color=(await self._style(ctx.guild)).color)
                embed.set_footer(text=f"{len(results)}{'+' if len(results) >= 200 else ''} verses · "
                                      f"page {len(pages) + 1} of {(len(lines) + 9) // 10} · "
                                      f"open one with {ctx.clean_prefix}verse <reference>")
                pages.append(embed)
                chunk = []
        await menu(ctx, pages)

    @verse.command(name="versions", aliases=["translations"])
    async def verse_versions(self, ctx: commands.Context):
        """The translations this bot can show."""
        async with ctx.typing():
            status = await self.sources.status()
        default = await self.default_translation(ctx.guild)
        lines = []
        for k in ORDER:
            tr = TRANSLATIONS[k]
            ok = status.get(k, "").startswith("ok")
            mark = "✅" if ok else "⚠️"
            star = " ⭐ *server default*" if k == default else ""
            lines.append(f"{mark} **{tr.name}**{star}\n{tr.description}" + ("" if ok else f"\n-# {status.get(k)}"))
        embed = discord.Embed(title="📚 Translations", description="\n\n".join(lines),
                              color=(await self._style(ctx.guild)).color)
        embed.set_footer(text="Add a translation to any lookup, e.g. verse John 3:16 new international version")
        await ctx.send(embed=embed)

    @verse.command(name="favorites", aliases=["favs", "saved"])
    async def verse_favorites(self, ctx: commands.Context):
        """Verses you've saved with the ⭐ button."""
        favs = await self.config.user(ctx.author).favorites()
        if not favs:
            return await ctx.send("You haven't saved any verses yet. Tap **⭐ Save** under any verse card.")
        lines = []
        for i, key in enumerate(favs, 1):
            p = Passage.from_key(key)
            preview = KJV.verse(p.book, p.c1, p.v1)
            preview = preview if len(preview) <= 90 else preview[:87].rsplit(" ", 1)[0] + "…"
            lines.append(f"`{i:>2}.` **{p.label}**: {preview}")
        for page in pagify("⭐ **Your saved verses**\n" + "\n".join(lines) +
                           f"\n-# Open one with `{ctx.clean_prefix}verse <reference>` · remove with "
                           f"`{ctx.clean_prefix}verse unsave <number>`", page_length=1900):
            await ctx.send(page)

    @verse.command(name="unsave", aliases=["unfavorite", "remove"])
    async def verse_unsave(self, ctx: commands.Context, *, which: str):
        """Remove a saved verse by its number or reference."""
        async with self.config.user(ctx.author).favorites() as favs:
            if which.isdigit() and 1 <= int(which) <= len(favs):
                key = favs.pop(int(which) - 1)
            else:
                try:
                    key = parse_reference(which).key
                except ReferenceError_ as exc:
                    return await ctx.send(str(exc))
                if key not in favs:
                    return await ctx.send("That verse isn't in your favorites.")
                favs.remove(key)
        await ctx.send(f"Removed **{Passage.from_key(key).label}** from your favorites.")

    @verse.command(name="top", aliases=["loved", "popular"])
    @commands.guild_only()
    async def verse_top(self, ctx: commands.Context):
        """The daily verses this server has reacted to most."""
        counts = await self.config.guild(ctx.guild).reaction_counts()
        ranked = sorted(((n, k) for k, n in counts.items() if n > 0), reverse=True)[:10]
        if not ranked:
            return await ctx.send("No reactions on daily verses yet.")
        medals = ["🥇", "🥈", "🥉"] + [f"`{i}.`" for i in range(4, 11)]
        lines = [f"{medals[i]} **{Passage.from_key(k).label}** · {n} reaction{'s' if n != 1 else ''}"
                 for i, (n, k) in enumerate(ranked)]
        embed = discord.Embed(title="❤️ Most-loved daily verses", description="\n".join(lines),
                              color=(await self._style(ctx.guild)).color)
        await ctx.send(embed=embed)

    @verse.command(name="subscribe", aliases=["dm"])
    async def verse_subscribe(self, ctx: commands.Context):
        """Also get the daily verse in your DMs."""
        await self.config.user(ctx.author).dm_daily.set(True)
        await ctx.send("✉️ You'll get the daily verse in your DMs. Stop any time with "
                       f"`{ctx.clean_prefix}verse unsubscribe`.")

    @verse.command(name="unsubscribe", aliases=["undm"])
    async def verse_unsubscribe(self, ctx: commands.Context):
        """Stop getting the daily verse in your DMs."""
        await self.config.user(ctx.author).dm_daily.set(False)
        await ctx.send("Okay, no more daily verse DMs.")

    # ═════════════════════════════════════════════════════════════════
    # Settings
    # ═════════════════════════════════════════════════════════════════
    @commands.group(name="verseset")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def verseset(self, ctx: commands.Context):
        """Set up the daily verse for this server."""

    @verseset.command(name="channel")
    async def vs_channel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Channel for the daily verse. Leave empty to turn the daily verse off."""
        if channel is None:
            await self.config.guild(ctx.guild).channel.set(None)
            return await ctx.send("Daily verse turned off.")
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.attach_files):
            return await ctx.send(f"I need **Send Messages** and **Attach Files** in {channel.mention}.")
        conf = self.config.guild(ctx.guild)
        await conf.channel.set(channel.id)
        tz_name = await conf.timezone()
        t = await conf.time()
        now = dt.datetime.now(ZoneInfo(tz_name))
        h, m = (int(x) for x in t.split(":"))
        if now.time() >= dt.time(h, m):  # don't surprise-post right away; start tomorrow
            await conf.last_date.set(now.date().isoformat())
            when = "starting tomorrow"
        else:
            when = "starting today"
        await ctx.send(f"📖 The daily verse will post in {channel.mention} at **{fmt_time(t)}** ({tz_name}), {when}.\n"
                       f"Change the time with `{ctx.clean_prefix}verseset time 7:30am` and the time zone with "
                       f"`{ctx.clean_prefix}verseset timezone America/Chicago`. "
                       f"Post one now with `{ctx.clean_prefix}verseset post`.")

    @verseset.command(name="time")
    async def vs_time(self, ctx: commands.Context, *, time: str):
        """What time to post, like `8:00`, `7:30am` or `18:00`."""
        parsed = parse_time(time)
        if not parsed:
            return await ctx.send("I couldn't read that time. Try `8:00`, `7:30am` or `18:00`.")
        hhmm = f"{parsed[0]:02d}:{parsed[1]:02d}"
        await self.config.guild(ctx.guild).time.set(hhmm)
        tz_name = await self.config.guild(ctx.guild).timezone()
        await ctx.send(f"⏰ Daily verse time set to **{fmt_time(hhmm)}** ({tz_name}).")

    @verseset.command(name="timezone", aliases=["tz"])
    async def vs_timezone(self, ctx: commands.Context, *, timezone: str):
        """Time zone, like `America/New_York`, `America/Chicago`, `Europe/London` or `eastern`.

        Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
        """
        name = TZ_SHORTCUTS.get(timezone.strip().lower(), timezone.strip())
        try:
            tz = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return await ctx.send(f"I don't know the time zone **{timezone}**. Try something like `America/New_York`.")
        await self.config.guild(ctx.guild).timezone.set(name)
        now = dt.datetime.now(tz).strftime("%I:%M %p").lstrip("0")
        await ctx.send(f"🌍 Time zone set to **{name}** (it's {now} there now).")

    @verseset.command(name="translation", aliases=["version"])
    async def vs_translation(self, ctx: commands.Context, *, translation: str):
        """Default translation for the daily verse and lookups."""
        key = resolve_translation(translation)
        if not key:
            names = humanize_list([TRANSLATIONS[k].name for k in ORDER], style="or")
            return await ctx.send(f"Pick one of: {names}.")
        await self.config.guild(ctx.guild).translation.set(key)
        await ctx.send(f"📖 Default translation set to the **{TRANSLATIONS[key].name}**.")

    @verseset.command(name="compare")
    async def vs_compare(self, ctx: commands.Context, *, translations: str):
        """Which translations "Compare translations" shows, separated by commas.

        Example: `[p]verseset compare new king james, new international, new american standard`
        """
        keys = []
        for part in re.split(r"[,;/|]+", translations):
            key = resolve_translation(part)
            if not key:
                return await ctx.send(f"I don't know a translation called **{part.strip()}**.")
            if key not in keys:
                keys.append(key)
        await self.config.guild(ctx.guild).compare.set(keys)
        await ctx.send("⚖️ Comparisons will show: " + humanize_list([f"**{TRANSLATIONS[k].name}**" for k in keys]))

    @verseset.command(name="style")
    async def vs_style(self, ctx: commands.Context, style: str = "rotate"):
        """Card style: `rotate` (a different one each day) or one of the styles by name."""
        style = style.lower()
        if style != "rotate" and style not in render.STYLES:
            return await ctx.send("Styles: `rotate`, " + ", ".join(f"`{k}`" for k in render.STYLES))
        await self.config.guild(ctx.guild).style.set(style)
        today = (await self._style(ctx.guild)).name
        msg = f"🎨 Styles rotate daily (today is **{today}**)." if style == "rotate" else f"🎨 Every card will use **{today}**."
        await ctx.send(msg)

    @verseset.command(name="preview", aliases=["styles"])
    @commands.bot_has_permissions(attach_files=True)
    async def vs_preview(self, ctx: commands.Context):
        """Preview every card style."""
        passage = parse_reference("Isaiah 41:10")
        text = KJV.verse(passage.book, passage.c1, passage.v1)
        async with ctx.typing():
            files = []
            for key, st in render.STYLES.items():
                img = await self._run(render.render_card, text, passage.label, "King James Version",
                                      "Public Domain", f"{st.name} style", st)
                files.append(jpg_file(img, key, f"{st.name} style"))
        await ctx.send("🎨 **Card styles:** " + ", ".join(f"`{k}`" for k in render.STYLES), files=files)

    @verseset.command(name="weekdays")
    async def vs_weekdays(self, ctx: commands.Context, on: bool):
        """Theme the daily verse by weekday (Motivation Monday, Faith Friday, …) or pick themes at random."""
        await self.config.guild(ctx.guild).weekday_themes.set(on)
        if on:
            lines = [f"**{dt.date(2024, 1, 1 + int(d)).strftime('%A')}**: {w['label']} · {self.themes[w['theme']]['name']}"
                     for d, w in sorted(self.weekdays.items())]
            await ctx.send("📅 Weekday themes on:\n" + "\n".join(lines))
        else:
            await ctx.send("🎲 Each day will pick a random theme.")

    @verseset.command(name="holidays")
    async def vs_holidays(self, ctx: commands.Context, on: bool):
        """Special verses on New Year's Day, Easter, Thanksgiving and Christmas."""
        await self.config.guild(ctx.guild).holidays.set(on)
        await ctx.send("🎄 Holiday verses on." if on else "Holiday verses off.")

    @verseset.command(name="reactions")
    async def vs_reactions(self, ctx: commands.Context, *emojis: str):
        """Reactions added to each daily verse, like `🙏 ❤️`. Leave empty for none."""
        await self.config.guild(ctx.guild).reactions.set(list(emojis)[:5])
        await ctx.send(f"Reactions set to {' '.join(emojis)}." if emojis else "No reactions will be added.")

    @verseset.command(name="thread")
    async def vs_thread(self, ctx: commands.Context, on: bool):
        """Open a prayer & reflection thread under every daily verse automatically."""
        await self.config.guild(ctx.guild).thread.set(on)
        await ctx.send("🙏 A thread will open under each daily verse." if on else "No automatic threads.")

    @verseset.command(name="role", aliases=["ping"])
    async def vs_role(self, ctx: commands.Context, role: Optional[discord.Role] = None):
        """A role to ping with each daily verse. Leave empty for no ping."""
        await self.config.guild(ctx.guild).role.set(role.id if role else None)
        await ctx.send(f"🔔 {role.mention} will be pinged with each daily verse." if role else "No role will be pinged.",
                       allowed_mentions=discord.AllowedMentions.none())

    @verseset.command(name="post", aliases=["now", "test"])
    @commands.bot_has_permissions(attach_files=True)
    async def vs_post(self, ctx: commands.Context):
        """Post a daily verse right now (in the daily channel, or here if none is set)."""
        channel_id = await self.config.guild(ctx.guild).channel()
        channel = ctx.guild.get_channel(channel_id) if channel_id else ctx.channel
        async with ctx.typing():
            msg = await self.post_daily(ctx.guild, channel or ctx.channel)
        if channel and channel != ctx.channel and msg:
            await ctx.send(f"Posted in {channel.mention}: {msg.jump_url}")

    @verseset.command(name="settings", aliases=["show"])
    async def vs_settings(self, ctx: commands.Context):
        """Show this server's daily verse settings."""
        c = await self.config.guild(ctx.guild).all()
        ch = ctx.guild.get_channel(c["channel"]) if c["channel"] else None
        role = ctx.guild.get_role(c["role"]) if c["role"] else None
        style = "rotate (" + (await self._style(ctx.guild)).name + " today)" if c["style"] == "rotate" else c["style"]
        lines = [
            ("Channel", ch.mention if ch else "off"),
            ("Time", f"{fmt_time(c['time'])} · {c['timezone']}"),
            ("Translation", TRANSLATIONS[c["translation"]].name),
            ("Compare", humanize_list([TRANSLATIONS[k].name for k in c["compare"]])),
            ("Style", style),
            ("Weekday themes", "on" if c["weekday_themes"] else "off"),
            ("Holiday verses", "on" if c["holidays"] else "off"),
            ("Reactions", " ".join(c["reactions"]) or "none"),
            ("Auto thread", "on" if c["thread"] else "off"),
            ("Role ping", role.mention if role else "none"),
            ("Verses used", f"{len(c['used'])} of {sum(len(t['verses']) for t in self.themes.values())}"),
        ]
        embed = discord.Embed(title="📖 Daily verse settings", color=(await self._style(ctx.guild)).color,
                              description="\n".join(f"**{k}:** {v}" for k, v in lines))
        await ctx.send(embed=embed)

    @verseset.command(name="apistatus", aliases=["api"])
    async def vs_apistatus(self, ctx: commands.Context):
        """Check which translations the API.Bible key can load."""
        async with ctx.typing():
            status = await self.sources.status()
        lines = [f"{'✅' if s.startswith('ok') else '⚠️'} **{TRANSLATIONS[k].name}**: {s}" for k, s in status.items()]
        if not await self.sources.has_key():
            lines.append(f"\nSet the key (bot owner, ideally in DMs): `{ctx.clean_prefix}set api apibible api_key,YOUR_KEY`")
        await ctx.send("\n".join(lines))
