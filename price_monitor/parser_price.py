from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup


def _make_soup(html: str) -> BeautifulSoup:
    """lxml is much faster than html.parser on large pages (e.g. Amazon)."""
    if len(html) > 9_000_000:
        html = html[:9_000_000]
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _first_float(text: str) -> float | None:
    """Pick the first plausible price from visible text."""
    if not text:
        return None
    normalized = text.replace("\xa0", " ").strip()
    candidates: list[float] = []
    for m in re.finditer(r"[\d.,]+", normalized):
        chunk = m.group(0)
        parsed = _parse_number_token(chunk)
        if parsed is not None and parsed > 0:
            candidates.append(parsed)
    if not candidates:
        return None
    return candidates[0]


def _extract_currency_price(text: str) -> float | None:
    """Extract price preceded by a currency symbol/word (₹, Rs., $, etc.)."""
    if not text:
        return None
    normalized = text.replace("\xa0", " ").strip()
    patterns = [
        r"(?:₹|Rs\.?|INR|USD|\$|€|£)\s*([\d,]+(?:\.\d{1,2})?)",
        r"([\d,]+(?:\.\d{1,2})?)\s*(?:₹|Rs\.?|INR|USD|\$|€|£)",
    ]
    for pat in patterns:
        for m in re.finditer(pat, normalized, re.IGNORECASE):
            parsed = _parse_number_token(m.group(1))
            if parsed is not None and parsed >= 1:
                return parsed
    return None


def _parse_number_token(s: str) -> float | None:
    s = s.strip()
    if not s or not re.search(r"\d", s):
        return None
    last_comma = s.rfind(",")
    last_dot = s.rfind(".")
    if last_comma > last_dot:
        # European style: 1.234,56
        s2 = s.replace(".", "").replace(",", ".")
    else:
        # US style: 1,234.56 or 1234.56
        s2 = s.replace(",", "")
    try:
        return float(s2)
    except ValueError:
        return None


def _extract_from_json_ld(soup: BeautifulSoup) -> float | None:
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        objs: list[Any] = data if isinstance(data, list) else [data]
        for obj in objs:
            price = _walk_ld_for_price(obj)
            if price is not None:
                return price
    return None


def _walk_ld_for_price(obj: Any) -> float | None:
    if isinstance(obj, dict):
        t = obj.get("@type")
        types = t if isinstance(t, list) else ([t] if t else [])
        if any(str(x).lower() in ("product", "course") for x in types if x):
            offers = obj.get("offers")
            p = _price_from_offers(offers)
            if p is not None:
                return p
        for v in obj.values():
            p = _walk_ld_for_price(v)
            if p is not None:
                return p
    elif isinstance(obj, list):
        for x in obj:
            p = _walk_ld_for_price(x)
            if p is not None:
                return p
    return None


def _price_from_offers(offers: Any) -> float | None:
    if offers is None:
        return None
    if isinstance(offers, list):
        for o in offers:
            p = _price_from_offers(o)
            if p is not None:
                return p
        return None
    if isinstance(offers, dict):
        if "price" in offers:
            return _parse_number_token(str(offers["price"]))
        if "lowPrice" in offers:
            return _parse_number_token(str(offers["lowPrice"]))
        if "@graph" in offers:
            return _price_from_offers(offers["@graph"])
    return None


def _ixigo_flight_listing_url(url: str | None) -> bool:
    if not url:
        return False
    u = url.lower()
    if "ixigo.com" not in u:
        return False
    return (
        "cheap-flights" in u
        or "/flights/" in u
        or "flight-" in u
        or "/flight/" in u
    )


def _walk_ld_collect_flight_offer_prices(obj: Any, out: list[float]) -> None:
    """Collect schema.org Flight offers.price from JSON-LD (ixigo embeds many Flight blocks)."""
    if isinstance(obj, dict):
        t = obj.get("@type")
        types = t if isinstance(t, list) else ([t] if t else [])
        if any(str(x).lower() == "flight" for x in types if x):
            p = _price_from_offers(obj.get("offers"))
            if p is not None and p >= 50:
                out.append(p)
        for v in obj.values():
            _walk_ld_collect_flight_offer_prices(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _walk_ld_collect_flight_offer_prices(x, out)


def _extract_json_ld_ixigo_flight_min(soup: BeautifulSoup) -> float | None:
    prices: list[float] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        objs: list[Any] = data if isinstance(data, list) else [data]
        for obj in objs:
            _walk_ld_collect_flight_offer_prices(obj, prices)
    if not prices:
        return None
    return min(prices)


def _meta_content(soup: BeautifulSoup, prop: str) -> str | None:
    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    if not tag:
        return None
    return tag.get("content")


def _classes(el: Any) -> list[str]:
    c = el.get("class") if el is not None else None
    if not c:
        return []
    return list(c)


def _price_from_amazon_a_price_container(container: Any) -> float | None:
    """Amazon often leaves .a-offscreen empty and puts digits in .a-price-whole / .a-price-fraction."""
    if container is None:
        return None
    off = container.select_one(".a-offscreen")
    if off is not None:
        t = off.get_text(" ", strip=True)
        p = _first_float(t)
        if p is not None:
            return p
    whole = container.select_one(".a-price-whole")
    frac_el = container.select_one(".a-price-fraction")
    if whole is not None:
        w = whole.get_text(strip=True).replace(",", "")
        f = frac_el.get_text(strip=True) if frac_el is not None else ""
        try:
            if f:
                return float(f"{w}.{f}")
            return float(w)
        except ValueError:
            return None
    return None


def _nearest_a_price_container(node: Any, max_depth: int = 8) -> Any:
    cur: Any = node
    for _ in range(max_depth):
        if cur is None:
            return None
        if "a-price" in set(_classes(cur)):
            return cur
        cur = getattr(cur, "parent", None)
    return None


def _price_from_selected_node(node: Any) -> float | None:
    if node is None:
        return None
    cls = set(_classes(node))
    if "a-offscreen" in cls:
        container = _nearest_a_price_container(node.parent)
        if container is not None:
            p = _price_from_amazon_a_price_container(container)
            if p is not None:
                return p
    if "a-price" in cls:
        p = _price_from_amazon_a_price_container(node)
        if p is not None:
            return p
    return _first_float(node.get_text(" ", strip=True))


def _is_amazon_url(url: str | None) -> bool:
    if not url:
        return False
    u = url.lower()
    return "amazon." in u and ("/dp/" in u or "/gp/product/" in u)


def _amazon_selector_fallbacks() -> list[str]:
    return [
        ".a-price.aok-align-center .a-offscreen",
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        ".reinventPricePriceToPayMargin .a-offscreen",
        "#ppd .a-price .a-offscreen",
        ".a-price.priceToPay .a-price-whole",
        ".priceToPay .a-price-whole",
        ".reinventPricePriceToPayMargin .a-price-whole",
        "#apex_desktop .a-price .a-offscreen",
        "span.a-price.a-text-price .a-offscreen",
        "#corePrice_desktop .a-price .a-offscreen",
        "#buybox .a-price .a-offscreen",
    ]


def _amazon_regex_price_from_html(html: str) -> float | None:
    """Last-resort: Amazon embeds pay price near priceToPay / apex-pricetopay-value."""
    for anchor in ("priceToPay", "apex-pricetopay-value", "data-csa-c-buying-price-currency"):
        for m in re.finditer(re.escape(anchor), html, flags=re.IGNORECASE):
            chunk = html[m.start() : m.start() + 3000]
            wm = re.search(
                r'<span[^>]*class="[^"]*a-price-whole[^"]*"[^>]*>([^<]+)',
                chunk,
                flags=re.IGNORECASE,
            )
            if not wm:
                continue
            frac_m = re.search(
                r'<span[^>]*class="[^"]*a-price-fraction[^"]*"[^>]*>([^<]+)',
                chunk,
                flags=re.IGNORECASE,
            )
            w = wm.group(1).strip().replace(",", "").replace("\xa0", "")
            f = frac_m.group(1).strip() if frac_m else ""
            try:
                if f:
                    return float(f"{w}.{f}")
                return float(w)
            except ValueError:
                continue
    return None


def _is_flipkart_url(url: str | None) -> bool:
    if not url:
        return False
    return "flipkart.com" in url.lower()


def _flipkart_selector_fallbacks() -> list[str]:
    return [
        "div._30jeq3._16Jk6d",
        "div._30jeq3",
        "div._25b18c span._16Jk6d",
        "div.Nx9bqj.CxhGGd",
        "div.Nx9bqj",
        "div._3I9_wc span._16Jk6d",
        "span.B_NuCI",
    ]


def _is_myntra_url(url: str | None) -> bool:
    if not url:
        return False
    return "myntra.com" in url.lower()


def _myntra_selector_fallbacks() -> list[str]:
    return [
        ".pdp-price strong",
        ".pdp-discountedPrice",
        ".pdp-discount-container .pdp-price strong",
        ".pdp-mrpPrice",
        "span.pdp-price",
    ]


def _extract_price_from_scripts(soup: BeautifulSoup) -> float | None:
    """Extract price from inline JS data objects (e.g. Myntra pdpData)."""
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text or len(text) < 50:
            continue
        for pat in [
            r'"discountedPrice"\s*:\s*(\d+(?:\.\d+)?)',
            r'"price"\s*:\s*\{\s*"mrp"\s*:\s*\d+\s*,\s*"discounted"\s*:\s*(\d+(?:\.\d+)?)',
            r'"price"\s*:\s*"?(\d+(?:\.\d+)?)"?\s*,\s*"priceCurrency"',
        ]:
            m = re.search(pat, text)
            if m:
                val = _parse_number_token(m.group(1))
                if val is not None and val >= 1:
                    return val
    return None


def _generic_price_selectors() -> list[str]:
    """Common selectors across Shopify, WooCommerce, and other e-commerce platforms."""
    return [
        # Shopify
        "[data-product-price]",
        ".product__price.on-sale",
        ".product__price:not(.product__price--compare)",
        ".price-item--sale",
        ".price-item--regular",
        ".product-price__price",
        ".product-single__price",
        ".ProductMeta__Price",
        # WooCommerce
        ".woocommerce-Price-amount.amount",
        "ins .woocommerce-Price-amount",
        "p.price ins .amount",
        "p.price .amount",
        # Generic e-commerce patterns
        "[data-price]",
        ".current-price",
        ".sale-price",
        ".offer-price",
        ".special-price .price",
        ".product-price-current",
        "#product-price",
        ".pdp-price",
    ]


def extract_product_info(html: str, product_url: str | None = None) -> dict:
    """Extract product name and sold-out status in a single pass.

    Returns dict with keys: 'name' (str|None), 'sold_out' (bool).
    """
    result: dict = {"name": None, "sold_out": False}

    # Use regex on raw HTML first (cheap, no soup needed)
    # Check meta and JSON-LD via regex before full parse
    lower = html.lower()

    # Quick sold-out check via meta tag
    avail_m = re.search(r'<meta\s+[^>]*property="product:availability"[^>]*content="([^"]*)"', html, re.I)
    if avail_m and avail_m.group(1).lower().replace(" ", "") in ("oos", "outofstock", "soldout"):
        result["sold_out"] = True

    # Extract name from og:title (cheap regex)
    og_m = re.search(r'<meta\s+[^>]*(?:property|name)="og:title"[^>]*content="([^"]*)"', html, re.I)
    if og_m and len(og_m.group(1).strip()) > 2:
        result["name"] = og_m.group(1).strip()

    # Extract name from <title> tag
    if not result["name"]:
        title_m = re.search(r'<title>([^<]{3,200})</title>', html, re.I)
        if title_m:
            raw_title = title_m.group(1).strip()
            for sep in [" - ", " | ", " – ", " — "]:
                if sep in raw_title:
                    raw_title = raw_title.split(sep)[0].strip()
                    break
            if len(raw_title) > 2:
                result["name"] = raw_title

    # Extract name from JSON-LD (regex, avoid full parse)
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.I | re.S):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ld_name = _walk_ld_for_name(data)
        if ld_name:
            result["name"] = ld_name.strip()
        if _ld_has_out_of_stock(data):
            result["sold_out"] = True

    # Myntra-specific: pdpData name (regex)
    if _is_myntra_url(product_url):
        pdp_m = re.search(r'pdpData.*?"name"\s*:\s*"([^"]{3,120})"', html[:100000], re.S)
        if pdp_m:
            result["name"] = pdp_m.group(1).strip()

    # Sold-out check: search visible-ish text (heuristic: avoid <script> blocks)
    if not result["sold_out"]:
        sold_out_phrases = [
            "currently sold out",
            "currently unavailable",
            "this product is currently sold out",
            "this item is no longer available",
            "item is currently out of stock",
            "sold out online",
            "product is out of stock",
        ]
        text_chunks = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.I | re.S)
        text_chunks = re.sub(r'<style[^>]*>.*?</style>', ' ', text_chunks, flags=re.I | re.S)
        text_lower = text_chunks.lower()
        for phrase in sold_out_phrases:
            if phrase in text_lower:
                result["sold_out"] = True
                break

    return result


def _walk_ld_for_name(obj: Any) -> str | None:
    if isinstance(obj, dict):
        t = obj.get("@type")
        types = t if isinstance(t, list) else ([t] if t else [])
        if any(str(x).lower() == "product" for x in types if x):
            name = obj.get("name")
            if name and isinstance(name, str):
                return name
        for v in obj.values():
            n = _walk_ld_for_name(v)
            if n:
                return n
    elif isinstance(obj, list):
        for x in obj:
            n = _walk_ld_for_name(x)
            if n:
                return n
    return None


def _ld_has_out_of_stock(obj: Any) -> bool:
    if isinstance(obj, dict):
        avail = str(obj.get("availability", "")).lower()
        if "outofstock" in avail or "soldout" in avail or "discontinued" in avail:
            return True
        for v in obj.values():
            if _ld_has_out_of_stock(v):
                return True
    elif isinstance(obj, list):
        for x in obj:
            if _ld_has_out_of_stock(x):
                return True
    return False


def names_match(user_name: str, detected_name: str, threshold: float = 0.3) -> bool:
    """Check if two product names are similar enough (word overlap ratio).

    Returns True if the overlap is above the threshold, meaning the names match.
    A threshold of 0.3 means at least 30% of words must overlap.
    """
    if not user_name or not detected_name:
        return True

    def normalize(s: str) -> set[str]:
        return set(re.sub(r"[^a-z0-9\s]", "", s.lower()).split())

    words_user = normalize(user_name)
    words_detected = normalize(detected_name)

    if not words_user or not words_detected:
        return True

    overlap = words_user & words_detected
    max_words = max(len(words_user), len(words_detected))
    return len(overlap) / max_words >= threshold


def extract_price(html: str, price_selector: str | None, product_url: str | None = None) -> float | None:
    soup = _make_soup(html)

    # 1. User-supplied selector first
    selectors: list[str] = []
    if price_selector and price_selector.strip():
        selectors.append(price_selector.strip())

    # 2. Site-specific selectors
    if _is_amazon_url(product_url):
        selectors.extend(s for s in _amazon_selector_fallbacks() if s not in selectors)
    if _is_flipkart_url(product_url):
        selectors.extend(s for s in _flipkart_selector_fallbacks() if s not in selectors)
    if _is_myntra_url(product_url):
        selectors.extend(s for s in _myntra_selector_fallbacks() if s not in selectors)

    for sel in selectors:
        node = soup.select_one(sel)
        if not node:
            continue
        p = _price_from_selected_node(node)
        if p is not None:
            return p

    # 3. Flight listing check (ixigo)
    if _ixigo_flight_listing_url(product_url):
        fp = _extract_json_ld_ixigo_flight_min(soup)
        if fp is not None:
            return fp

    # 4. Amazon regex fallback
    if _is_amazon_url(product_url):
        rx = _amazon_regex_price_from_html(html)
        if rx is not None:
            return rx

    # 5. Open Graph / meta price tags
    og = _meta_content(soup, "product:price:amount") or _meta_content(soup, "og:price:amount")
    if og:
        p = _parse_number_token(str(og))
        if p is not None:
            return p

    # 6. JSON-LD structured data
    ld = _extract_from_json_ld(soup)
    if ld is not None:
        return ld

    # 7. Generic e-commerce selectors (Shopify, WooCommerce, etc.)
    for sel in _generic_price_selectors():
        node = soup.select_one(sel)
        if not node:
            continue
        text = node.get_text(" ", strip=True)
        p = _extract_currency_price(text)
        if p is not None:
            return p
        p = _first_float(text)
        if p is not None and p >= 1:
            return p

    # 8. Price from inline JS data objects (Myntra, etc.)
    sp = _extract_price_from_scripts(soup)
    if sp is not None:
        return sp

    # 9. Look for prices near currency symbols in the page body
    body = soup.body
    if body:
        body_text = body.get_text(" ", strip=True)
        cp = _extract_currency_price(body_text)
        if cp is not None:
            return cp

    return None
