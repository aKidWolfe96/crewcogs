"""Book names, reference parsing and the bundled King James Version text.

Everything here is plain Python (no Discord), so it can be tested on its own.
Verse numbering follows the King James Version, which the New International
Version and English Standard Version also use for almost every verse.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

DATA_DIR = Path(__file__).parent / "data"

# (display name, USFM code used by API.Bible, extra aliases)
BOOKS: list[tuple[str, str, tuple[str, ...]]] = [
    ("Genesis", "GEN", ("gen", "ge", "gn")),
    ("Exodus", "EXO", ("ex", "exo", "exod")),
    ("Leviticus", "LEV", ("lev", "le", "lv")),
    ("Numbers", "NUM", ("num", "nu", "nm", "numb")),
    ("Deuteronomy", "DEU", ("deut", "de", "dt", "deu")),
    ("Joshua", "JOS", ("josh", "jos", "jsh")),
    ("Judges", "JDG", ("judg", "jdg", "jg", "jdgs")),
    ("Ruth", "RUT", ("ru", "rth", "rut")),
    ("1 Samuel", "1SA", ("1sam", "1sa", "1sm", "1s")),
    ("2 Samuel", "2SA", ("2sam", "2sa", "2sm", "2s")),
    ("1 Kings", "1KI", ("1kgs", "1ki", "1k", "1kin")),
    ("2 Kings", "2KI", ("2kgs", "2ki", "2k", "2kin")),
    ("1 Chronicles", "1CH", ("1chron", "1chr", "1ch")),
    ("2 Chronicles", "2CH", ("2chron", "2chr", "2ch")),
    ("Ezra", "EZR", ("ezr", "ez")),
    ("Nehemiah", "NEH", ("neh", "ne")),
    ("Esther", "EST", ("esth", "est", "es")),
    ("Job", "JOB", ("jb",)),
    ("Psalms", "PSA", ("psalm", "ps", "psa", "pss", "psm", "pslm")),
    ("Proverbs", "PRO", ("prov", "pro", "prv", "pr")),
    ("Ecclesiastes", "ECC", ("eccles", "eccl", "ecc", "ec", "qoheleth")),
    ("Song of Solomon", "SNG", ("song", "songofsongs", "sos", "canticles", "song of songs")),
    ("Isaiah", "ISA", ("isa", "is")),
    ("Jeremiah", "JER", ("jer", "je", "jr")),
    ("Lamentations", "LAM", ("lam", "la")),
    ("Ezekiel", "EZK", ("ezek", "eze", "ezk")),
    ("Daniel", "DAN", ("dan", "da", "dn")),
    ("Hosea", "HOS", ("hos", "ho")),
    ("Joel", "JOL", ("jl",)),
    ("Amos", "AMO", ("am",)),
    ("Obadiah", "OBA", ("obad", "ob")),
    ("Jonah", "JON", ("jnh", "jon")),
    ("Micah", "MIC", ("mic", "mc")),
    ("Nahum", "NAM", ("nah", "na")),
    ("Habakkuk", "HAB", ("hab", "hb")),
    ("Zephaniah", "ZEP", ("zeph", "zep", "zp")),
    ("Haggai", "HAG", ("hag", "hg")),
    ("Zechariah", "ZEC", ("zech", "zec", "zc")),
    ("Malachi", "MAL", ("mal", "ml")),
    ("Matthew", "MAT", ("matt", "mt", "mat")),
    ("Mark", "MRK", ("mrk", "mk", "mr")),
    ("Luke", "LUK", ("luk", "lk")),
    ("John", "JHN", ("jn", "jhn", "joh")),
    ("Acts", "ACT", ("act", "ac")),
    ("Romans", "ROM", ("rom", "ro", "rm")),
    ("1 Corinthians", "1CO", ("1cor", "1co")),
    ("2 Corinthians", "2CO", ("2cor", "2co")),
    ("Galatians", "GAL", ("gal", "ga")),
    ("Ephesians", "EPH", ("eph", "ephes")),
    ("Philippians", "PHP", ("phil", "php", "pp")),
    ("Colossians", "COL", ("col", "co")),
    ("1 Thessalonians", "1TH", ("1thess", "1thes", "1th")),
    ("2 Thessalonians", "2TH", ("2thess", "2thes", "2th")),
    ("1 Timothy", "1TI", ("1tim", "1ti", "1tm")),
    ("2 Timothy", "2TI", ("2tim", "2ti", "2tm")),
    ("Titus", "TIT", ("tit", "ti")),
    ("Philemon", "PHM", ("philem", "phm", "pm")),
    ("Hebrews", "HEB", ("heb",)),
    ("James", "JAS", ("jas", "jm")),
    ("1 Peter", "1PE", ("1pet", "1pe", "1pt", "1p")),
    ("2 Peter", "2PE", ("2pet", "2pe", "2pt", "2p")),
    ("1 John", "1JN", ("1jn", "1jhn", "1jo", "1j")),
    ("2 John", "2JN", ("2jn", "2jhn", "2jo", "2j")),
    ("3 John", "3JN", ("3jn", "3jhn", "3jo", "3j")),
    ("Jude", "JUD", ("jud", "jd")),
    ("Revelation", "REV", ("rev", "re", "revelations", "apocalypse", "rv")),
]


class ReferenceError_(ValueError):
    """A reference that couldn't be understood; the message is shown to users."""


def _norm_book(text: str) -> str:
    t = text.lower().replace(".", " ").strip()
    t = re.sub(r"^(first|1st|i)\s+", "1", t)
    t = re.sub(r"^(second|2nd|ii)\s+", "2", t)
    t = re.sub(r"^(third|3rd|iii)\s+", "3", t)
    return re.sub(r"\s+", "", t)


_ALIASES: dict[str, int] = {}
_FULL: list[tuple[str, int]] = []
for _i, (_name, _code, _extra) in enumerate(BOOKS):
    _full = _norm_book(_name)
    _FULL.append((_full, _i))
    for _a in (_name, _code, *_extra):
        _ALIASES.setdefault(_norm_book(_a), _i)
    _ALIASES[_full] = _i


def find_book(text: str) -> Optional[int]:
    """Book index (0-65) for a name, abbreviation or unique prefix."""
    key = _norm_book(text)
    if not key:
        return None
    if key in _ALIASES:
        return _ALIASES[key]
    hits = {i for full, i in _FULL if full.startswith(key)}
    if len(hits) == 1 and len(key) >= 3:
        return hits.pop()
    return None


# ── the bundled King James Version ──────────────────────────────────────
class KJV:
    """All 31,102 verses of the King James Version, loaded once."""

    _books: list[list[list[str]]] | None = None
    _search_index: list[tuple[int, int, int, str]] | None = None

    @classmethod
    def books(cls) -> list[list[list[str]]]:
        if cls._books is None:
            with gzip.open(DATA_DIR / "kjv.json.gz", "rt", encoding="utf-8") as fh:
                cls._books = json.load(fh)["books"]
        return cls._books

    @classmethod
    def chapter_count(cls, book: int) -> int:
        return len(cls.books()[book])

    @classmethod
    def verse_count(cls, book: int, chapter: int) -> int:
        return len(cls.books()[book][chapter - 1])

    @classmethod
    def verse(cls, book: int, chapter: int, verse: int) -> str:
        return cls.books()[book][chapter - 1][verse - 1]

    @classmethod
    def search(cls, query: str, limit: int = 200) -> list[tuple[int, int, int, str]]:
        """Verses containing every word of the query (or the exact phrase in quotes)."""
        if cls._search_index is None:
            idx = []
            for b, chapters in enumerate(cls.books()):
                for c, verses in enumerate(chapters, 1):
                    for v, text in enumerate(verses, 1):
                        idx.append((b, c, v, " " + re.sub(r"[^a-z0-9 ]", " ", text.lower()) + " "))
            cls._search_index = idx
        query = query.strip()
        phrase = None
        m = re.fullmatch(r'["“](.+)["”]', query)
        if m:
            phrase = " " + " ".join(re.sub(r"[^a-z0-9 ]", " ", m.group(1).lower()).split()) + " "
            words = []
        else:
            words = [" " + w + " " for w in re.sub(r"[^a-z0-9 ]", " ", query.lower()).split()]
        if not words and not phrase:
            return []
        out = []
        for b, c, v, low in cls._search_index:
            low_c = " ".join(low.split())
            low_c = f" {low_c} "
            if phrase and phrase not in low_c:
                continue
            if words and not all(w in low_c for w in words):
                continue
            out.append((b, c, v, cls.verse(b, c, v)))
            if len(out) >= limit:
                break
        return out


# ── passages ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Passage:
    book: int
    c1: int
    v1: int
    c2: int
    v2: int

    # names
    @property
    def book_name(self) -> str:
        return BOOKS[self.book][0]

    @property
    def usfm(self) -> str:
        return BOOKS[self.book][1]

    def _bname(self) -> str:
        if self.book == 18:  # Psalms: "Psalm 23", "Psalms 23–24"
            return "Psalm" if self.c1 == self.c2 else "Psalms"
        return self.book_name

    @property
    def whole_chapters(self) -> bool:
        return self.v1 == 1 and self.v2 == KJV.verse_count(self.book, self.c2)

    @property
    def label(self) -> str:
        b = self._bname()
        if self.whole_chapters and KJV.chapter_count(self.book) > 1:
            return f"{b} {self.c1}" if self.c1 == self.c2 else f"{b} {self.c1}–{self.c2}"
        if self.c1 == self.c2:
            if self.v1 == self.v2:
                return f"{b} {self.c1}:{self.v1}"
            return f"{b} {self.c1}:{self.v1}–{self.v2}"
        return f"{b} {self.c1}:{self.v1}–{self.c2}:{self.v2}"

    @property
    def plain_label(self) -> str:
        """Same as label but with a normal hyphen, for APIs."""
        return f"{self.book_name} {self.c1}:{self.v1}-{self.c2}:{self.v2}"

    @property
    def usfm_id(self) -> str:
        if self.c1 == self.c2 and self.v1 == self.v2:
            return f"{self.usfm}.{self.c1}.{self.v1}"
        return f"{self.usfm}.{self.c1}.{self.v1}-{self.usfm}.{self.c2}.{self.v2}"

    # storage
    @property
    def key(self) -> str:
        return f"{self.book}.{self.c1}.{self.v1}.{self.c2}.{self.v2}"

    @classmethod
    def from_key(cls, key: str) -> "Passage":
        b, c1, v1, c2, v2 = (int(x) for x in key.split("."))
        return cls(b, c1, v1, c2, v2)

    # contents
    def verse_ids(self) -> Iterator[tuple[int, int]]:
        for c in range(self.c1, self.c2 + 1):
            start = self.v1 if c == self.c1 else 1
            end = self.v2 if c == self.c2 else KJV.verse_count(self.book, c)
            for v in range(start, end + 1):
                yield c, v

    def __len__(self) -> int:
        return sum(1 for _ in self.verse_ids())

    @classmethod
    def chapter(cls, book: int, chapter: int) -> "Passage":
        return cls(book, chapter, 1, chapter, KJV.verse_count(book, chapter))


_REF = re.compile(
    r"^\s*(?P<book>(?:[123](?:st|nd|rd)?\s*)?[^\d\s][^\d]*?)\s*"
    r"(?P<c1>\d+)(?:\s*[:.]\s*(?P<v1>\d+))?"
    r"(?:\s*[-–—]\s*(?:(?P<c2>\d+)\s*[:.]\s*)?(?P<v2>\d+))?\s*$",
    re.IGNORECASE,
)


def parse_reference(text: str) -> Passage:
    """Turn things like "John 3:16", "1 Cor 13:4-7", "Psalm 23" or "Jude 5" into a Passage."""
    m = _REF.match(text or "")
    if not m:
        raise ReferenceError_(f"I couldn't read **{text}** as a Bible reference. Try something like `John 3:16`.")
    book = find_book(m.group("book"))
    if book is None:
        raise ReferenceError_(f"I don't know a book called **{m.group('book').strip()}**.")
    name = BOOKS[book][0]
    chapters = KJV.chapter_count(book)
    c1 = int(m.group("c1"))
    v1 = int(m.group("v1")) if m.group("v1") else None
    c2 = int(m.group("c2")) if m.group("c2") else None
    v2 = int(m.group("v2")) if m.group("v2") else None

    if chapters == 1 and v1 is None:  # "Jude 5" means verse 5, "Jude 3-5" verses 3-5
        c1, v1 = 1, c1
        if v2 is not None and c2 is None:
            c2 = 1

    if c1 < 1 or c1 > chapters:
        raise ReferenceError_(f"**{name}** has {chapters} chapter{'s' if chapters != 1 else ''}.")

    if v1 is None:  # whole chapter(s): "John 3" or "John 3-4"
        end = v2 if v2 is not None else c1
        if c2 is not None or end < c1 or end > chapters:
            raise ReferenceError_(f"**{name}** has {chapters} chapters.")
        return Passage(book, c1, 1, end, KJV.verse_count(book, end))

    if c2 is None:
        c2 = c1
    if v2 is None:
        v2 = v1
    if c2 < c1 or c2 > chapters:
        raise ReferenceError_(f"**{name}** has {chapters} chapters.")
    for c, v in ((c1, v1), (c2, v2)):
        count = KJV.verse_count(book, c)
        if v < 1 or v > count:
            raise ReferenceError_(f"**{name} {c}** has {count} verses.")
    if (c2, v2) < (c1, v1):
        raise ReferenceError_("The end of that range comes before the start.")
    return Passage(book, c1, v1, c2, v2)
