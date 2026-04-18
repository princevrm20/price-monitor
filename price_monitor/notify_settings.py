from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class NotifySettings:
    ntfy_topic: str | None
    ntfy_server: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    callmebot_phone: str | None
    callmebot_apikey: str | None
    webhook_url: str | None
    desktop: bool
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_pass: str | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None

    def any_remote(self) -> bool:
        return bool(
            self.ntfy_topic
            or (self.telegram_bot_token and self.telegram_chat_id)
            or (self.callmebot_phone and self.callmebot_apikey)
            or self.webhook_url
            or (self.smtp_host and self.smtp_to)
        )


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def notify_config_path_for_items(items_path: Path) -> Path:
    return items_path.parent / "notify.json"


def read_notify_config_file(items_path: Path | None = None) -> dict[str, Any]:
    """Values as stored in ``notify.json`` (no environment-variable overlay)."""
    from price_monitor.storage import default_data_path

    ip = items_path or default_data_path()
    return _read_json_file(notify_config_path_for_items(ip))


def write_notify_config_file(items_path: Path, data: dict[str, Any]) -> None:
    """Write ``notify.json`` next to ``items.json`` (parent directory is created if needed)."""
    path = notify_config_path_for_items(items_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def notify_settings_from_stored_dict(data: dict[str, Any]) -> NotifySettings:
    """Build ``NotifySettings`` from a ``notify.json``-shaped dict (file only; ignores env)."""
    ntfy_topic = str(data.get("ntfy_topic") or "").strip() or None
    ntfy_server = str(data.get("ntfy_server") or "https://ntfy.sh").strip().rstrip("/") or "https://ntfy.sh"
    tg_token = str(data.get("telegram_bot_token") or "").strip() or None
    tg_chat = str(data.get("telegram_chat_id") or "").strip() or None
    cb_phone = str(data.get("callmebot_phone") or "").strip() or None
    cb_key = str(data.get("callmebot_apikey") or "").strip() or None
    webhook = str(data.get("webhook_url") or "").strip() or None
    desktop = bool(data.get("desktop", True))
    smtp_host = str(data.get("smtp_host") or "").strip() or None
    smtp_port = int(data.get("smtp_port") or 587)
    smtp_user = str(data.get("smtp_user") or "").strip() or None
    smtp_pass = str(data.get("smtp_pass") or "").strip() or None
    smtp_from = str(data.get("smtp_from") or "").strip() or None
    smtp_to = str(data.get("smtp_to") or "").strip() or None
    return NotifySettings(
        ntfy_topic=ntfy_topic,
        ntfy_server=ntfy_server,
        telegram_bot_token=tg_token,
        telegram_chat_id=tg_chat,
        callmebot_phone=cb_phone,
        callmebot_apikey=cb_key,
        webhook_url=webhook,
        desktop=desktop,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
        smtp_from=smtp_from,
        smtp_to=smtp_to,
    )


def build_notify_settings(user_config: dict[str, Any], global_config: dict[str, Any]) -> NotifySettings:
    """Merge per-user destination fields with global SMTP server config."""
    def _s(d: dict, key: str) -> str | None:
        return str(d.get(key) or "").strip() or None

    return NotifySettings(
        ntfy_topic=_s(user_config, "ntfy_topic"),
        ntfy_server=(str(user_config.get("ntfy_server") or "").strip().rstrip("/") or "https://ntfy.sh"),
        telegram_bot_token=_s(user_config, "telegram_bot_token"),
        telegram_chat_id=_s(user_config, "telegram_chat_id"),
        callmebot_phone=_s(user_config, "callmebot_phone"),
        callmebot_apikey=_s(user_config, "callmebot_apikey"),
        webhook_url=_s(user_config, "webhook_url"),
        desktop=False,
        smtp_host=_s(global_config, "smtp_host"),
        smtp_port=int(global_config.get("smtp_port") or 587),
        smtp_user=_s(global_config, "smtp_user"),
        smtp_pass=_s(global_config, "smtp_pass"),
        smtp_from=_s(global_config, "smtp_from"),
        smtp_to=_s(user_config, "smtp_to"),
    )


def load_notify_settings(items_path: Path | None = None) -> NotifySettings:
    from price_monitor.storage import default_data_path

    ip = items_path or default_data_path()
    data = _read_json_file(notify_config_path_for_items(ip))

    def jget(key: str, default: Any = None) -> Any:
        return data.get(key, default)

    ntfy_topic = _env("PRICE_MONITOR_NTFY_TOPIC") or (str(jget("ntfy_topic") or "").strip() or None)
    ntfy_server = (
        _env("PRICE_MONITOR_NTFY_SERVER")
        or str(jget("ntfy_server") or "https://ntfy.sh").strip().rstrip("/")
        or "https://ntfy.sh"
    )

    tg_token = _env("PRICE_MONITOR_TELEGRAM_BOT_TOKEN") or (str(jget("telegram_bot_token") or "").strip() or None)
    tg_chat = _env("PRICE_MONITOR_TELEGRAM_CHAT_ID") or (str(jget("telegram_chat_id") or "").strip() or None)

    cb_phone = _env("PRICE_MONITOR_CALLMEBOT_PHONE") or (str(jget("callmebot_phone") or "").strip() or None)
    cb_key = _env("PRICE_MONITOR_CALLMEBOT_APIKEY") or (str(jget("callmebot_apikey") or "").strip() or None)

    webhook = _env("PRICE_MONITOR_WEBHOOK_URL") or (str(jget("webhook_url") or "").strip() or None)

    desktop_env = _env("PRICE_MONITOR_DESKTOP")
    if desktop_env is not None:
        desktop = desktop_env.lower() in ("1", "true", "yes", "on")
    else:
        desktop = bool(jget("desktop", True))

    smtp_host = _env("SMTP_HOST") or (str(jget("smtp_host") or "").strip() or None)
    smtp_port = int(_env("SMTP_PORT") or jget("smtp_port") or 587)
    smtp_user = _env("SMTP_USER") or (str(jget("smtp_user") or "").strip() or None)
    smtp_pass = _env("SMTP_PASS") or (str(jget("smtp_pass") or "").strip() or None)
    smtp_from = _env("SMTP_FROM") or (str(jget("smtp_from") or "").strip() or None)
    smtp_to = _env("SMTP_TO") or (str(jget("smtp_to") or "").strip() or None)

    return NotifySettings(
        ntfy_topic=ntfy_topic,
        ntfy_server=ntfy_server,
        telegram_bot_token=tg_token,
        telegram_chat_id=tg_chat,
        callmebot_phone=cb_phone,
        callmebot_apikey=cb_key,
        webhook_url=webhook,
        desktop=desktop,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
        smtp_from=smtp_from,
        smtp_to=smtp_to,
    )
