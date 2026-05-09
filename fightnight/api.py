"""
UFC data fetching.
- ESPN undocumented API for events/cards/results
- Sherdog for fighter bios and fight history
"""
import aiohttp
from datetime import datetime, timezone
from typing import Optional
from bs4 import BeautifulSoup

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
ESPN_ATHLETE    = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/athletes/{id}"
SHERDOG_BASE    = "https://www.sherdog.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
}


# ── low-level helpers ─────────────────────────────────────────────────────────

async def _get_json(session: aiohttp.ClientSession, url: str) -> Optional[dict]:
    try:
        async with session.get(url, headers=HEADERS,
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status == 200:
                return await r.json(content_type=None)
    except Exception:
        pass
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


# ── ESPN scoreboard ───────────────────────────────────────────────────────────

async def _scoreboard(session: aiohttp.ClientSession) -> list:
    """Return raw list of ESPN events."""
    data = await _get_json(session, ESPN_SCOREBOARD)
    return data.get("events", []) if data else []


def _parse_date(event: dict) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
    except Exception:
        return None


def _fmt_event(raw: dict) -> dict:
    """Turn a raw ESPN event dict into a clean one the cog uses."""
    fights = []
    for comp in raw.get("competitions", []):
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue
        red_c, blue_c = competitors[0], competitors[1]

        # winner / result
        winner = ""
        for c in competitors:
            if c.get("winner"):
                winner = c.get("athlete", {}).get("displayName", "")

        # method from notes
        method = ""
        for note in comp.get("notes", []):
            t = note.get("text", "")
            if any(k in t.lower() for k in ["ko", "tko", "sub", "decision", "round"]):
                method = t
                break

        status = comp.get("status", {})
        fights.append({
            "red":        red_c.get("athlete", {}).get("displayName", "TBD"),
            "blue":       blue_c.get("athlete", {}).get("displayName", "TBD"),
            "red_id":     red_c.get("athlete", {}).get("id", ""),
            "blue_id":    blue_c.get("athlete", {}).get("id", ""),
            "red_record": _athlete_record(red_c),
            "blue_record":_athlete_record(blue_c),
            "weight_class": comp.get("type", {}).get("text", ""),
            "is_title":   "title" in comp.get("type", {}).get("text", "").lower(),
            "winner":     winner,
            "method":     method,
            "round":      str(status.get("period", "")),
            "time":       status.get("displayClock", ""),
        })

    dt = _parse_date(raw)
    return {
        "id":        raw.get("id", ""),
        "name":      raw.get("name", "UFC Event"),
        "shortname": raw.get("shortName", raw.get("name", "UFC Event")),
        "date":      dt.strftime("%B %d, %Y") if dt else "",
        "timestamp": int(dt.timestamp()) if dt else None,
        "location":  _event_location(raw),
        "fights":    fights,
    }


def _athlete_record(competitor: dict) -> str:
    for s in competitor.get("statistics", []):
        if s.get("name") == "record":
            return s.get("displayValue", "")
    return competitor.get("athlete", {}).get("record", "")


def _event_location(raw: dict) -> str:
    comps = raw.get("competitions", [])
    if comps:
        venue = comps[0].get("venue", {})
        return venue.get("fullName", "") or raw.get("location", "")
    return raw.get("location", "")


# ── public event functions ────────────────────────────────────────────────────

async def get_upcoming_event(session: aiohttp.ClientSession) -> Optional[dict]:
    now = datetime.now(timezone.utc)
    events = await _scoreboard(session)
    upcoming = []
    for e in events:
        dt = _parse_date(e)
        if dt and dt >= now:
            upcoming.append((dt, e))
    if not upcoming:
        return None
    upcoming.sort(key=lambda x: x[0])
    return _fmt_event(upcoming[0][1])


async def get_recent_event(session: aiohttp.ClientSession) -> Optional[dict]:
    now = datetime.now(timezone.utc)
    events = await _scoreboard(session)
    past = []
    for e in events:
        dt = _parse_date(e)
        if dt and dt < now:
            past.append((dt, e))
    if not past:
        # fallback: just return last event in list
        return _fmt_event(events[-1]) if events else None
    past.sort(key=lambda x: x[0], reverse=True)
    return _fmt_event(past[0][1])


# ── fighter lookup ────────────────────────────────────────────────────────────

async def get_fighter(session: aiohttp.ClientSession, name: str) -> Optional[dict]:
    """
    Try ESPN first (athletes on current card), then Sherdog.
    Returns merged result.
    """
    espn   = await _espn_fighter(session, name)
    sherd  = await _sherdog_fighter(session, name)

    if not espn and not sherd:
        return None

    if espn and sherd:
        # merge: ESPN bio + Sherdog fights/extra bio
        if not espn.get("record"):
            espn["record"] = sherd.get("record", "")
        if not espn.get("height"):
            espn["height"] = sherd.get("height", "")
        if not espn.get("weight"):
            espn["weight"] = sherd.get("weight", "")
        if not espn.get("gym"):
            espn["gym"] = sherd.get("association", "")
        espn["fights"] = sherd.get("fights", [])
        return espn

    return espn or sherd


# ── ESPN fighter ──────────────────────────────────────────────────────────────

async def _espn_fighter(session: aiohttp.ClientSession, name: str) -> Optional[dict]:
    """Find a fighter by scanning the current ESPN scoreboard for their name."""
    events = await _scoreboard(session)
    name_lower = name.lower().strip()

    for event in events:
        for comp in event.get("competitions", []):
            for comp_entry in comp.get("competitors", []):
                athlete = comp_entry.get("athlete", {})
                display = athlete.get("displayName", "")
                if name_lower in display.lower():
                    aid = athlete.get("id")
                    if aid:
                        detail = await _get_json(
                            session, ESPN_ATHLETE.format(id=aid)
                        )
                        if detail:
                            return _parse_espn_athlete(detail, comp_entry)
                    # fallback: minimal profile from scoreboard data
                    return {
                        "name":         display,
                        "nickname":     "",
                        "record":       _athlete_record(comp_entry),
                        "weight_class": comp.get("type", {}).get("text", ""),
                        "height": "", "weight": "", "age": "",
                        "country": "", "gym": "", "ranking": "",
                        "headshot": (
                            athlete.get("headshot", {}).get("href", "")
                            if isinstance(athlete.get("headshot"), dict) else ""
                        ),
                        "stat_categories": [],
                        "fights": [],
                        "source": "espn",
                    }
    return None


def _parse_espn_athlete(data: dict, comp_entry: dict) -> dict:
    a = data.get("athlete", data)

    record = a.get("record") or a.get("displayRecord", "")

    wc = a.get("weightClass", "")
    if isinstance(wc, dict):
        wc = wc.get("displayName", "")

    country = a.get("citizenship", "")
    if not country and isinstance(a.get("country"), dict):
        country = a["country"].get("name", "")

    gym = ""
    if isinstance(a.get("college"), dict):
        gym = a["college"].get("name", "")

    status = ""
    if isinstance(a.get("status"), dict):
        status = a["status"].get("name", "")

    headshot = ""
    if isinstance(a.get("headshot"), dict):
        headshot = a["headshot"].get("href", "")

    return {
        "name":         a.get("displayName", "Unknown"),
        "nickname":     a.get("nickname", ""),
        "record":       record,
        "weight_class": wc,
        "height":       a.get("displayHeight", ""),
        "weight":       a.get("displayWeight", ""),
        "age":          str(a.get("age", "")),
        "country":      country,
        "gym":          gym,
        "ranking":      str(a.get("ranking", "")),
        "status":       status,
        "headshot":     headshot,
        "stat_categories": [],
        "fights": [],
        "source": "espn",
    }


# ── Sherdog fighter ───────────────────────────────────────────────────────────

async def _sherdog_fighter(session: aiohttp.ClientSession, name: str) -> Optional[dict]:
    url = await _sherdog_search_url(session, name)
    if not url:
        return None
    html = await _get_html(session, url)
    if not html:
        return None
    return _parse_sherdog_page(html, name)


async def _sherdog_search_url(session: aiohttp.ClientSession, name: str) -> Optional[str]:
    encoded = name.replace(" ", "+")

    # Try 1: fightfinder
    html = await _get_html(
        session,
        f"{SHERDOG_BASE}/stats/fightfinder?SearchTxt={encoded}"
    )
    if html:
        soup = BeautifulSoup(html, "html.parser")
        link = (
            soup.select_one("table.fightfinder_result a[href*='/fighter/']")
            or soup.select_one("a[href*='/fighter/']")
        )
        if link:
            href = link["href"]
            return (SHERDOG_BASE + href) if href.startswith("/") else href

    # Try 2: google-proxy search
    html2 = await _get_html(
        session,
        f"{SHERDOG_BASE}/search/google/?q={encoded}"
    )
    if html2:
        soup2 = BeautifulSoup(html2, "html.parser")
        link = soup2.select_one("a[href*='/fighter/']")
        if link:
            href = link["href"]
            return (SHERDOG_BASE + href) if href.startswith("/") else href

    return None


def _parse_sherdog_page(html: str, fallback_name: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    def txt(*selectors) -> str:
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                return el.get_text(strip=True)
        return ""

    # Record
    wins = losses = draws = nc = "0"
    graphs = soup.select(".bio_graph")
    if graphs:
        vals = []
        for g in graphs:
            c = g.select_one(".counter")
            vals.append(c.get_text(strip=True) if c else "0")
        wins    = vals[0] if len(vals) > 0 else "0"
        losses  = vals[1] if len(vals) > 1 else "0"
        draws   = vals[2] if len(vals) > 2 else "0"
        nc      = vals[3] if len(vals) > 3 else "0"

    # Fight history
    fights = []
    for sel in ["table.new_table.result", "table[class*='result']"]:
        rows = soup.select(f"{sel} tr")[1:6]
        if rows:
            for row in rows:
                cols = row.select("td")
                if len(cols) >= 4:
                    fights.append({
                        "result":   cols[0].get_text(strip=True),
                        "opponent": cols[1].get_text(strip=True),
                        "event":    cols[2].get_text(strip=True),
                        "method":   cols[3].get_text(strip=True),
                        "round":    cols[4].get_text(strip=True) if len(cols) > 4 else "",
                        "time":     cols[5].get_text(strip=True) if len(cols) > 5 else "",
                    })
            break

    return {
        "name":        txt(".fn", "h1[itemprop='name']", "h1") or fallback_name,
        "nickname":    txt(".nickname em"),
        "nationality": txt("[itemprop='nationality']"),
        "birthdate":   txt("[itemprop='birthDate']"),
        "height":      txt("[data-key='height']", ".height"),
        "weight":      txt("[data-key='weight']", ".weight"),
        "association": txt(".association .name"),
        "weight_class":txt(".wclass a", ".weight_class"),
        "wins":    wins,
        "losses":  losses,
        "draws":   draws,
        "nc":      nc,
        "record":  f"{wins}-{losses}-{draws}",
        "fights":  fights,
        "source":  "sherdog",
    }
