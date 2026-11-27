"""Where each translation's text comes from.

* New King James Version, New International Version and New American Standard
  Bible (1995): API.Bible, using the bot owner's key (the plan must include them).
* King James Version: bundled with the cog (public domain). Always available, so
  it is also the backup when API.Bible can't be reached.

Copyrighted text is only cached briefly in memory and never written to disk.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional

import aiohttp

from .bible import KJV, Passage

log = logging.getLogger("red.dailyverse.sources")

APIBIBLE_BASES = ("https://rest.api.bible/v1", "https://api.scripture.api.bible/v1")


@dataclass(frozen=True)
class Translation:
    key: str
    name: str            # always shown in full
    card_name: str       # name as printed on the card (with the publisher's ® mark)
    card_notice: str     # short line on the card footer
    description: str
    aliases: tuple[str, ...]
    full_notice: Optional[str] = None      # shown under messages when the publisher requires it
    matcher: Optional[Callable[[dict], int]] = None  # scores API.Bible list entries (higher = better)

    @property
    def bundled(self) -> bool:
        return self.matcher is None


def _abbr(b: dict) -> set[str]:
    return {str(b.get(k) or "").upper().replace(" ", "") for k in ("abbreviation", "abbreviationLocal")}


def _name(b: dict) -> str:
    return f"{b.get('name') or ''} {b.get('nameLocal') or ''} {b.get('description') or ''}".lower()


def _match_nkjv(b: dict) -> int:
    if "NKJV" in _abbr(b):
        return 3
    return 2 if "new king james" in _name(b) else 0


def _match_niv(b: dict) -> int:
    n = _name(b)
    if "reader" in n or "nirv" in n:
        return 0
    if "NIV" in _abbr(b):
        return 3 if "uk" not in n and "anglicised" not in n else 1
    return 2 if "new international version" in n else 0


def _match_nasb(b: dict) -> int:
    a, n = _abbr(b), _name(b)
    is_nasb = bool(a & {"NASB", "NASB95", "NASB1995", "NAS"}) or "new american standard" in n
    if not is_nasb:
        return 0
    if "1995" in n or a & {"NASB95", "NASB1995"}:
        return 3
    return 1 if "2020" in n else 2


TRANSLATIONS: dict[str, Translation] = {
    "nkjv": Translation(
        key="nkjv",
        name="New King James Version",
        card_name="New King James Version®",
        card_notice="© 1982 Thomas Nelson",
        description="1982. The King James Version updated into modern English, keeping its style.",
        aliases=("nkjv", "new king james", "new king james version", "new kjv", "newkjv"),
        full_notice="Scripture taken from the New King James Version®. Copyright © 1982 by Thomas Nelson. "
                    "Used by permission. All rights reserved.",
        matcher=_match_nkjv,
    ),
    "niv": Translation(
        key="niv",
        name="New International Version",
        card_name="New International Version®",
        card_notice="© 2011 Biblica, Inc.",
        description="2011. The best-selling modern English translation, clear and readable.",
        aliases=("niv", "new international", "new international version", "international"),
        full_notice="Scripture quotations taken from The Holy Bible, New International Version® NIV®. "
                    "Copyright © 1973, 1978, 1984, 2011 by Biblica, Inc.™ Used by permission. All rights reserved worldwide.",
        matcher=_match_niv,
    ),
    "nasb": Translation(
        key="nasb",
        name="New American Standard Bible (1995)",
        card_name="New American Standard Bible® 1995",
        card_notice="© The Lockman Foundation",
        description="1995. Known as one of the most word-for-word accurate English translations.",
        aliases=("nasb", "nasb95", "nasb1995", "new american standard", "new american standard bible",
                 "american standard", "new american standard bible 1995", "new american standard bible (1995)"),
        full_notice="Scripture taken from the New American Standard Bible®, Copyright © 1960, 1962, 1963, 1968, "
                    "1971, 1972, 1973, 1975, 1977, 1995 by The Lockman Foundation. Used by permission. <https://www.lockman.org>",
        matcher=_match_nasb,
    ),
    "kjv": Translation(
        key="kjv",
        name="King James Version",
        card_name="King James Version",
        card_notice="Public Domain",
        description="1611, revised 1769. The classic English Bible. Built in, so it always works.",
        aliases=("kjv", "king james", "king james version", "kingjames", "authorized version", "av"),
    ),
}
ORDER = ("nkjv", "niv", "nasb", "kjv")
COMPARE_DEFAULT = ("nkjv", "niv", "nasb")


def resolve_translation(text: str) -> Optional[str]:
    t = " ".join((text or "").lower().replace(".", " ").split())
    if not t:
        return None
    for tr in TRANSLATIONS.values():
        if t in tr.aliases or t == tr.name.lower():
            return tr.key
    return None


def split_translation(text: str) -> tuple[str, Optional[str]]:
    """Peel a translation name off the end of 'John 3:16 new king james'."""
    words = text.split()
    for n in (5, 4, 3, 2, 1):
        if len(words) > n:
            key = resolve_translation(" ".join(words[-n:]))
            if key:
                return " ".join(words[:-n]), key
    return text, None


class SourceError(Exception):
    """A friendly, user-facing reason a translation couldn't be loaded."""


@dataclass
class PassageText:
    passage: Passage
    translation: Translation
    verses: list[tuple[int, int, str]] = field(default_factory=list)
    copyright: Optional[str] = None

    @property
    def text(self) -> str:
        return " ".join(t for _, _, t in self.verses)

    @property
    def notice(self) -> Optional[str]:
        return self.copyright or self.translation.full_notice


_MARK = re.compile(r"\[(?:(\d+):)?(\d+)\]")


def split_numbered(passage: Passage, raw: str) -> list[tuple[int, int, str]]:
    """Map '[16] For God ... [17] For God sent ...' onto the passage's verses."""
    expected = list(passage.verse_ids())
    parts = _MARK.split(raw)
    out: list[tuple[int, int, str]] = []
    pos = 0
    lead = " ".join(parts[0].split())  # parts = [lead, ch, v, text, ch, v, text, ...]
    for i in range(1, len(parts) - 2, 3):
        ch, num, text = parts[i], int(parts[i + 1]), " ".join(parts[i + 2].split())
        j = pos
        while j < len(expected) and not (expected[j][1] == num and (ch is None or expected[j][0] == int(ch))):
            j += 1
        if j == len(expected):  # unexpected number: keep the words with the previous verse
            if out:
                c, v, t = out[-1]
                out[-1] = (c, v, f"{t} {text}".strip())
            continue
        pos = j + 1
        out.append((*expected[j], text))
    if not out and lead:  # a single verse without a marker
        out.append((passage.c1, passage.v1, lead))
    elif lead and out:
        c, v, t = out[0]
        out[0] = (c, v, f"{lead} {t}".strip())
    return [(c, v, t) for c, v, t in out if t]


class Sources:
    MAX_CACHED_VERSES = 400   # copyrighted text kept in memory only
    DISCOVERY_TTL = 6 * 3600

    def __init__(self, bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self._cache: "OrderedDict[tuple[str, str], PassageText]" = OrderedDict()
        self._cached_verses = 0
        self._base: Optional[str] = None
        self._ids: dict[str, str] = {}
        self._ids_at = 0.0
        self._ids_key: Optional[str] = None
        self._fums_session = uuid.uuid4().hex

    async def start(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _tokens(self) -> dict:
        return await self.bot.get_shared_api_tokens("apibible")

    async def has_key(self) -> bool:
        return bool((await self._tokens()).get("api_key"))

    # ── public ────────────────────────────────────────────────────────
    async def get(self, passage: Passage, key: str) -> PassageText:
        tr = TRANSLATIONS[key]
        if tr.bundled:
            return PassageText(passage, tr, [(c, v, KJV.verse(passage.book, c, v)) for c, v in passage.verse_ids()])
        ck = (key, passage.key)
        if ck in self._cache:
            self._cache.move_to_end(ck)
            return self._cache[ck]
        result = await self._apibible(passage, tr)
        self._remember(ck, result)
        return result

    async def status(self) -> dict[str, str]:
        """Per translation: 'ok', or a short reason it isn't available."""
        out = {"kjv": "ok"}
        if not await self.has_key():
            return {**out, **{k: "no API.Bible key set" for k in TRANSLATIONS if k != "kjv"}}
        try:
            ids = await self._discover(force=True)
        except SourceError as exc:
            return {**out, **{k: str(exc) for k in TRANSLATIONS if k != "kjv"}}
        for k, tr in TRANSLATIONS.items():
            if not tr.bundled:
                out[k] = f"ok (Bible ID `{ids[k]}`)" if k in ids else "not included in your API.Bible plan"
        return out

    def _remember(self, ck, result: PassageText):
        n = len(result.verses)
        if n > self.MAX_CACHED_VERSES:
            return
        self._cache[ck] = result
        self._cached_verses += n
        while self._cached_verses > self.MAX_CACHED_VERSES and self._cache:
            _, old = self._cache.popitem(last=False)
            self._cached_verses -= len(old.verses)

    # ── API.Bible ─────────────────────────────────────────────────────
    async def _request(self, path: str, key: str, params: Optional[dict] = None) -> dict:
        """GET from API.Bible, finding the working base URL the first time."""
        await self.start()
        tokens = await self._tokens()
        if tokens.get("base_url"):
            bases = [tokens["base_url"].rstrip("/")]
        elif self._base:
            bases = [self._base]
        else:
            bases = list(APIBIBLE_BASES)
        last: Optional[Exception] = None
        for base in bases:
            try:
                async with self.session.get(f"{base}{path}", params=params, headers={"api-key": key}) as resp:
                    if resp.status == 401:
                        raise SourceError("The API.Bible key was rejected. A bot owner should check it with `[p]set api apibible api_key,YOUR_KEY`.")
                    if resp.status == 403:
                        raise SourceError("forbidden")
                    if resp.status == 404:
                        raise SourceError("not found")
                    if resp.status == 429:
                        raise SourceError("API.Bible is busy right now. Try again in a minute.")
                    if resp.status != 200:
                        raise SourceError(f"API.Bible had a problem (HTTP {resp.status}). Try again later.")
                    self._base = base
                    return await resp.json(content_type=None)
            except aiohttp.ClientConnectorError as exc:  # old or unknown host: try the next one
                last = exc
                continue
            except aiohttp.ClientError as exc:
                log.warning("API.Bible request failed: %s", exc)
                raise SourceError("Couldn't reach API.Bible right now. Try again later.") from exc
        log.warning("API.Bible unreachable: %s", last)
        raise SourceError("Couldn't reach API.Bible right now. Try again later.")

    async def _discover(self, force: bool = False) -> dict[str, str]:
        """Find the Bible IDs for each translation in the key's plan."""
        tokens = await self._tokens()
        key = tokens.get("api_key")
        if not key:
            raise SourceError("no key")
        fresh = time.monotonic() - self._ids_at < self.DISCOVERY_TTL and self._ids_key == key
        if self._ids and fresh and not force:
            return self._ids
        payload = await self._request("/bibles", key, {"language": "eng"})
        bibles = payload.get("data") or []
        ids: dict[str, str] = {}
        for k, tr in TRANSLATIONS.items():
            override = tokens.get(f"{k}_id")
            if override:
                ids[k] = override
                continue
            if tr.bundled:
                continue
            scored = sorted(((tr.matcher(b), b.get("id")) for b in bibles), reverse=True)
            if scored and scored[0][0] > 0 and scored[0][1]:
                ids[k] = scored[0][1]
        self._ids, self._ids_at, self._ids_key = ids, time.monotonic(), key
        log.info("API.Bible translations found: %s", ids)
        return ids

    async def _apibible(self, passage: Passage, tr: Translation) -> PassageText:
        tokens = await self._tokens()
        key = tokens.get("api_key")
        if not key:
            raise SourceError(
                f"The **{tr.name}** isn't set up yet. A bot owner can add an API.Bible key with "
                "`[p]set api apibible api_key,YOUR_KEY`."
            )
        ids = await self._discover()
        bible_id = ids.get(tr.key)
        if not bible_id:
            raise SourceError(f"The **{tr.name}** isn't included in this bot's API.Bible plan.")
        params = {
            "content-type": "text",
            "include-notes": "false",
            "include-titles": "false",
            "include-chapter-numbers": "false",
            "include-verse-numbers": "true",
            "include-verse-spans": "false",
        }
        try:
            payload = await self._request(f"/bibles/{bible_id}/passages/{passage.usfm_id}", key, params)
        except SourceError as exc:
            if str(exc) == "forbidden":
                raise SourceError(f"The API.Bible key doesn't have access to the **{tr.name}**.") from None
            if str(exc) == "not found":
                raise SourceError(f"The **{tr.name}** doesn't include {passage.label}.") from None
            raise
        data = payload.get("data") or {}
        verses = split_numbered(passage, data.get("content") or "")
        if not verses:
            raise SourceError(f"The **{tr.name}** doesn't include {passage.label}.")
        await self._fums((payload.get("meta") or {}).get("fumsToken"))
        copyright_ = " ".join((data.get("copyright") or "").split()) or None
        return PassageText(passage, tr, verses, copyright_)

    async def _fums(self, token: Optional[str]):
        """API.Bible's fair-use reporting: lets the publisher know a passage was shown."""
        if not token:
            return
        try:
            user = getattr(self.bot, "user", None)
            async with self.session.get(
                "https://fums.api.bible/f3",
                params={"t": token, "dId": f"redbot-{user.id if user else 0}", "sId": self._fums_session},
            ):
                pass
        except Exception:
            log.debug("FUMS report failed", exc_info=True)
