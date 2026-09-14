from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

log = logging.getLogger("red.krewfantasy.sleeper")


class SleeperAPIError(RuntimeError):
    pass


class SleeperClient:
    """Small async Sleeper client.

    Documented v1 endpoints use api.sleeper.app. Projection/stats/schedule feeds are
    intentionally isolated because Sleeper does not include them in the public v1 docs.
    """

    V1_BASE = "https://api.sleeper.app/v1"
    APP_BASE = "https://api.sleeper.app"
    DATA_BASE = "https://api.sleeper.com"

    def __init__(self, session: aiohttp.ClientSession, cache_dir: Path):
        self.session = session
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.players_cache_path = self.cache_dir / "players_nfl.json"
        self.projection_cache_dir = self.cache_dir / "projections"
        self.projection_cache_dir.mkdir(parents=True, exist_ok=True)
        self.schedule_cache_dir = self.cache_dir / "schedule"
        self.schedule_cache_dir.mkdir(parents=True, exist_ok=True)
        self._players_mem: Optional[Dict[str, Dict[str, Any]]] = None
        self._players_mem_loaded_at = 0.0
        self._lock = asyncio.Lock()

    async def _get_json(self, url: str, *, timeout: int = 25) -> Any:
        headers = {
            "User-Agent": "Mozilla/5.0 KrewFantasy/1.0 (Red-DiscordBot; Sleeper read-only integration)",
            "Accept": "application/json",
        }
        try:
            async with self.session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise SleeperAPIError(f"Sleeper returned HTTP {resp.status} for {url}: {text[:180]}")
                try:
                    return json.loads(text)
                except json.JSONDecodeError as exc:
                    raise SleeperAPIError(f"Sleeper returned invalid JSON for {url}") from exc
        except asyncio.TimeoutError as exc:
            raise SleeperAPIError(f"Sleeper request timed out: {url}") from exc
        except aiohttp.ClientError as exc:
            raise SleeperAPIError(f"Sleeper request failed: {exc}") from exc

    async def league(self, league_id: str) -> Dict[str, Any]:
        return await self._get_json(f"{self.V1_BASE}/league/{league_id}")

    async def rosters(self, league_id: str) -> List[Dict[str, Any]]:
        return await self._get_json(f"{self.V1_BASE}/league/{league_id}/rosters")

    async def users(self, league_id: str) -> List[Dict[str, Any]]:
        return await self._get_json(f"{self.V1_BASE}/league/{league_id}/users")

    async def matchups(self, league_id: str, week: int) -> List[Dict[str, Any]]:
        return await self._get_json(f"{self.V1_BASE}/league/{league_id}/matchups/{int(week)}")

    async def transactions(self, league_id: str, week: int) -> List[Dict[str, Any]]:
        rows = await self._get_json(f"{self.V1_BASE}/league/{league_id}/transactions/{int(week)}")
        # Some league responses have historically been broader than requested.
        return [r for r in rows if int(r.get("leg") or week) == int(week)]

    async def winners_bracket(self, league_id: str) -> List[Dict[str, Any]]:
        return await self._get_json(f"{self.V1_BASE}/league/{league_id}/winners_bracket")

    async def losers_bracket(self, league_id: str) -> List[Dict[str, Any]]:
        return await self._get_json(f"{self.V1_BASE}/league/{league_id}/losers_bracket")

    async def state(self) -> Dict[str, Any]:
        return await self._get_json(f"{self.V1_BASE}/state/nfl")

    async def traded_picks(self, league_id: str) -> List[Dict[str, Any]]:
        return await self._get_json(f"{self.V1_BASE}/league/{league_id}/traded_picks")

    async def drafts(self, league_id: str) -> List[Dict[str, Any]]:
        return await self._get_json(f"{self.V1_BASE}/league/{league_id}/drafts")

    async def draft_picks(self, draft_id: str) -> List[Dict[str, Any]]:
        return await self._get_json(f"{self.V1_BASE}/draft/{draft_id}/picks")

    async def trending(self, kind: str = "add", hours: int = 24, limit: int = 25) -> List[Dict[str, Any]]:
        kind = "drop" if kind == "drop" else "add"
        return await self._get_json(
            f"{self.V1_BASE}/players/nfl/trending/{kind}?lookback_hours={int(hours)}&limit={int(limit)}"
        )

    async def players(self, *, force: bool = False) -> Dict[str, Dict[str, Any]]:
        """Return player map, refreshing no more than once daily unless forced."""
        async with self._lock:
            now = time.time()
            if self._players_mem and not force and now - self._players_mem_loaded_at < 3600:
                return self._players_mem

            if self.players_cache_path.exists() and not force:
                age = now - self.players_cache_path.stat().st_mtime
                if age < 86400:
                    try:
                        self._players_mem = json.loads(self.players_cache_path.read_text(encoding="utf-8"))
                        self._players_mem_loaded_at = now
                        return self._players_mem
                    except (OSError, json.JSONDecodeError):
                        log.warning("Player cache unreadable; refreshing from Sleeper.")

            data = await self._get_json(f"{self.V1_BASE}/players/nfl", timeout=60)
            try:
                tmp = self.players_cache_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self.players_cache_path)
            except OSError:
                log.exception("Could not persist Sleeper player cache")
            self._players_mem = data
            self._players_mem_loaded_at = now
            return data

    async def projections(self, season: int, week: int, *, force: bool = False) -> Dict[str, Dict[str, Any]]:
        """Unofficial Sleeper weekly projection feed; cached 6 hours."""
        cache = self.projection_cache_dir / f"{season}_{week}.json"
        now = time.time()
        if cache.exists() and not force and now - cache.stat().st_mtime < 21600:
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        urls = [
            f"{self.DATA_BASE}/projections/nfl/regular/{season}/{week}",
            f"{self.DATA_BASE}/projections/nfl/{season}/{week}?season_type=regular",
        ]
        last_error: Optional[Exception] = None
        for url in urls:
            try:
                raw = await self._get_json(url, timeout=35)
                data = self._normalize_player_feed(raw)
                if data:
                    try:
                        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    except OSError:
                        pass
                    return data
            except SleeperAPIError as exc:
                last_error = exc
        if cache.exists():
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except Exception:
                pass
        if last_error:
            raise SleeperAPIError(f"Sleeper projection feed unavailable: {last_error}")
        return {}

    async def schedule(self, season: int, *, force: bool = False) -> List[Dict[str, Any]]:
        """Unofficial Sleeper NFL schedule; used only for bye-week detection."""
        cache = self.schedule_cache_dir / f"{season}.json"
        now = time.time()
        if cache.exists() and not force and now - cache.stat().st_mtime < 604800:
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        url = f"{self.APP_BASE}/schedule/nfl/regular/{season}"
        data = await self._get_json(url, timeout=35)
        if isinstance(data, dict):
            # Some variants wrap the list.
            data = data.get("games") or data.get("schedule") or []
        if not isinstance(data, list):
            return []
        try:
            cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return data

    @staticmethod
    def _normalize_player_feed(raw: Any) -> Dict[str, Dict[str, Any]]:
        if isinstance(raw, dict):
            # Normal modern shape: player id -> stats/projection dict.
            if all(isinstance(v, dict) for v in raw.values()):
                return {str(k): v for k, v in raw.items()}
            return {}
        if isinstance(raw, list):
            out: Dict[str, Dict[str, Any]] = {}
            for row in raw:
                if not isinstance(row, dict):
                    continue
                pid = row.get("player_id") or (row.get("player") or {}).get("player_id")
                if not pid:
                    continue
                merged = {}
                if isinstance(row.get("stats"), dict):
                    merged.update(row["stats"])
                merged.update({k: v for k, v in row.items() if k not in {"stats", "player"}})
                out[str(pid)] = merged
            return out
        return {}
