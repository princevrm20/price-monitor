from __future__ import annotations

import json

from price_monitor.notify_settings import load_notify_settings, notify_config_path_for_items


def test_notify_config_path_next_to_items(tmp_path):
    items = tmp_path / "nested" / "items.json"
    assert notify_config_path_for_items(items) == tmp_path / "nested" / "notify.json"


def test_load_notify_from_json(tmp_path):
    items_path = tmp_path / "data" / "items.json"
    items_path.parent.mkdir(parents=True, exist_ok=True)
    items_path.write_text("[]", encoding="utf-8")
    cfg = items_path.parent / "notify.json"
    cfg.write_text(
        json.dumps(
            {
                "ntfy_topic": "secret-topic",
                "desktop": False,
                "telegram_bot_token": "",
                "telegram_chat_id": "",
            }
        ),
        encoding="utf-8",
    )
    s = load_notify_settings(items_path)
    assert s.ntfy_topic == "secret-topic"
    assert s.desktop is False
