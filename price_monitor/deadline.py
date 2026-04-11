from __future__ import annotations

from datetime import datetime, timezone


def parse_monitor_until(value: str | None) -> datetime | None:
    """Return deadline as timezone-aware UTC, or None if not set."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        y, mo, d = (int(s[0:4]), int(s[5:7]), int(s[8:10]))
        return datetime(y, mo, d, 23, 59, 59, tzinfo=timezone.utc)
    raw = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_past_deadline(monitor_until_iso: str | None, now: datetime | None = None) -> bool:
    deadline = parse_monitor_until(monitor_until_iso)
    if deadline is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    else:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
    return now > deadline
