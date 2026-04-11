from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import httpx

from price_monitor.notify_settings import NotifySettings
from price_monitor.notifier import notify_price_alert
from price_monitor.storage import TrackedTrain

_API_BASE = "https://indianrailapi.com/api/v2"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _api_key() -> str | None:
    v = os.environ.get("INDIAN_RAIL_API_KEY", "").strip()
    return v or None


def _availability_is_available(status: str) -> bool:
    """Return True when the status string indicates confirmed seats exist."""
    s = status.upper().strip()
    if s.startswith("AVAILABLE") or s.startswith("AVL"):
        return True
    if re.match(r"^(CNF|CONFIRMED)", s):
        return True
    return False


def fetch_fare(
    api_key: str,
    train_number: str,
    from_station: str,
    to_station: str,
    quota: str = "GN",
) -> dict:
    """Call Indian Rail API TrainFare endpoint.

    Returns the raw JSON dict.  Raises on HTTP or API errors.
    """
    url = (
        f"{_API_BASE}/TrainFare"
        f"/apikey/{api_key}"
        f"/TrainNumber/{train_number}"
        f"/From/{from_station}"
        f"/To/{to_station}"
        f"/Quota/{quota}"
    )
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.json()


def fetch_availability(
    api_key: str,
    train_number: str,
    from_station: str,
    to_station: str,
    date: str,
    class_code: str,
) -> dict:
    """Call Indian Rail API SeatAvailability endpoint.

    *date* must be in ``yyyyMMdd`` format (e.g. ``20260415``).
    Returns the raw JSON dict.
    """
    url = (
        f"{_API_BASE}/SeatAvailability"
        f"/apikey/{api_key}"
        f"/TrainNumber/{train_number}"
        f"/From/{from_station}"
        f"/To/{to_station}"
        f"/Date/{date}"
        f"/Quota/GN"
        f"/Class/{class_code}"
    )
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.json()


def _date_to_api(dd_mm_yyyy: str) -> str:
    """Convert DD-MM-YYYY to yyyyMMdd for the availability API."""
    parts = dd_mm_yyyy.strip().split("-")
    if len(parts) == 3 and len(parts[2]) == 4:
        return f"{parts[2]}{parts[1]}{parts[0]}"
    return dd_mm_yyyy.replace("-", "")


def _fare_for_class(fares_list: list[dict], class_code: str) -> float | None:
    for f in fares_list:
        if f.get("Code", "").upper() == class_code.upper():
            try:
                return float(f["Fare"])
            except (KeyError, ValueError, TypeError):
                return None
    return None


def check_train(train: TrackedTrain, settings: NotifySettings) -> TrackedTrain:
    """Run fare + availability check for a single train, send notifications."""
    key = _api_key()
    if not key:
        raise RuntimeError("INDIAN_RAIL_API_KEY environment variable is not set")

    now_iso = datetime.now(timezone.utc).isoformat()
    train.last_checked_iso = now_iso

    try:
        fare_data = fetch_fare(key, train.train_number, train.from_station, train.to_station, train.quota)
        if fare_data.get("Fares"):
            price = _fare_for_class(fare_data["Fares"], train.class_code)
            if price is not None:
                train.last_fare = price
            if not train.train_name and fare_data.get("TrainName"):
                train.train_name = fare_data["TrainName"]
    except Exception:  # noqa: BLE001
        pass

    try:
        api_date = _date_to_api(train.travel_date)
        avail_data = fetch_availability(
            key, train.train_number, train.from_station, train.to_station,
            api_date, train.class_code,
        )
        if avail_data.get("Availability"):
            first = avail_data["Availability"][0]
            train.last_availability = first.get("Availability", "Unknown")
    except Exception:  # noqa: BLE001
        pass

    label = train.train_name or train.train_number

    if (
        train.fare_budget is not None
        and train.last_fare is not None
        and train.last_fare <= train.fare_budget
        and not train.notified_fare
    ):
        notify_price_alert(
            settings,
            title=f"Train fare alert: {label}",
            message=(
                f"{train.class_code} fare is {train.last_fare:g} "
                f"(budget {train.fare_budget:g}).\n"
                f"{train.from_station} \u2192 {train.to_station} on {train.travel_date}"
            ),
        )
        train.notified_fare = True

    if train.last_fare is not None and train.fare_budget is not None and train.last_fare > train.fare_budget:
        train.notified_fare = False

    if (
        train.notify_on_available
        and train.last_availability
        and _availability_is_available(train.last_availability)
        and not train.notified_available
    ):
        notify_price_alert(
            settings,
            title=f"Seats available: {label}",
            message=(
                f"{train.class_code} status: {train.last_availability}\n"
                f"{train.from_station} \u2192 {train.to_station} on {train.travel_date}"
            ),
        )
        train.notified_available = True

    if train.last_availability and not _availability_is_available(train.last_availability):
        train.notified_available = False

    return train
