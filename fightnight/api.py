"""
UFC data layer.

Sources (in order of preference, all free / no key):
  • ESPN search API   -> finds ANY fighter by name (not just those on a card)
  • ESPN athlete API  -> bio, record, headshot, stats
  • ESPN scoreboard   -> events, cards, results
  • Sherdog (scrape)  -> record + full fight history (enrichment / fallback)

Everything degrades gracefully: if one source is down, the others still answer.
"""
import re
import logging
import time
import asyncio
import aiohttp
from urllib.parse import quote
from datetime import datetime, timezone, timedelta
from typing import Optional
from bs4 import BeautifulSoup

log = logging.getLogger("red.akidwolfe.fightnight.api")

# ── endpoints ─────────────────────────────────────────────────────────────────
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
ESPN_SEARCH     = "https://site.web.api.espn.com/apis/common/v3/search"
ESPN_WEB_HEADER = "https://site.web.api.espn.com/apis/v2/scoreboard/header"
ESPN_FIGHTCENTER = "https://site.web.api.espn.com/apis/common/v3/sports/mma/ufc/fightcenter/{id}"

# athlete detail has moved around over the years — we try each in order
ESPN_ATHLETE_ENDPOINTS = [
    "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/athletes/{id}",
    "https://site.web.api.espn.com/apis/common/v3/sports/mma/ufc/athletes/{id}",
    "https://sports.core.api.espn.com/v2/sports/mma/athletes/{id}",
]
ESPN_ATHLETE_STATS = "https://site.web.api.espn.com/apis/common/v3/sports/mma/ufc/athletes/{id}/stats"

SHERDOG_BASE = "https://www.sherdog.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/",
}


# ── low-level fetch helpers ───────────────────────────────────────────────────

async def _get_json(session: aiohttp.ClientSession, url: str) -> Optional[dict]:
    """Fetch JSON and log the real failure instead of silently swallowing it."""
    try:
        async with session.get(
            url,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
            allow_redirects=True,
        ) as r:
            if r.status != 200:
                try:
                    body = (await r.text())[:300].replace("\n", " ")
                except Exception:
                    body = "<unreadable body>"
                log.warning("HTTP %s fetching %s :: %s", r.status, url, body)
                return None
            try:
                return await r.json(content_type=None)
            except Exception as exc:
                log.warning("Invalid JSON from %s: %r", url, exc)
                return None
    except Exception as exc:
        log.warning("Request failed for %s: %s: %s", url, type(exc).__name__, exc)
        return None


async def _get_html(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(url, headers=HEADERS,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status == 200:
                return await r.text()
    except Exception:
        pass
    return None


def _norm(s: str) -> str:
    """Lowercase, strip, collapse whitespace — for name comparison."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# ════════════════════════════════════════════════════════════════════════════
#  EVENTS  (card / results)
# ════════════════════════════════════════════════════════════════════════════



async def _web_header_data(session: aiohttp.ClientSession, ymd: str) -> Optional[dict]:
    """Fetch ESPN's web scoreboard header for a single date.

    This is hosted on site.web.api.espn.com, which is separate from the
    site.api.espn.com scoreboard host that may return Akamai 403s to bots.
    """
    url = (
        f"{ESPN_WEB_HEADER}?sport=mma&league=ufc&region=us&lang=en"
        f"&contentorigin=espn&buyWindow=1m&showAirings=buy%2Clive%2Creplay"
        f"&tz=America%2FNew_York&dates={ymd}"
    )
    return await _get_json(session, url)


def _web_header_events_from_data(data: dict) -> list:
    """Extract events from the web scoreboard/header response."""
    if not isinstance(data, dict):
        return []
    direct = data.get("events")
    if isinstance(direct, list):
        return direct
    for sport in (data.get("sports") or []):
        for league in ((sport or {}).get("leagues") or []):
            events = (league or {}).get("events") or []
            if isinstance(events, list) and events:
                return events
    return []


async def _web_header_events(session: aiohttp.ClientSession, ymd: str) -> list:
    data = await _web_header_data(session, ymd)
    return _web_header_events_from_data(data or {})


async def _web_find_event(session: aiohttp.ClientSession, *, forward: bool,
                          days: int = 21, exact_ymd: str = "") -> Optional[dict]:
    """Find a real UFC event using the site.web.api scoreboard/header feed."""
    now = datetime.now(timezone.utc)
    if exact_ymd:
        dates = [exact_ymd]
    else:
        base = now.date()
        offsets = range(0, days + 1)
        if not forward:
            offsets = range(0, days + 1)
        dates = []
        for i in offsets:
            d = base + timedelta(days=i if forward else -i)
            dates.append(d.strftime("%Y%m%d"))

    for ymd in dates:
        events = await _web_header_events(session, ymd)
        real = [e for e in events if _is_ufc_event(e)]
        if not real:
            continue
        parsed = [(d, e) for e in real if (d := _parse_date(e))]
        if not parsed:
            # If ESPN omitted the date but returned a single real UFC event for
            # the explicitly requested day, keep it rather than failing.
            return real[0]
        if forward:
            eligible = [(d, e) for d, e in parsed if d >= now - LIVE_WINDOW]
            if eligible:
                eligible.sort(key=lambda x: x[0])
                return eligible[0][1]
        else:
            eligible = [(d, e) for d, e in parsed if d <= now]
            if eligible:
                eligible.sort(key=lambda x: x[0], reverse=True)
                return eligible[0][1]
    return None


async def _fightcenter_data(session: aiohttp.ClientSession, eid: str) -> Optional[dict]:
    if not eid:
        return None
    url = (
        ESPN_FIGHTCENTER.format(id=eid)
        + "?region=us&lang=en&contentorigin=espn"
        + "&showAirings=buy%2Clive%2Creplay&buyWindow=1m"
    )
    return await _get_json(session, url)


def _fightcenter_location(event: dict) -> str:
    venue = event.get("venue") or {}
    name = venue.get("fullName", "") or venue.get("name", "")
    addr = venue.get("address") or {}
    city = addr.get("city", "")
    state = addr.get("state", "")
    country = addr.get("country", "")
    place = ", ".join(x for x in (city, state, country) if x)
    if name and place:
        return f"{name} — {place}"
    return name or place or event.get("location", "") or ""


def _fmt_fightcenter(data: dict) -> Optional[dict]:
    """Normalize site.web.api ESPN FightCenter JSON into the cog event shape."""
    if not isinstance(data, dict):
        return None
    event = data.get("event") or {}
    if not event:
        return None

    competitions = []
    cards = data.get("cards") or {}
    # ESPN has used main/prelims1/prelims2 and a few naming variants.
    preferred = ("main", "prelims1", "prelims2", "prelims", "earlyPrelims", "early")
    seen = set()
    for key in preferred:
        card = cards.get(key) or {}
        for comp in (card.get("competitions") or []):
            cid = str((comp or {}).get("id", ""))
            marker = cid or id(comp)
            if marker in seen:
                continue
            seen.add(marker)
            competitions.append(comp or {})
    for key, card in cards.items():
        if key in preferred or not isinstance(card, dict):
            continue
        for comp in (card.get("competitions") or []):
            cid = str((comp or {}).get("id", ""))
            marker = cid or id(comp)
            if marker in seen:
                continue
            seen.add(marker)
            competitions.append(comp or {})

    fights = []
    for comp in competitions:
        competitors = comp.get("competitors") or []
        if len(competitors) < 2:
            continue
        red_c, blue_c = competitors[0] or {}, competitors[1] or {}
        winner = ""
        for c in competitors:
            if (c or {}).get("winner"):
                winner = _athlete_name(c or {})
                break

        status = comp.get("status") or {}
        status_type = status.get("type") or {}
        result = status.get("result") or {}
        method = (
            result.get("displayName", "")
            or result.get("shortDisplayName", "")
            or status_type.get("description", "")
            or ""
        )
        completed = bool(status_type.get("completed") or winner)
        weight_class = _competition_label(comp)
        notes = comp.get("notes") or []
        note_blob = " ".join(str((n or {}).get("text", "") or "") for n in notes)
        title_blob = f"{weight_class} {comp.get('note', '')} {note_blob}".lower()

        fights.append({
            "red": _athlete_name(red_c),
            "blue": _athlete_name(blue_c),
            "red_record": _athlete_record(red_c),
            "blue_record": _athlete_record(blue_c),
            "weight_class": weight_class,
            "is_title": "title" in title_blob or "championship" in title_blob,
            "winner": winner,
            "method": method,
            "round": str(status.get("period", "") or ""),
            "time": status.get("displayClock", "") or "",
            "completed": completed,
        })

    dt = _parse_date(event)
    return {
        "id": str(event.get("id", "")),
        "name": event.get("name", "UFC Event"),
        "shortname": event.get("shortName", event.get("name", "UFC Event")),
        "date": dt.strftime("%B %d, %Y") if dt else "",
        "date_compact": dt.strftime("%Y%m%d") if dt else "",
        "timestamp": int(dt.timestamp()) if dt else None,
        "location": _fightcenter_location(event),
        "fights": fights,
    }


async def _web_event_full(session: aiohttp.ClientSession, header_event: dict) -> Optional[dict]:
    """Resolve a web-header event to its full FightCenter card."""
    eid = str((header_event or {}).get("id", ""))
    data = await _fightcenter_data(session, eid)
    full = _fmt_fightcenter(data or {})
    if full:
        return full
    # Last-resort normalization if FightCenter is temporarily unavailable.
    return _fmt_event(header_event) if header_event else None


async def _scoreboard_data(session: aiohttp.ClientSession, ymd: str = "") -> Optional[dict]:
    url = ESPN_SCOREBOARD if not ymd else f"{ESPN_SCOREBOARD}?dates={ymd}"
    return await _get_json(session, url)


async def _scoreboard(session: aiohttp.ClientSession, ymd: str = "") -> list:
    data = await _scoreboard_data(session, ymd)
    return data.get("events", []) if data else []


async def _scoreboard_range(session: aiohttp.ClientSession, start_ymd: str, end_ymd: str) -> list:
    """Return ESPN events over a date range in one request.

    ESPN's default UFC scoreboard can be centered on DWCS. A range request is
    considerably more reliable than walking calendar dates one request at a
    time and is supported by the same scoreboard backend.
    """
    url = f"{ESPN_SCOREBOARD}?dates={start_ymd}-{end_ymd}&limit=100"
    data = await _get_json(session, url)
    return data.get("events", []) if data else []


def _select_current_or_next(events: list, now: datetime) -> Optional[dict]:
    real = [e for e in (events or []) if _is_ufc_event(e)]
    parsed = [(d, e) for e in real if (d := _parse_date(e))]
    live = [(d, e) for d, e in parsed if d <= now and (now - d) <= LIVE_WINDOW]
    if live:
        live.sort(key=lambda x: x[0], reverse=True)
        return live[0][1]
    future = [(d, e) for d, e in parsed if d > now]
    if future:
        future.sort(key=lambda x: x[0])
        return future[0][1]
    return None


def _parse_date(event: dict) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
    except Exception:
        return None


def _is_ufc_name(name: str) -> bool:
    """True for actual UFC cards/Fight Nights, excluding DWCS and other feeder shows."""
    n = _norm(name)
    return n.startswith("ufc ") or n.startswith("noche ufc")


def _is_ufc_event(event: dict) -> bool:
    return _is_ufc_name(event.get("name", "") or event.get("shortName", ""))


def _calendar_event_id(entry: dict) -> str:
    ref = str((entry.get("event") or {}).get("$ref", ""))
    m = re.search(r"/events/(\d+)", ref)
    return m.group(1) if m else ""


def _calendar_date(entry: dict) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(entry.get("startDate", "")).replace("Z", "+00:00"))
    except Exception:
        return None


async def _fetch_calendar_entry(session: aiohttp.ClientSession, entry: dict) -> Optional[dict]:
    """Resolve a calendar entry to the full scoreboard event.

    ESPN's calendar start date can be offset from the scoreboard's dated bucket,
    so search the calendar day plus one day on either side and match by event id.
    """
    target_id = _calendar_event_id(entry)
    dt = _calendar_date(entry)
    if not dt:
        return None

    for offset in (0, -1, 1):
        ymd = (dt + timedelta(days=offset)).strftime("%Y%m%d")
        events = await _scoreboard(session, ymd)
        if target_id:
            for event in events:
                if str(event.get("id", "")) == target_id:
                    return event
        for event in events:
            if _is_ufc_event(event) and _norm(event.get("name", "")) == _norm(entry.get("label", "")):
                return event
    return None


def _athlete_name(competitor: dict) -> str:
    """Return a safe display name even when ESPN sends a null athlete object."""
    athlete = competitor.get("athlete") or {}
    return (
        athlete.get("displayName")
        or athlete.get("fullName")
        or athlete.get("shortName")
        or "TBD"
    )


def _athlete_record(competitor: dict) -> str:
    if competitor.get("displayRecord"):
        return str(competitor.get("displayRecord") or "")
    for stat in (competitor.get("statistics") or []):
        if stat.get("name") == "record":
            return stat.get("displayValue", "")
    rec = competitor.get("records") or []
    if isinstance(rec, list) and rec:
        return (rec[0] or {}).get("summary", "")
    athlete = competitor.get("athlete") or {}
    return athlete.get("record", "") or ""


def _event_location(raw: dict) -> str:
    comps = raw.get("competitions") or []
    if comps:
        venue = (comps[0] or {}).get("venue") or {}
        loc = venue.get("fullName", "")
        addr = venue.get("address") or {}
        city = addr.get("city", "")
        if loc and city:
            return f"{loc} — {city}"
        return loc or raw.get("location", "") or ""
    return raw.get("location", "") or ""


def _competition_label(comp: dict) -> str:
    ctype = comp.get("type", {}) or {}
    return (
        ctype.get("text", "")
        or ctype.get("displayName", "")
        or ctype.get("abbreviation", "")
        or comp.get("note", "")
        or ""
    )


def _fmt_event(raw: dict) -> dict:
    fights = []
    for comp in (raw.get("competitions") or []):
        comp = comp or {}
        competitors = comp.get("competitors") or []
        if len(competitors) < 2:
            continue
        red_c, blue_c = (competitors[0] or {}), (competitors[1] or {})

        winner = ""
        for c in competitors:
            c = c or {}
            if c.get("winner"):
                winner = _athlete_name(c)

        notes = comp.get("notes") or []
        method = ""
        for note in notes:
            note = note or {}
            t = str(note.get("text", "") or "")
            if any(k in t.lower() for k in ["ko", "tko", "sub", "decision", "round"]):
                method = t
                break

        status = comp.get("status") or {}
        status_type = status.get("type") or {}
        if not method:
            method = status_type.get("description", "") if status_type.get("completed") else ""

        completed = bool(status_type.get("completed"))
        weight_class = _competition_label(comp)
        note_blob = " ".join(str((n or {}).get("text", "") or "") for n in notes)
        title_blob = f"{weight_class} {comp.get('note', '')} {note_blob}".lower()

        fights.append({
            "red":         _athlete_name(red_c),
            "blue":        _athlete_name(blue_c),
            "red_record":  _athlete_record(red_c),
            "blue_record": _athlete_record(blue_c),
            "weight_class": weight_class,
            "is_title":    "title" in title_blob or "championship" in title_blob,
            "winner":      winner,
            "method":      method,
            "round":       str(status.get("period", "") or ""),
            "time":        status.get("displayClock", "") or "",
            "completed":   completed,
        })

    dt = _parse_date(raw)
    return {
        "id":           str(raw.get("id", "")),
        "name":         raw.get("name", "UFC Event"),
        "shortname":    raw.get("shortName", raw.get("name", "UFC Event")),
        "date":         dt.strftime("%B %d, %Y") if dt else "",
        "date_compact": dt.strftime("%Y%m%d") if dt else "",
        "timestamp":    int(dt.timestamp()) if dt else None,
        "location":     _event_location(raw),
        "fights":       fights,
    }


# A card runs several hours; treat an event that started within this window as
# the "current" event so `card`/`picks` keep working once the fights go live.
LIVE_WINDOW = timedelta(hours=24)


async def _calendar_ufc_entries(session: aiohttp.ClientSession) -> list:
    data = await _scoreboard_data(session)
    if not data:
        return []
    entries = []
    leagues = data.get("leagues") or []
    if not leagues:
        return []
    for entry in ((leagues[0] or {}).get("calendar") or []):
        if not _is_ufc_name(entry.get("label", "")):
            continue
        dt = _calendar_date(entry)
        if dt:
            entries.append((dt, entry))
    return entries


async def get_upcoming_event(session: aiohttp.ClientSession) -> Optional[dict]:
    """Return the current/next actual UFC card.

    Primary path uses site.web.api ESPN endpoints because some networks receive
    HTTP 403 from site.api.espn.com's scoreboard while site.web.api remains
    reachable. The legacy scoreboard remains as a fallback.
    """
    header = await _web_find_event(session, forward=True, days=21)
    if header:
        full = await _web_event_full(session, header)
        if full:
            return full

    now = datetime.now(timezone.utc)
    raw = _select_current_or_next(await _scoreboard(session), now)
    if raw:
        return _fmt_event(raw)
    start = (now - timedelta(days=1)).strftime("%Y%m%d")
    end = (now + timedelta(days=21)).strftime("%Y%m%d")
    raw = _select_current_or_next(await _scoreboard_range(session, start, end), now)
    return _fmt_event(raw) if raw else None


async def get_recent_event(session: aiohttp.ClientSession) -> Optional[dict]:
    """Return the most recent actual UFC card, excluding DWCS/feeder events."""
    header = await _web_find_event(session, forward=False, days=21)
    if header:
        full = await _web_event_full(session, header)
        if full:
            return full

    now = datetime.now(timezone.utc)
    direct = [e for e in await _scoreboard(session) if _is_ufc_event(e)]
    past = [(d, e) for e in direct if (d := _parse_date(e)) and d < now]
    if past:
        past.sort(key=lambda x: x[0], reverse=True)
        return _fmt_event(past[0][1])
    start = (now - timedelta(days=21)).strftime("%Y%m%d")
    end = now.strftime("%Y%m%d")
    ranged = [e for e in await _scoreboard_range(session, start, end) if _is_ufc_event(e)]
    past = [(d, e) for e in ranged if (d := _parse_date(e)) and d < now]
    if past:
        past.sort(key=lambda x: x[0], reverse=True)
        return _fmt_event(past[0][1])
    return None


async def get_event_on_date(session: aiohttp.ClientSession, ymd: str) -> Optional[dict]:
    """Fetch an actual UFC event on a specific ESPN date."""
    if not ymd:
        return None
    for event in await _web_header_events(session, ymd):
        if _is_ufc_event(event):
            full = await _web_event_full(session, event)
            if full:
                return full
    for event in await _scoreboard(session, ymd):
        if _is_ufc_event(event):
            return _fmt_event(event)
    return None


async def get_event_by_id(session: aiohttp.ClientSession, eid: str,
                          ymd: str = "") -> Optional[dict]:
    """Fetch a specific event by id. FightCenter allows direct ID lookup."""
    eid = str(eid)
    full = _fmt_fightcenter((await _fightcenter_data(session, eid)) or {})
    if full:
        return full

    if ymd:
        try:
            base = datetime.strptime(ymd, "%Y%m%d").replace(tzinfo=timezone.utc)
            ym_dates = [(base + timedelta(days=o)).strftime("%Y%m%d") for o in (0, -1, 1)]
        except ValueError:
            ym_dates = [ymd]
        for day in ym_dates:
            for event in await _web_header_events(session, day):
                if str(event.get("id")) == eid:
                    resolved = await _web_event_full(session, event)
                    if resolved:
                        return resolved
            for event in await _scoreboard(session, day):
                if str(event.get("id")) == eid:
                    return _fmt_event(event)

    for event in await _scoreboard(session):
        if str(event.get("id")) == eid:
            return _fmt_event(event)
    return None


async def espn_api_status(session: aiohttp.ClientSession) -> list:
    """Probe both ESPN event routes plus fighter search."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y%m%d")
    start = (now - timedelta(days=1)).strftime("%Y%m%d")
    end = (now + timedelta(days=21)).strftime("%Y%m%d")
    probes = [
        ("scoreboard (legacy)", ESPN_SCOREBOARD),
        ("date-range (legacy)", f"{ESPN_SCOREBOARD}?dates={start}-{end}&limit=100"),
        ("web-header (primary)",
         f"{ESPN_WEB_HEADER}?sport=mma&league=ufc&region=us&lang=en&contentorigin=espn&dates={today}"),
        ("fightcenter (primary)",
         ESPN_FIGHTCENTER.format(id="600060493") + "?region=us&lang=en&contentorigin=espn"),
        ("fighter-search", f"{ESPN_SEARCH}?query={quote('Islam Makhachev')}&limit=3"),
    ]
    out = []
    for label, url in probes:
        t0 = time.perf_counter()
        try:
            async with session.get(
                url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
            ) as r:
                elapsed = int((time.perf_counter() - t0) * 1000)
                body = await r.text()
                json_ok = False
                if r.status == 200:
                    try:
                        import json
                        json.loads(body)
                        json_ok = True
                    except Exception:
                        pass
                detail = f"HTTP {r.status}, {elapsed}ms, json={'yes' if json_ok else 'no'}"
                if r.status != 200:
                    detail += f", body={body[:100].replace(chr(10), ' ')}"
                out.append((label, r.status == 200 and json_ok, detail))
        except Exception as exc:
            elapsed = int((time.perf_counter() - t0) * 1000)
            out.append((label, False, f"{type(exc).__name__}: {exc} ({elapsed}ms)"))
    return out


# ════════════════════════════════════════════════════════════════════════════
#  FIGHTER LOOKUP
# ════════════════════════════════════════════════════════════════════════════

async def get_fighter(session: aiohttp.ClientSession, name: str) -> Optional[dict]:
    """
    Resolve a fighter from multiple sources and merge.
    Works for ANY fighter, not just those on the current card.
    """
    espn = await _espn_fighter(session, name)
    sher = await _sherdog_fighter(session, name)

    if not espn and not sher:
        return None
    if espn and not sher:
        return espn
    if sher and not espn:
        return sher

    # merge — prefer whichever field has data, keep ESPN headshot + Sherdog history
    merged = dict(espn)
    for key in ("record", "height", "weight", "nickname", "weight_class"):
        if not merged.get(key) and sher.get(key):
            merged[key] = sher[key]
    if not merged.get("gym"):
        merged["gym"] = sher.get("association", "")
    if not merged.get("country"):
        merged["country"] = sher.get("nationality", "")
    # Sherdog has the real fight history
    if sher.get("fights"):
        merged["fights"] = sher["fights"]
    merged["source"] = "espn+sherdog"
    return merged


# ── ESPN fighter (search-first, scoreboard fallback) ──────────────────────────

async def _espn_fighter(session: aiohttp.ClientSession, name: str) -> Optional[dict]:
    candidate = await _espn_search_athlete(session, name)
    if candidate:
        aid = candidate.get("id")
        detail = None
        if aid:
            detail = await _espn_athlete_detail(session, aid)
        base = _espn_from_search(candidate)
        if detail:
            base = _merge_espn_detail(base, detail)
            stats = await _espn_athlete_stats(session, aid)
            if stats:
                base["stat_categories"] = stats
        return base

    # fallback: scan scoreboard (handles search API being unavailable)
    return await _espn_scoreboard_fighter(session, name)


async def _espn_search_athlete(session: aiohttp.ClientSession, name: str) -> Optional[dict]:
    # try both query param styles ESPN has used
    urls = [
        f"{ESPN_SEARCH}?query={quote(name)}&limit=10&mode=prefix",
        f"{ESPN_SEARCH}?query={quote(name)}&limit=10",
    ]
    data = None
    for u in urls:
        data = await _get_json(session, u)
        if data:
            break
    if not data:
        return None

    # flatten results, tagging each item with its group-level type/name
    items = []
    for group in data.get("results", []):
        gtag = _norm(group.get("type", "")) + " " + _norm(group.get("name", ""))
        for it in group.get("contents", []):
            it = dict(it)
            it["_gtag"] = gtag
            items.append(it)
    items.extend(data.get("items", []))

    def is_mma(it: dict) -> bool:
        sport = _norm(it.get("sport", ""))
        link = _norm(str(it.get("link", "")))
        return "mma" in sport or "/mma/" in link

    def is_player(it: dict) -> bool:
        blob = (
            _norm(it.get("type", "")) + _norm(it.get("subType", ""))
            + _norm(it.get("_gtag", "")) + _norm(str(it.get("link", "")))
        )
        return any(k in blob for k in ("player", "fighter", "athlete"))

    mma = [it for it in items if is_mma(it) and is_player(it)]
    if not mma:
        mma = [it for it in items if is_mma(it)]
    if not mma:
        return None

    # rank by name match so we never return the wrong fighter
    mma.sort(key=lambda it: _name_score(it.get("displayName", ""), name), reverse=True)
    if _name_score(mma[0].get("displayName", ""), name) <= 0:
        return None
    return _coerce_search_item(mma[0])


def _coerce_search_item(it: dict) -> dict:
    aid = it.get("id") or it.get("uid", "")
    # extract numeric id from link if needed:  /mma/fighter/_/id/2335639/jon-jones
    if not str(aid).isdigit():
        m = re.search(r"/id/(\d+)", str(it.get("link", "")))
        if m:
            aid = m.group(1)
        else:
            m2 = re.search(r"a:(\d+)", str(it.get("uid", "")))
            aid = m2.group(1) if m2 else ""
    image = ""
    img = it.get("image")
    if isinstance(img, dict):
        image = img.get("default", "") or img.get("href", "")
    elif isinstance(img, str):
        image = img
    return {
        "id": str(aid),
        "displayName": it.get("displayName", ""),
        "image": image,
        "subtitle": it.get("subtitle", "") or it.get("description", ""),
    }


def _espn_from_search(c: dict) -> dict:
    """Minimal profile from just the search hit (used if detail fetch fails)."""
    record = ""
    # subtitle sometimes looks like "26-1-0 • Light Heavyweight"
    sub = c.get("subtitle", "")
    m = re.search(r"\d+-\d+(-\d+)?", sub)
    if m:
        record = m.group(0)
    return {
        "name": c.get("displayName", "Unknown"),
        "nickname": "",
        "record": record,
        "weight_class": "",
        "height": "", "weight": "", "age": "",
        "country": "", "gym": "", "ranking": "",
        "headshot": c.get("image", ""),
        "stat_categories": [],
        "fights": [],
        "source": "espn",
    }


async def _espn_athlete_detail(session: aiohttp.ClientSession, aid: str) -> Optional[dict]:
    for tmpl in ESPN_ATHLETE_ENDPOINTS:
        data = await _get_json(session, tmpl.format(id=aid))
        if data:
            return data
    return None


def _merge_espn_detail(base: dict, data: dict) -> dict:
    a = data.get("athlete", data)

    def first_nonempty(*vals):
        for v in vals:
            if v:
                return v
        return ""

    record = first_nonempty(a.get("record"), a.get("displayRecord"))
    if isinstance(record, dict):
        record = record.get("displayValue", "")
    if isinstance(record, list) and record:
        record = record[0].get("summary", "") if isinstance(record[0], dict) else ""

    wc = a.get("weightClass", "")
    if isinstance(wc, dict):
        wc = wc.get("displayName", "")

    country = a.get("citizenship", "")
    if not country and isinstance(a.get("country"), dict):
        country = a["country"].get("name", "")
    if not country and isinstance(a.get("birthPlace"), dict):
        country = a["birthPlace"].get("country", "")

    gym = ""
    if isinstance(a.get("college"), dict):
        gym = a["college"].get("name", "")
    gym = gym or a.get("association", "")

    status = a.get("status", "")
    if isinstance(status, dict):
        status = status.get("name", "") or status.get("type", "")

    headshot = base.get("headshot", "")
    if not headshot and isinstance(a.get("headshot"), dict):
        headshot = a["headshot"].get("href", "")

    base.update({
        "name":         first_nonempty(a.get("displayName"), base.get("name")),
        "nickname":     first_nonempty(a.get("nickname"), base.get("nickname")),
        "record":       first_nonempty(record, base.get("record")),
        "weight_class": first_nonempty(wc, base.get("weight_class")),
        "height":       first_nonempty(a.get("displayHeight"), base.get("height")),
        "weight":       first_nonempty(a.get("displayWeight"), base.get("weight")),
        "age":          str(first_nonempty(a.get("age"), base.get("age"))),
        "country":      first_nonempty(country, base.get("country")),
        "gym":          first_nonempty(gym, base.get("gym")),
        "ranking":      str(first_nonempty(a.get("ranking"), base.get("ranking"))),
        "status":       status,
        "headshot":     headshot,
    })
    return base


async def _espn_athlete_stats(session: aiohttp.ClientSession, aid: str) -> list:
    data = await _get_json(session, ESPN_ATHLETE_STATS.format(id=aid))
    if not data:
        return []
    categories = []
    cats = (
        data.get("splits", {}).get("categories", [])
        or data.get("categories", [])
    )
    for cat in cats:
        stats = {}
        for s in cat.get("stats", []):
            label = s.get("shortDisplayName") or s.get("displayName") or s.get("name", "")
            value = s.get("displayValue", s.get("value", ""))
            if label and value not in ("", None):
                stats[label] = value
        if stats:
            categories.append({"name": cat.get("displayName", "Stats"), "stats": stats})
    return categories[:3]


async def _espn_scoreboard_fighter(session: aiohttp.ClientSession, name: str) -> Optional[dict]:
    events = await _scoreboard(session)
    target = _norm(name)
    for event in events:
        for comp in event.get("competitions", []):
            for entry in comp.get("competitors", []):
                ath = entry.get("athlete", {})
                disp = ath.get("displayName", "")
                if target in _norm(disp):
                    headshot = ""
                    if isinstance(ath.get("headshot"), dict):
                        headshot = ath["headshot"].get("href", "")
                    return {
                        "name": disp,
                        "nickname": "",
                        "record": _athlete_record(entry),
                        "weight_class": comp.get("type", {}).get("text", ""),
                        "height": "", "weight": "", "age": "",
                        "country": "", "gym": "", "ranking": "",
                        "headshot": headshot,
                        "stat_categories": [],
                        "fights": [],
                        "source": "espn",
                    }
    return None


# ── Sherdog fighter ───────────────────────────────────────────────────────────

_VALID_RESULTS = {"win", "loss", "loses", "lose", "draw", "nc", "n/a"}


def _name_score(text: str, query: str) -> int:
    """Score how well a fighter NAME matches the search query."""
    t, q = _norm(text), _norm(query)
    if not t:
        return -1
    if t == q:
        return 100
    if t.startswith(q):
        return 80
    if q in t:
        return 60
    qtok, ttok = set(q.split()), set(t.split())
    return len(qtok & ttok) * 25


async def _sherdog_fighter(session: aiohttp.ClientSession, name: str) -> Optional[dict]:
    url = await _sherdog_url(session, name)
    if not url:
        return None
    html = await _get_html(session, url)
    if not html:
        return None
    return _parse_sherdog(html, name)


async def _sherdog_url(session: aiohttp.ClientSession, name: str) -> Optional[str]:
    """
    Find a fighter's Sherdog profile, choosing the result whose NAME best
    matches the query — so searching "Conor McGregor" never returns some
    amateur whose *nickname* happens to be "Conor McGregor".
    """
    encoded = quote(name)
    for search_url in (
        f"{SHERDOG_BASE}/stats/fightfinder?SearchTxt={encoded}",
        f"{SHERDOG_BASE}/search/google/?q={encoded}",
    ):
        html = await _get_html(session, search_url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")

        candidates = []
        for a in soup.select("a[href*='/fighter/']"):
            link_name = a.get_text(strip=True)
            href = a.get("href", "")
            if href and link_name:
                candidates.append((_name_score(link_name, name), href))
        if not candidates:
            continue
        candidates.sort(key=lambda x: x[0], reverse=True)
        best_score, best_href = candidates[0]
        if best_score <= 0:
            continue  # no decent name match here -> try next search method
        return (SHERDOG_BASE + best_href) if best_href.startswith("/") else best_href

    return None


def _parse_record(soup) -> tuple:
    """Return (wins, losses, draws, nc) across several Sherdog layouts."""
    wins = losses = draws = nc = "0"

    # modern: .winloses  ->  <span>label</span><span>number</span>
    wl = soup.select(".winloses")
    if wl:
        for div in wl:
            spans = div.select("span")
            if len(spans) >= 2:
                label = spans[0].get_text(strip=True).lower()
                num = spans[-1].get_text(strip=True)
                if not num.isdigit():
                    continue
                if "win" in label:
                    wins = num
                elif "los" in label:
                    losses = num
                elif "draw" in label:
                    draws = num
                elif "nc" in label or "contest" in label:
                    nc = num
        if wins != "0" or losses != "0":
            return wins, losses, draws, nc

    # older: .bio_graph .counter
    counters = soup.select(".bio_graph .counter")
    if counters:
        vals = [c.get_text(strip=True) for c in counters]
        wins   = vals[0] if len(vals) > 0 else "0"
        losses = vals[1] if len(vals) > 1 else "0"
        draws  = vals[2] if len(vals) > 2 else "0"
        nc     = vals[3] if len(vals) > 3 else "0"
        return wins, losses, draws, nc

    return wins, losses, draws, nc


def _parse_fights(soup) -> list:
    """Parse recent fight history, skipping header rows and splitting method/referee."""
    fights = []
    table = None
    for sel in ("table.new_table.fighter", "table.new_table.result",
                ".module.fight_history table", "table[class*='result']"):
        table = soup.select_one(sel)
        if table:
            break
    if not table:
        return fights

    for row in table.select("tr"):
        if row.find("th"):           # header row
            continue
        cells = row.select("td")
        if len(cells) < 4:
            continue

        res_el = row.select_one(".final_result") or cells[0]
        result = res_el.get_text(strip=True).lower()
        if result not in _VALID_RESULTS:   # guards against stray header/spacer rows
            continue
        if result in ("loses", "lose"):
            result = "loss"

        opp_link = cells[1].select_one("a")
        opponent = (opp_link.get_text(strip=True) if opp_link
                    else cells[1].get_text(strip=True))

        # method cell also holds the referee in a .sub_line — separate them
        method_cell = cells[3]
        sub = method_cell.select_one(".sub_line")
        referee = sub.get_text(strip=True) if sub else ""
        if sub:
            sub.extract()
        method = method_cell.get_text(" ", strip=True)

        # event cell similarly holds the date in a .sub_line
        event_cell = cells[2]
        ev_link = event_cell.select_one("a")
        event = ev_link.get_text(strip=True) if ev_link else event_cell.get_text(" ", strip=True)

        fights.append({
            "result":   result,
            "opponent": opponent,
            "event":    event,
            "method":   method,
            "referee":  referee,
            "round":    cells[4].get_text(strip=True) if len(cells) > 4 else "",
            "time":     cells[5].get_text(strip=True) if len(cells) > 5 else "",
        })
        if len(fights) >= 5:
            break
    return fights


def _parse_sherdog(html: str, fallback_name: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    def txt(*selectors) -> str:
        for sel in selectors:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                return el.get_text(strip=True)
        return ""

    wins, losses, draws, nc = _parse_record(soup)
    fights = _parse_fights(soup)

    return {
        "name":         txt("span.fn", ".fn", "h1.fighter-title span", "h1[itemprop='name']", "h1") or fallback_name,
        "nickname":     txt("span.nickname em", ".nickname em", "[class*='nickname'] em"),
        "nationality":  txt("[itemprop='nationality']", ".item.birthplace .nationality", "strong[itemprop='nationality']"),
        "birthdate":    txt("[itemprop='birthDate']", ".item.birthday time"),
        "height":       txt("[itemprop='height']", ".item.height strong", "[data-key='height']"),
        "weight":       txt("[itemprop='weight']", ".item.weight strong", "[data-key='weight']"),
        "association":  txt(".association span[itemprop='name']", ".association .name", "[class*='association'] a"),
        "weight_class": txt(".association_class", ".wclass a", ".weight_class"),
        "wins": wins, "losses": losses, "draws": draws, "nc": nc,
        "record": f"{wins}-{losses}-{draws}",
        "fights": fights,
        "source": "sherdog",
    }
