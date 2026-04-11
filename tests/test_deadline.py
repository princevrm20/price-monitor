from __future__ import annotations

from datetime import datetime, timezone

from price_monitor.deadline import is_past_deadline, parse_monitor_until


def test_parse_date_only_end_of_utc_day():
    d = parse_monitor_until("2026-04-30")
    assert d == datetime(2026, 4, 30, 23, 59, 59, tzinfo=timezone.utc)


def test_parse_iso_z():
    d = parse_monitor_until("2026-01-02T03:04:05Z")
    assert d == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_is_past_deadline():
    assert is_past_deadline("2000-01-01", datetime(2026, 1, 1, tzinfo=timezone.utc)) is True
    assert is_past_deadline("2099-01-01", datetime(2026, 1, 1, tzinfo=timezone.utc)) is False
    assert is_past_deadline(None, datetime(2026, 1, 1, tzinfo=timezone.utc)) is False
