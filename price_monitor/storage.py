from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class TrackedItem:
    id: str
    name: str
    url: str
    budget: float
    price_selector: str | None = None
    last_price: float | None = None
    last_checked_iso: str | None = None
    notified_at_budget: bool = False
    monitor_until_iso: str | None = None
    deadline_notified: bool = False
    last_notify_delivered: bool | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(data: dict[str, Any]) -> TrackedItem:
        return TrackedItem(
            id=data["id"],
            name=data["name"],
            url=data["url"],
            budget=float(data["budget"]),
            price_selector=data.get("price_selector"),
            last_price=float(data["last_price"]) if data.get("last_price") is not None else None,
            last_checked_iso=data.get("last_checked_iso"),
            notified_at_budget=bool(data.get("notified_at_budget", False)),
            monitor_until_iso=data.get("monitor_until_iso"),
            deadline_notified=bool(data.get("deadline_notified", False)),
            last_notify_delivered=(
                bool(data["last_notify_delivered"])
                if data.get("last_notify_delivered") is not None
                else None
            ),
        )


def default_data_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "items.json"


def load_items(path: Path | None = None) -> list[TrackedItem]:
    p = path or default_data_path()
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    return [TrackedItem.from_json(x) for x in raw]


def save_items(items: list[TrackedItem], path: Path | None = None) -> None:
    p = path or default_data_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = [i.to_json() for i in items]
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def new_item(
    name: str,
    url: str,
    budget: float,
    price_selector: str | None,
    monitor_until_iso: str | None = None,
) -> TrackedItem:
    return TrackedItem(
        id=str(uuid.uuid4()),
        name=name,
        url=url,
        budget=budget,
        price_selector=price_selector,
        monitor_until_iso=monitor_until_iso,
    )


# ---------------------------------------------------------------------------
# Train tracking
# ---------------------------------------------------------------------------

@dataclass
class TrackedTrain:
    id: str
    train_number: str
    train_name: str
    from_station: str
    to_station: str
    travel_date: str
    class_code: str
    quota: str
    fare_budget: float | None = None
    notify_on_available: bool = True
    last_fare: float | None = None
    last_availability: str | None = None
    last_checked_iso: str | None = None
    notified_fare: bool = False
    notified_available: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(data: dict[str, Any]) -> TrackedTrain:
        fb = data.get("fare_budget")
        return TrackedTrain(
            id=data["id"],
            train_number=data["train_number"],
            train_name=data.get("train_name", ""),
            from_station=data["from_station"],
            to_station=data["to_station"],
            travel_date=data["travel_date"],
            class_code=data.get("class_code", "SL"),
            quota=data.get("quota", "GN"),
            fare_budget=float(fb) if fb is not None else None,
            notify_on_available=bool(data.get("notify_on_available", True)),
            last_fare=float(data["last_fare"]) if data.get("last_fare") is not None else None,
            last_availability=data.get("last_availability"),
            last_checked_iso=data.get("last_checked_iso"),
            notified_fare=bool(data.get("notified_fare", False)),
            notified_available=bool(data.get("notified_available", False)),
        )


def default_trains_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "trains.json"


def trains_path_for_items(items_path: Path) -> Path:
    return items_path.parent / "trains.json"


def load_trains(items_path: Path | None = None) -> list[TrackedTrain]:
    p = trains_path_for_items(items_path) if items_path else default_trains_path()
    if not p.exists():
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    return [TrackedTrain.from_json(x) for x in raw]


def save_trains(trains: list[TrackedTrain], items_path: Path | None = None) -> None:
    p = trains_path_for_items(items_path) if items_path else default_trains_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = [t.to_json() for t in trains]
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
