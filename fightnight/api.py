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
import aiohttp
from urllib.parse import quote
from datetime import datetime, timezone
from typing import Optional
from bs4 import BeautifulSoup

# ── endpoints ─────────────────────────────────────────────────────────────────
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
ESPN_SEARCH     = "https://site.web.api.espn.com/apis/common/v3/search"

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
    "Accept": "application/json, text/html, */*",
}


# ── low-level fetch helpers ───────────────────────────────────────────────────

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


def _norm(s: str) -> str:
    """Lowercase, strip, collapse whitespace — for name comparison."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# ════════════════════════════════════════════════════════════════════════════
#  EVENTS  (card / results)
# ════════════════════════════════════════════════════════════════════════════

async def _scoreboard(session: aiohttp.ClientSession) -> list:
    data = await _get_json(session, ESPN_SCOREBOARD)
    return data.get("events", []) if data else []


def _parse_date(event: dict) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
    except Exception:
        return None


def _athlete_record(competitor: dict) -> str:
    for s in competitor.get("statistics", []):
        if s.get("name") == "record":
            return s.get("displayValue", "")
    rec = competitor.get("records")
    if isinstance(rec, list) and rec:
        return rec[0].get("summary", "")
    return competitor.get("athlete", {}).get("record", "") or ""


def _event_location(raw: dict) -> str:
    comps = raw.get("competitions", [])
    if comps:
        venue = comps[0].get("venue", {})
        loc = venue.get("fullName", "")
        addr = venue.get("address", {})
        city = addr.get("city", "")
        if loc and city:
            return f"{loc} — {city}"
        return loc or raw.get("location", "")
    return raw.get("location", "")


def _fmt_event(raw: dict) -> dict:
    fights = []
    for comp in raw.get("competitions", []):
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue
        red_c, blue_c = competitors[0], competitors[1]

        winner = ""
        for c in competitors:
            if c.get("winner"):
                winner = c.get("athlete", {}).get("displayName", "")

        method = ""
        for note in comp.get("notes", []):
            t = note.get("text", "")
            if any(k in t.lower() for k in ["ko", "tko", "sub", "decision", "round"]):
                method = t
                break
        # some payloads expose method on the status type
        if not method:
            st = comp.get("status", {}).get("type", {})
            method = st.get("description", "") if st.get("completed") else ""

        status = comp.get("status", {})
        completed = bool(status.get("type", {}).get("completed"))

        fights.append({
            "red":         red_c.get("athlete", {}).get("displayName", "TBD"),
            "blue":        blue_c.get("athlete", {}).get("displayName", "TBD"),
            "red_record":  _athlete_record(red_c),
            "blue_record": _athlete_record(blue_c),
            "weight_class": comp.get("type", {}).get("text", "") or comp.get("note", ""),
            "is_title":    "title" in (comp.get("type", {}).get("text", "") or "").lower(),
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


async def get_upcoming_event(session: aiohttp.ClientSession) -> Optional[dict]:
    now = datetime.now(timezone.utc)
    events = await _scoreboard(session)
    upcoming = [(d, e) for e in events if (d := _parse_date(e)) and d >= now]
    if upcoming:
        upcoming.sort(key=lambda x: x[0])
        return _fmt_event(upcoming[0][1])
    return None


async def get_recent_event(session: aiohttp.ClientSession) -> Optional[dict]:
    """Most recent PAST event in the scoreboard window. Never returns a future card."""
    now = datetime.now(timezone.utc)
    events = await _scoreboard(session)
    past = [(d, e) for e in events if (d := _parse_date(e)) and d < now]
    if not past:
        return None  # do NOT fall back to a future event
    past.sort(key=lambda x: x[0], reverse=True)
    return _fmt_event(past[0][1])


async def get_event_on_date(session: aiohttp.ClientSession, ymd: str) -> Optional[dict]:
    """
    Fetch the event on a specific date (ymd = 'YYYYMMDD') via ESPN's dated
    scoreboard. Works even after the event has dropped out of the default window
    — this is what lets settle find the RIGHT past card.
    """
    if not ymd:
        return None
    data = await _get_json(session, f"{ESPN_SCOREBOARD}?dates={ymd}")
    events = data.get("events", []) if data else []
    if not events:
        return None
    return _fmt_event(events[0])


async def get_event_by_id(session: aiohttp.ClientSession, eid: str,
                          ymd: str = "") -> Optional[dict]:
    """Fetch a specific event by id, using its date to query the dated scoreboard."""
    eid = str(eid)
    if ymd:
        data = await _get_json(session, f"{ESPN_SCOREBOARD}?dates={ymd}")
        for e in (data or {}).get("events", []):
            if str(e.get("id")) == eid:
                return _fmt_event(e)
        evs = (data or {}).get("events", [])
        if evs:
            return _fmt_event(evs[0])
    # last resort: current scoreboard
    for e in await _scoreboard(session):
        if str(e.get("id")) == eid:
            return _fmt_event(e)
    return None


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
