"""Probe HTML from various Indian retail / travel sites (read-only)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PRICE_MONITOR_SSL_INSECURE_RETRY", "1")

import httpx  # noqa: E402

from price_monitor.amazon_url import shorten_amazon_product_url  # noqa: E402
from price_monitor.monitor import DEFAULT_HEADERS, fetch_html  # noqa: E402

CASES = [
    ("flipkart", "https://www.flipkart.com/poco-c75-5g-silver-stardust-64-gb/p/itm8e3f17ac2e724"),
    ("myntra", "https://www.myntra.com/tshirts/tqh/tqh-men-typography-printed-slim-fit-t-shirt/36036023/buy"),
    ("ajio", "https://www.ajio.com/ajio-checked-slim-fit-shirt-with-back-print/p/460278215_blue"),
    ("ixigo_flight", "https://www.ixigo.com/cheap-flights/new-delhi-pune-del-pnq"),
    ("ixigo_train", "https://www.ixigo.com/trains/jammu-to-kanpur-trains"),
    ("udemy", "https://www.udemy.com/course/web-scraping-python-tutorial/"),
]


def main() -> None:
    out = Path("scripts/probe_market_out.txt")
    lines: list[str] = []
    for name, url in CASES:
        try:
            html = fetch_html(url)
            lines.append(f"=== {name} OK len={len(html)} url={url[:70]}")
            for needle in (
                "application/ld+json",
                "__NEXT_DATA__",
                "SellingPrice",
                "price",
                "offers",
            ):
                lines.append(f"  {needle!r}: {needle in html}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"=== {name} FAIL {e!r} url={url[:70]}")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
