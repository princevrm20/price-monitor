from __future__ import annotations

from unittest.mock import patch

from price_monitor.monitor import check_item, fetch_html
from price_monitor.notify_settings import NotifySettings
from price_monitor.storage import TrackedItem


def _settings_no_desktop() -> NotifySettings:
    return NotifySettings(
        ntfy_topic=None,
        ntfy_server="https://ntfy.sh",
        telegram_bot_token=None,
        telegram_chat_id=None,
        callmebot_phone=None,
        callmebot_apikey=None,
        webhook_url=None,
        desktop=False,
    )


def test_check_item_notifies_when_at_budget():
    html = '<html><body><div class="p">$50.00</div></body></html>'
    item = TrackedItem(
        id="1",
        name="Thing",
        url="https://x.test",
        budget=50.0,
        price_selector=".p",
    )
    with patch("price_monitor.monitor.fetch_html", return_value=html):
        with patch("price_monitor.monitor.notify_price_alert", return_value=True) as n:
            out = check_item(item, _settings_no_desktop())
    assert out.last_price == 50.0
    assert out.notified_at_budget is True
    assert out.last_notify_delivered is True
    n.assert_called_once()


def test_check_item_skips_fetch_after_deadline():
    item = TrackedItem(
        id="1",
        name="Thing",
        url="https://x.test",
        budget=50.0,
        price_selector=".p",
        monitor_until_iso="2000-01-01",
    )
    with patch("price_monitor.monitor.fetch_html") as fh:
        with patch("price_monitor.monitor.notify_price_alert") as n:
            out = check_item(item, _settings_no_desktop())
    fh.assert_not_called()
    assert out.deadline_notified is True


def test_check_item_no_second_notification_until_price_rises():
    html = '<html><body><div class="p">$50.00</div></body></html>'
    item = TrackedItem(
        id="1",
        name="Thing",
        url="https://x.test",
        budget=50.0,
        price_selector=".p",
        notified_at_budget=True,
    )
    with patch("price_monitor.monitor.fetch_html", return_value=html):
        with patch("price_monitor.monitor.notify_price_alert") as n:
            check_item(item, _settings_no_desktop())
    n.assert_not_called()

    html_up = '<html><body><div class="p">$60.00</div></body></html>'
    with patch("price_monitor.monitor.fetch_html", return_value=html_up):
        with patch("price_monitor.monitor.notify_price_alert"):
            mid = check_item(item, _settings_no_desktop())
    assert mid.notified_at_budget is False

    html_down = '<html><body><div class="p">$45.00</div></body></html>'
    with patch("price_monitor.monitor.fetch_html", return_value=html_down):
        with patch("price_monitor.monitor.notify_price_alert", return_value=True) as n2:
            final = check_item(mid, _settings_no_desktop())
    assert final.last_price == 45.0
    assert final.notified_at_budget is True
    n2.assert_called_once()


def test_fetch_html_reads_file_uri(tmp_path):
    p = tmp_path / "page.html"
    p.write_text("<html><body>snapshot</body></html>", encoding="utf-8")
    assert "snapshot" in fetch_html(p.as_uri())
