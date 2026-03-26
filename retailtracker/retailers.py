"""
Retailer lookup modules for TCGTracker.
Each checker returns a list of StockResult dicts:
  {
    "retailer": str,
    "name": str,
    "price": float | None,
    "in_stock": bool,
    "url": str,
  }
"""
from __future__ import annotations

import asyncio
import re
import json
from typing import List, Optional, Dict, Any

import aiohttp
from bs4 import BeautifulSoup

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
            f"?format=json&show=name,salePrice,onlineAvailability,url,upc"
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
            })
        return results
    except Exception:
        return []


# ── Walmart (HTML scrape) ─────────────────────────────────────────────────────

async def check_walmart(
    session: aiohttp.ClientSession, upc: str, api_key: str = ""
) -> List[StockResult]:
    """
    Searches Walmart by UPC using their internal search endpoint.
    No API key required — scrapes search results directly.
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
                    })
                if results:
                    return results
            except Exception:
                pass

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
            })

        return results
    except Exception:
        return []


# ── Target (unofficial Redsky API) ────────────────────────────────────────────

async def check_target(
    session: aiohttp.ClientSession, upc: str
) -> List[StockResult]:
    """
    Uses Target's internal Redsky API to search by UPC.
    This is unofficial but well-known and widely used by trackers.
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
        for item in items[:3]:  # limit to top 3 results
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
            })
        return results
    except Exception:
        return []


# ── GameStop (HTML scrape) ────────────────────────────────────────────────────

async def check_gamestop(
    session: aiohttp.ClientSession, upc: str
) -> List[StockResult]:
    """
    Searches GameStop's website by UPC query string.
    GameStop has no bot protection so standard requests work.
    """
    try:
        search_url = f"https://www.gamestop.com/search/?q={upc}&lang=default"
        async with session.get(search_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        results = []

        # GameStop product tiles
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
        return []


# ── Pokémon Center (HTML scrape) ──────────────────────────────────────────────

async def check_pokemon_center(
    session: aiohttp.ClientSession, upc: str, product_name: str
) -> List[StockResult]:
    """
    Searches Pokémon Center by product name (they don't expose UPC search).
    Falls back to name-based search since PC doesn't surface UPCs in search.
    """
    try:
        # Use the product name for the search query since PC doesn't support UPC search
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
        return []


# ── Master checker ────────────────────────────────────────────────────────────

async def check_all_retailers(
    session: aiohttp.ClientSession,
    upc: str,
    product_name: str,
    bestbuy_key: str = "",
    walmart_key: str = "",  # kept for backwards compat, no longer used
) -> List[StockResult]:
    """Run all retailer checks concurrently and return combined results."""
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
    return combined
