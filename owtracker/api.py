"""Small async client for the unofficial OverFast API (https://overfast-api.tekrop.fr).

OverFast scrapes Blizzard's public career pages, so everything here is public
data. Responses are cached for 10 minutes (OverFast's own TTL) and images for
the life of the cog.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import aiohttp

BASE = "https://overfast-api.tekrop.fr"
CACHE_TTL = 600
STATIC_TTL = 86_400
TIMEOUT = 15
CONCURRENCY = 4

DIVISIONS = ("bronze", "silver", "gold", "platinum", "diamond", "master", "grandmaster", "champion")
ROLE_ORDER = ("tank", "damage", "support", "open")


class OverFastError(Exception):
    """Raised for anything other than a clean 200/404 so callers can say *why*."""

    def __init__(self, status: int, message: str = ""):
        super().__init__(message or f"HTTP {status}")
        self.status = status


class OverFast:
    def __init__(self, user_agent: str):
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": user_agent},
            timeout=aiohttp.ClientTimeout(total=TIMEOUT),
        )
        self._cache: dict[str, tuple[float, Any]] = {}
        self._images: dict[str, Optional[bytes]] = {}
        self._sem = asyncio.Semaphore(CONCURRENCY)

    async def close(self):
        await self.session.close()

    async def get(self, path: str, ttl: int = CACHE_TTL) -> Optional[Any]:
        """JSON for `path`; None on 404. Raises OverFastError on rate limits/outages."""
        hit = self._cache.get(path)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        async with self._sem:
            try:
                async with self.session.get(BASE + path) as r:
                    if r.status == 404:
                        return None
                    if r.status != 200:
                        raise OverFastError(r.status)
                    data = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise OverFastError(0, str(exc) or type(exc).__name__) from exc
        self._cache[path] = (time.monotonic(), data)
        if len(self._cache) > 2000:
            cutoff = time.monotonic() - STATIC_TTL
            self._cache = {k: v for k, v in self._cache.items() if v[0] > cutoff}
        return data

    async def image(self, url: Optional[str]) -> Optional[bytes]:
        if not url or url.endswith(".svg") or ".svg#" in url:
            return None
        if url in self._images:
            return self._images[url]
        async with self._sem:
            try:
                async with self.session.get(url) as r:
                    data = await r.read() if r.status == 200 else None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                data = None
        self._images[url] = data
        return data

    async def images(self, urls) -> dict[str, Optional[bytes]]:
        urls = [u for u in dict.fromkeys(urls) if u]
        results = await asyncio.gather(*(self.image(u) for u in urls))
        return dict(zip(urls, results))

    # ── endpoints ──────────────────────────────────────────────────────
    async def summary(self, player_id: str):
        return await self.get(f"/players/{player_id}/summary")

    async def stats(self, player_id: str, gamemode: Optional[str] = None, platform: Optional[str] = None):
        q = "&".join(f"{k}={v}" for k, v in (("gamemode", gamemode), ("platform", platform)) if v)
        return await self.get(f"/players/{player_id}/stats/summary" + (f"?{q}" if q else ""))

    async def search(self, name: str):
        data = await self.get(f"/players?name={name}&limit=10")
        return (data or {}).get("results", [])

    async def heroes(self) -> list[dict]:
        return await self.get("/heroes", ttl=STATIC_TTL) or []

    async def maps(self) -> list[dict]:
        return await self.get("/maps", ttl=STATIC_TTL) or []

    async def meta(self, gamemode: str, region: str, role: Optional[str] = None, division: Optional[str] = None):
        path = f"/heroes/stats?platform=pc&gamemode={gamemode}&region={region}"
        if role:
            path += f"&role={role}"
        if division and gamemode == "competitive":
            path += f"&competitive_division={division}"
        return await self.get(path) or []


def to_player_id(battletag: str) -> str:
    """'Aaron#1234' -> 'Aaron-1234' (OverFast's format)."""
    return battletag.strip().replace("#", "-")


def rank_value(rank: Optional[dict]) -> int:
    """Comparable number for a rank: higher is better. 0 = unranked."""
    if not rank or not rank.get("division"):
        return 0
    try:
        div = DIVISIONS.index(rank["division"].lower())
    except ValueError:
        return 0
    tier = rank.get("tier") or 5
    return div * 5 + (6 - tier)


def rank_label(rank: Optional[dict]) -> str:
    if not rank or not rank.get("division"):
        return "Unranked"
    div = rank["division"].title()
    return f"{div} {rank['tier']}" if rank.get("tier") else div


def ranks_by_platform(summary: Optional[dict]) -> dict[str, dict]:
    """{'pc': {'tank': {...}, ..., 'season': 22}, 'console': {...}} with empty platforms dropped."""
    comp = (summary or {}).get("competitive") or {}
    out = {}
    for platform in ("pc", "console"):
        block = comp.get(platform) or {}
        if any(block.get(r) for r in ROLE_ORDER):
            out[platform] = block
    return out
