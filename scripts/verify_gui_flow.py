"""Simulate GUI 'Check price now': load items.json, run check_all like the GUI."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from price_monitor.monitor import check_all  # noqa: E402
from price_monitor.storage import default_data_path, load_items, save_items  # noqa: E402


def main() -> int:
    path = default_data_path()
    items = load_items(path)
    if not items:
        print("No items in", path)
        return 1
    it = items[0]
    print("URL:", it.url[:80], "...")
    print("selector:", repr(it.price_selector))
    try:
        fresh = check_all(items, items_path=path, propagate=True)
    except Exception as e:
        print("check_all raised:", repr(e))
        return 2
    save_items(fresh, path)
    x = fresh[0]
    print("last_price:", x.last_price)
    return 0 if x.last_price is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
