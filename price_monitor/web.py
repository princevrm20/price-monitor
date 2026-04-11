from __future__ import annotations

import csv
import io
import os
import secrets
import threading
from collections import deque
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, jsonify, redirect, render_template, request, session, url_for,
)

from price_monitor.db import (
    add_event, create_monitor, delete_monitor, get_events, get_monitor,
    get_recent_events, get_settings, list_monitors, save_settings,
    update_monitor,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

_lock = threading.Lock()
_monitor_running = False
_monitor_thread: threading.Thread | None = None
_stop_event = threading.Event()
_global_log: deque[str] = deque(maxlen=200)


def _add_log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    _global_log.appendleft(f"[{ts}] {msg}")


# ═══════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════

def _admin_password() -> str | None:
    return os.environ.get("ADMIN_PASSWORD", "").strip() or None


def require_auth(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        pw = _admin_password()
        if pw and not session.get("authed"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify(ok=False, error="Not authenticated"), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login_page():
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == _admin_password():
            session["authed"] = True
            return redirect(url_for("admin_dashboard"))
        error = "Wrong password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ═══════════════════════════════════════════════════════════════════════
# PAGES
# ═══════════════════════════════════════════════════════════════════════

@app.route("/")
@require_auth
def admin_dashboard():
    filt_type = request.args.get("type", "")
    filt_status = request.args.get("status", "")
    filt_category = request.args.get("category", "")
    monitors = list_monitors(
        monitor_type=filt_type or None,
        status=filt_status or None,
        category=filt_category or None,
    )
    all_monitors = list_monitors()
    categories = sorted({m.get("category", "") for m in all_monitors if m.get("category")})
    return render_template(
        "admin.html",
        monitors=monitors,
        categories=categories,
        monitoring=_monitor_running,
        filt_type=filt_type,
        filt_status=filt_status,
        filt_category=filt_category,
        logs=list(_global_log),
    )


@app.route("/monitor/<monitor_id>")
@require_auth
def monitor_detail_page(monitor_id):
    mon = get_monitor(monitor_id)
    if not mon:
        return "Not found", 404
    events = get_events(monitor_id, limit=200)
    price_history = [
        {"t": e["created_at"], "v": e["details"].get("price") or e["details"].get("fare")}
        for e in reversed(events)
        if e["event_type"] == "check" and (e["details"].get("price") is not None or e["details"].get("fare") is not None)
    ]
    return render_template(
        "monitor_detail.html",
        mon=mon,
        events=events,
        price_history=price_history,
        monitoring=_monitor_running,
    )


# ═══════════════════════════════════════════════════════════════════════
# MONITOR CRUD API
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/monitors", methods=["GET"])
@require_auth
def api_list_monitors():
    status = request.args.get("status")
    mtype = request.args.get("type")
    cat = request.args.get("category")
    return jsonify(monitors=list_monitors(status=status, monitor_type=mtype, category=cat))


@app.route("/api/monitors", methods=["POST"])
@require_auth
def api_create_monitor():
    data = request.get_json(force=True)
    mtype = data.get("type", "product")

    mon: dict = {
        "type": mtype,
        "name": (data.get("name") or "").strip(),
        "category": (data.get("category") or "").strip(),
        "created_by": (data.get("created_by") or "admin").strip(),
        "check_interval_min": int(data.get("check_interval_min", 60)),
        "status": "active",
    }

    if mtype == "product":
        url = (data.get("url") or "").strip()
        budget_raw = str(data.get("budget", "")).strip()
        if not mon["name"] or not url:
            return jsonify(ok=False, error="Name and URL are required"), 400
        try:
            budget = float(budget_raw.replace(",", "."))
        except (ValueError, AttributeError):
            return jsonify(ok=False, error="Budget must be a number"), 400
        mon.update({
            "url": url,
            "budget": budget,
            "price_selector": (data.get("price_selector") or "").strip() or None,
            "monitor_until": (data.get("monitor_until") or "").strip() or None,
        })
    elif mtype == "train":
        train_num = (data.get("train_number") or "").strip()
        from_st = (data.get("from_station") or "").strip().upper()
        to_st = (data.get("to_station") or "").strip().upper()
        travel_date = (data.get("travel_date") or "").strip()
        if not train_num or not from_st or not to_st or not travel_date:
            return jsonify(ok=False, error="Train number, stations, and date are required"), 400
        fare_budget = None
        fb_raw = str(data.get("fare_budget", "") or "").strip()
        if fb_raw:
            try:
                fare_budget = float(fb_raw.replace(",", "."))
            except ValueError:
                return jsonify(ok=False, error="Fare budget must be a number"), 400
        mon.update({
            "train_number": train_num,
            "train_name": (data.get("train_name") or "").strip(),
            "from_station": from_st,
            "to_station": to_st,
            "travel_date": travel_date,
            "class_code": (data.get("class_code") or "SL").strip().upper(),
            "quota": (data.get("quota") or "GN").strip().upper(),
            "fare_budget": fare_budget,
        })
        if not mon["name"]:
            mon["name"] = f"{train_num} {from_st}-{to_st}"
    else:
        return jsonify(ok=False, error="type must be 'product' or 'train'"), 400

    created = create_monitor(mon)
    add_event(created["id"], "status_change", {"status": "active", "action": "created"})
    _add_log(f"Monitor created: {created.get('name', created['id'])}")
    return jsonify(ok=True, monitor=created)


@app.route("/api/monitors/<monitor_id>", methods=["GET"])
@require_auth
def api_get_monitor(monitor_id):
    mon = get_monitor(monitor_id)
    if not mon:
        return jsonify(ok=False, error="Not found"), 404
    return jsonify(monitor=mon)


@app.route("/api/monitors/<monitor_id>", methods=["PUT"])
@require_auth
def api_update_monitor(monitor_id):
    data = request.get_json(force=True)
    safe_keys = {
        "name", "category", "created_by", "check_interval_min", "url", "budget",
        "price_selector", "monitor_until", "train_number", "train_name",
        "from_station", "to_station", "travel_date", "class_code", "quota",
        "fare_budget",
    }
    updates = {k: v for k, v in data.items() if k in safe_keys}
    updated = update_monitor(monitor_id, updates)
    if not updated:
        return jsonify(ok=False, error="Not found"), 404
    add_event(monitor_id, "status_change", {"action": "edited", "fields": list(updates.keys())})
    _add_log(f"Monitor updated: {updated.get('name', monitor_id)}")
    return jsonify(ok=True, monitor=updated)


@app.route("/api/monitors/<monitor_id>", methods=["DELETE"])
@require_auth
def api_delete_monitor(monitor_id):
    mon = get_monitor(monitor_id)
    name = mon.get("name", monitor_id) if mon else monitor_id
    if delete_monitor(monitor_id):
        _add_log(f"Monitor deleted: {name}")
        return jsonify(ok=True)
    return jsonify(ok=False, error="Not found"), 404


@app.route("/api/monitors/<monitor_id>/pause", methods=["POST"])
@require_auth
def api_pause_monitor(monitor_id):
    updated = update_monitor(monitor_id, {"status": "paused"})
    if not updated:
        return jsonify(ok=False, error="Not found"), 404
    add_event(monitor_id, "status_change", {"status": "paused"})
    _add_log(f"Monitor paused: {updated.get('name', monitor_id)}")
    return jsonify(ok=True, monitor=updated)


@app.route("/api/monitors/<monitor_id>/resume", methods=["POST"])
@require_auth
def api_resume_monitor(monitor_id):
    updated = update_monitor(monitor_id, {
        "status": "active", "notified_budget": False, "notified_available": False,
    })
    if not updated:
        return jsonify(ok=False, error="Not found"), 404
    add_event(monitor_id, "status_change", {"status": "active", "action": "resumed"})
    _add_log(f"Monitor resumed: {updated.get('name', monitor_id)}")
    return jsonify(ok=True, monitor=updated)


@app.route("/api/monitors/<monitor_id>/duplicate", methods=["POST"])
@require_auth
def api_duplicate_monitor(monitor_id):
    original = get_monitor(monitor_id)
    if not original:
        return jsonify(ok=False, error="Not found"), 404
    clone = dict(original)
    for k in ("id", "created_at", "last_checked_at", "next_check_at",
              "last_price", "last_fare", "last_availability",
              "notified_budget", "notified_available"):
        clone.pop(k, None)
    clone["name"] = f"{clone.get('name', '')} (copy)"
    clone["status"] = "active"
    created = create_monitor(clone)
    add_event(created["id"], "status_change", {"action": "duplicated_from", "source_id": monitor_id})
    _add_log(f"Monitor duplicated: {created.get('name', created['id'])}")
    return jsonify(ok=True, monitor=created)


# ═══════════════════════════════════════════════════════════════════════
# CHECK (per-monitor)
# ═══════════════════════════════════════════════════════════════════════

def _do_check_product(mon: dict) -> dict:
    from price_monitor.monitor import fetch_html
    from price_monitor.parser_price import extract_price

    html = fetch_html(mon["url"])
    price = extract_price(html, mon.get("price_selector"), mon["url"])
    updates: dict = {"last_price": price, "last_checked_at": datetime.now(timezone.utc).isoformat()}
    details: dict = {"price": price}

    if price is not None and mon.get("budget") is not None:
        if price <= mon["budget"] and not mon.get("notified_budget"):
            _send_alert(mon, f"Price alert: {mon['name']}", f"Now {price:g} (budget {mon['budget']:g}).\n{mon['url']}")
            updates["notified_budget"] = True
            details["alerted"] = True
        elif price > mon["budget"]:
            updates["notified_budget"] = False

    return updates, details


def _do_check_train(mon: dict) -> dict:
    from price_monitor.train_checker import (
        _api_key, _availability_is_available, _date_to_api, _fare_for_class,
        fetch_availability, fetch_fare,
    )

    key = _api_key()
    if not key:
        raise RuntimeError("INDIAN_RAIL_API_KEY not set")

    updates: dict = {"last_checked_at": datetime.now(timezone.utc).isoformat()}
    details: dict = {}

    try:
        fare_data = fetch_fare(key, mon["train_number"], mon["from_station"], mon["to_station"], mon.get("quota", "GN"))
        if fare_data.get("Fares"):
            price = _fare_for_class(fare_data["Fares"], mon.get("class_code", "SL"))
            if price is not None:
                updates["last_fare"] = price
                details["fare"] = price
            if fare_data.get("TrainName") and not mon.get("train_name"):
                updates["train_name"] = fare_data["TrainName"]
    except Exception:
        pass

    try:
        api_date = _date_to_api(mon["travel_date"])
        avail_data = fetch_availability(key, mon["train_number"], mon["from_station"], mon["to_station"], api_date, mon.get("class_code", "SL"))
        if avail_data.get("Availability"):
            avail_str = avail_data["Availability"][0].get("Availability", "Unknown")
            updates["last_availability"] = avail_str
            details["availability"] = avail_str
    except Exception:
        pass

    label = mon.get("train_name") or mon.get("train_number", "")

    if (mon.get("fare_budget") and updates.get("last_fare")
            and updates["last_fare"] <= mon["fare_budget"] and not mon.get("notified_budget")):
        _send_alert(mon, f"Train fare alert: {label}",
                     f"{mon.get('class_code','SL')} fare is {updates['last_fare']:g} (budget {mon['fare_budget']:g}).\n"
                     f"{mon['from_station']} -> {mon['to_station']} on {mon['travel_date']}")
        updates["notified_budget"] = True
        details["fare_alerted"] = True
    elif updates.get("last_fare") and mon.get("fare_budget") and updates["last_fare"] > mon["fare_budget"]:
        updates["notified_budget"] = False

    avail_str = updates.get("last_availability", "")
    if avail_str and _availability_is_available(avail_str) and not mon.get("notified_available"):
        _send_alert(mon, f"Seats available: {label}",
                     f"{mon.get('class_code','SL')} status: {avail_str}\n"
                     f"{mon['from_station']} -> {mon['to_station']} on {mon['travel_date']}")
        updates["notified_available"] = True
        details["avail_alerted"] = True
    elif avail_str and not _availability_is_available(avail_str):
        updates["notified_available"] = False

    return updates, details


def _send_alert(mon: dict, title: str, message: str) -> None:
    from price_monitor.notify_settings import load_notify_settings
    from price_monitor.notifier import notify_price_alert
    from price_monitor.db import _json_path

    settings = load_notify_settings(_json_path("items.json"))
    notify_price_alert(settings, title=title, message=message)


def check_single_monitor(monitor_id: str) -> dict:
    mon = get_monitor(monitor_id)
    if not mon:
        raise ValueError("Monitor not found")
    if mon["type"] == "product":
        updates, details = _do_check_product(mon)
    elif mon["type"] == "train":
        updates, details = _do_check_train(mon)
    else:
        raise ValueError(f"Unknown monitor type: {mon['type']}")

    update_monitor(monitor_id, updates)
    add_event(monitor_id, "check", details)
    return {**mon, **updates}


@app.route("/api/monitors/<monitor_id>/check", methods=["POST"])
@require_auth
def api_check_monitor(monitor_id):
    try:
        result = check_single_monitor(monitor_id)
        _add_log(f"Manual check: {result.get('name', monitor_id)}")
        return jsonify(ok=True, monitor=result)
    except Exception as e:
        add_event(monitor_id, "error", {"error": str(e)})
        _add_log(f"Check failed: {monitor_id} - {e}")
        return jsonify(ok=False, error=str(e)), 500


# ═══════════════════════════════════════════════════════════════════════
# EVENTS API
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/monitors/<monitor_id>/events")
@require_auth
def api_get_events(monitor_id):
    limit = int(request.args.get("limit", 100))
    etype = request.args.get("type")
    events = get_events(monitor_id, limit=limit, event_type=etype)
    return jsonify(events=events)


@app.route("/api/events/recent")
@require_auth
def api_recent_events():
    return jsonify(events=get_recent_events(50))


# ═══════════════════════════════════════════════════════════════════════
# EXPORT CSV
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/monitors/<monitor_id>/export")
@require_auth
def api_export_csv(monitor_id):
    mon = get_monitor(monitor_id)
    if not mon:
        return "Not found", 404
    events = get_events(monitor_id, limit=10000, event_type="check")
    buf = io.StringIO()
    writer = csv.writer(buf)
    if mon["type"] == "product":
        writer.writerow(["timestamp", "price"])
        for e in reversed(events):
            writer.writerow([e["created_at"], e["details"].get("price", "")])
    else:
        writer.writerow(["timestamp", "fare", "availability"])
        for e in reversed(events):
            writer.writerow([e["created_at"], e["details"].get("fare", ""), e["details"].get("availability", "")])
    from flask import Response
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={monitor_id}.csv"},
    )


# ═══════════════════════════════════════════════════════════════════════
# NOTIFICATION SETTINGS
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/notifications", methods=["GET"])
@require_auth
def api_get_notifications():
    return jsonify(settings=get_settings())


@app.route("/api/notifications", methods=["POST"])
@require_auth
def api_save_notifications():
    data = request.get_json(force=True)
    save_settings(data)

    from price_monitor.notify_settings import write_notify_config_file
    from price_monitor.db import _json_path
    write_notify_config_file(_json_path("items.json"), data)

    _add_log("Notification settings saved")
    return jsonify(ok=True)


@app.route("/api/notifications/test", methods=["POST"])
@require_auth
def api_test_notification():
    data = request.get_json(force=True)
    from price_monitor.notify_settings import notify_settings_from_stored_dict
    from price_monitor.notifier import notify_price_alert
    s = notify_settings_from_stored_dict(data)
    if not s.desktop and not s.any_remote():
        return jsonify(ok=False, error="Configure at least one channel first"), 400
    ok = notify_price_alert(s, title="Price Monitor Test", message="If you received this, notifications work.")
    return jsonify(ok=ok)


# ═══════════════════════════════════════════════════════════════════════
# BACKGROUND MONITOR LOOP
# ═══════════════════════════════════════════════════════════════════════

def _monitor_loop() -> None:
    global _monitor_running
    while _monitor_running:
        active = list_monitors(status="active")
        if not active:
            _add_log("No active monitors")
            if _stop_event.wait(timeout=60):
                break
            continue

        for mon in active:
            if not _monitor_running:
                break
            try:
                result = check_single_monitor(mon["id"])
                name = result.get("name", mon["id"])
                if mon["type"] == "product":
                    p = result.get("last_price")
                    _add_log(f"Check {name}: {p:g}" if p is not None else f"Check {name}: no price")
                else:
                    parts = []
                    if result.get("last_fare") is not None:
                        parts.append(f"fare {result['last_fare']:g}")
                    if result.get("last_availability"):
                        parts.append(result["last_availability"])
                    _add_log(f"Check {name}: {', '.join(parts) if parts else 'no data'}")
            except Exception as e:
                _add_log(f"Check error {mon.get('name', mon['id'])}: {e}")

        min_interval = min((m.get("check_interval_min", 60) for m in active), default=60)
        if _stop_event.wait(timeout=max(60, min_interval * 60)):
            break
    _monitor_running = False


@app.route("/api/monitor/start", methods=["POST"])
@require_auth
def start_monitor_loop():
    global _monitor_running, _monitor_thread
    with _lock:
        if _monitor_running:
            return jsonify(ok=True, monitoring=True)
        active = list_monitors(status="active")
        if not active:
            return jsonify(ok=False, error="No active monitors"), 400
        _stop_event.clear()
        _monitor_running = True
        _monitor_thread = threading.Thread(target=_monitor_loop, daemon=True)
        _monitor_thread.start()
    _add_log(f"Monitoring started ({len(active)} active monitors)")
    return jsonify(ok=True, monitoring=True)


@app.route("/api/monitor/stop", methods=["POST"])
@require_auth
def stop_monitor_loop():
    global _monitor_running
    with _lock:
        _monitor_running = False
        _stop_event.set()
    _add_log("Monitoring stopped")
    return jsonify(ok=True, monitoring=False)


# ═══════════════════════════════════════════════════════════════════════
# STATUS
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/status")
@require_auth
def api_status():
    monitors = list_monitors()
    return jsonify(
        monitoring=_monitor_running,
        total=len(monitors),
        active=sum(1 for m in monitors if m.get("status") == "active"),
        paused=sum(1 for m in monitors if m.get("status") == "paused"),
        completed=sum(1 for m in monitors if m.get("status") == "completed"),
        logs=list(_global_log),
    )


@app.route("/health")
def health():
    return jsonify(status="ok", monitoring=_monitor_running)


# ═══════════════════════════════════════════════════════════════════════
# AUTO-RESUME ON STARTUP
# ═══════════════════════════════════════════════════════════════════════

def _bootstrap() -> None:
    """Auto-start monitoring if there are active monitors in the database."""
    global _monitor_running, _monitor_thread

    active = list_monitors(status="active")
    if not active:
        _add_log("No active monitors to auto-resume")
        return

    auto = os.environ.get("PRICE_MONITOR_AUTO_START", "1").strip().lower()
    if auto not in ("1", "true", "yes"):
        _add_log(f"Auto-start disabled; {len(active)} active monitors waiting")
        return

    with _lock:
        if _monitor_running:
            return
        _stop_event.clear()
        _monitor_running = True
        _monitor_thread = threading.Thread(target=_monitor_loop, daemon=True)
        _monitor_thread.start()

    _add_log(f"Auto-resumed monitoring ({len(active)} active monitors)")


def _keep_alive_loop() -> None:
    import time
    time.sleep(10)
    ext = (
        os.environ.get("RENDER_EXTERNAL_URL")
        or os.environ.get("PRICE_MONITOR_EXTERNAL_URL", "").strip()
    )
    if not ext:
        return
    url = f"{ext.rstrip('/')}/health"
    _add_log(f"Keep-alive: pinging {url} every 13 min")
    while True:
        time.sleep(13 * 60)
        try:
            import httpx
            httpx.get(url, timeout=30)
        except Exception:
            pass


def _deferred_bootstrap() -> None:
    import time
    time.sleep(2)
    try:
        _bootstrap()
    except Exception as exc:
        _add_log(f"Bootstrap error: {exc}")

threading.Thread(target=_deferred_bootstrap, daemon=True).start()
threading.Thread(target=_keep_alive_loop, daemon=True).start()


def run_web(host: str = "0.0.0.0", port: int = 8080) -> None:
    _add_log("Web dashboard started")
    app.run(host=host, port=port, debug=False)
