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
SHERDOG_SEARCH = "https://www.sherdog.com/search/google/?q="
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

    # fallback: grab last scoreboard event if none past
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
    """Search ESPN for a fighter by name."""
    data = await fetch_json(
        session,
        f"{ESPN_BASE}/athletes",
        params={"limit": 5, "search": name}
    )
    if not data:
        return None

    athletes = data.get("athletes", [])
    if not athletes:
        return None

    # Best match: first result
    athlete = athletes[0]
    athlete_id = athlete.get("id")
    if not athlete_id:
        return None

    return await get_fighter_by_id(session, athlete_id)


async def get_fighter_by_id(session: aiohttp.ClientSession, athlete_id: str) -> Optional[dict]:
    data = await fetch_json(session, f"{ESPN_BASE}/athletes/{athlete_id}")
    if not data:
        return None

    athlete = data.get("athlete", data)
    stats_data = await fetch_json(session, f"{ESPN_BASE}/athletes/{athlete_id}/statistics")

    record = athlete.get("record", "")
    if not record:
        record_data = athlete.get("displayRecord", "")
        record = record_data

    categories = []
    if stats_data:
        cats = stats_data.get("splits", {}).get("categories", [])
        for cat in cats:
            cat_name = cat.get("displayName", "")
            stats = {}
            for s in cat.get("stats", []):
                stats[s.get("shortDisplayName", s.get("name", ""))] = s.get("displayValue", s.get("value", ""))
            if stats:
                categories.append({"name": cat_name, "stats": stats})

    flag = ""
    country = athlete.get("citizenship", "") or athlete.get("country", {}).get("name", "")

    return {
        "id": athlete_id,
        "name": athlete.get("displayName", "Unknown"),
        "nickname": athlete.get("nickname", ""),
        "record": record,
        "weight_class": athlete.get("weightClass", {}).get("displayName", "") if isinstance(athlete.get("weightClass"), dict) else athlete.get("weightClass", ""),
        "height": athlete.get("displayHeight", ""),
        "weight": athlete.get("displayWeight", ""),
        "age": athlete.get("age", ""),
        "country": country,
        "gym": athlete.get("college", {}).get("name", "") if isinstance(athlete.get("college"), dict) else "",
        "ranking": athlete.get("ranking", ""),
        "status": athlete.get("status", {}).get("name", "") if isinstance(athlete.get("status"), dict) else "",
        "headshot": athlete.get("headshot", {}).get("href", "") if isinstance(athlete.get("headshot"), dict) else "",
        "stat_categories": categories,
    }


# ─── Sherdog fallback: Fighter scrape ─────────────────────────────────────────

async def get_fighter_sherdog(session: aiohttp.ClientSession, name: str) -> Optional[dict]:
    """Fallback: scrape Sherdog for fighter data."""
    search_url = f"https://www.sherdog.com/search/google/?q={name.replace(' ', '+')}"
    html = await fetch_html(session, search_url)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    # Find first fighter result link
    link = soup.select_one("a[href*='/fighter/']")
    if not link:
        return None

    fighter_url = SHERDOG_BASE + link["href"] if link["href"].startswith("/") else link["href"]
    html2 = await fetch_html(session, fighter_url)
    if not html2:
        return None

    soup2 = BeautifulSoup(html2, "html.parser")

    def text(sel):
        el = soup2.select_one(sel)
        return el.get_text(strip=True) if el else ""

    record_section = soup2.select_one(".record")
    wins = losses = draws = nc = "0"
    if record_section:
        nums = record_section.select(".bio_graph")
        if len(nums) >= 2:
            wins = nums[0].select_one(".counter").get_text(strip=True) if nums[0].select_one(".counter") else "0"
            losses = nums[1].select_one(".counter").get_text(strip=True) if nums[1].select_one(".counter") else "0"
        if len(nums) >= 3:
            draws = nums[2].select_one(".counter").get_text(strip=True) if nums[2].select_one(".counter") else "0"
        if len(nums) >= 4:
            nc = nums[3].select_one(".counter").get_text(strip=True) if nums[3].select_one(".counter") else "0"

    # Fight history (last 5)
    fights = []
    rows = soup2.select("table.new_table.result tr")[1:6]
    for row in rows:
        cols = row.select("td")
        if len(cols) >= 6:
            fights.append({
                "result": cols[0].get_text(strip=True),
                "opponent": cols[1].get_text(strip=True),
                "event": cols[2].get_text(strip=True),
                "method": cols[3].get_text(strip=True),
                "round": cols[4].get_text(strip=True),
                "time": cols[5].get_text(strip=True),
            })

    return {
        "name": text(".fn") or text("h1[itemprop='name']") or name,
        "nickname": text(".nickname em"),
        "nationality": text("[itemprop='nationality']"),
        "birthdate": text("[itemprop='birthDate']"),
        "height": text("[data-key='height']") or text(".item_stat_holder .height"),
        "weight": text("[data-key='weight']") or "",
        "association": text(".association .name"),
        "weight_class": text(".wclass a") or text(".weight_class"),
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "nc": nc,
        "record": f"{wins}-{losses}-{draws}",
        "fights": fights,
        "source": "sherdog",
    }
