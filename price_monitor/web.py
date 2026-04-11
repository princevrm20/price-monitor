from __future__ import annotations

import os
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from price_monitor.monitor import check_all
from price_monitor.notifier import notify_price_alert
from price_monitor.notify_settings import (
    notify_settings_from_stored_dict,
    read_notify_config_file,
    write_notify_config_file,
)
from price_monitor.storage import (
    TrackedItem, TrackedTrain, default_data_path,
    load_items, save_items, load_trains, save_trains,
)
from price_monitor.train_checker import check_train

app = Flask(__name__)

_lock = threading.Lock()
_monitor_running = False
_monitor_thread: threading.Thread | None = None
_stop_event = threading.Event()
_monitor_interval_min = 60
_log: deque[str] = deque(maxlen=200)


def _data_path() -> Path:
    env = os.environ.get("PRICE_MONITOR_DATA_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p / "items.json"
    return default_data_path()


def _add_log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    _log.appendleft(f"[{ts}] {msg}")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    dp = _data_path()
    items = load_items(dp)
    item = items[0] if items else None
    trains = load_trains(dp)
    train = trains[0] if trains else None
    notify = read_notify_config_file(dp)
    return render_template(
        "index.html",
        item=item,
        train=train,
        monitoring=_monitor_running,
        interval=_monitor_interval_min,
        notify=notify,
        logs=list(_log),
    )


# ---------------------------------------------------------------------------
# Product API
# ---------------------------------------------------------------------------

@app.route("/api/product", methods=["POST"])
def save_product():
    dp = _data_path()
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    url = (data.get("url") or "").strip()
    budget_raw = str(data.get("budget", "")).strip()
    selector = (data.get("selector") or "").strip() or None
    until = (data.get("until") or "").strip() or None

    if not name or not url:
        return jsonify(ok=False, error="Name and URL are required"), 400
    try:
        budget = float(budget_raw.replace(",", "."))
    except (ValueError, AttributeError):
        return jsonify(ok=False, error="Budget must be a number"), 400

    existing = load_items(dp)
    item_id = existing[0].id if existing else str(uuid.uuid4())
    item = TrackedItem(
        id=item_id, name=name, url=url, budget=budget,
        price_selector=selector, monitor_until_iso=until,
    )
    save_items([item], dp)
    _add_log(f"Product saved: {name}")
    return jsonify(ok=True, item=item.to_json())


@app.route("/api/check", methods=["POST"])
def check_now():
    dp = _data_path()
    items = load_items(dp)
    if not items:
        return jsonify(ok=False, error="No product saved yet"), 400
    _add_log("Manual check started\u2026")
    try:
        fresh = check_all(items, items_path=dp, propagate=True)
        save_items(fresh, dp)
        it = fresh[0]
        if it.last_price is not None:
            rel = "at/below budget" if it.last_price <= it.budget else "above budget"
            _add_log(f"Check done: {it.last_price:g} ({rel})")
        else:
            _add_log("Check done: no price found")
        return jsonify(ok=True, item=it.to_json())
    except Exception as e:
        _add_log(f"Check failed: {e}")
        return jsonify(ok=False, error=str(e)), 500


# ---------------------------------------------------------------------------
# Train API
# ---------------------------------------------------------------------------

@app.route("/api/train", methods=["POST"])
def save_train():
    dp = _data_path()
    data = request.get_json(force=True)
    train_number = (data.get("train_number") or "").strip()
    from_st = (data.get("from_station") or "").strip().upper()
    to_st = (data.get("to_station") or "").strip().upper()
    travel_date = (data.get("travel_date") or "").strip()
    class_code = (data.get("class_code") or "SL").strip().upper()
    quota = (data.get("quota") or "GN").strip().upper()
    fare_budget_raw = str(data.get("fare_budget", "") or "").strip()
    notify_avail = bool(data.get("notify_on_available", True))

    if not train_number or not from_st or not to_st or not travel_date:
        return jsonify(ok=False, error="Train number, stations, and date are required"), 400

    fare_budget = None
    if fare_budget_raw:
        try:
            fare_budget = float(fare_budget_raw.replace(",", "."))
        except ValueError:
            return jsonify(ok=False, error="Fare budget must be a number"), 400

    existing = load_trains(dp)
    train_id = existing[0].id if existing else str(uuid.uuid4())
    train = TrackedTrain(
        id=train_id,
        train_number=train_number,
        train_name=data.get("train_name", "").strip(),
        from_station=from_st,
        to_station=to_st,
        travel_date=travel_date,
        class_code=class_code,
        quota=quota,
        fare_budget=fare_budget,
        notify_on_available=notify_avail,
    )
    save_trains([train], dp)
    _add_log(f"Train saved: {train_number} {from_st}\u2192{to_st}")
    return jsonify(ok=True, train=train.to_json())


@app.route("/api/train/check", methods=["POST"])
def check_train_now():
    dp = _data_path()
    trains = load_trains(dp)
    if not trains:
        return jsonify(ok=False, error="No train saved yet"), 400
    _add_log("Train check started\u2026")
    try:
        from price_monitor.notify_settings import load_notify_settings
        settings = load_notify_settings(dp)
        t = check_train(trains[0], settings)
        save_trains([t], dp)
        parts = []
        if t.last_fare is not None:
            parts.append(f"fare {t.last_fare:g}")
        if t.last_availability:
            parts.append(t.last_availability)
        _add_log(f"Train check done: {', '.join(parts) if parts else 'no data'}")
        return jsonify(ok=True, train=t.to_json())
    except Exception as e:
        _add_log(f"Train check failed: {e}")
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/train/status")
def train_status():
    dp = _data_path()
    trains = load_trains(dp)
    train = trains[0] if trains else None
    return jsonify(train=train.to_json() if train else None)


# ---------------------------------------------------------------------------
# Background monitor
# ---------------------------------------------------------------------------

def _monitor_loop() -> None:
    global _monitor_running
    dp = _data_path()
    while _monitor_running:
        items = load_items(dp)
        if items:
            try:
                fresh = check_all(items, items_path=dp)
                save_items(fresh, dp)
                it = fresh[0]
                if it.last_price is not None:
                    rel = "at/below budget" if it.last_price <= it.budget else "above budget"
                    _add_log(f"Product check: {it.last_price:g} ({rel})")
                else:
                    _add_log("Product check: could not read price")
            except Exception as e:  # noqa: BLE001
                _add_log(f"Product check error: {e}")

        trains = load_trains(dp)
        if trains:
            try:
                from price_monitor.notify_settings import load_notify_settings
                settings = load_notify_settings(dp)
                t = check_train(trains[0], settings)
                save_trains([t], dp)
                parts = []
                if t.last_fare is not None:
                    parts.append(f"fare {t.last_fare:g}")
                if t.last_availability:
                    parts.append(t.last_availability)
                _add_log(f"Train check: {', '.join(parts) if parts else 'no data'}")
            except Exception as e:  # noqa: BLE001
                _add_log(f"Train check error: {e}")

        if not items and not trains:
            _add_log("Nothing to monitor")

        if _stop_event.wait(timeout=_monitor_interval_min * 60):
            break
    _monitor_running = False


@app.route("/api/monitor/start", methods=["POST"])
def start_monitor():
    global _monitor_running, _monitor_thread, _monitor_interval_min
    dp = _data_path()
    data = request.get_json(force=True) or {}
    interval = int(data.get("interval", 60))
    _monitor_interval_min = max(1, min(1440, interval))

    with _lock:
        if _monitor_running:
            return jsonify(ok=True, monitoring=True, interval=_monitor_interval_min)
        items = load_items(dp)
        trains = load_trains(dp)
        if not items and not trains:
            return jsonify(ok=False, error="Save a product or train first"), 400
        if items:
            it = items[0]
            it.notified_at_budget = False
            it.last_notify_delivered = None
            save_items([it], dp)
        if trains:
            tr = trains[0]
            tr.notified_fare = False
            tr.notified_available = False
            save_trains([tr], dp)

        _stop_event.clear()
        _monitor_running = True
        _monitor_thread = threading.Thread(target=_monitor_loop, daemon=True)
        _monitor_thread.start()

    _add_log(f"Monitoring started (every {_monitor_interval_min} min)")
    return jsonify(ok=True, monitoring=True, interval=_monitor_interval_min)


@app.route("/api/monitor/stop", methods=["POST"])
def stop_monitor():
    global _monitor_running
    with _lock:
        _monitor_running = False
        _stop_event.set()
    _add_log("Monitoring stopped")
    return jsonify(ok=True, monitoring=False)


# ---------------------------------------------------------------------------
# Status polling
# ---------------------------------------------------------------------------

@app.route("/api/status")
def get_status():
    dp = _data_path()
    items = load_items(dp)
    item = items[0] if items else None
    trains = load_trains(dp)
    train = trains[0] if trains else None
    return jsonify(
        item=item.to_json() if item else None,
        train=train.to_json() if train else None,
        monitoring=_monitor_running,
        interval=_monitor_interval_min,
        logs=list(_log),
    )


# ---------------------------------------------------------------------------
# Notifications API
# ---------------------------------------------------------------------------

@app.route("/api/notifications", methods=["POST"])
def save_notifications():
    dp = _data_path()
    data = request.get_json(force=True)
    write_notify_config_file(dp, data)
    _add_log("Notification settings saved")
    return jsonify(ok=True)


@app.route("/api/notifications/test", methods=["POST"])
def test_notification():
    data = request.get_json(force=True)
    s = notify_settings_from_stored_dict(data)
    if not s.desktop and not s.any_remote():
        return jsonify(ok=False, error="Configure at least one channel first"), 400
    ok = notify_price_alert(
        s,
        title="Price monitor test",
        message="If you received this, notifications are working.",
    )
    if ok:
        _add_log("Test notification: succeeded")
    else:
        _add_log("Test notification: failed — check settings")
    return jsonify(ok=ok)


# ---------------------------------------------------------------------------
# Health (keep-alive endpoint for free-tier hosts)
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify(status="ok", monitoring=_monitor_running)


# ---------------------------------------------------------------------------
# Auto-bootstrap from environment variables
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Seed the product from env vars and auto-start monitoring on boot.

    Render (and similar free hosts) have ephemeral filesystems — the data
    files vanish on every restart.  By reading the product definition and
    notification config from environment variables, the app recreates them
    on every cold start and immediately begins monitoring.
    """
    global _monitor_running, _monitor_thread, _monitor_interval_min

    dp = _data_path()

    prod_url = os.environ.get("PRICE_MONITOR_PRODUCT_URL", "").strip()
    prod_name = os.environ.get("PRICE_MONITOR_PRODUCT_NAME", "").strip()
    prod_budget = os.environ.get("PRICE_MONITOR_PRODUCT_BUDGET", "").strip()

    if prod_url and prod_budget:
        try:
            budget = float(prod_budget.replace(",", "."))
        except ValueError:
            _add_log("PRICE_MONITOR_PRODUCT_BUDGET is not a valid number — skipping auto-setup")
            return
        selector = os.environ.get("PRICE_MONITOR_PRODUCT_SELECTOR", "").strip() or None
        until = os.environ.get("PRICE_MONITOR_PRODUCT_UNTIL", "").strip() or None
        existing = load_items(dp)
        item_id = existing[0].id if existing else str(uuid.uuid4())
        item = TrackedItem(
            id=item_id,
            name=prod_name or "Auto-configured product",
            url=prod_url,
            budget=budget,
            price_selector=selector,
            monitor_until_iso=until,
        )
        save_items([item], dp)
        _add_log(f"Product loaded from env: {item.name}")

    train_num = os.environ.get("PRICE_MONITOR_TRAIN_NUMBER", "").strip()
    train_from = os.environ.get("PRICE_MONITOR_TRAIN_FROM", "").strip().upper()
    train_to = os.environ.get("PRICE_MONITOR_TRAIN_TO", "").strip().upper()
    train_date = os.environ.get("PRICE_MONITOR_TRAIN_DATE", "").strip()

    if train_num and train_from and train_to and train_date:
        train_class = os.environ.get("PRICE_MONITOR_TRAIN_CLASS", "SL").strip().upper()
        train_budget_raw = os.environ.get("PRICE_MONITOR_TRAIN_BUDGET", "").strip()
        train_budget = None
        if train_budget_raw:
            try:
                train_budget = float(train_budget_raw)
            except ValueError:
                pass
        existing_trains = load_trains(dp)
        t_id = existing_trains[0].id if existing_trains else str(uuid.uuid4())
        train = TrackedTrain(
            id=t_id, train_number=train_num,
            train_name="", from_station=train_from, to_station=train_to,
            travel_date=train_date, class_code=train_class, quota="GN",
            fare_budget=train_budget, notify_on_available=True,
        )
        save_trains([train], dp)
        _add_log(f"Train loaded from env: {train_num} {train_from}\u2192{train_to}")

    auto = os.environ.get("PRICE_MONITOR_AUTO_START", "").strip().lower()
    if auto not in ("1", "true", "yes"):
        return

    items = load_items(dp)
    trains = load_trains(dp)
    if not items and not trains:
        return

    interval_raw = os.environ.get("PRICE_MONITOR_CHECK_INTERVAL", "60").strip()
    try:
        _monitor_interval_min = max(1, min(1440, int(interval_raw)))
    except ValueError:
        _monitor_interval_min = 60

    with _lock:
        if _monitor_running:
            return
        _stop_event.clear()
        _monitor_running = True
        _monitor_thread = threading.Thread(target=_monitor_loop, daemon=True)
        _monitor_thread.start()

    _add_log(f"Auto-started monitoring (every {_monitor_interval_min} min)")


# ---------------------------------------------------------------------------
# Self-ping: keep free-tier hosts (Render, etc.) from sleeping
# ---------------------------------------------------------------------------

def _keep_alive_loop() -> None:
    """Ping our own /health endpoint every 13 minutes so the free instance
    stays awake.  Uses RENDER_EXTERNAL_URL (set automatically by Render)
    or PRICE_MONITOR_EXTERNAL_URL if provided manually."""
    import time

    time.sleep(10)
    ext = (
        os.environ.get("RENDER_EXTERNAL_URL")
        or os.environ.get("PRICE_MONITOR_EXTERNAL_URL", "").strip()
    )
    if not ext:
        return
    url = f"{ext.rstrip('/')}/health"
    _add_log(f"Keep-alive enabled: pinging {url} every 13 min")
    while True:
        time.sleep(13 * 60)
        try:
            import httpx
            httpx.get(url, timeout=30)
        except Exception:  # noqa: BLE001
            pass


# Run bootstrap and keep-alive in background threads.
def _deferred_bootstrap() -> None:
    import time
    time.sleep(2)
    try:
        _bootstrap()
    except Exception as exc:  # noqa: BLE001
        _add_log(f"Bootstrap error: {exc}")

threading.Thread(target=_deferred_bootstrap, daemon=True).start()
threading.Thread(target=_keep_alive_loop, daemon=True).start()


def run_web(host: str = "0.0.0.0", port: int = 8080) -> None:
    _add_log("Web dashboard started")
    app.run(host=host, port=port, debug=False)
