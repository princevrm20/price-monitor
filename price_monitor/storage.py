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
