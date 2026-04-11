from __future__ import annotations

import csv
import io
import json
import os
import queue
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, Response, jsonify, redirect, render_template, request, session, url_for,
)

from price_monitor.db import (
    add_audit, add_event, create_monitor, delete_monitor, get_audit_log,
    get_events, get_monitor, get_recent_events, get_settings, list_monitors,
    save_settings, update_monitor,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

_lock = threading.Lock()
_monitor_running = False
_monitor_thread: threading.Thread | None = None
_stop_event = threading.Event()
_global_log: deque[str] = deque(maxlen=200)

_sse_listeners: list[queue.Queue] = []
_sse_lock = threading.Lock()


def _add_log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    entry = f"[{ts}] {msg}"
    _global_log.appendleft(entry)
    _sse_broadcast({"type": "log", "message": entry})


def _sse_broadcast(data: dict) -> None:
    with _sse_lock:
        dead = []
        for q in _sse_listeners:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_listeners.remove(q)


# ═══════════════════════════════════════════════════════════════════════
# AUTH (admin / viewer roles)
# ═══════════════════════════════════════════════════════════════════════

def _admin_password() -> str | None:
    return os.environ.get("ADMIN_PASSWORD", "").strip() or None


def _viewer_password() -> str | None:
    return os.environ.get("VIEWER_PASSWORD", "").strip() or None


def _session_role() -> str | None:
    return session.get("role")


def require_auth(f):
    """Allow any authenticated role."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if _admin_password() and not session.get("authed"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify(ok=False, error="Not authenticated"), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return wrapped


def require_admin(f):
    """Only allow admin role."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if _admin_password() and not session.get("authed"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify(ok=False, error="Not authenticated"), 401
            return redirect(url_for("login_page"))
        if session.get("role") == "viewer":
            if request.is_json or request.path.startswith("/api/"):
                return jsonify(ok=False, error="Admin access required"), 403
            return "Forbidden", 403
        return f(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login_page():
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw and pw == _admin_password():
            session["authed"] = True
            session["role"] = "admin"
            return redirect(url_for("admin_dashboard"))
        elif pw and _viewer_password() and pw == _viewer_password():
            session["authed"] = True
            session["role"] = "viewer"
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
    filt_tag = request.args.get("tag", "")
    filt_creator = request.args.get("creator", "")
    monitors = list_monitors(
        monitor_type=filt_type or None,
        status=filt_status or None,
        category=filt_category or None,
        tag=filt_tag or None,
        created_by=filt_creator or None,
    )
    all_monitors = list_monitors()
    categories = sorted({m.get("category", "") for m in all_monitors if m.get("category")})
    creators = sorted({m.get("created_by", "") for m in all_monitors if m.get("created_by")})
    all_tags: set[str] = set()
    for m in all_monitors:
        for t in (m.get("tags") or []):
            if t:
                all_tags.add(t)
    recent_audit = get_audit_log(limit=30)
    return render_template(
        "admin.html",
        monitors=monitors,
        categories=categories,
        creators=creators,
        all_tags=sorted(all_tags),
        monitoring=_monitor_running,
        filt_type=filt_type,
        filt_status=filt_status,
        filt_category=filt_category,
        filt_tag=filt_tag,
        filt_creator=filt_creator,
        logs=list(_global_log),
        role=session.get("role", "admin"),
        audit_entries=recent_audit,
    )


@app.route("/monitor/<monitor_id>")
@require_auth
def monitor_detail_page(monitor_id):
    mon = get_monitor(monitor_id)
    if not mon:
        return "Not found", 404
    events = get_events(monitor_id, limit=200)
    price_key = "price" if mon.get("type") == "product" else ("fare" if mon.get("type") == "train" else "flight_price")
    price_history = [
        {"t": e["created_at"], "v": e["details"].get("price") or e["details"].get("fare") or e["details"].get("flight_price")}
        for e in reversed(events)
        if e["event_type"] == "check" and (
            e["details"].get("price") is not None
            or e["details"].get("fare") is not None
            or e["details"].get("flight_price") is not None
        )
    ]
    comparison = []
    cg = mon.get("comparison_group", "")
    if cg:
        comparison = [m for m in list_monitors(comparison_group=cg) if m["id"] != monitor_id]
    return render_template(
        "monitor_detail.html",
        mon=mon,
        events=events,
        price_history=price_history,
        monitoring=_monitor_running,
        comparison=comparison,
        role=session.get("role", "admin"),
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
    tag = request.args.get("tag")
    creator = request.args.get("created_by")
    return jsonify(monitors=list_monitors(status=status, monitor_type=mtype, category=cat, tag=tag, created_by=creator))


@app.route("/api/monitors", methods=["POST"])
@require_admin
def api_create_monitor():
    data = request.get_json(force=True)
    mtype = data.get("type", "product")

    mon: dict = {
        "type": mtype,
        "name": (data.get("name") or "").strip(),
        "category": (data.get("category") or "").strip(),
        "tags": _parse_tags(data.get("tags")),
        "created_by": (data.get("created_by") or "admin").strip(),
        "check_interval_min": int(data.get("check_interval_min", 60)),
        "status": "active",
        "alert_mode": data.get("alert_mode", "budget"),
        "alert_drop_percent": _safe_float(data.get("alert_drop_percent")),
        "comparison_group": (data.get("comparison_group") or "").strip(),
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
        fare_budget = _safe_float(data.get("fare_budget"))
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
    elif mtype == "flight":
        origin = (data.get("flight_origin") or "").strip().upper()
        dest = (data.get("flight_destination") or "").strip().upper()
        fdate = (data.get("flight_date") or "").strip()
        if not origin or not dest or not fdate:
            return jsonify(ok=False, error="Origin, destination, and date are required"), 400
        mon.update({
            "flight_origin": origin,
            "flight_destination": dest,
            "flight_date": fdate,
            "flight_return_date": (data.get("flight_return_date") or "").strip() or None,
            "flight_max_price": _safe_float(data.get("flight_max_price")),
            "flight_airline_pref": (data.get("flight_airline_pref") or "").strip() or None,
        })
        if not mon["name"]:
            mon["name"] = f"Flight {origin}-{dest}"
    else:
        return jsonify(ok=False, error="type must be 'product', 'train', or 'flight'"), 400

    created = create_monitor(mon)
    add_event(created["id"], "status_change", {"status": "active", "action": "created"})
    add_audit("create", "monitor", created["id"], created.get("name", ""), session.get("role", "admin"))
    _add_log(f"Monitor created: {created.get('name', created['id'])}")
    return jsonify(ok=True, monitor=created), 201


def _parse_tags(raw) -> list[str]:
    if isinstance(raw, list):
        return [t.strip() for t in raw if isinstance(t, str) and t.strip()]
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    return []


def _safe_float(val) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        return None


@app.route("/api/monitors/<monitor_id>", methods=["GET"])
@require_auth
def api_get_monitor(monitor_id):
    mon = get_monitor(monitor_id)
    if not mon:
        return jsonify(ok=False, error="Not found"), 404
    return jsonify(monitor=mon)


@app.route("/api/monitors/<monitor_id>", methods=["PUT"])
@require_admin
def api_update_monitor(monitor_id):
    data = request.get_json(force=True)
    safe_keys = {
        "name", "category", "tags", "created_by", "check_interval_min", "url", "budget",
        "price_selector", "monitor_until", "train_number", "train_name",
        "from_station", "to_station", "travel_date", "class_code", "quota",
        "fare_budget", "alert_mode", "alert_drop_percent", "comparison_group",
        "flight_origin", "flight_destination", "flight_date", "flight_return_date",
        "flight_max_price", "flight_airline_pref",
    }
    updates = {k: v for k, v in data.items() if k in safe_keys}
    if "tags" in updates:
        updates["tags"] = _parse_tags(updates["tags"])
    updated = update_monitor(monitor_id, updates)
    if not updated:
        return jsonify(ok=False, error="Not found"), 404
    add_event(monitor_id, "status_change", {"action": "edited", "fields": list(updates.keys())})
    add_audit("update", "monitor", monitor_id, updated.get("name", ""), session.get("role", "admin"),
              {"fields": list(updates.keys())})
    _add_log(f"Monitor updated: {updated.get('name', monitor_id)}")
    return jsonify(ok=True, monitor=updated)


@app.route("/api/monitors/<monitor_id>", methods=["DELETE"])
@require_admin
def api_delete_monitor(monitor_id):
    mon = get_monitor(monitor_id)
    name = mon.get("name", monitor_id) if mon else monitor_id
    if delete_monitor(monitor_id):
        add_audit("delete", "monitor", monitor_id, name, session.get("role", "admin"))
        _add_log(f"Monitor deleted: {name}")
        return jsonify(ok=True)
    return jsonify(ok=False, error="Not found"), 404


@app.route("/api/monitors/<monitor_id>/pause", methods=["POST"])
@require_admin
def api_pause_monitor(monitor_id):
    updated = update_monitor(monitor_id, {"status": "paused"})
    if not updated:
        return jsonify(ok=False, error="Not found"), 404
    add_event(monitor_id, "status_change", {"status": "paused"})
    add_audit("pause", "monitor", monitor_id, updated.get("name", ""), session.get("role", "admin"))
    _add_log(f"Monitor paused: {updated.get('name', monitor_id)}")
    return jsonify(ok=True, monitor=updated)


@app.route("/api/monitors/<monitor_id>/resume", methods=["POST"])
@require_admin
def api_resume_monitor(monitor_id):
    updated = update_monitor(monitor_id, {
        "status": "active", "notified_budget": False, "notified_available": False,
    })
    if not updated:
        return jsonify(ok=False, error="Not found"), 404
    add_event(monitor_id, "status_change", {"status": "active", "action": "resumed"})
    add_audit("resume", "monitor", monitor_id, updated.get("name", ""), session.get("role", "admin"))
    _add_log(f"Monitor resumed: {updated.get('name', monitor_id)}")
    return jsonify(ok=True, monitor=updated)


@app.route("/api/monitors/<monitor_id>/duplicate", methods=["POST"])
@require_admin
def api_duplicate_monitor(monitor_id):
    original = get_monitor(monitor_id)
    if not original:
        return jsonify(ok=False, error="Not found"), 404
    clone = dict(original)
    for k in ("id", "created_at", "last_checked_at", "next_check_at",
              "last_price", "last_fare", "last_availability", "last_flight_price",
              "notified_budget", "notified_available", "highest_price", "highest_fare",
              "consecutive_errors", "last_error_at", "last_error_msg"):
        clone.pop(k, None)
    clone["name"] = f"{clone.get('name', '')} (copy)"
    clone["status"] = "active"
    created = create_monitor(clone)
    add_event(created["id"], "status_change", {"action": "duplicated_from", "source_id": monitor_id})
    add_audit("duplicate", "monitor", created["id"], created.get("name", ""), session.get("role", "admin"))
    _add_log(f"Monitor duplicated: {created.get('name', created['id'])}")
    return jsonify(ok=True, monitor=created)


# ═══════════════════════════════════════════════════════════════════════
# BULK OPERATIONS
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/monitors/batch/pause", methods=["POST"])
@require_admin
def api_batch_pause():
    ids = (request.get_json(force=True) or {}).get("ids", [])
    done = 0
    for mid in ids:
        if update_monitor(mid, {"status": "paused"}):
            add_event(mid, "status_change", {"status": "paused"})
            done += 1
    add_audit("batch_pause", "monitor", "", f"{done} monitors", session.get("role", "admin"))
    _add_log(f"Batch paused {done} monitors")
    return jsonify(ok=True, count=done)


@app.route("/api/monitors/batch/resume", methods=["POST"])
@require_admin
def api_batch_resume():
    ids = (request.get_json(force=True) or {}).get("ids", [])
    done = 0
    for mid in ids:
        if update_monitor(mid, {"status": "active", "notified_budget": False, "notified_available": False}):
            add_event(mid, "status_change", {"status": "active", "action": "resumed"})
            done += 1
    add_audit("batch_resume", "monitor", "", f"{done} monitors", session.get("role", "admin"))
    _add_log(f"Batch resumed {done} monitors")
    return jsonify(ok=True, count=done)


@app.route("/api/monitors/batch/delete", methods=["POST"])
@require_admin
def api_batch_delete():
    ids = (request.get_json(force=True) or {}).get("ids", [])
    done = 0
    for mid in ids:
        if delete_monitor(mid):
            done += 1
    add_audit("batch_delete", "monitor", "", f"{done} monitors", session.get("role", "admin"))
    _add_log(f"Batch deleted {done} monitors")
    return jsonify(ok=True, count=done)


# ═══════════════════════════════════════════════════════════════════════
# IMPORT / EXPORT
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/monitors/export-all")
@require_auth
def api_export_all():
    monitors = list_monitors()
    export = []
    skip_keys = {"id", "created_at", "last_checked_at", "next_check_at",
                 "last_price", "last_fare", "last_availability", "last_flight_price",
                 "notified_budget", "notified_available", "highest_price", "highest_fare",
                 "consecutive_errors", "last_error_at", "last_error_msg"}
    for m in monitors:
        export.append({k: v for k, v in m.items() if k not in skip_keys})
    return Response(
        json.dumps(export, indent=2, default=str),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=monitors_export.json"},
    )


@app.route("/api/monitors/import", methods=["POST"])
@require_admin
def api_import_monitors():
    data = request.get_json(force=True)
    if not isinstance(data, list):
        return jsonify(ok=False, error="Expected a JSON array"), 400
    created = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        item.pop("id", None)
        item.pop("created_at", None)
        item.setdefault("status", "active")
        create_monitor(item)
        created += 1
    add_audit("import", "monitor", "", f"{created} monitors", session.get("role", "admin"))
    _add_log(f"Imported {created} monitors")
    return jsonify(ok=True, count=created)


# ═══════════════════════════════════════════════════════════════════════
# CHECK (per-monitor) with alert modes, health tracking
# ═══════════════════════════════════════════════════════════════════════

def _should_alert(mon: dict, current_value: float | None, value_key: str, budget_key: str) -> bool:
    """Determine if an alert should fire based on alert_mode."""
    if current_value is None:
        return False

    mode = mon.get("alert_mode", "budget")

    if mode == "budget":
        budget = mon.get(budget_key)
        if budget is not None and current_value <= budget and not mon.get("notified_budget"):
            return True

    elif mode == "drop_percent":
        peak_key = "highest_price" if value_key == "price" else ("highest_fare" if value_key == "fare" else "highest_price")
        peak = mon.get(peak_key)
        drop_pct = mon.get("alert_drop_percent") or 10
        if peak and peak > 0:
            actual_drop = ((peak - current_value) / peak) * 100
            if actual_drop >= drop_pct and not mon.get("notified_budget"):
                return True

    elif mode == "any_change":
        last_key = f"last_{value_key}"
        last_val = mon.get(last_key)
        if last_val is not None and current_value != last_val:
            return True

    return False


def _update_peak(mon: dict, current_value: float | None, peak_key: str) -> dict:
    """Track highest seen price for drop-percent calculations."""
    updates = {}
    if current_value is not None:
        old_peak = mon.get(peak_key)
        if old_peak is None or current_value > old_peak:
            updates[peak_key] = current_value
    return updates


def _do_check_product(mon: dict) -> tuple[dict, dict]:
    from price_monitor.monitor import fetch_html
    from price_monitor.parser_price import extract_price

    html = fetch_html(mon["url"])
    price = extract_price(html, mon.get("price_selector"), mon["url"])
    updates: dict = {"last_price": price, "last_checked_at": datetime.now(timezone.utc).isoformat()}
    details: dict = {"price": price}

    updates.update(_update_peak(mon, price, "highest_price"))

    if _should_alert(mon, price, "price", "budget"):
        mode = mon.get("alert_mode", "budget")
        if mode == "budget":
            _send_alert(mon, f"Price alert: {mon['name']}", f"Now {price:g} (budget {mon['budget']:g}).\n{mon['url']}")
        elif mode == "drop_percent":
            peak = mon.get("highest_price") or price
            drop = ((peak - price) / peak) * 100 if peak else 0
            _send_alert(mon, f"Price drop: {mon['name']}", f"Dropped {drop:.1f}% to {price:g} (peak was {peak:g}).\n{mon['url']}")
        elif mode == "any_change":
            old = mon.get("last_price")
            direction = "dropped" if old and price < old else "changed"
            _send_alert(mon, f"Price {direction}: {mon['name']}", f"Now {price:g} (was {old:g if old else '?'}).\n{mon['url']}")
        updates["notified_budget"] = True
        details["alerted"] = True
    elif price is not None and mon.get("budget") is not None and price > mon.get("budget", 0):
        updates["notified_budget"] = False

    return updates, details


def _do_check_train(mon: dict) -> tuple[dict, dict]:
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

    updates.update(_update_peak(mon, updates.get("last_fare"), "highest_fare"))

    label = mon.get("train_name") or mon.get("train_number", "")
    fare = updates.get("last_fare")

    if _should_alert(mon, fare, "fare", "fare_budget"):
        mode = mon.get("alert_mode", "budget")
        if mode == "budget":
            _send_alert(mon, f"Train fare alert: {label}",
                        f"{mon.get('class_code','SL')} fare is {fare:g} (budget {mon['fare_budget']:g}).\n"
                        f"{mon['from_station']} -> {mon['to_station']} on {mon['travel_date']}")
        elif mode == "drop_percent":
            peak = mon.get("highest_fare") or fare
            drop = ((peak - fare) / peak) * 100 if peak else 0
            _send_alert(mon, f"Train fare drop: {label}", f"Dropped {drop:.1f}% to {fare:g}.")
        elif mode == "any_change":
            _send_alert(mon, f"Train fare changed: {label}", f"Now {fare:g}.")
        updates["notified_budget"] = True
        details["fare_alerted"] = True
    elif fare and mon.get("fare_budget") and fare > mon["fare_budget"]:
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


def _do_check_flight(mon: dict) -> tuple[dict, dict]:
    from price_monitor.flight_checker import search_flights

    result = search_flights(
        origin=mon["flight_origin"],
        destination=mon["flight_destination"],
        date=mon["flight_date"],
        return_date=mon.get("flight_return_date"),
        max_price=mon.get("flight_max_price"),
        airline_pref=mon.get("flight_airline_pref"),
    )

    price = result.get("lowest_price")
    updates: dict = {
        "last_flight_price": price,
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
    }
    details: dict = {
        "flight_price": price,
        "airline": result.get("airline"),
        "departure": result.get("departure"),
        "offers_count": result.get("offers_count", 0),
    }

    updates.update(_update_peak(mon, price, "highest_price"))

    max_p = mon.get("flight_max_price")
    if _should_alert(mon, price, "flight_price", "flight_max_price"):
        mode = mon.get("alert_mode", "budget")
        if mode == "budget" and max_p:
            _send_alert(mon, f"Flight price alert: {mon['name']}",
                        f"Lowest fare {price:g} (max {max_p:g}).\n"
                        f"{mon['flight_origin']} -> {mon['flight_destination']} on {mon['flight_date']}")
        elif mode == "drop_percent":
            peak = mon.get("highest_price") or price
            drop = ((peak - price) / peak) * 100 if peak else 0
            _send_alert(mon, f"Flight price drop: {mon['name']}", f"Dropped {drop:.1f}% to {price:g}.")
        elif mode == "any_change":
            _send_alert(mon, f"Flight price changed: {mon['name']}", f"Now {price:g}.")
        updates["notified_budget"] = True
        details["alerted"] = True
    elif price and max_p and price > max_p:
        updates["notified_budget"] = False

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

    try:
        if mon["type"] == "product":
            updates, details = _do_check_product(mon)
        elif mon["type"] == "train":
            updates, details = _do_check_train(mon)
        elif mon["type"] == "flight":
            updates, details = _do_check_flight(mon)
        else:
            raise ValueError(f"Unknown monitor type: {mon['type']}")

        updates["consecutive_errors"] = 0
        updates["last_error_msg"] = None
        update_monitor(monitor_id, updates)
        add_event(monitor_id, "check", details)
        _sse_broadcast({"type": "check", "monitor_id": monitor_id})
        return {**mon, **updates}

    except Exception as e:
        err_updates = {
            "consecutive_errors": (mon.get("consecutive_errors") or 0) + 1,
            "last_error_at": datetime.now(timezone.utc).isoformat(),
            "last_error_msg": str(e)[:500],
        }
        update_monitor(monitor_id, err_updates)
        add_event(monitor_id, "error", {"error": str(e)[:500]})
        raise


@app.route("/api/monitors/<monitor_id>/check", methods=["POST"])
@require_admin
def api_check_monitor(monitor_id):
    try:
        result = check_single_monitor(monitor_id)
        _add_log(f"Manual check: {result.get('name', monitor_id)}")
        return jsonify(ok=True, monitor=result)
    except Exception as e:
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
    elif mon["type"] == "train":
        writer.writerow(["timestamp", "fare", "availability"])
        for e in reversed(events):
            writer.writerow([e["created_at"], e["details"].get("fare", ""), e["details"].get("availability", "")])
    else:
        writer.writerow(["timestamp", "flight_price", "airline"])
        for e in reversed(events):
            writer.writerow([e["created_at"], e["details"].get("flight_price", ""), e["details"].get("airline", "")])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={monitor_id}.csv"},
    )


# ═══════════════════════════════════════════════════════════════════════
# COMPARISON
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/comparison/<group_name>")
@require_auth
def api_comparison(group_name):
    monitors = list_monitors(comparison_group=group_name)
    items = []
    for m in monitors:
        val = m.get("last_price") or m.get("last_fare") or m.get("last_flight_price")
        items.append({"id": m["id"], "name": m["name"], "type": m["type"],
                       "value": val, "url": m.get("url", "")})
    items.sort(key=lambda x: x["value"] if x["value"] is not None else 999999)
    return jsonify(group=group_name, items=items)


# ═══════════════════════════════════════════════════════════════════════
# NOTIFICATION SETTINGS
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/notifications", methods=["GET"])
@require_auth
def api_get_notifications():
    return jsonify(settings=get_settings())


@app.route("/api/notifications", methods=["POST"])
@require_admin
def api_save_notifications():
    data = request.get_json(force=True)
    save_settings(data)

    from price_monitor.notify_settings import write_notify_config_file
    from price_monitor.db import _json_path
    write_notify_config_file(_json_path("items.json"), data)

    add_audit("update", "settings", "notify", "notification settings", session.get("role", "admin"))
    _add_log("Notification settings saved")
    return jsonify(ok=True)


@app.route("/api/notifications/test", methods=["POST"])
@require_admin
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
# AUDIT LOG API
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/audit")
@require_auth
def api_audit():
    limit = int(request.args.get("limit", 100))
    action = request.args.get("action")
    target_id = request.args.get("target_id")
    entries = get_audit_log(limit=limit, action=action, target_id=target_id)
    return jsonify(entries=entries)


# ═══════════════════════════════════════════════════════════════════════
# SSE (Server-Sent Events)
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/stream")
@require_auth
def api_sse_stream():
    q: queue.Queue = queue.Queue(maxsize=50)
    with _sse_lock:
        _sse_listeners.append(q)

    def generate():
        try:
            while True:
                try:
                    data = q.get(timeout=30)
                    yield f"data: {json.dumps(data)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                if q in _sse_listeners:
                    _sse_listeners.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ═══════════════════════════════════════════════════════════════════════
# BACKGROUND MONITOR LOOP (with auto-expiry + smart intervals)
# ═══════════════════════════════════════════════════════════════════════

def _is_expired(mon: dict) -> bool:
    """Check if a monitor should be auto-expired."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mtype = mon.get("type", "product")

    if mtype == "train":
        td = mon.get("travel_date", "")
        if td:
            normalized = td.replace("/", "-")
            parts = normalized.split("-")
            if len(parts) == 3:
                if len(parts[0]) == 4:
                    iso = normalized
                else:
                    iso = f"{parts[2]}-{parts[1]}-{parts[0]}"
                if iso < now_str:
                    return True

    elif mtype == "flight":
        fd = mon.get("flight_date", "")
        if fd and fd < now_str:
            return True

    elif mtype == "product":
        mu = mon.get("monitor_until", "")
        if mu and mu < now_str:
            return True

    return False


def _smart_interval(mon: dict) -> int:
    """Adjust check interval based on how close price is to budget."""
    base = mon.get("check_interval_min", 60)
    mode = mon.get("alert_mode", "budget")

    if mode != "budget":
        return base

    mtype = mon.get("type", "product")
    if mtype == "product":
        current = mon.get("last_price")
        budget = mon.get("budget")
    elif mtype == "train":
        current = mon.get("last_fare")
        budget = mon.get("fare_budget")
    elif mtype == "flight":
        current = mon.get("last_flight_price")
        budget = mon.get("flight_max_price")
    else:
        return base

    if current is None or budget is None or budget <= 0:
        return base

    ratio = current / budget
    if ratio <= 1.15:
        adjusted = base // 2
    elif ratio >= 2.0:
        adjusted = base * 2
    else:
        adjusted = base

    return max(5, min(480, adjusted))


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

            if _is_expired(mon):
                update_monitor(mon["id"], {"status": "expired"})
                add_event(mon["id"], "status_change", {"status": "expired", "reason": "auto_expired"})
                _add_log(f"Auto-expired: {mon.get('name', mon['id'])}")
                continue

            try:
                result = check_single_monitor(mon["id"])
                name = result.get("name", mon["id"])
                if mon["type"] == "product":
                    p = result.get("last_price")
                    _add_log(f"Check {name}: {p:g}" if p is not None else f"Check {name}: no price")
                elif mon["type"] == "train":
                    parts = []
                    if result.get("last_fare") is not None:
                        parts.append(f"fare {result['last_fare']:g}")
                    if result.get("last_availability"):
                        parts.append(result["last_availability"])
                    _add_log(f"Check {name}: {', '.join(parts) if parts else 'no data'}")
                elif mon["type"] == "flight":
                    fp = result.get("last_flight_price")
                    _add_log(f"Check {name}: {fp:g}" if fp is not None else f"Check {name}: no price")
            except Exception as e:
                _add_log(f"Check error {mon.get('name', mon['id'])}: {e}")

        intervals = [_smart_interval(m) for m in active if not _is_expired(m)]
        min_interval = min(intervals) if intervals else 60
        if _stop_event.wait(timeout=max(60, min_interval * 60)):
            break
    _monitor_running = False


@app.route("/api/monitor/start", methods=["POST"])
@require_admin
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
    add_audit("start", "monitor_loop", "", "", session.get("role", "admin"))
    _add_log(f"Monitoring started ({len(active)} active monitors)")
    return jsonify(ok=True, monitoring=True)


@app.route("/api/monitor/stop", methods=["POST"])
@require_admin
def stop_monitor_loop():
    global _monitor_running
    with _lock:
        _monitor_running = False
        _stop_event.set()
    add_audit("stop", "monitor_loop", "", "", session.get("role", "admin"))
    _add_log("Monitoring stopped")
    return jsonify(ok=True, monitoring=False)


# ═══════════════════════════════════════════════════════════════════════
# DIGEST
# ═══════════════════════════════════════════════════════════════════════

def _build_digest() -> str:
    all_m = list_monitors()
    active = [m for m in all_m if m.get("status") == "active"]
    paused = [m for m in all_m if m.get("status") == "paused"]
    expired = [m for m in all_m if m.get("status") == "expired"]

    lines = [
        "=== Price Monitor Daily Digest ===",
        f"Total monitors: {len(all_m)}",
        f"Active: {len(active)} | Paused: {len(paused)} | Expired: {len(expired)}",
        "",
    ]

    for m in active:
        name = m.get("name", m["id"][:8])
        mtype = m.get("type", "?")
        if mtype == "product":
            val = m.get("last_price")
            budget = m.get("budget")
            val_s = f"{val:g}" if val is not None else "?"
            bud_s = f" (budget {budget:g})" if budget is not None else ""
            line = f"  [{mtype}] {name}: price={val_s}{bud_s}"
        elif mtype == "train":
            val = m.get("last_fare")
            avail = m.get("last_availability", "")
            val_s = f"{val:g}" if val is not None else "?"
            line = f"  [{mtype}] {name}: fare={val_s} {avail}"
        elif mtype == "flight":
            val = m.get("last_flight_price")
            val_s = f"{val:g}" if val is not None else "?"
            line = f"  [{mtype}] {name}: price={val_s}"
        else:
            line = f"  [{mtype}] {name}"
        errs = m.get("consecutive_errors", 0)
        if errs:
            line += f" [!{errs} errors]"
        lines.append(line)

    return "\n".join(lines)


@app.route("/api/digest/send", methods=["POST"])
@require_admin
def api_send_digest():
    try:
        text = _build_digest()
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500
    try:
        _send_alert({"name": "digest"}, "Price Monitor Digest", text)
    except Exception:
        pass
    return jsonify(ok=True, digest=text)


def _digest_loop() -> None:
    interval_h = int(os.environ.get("DIGEST_INTERVAL_HOURS", "24"))
    time.sleep(30)
    while True:
        time.sleep(interval_h * 3600)
        try:
            text = _build_digest()
            from price_monitor.notify_settings import load_notify_settings
            from price_monitor.notifier import notify_price_alert
            from price_monitor.db import _json_path
            settings = load_notify_settings(_json_path("items.json"))
            notify_price_alert(settings, title="Price Monitor Digest", message=text)
            _add_log("Digest sent")
        except Exception as e:
            _add_log(f"Digest error: {e}")


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
        expired=sum(1 for m in monitors if m.get("status") == "expired"),
        logs=list(_global_log),
    )


@app.route("/health")
def health():
    return jsonify(status="ok", monitoring=_monitor_running)


# ═══════════════════════════════════════════════════════════════════════
# AUTO-RESUME ON STARTUP
# ═══════════════════════════════════════════════════════════════════════

def _bootstrap() -> None:
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
    time.sleep(2)
    try:
        _bootstrap()
    except Exception as exc:
        _add_log(f"Bootstrap error: {exc}")

threading.Thread(target=_deferred_bootstrap, daemon=True).start()
threading.Thread(target=_keep_alive_loop, daemon=True).start()

digest_enabled = os.environ.get("DIGEST_INTERVAL_HOURS", "").strip()
if digest_enabled:
    threading.Thread(target=_digest_loop, daemon=True).start()


def run_web(host: str = "0.0.0.0", port: int = 8080) -> None:
    _add_log("Web dashboard started")
    app.run(host=host, port=port, debug=False)
