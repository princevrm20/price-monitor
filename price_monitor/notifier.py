from __future__ import annotations

import sys
from urllib.parse import urlencode

import httpx

from price_monitor.notify_settings import NotifySettings


def _desktop_notify(title: str, message: str, timeout: int = 12) -> None:
    from plyer import notification

    # Windows tray API limits title length; avoid plyer background thread crash.
    safe_title = title[:64] if len(title) > 64 else title
    notification.notify(title=safe_title, message=message, timeout=timeout, app_name="Price Monitor")


def _ntfy(server: str, topic: str, title: str, message: str) -> None:
    url = f"{server}/{topic}"
    headers = {"Title": title, "Priority": "high", "Tags": "money"}
    with httpx.Client(timeout=30.0) as client:
        r = client.post(url, content=message, headers=headers)
        r.raise_for_status()


def _telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with httpx.Client(timeout=30.0) as client:
        r = client.post(url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
        r.raise_for_status()


def _callmebot(phone: str, apikey: str, text: str) -> None:
    q = urlencode({"phone": phone, "text": text, "apikey": apikey})
    url = f"https://api.callmebot.com/whatsapp.php?{q}"
    with httpx.Client(timeout=30.0) as client:
        r = client.get(url)
        r.raise_for_status()
        body = (r.text or "").lower()
        if "error" in body and "success" not in body:
            raise RuntimeError(f"CallMeBot response: {r.text[:500]}")


def _webhook(webhook_url: str, title: str, message: str) -> None:
    payload = {"title": title, "message": message, "source": "price_monitor"}
    with httpx.Client(timeout=30.0) as client:
        r = client.post(webhook_url, json=payload)
        r.raise_for_status()


def notify_price_alert(settings: NotifySettings, title: str, message: str) -> bool:
    """Send to all configured channels. Returns True if at least one channel succeeded."""
    errors: list[str] = []
    delivered = False

    if not settings.desktop and not settings.any_remote():
        print(
            "[notify] No channels configured. Add data/notify.json next to items.json "
            "(see notify.example.json) or set PRICE_MONITOR_* env vars.",
            file=sys.stderr,
        )
        return False

    if settings.desktop:
        try:
            _desktop_notify(title, message)
            delivered = True
        except Exception as e:  # noqa: BLE001
            errors.append(f"desktop: {e}")

    if settings.ntfy_topic:
        try:
            _ntfy(settings.ntfy_server, settings.ntfy_topic, title, message)
            delivered = True
        except Exception as e:  # noqa: BLE001
            errors.append(f"ntfy: {e}")

    if settings.telegram_bot_token and settings.telegram_chat_id:
        try:
            _telegram(settings.telegram_bot_token, settings.telegram_chat_id, f"{title}\n{message}")
            delivered = True
        except Exception as e:  # noqa: BLE001
            errors.append(f"telegram: {e}")

    if settings.callmebot_phone and settings.callmebot_apikey:
        try:
            _callmebot(settings.callmebot_phone, settings.callmebot_apikey, f"{title}\n{message}")
            delivered = True
        except Exception as e:  # noqa: BLE001
            errors.append(f"whatsapp(callmebot): {e}")

    if settings.webhook_url:
        try:
            _webhook(settings.webhook_url, title, message)
            delivered = True
        except Exception as e:  # noqa: BLE001
            errors.append(f"webhook: {e}")

    if errors:
        for line in errors:
            print(f"[notify] {line}", file=sys.stderr)

    return delivered
