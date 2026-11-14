"""Dead by Daylight game data from the free dbd.tricky.lol API (no key needed).

Perks, characters and the weekly Shrine of Secrets. Responses are cached, and
perk descriptions are turned from game markup into clean Discord text.
"""

from __future__ import annotations

import asyncio
import difflib
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

BASE = "https://dbd.tricky.lol/api"
STATIC_TTL = 6 * 3600
SHRINE_TTL = 600

PERK_ALIASES = {
    "dh": "dead hard", "ds": "decisive strike", "bt": "borrowed time", "sb": "sprint burst",
    "noed": "no one escapes death", "bbq": "barbecue & chili",
    "pop": "pop goes the weasel", "cob": "call of brine", "dms": "dead man's switch",
    "ota": "off the record", "otr": "off the record", "we": "adrenaline", "mft": "made for this",
    "pwyf": "make your choice", "myc": "make your choice", "nwo": "no way out", "sh": "self-care",
    "uh": "unbreakable", "lithe": "lithe", "bnp": "botany knowledge", "kindred": "kindred",
    "fta": "friends 'til the end", "gens": "deadlock", "jolt": "surge", "eruption": "eruption",
    "starstruck": "starstruck", "pain res": "scourge hook: pain resonance", "pr": "scourge hook: pain resonance",
    "grim": "grim embrace", "lightborn": "lightborn", "corrupt": "corrupt intervention", "ci": "corrupt intervention",
}


CHARACTER_ALIASES = {
    "chucky": "the good guy", "xeno": "the xenomorph", "alien": "the xenomorph", "myers": "the shape",
    "michael": "the shape", "mikey": "the shape", "freddy": "the nightmare", "bubba": "the cannibal",
    "leatherface": "the cannibal", "pinhead": "the cenobite", "wesker": "the mastermind", "vecna": "the lich",
    "dracula": "the dark lord", "sadako": "the onryo", "demo": "the demogorgon", "pyramid head": "the executioner",
    "ph": "the executioner", "springtrap": "the animatronic", "kaneki": "the ghoul", "billy": "the hillbilly",
    "deathslinger": "the deathslinger", "slinger": "the deathslinger", "gf": "the ghost face",
    "ghostface": "the ghost face", "plague": "the plague", "legion": "the legion", "nemmy": "the nemesis",
    "nemesis": "the nemesis", "skully": "the skull merchant", "sm": "the skull merchant", "pig": "the pig",
    "amanda": "the pig", "trapper": "the trapper", "wraith": "the wraith", "hag": "the hag",
}


class DataError(Exception):
    pass


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return "".join(ch for ch in text.lower() if ch.isalnum())


def short_name(name: str) -> str:
    """'The Trapper' -> 'Trapper'; survivors keep their full name."""
    return name[4:] if name.startswith("The ") else name


@dataclass
class Character:
    key: str            # API index ("0", "268435456"…)
    id: str             # stable game id ("Dwight", "Chuckles") — what we store
    name: str
    role: str
    perks: list[str] = field(default_factory=list)
    bio: str = ""
    difficulty: str = ""

    @property
    def short(self) -> str:
        return short_name(self.name)


@dataclass
class Perk:
    key: str
    name: str
    role: str
    description: str
    character: Optional[Character]
    teachable: Optional[int]
    categories: list[str]


def _fmt_value(v) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def clean_description(raw: str, tunables: dict) -> str:
    """Game markup -> Discord text, with tier values filled in ('40/50/60')."""

    def placeholder(m: re.Match) -> str:
        parts = m.group(1).split(".")
        kind = parts[0]
        if kind == "Tunable" and len(parts) >= 3:
            key = parts[2].lower()
            values = tunables.get(key)
            if values is None:
                return "?"
            values = [_fmt_value(x) for x in values]
            if len(set(values)) == 1:
                return f"**{values[0]}**"
            return "**" + "/".join(values) + "**"
        if kind == "Keyword" and len(parts) >= 2:
            return re.sub(r"(?<!^)(?=[A-Z])", " ", parts[1]).replace("_", " ")
        if kind == "Input" and len(parts) >= 2:
            name = parts[1].lower()
            if "activatable" in name or "ability" in name:
                return "the Active Ability button"
            return re.sub(r"(?<!^)(?=[A-Z])", " ", parts[1]).replace("Button", " button").strip()
        return parts[-1]

    text = re.sub(r"\{([^}]*)\}", placeholder, raw or "")
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<li>", "• ", text)
    text = re.sub(r"</li>", "\n", text)
    text = re.sub(r"</?ul>", "\n", text)
    text = re.sub(r"</?b>", "**", text)
    text = re.sub(r"</?i>", "*", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("****", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class GameData:
    def __init__(self, user_agent: str):
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": user_agent}, timeout=aiohttp.ClientTimeout(total=30)
        )
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = asyncio.Lock()
        self.characters: dict[str, Character] = {}   # by stable id
        self.perks: dict[str, Perk] = {}             # by api key
        self._loaded = 0.0

    async def close(self):
        await self.session.close()

    async def _get(self, path: str, ttl: int):
        hit = self._cache.get(path)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        try:
            async with self.session.get(BASE + path) as r:
                if r.status != 200:
                    raise DataError(f"HTTP {r.status}")
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            if hit:  # serve stale data rather than nothing
                return hit[1]
            raise DataError(str(exc) or type(exc).__name__) from exc
        self._cache[path] = (time.monotonic(), data)
        return data

    async def load(self, force: bool = False):
        async with self._lock:
            if self.perks and not force and time.monotonic() - self._loaded < STATIC_TTL:
                return
            chars_raw, perks_raw = await asyncio.gather(
                self._get("/characters", STATIC_TTL), self._get("/perks", STATIC_TTL)
            )
            by_index: dict[int, Character] = {}
            characters = {}
            for key, c in chars_raw.items():
                ch = Character(key=key, id=c["id"], name=c["name"], role=c["role"],
                               perks=list(c.get("perks") or []), bio=c.get("bio") or "",
                               difficulty=c.get("difficulty") or "")
                characters[ch.id] = ch
                by_index[int(key)] = ch
            perks = {}
            for key, p in perks_raw.items():
                perks[key] = Perk(
                    key=key, name=p["name"], role=p.get("role", ""),
                    description=clean_description(p.get("description", ""), p.get("tunables") or {}),
                    character=by_index.get(p.get("character")) if p.get("character") is not None else None,
                    teachable=p.get("teachable"), categories=list(p.get("categories") or []),
                )
            self.characters, self.perks, self._loaded = characters, perks, time.monotonic()

    async def shrine(self) -> dict:
        return await self._get("/shrine", SHRINE_TTL)

    # ── lookups ────────────────────────────────────────────────────────
    def find_perk(self, query: str) -> tuple[Optional[Perk], list[Perk]]:
        """Best match plus a few runner-ups."""
        q = PERK_ALIASES.get(query.lower().strip(), query)
        nq = norm(q)
        perks = list(self.perks.values())
        exact = [p for p in perks if norm(p.name) == nq]
        if exact:
            return exact[0], []
        starts = [p for p in perks if norm(p.name).startswith(nq)]
        contains = [p for p in perks if nq and nq in norm(p.name)]
        pool = starts or contains
        if pool:
            pool.sort(key=lambda p: len(p.name))
            return pool[0], pool[1:4]
        names = {norm(p.name): p for p in perks}
        close = difflib.get_close_matches(nq, list(names), n=4, cutoff=0.8)
        if close:
            return names[close[0]], [names[c] for c in close[1:]]
        return None, []

    def find_character(self, query: str, role: Optional[str] = None) -> Optional[Character]:
        query = CHARACTER_ALIASES.get(query.lower().strip(), query)
        nq = norm(query.lower().removeprefix("the "))
        pool = [c for c in self.characters.values() if not role or c.role == role]
        for getter in (lambda c: norm(c.short), lambda c: norm(c.name), lambda c: norm(c.name.split()[0])):
            hits = [c for c in pool if getter(c) == nq]
            if hits:
                return hits[0]
        hits = [c for c in pool if nq and (norm(c.name).startswith(nq) or norm(c.short).startswith(nq))]
        if hits:
            return sorted(hits, key=lambda c: len(c.name))[0]
        names = {norm(c.short): c for c in pool}
        close = difflib.get_close_matches(nq, list(names), n=1, cutoff=0.7)
        return names[close[0]] if close else None

    def perks_for_role(self, role: str) -> list[Perk]:
        return [p for p in self.perks.values() if p.role == role]

    def character_perks(self, ch: Character) -> list[Perk]:
        return [self.perks[k] for k in ch.perks if k in self.perks]

    def roster(self, role: str) -> list[Character]:
        return sorted((c for c in self.characters.values() if c.role == role), key=lambda c: norm(c.short))
