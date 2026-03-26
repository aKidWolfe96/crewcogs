"""
Retailer lookup modules for TCGTracker.

Each online checker returns a list of StockResult dicts:
  {
    "retailer": str,
    "name": str,
    "price": float | None,
    "in_stock": bool,
    "url": str,
  }

Each in-store checker returns a list of StoreResult dicts:
  {
    "retailer": str,
    "store_name": str,
    "address": str,
    "city": str,
    "state": str,
    "zip": str,
    "in_stock": bool,
    "quantity": int | None,
    "distance_miles": float | None,
  }
"""
from __future__ import annotations

import asyncio
import re
import json
import logging
from typing import List, Optional, Dict, Any

import aiohttp
from bs4 import BeautifulSoup

log = logging.getLogger("red.tcgtracker")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

StockResult = Dict[str, Any]
StoreResult = Dict[str, Any]


# ═══════════════════════════════════════════════════════════════════════════════
# ONLINE AVAILABILITY CHECKERS
# ═══════════════════════════════════════════════════════════════════════════════

# ── Best Buy (official API) ────────────────────────────────────────────────────

async def check_bestbuy(
    session: aiohttp.ClientSession, upc: str, api_key: str
) -> List[StockResult]:
    """Uses Best Buy's free official Products API to search by UPC."""
    if not api_key:
        return []
    try:
        url = (
            f"https://api.bestbuy.com/v1/products(upc={upc})"
            f"?format=json&show=name,salePrice,onlineAvailability,url,upc,sku"
            f"&apiKey={api_key}"
        )
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()

        results = []
        for product in data.get("products", []):
            results.append({
                "retailer": "Best Buy",
                "name": product.get("name", "Unknown"),
                "price": product.get("salePrice"),
                "in_stock": product.get("onlineAvailability", False),
                "url": product.get("url", "https://www.bestbuy.com"),
                "sku": str(product.get("sku", "")),
            })
        return results
    except Exception:
        log.exception("Best Buy online check failed for UPC %s", upc)
        return []


# ── Walmart (HTML scrape) ─────────────────────────────────────────────────────

async def check_walmart(
    session: aiohttp.ClientSession, upc: str
) -> List[StockResult]:
    """
    Searches Walmart by UPC using their internal search endpoint.
    No API key required — scrapes search results directly.
    Note: Walmart's __NEXT_DATA__ structure may change without notice.
    """
    try:
        search_url = f"https://www.walmart.com/search?q={upc}"
        wmt_headers = {
            **HEADERS,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.walmart.com/",
        }
        async with session.get(search_url, headers=wmt_headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        results = []

        # Walmart embeds product data as JSON in a __NEXT_DATA__ script tag
        next_data_tag = soup.find("script", id="__NEXT_DATA__")
        if next_data_tag:
            try:
                next_data = json.loads(next_data_tag.string or "")
                search_results = (
                    next_data.get("props", {})
                    .get("pageProps", {})
                    .get("initialData", {})
                    .get("searchResult", {})
                    .get("itemStacks", [{}])[0]
                    .get("items", [])
                )
                for item in search_results[:3]:
                    name     = item.get("name", "Unknown")
                    price    = item.get("priceInfo", {}).get("currentPrice", {}).get("price")
                    in_stock = item.get("availabilityStatus", "").upper() in ("IN_STOCK", "LIMITED")
                    item_id  = item.get("usItemId", "")
                    url      = f"https://www.walmart.com/ip/{item_id}" if item_id else "https://www.walmart.com"
                    results.append({
                        "retailer": "Walmart",
                        "name": name,
                        "price": float(price) if price else None,
                        "in_stock": in_stock,
                        "url": url,
                        "item_id": item_id,
                    })
                if results:
                    return results
            except Exception:
                log.exception("Walmart __NEXT_DATA__ parse failed for UPC %s", upc)

        # Fallback: parse product tiles from HTML
        tiles = soup.select("[data-item-id], .search-result-gridview-item")[:3]
        for tile in tiles:
            name_el  = tile.select_one("[data-automation-id='product-title'], .product-title-link span")
            price_el = tile.select_one("[itemprop='price'], .price-characteristic")
            avail_el = tile.select_one("[data-automation-id='fulfillment-badge'], .fulfillment-badge")
            link_el  = tile.select_one("a")

            name = name_el.get_text(strip=True) if name_el else "Unknown"
            price_text = price_el.get("content") or (price_el.get_text(strip=True) if price_el else "")
            match = re.search(r"[\d.]+", price_text.replace(",", ""))
            price = float(match.group()) if match else None

            avail_text = avail_el.get_text(strip=True).lower() if avail_el else ""
            in_stock = "out of stock" not in avail_text and avail_text != ""

            href = link_el.get("href", "") if link_el else ""
            url  = f"https://www.walmart.com{href}" if href.startswith("/") else href or "https://www.walmart.com"

            results.append({
                "retailer": "Walmart",
                "name": name,
                "price": price,
                "in_stock": in_stock,
                "url": url,
                "item_id": "",
            })

        return results
    except Exception:
        log.exception("Walmart online check failed for UPC %s", upc)
        return []


# ── Target (unofficial Redsky API) ────────────────────────────────────────────

async def check_target(
    session: aiohttp.ClientSession, upc: str
) -> List[StockResult]:
    """
    Uses Target's internal Redsky API to search by UPC.
    NOTE: The API key below is unofficial/undocumented. If Target rotates it,
    this checker will silently return []. Monitor for empty results.
    """
    try:
        search_url = (
            f"https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2"
            f"?key=9f36aeafbe60771e321a7cc95a78140772ab3e96"
            f"&channel=WEB&count=10&default_purchasability_filter=true"
            f"&include_sponsored=true&keyword={upc}&offset=0"
            f"&platform=desktop&pricing_store_id=1328&scheduled_delivery_store_id=1328"
            f"&store_ids=1328&useragent=Mozilla&visitor_id=018F6D3E"
        )
        target_headers = {**HEADERS, "Accept": "application/json"}
        async with session.get(search_url, headers=target_headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()

        results = []
        items = data.get("data", {}).get("search", {}).get("products", [])
        for item in items[:3]:
            tcin = item.get("tcin", "")
            title = item.get("item", {}).get("product_description", {}).get("title", "Unknown")
            price_info = item.get("price", {})
            price = price_info.get("current_retail") or price_info.get("reg_retail")
            avail = item.get("availability_status", "").lower()
            in_stock = avail in ("in_stock", "limited_stock")
            product_url = f"https://www.target.com/p/-/A-{tcin}" if tcin else "https://www.target.com"
            results.append({
                "retailer": "Target",
                "name": title,
                "price": price,
                "in_stock": in_stock,
                "url": product_url,
                "tcin": tcin,
            })
        return results
    except Exception:
        log.exception("Target online check failed for UPC %s", upc)
        return []


# ── GameStop (HTML scrape) ────────────────────────────────────────────────────

async def check_gamestop(
    session: aiohttp.ClientSession, upc: str
) -> List[StockResult]:
    """
    Searches GameStop's website by UPC query string.
    GameStop has minimal bot protection so standard requests generally work.
    """
    try:
        search_url = f"https://www.gamestop.com/search/?q={upc}&lang=default"
        async with session.get(search_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        results = []

        tiles = soup.select(".product-tile")[:3]
        for tile in tiles:
            name_el = tile.select_one(".pdp-link a, .product-name a")
            price_el = tile.select_one(".price .value, .sales .value")
            avail_el = tile.select_one(".availability-msg, .product-availability")
            link_el  = tile.select_one("a.thumb-link, .pdp-link a")

            name  = name_el.get_text(strip=True) if name_el else "Unknown"
            price_text = price_el.get("content") or price_el.get_text(strip=True) if price_el else None
            price = None
            if price_text:
                match = re.search(r"[\d.]+", price_text.replace(",", ""))
                price = float(match.group()) if match else None

            avail_text = avail_el.get_text(strip=True).lower() if avail_el else ""
            in_stock = "out of stock" not in avail_text and "unavailable" not in avail_text and avail_text != ""

            href = link_el.get("href", "") if link_el else ""
            url  = f"https://www.gamestop.com{href}" if href.startswith("/") else href or "https://www.gamestop.com"

            results.append({
                "retailer": "GameStop",
                "name": name,
                "price": price,
                "in_stock": in_stock,
                "url": url,
            })
        return results
    except Exception:
        log.exception("GameStop online check failed for UPC %s", upc)
        return []


# ── Pokémon Center (HTML scrape) ──────────────────────────────────────────────

async def check_pokemon_center(
    session: aiohttp.ClientSession, upc: str, product_name: str
) -> List[StockResult]:
    """
    Searches Pokémon Center by product name (they don't expose UPC search).
    NOTE: Name-based search means results may not be an exact UPC match.
    Consider using specific product names to reduce false positives.
    """
    try:
        query = product_name.replace(" ", "+")
        search_url = f"https://www.pokemoncenter.com/en-us/search?q={query}"
        pc_headers = {
            **HEADERS,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        async with session.get(search_url, headers=pc_headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        results = []

        # Try to find product cards
        cards = soup.select("[class*='ProductCard'], [class*='product-card'], [class*='product-tile']")[:3]

        # Fallback: look for JSON-LD structured data
        if not cards:
            scripts = soup.find_all("script", type="application/ld+json")
            for script in scripts:
                try:
                    ld = json.loads(script.string or "")
                    items = ld if isinstance(ld, list) else [ld]
                    for item in items:
                        if item.get("@type") in ("Product", "ItemList"):
                            offers = item.get("offers", {})
                            availability = offers.get("availability", "")
                            in_stock = "InStock" in availability
                            price = offers.get("price")
                            results.append({
                                "retailer": "Pokémon Center",
                                "name": item.get("name", "Unknown"),
                                "price": float(price) if price else None,
                                "in_stock": in_stock,
                                "url": item.get("url", "https://www.pokemoncenter.com"),
                            })
                except Exception:
                    continue

        for card in cards:
            name_el  = card.select_one("[class*='title'], [class*='name'], h2, h3")
            price_el = card.select_one("[class*='price'], [class*='Price']")
            avail_el = card.select_one("[class*='stock'], [class*='avail'], button")
            link_el  = card.select_one("a")

            name = name_el.get_text(strip=True) if name_el else "Unknown"
            price_text = price_el.get_text(strip=True) if price_el else ""
            match = re.search(r"[\d.]+", price_text.replace(",", ""))
            price = float(match.group()) if match else None

            avail_text = avail_el.get_text(strip=True).lower() if avail_el else ""
            in_stock = "out of stock" not in avail_text and "sold out" not in avail_text

            href = link_el.get("href", "") if link_el else ""
            url  = f"https://www.pokemoncenter.com{href}" if href.startswith("/") else href or "https://www.pokemoncenter.com"

            results.append({
                "retailer": "Pokémon Center",
                "name": name,
                "price": price,
                "in_stock": in_stock,
                "url": url,
            })

        return results
    except Exception:
        log.exception("Pokémon Center online check failed for UPC %s", upc)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# IN-STORE AVAILABILITY CHECKERS (by ZIP code)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Best Buy in-store (official API) ──────────────────────────────────────────

async def check_bestbuy_stores(
    session: aiohttp.ClientSession, sku: str, zip_code: str, api_key: str, radius: int = 25
) -> List[StoreResult]:
    """
    Uses Best Buy's official Stores API to check in-store availability by SKU and ZIP.
    Requires the same free API key as the online checker.
    """
    if not api_key or not sku:
        return []
    try:
        # First: find nearby stores
        stores_url = (
            f"https://api.bestbuy.com/v1/stores(area({zip_code},{radius}))"
            f"?format=json&show=storeId,name,address,city,state,postalCode,distance"
            f"&apiKey={api_key}"
        )
        async with session.get(stores_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            stores_data = await resp.json()

        nearby_stores = stores_data.get("stores", [])
        if not nearby_stores:
            return []

        store_ids = "+".join(str(s["storeId"]) for s in nearby_stores[:10])

        # Second: check availability at those stores
        avail_url = (
            f"https://api.bestbuy.com/v1/products/{sku}/stores(storeId in({store_ids}))"
            f"?format=json&show=storeId,name,city,state,inStoreAvailability,inStoreAvailabilityUpdateDate"
            f"&apiKey={api_key}"
        )
        async with session.get(avail_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            avail_data = await resp.json()

        # Build a store lookup for address info
        store_map = {str(s["storeId"]): s for s in nearby_stores}

        results = []
        for store in avail_data.get("stores", []):
            sid = str(store.get("storeId", ""))
            info = store_map.get(sid, {})
            results.append({
                "retailer": "Best Buy",
                "store_name": store.get("name", info.get("name", "Unknown")),
                "address": info.get("address", ""),
                "city": store.get("city", info.get("city", "")),
                "state": store.get("state", info.get("state", "")),
                "zip": info.get("postalCode", ""),
                "in_stock": store.get("inStoreAvailability", False),
                "quantity": None,
                "distance_miles": info.get("distance"),
            })
        return results
    except Exception:
        log.exception("Best Buy store check failed for SKU %s / ZIP %s", sku, zip_code)
        return []


# ── Walmart in-store ──────────────────────────────────────────────────────────

async def check_walmart_stores(
    session: aiohttp.ClientSession, item_id: str, zip_code: str
) -> List[StoreResult]:
    """
    Uses Walmart's internal store availability API by item ID and ZIP code.
    Unofficial but stable; used by many third-party trackers.
    """
    if not item_id:
        return []
    try:
        # Walmart's storeFinder endpoint to get nearby store IDs
        finder_url = (
            f"https://www.walmart.com/store/finder/electrode/api/fetchNearestStores"
            f"?distance=25&postalCode={zip_code}"
        )
        finder_headers = {
            **HEADERS,
            "Accept": "application/json",
            "Referer": "https://www.walmart.com/",
        }
        async with session.get(finder_url, headers=finder_headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            finder_data = await resp.json()

        stores = finder_data.get("payload", {}).get("storesData", {}).get("stores", [])
        if not stores:
            return []

        results = []
        for store in stores[:8]:
            store_id  = store.get("id", "")
            store_num = store.get("storeNumber", store_id)
            if not store_id:
                continue

            avail_url = (
                f"https://www.walmart.com/store/{store_id}/product/{item_id}/availability"
            )
            try:
                async with session.get(avail_url, headers=finder_headers, timeout=aiohttp.ClientTimeout(total=8)) as avail_resp:
                    if avail_resp.status != 200:
                        continue
                    avail_data = await avail_resp.json()

                in_stock = avail_data.get("availabilityStatus", "").upper() in ("IN_STOCK", "LIMITED_STOCK", "INSTOCK")
                qty = avail_data.get("quantity")
                results.append({
                    "retailer": "Walmart",
                    "store_name": f"Walmart #{store_num}",
                    "address": store.get("address", {}).get("addressLineOne", ""),
                    "city": store.get("address", {}).get("city", ""),
                    "state": store.get("address", {}).get("state", ""),
                    "zip": store.get("address", {}).get("postalCode", ""),
                    "in_stock": in_stock,
                    "quantity": int(qty) if qty is not None else None,
                    "distance_miles": store.get("distance"),
                })
            except Exception:
                continue

            await asyncio.sleep(0.3)  # Be polite between per-store requests

        return results
    except Exception:
        log.exception("Walmart store check failed for item %s / ZIP %s", item_id, zip_code)
        return []


# ── Target in-store (unofficial Redsky) ──────────────────────────────────────

async def check_target_stores(
    session: aiohttp.ClientSession, tcin: str, zip_code: str
) -> List[StoreResult]:
    """
    Uses Target's internal Redsky API to check in-store availability by TCIN and ZIP.
    NOTE: Uses the same unofficial API key as the online checker.
    """
    if not tcin:
        return []
    try:
        # Find nearby Target stores
        geo_url = (
            f"https://redsky.target.com/v3/stores/nearby/{zip_code}"
            f"?key=9f36aeafbe60771e321a7cc95a78140772ab3e96"
            f"&limit=10&within=25&unit=mile"
        )
        target_headers = {**HEADERS, "Accept": "application/json"}
        async with session.get(geo_url, headers=target_headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            geo_data = await resp.json()

        stores = geo_data[0].get("locations", []) if geo_data else []
        if not stores:
            return []

        store_ids = ",".join(str(s.get("location_id", "")) for s in stores[:10] if s.get("location_id"))
        if not store_ids:
            return []

        # Check in-store availability
        avail_url = (
            f"https://redsky.target.com/redsky_aggregations/v1/web/pdp_fulfillment_v1"
            f"?key=9f36aeafbe60771e321a7cc95a78140772ab3e96"
            f"&tcin={tcin}&store_ids={store_ids}&zip={zip_code}&state=US&latitude=0&longitude=0"
        )
        async with session.get(avail_url, headers=target_headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            avail_data = await resp.json()

        # Build store lookup
        store_map = {str(s.get("location_id", "")): s for s in stores}

        results = []
        store_avail = (
            avail_data.get("data", {})
            .get("product", {})
            .get("fulfillment", {})
            .get("store_options", [])
        )
        for option in store_avail:
            sid = str(option.get("location_id", ""))
            info = store_map.get(sid, {})
            avail = option.get("in_store_only", {}).get("availability_status", "").lower()
            in_stock = avail in ("in_stock", "limited_stock")
            results.append({
                "retailer": "Target",
                "store_name": info.get("store_name", f"Target #{sid}"),
                "address": info.get("address", {}).get("formatted_address", ""),
                "city": info.get("address", {}).get("city", ""),
                "state": info.get("address", {}).get("state", ""),
                "zip": info.get("address", {}).get("postal_code", ""),
                "in_stock": in_stock,
                "quantity": None,
                "distance_miles": info.get("distance_from_store"),
            })
        return results
    except Exception:
        log.exception("Target store check failed for TCIN %s / ZIP %s", tcin, zip_code)
        return []


# ── GameStop in-store ─────────────────────────────────────────────────────────

async def check_gamestop_stores(
    session: aiohttp.ClientSession, upc: str, zip_code: str
) -> List[StoreResult]:
    """
    Uses GameStop's internal store search API to check in-store availability by UPC and ZIP.
    """
    try:
        search_url = (
            f"https://www.gamestop.com/on/demandware.store/Sites-gamestop-us-Site/default"
            f"/StoreInventory-CheckInventory?pid={upc}&postalCode={zip_code}&maxDistance=25"
        )
        gs_headers = {
            **HEADERS,
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.gamestop.com/",
        }
        async with session.get(search_url, headers=gs_headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()

        results = []
        stores = data.get("stores", [])
        for store in stores[:10]:
            avail = store.get("inventoryStatus", "").lower()
            in_stock = avail in ("in_stock", "instock", "available")
            qty = store.get("inventoryLevel")
            results.append({
                "retailer": "GameStop",
                "store_name": store.get("name", "Unknown GameStop"),
                "address": store.get("address1", ""),
                "city": store.get("city", ""),
                "state": store.get("stateCode", ""),
                "zip": store.get("postalCode", ""),
                "in_stock": in_stock,
                "quantity": int(qty) if qty is not None else None,
                "distance_miles": store.get("distanceInfo"),
            })
        return results
    except Exception:
        log.exception("GameStop store check failed for UPC %s / ZIP %s", upc, zip_code)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# MASTER CHECKERS
# ═══════════════════════════════════════════════════════════════════════════════

async def check_all_retailers(
    session: aiohttp.ClientSession,
    upc: str,
    product_name: str,
    bestbuy_key: str = "",
) -> List[StockResult]:
    """Run all online retailer checks concurrently and return combined results."""
    tasks = [
        check_bestbuy(session, upc, bestbuy_key),
        check_walmart(session, upc),
        check_target(session, upc),
        check_gamestop(session, upc),
        check_pokemon_center(session, upc, product_name),
    ]
    results_nested = await asyncio.gather(*tasks, return_exceptions=True)
    combined = []
    for r in results_nested:
        if isinstance(r, list):
            combined.extend(r)
        elif isinstance(r, Exception):
            log.exception("Retailer check raised an exception: %s", r)
    return combined


async def check_all_stores(
    session: aiohttp.ClientSession,
    upc: str,
    zip_code: str,
    online_results: List[StockResult],
    bestbuy_key: str = "",
) -> List[StoreResult]:
    """
    Run all in-store checks concurrently for a given ZIP code.
    Reuses identifiers (SKU, item_id, TCIN) already extracted from online_results
    to avoid redundant lookups.
    """
    # Extract identifiers from online results to feed the store checkers
    bby_sku  = next((r.get("sku", "") for r in online_results if r["retailer"] == "Best Buy" and r.get("sku")), "")
    wmt_id   = next((r.get("item_id", "") for r in online_results if r["retailer"] == "Walmart" and r.get("item_id")), "")
    tgt_tcin = next((r.get("tcin", "") for r in online_results if r["retailer"] == "Target" and r.get("tcin")), "")

    tasks = [
        check_bestbuy_stores(session, bby_sku, zip_code, bestbuy_key) if bby_sku else asyncio.sleep(0),
        check_walmart_stores(session, wmt_id, zip_code) if wmt_id else asyncio.sleep(0),
        check_target_stores(session, tgt_tcin, zip_code) if tgt_tcin else asyncio.sleep(0),
        check_gamestop_stores(session, upc, zip_code),
    ]
    results_nested = await asyncio.gather(*tasks, return_exceptions=True)
    combined: List[StoreResult] = []
    for r in results_nested:
        if isinstance(r, list):
            combined.extend(r)
        elif isinstance(r, Exception):
            log.exception("Store check raised an exception: %s", r)
    return combined
