from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import httpx

from price_monitor.amazon_url import shorten_amazon_product_url
from price_monitor.deadline import is_past_deadline
from price_monitor.notify_settings import NotifySettings, load_notify_settings
from price_monitor.notifier import notify_price_alert
from price_monitor.parser_price import extract_price
from price_monitor.storage import TrackedItem, load_items, save_items


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9,en-US;q=0.8",
}


def _resolve_tls_verify() -> bool | str:
    """Prefer certifi CA bundle; set PRICE_MONITOR_HTTP_VERIFY=0 to disable verification entirely."""
    v = (os.environ.get("PRICE_MONITOR_HTTP_VERIFY") or "1").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    try:
        import certifi

        return certifi.where()
    except Exception:
        return True


def _tls_retryable(exc: Exception) -> bool:
    low = str(exc).lower()
    return "certificate" in low or "ssl" in low or "tls" in low


def _read_html_from_file_url(url: str) -> str | None:
    """Return page HTML if url is a file:// URI; otherwise None."""
    s = url.strip()
    if not s.lower().startswith("file:"):
        return None
    parsed = urlparse(s)
    if parsed.scheme != "file":
        return None
    local_path = url2pathname(unquote(parsed.path))
    p = Path(local_path)
    if not p.is_file():
        raise FileNotFoundError(f"file URL does not point to an existing file: {p}")
    return p.read_text(encoding="utf-8", errors="replace")


def _fetch_html_once(url: str, timeout: httpx.Timeout, verify: bool | str) -> str:
    with httpx.Client(
        headers=DEFAULT_HEADERS,
        follow_redirects=True,
        timeout=timeout,
        verify=verify,
    ) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def fetch_html(url: str, timeout: httpx.Timeout | float | None = None) -> str:
    url = shorten_amazon_product_url(url.strip())
    file_html = _read_html_from_file_url(url)
    if file_html is not None:
        return file_html
    t = timeout if timeout is not None else httpx.Timeout(40.0, connect=12.0)
    verify = _resolve_tls_verify()
    if verify is False:
        return _fetch_html_once(url, t, False)
    try:
        return _fetch_html_once(url, t, verify)
    except Exception as e:
        allow = (os.environ.get("PRICE_MONITOR_SSL_INSECURE_RETRY") or "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        if allow and _tls_retryable(e):
            print(
                "[price_monitor] TLS verification failed; retrying once without verifying the certificate.",
                file=sys.stderr,
            )
            return _fetch_html_once(url, t, False)
        raise


def check_item(item: TrackedItem, settings: NotifySettings) -> TrackedItem:
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    item.last_checked_iso = now_iso
    item.last_notify_delivered = None

    if item.monitor_until_iso and is_past_deadline(item.monitor_until_iso, now_dt):
        if not item.deadline_notified:
            notify_price_alert(
                settings,
                title=f"Monitoring ended: {item.name}",
                message="The monitor-until deadline has passed. Automatic checks will skip this item.",
            )
            item.deadline_notified = True
        return item

    html = fetch_html(item.url)
    price = extract_price(html, item.price_selector, item.url)
    item.last_price = price

    if price is None:
        return item

    if price > item.budget and item.notified_at_budget:
        item.notified_at_budget = False

    if price <= item.budget and not item.notified_at_budget:
        title = f"Price alert: {item.name}"
        message = f"Now {price:g} (budget {item.budget:g}).\n{item.url}"
        item.last_notify_delivered = notify_price_alert(settings, title=title, message=message)
        item.notified_at_budget = True

    return item


def check_all(
    items: list[TrackedItem],
    *,
    items_path: Path,
    on_error: str = "print",
    propagate: bool = False,
) -> list[TrackedItem]:
    settings = load_notify_settings(items_path)
    updated: list[TrackedItem] = []
    for item in items:
        try:
            updated.append(check_item(item, settings))
        except Exception as e:  # noqa: BLE001 — surface per-item failures without stopping the batch
            if on_error == "print":
                print(f"[error] {item.name} ({item.url}): {e}", file=sys.stderr)
            updated.append(item)
            if propagate:
                raise
    return updated


def run_loop(interval_seconds: int, data_path: str | None = None) -> None:
    from price_monitor.storage import default_data_path, save_items

    path = Path(data_path) if data_path else default_data_path()
    import time

    while True:
        items = load_items(path)
        if not items:
            print("No items tracked. Use: python -m price_monitor add ...", file=sys.stderr)
        else:
            fresh = check_all(items, items_path=path)
            save_items(fresh, path)
            if all(
                i.last_price is not None and i.last_price <= i.budget and i.notified_at_budget for i in fresh
            ):
                print(
                    "Price is at or below budget for all items (alerts already sent). Exiting monitor loop.",
                    file=sys.stderr,
                )
                return
        time.sleep(max(60, interval_seconds))
