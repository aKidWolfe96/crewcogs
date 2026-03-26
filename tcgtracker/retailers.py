"""
Retailer lookup modules for TCGTracker.
Supports Best Buy via their official free Products API.

Reference: https://bestbuyapis.github.io/api-documentation/

Online StockResult dict:
  {
    "retailer":  str,
    "name":      str,
    "price":     float | None,
    "in_stock":  bool,
    "url":       str,
    "sku":       str,
  }

In-store StoreResult dict:
  {
    "retailer":       str,
    "store_name":     str,
    "address":        str,
    "city":           str,
    "state":          str,
    "zip":            str,
    "low_stock":      bool,
    "distance_miles": float | None,
  }

  NOTE: The official in-store endpoint only returns stores where the product
  IS in stock. Stores absent from the response are out of stock or don't carry it.
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

BBY_BASE = "https://api.bestbuy.com/v1"


# ── Internal search helper ────────────────────────────────────────────────────

async def _bestbuy_search(
    session: aiohttp.ClientSession,
    query: str,
    api_key: str,
    label: str,
) -> List[StockResult]:
    """
    Runs one Products API query and returns StockResults.
    `query` is the filter expression, e.g.:
      'upc=196214132474'
      'search=Elite&search=Gengar&search=Binder'
    """
    try:
        url = (
            f"{BBY_BASE}/products({query})"
            f"?format=json"
            f"&show=name,salePrice,onlineAvailability,url,sku,upc"
            f"&apiKey={api_key}"
        )
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 403:
                log.error("Best Buy API key invalid or rate limit exceeded (HTTP 403)")
                return []
            if resp.status != 200:
                log.warning("Best Buy Products API HTTP %s for %s", resp.status, label)
                return []
            data = await resp.json()

        products = data.get("products", [])
        if not products:
            log.debug("Best Buy: no products found for %s", label)
            return []

        results = []
        for product in products:
            results.append({
                "retailer": "Best Buy",
                "name":     product.get("name", "Unknown"),
                "price":    product.get("salePrice"),
                "in_stock": product.get("onlineAvailability", False),
                "url":      product.get("url", "https://www.bestbuy.com"),
                "sku":      str(product.get("sku", "")),
            })

        log.debug("Best Buy: %d result(s) for %s", len(results), label)
        return results

    except Exception:
        log.exception("Best Buy search failed for %s", label)
        return []


# ── Best Buy online ───────────────────────────────────────────────────────────

async def check_bestbuy(
    session: aiohttp.ClientSession,
    upc: str,
    api_key: str,
    product_name: str = "",
) -> List[StockResult]:
    """
    Search Best Buy by UPC. If UPC returns no results (product not yet indexed
    or catalog mismatch), falls back to keyword search by product name.

    Keyword search syntax per official docs (page 14):
      search=word1&search=word2&search=word3
    Each word is a separate search= parameter joined with &.
    """
    if not api_key:
        log.warning("Best Buy API key not set — skipping UPC %s", upc)
        return []

    # Primary: search by UPC
    results = await _bestbuy_search(
        session, query=f"upc={upc}", api_key=api_key, label=f"UPC {upc}"
    )

    # Fallback: keyword search by product name if UPC found nothing
    if not results and product_name:
        words = product_name.strip().split()
        if words:
            keyword_query = "&search=".join(words)
            log.info(
                "UPC %s not in Best Buy catalog — trying keyword search: '%s'",
                upc, product_name,
            )
            results = await _bestbuy_search(
                session,
                query=f"search={keyword_query}",
                api_key=api_key,
                label=f"keyword '{product_name}'",
            )

    return results


# ── Best Buy in-store ─────────────────────────────────────────────────────────

async def check_bestbuy_stores(
    session: aiohttp.ClientSession,
    sku: str,
    zip_code: str,
    api_key: str,
) -> List[StoreResult]:
    """
    Uses the correct in-store availability endpoint per official docs (page 38):
      GET /v1/products/{sku}/stores.json?postalCode={zip}&apiKey={key}

    Returns ONLY stores where the product is currently IN STOCK, within 250
    miles of the ZIP code, sorted by proximity. Stores not in the response
    are out of stock or don't carry the product — this is by API design.

    Also maps the correct field names from the documented response:
      storeID, name, address, city, state, postalCode, lowStock, distance
    """
    if not api_key or not sku:
        return []
    try:
        url = (
            f"{BBY_BASE}/products/{sku}/stores.json"
            f"?postalCode={zip_code}"
            f"&apiKey={api_key}"
        )
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 404:
                log.debug("Best Buy SKU %s: no in-store availability (404)", sku)
                return []
            if resp.status == 403:
                log.error("Best Buy API key invalid or rate limit exceeded (HTTP 403)")
                return []
            if resp.status != 200:
                log.warning(
                    "Best Buy in-store API HTTP %s for SKU %s / ZIP %s",
                    resp.status, sku, zip_code,
                )
                return []
            data = await resp.json()

        stores = data.get("stores", [])
        if not stores:
            log.debug("Best Buy: no in-stock stores for SKU %s near ZIP %s", sku, zip_code)
            return []

        results = []
        for store in stores:
            results.append({
                "retailer":       "Best Buy",
                "store_name":     store.get("name", f"Store #{store.get('storeID', '?')}"),
                "address":        store.get("address", ""),
                "city":           store.get("city", ""),
                "state":          store.get("state", ""),
                "zip":            store.get("postalCode", ""),
                "low_stock":      store.get("lowStock", False),
                "distance_miles": store.get("distance"),
            })

        log.debug(
            "Best Buy in-store: %d location(s) in stock for SKU %s near ZIP %s",
            len(results), sku, zip_code,
        )
        return results

    except Exception:
        log.exception("Best Buy in-store check failed for SKU %s / ZIP %s", sku, zip_code)
        return []
