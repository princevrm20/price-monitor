from __future__ import annotations

from pathlib import Path

from price_monitor.notify_settings import (
    notify_settings_from_stored_dict,
    read_notify_config_file,
    write_notify_config_file,
)


def test_write_read_notify_roundtrip(tmp_path: Path) -> None:
    items = tmp_path / "items.json"
    items.write_text("[]", encoding="utf-8")
    payload = {
        "desktop": False,
        "ntfy_topic": "my-secret-topic",
        "ntfy_server": "https://ntfy.sh",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "webhook_url": "",
        "callmebot_phone": "",
        "callmebot_apikey": "",
    }
    write_notify_config_file(items, payload)
    raw = read_notify_config_file(items)
    assert raw.get("ntfy_topic") == "my-secret-topic"
    assert raw.get("desktop") is False
    s = notify_settings_from_stored_dict(raw)
    assert s.ntfy_topic == "my-secret-topic"
    assert s.desktop is False
    assert s.any_remote() is True
