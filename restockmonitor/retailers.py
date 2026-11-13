"""Free stock checkers for Pokémon TCG retailers.

No paid services. Each retailer uses whatever the store's own website already
exposes publicly:

* Target          – the JSON API target.com's own product pages call (redsky)
* Best Buy        – the price/button-state JSON bestbuy.com uses, or the official
                    *free* Best Buy developer API if you add a key
* Walmart         – the product data embedded in the product page
* Shopify stores  – `/products/<handle>.js` (most local game stores run Shopify)
* GameStop, Pokémon Center, Amazon and anything else – schema.org product data
                    (JSON-LD) embedded in the page, plus a few page markers

The parse_* functions are pure (no network) so they can be unit-tested.
If a store answers with a bot check / CAPTCHA / queue page we report BLOCKED
and back off — we never try to get around a store's protections.
"""
from __future__ import annotations

import asyncio
import html as htmllib
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse, urlunparse

import aiohttp

# ──────────────────────────────────────────────────────────────────────────────
# Statuses
# ──────────────────────────────────────────────────────────────────────────────

IN_STOCK = "in_stock"
PREORDER = "preorder"
OUT_OF_STOCK = "out_of_stock"
UNKNOWN = "unknown"      # page loaded but we couldn't tell
BLOCKED = "blocked"      # bot check / CAPTCHA / rate limit
ERROR = "error"          # network or parsing failure

AVAILABLE = {IN_STOCK, PREORDER}
DEFINITIVE = {IN_STOCK, PREORDER, OUT_OF_STOCK}

STATUS_LABEL = {
    IN_STOCK: "In stock",
    PREORDER: "Pre-order open",
    OUT_OF_STOCK: "Out of stock",
    UNKNOWN: "Unknown",
    BLOCKED: "Blocked by store",
    ERROR: "Check failed",
}
STATUS_EMOJI = {
    IN_STOCK: "🟢", PREORDER: "🟣", OUT_OF_STOCK: "🔴",
    UNKNOWN: "⚪", BLOCKED: "🟠", ERROR: "⚠️",
}


@dataclass
class CheckResult:
    status: str
    name: Optional[str] = None
    price: Optional[float] = None
    image: Optional[str] = None
    detail: Optional[str] = None      # e.g. "Sold by a third-party seller", "Pickup only"
    third_party: bool = False         # marketplace seller (often scalper pricing)

    @property
    def available(self) -> bool:
        return self.status in AVAILABLE

    @property
    def definitive(self) -> bool:
        return self.status in DEFINITIVE


# ──────────────────────────────────────────────────────────────────────────────
# Retailer registry & URL detection
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Retailer:
    key: str
    name: str
    emoji: str
    color: int
    domains: Tuple[str, ...] = ()
    reliability: str = "good"   # good | ok | best-effort
    note: str = ""


RETAILERS: Dict[str, Retailer] = {
    "target": Retailer("target", "Target", "🎯", 0xCC0000, ("target.com",), "good",
                       "Online shipping stock; also notes store pickup near your ZIP."),
    "bestbuy": Retailer("bestbuy", "Best Buy", "🟨", 0x0046BE, ("bestbuy.com",), "good",
                        "Add a free Best Buy developer key for the most reliable checks."),
    "walmart": Retailer("walmart", "Walmart", "🔵", 0x0071CE, ("walmart.com",), "ok",
                        "Flags third-party sellers. Walmart sometimes shows bot checks."),
    "shopify": Retailer("shopify", "Shopify store", "🛍️", 0x95BF47, (), "good",
                        "Most local game & card shops. Also supports whole-store watching."),
    "gamestop": Retailer("gamestop", "GameStop", "🎮", 0xE60012, ("gamestop.com",), "ok",
                         "Reads the product data on the page."),
    "pokemoncenter": Retailer("pokemoncenter", "Pokémon Center", "⚡", 0xFFCB05, ("pokemoncenter.com",),
                              "best-effort", "Uses a waiting room on big drops — checks may be blocked then."),
    "amazon": Retailer("amazon", "Amazon", "📦", 0xFF9900, ("amazon.com",), "best-effort",
                       "Amazon often blocks automated checks; works sometimes."),
    "generic": Retailer("generic", "Website", "🌐", 0x95A5A6, (), "ok",
                        "Any shop that publishes standard product data on its pages."),
}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def normalize_url(url: str) -> str:
    url = url.strip().strip("<>")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def detect_retailer(url: str) -> str:
    host = _host(url)
    for key, r in RETAILERS.items():
        if any(host == d or host.endswith("." + d) for d in r.domains):
            return key
    if "/products/" in urlparse(url).path:
        return "shopify"
    return "generic"


def target_tcin(url: str) -> Optional[str]:
    m = re.search(r"/A-(\d{6,10})", url) or re.search(r"[?&](?:tcin|preselect)=(\d{6,10})", url)
    return m.group(1) if m else None


def bestbuy_sku(url: str) -> Optional[str]:
    if re.fullmatch(r"\d{6,8}", url.strip()):
        return url.strip()
    for pat in (r"[?&]skuId=(\d{6,8})", r"/sku/(\d{6,8})", r"/(\d{7})\.p", r"/(\d{6,8})(?:[/?#]|$)"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def walmart_id(url: str) -> Optional[str]:
    m = re.search(r"/ip/(?:[^/?#]+/)?(\d{5,})", url)
    return m.group(1) if m else None


def shopify_product_js_url(url: str) -> Optional[str]:
    """https://shop/collections/x/products/handle?variant=1 -> https://shop/products/handle.js"""
    p = urlparse(url)
    m = re.search(r"/products/([^/?#]+)", p.path)
    if not m:
        return None
    handle = m.group(1)
    if handle.endswith(".js") or handle.endswith(".json"):
        handle = handle.rsplit(".", 1)[0]
    return urlunparse((p.scheme or "https", p.netloc, f"/products/{handle}.js", "", "", ""))


def shopify_variant(url: str) -> Optional[int]:
    q = parse_qs(urlparse(url).query)
    v = q.get("variant", [None])[0]
    return int(v) if v and v.isdigit() else None


def clean_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    text = htmllib.unescape(re.sub(r"<[^>]+>", "", str(text))).strip()
    return re.sub(r"\s+", " ", text) or None


def to_price(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)", str(value))
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return v if v > 0 else None


# ──────────────────────────────────────────────────────────────────────────────
# Generic page parsing (schema.org JSON-LD, Open Graph, __NEXT_DATA__)
# ──────────────────────────────────────────────────────────────────────────────

class _PageScraper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: Dict[str, str] = {}
        self.ld_json: List[str] = []
        self.next_data: Optional[str] = None
        self.title: Optional[str] = None
        self._script: Optional[str] = None
        self._buf: List[str] = []
        self._in_title = False
        self.item_availability: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "link" and a.get("itemprop", "").lower() == "availability":
            self.item_availability = a.get("href")
        elif tag == "script":
            stype = a.get("type", "").lower()
            if "ld+json" in stype:
                self._script, self._buf = "ld", []
            elif a.get("id") == "__NEXT_DATA__":
                self._script, self._buf = "next", []
        elif tag == "title":
            self._in_title, self._buf = True, []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script:
            data = "".join(self._buf)
            if self._script == "ld":
                self.ld_json.append(data)
            else:
                self.next_data = data
            self._script = None
        elif tag == "title" and self._in_title:
            self.title = "".join(self._buf).strip()
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._script or self._in_title:
            self._buf.append(data)


def scrape_page(html: str) -> _PageScraper:
    sc = _PageScraper()
    try:
        sc.feed(html)
        sc.close()
    except Exception:  # malformed HTML — keep whatever we managed to read
        pass
    return sc


def availability_from_schema(value: Any) -> Optional[str]:
    """Map schema.org ItemAvailability (any spelling) to our statuses."""
    if not value:
        return None
    v = str(value).rsplit("/", 1)[-1].strip().lower().replace("_", "").replace(" ", "")
    if v in ("instock", "instoreonly", "limitedavailability", "onlineonly"):
        return IN_STOCK if v != "instoreonly" else OUT_OF_STOCK
    if v in ("preorder", "presale"):
        return PREORDER
    if v in ("outofstock", "soldout", "discontinued", "backorder", "preordernotavailable"):
        return OUT_OF_STOCK
    return None


def _iter_ld_nodes(blob: Any) -> Iterable[dict]:
    if isinstance(blob, list):
        for item in blob:
            yield from _iter_ld_nodes(item)
    elif isinstance(blob, dict):
        yield blob
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in blob:
                yield from _iter_ld_nodes(blob[key])


def _is_product(node: dict) -> bool:
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(str(x).lower() in ("product", "productgroup", "individualproduct") for x in types if x)


def parse_jsonld(scripts: List[str]) -> Optional[CheckResult]:
    """Read the first schema.org Product found in JSON-LD script blocks."""
    for raw in scripts:
        try:
            blob = json.loads(raw.strip())
        except (ValueError, TypeError):
            continue
        for node in _iter_ld_nodes(blob):
            if not _is_product(node):
                continue
            offers = node.get("offers")
            if isinstance(offers, dict) and "offers" in offers and isinstance(offers["offers"], list):
                offers = offers["offers"]  # AggregateOffer
            offer_list = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
            statuses = [availability_from_schema(o.get("availability")) for o in offer_list if isinstance(o, dict)]
            if not statuses and "hasVariant" in node:
                for var in node.get("hasVariant") or []:
                    vo = var.get("offers") if isinstance(var, dict) else None
                    for o in (vo if isinstance(vo, list) else [vo]):
                        if isinstance(o, dict):
                            statuses.append(availability_from_schema(o.get("availability")))
            statuses = [s for s in statuses if s]
            if IN_STOCK in statuses:
                status = IN_STOCK
            elif PREORDER in statuses:
                status = PREORDER
            elif statuses:
                status = OUT_OF_STOCK
            else:
                status = UNKNOWN
            price = None
            for o in offer_list:
                if isinstance(o, dict):
                    price = to_price(o.get("price") or o.get("lowPrice"))
                    if price:
                        break
            image = node.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            if isinstance(image, dict):
                image = image.get("url")
            seller = None
            for o in offer_list:
                if isinstance(o, dict) and isinstance(o.get("seller"), dict):
                    seller = o["seller"].get("name")
                    break
            return CheckResult(
                status=status, name=clean_text(node.get("name")), price=price,
                image=image if isinstance(image, str) else None,
                detail=f"Sold by {seller}" if seller else None,
            )
    return None


def parse_generic_html(html: str) -> CheckResult:
    sc = scrape_page(html)
    result = parse_jsonld(sc.ld_json) or CheckResult(status=UNKNOWN)
    if result.status == UNKNOWN:
        for key in ("product:availability", "og:availability", "availability"):
            mapped = availability_from_schema(sc.meta.get(key))
            if mapped:
                result.status = mapped
                break
        else:
            mapped = availability_from_schema(sc.item_availability)
            if mapped:
                result.status = mapped
    result.name = result.name or clean_text(sc.meta.get("og:title")) or clean_text(sc.title)
    result.image = result.image or sc.meta.get("og:image")
    if result.price is None:
        result.price = to_price(sc.meta.get("product:price:amount") or sc.meta.get("og:price:amount"))
    if result.status == UNKNOWN and not result.detail:
        result.detail = "This page doesn't publish stock info the checker can read."
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Bot-check detection
# ──────────────────────────────────────────────────────────────────────────────

_BLOCK_MARKERS = (
    "robot or human", "are you a robot", "px-captcha", "captcha-delivery", "/captcha",
    "enter the characters you see below", "access denied", "request unsuccessful. incapsula",
    "queue-it", "you are now in line", "pardon our interruption", "please verify you are a human",
    "cf-challenge", "challenge-platform", "attention required! | cloudflare",
)


def looks_blocked(status_code: int, body: str) -> bool:
    if status_code in (403, 429):
        return True
    low = body[:20000].lower()
    return any(m in low for m in _BLOCK_MARKERS)


# ──────────────────────────────────────────────────────────────────────────────
# Retailer-specific parsers (pure)
# ──────────────────────────────────────────────────────────────────────────────

TARGET_KEY = "9f36aeafbe60771e321a7cc95a78140772ab3e96"  # public key target.com's own web pages use
TARGET_STATUS = {
    "IN_STOCK": IN_STOCK, "LIMITED_STOCK": IN_STOCK,
    "PRE_ORDER_SELLABLE": PREORDER,
    "OUT_OF_STOCK": OUT_OF_STOCK, "PRE_ORDER_UNSELLABLE": OUT_OF_STOCK,
    "UNAVAILABLE": OUT_OF_STOCK, "DISCONTINUED": OUT_OF_STOCK,
}


def _dig(obj: Any, *path: Any) -> Any:
    for p in path:
        if isinstance(obj, dict):
            obj = obj.get(p)
        elif isinstance(obj, list) and isinstance(p, int) and -len(obj) <= p < len(obj):
            obj = obj[p]
        else:
            return None
    return obj


def parse_target(pdp: Optional[dict], fulfillment: Optional[dict]) -> CheckResult:
    product = _dig(pdp, "data", "product") or {}
    name = clean_text(_dig(product, "item", "product_description", "title"))
    price = to_price(_dig(product, "price", "current_retail")) or to_price(_dig(product, "price", "formatted_current_price"))
    image = _dig(product, "item", "enrichment", "images", "primary_image_url")

    ful = _dig(fulfillment, "data", "product", "fulfillment") or {}
    ship = _dig(ful, "shipping_options", "availability_status")
    status = TARGET_STATUS.get(str(ship).upper(), UNKNOWN) if ship else UNKNOWN
    if status == UNKNOWN and ful.get("sold_out") is True:
        status = OUT_OF_STOCK
    detail = None
    pickup = _dig(ful, "store_options", 0, "order_pickup", "availability_status")
    store_name = _dig(ful, "store_options", 0, "location_name")
    if pickup:
        if str(pickup).upper() == "IN_STOCK":
            detail = f"Pickup available at {store_name}" if store_name else "Store pickup available nearby"
            if status == OUT_OF_STOCK:
                detail = f"Online sold out — but pickup available at {store_name or 'a nearby store'}"
        elif status == IN_STOCK:
            detail = "Ships online (not in your local store)"
    if status == UNKNOWN and product and fulfillment is None:
        detail = "Got product info, but Target didn't return stock data."
    return CheckResult(status=status, name=name, price=price, image=image, detail=detail)


BESTBUY_BUTTONS = {
    "ADD_TO_CART": IN_STOCK, "PRE_ORDER": PREORDER,
    "SOLD_OUT": OUT_OF_STOCK, "COMING_SOON": OUT_OF_STOCK, "CHECK_STORES": OUT_OF_STOCK,
    "UNAVAILABLE_NEARBY": OUT_OF_STOCK, "NOT_AVAILABLE": OUT_OF_STOCK, "CHECK_AVAILABILITY": OUT_OF_STOCK,
}


def bestbuy_image(sku: str) -> str:
    return f"https://pisces.bbystatic.com/image2/BestBuy_US/images/products/{sku[:4]}/{sku}_sd.jpg"


def parse_bestbuy_priceblocks(data: Any, sku: str) -> CheckResult:
    items = data if isinstance(data, list) else [data]
    for item in items:
        sku_node = _dig(item, "sku") or {}
        if str(sku_node.get("skuId", sku)) != str(sku):
            continue
        state = _dig(sku_node, "buttonState", "buttonState")
        status = BESTBUY_BUTTONS.get(str(state).upper(), UNKNOWN) if state else UNKNOWN
        detail = None
        if str(state).upper() == "CHECK_STORES":
            detail = "Online sold out — some stores may have it"
        elif str(state).upper() == "COMING_SOON":
            detail = "Coming soon"
        return CheckResult(
            status=status,
            name=clean_text(_dig(sku_node, "names", "short")),
            price=to_price(_dig(sku_node, "price", "currentPrice")),
            image=bestbuy_image(sku),
            detail=detail,
        )
    return CheckResult(status=UNKNOWN, image=bestbuy_image(sku), detail="Best Buy didn't return this SKU.")


def parse_bestbuy_api(data: Any, sku: str) -> CheckResult:
    """Official (free) Best Buy Products API: /v1/products/{sku}.json"""
    if not isinstance(data, dict):
        return CheckResult(status=UNKNOWN)
    orderable = str(data.get("orderable") or "").lower()
    if orderable == "preorder":
        status = PREORDER
    elif data.get("onlineAvailability") is True:
        status = IN_STOCK
    elif data.get("onlineAvailability") is False:
        status = OUT_OF_STOCK
    else:
        status = UNKNOWN
    detail = "Coming soon" if orderable == "comingsoon" else None
    if status == OUT_OF_STOCK and data.get("inStoreAvailability") is True:
        detail = "Online sold out — available in some stores"
    return CheckResult(
        status=status, name=clean_text(data.get("name")),
        price=to_price(data.get("salePrice") or data.get("regularPrice")),
        image=data.get("image") or bestbuy_image(sku), detail=detail,
    )


def parse_walmart_html(html: str) -> CheckResult:
    sc = scrape_page(html)
    product = None
    if sc.next_data:
        try:
            nd = json.loads(sc.next_data)
            product = _dig(nd, "props", "pageProps", "initialData", "data", "product")
        except ValueError:
            product = None
    if isinstance(product, dict):
        avail = str(product.get("availabilityStatus") or "").upper()
        status = {"IN_STOCK": IN_STOCK, "OUT_OF_STOCK": OUT_OF_STOCK}.get(avail, UNKNOWN)
        if product.get("preOrder", {}).get("isPreOrder") and status == IN_STOCK:
            status = PREORDER
        seller = product.get("sellerDisplayName") or product.get("sellerName")
        third_party = bool(seller) and "walmart" not in str(seller).lower()
        return CheckResult(
            status=status,
            name=clean_text(product.get("name")),
            price=to_price(_dig(product, "priceInfo", "currentPrice", "price")),
            image=_dig(product, "imageInfo", "thumbnailUrl"),
            detail=f"Sold by {seller} (third-party)" if third_party else ("Sold by Walmart" if seller else None),
            third_party=third_party,
        )
    result = parse_generic_html(html)
    if result.detail and "Sold by" in result.detail and "walmart" not in result.detail.lower():
        result.third_party = True
    return result


def parse_shopify_js(data: Any, variant_id: Optional[int] = None) -> CheckResult:
    if not isinstance(data, dict) or "variants" not in data:
        return CheckResult(status=UNKNOWN, detail="Not a Shopify product page.")
    variants = data.get("variants") or []
    chosen = [v for v in variants if variant_id and v.get("id") == variant_id] or variants
    available = any(v.get("available") for v in chosen) if chosen else bool(data.get("available"))
    price_cents = (chosen[0].get("price") if chosen else None) or data.get("price")
    price = (float(price_cents) / 100) if isinstance(price_cents, (int, float)) and price_cents else to_price(price_cents)
    image = data.get("featured_image")
    if isinstance(image, str) and image.startswith("//"):
        image = "https:" + image
    title = clean_text(data.get("title"))
    if variant_id and chosen and chosen[0].get("title") not in (None, "Default Title"):
        title = f"{title} — {chosen[0]['title']}"
    tags = " ".join(data.get("tags") or []).lower() if isinstance(data.get("tags"), list) else str(data.get("tags") or "").lower()
    status = IN_STOCK if available else OUT_OF_STOCK
    if available and ("pre-order" in tags or "preorder" in tags or "pre-order" in (title or "").lower()):
        status = PREORDER
    return CheckResult(status=status, name=title, price=price, image=image)


def parse_amazon_html(html: str) -> CheckResult:
    result = parse_generic_html(html)
    m = re.search(r'id="productTitle"[^>]*>\s*([^<]+)<', html)
    if m:
        result.name = clean_text(m.group(1))
    low = html.lower()
    if 'id="add-to-cart-button"' in low or "id='add-to-cart-button'" in low:
        result.status = IN_STOCK
    elif 'id="buy-now-button"' in low:
        result.status = IN_STOCK
    elif "currently unavailable" in low or 'id="outofstock"' in low:
        result.status = OUT_OF_STOCK
    if result.status == IN_STOCK and "pre-order" in low and 'id="preorder' in low:
        result.status = PREORDER
    img = re.search(r'"hiRes":"(https://[^"]+)"', html) or re.search(r'id="landingImage"[^>]+src="([^"]+)"', html)
    if img and not result.image:
        result.image = img.group(1)
    if result.status != UNKNOWN:
        result.detail = None
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Shopify whole-store watching (new listings + restocks by keyword)
# ──────────────────────────────────────────────────────────────────────────────

def fold(text: str) -> str:
    return (text or "").lower().replace("é", "e").replace("’", "'")


def matches_keywords(title: str, keywords: List[str]) -> bool:
    t = fold(title)
    return any(all(word in t for word in fold(k).split()) for k in keywords) if keywords else True


def parse_shopify_catalog(data: Any, base: str, keywords: List[str]) -> List[dict]:
    """[{id, title, handle, url, available, price, image}] for products matching keywords."""
    out = []
    for p in (data or {}).get("products", []) if isinstance(data, dict) else []:
        title = p.get("title") or ""
        haystack = " ".join([title, p.get("product_type") or "", p.get("vendor") or "",
                             " ".join(p.get("tags") or []) if isinstance(p.get("tags"), list) else str(p.get("tags") or "")])
        if not matches_keywords(haystack, keywords):
            continue
        variants = p.get("variants") or []
        price = to_price(variants[0].get("price")) if variants else None
        images = p.get("images") or []
        out.append({
            "id": str(p.get("id")),
            "title": clean_text(title) or "Untitled",
            "url": f"{base}/products/{p.get('handle')}",
            "available": any(v.get("available") for v in variants),
            "price": price,
            "image": images[0].get("src") if images and isinstance(images[0], dict) else None,
        })
    return out


def shopify_base(url: str) -> str:
    p = urlparse(normalize_url(url))
    return f"{p.scheme}://{p.netloc}"


# ──────────────────────────────────────────────────────────────────────────────
# Network layer
# ──────────────────────────────────────────────────────────────────────────────

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
HTML_ACCEPT = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
JSON_ACCEPT = {"Accept": "application/json, text/plain, */*"}


class FetchError(Exception):
    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class CheckContext:
    session: aiohttp.ClientSession
    zip_code: Optional[str] = None
    bestbuy_key: Optional[str] = None
    target_store_cache: Dict[str, str] = field(default_factory=dict)


async def fetch_text(session: aiohttp.ClientSession, url: str, *, accept_json: bool = False,
                     timeout: float = 20.0) -> Tuple[int, str]:
    headers = {**BROWSER_HEADERS, **(JSON_ACCEPT if accept_json else HTML_ACCEPT)}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout),
                               allow_redirects=True) as resp:
            body = await resp.text(errors="replace")
            final = str(resp.url).lower()
            if "captcha" in final or "/blocked" in final:
                raise FetchError(BLOCKED, "The store showed a bot check.")
            return resp.status, body
    except asyncio.TimeoutError as exc:
        raise FetchError(ERROR, "The store took too long to answer.") from exc
    except aiohttp.ClientError as exc:
        raise FetchError(ERROR, f"Couldn't reach the store ({type(exc).__name__}).") from exc


async def fetch_json(session: aiohttp.ClientSession, url: str) -> Any:
    status, body = await fetch_text(session, url, accept_json=True)
    if looks_blocked(status, body):
        raise FetchError(BLOCKED, f"The store refused the check (HTTP {status}).")
    if status == 404:
        raise FetchError(ERROR, "Product not found (404).")
    if status >= 400:
        raise FetchError(ERROR, f"The store returned HTTP {status}.")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise FetchError(ERROR, "The store returned something that isn't JSON.") from exc


async def fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    status, body = await fetch_text(session, url)
    if looks_blocked(status, body):
        raise FetchError(BLOCKED, f"The store showed a bot check (HTTP {status}).")
    if status == 404:
        raise FetchError(ERROR, "Product page not found (404).")
    if status >= 400:
        raise FetchError(ERROR, f"The store returned HTTP {status}.")
    return body


async def _target_store_id(ctx: CheckContext) -> str:
    zip_code = ctx.zip_code or "55423"
    if zip_code in ctx.target_store_cache:
        return ctx.target_store_cache[zip_code]
    store_id = "3991"
    try:
        data = await fetch_json(
            ctx.session,
            f"https://redsky.target.com/redsky_aggregations/v1/web/nearby_stores_v1"
            f"?key={TARGET_KEY}&limit=1&within=100&place={zip_code}",
        )
        sid = _dig(data, "data", "nearby_stores", "stores", 0, "store_id")
        if sid:
            store_id = str(sid)
    except FetchError:
        pass
    ctx.target_store_cache[zip_code] = store_id
    return store_id


async def check_target(url: str, ctx: CheckContext) -> CheckResult:
    tcin = target_tcin(url)
    if not tcin:
        raise FetchError(ERROR, "Couldn't find the Target item number (A-12345678) in that link.")
    store = await _target_store_id(ctx)
    zip_code = ctx.zip_code or "55423"
    common = f"key={TARGET_KEY}&tcin={tcin}&channel=WEB&page=%2Fp%2FA-{tcin}"
    pdp = fulfillment = None
    pdp_err: Optional[FetchError] = None
    try:
        pdp = await fetch_json(ctx.session, "https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1"
                                            f"?{common}&pricing_store_id={store}&store_id={store}")
    except FetchError as exc:
        pdp_err = exc
    try:
        fulfillment = await fetch_json(
            ctx.session,
            "https://redsky.target.com/redsky_aggregations/v1/web/product_fulfillment_and_variation_hierarchy_v1"
            f"?{common}&store_id={store}&required_store_id={store}&zip={zip_code}",
        )
    except FetchError as exc:
        if pdp_err is not None:
            raise exc
    return parse_target(pdp, fulfillment)


async def check_bestbuy(url: str, ctx: CheckContext) -> CheckResult:
    sku = bestbuy_sku(url)
    if not sku:
        html = await fetch_html(ctx.session, url)
        m = re.search(r'"skuId"\s*:\s*"?(\d{6,8})', html)
        if not m:
            raise FetchError(ERROR, "Couldn't find the Best Buy SKU in that link.")
        sku = m.group(1)
    if ctx.bestbuy_key:
        data = await fetch_json(
            ctx.session,
            f"https://api.bestbuy.com/v1/products/{sku}.json?apiKey={ctx.bestbuy_key}"
            "&show=sku,name,salePrice,regularPrice,onlineAvailability,inStoreAvailability,orderable,image,url",
        )
        return parse_bestbuy_api(data, sku)
    data = await fetch_json(ctx.session, f"https://www.bestbuy.com/api/3.0/priceBlocks?skus={sku}")
    return parse_bestbuy_priceblocks(data, sku)


async def check_walmart(url: str, ctx: CheckContext) -> CheckResult:
    html = await fetch_html(ctx.session, url)
    return await asyncio.to_thread(parse_walmart_html, html)


async def check_shopify(url: str, ctx: CheckContext) -> CheckResult:
    js_url = shopify_product_js_url(url)
    if js_url:
        try:
            data = await fetch_json(ctx.session, js_url)
            result = parse_shopify_js(data, shopify_variant(url))
            if result.status != UNKNOWN:
                return result
        except FetchError as exc:
            if exc.status == BLOCKED:
                raise
    return await check_generic(url, ctx)  # not actually Shopify — read the page instead


async def check_amazon(url: str, ctx: CheckContext) -> CheckResult:
    html = await fetch_html(ctx.session, url)
    return await asyncio.to_thread(parse_amazon_html, html)


async def check_generic(url: str, ctx: CheckContext) -> CheckResult:
    html = await fetch_html(ctx.session, url)
    return await asyncio.to_thread(parse_generic_html, html)


CHECKERS = {
    "target": check_target,
    "bestbuy": check_bestbuy,
    "walmart": check_walmart,
    "shopify": check_shopify,
    "amazon": check_amazon,
    "gamestop": check_generic,
    "pokemoncenter": check_generic,
    "generic": check_generic,
}


async def check_product(url: str, retailer: str, ctx: CheckContext) -> CheckResult:
    """Run a check and never raise — failures come back as ERROR / BLOCKED results."""
    checker = CHECKERS.get(retailer, check_generic)
    try:
        return await checker(url, ctx)
    except FetchError as exc:
        return CheckResult(status=exc.status, detail=exc.detail)
    except Exception as exc:  # parser surprise — report, don't crash the loop
        return CheckResult(status=ERROR, detail=f"Unexpected {type(exc).__name__} while checking.")


async def fetch_shopify_catalog(session: aiohttp.ClientSession, base: str, keywords: List[str],
                                pages: int = 2) -> List[dict]:
    found: List[dict] = []
    for page in range(1, pages + 1):
        data = await fetch_json(session, f"{base}/products.json?limit=250&page={page}")
        batch = parse_shopify_catalog(data, base, keywords)
        found.extend(batch)
        if len((data or {}).get("products", [])) < 250:
            break
    return found
