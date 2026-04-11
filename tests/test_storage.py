from __future__ import annotations

import json

from price_monitor.storage import TrackedItem, load_items, new_item, save_items


def test_roundtrip_items(tmp_path):
    p = tmp_path / "items.json"
    items = [
        new_item("A", "https://a.test", 10.0, ".p"),
        TrackedItem(
            id="fixed-id",
            name="B",
            url="https://b.test",
            budget=5.0,
            last_price=4.99,
            notified_at_budget=True,
        ),
    ]
    save_items(items, p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw[0]["name"] == "A"
    assert raw[1]["id"] == "fixed-id"

    loaded = load_items(p)
    assert len(loaded) == 2
    assert loaded[1].last_price == 4.99
    assert loaded[1].notified_at_budget is True
