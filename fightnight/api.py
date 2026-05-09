"""
UFC API module - fetches data from ESPN's undocumented MMA API and Sherdog for fighter stats.
"""
import aiohttp
import asyncio
import re
from datetime import datetime, timezone
from typing import Optional
from bs4 import BeautifulSoup

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc"
SHERDOG_BASE = "https://www.sherdog.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict = None) -> Optional[dict]:
    try:
        async with session.get(url, params=params, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
    except Exception:
        pass
    return None


async def fetch_html(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                return await resp.text()
    except Exception:
        pass
    return None


# ─── ESPN: Upcoming Event / Fight Card ────────────────────────────────────────

async def get_upcoming_event(session: aiohttp.ClientSession) -> Optional[dict]:
    """Fetch the next scheduled UFC event from ESPN."""
    data = await fetch_json(session, f"{ESPN_BASE}/scoreboard")
    if not data:
        return None

    events = data.get("events", [])
    now = datetime.now(timezone.utc)

    upcoming = []
    past = []

    for event in events:
        date_str = event.get("date", "")
        try:
            event_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if event_date >= now:
            upcoming.append((event_date, event))
        else:
            past.append((event_date, event))

    if upcoming:
        upcoming.sort(key=lambda x: x[0])
        return _parse_event(upcoming[0][1], is_upcoming=True)
    return None


async def get_recent_event(session: aiohttp.ClientSession) -> Optional[dict]:
    """Fetch the most recent completed UFC event from ESPN."""
    data = await fetch_json(session, f"{ESPN_BASE}/scoreboard")
    if not data:
        return None

    events = data.get("events", [])
    now = datetime.now(timezone.utc)
    past = []

    for event in events:
        date_str = event.get("date", "")
        try:
            event_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if event_date < now:
            past.append((event_date, event))

    if past:
        past.sort(key=lambda x: x[0], reverse=True)
        return _parse_event(past[0][1], is_upcoming=False)

    if events:
        return _parse_event(events[-1], is_upcoming=False)
    return None


def _parse_event(event: dict, is_upcoming: bool) -> dict:
    """Parse a raw ESPN event into a clean dict."""
    competitions = event.get("competitions", [])
    fights = []

    for comp in competitions:
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue

        red = competitors[0]
        blue = competitors[1]

        fight = {
            "red_name": red.get("athlete", {}).get("displayName", "TBD"),
            "blue_name": blue.get("athlete", {}).get("displayName", "TBD"),
            "red_record": _get_record(red),
            "blue_record": _get_record(blue),
            "weight_class": comp.get("type", {}).get("text", ""),
            "is_main_event": comp.get("headlines", [{}])[0].get("shortLinkText", "").lower().startswith("main") if comp.get("headlines") else False,
            "is_title": "title" in comp.get("type", {}).get("text", "").lower(),
            "status": comp.get("status", {}).get("type", {}).get("name", ""),
            "result": _get_result(comp) if not is_upcoming else None,
            "athlete_ids": [
                red.get("athlete", {}).get("id", ""),
                blue.get("athlete", {}).get("id", ""),
            ],
        }
        fights.append(fight)

    date_str = event.get("date", "")
    try:
        event_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        formatted_date = event_date.strftime("%B %d, %Y")
        timestamp = int(event_date.timestamp())
    except Exception:
        formatted_date = date_str
        timestamp = None

    venue = event.get("competitions", [{}])[0].get("venue", {}) if event.get("competitions") else {}

    return {
        "name": event.get("name", "UFC Event"),
        "short_name": event.get("shortName", event.get("name", "UFC Event")),
        "date": formatted_date,
        "timestamp": timestamp,
        "location": venue.get("fullName", "") or event.get("location", ""),
        "fights": fights,
        "id": event.get("id", ""),
    }


def _get_record(competitor: dict) -> str:
    stats = competitor.get("statistics", [])
    for stat in stats:
        if stat.get("name") == "record":
            return stat.get("displayValue", "")
    return competitor.get("athlete", {}).get("record", "")


def _get_result(comp: dict) -> Optional[dict]:
    competitors = comp.get("competitors", [])
    if len(competitors) < 2:
        return None

    winner = None
    method = ""
    round_num = ""
    time = ""

    for c in competitors:
        if c.get("winner"):
            winner = c.get("athlete", {}).get("displayName", "")

    notes = comp.get("notes", [])
    for note in notes:
        text = note.get("text", "")
        if "round" in text.lower() or any(m in text.lower() for m in ["ko", "tko", "sub", "decision"]):
            method = text

    status = comp.get("status", {})
    period = status.get("period", "")
    clock = status.get("displayClock", "")
    if period:
        round_num = str(period)
    if clock:
        time = clock

    return {
        "winner": winner,
        "method": method,
        "round": round_num,
        "time": time,
    }


# ─── ESPN: Fighter Stats ───────────────────────────────────────────────────────

async def search_fighter_espn(session: aiohttp.ClientSession, name: str) -> Optional[dict]:
    """
    Search ESPN for a fighter by scanning the scoreboard for their name.
    ESPN's /athletes search endpoint is unreliable so we match against
    fighters appearing in current/upcoming events instead.
    """
    data = await fetch_json(session, f"{ESPN_BASE}/scoreboard")
    if not data:
        return None

    name_lower = name.lower().strip()
    best_basic = None

    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            for competitor in comp.get("competitors", []):
                athlete = competitor.get("athlete", {})
                display_name = athlete.get("displayName", "")
                if name_lower in display_name.lower():
                    athlete_id = athlete.get("id")
                    if athlete_id:
                        result = await get_fighter_by_id(session, athlete_id)
                        if result:
                            return result
                    # Store a minimal profile as fallback
                    if not best_basic and display_name:
                        best_basic = {
                            "name": display_name,
                            "nickname": "",
                            "record": _get_record(competitor),
                            "weight_class": comp.get("type", {}).get("text", ""),
                            "height": "", "weight": "", "age": "",
                            "country": "", "gym": "", "ranking": "",
                            "status": "",
                            "headshot": athlete.get("headshot", {}).get("href", "")
                                if isinstance(athlete.get("headshot"), dict) else "",
                            "stat_categories": [],
                        }

    return best_basic  # None if not on any current card


async def get_fighter_by_id(session: aiohttp.ClientSession, athlete_id: str) -> Optional[dict]:
    data = await fetch_json(session, f"{ESPN_BASE}/athletes/{athlete_id}")
    if not data:
        return None

    athlete = data.get("athlete", data)
    stats_data = await fetch_json(session, f"{ESPN_BASE}/athletes/{athlete_id}/statistics")

    record = athlete.get("record", "") or athlete.get("displayRecord", "")

    categories = []
    if stats_data:
        for cat in stats_data.get("splits", {}).get("categories", []):
            cat_name = cat.get("displayName", "")
            stats = {}
            for s in cat.get("stats", []):
                stats[s.get("shortDisplayName", s.get("name", ""))] = s.get("displayValue", s.get("value", ""))
            if stats:
                categories.append({"name": cat_name, "stats": stats})

    country = athlete.get("citizenship", "") or (
        athlete.get("country", {}).get("name", "") if isinstance(athlete.get("country"), dict) else ""
    )

    return {
        "id": athlete_id,
        "name": athlete.get("displayName", "Unknown"),
        "nickname": athlete.get("nickname", ""),
        "record": record,
        "weight_class": (
            athlete.get("weightClass", {}).get("displayName", "")
            if isinstance(athlete.get("weightClass"), dict)
            else athlete.get("weightClass", "")
        ),
        "height": athlete.get("displayHeight", ""),
        "weight": athlete.get("displayWeight", ""),
        "age": athlete.get("age", ""),
        "country": country,
        "gym": (
            athlete.get("college", {}).get("name", "")
            if isinstance(athlete.get("college"), dict)
            else ""
        ),
        "ranking": athlete.get("ranking", ""),
        "status": (
            athlete.get("status", {}).get("name", "")
            if isinstance(athlete.get("status"), dict)
            else ""
        ),
        "headshot": (
            athlete.get("headshot", {}).get("href", "")
            if isinstance(athlete.get("headshot"), dict)
            else ""
        ),
        "stat_categories": categories,
    }


# ─── Sherdog: Fighter scrape ──────────────────────────────────────────────────

async def get_fighter_sherdog(session: aiohttp.ClientSession, name: str) -> Optional[dict]:
    """Scrape Sherdog for fighter data. Tries two search methods."""
    fighter_url = await _find_sherdog_url(session, name)
    if not fighter_url:
        return None

    page_html = await fetch_html(session, fighter_url)
    if not page_html:
        return None

    soup = BeautifulSoup(page_html, "html.parser")

    def text(sel):
        el = soup.select_one(sel)
        return el.get_text(strip=True) if el else ""

    # Record — try multiple known Sherdog layouts
    wins = losses = draws = nc = "0"
    bio_graphs = soup.select(".bio_graph")
    if bio_graphs:
        counters = [bg.select_one(".counter") for bg in bio_graphs]
        vals = [c.get_text(strip=True) if c else "0" for c in counters]
        if len(vals) > 0: wins = vals[0]
        if len(vals) > 1: losses = vals[1]
        if len(vals) > 2: draws = vals[2]
        if len(vals) > 3: nc = vals[3]
    else:
        rec_el = soup.select_one(".record span") or soup.select_one("[class*='record']")
        if rec_el:
            parts = rec_el.get_text(strip=True).split("-")
            if len(parts) >= 2:
                wins, losses = parts[0].strip(), parts[1].strip()
            if len(parts) >= 3:
                draws = parts[2].strip()

    # Fight history — try a few table selectors Sherdog has used over the years
    fights = []
    for table_sel in ["table.new_table.result", "table[class*='result']", ".fighter-record table"]:
        rows = soup.select(f"{table_sel} tr")[1:6]
        if rows:
            for row in rows:
                cols = row.select("td")
                if len(cols) >= 4:
                    fights.append({
                        "result": cols[0].get_text(strip=True),
                        "opponent": cols[1].get_text(strip=True),
                        "event": cols[2].get_text(strip=True),
                        "method": cols[3].get_text(strip=True),
                        "round": cols[4].get_text(strip=True) if len(cols) > 4 else "",
                        "time": cols[5].get_text(strip=True) if len(cols) > 5 else "",
                    })
            break

    fighter_name = (
        text(".fn")
        or text("h1[itemprop='name']")
        or text("h1.hero-profile__name")
        or text("h1")
        or name
    )

    return {
        "name": fighter_name,
        "nickname": text(".nickname em") or text("[class*='nickname']"),
        "nationality": text("[itemprop='nationality']") or text("[class*='nationality']"),
        "birthdate": text("[itemprop='birthDate']") or text("[class*='birthdate']"),
        "height": (
            text("[data-key='height']")
            or text(".item_stat_holder .height")
            or text("[class*='height']")
        ),
        "weight": text("[data-key='weight']") or text("[class*='weight']"),
        "association": text(".association .name") or text("[class*='association']"),
        "weight_class": (
            text(".wclass a") or text(".weight_class") or text("[class*='weight-class']")
        ),
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "nc": nc,
        "record": f"{wins}-{losses}-{draws}",
        "fights": fights,
        "source": "sherdog",
    }


async def _find_sherdog_url(session: aiohttp.ClientSession, name: str) -> Optional[str]:
    """Try to locate a fighter's Sherdog profile URL via their search pages."""
    encoded = name.replace(" ", "+")

    # Method 1: Sherdog FightFinder search
    html = await fetch_html(
        session,
        f"https://www.sherdog.com/stats/fightfinder?SearchTxt={encoded}"
    )
    if html:
        soup = BeautifulSoup(html, "html.parser")
        link = soup.select_one("table.fightfinder_result a[href*='/fighter/']")
        if not link:
            link = soup.select_one("a[href*='/fighter/']")
        if link:
            href = link.get("href", "")
            return (SHERDOG_BASE + href) if href.startswith("/") else href

    # Method 2: Sherdog Google-proxy search
    html2 = await fetch_html(
        session,
        f"https://www.sherdog.com/search/google/?q={encoded}"
    )
    if html2:
        soup2 = BeautifulSoup(html2, "html.parser")
        link = soup2.select_one("a[href*='/fighter/']")
        if link:
            href = link.get("href", "")
            return (SHERDOG_BASE + href) if href.startswith("/") else href

    return None
