"""
Retailer lookup modules for TCGTracker.
Currently supports Best Buy via their official free Products API.

Each online checker returns a list of StockResult dicts:
  {
    "retailer": str,
    "name": str,
    "price": float | None,
    "in_stock": bool,
    "url": str,
    "sku": str,
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
    "distance_miles": float | None,
  }
"""
from __future__ import annotations

import logging
from typing import List, Dict, Any

import aiohttp

log = logging.getLogger("red.tcgtracker")

StockResult = Dict[str, Any]
StoreResult = Dict[str, Any]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json",
}


# ── Best Buy online (official Products API) ───────────────────────────────────

async def check_bestbuy(
    session: aiohttp.ClientSession, upc: str, api_key: str
) -> List[StockResult]:
    """
    Uses Best Buy's free official Products API to search by UPC.
    Get a free key at developer.bestbuy.com.
    Returns an empty list if the product is not in Best Buy's API catalog.
    """
    if not api_key:
        log.warning("Best Buy API key not set — skipping check for UPC %s", upc)
        return []
    try:
        url = (
            f"https://api.bestbuy.com/v1/products(upc={upc})"
            f"?format=json&show=name,salePrice,onlineAvailability,url,upc,sku"
            f"&apiKey={api_key}"
        )
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                log.warning("Best Buy API returned HTTP %s for UPC %s", resp.status, upc)
                return []
            data = await resp.json()

        results = []
        for product in data.get("products", []):
            results.append({
                "retailer": "Best Buy",
                "name":     product.get("name", "Unknown"),
                "price":    product.get("salePrice"),
                "in_stock": product.get("onlineAvailability", False),
                "url":      product.get("url", "https://www.bestbuy.com"),
                "sku":      str(product.get("sku", "")),
            })

        if not results:
            log.debug("Best Buy returned no products for UPC %s (not in their catalog)", upc)

        return results
    except Exception:
        log.exception("Best Buy online check failed for UPC %s", upc)
        return []


# ── Best Buy in-store (official Stores API) ───────────────────────────────────

async def check_bestbuy_stores(
    session: aiohttp.ClientSession,
    sku: str,
    zip_code: str,
    api_key: str,
    radius: int = 25,
) -> List[StoreResult]:
    """
    Uses Best Buy's official Stores API to check in-store availability by SKU and ZIP.
    Requires the same free API key as the online checker.
    """
    if not api_key or not sku:
        return []
    try:
        # Step 1: find nearby stores
        stores_url = (
            f"https://api.bestbuy.com/v1/stores(area({zip_code},{radius}))"
            f"?format=json&show=storeId,name,address,city,state,postalCode,distance"
            f"&apiKey={api_key}"
        )
        async with session.get(stores_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                log.warning("Best Buy Stores API returned HTTP %s for ZIP %s", resp.status, zip_code)
                return []
            stores_data = await resp.json()

        nearby_stores = stores_data.get("stores", [])
        if not nearby_stores:
            log.debug("No Best Buy stores found within %s miles of ZIP %s", radius, zip_code)
            return []

        store_ids = "+".join(str(s["storeId"]) for s in nearby_stores[:10])

        # Step 2: check availability at those stores
        avail_url = (
            f"https://api.bestbuy.com/v1/products/{sku}/stores(storeId in({store_ids}))"
            f"?format=json&show=storeId,name,city,state,inStoreAvailability"
            f"&apiKey={api_key}"
        )
        async with session.get(avail_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                log.warning("Best Buy store availability returned HTTP %s for SKU %s", resp.status, sku)
                return []
            avail_data = await resp.json()

        store_map = {str(s["storeId"]): s for s in nearby_stores}

        results = []
        for store in avail_data.get("stores", []):
            sid  = str(store.get("storeId", ""))
            info = store_map.get(sid, {})
            results.append({
                "retailer":       "Best Buy",
                "store_name":     store.get("name", info.get("name", "Unknown")),
                "address":        info.get("address", ""),
                "city":           store.get("city", info.get("city", "")),
                "state":          store.get("state", info.get("state", "")),
                "zip":            info.get("postalCode", ""),
                "in_stock":       store.get("inStoreAvailability", False),
                "distance_miles": info.get("distance"),
            })
        return results
    except Exception:
        log.exception("Best Buy store check failed for SKU %s / ZIP %s", sku, zip_code)
        return []
