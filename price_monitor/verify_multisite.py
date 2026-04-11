"""Smoke-test price parsing across retailers, travel, and fixtures (retry loop)."""
from __future__ import annotations

import os
import time
from pathlib import Path

from price_monitor.monitor import fetch_html
from price_monitor.parser_price import extract_price

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = _PACKAGE_ROOT / "tests" / "fixtures"

CASES: list[dict[str, object]] = [
    {
        "id": "flipkart",
        "source": "live",
        "url": "https://www.flipkart.com/poco-c75-5g-silver-stardust-64-gb/p/itm8e3f17ac2e724",
        "min_price": 500.0,
    },
    {
        "id": "myntra",
        "source": "live",
        "url": "https://www.myntra.com/tshirts/tqh/tqh-men-typography-printed-slim-fit-t-shirt/36036023/buy",
        "min_price": 50.0,
    },
    {
        "id": "ixigo_flight_del_pnq",
        "source": "live",
        "url": "https://www.ixigo.com/cheap-flights/new-delhi-pune-del-pnq",
        "min_price": 500.0,
    },
    {
        "id": "ajio",
        "source": "fixture",
        "fixture": FIXTURES / "ajio_product_sample.html",
        "url": "https://www.ajio.com/ajio-checked-slim-fit-shirt-with-back-print/p/460278215_blue",
        "min_price": 500.0,
        "expect": 1299.0,
    },
    {
        "id": "udemy_course",
        "source": "fixture",
        "fixture": FIXTURES / "udemy_course_sample.html",
        "url": "https://www.udemy.com/course/web-scraping-python-tutorial/",
        "min_price": 100.0,
        "expect": 449.0,
    },
    {
        "id": "train_jammu_kanpur",
        "source": "fixture",
        "fixture": FIXTURES / "train_fare_sample.html",
        "url": "https://erail.in/trains/jammu-tawi-JAT/kanpur-central-CNB",
        "min_price": 100.0,
        "expect": 2850.0,
    },
]


def load_html(case: dict[str, object]) -> str:
    if case["source"] == "fixture":
        path = Path(case["fixture"])
        return path.read_text(encoding="utf-8")
    return fetch_html(str(case["url"]))


def run_once() -> tuple[bool, list[str]]:
    lines: list[str] = []
    all_ok = True
    for case in CASES:
        cid = str(case["id"])
        url = str(case["url"])
        min_p = float(case["min_price"])
        try:
            html = load_html(case)
        except Exception as e:  # noqa: BLE001
            lines.append(f"{cid}: FETCH_FAIL {e!r}")
            all_ok = False
            continue
        price = extract_price(html, None, product_url=url)
        exp = case.get("expect")
        if exp is not None and price != float(exp):
            lines.append(f"{cid}: price={price} expected={exp}")
            all_ok = False
            continue
        if price is None or price < min_p:
            lines.append(f"{cid}: price={price} min_ok={min_p} len={len(html)}")
            all_ok = False
        else:
            lines.append(f"{cid}: OK price={price}")
    return all_ok, lines


def verify_multisite_loop(*, max_rounds: int, sleep_s: float) -> int:
    os.environ.setdefault("PRICE_MONITOR_SSL_INSECURE_RETRY", "1")
    for r in range(1, max_rounds + 1):
        ok, lines = run_once()
        print(f"--- round {r}/{max_rounds} ---")
        for ln in lines:
            print(ln)
        if ok:
            print("ALL_OK")
            return 0
        time.sleep(sleep_s)
    print("GAVE_UP_AFTER_MAX_ROUNDS")
    return 1


def main(argv: list[str] | None = None) -> int:
    import argparse

    os.environ.setdefault("PRICE_MONITOR_SSL_INSECURE_RETRY", "1")

    p = argparse.ArgumentParser(
        description="Run parser smoke tests (live Flipkart/Myntra/ixigo flight + HTML fixtures). Retries until all pass.",
    )
    p.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help="Max retry rounds (default: VERIFY_MULTISITE_MAX_ROUNDS or 30)",
    )
    p.add_argument(
        "--sleep",
        type=float,
        default=None,
        help="Seconds between rounds (default: VERIFY_MULTISITE_SLEEP or 3)",
    )
    args = p.parse_args(argv)

    max_rounds = args.max_rounds
    if max_rounds is None:
        max_rounds = int(os.environ.get("VERIFY_MULTISITE_MAX_ROUNDS", "30"))
    sleep_s = args.sleep
    if sleep_s is None:
        sleep_s = float(os.environ.get("VERIFY_MULTISITE_SLEEP", "3"))

    return verify_multisite_loop(max_rounds=max_rounds, sleep_s=sleep_s)


if __name__ == "__main__":
    raise SystemExit(main())
