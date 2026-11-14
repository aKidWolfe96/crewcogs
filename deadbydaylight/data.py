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
    image: str = ""     # game asset path of the portrait
    power: str = ""     # killers: item key of their power

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
    image: str = ""


RARITY_ORDER = ("common", "uncommon", "rare", "veryrare", "visceral")
RARITY_LABELS = {"common": "Common", "uncommon": "Uncommon", "rare": "Rare", "veryrare": "Very Rare",
                 "visceral": "Ultra Rare", "none": "", "event": "Event"}
ITEM_TYPE_LABELS = {"toolbox": "Toolbox", "medkit": "Med-Kit", "flashlight": "Flashlight", "map": "Map",
                    "key": "Key", "fogvial": "Fog Vial", "firecracker": "Firecracker"}
ITEM_TYPE_ALIASES = {"toolbox": "toolbox", "toolboxes": "toolbox", "tb": "toolbox", "medkit": "medkit",
                     "med-kit": "medkit", "medkits": "medkit", "med": "medkit", "flashlight": "flashlight",
                     "flashlights": "flashlight", "fl": "flashlight", "torch": "flashlight", "map": "map",
                     "maps": "map", "key": "key", "keys": "key", "fogvial": "fogvial", "fog vial": "fogvial",
                     "vial": "fogvial", "fog": "fogvial"}
# Offerings that live in event folders but are normal Bloodweb offerings.
EVENT_FOLDERS = {"anniversary", "halloween", "halloween2021", "halloween2022", "lunarnewyear", "spring",
                 "summer", "gelato", "winter"}
EVENT_OFFERING_KEEP = {"bloody party streamers", "escape! cake", "survivor pudding"}
# 'not_available_for_<x>' offering tags use internal killer names.
SPLINTER_KILLERS = {"witch": "hag", "killer07": "doctor"}


@dataclass
class Gear:
    """An item, killer power, add-on or offering."""
    key: str
    kind: str            # item | power | addon | offering
    name: str
    role: Optional[str]
    rarity: str
    description: str
    image: str
    item_type: Optional[str] = None
    parents: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    randomizable: bool = True     # false for event / non-Bloodweb stuff

    @property
    def rarity_label(self) -> str:
        return RARITY_LABELS.get(self.rarity, self.rarity.title())


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
    # Item/offering text runs sentences together ("…use</b>.<b>Decreases…"); the game
    # shows each on its own line. Abbreviations like S.T.A.R.S. are left alone.
    text = re.sub(r"(?<=[a-z0-9%)])\.(</b>)?(?=(?:<b>)?[A-Z])", lambda m: "." + (m.group(1) or "") + "<br>", text)
    text = re.sub(r"(?<=</b>)\.(?=(?:<b>)?[A-Z])", ".<br>", text)
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
        self.items: dict[str, Gear] = {}             # items + killer powers
        self.addons: dict[str, Gear] = {}
        self.offerings: dict[str, Gear] = {}
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
            try:
                items_raw, addons_raw, offerings_raw = await asyncio.gather(
                    self._get("/items", STATIC_TTL), self._get("/addons", STATIC_TTL),
                    self._get("/offerings", STATIC_TTL),
                )
            except DataError:
                items_raw, addons_raw, offerings_raw = {}, {}, {}
            by_index: dict[int, Character] = {}
            characters = {}
            for key, c in chars_raw.items():
                ch = Character(key=key, id=c["id"], name=c["name"], role=c["role"],
                               perks=list(c.get("perks") or []), bio=c.get("bio") or "",
                               difficulty=c.get("difficulty") or "", image=c.get("image") or "",
                               power=(c.get("item") or "") if c["role"] == "killer" else "")
                characters[ch.id] = ch
                by_index[int(key)] = ch
            perks = {}
            for key, p in perks_raw.items():
                perks[key] = Perk(
                    key=key, name=p["name"], role=p.get("role", ""),
                    description=clean_description(p.get("description", ""), p.get("tunables") or {}),
                    character=by_index.get(p.get("character")) if p.get("character") is not None else None,
                    teachable=p.get("teachable"), categories=list(p.get("categories") or []),
                    image=p.get("image") or "",
                )
            self.items, self.addons, self.offerings = self._gear(items_raw, addons_raw, offerings_raw)
            self.characters, self.perks, self._loaded = characters, perks, time.monotonic()

    @staticmethod
    def _gear(items_raw: dict, addons_raw: dict, offerings_raw: dict):
        items, addons, offerings = {}, {}, {}
        for key, v in (items_raw or {}).items():
            kind = "power" if v.get("type") == "power" else "item"
            if kind == "item" and v.get("type") != "item":
                continue
            items[key] = Gear(
                key=key, kind=kind, name=v.get("name") or key, role=v.get("role"), rarity=v.get("rarity") or "none",
                description=clean_description(v.get("description"), {}), image=v.get("image") or "",
                item_type=v.get("item_type"),
                randomizable=kind == "power" or bool(
                    v.get("bloodweb") and not v.get("event") and v.get("item_type") and v.get("rarity") != "none"
                    and not str(v.get("name", "")).startswith("@#")
                ),
            )
        for key, v in (addons_raw or {}).items():
            addons[key] = Gear(
                key=key, kind="addon", name=v.get("name") or key, role=v.get("role"), rarity=v.get("rarity") or "none",
                description=clean_description(v.get("description"), {}), image=v.get("image") or "",
                item_type=v.get("item_type"), parents=list(v.get("parents") or []),
                randomizable=bool(v.get("bloodweb") and v.get("rarity") != "none"
                                  and not str(v.get("name", "")).startswith("@#")),
            )
        for key, v in (offerings_raw or {}).items():
            image = v.get("image") or ""
            folder = image.split("/Favors/", 1)[1].rsplit("/", 1)[0].lower() if "/Favors/" in image and \
                image.split("/Favors/", 1)[1].count("/") else ""
            name = v.get("name") or key
            event = folder in EVENT_FOLDERS and name.lower() not in EVENT_OFFERING_KEEP
            offerings[key] = Gear(
                key=key, kind="offering", name=name, role=v.get("role"), rarity=v.get("rarity") or "none",
                description=clean_description(v.get("description"), {}), image=image,
                tags=list(v.get("tags") or []),
                randomizable=not event and not v.get("retired") and v.get("rarity") != "none",
            )
        return items, addons, offerings

    # ── gear helpers ───────────────────────────────────────────────────
    def power_of(self, ch: Character) -> Optional[Gear]:
        return self.items.get(ch.power) if ch.power else None

    def addons_for(self, parent: Gear, randomizable_only: bool = False) -> list[Gear]:
        """Add-ons for a killer power (by parent key) or a survivor item (by item type)."""
        if parent.kind == "power":
            pool = [a for a in self.addons.values() if parent.key in a.parents]
        else:
            pool = [a for a in self.addons.values() if a.role == "survivor" and a.item_type == parent.item_type]
        if randomizable_only:
            pool = [a for a in pool if a.randomizable]
        return sorted(pool, key=lambda a: (RARITY_ORDER.index(a.rarity) if a.rarity in RARITY_ORDER else 9, a.name))

    def offerings_for(self, role: str, character: Optional[Character] = None) -> list[Gear]:
        out = []
        for o in self.offerings.values():
            if not o.randomizable or (o.role and o.role != role):
                continue
            blocked = [t[len("not_available_for_"):] for t in o.tags if t.startswith("not_available_for_")]
            if character and blocked:
                me = {norm(character.short), norm(character.id)}
                if any(norm(SPLINTER_KILLERS.get(b, b)) in me for b in blocked):
                    continue
            out.append(o)
        return out

    def survivor_items(self, item_type: Optional[str] = None) -> list[Gear]:
        return [i for i in self.items.values()
                if i.kind == "item" and i.randomizable and (item_type is None or i.item_type == item_type)]

    @staticmethod
    def _find(pool, query: str):
        nq = norm(query)
        if not nq:
            return None, []
        exact = [g for g in pool if norm(g.name) == nq]
        if exact:
            return exact[0], []
        hits = sorted([g for g in pool if nq in norm(g.name)], key=lambda g: (not norm(g.name).startswith(nq), len(g.name)))
        if hits:
            return hits[0], hits[1:4]
        names = {norm(g.name): g for g in pool}
        close = difflib.get_close_matches(nq, list(names), n=4, cutoff=0.8)
        return (names[close[0]], [names[c] for c in close[1:]]) if close else (None, [])

    def find_item(self, query: str):
        return self._find([i for i in self.items.values() if i.kind == "item" and not i.name.startswith("@#")], query)

    def find_addon(self, query: str):
        return self._find([a for a in self.addons.values() if not a.name.startswith("@#")], query)

    def find_offering(self, query: str):
        return self._find(list(self.offerings.values()), query)

    def addon_owner(self, addon: Gear) -> str:
        if addon.role == "survivor":
            return ITEM_TYPE_LABELS.get(addon.item_type or "", "Item") + " add-on"
        for parent in addon.parents:
            power = self.items.get(parent)
            if power:
                killer = next((c for c in self.characters.values() if c.power == parent), None)
                return f"{power.name} ({killer.name})" if killer else power.name
        return "Killer add-on"

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
