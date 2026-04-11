from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from price_monitor.monitor import check_all, run_loop
from price_monitor.notify_settings import load_notify_settings
from price_monitor.notifier import notify_price_alert
from price_monitor.storage import default_data_path, load_items, new_item, save_items


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="price_monitor",
        description="Track a product URL and notify when price is at or below your budget.",
    )
    p.add_argument("--data", type=Path, default=None, help="Path to items.json (default: ./data/items.json)")

    sub = p.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="Track a new item")
    add.add_argument("--name", required=True)
    add.add_argument("--url", required=True)
    add.add_argument("--budget", type=float, required=True, help="Notify when price <= this amount")
    add.add_argument(
        "--selector",
        default=None,
        help="CSS selector for the price element (recommended if auto-detect fails)",
    )
    add.add_argument(
        "--until",
        default=None,
        dest="monitor_until",
        help="Stop monitoring after this instant: YYYY-MM-DD or full ISO datetime (UTC if no offset)",
    )

    sub.add_parser("list", help="Show tracked items")

    rm = sub.add_parser("remove", help="Remove an item by id")
    rm.add_argument("--id", dest="item_id", required=True)

    once = sub.add_parser("check", help="Run one check cycle and exit")
    once.add_argument("--quiet", action="store_true")

    nt = sub.add_parser(
        "notify-test",
        help="Send a test alert through configured channels (data/notify.json or PRICE_MONITOR_* env)",
    )

    sub.add_parser("gui", help="Open the graphical window (same as running with no arguments)")

    loop = sub.add_parser("run", help="Check periodically until stopped (Ctrl+C)")
    loop.add_argument(
        "--interval-minutes",
        type=int,
        default=60,
        help="Minutes between full check cycles (minimum 1)",
    )

    vm = sub.add_parser(
        "verify-multisite",
        help="Smoke-test parsers (Flipkart, Myntra, ixigo flight + fixtures); retries until all pass",
    )
    vm.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help="Max rounds (default: env VERIFY_MULTISITE_MAX_ROUNDS or 30)",
    )
    vm.add_argument(
        "--sleep",
        type=float,
        default=None,
        help="Seconds between rounds (default: env VERIFY_MULTISITE_SLEEP or 3)",
    )

    gh = sub.add_parser(
        "gui-harness",
        help="Run automated Tk GUI flows (Save / Check / Monitor) with the window hidden",
    )

    wb = sub.add_parser("web", help="Start the web dashboard for remote / 24×7 deployment")
    wb.add_argument("--host", default="0.0.0.0", help="Bind address (default 0.0.0.0)")
    wb.add_argument("--port", type=int, default=8080, help="Port (default 8080)")

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    path: Path = args.data or default_data_path()

    if args.cmd == "add":
        items = load_items(path)
        items.append(
            new_item(
                name=args.name,
                url=args.url,
                budget=args.budget,
                price_selector=args.selector,
                monitor_until_iso=args.monitor_until,
            )
        )
        save_items(items, path)
        print(f"Saved to {path}")
        return 0

    if args.cmd == "list":
        items = load_items(path)
        if not items:
            print("(no items)")
            return 0
        for i in items:
            sel = i.price_selector or "(auto)"
            lp = f"{i.last_price:g}" if i.last_price is not None else "?"
            print(f"{i.id}\t{i.name}\tbudget={i.budget:g}\tlast={lp}\tselector={sel}\n  {i.url}")
        return 0

    if args.cmd == "remove":
        items = load_items(path)
        n = len(items)
        items = [x for x in items if x.id != args.item_id]
        if len(items) == n:
            print("No item with that id.", file=sys.stderr)
            return 1
        save_items(items, path)
        print("Removed.")
        return 0

    if args.cmd == "check":
        items = load_items(path)
        if not items:
            print("No items to check.", file=sys.stderr)
            return 1
        fresh = check_all(items, items_path=path)
        save_items(fresh, path)
        if not args.quiet:
            for i in fresh:
                lp = f"{i.last_price:g}" if i.last_price is not None else "unknown"
                ok = "AT/Below budget" if i.last_price is not None and i.last_price <= i.budget else "above budget"
                print(f"{i.name}: {lp} ({ok})")
        return 0

    if args.cmd == "notify-test":
        settings = load_notify_settings(path)
        ok = notify_price_alert(
            settings,
            title="Price monitor test",
            message="If you see this on your phone, notifications are configured correctly.",
        )
        print("Notification delivered:", ok)
        return 0 if ok else 1

    if args.cmd == "gui":
        from price_monitor.gui import run_gui

        return run_gui(items_path=path)

    if args.cmd == "run":
        minutes = max(1, args.interval_minutes)
        interval_seconds = minutes * 60
        print(f"Checking every {minutes} minutes. Data file: {path}")
        run_loop(interval_seconds=interval_seconds, data_path=str(path))
        return 0

    if args.cmd == "verify-multisite":
        from price_monitor.verify_multisite import verify_multisite_loop

        max_rounds = args.max_rounds
        if max_rounds is None:
            max_rounds = int(os.environ.get("VERIFY_MULTISITE_MAX_ROUNDS", "30"))
        sleep_s = args.sleep
        if sleep_s is None:
            sleep_s = float(os.environ.get("VERIFY_MULTISITE_SLEEP", "3"))
        return verify_multisite_loop(max_rounds=max_rounds, sleep_s=sleep_s)

    if args.cmd == "gui-harness":
        from price_monitor.gui_harness import run_gui_harness

        return run_gui_harness()

    if args.cmd == "web":
        from price_monitor.web import run_web

        run_web(host=args.host, port=args.port)
        return 0

    return 1
