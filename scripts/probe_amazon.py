"""One-off probe: fetch Amazon HTML and try price extraction."""
from __future__ import annotations

import httpx
from price_monitor.parser_price import extract_price

URL = "https://www.amazon.com/dp/B09B8XVQC7"  # common consumer ASIN (varies by region)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def main() -> None:
    r = httpx.get(URL, headers=HEADERS, follow_redirects=True, timeout=30)
    print("status", r.status_code, "len", len(r.text))
    html = r.text
    markers = [
        "a-offscreen",
        "priceblock_ourprice",
        "priceblock_dealprice",
        "corePrice_feature_div",
        "twister-plus-price-data-price",
    ]
    for m in markers:
        print(m, m in html)
    print("auto", extract_price(html, None))
    for sel in [
        "#corePriceDisplay_desktop_feature_div .a-offscreen",
        "#corePrice_feature_div .a-offscreen",
        "span.a-offscreen",
    ]:
        print(sel, extract_price(html, sel))


if __name__ == "__main__":
    main()
