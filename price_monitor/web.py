from __future__ import annotations

import csv
import io
import json
import logging
import logging.handlers
import os
import queue
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask, Response, jsonify, redirect, render_template, request, session, url_for,
)

from price_monitor.db import (
    add_audit, add_event, count_users, create_monitor, create_user,
    create_delete_request, delete_monitor, delete_user, get_audit_log,
    get_delete_request, get_events, get_monitor, get_recent_events,
    get_settings, get_user_by_username, list_delete_requests, list_monitors,
    list_users, save_settings, update_delete_request, update_monitor,
    update_user, verify_user,
)

# ═══════════════════════════════════════════════════════════════════════
# FILE-BASED LOGGING
# ═══════════════════════════════════════════════════════════════════════

def _setup_logging() -> logging.Logger:
    env_dir = os.environ.get("PRICE_MONITOR_DATA_DIR", "").strip()
    log_dir = Path(env_dir) if env_dir else (Path(__file__).resolve().parent.parent / "data")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "price_monitor.log"

    logger = logging.getLogger("price_monitor")
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


_logger = _setup_logging()


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

_lock = threading.Lock()
_monitor_running = False
_monitor_thread: threading.Thread | None = None
_stop_event = threading.Event()
_global_log: deque[str] = deque(maxlen=200)

_sse_listeners: list[queue.Queue] = []
_sse_lock = threading.Lock()


def _add_log(msg: str, level: str = "info") -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    entry = f"[{ts}] {msg}"
    _global_log.appendleft(entry)
    _sse_broadcast({"type": "log", "message": entry})
    getattr(_logger, level, _logger.info)(msg)


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
# AUTH (fixed admin + user registration)
# ═══════════════════════════════════════════════════════════════════════

_ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin").strip()
_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123").strip()


def _ensure_admin_exists() -> None:
    """Create the fixed admin account on first startup if it doesn't exist."""
    existing = get_user_by_username(_ADMIN_USERNAME)
    if existing:
        return
    create_user({
        "username": _ADMIN_USERNAME,
        "password": _ADMIN_PASSWORD,
        "role": "admin",
    })
    _logger.info("STARTUP     | Admin account '%s' created", _ADMIN_USERNAME)


def _session_role() -> str | None:
    return session.get("role")


def _validate_session():
    """Check the session user still exists and sync role from DB."""
    if not session.get("authed"):
        return False
    user = get_user_by_username(session.get("username", ""))
    if not user:
        session.clear()
        return False
    if user.get("role") != session.get("role"):
        session["role"] = user["role"]
    return True


def require_auth(f):
    """Allow any authenticated user."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not _validate_session():
            if request.is_json or request.path.startswith("/api/"):
                return jsonify(ok=False, error="Not authenticated"), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return wrapped


def require_admin(f):
    """Only allow admin role."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not _validate_session():
            if request.is_json or request.path.startswith("/api/"):
                return jsonify(ok=False, error="Not authenticated"), 401
            return redirect(url_for("login_page"))
        if session.get("role") != "admin":
            if request.is_json or request.path.startswith("/api/"):
                return jsonify(ok=False, error="Admin access required"), 403
            return "Forbidden", 403
        return f(*args, **kwargs)
    return wrapped


def _can_access_monitor(mon: dict) -> bool:
    """Check if current user owns the monitor or is admin."""
    if session.get("role") == "admin":
        return True
    return mon.get("created_by", "") == session.get("username", "")


@app.route("/login", methods=["GET", "POST"])
def login_page():
    error = None
    registered = request.args.get("registered") == "1"
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            error = "Username and password are required"
        else:
            user = verify_user(username, password)
            if user:
                session["authed"] = True
                session["role"] = user["role"]
                session["username"] = user["username"]
                session["user_id"] = user["id"]
                _logger.info("LOGIN OK    | user=%s role=%s ip=%s", username, user["role"], request.remote_addr)
                return redirect(url_for("admin_dashboard"))
            else:
                _logger.warning("LOGIN FAIL  | user=%s ip=%s", username, request.remote_addr)
                error = "Invalid username or password"
    return render_template("login.html", error=error, registered=registered)


@app.route("/register", methods=["GET", "POST"])
def register_page():
    error = None
    success = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not username or not password:
            error = "Username and password are required"
        elif len(username) < 10:
            error = "Username must be at least 10 characters"
        elif len(username) > 30:
            error = "Username must be at most 30 characters"
        elif not username.replace("_", "").replace("@", "").isalnum():
            error = "Username can only contain letters, numbers, underscores, and @"
        elif len(password) < 8:
            error = "Password must be at least 8 characters"
        elif password != confirm:
            error = "Passwords do not match"
        elif username == _ADMIN_USERNAME.lower():
            error = "This username is reserved"
        elif get_user_by_username(username):
            error = "Username already taken"
        else:
            create_user({
                "username": username,
                "password": password,
                "role": "viewer",
            })
            add_audit("register", "user", "", username, "viewer")
            _logger.info("REGISTER    | user=%s ip=%s", username, request.remote_addr)
            return redirect(url_for("login_page", registered="1"))

    return render_template("register.html", error=error)


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
    role = session.get("role", "viewer")
    username = session.get("username", "")
    is_admin = role == "admin"

    filt_type = request.args.get("type", "")
    filt_status = request.args.get("status", "")
    filt_category = request.args.get("category", "")
    filt_tag = request.args.get("tag", "")
    filt_creator = request.args.get("creator", "")

    if is_admin:
        monitors = list_monitors(
            monitor_type=filt_type or None,
            status=filt_status or None,
            category=filt_category or None,
            tag=filt_tag or None,
            created_by=filt_creator or None,
        )
        all_monitors = list_monitors()
    else:
        monitors = list_monitors(
            monitor_type=filt_type or None,
            status=filt_status or None,
            category=filt_category or None,
            tag=filt_tag or None,
            created_by=username,
        )
        all_monitors = list_monitors(created_by=username)

    categories = sorted({m.get("category", "") for m in all_monitors if m.get("category")})
    creators = sorted({m.get("created_by", "") for m in all_monitors if m.get("created_by")})
    all_tags: set[str] = set()
    for m in all_monitors:
        for t in (m.get("tags") or []):
            if t:
                all_tags.add(t)
    recent_audit = get_audit_log(limit=30) if is_admin else []
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
        logs=list(_global_log) if is_admin else [],
        role=role,
        username=username,
        primary_admin=_ADMIN_USERNAME,
        audit_entries=recent_audit,
    )


@app.route("/monitor/<monitor_id>")
@require_auth
def monitor_detail_page(monitor_id):
    mon = get_monitor(monitor_id)
    if not mon:
        return "Not found", 404
    if session.get("role") != "admin" and mon.get("created_by", "") != session.get("username", ""):
        return "Access denied", 403
    events = get_events(monitor_id, limit=200)
    price_key = "price" if mon.get("type") == "product" else "flight_price"
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
        username=session.get("username", ""),
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
@require_auth
def api_create_monitor():
    data = request.get_json(force=True)
    mtype = data.get("type", "product")

    mon: dict = {
        "type": mtype,
        "name": (data.get("name") or "").strip(),
        "category": (data.get("category") or "").strip(),
        "tags": _parse_tags(data.get("tags")),
        "created_by": (data.get("created_by") or session.get("username") or "admin").strip(),
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
        return jsonify(ok=False, error="type must be 'product' or 'flight'"), 400

    created = create_monitor(mon)
    add_event(created["id"], "status_change", {"status": "active", "action": "created"})
    add_audit("create", "monitor", created["id"], created.get("name", ""), session.get("username", "system"))
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
@require_auth
def api_update_monitor(monitor_id):
    mon = get_monitor(monitor_id)
    if not mon:
        return jsonify(ok=False, error="Not found"), 404
    if not _can_access_monitor(mon):
        return jsonify(ok=False, error="Access denied"), 403
    data = request.get_json(force=True)
    safe_keys = {
        "name", "category", "tags", "check_interval_min", "url", "budget",
        "price_selector", "monitor_until", "alert_mode", "alert_drop_percent",
        "comparison_group", "flight_origin", "flight_destination", "flight_date",
        "flight_return_date", "flight_max_price", "flight_airline_pref",
    }
    if session.get("role") == "admin":
        safe_keys.add("created_by")
    updates = {k: v for k, v in data.items() if k in safe_keys}
    if "tags" in updates:
        updates["tags"] = _parse_tags(updates["tags"])
    updated = update_monitor(monitor_id, updates)
    if not updated:
        return jsonify(ok=False, error="Not found"), 404
    add_event(monitor_id, "status_change", {"action": "edited", "fields": list(updates.keys())})
    add_audit("update", "monitor", monitor_id, updated.get("name", ""), session.get("username", "system"),
              {"fields": list(updates.keys())})
    _add_log(f"Monitor updated: {updated.get('name', monitor_id)}")
    return jsonify(ok=True, monitor=updated)


@app.route("/api/monitors/<monitor_id>", methods=["DELETE"])
@require_auth
def api_delete_monitor(monitor_id):
    mon = get_monitor(monitor_id)
    if mon and not _can_access_monitor(mon):
        return jsonify(ok=False, error="Access denied"), 403
    name = mon.get("name", monitor_id) if mon else monitor_id
    if delete_monitor(monitor_id):
        add_audit("delete", "monitor", monitor_id, name, session.get("username", "system"))
        _add_log(f"Monitor deleted: {name}")
        return jsonify(ok=True)
    return jsonify(ok=False, error="Not found"), 404


@app.route("/api/monitors/<monitor_id>/pause", methods=["POST"])
@require_auth
def api_pause_monitor(monitor_id):
    mon = get_monitor(monitor_id)
    if not mon:
        return jsonify(ok=False, error="Not found"), 404
    if not _can_access_monitor(mon):
        return jsonify(ok=False, error="Access denied"), 403
    updated = update_monitor(monitor_id, {"status": "paused"})
    if not updated:
        return jsonify(ok=False, error="Not found"), 404
    add_event(monitor_id, "status_change", {"status": "paused"})
    add_audit("pause", "monitor", monitor_id, updated.get("name", ""), session.get("username", "system"))
    _add_log(f"Monitor paused: {updated.get('name', monitor_id)}")
    return jsonify(ok=True, monitor=updated)


@app.route("/api/monitors/<monitor_id>/resume", methods=["POST"])
@require_auth
def api_resume_monitor(monitor_id):
    mon = get_monitor(monitor_id)
    if not mon:
        return jsonify(ok=False, error="Not found"), 404
    if not _can_access_monitor(mon):
        return jsonify(ok=False, error="Access denied"), 403
    updated = update_monitor(monitor_id, {
        "status": "active", "notified_budget": False, "notified_available": False,
    })
    if not updated:
        return jsonify(ok=False, error="Not found"), 404
    add_event(monitor_id, "status_change", {"status": "active", "action": "resumed"})
    add_audit("resume", "monitor", monitor_id, updated.get("name", ""), session.get("username", "system"))
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
    add_audit("duplicate", "monitor", created["id"], created.get("name", ""), session.get("username", "system"))
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
    add_audit("batch_pause", "monitor", "", f"{done} monitors", session.get("username", "system"))
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
    add_audit("batch_resume", "monitor", "", f"{done} monitors", session.get("username", "system"))
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
    add_audit("batch_delete", "monitor", "", f"{done} monitors", session.get("username", "system"))
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
    skipped = 0
    for item in data:
        if not isinstance(item, dict):
            skipped += 1
            continue
        mtype = item.get("type", "product")
        if mtype == "product" and (not item.get("name") or not item.get("url")):
            skipped += 1
            continue
        if mtype == "train":
            skipped += 1
            continue
        if mtype == "flight" and (not item.get("flight_origin") or not item.get("flight_destination")):
            skipped += 1
            continue
        item.pop("id", None)
        item.pop("created_at", None)
        item.setdefault("status", "active")
        item.setdefault("created_by", session.get("username", "admin"))
        create_monitor(item)
        created += 1
    add_audit("import", "monitor", "", f"{created} monitors", session.get("username", "system"))
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
    _logger.debug("FETCH       | %s | got %d bytes from %s", mon.get("name", "?"), len(html), mon["url"][:80])
    price = extract_price(html, mon.get("price_selector"), mon["url"])
    if price is None:
        _logger.warning("NO PRICE    | %s | could not extract price from %s (html=%d bytes, selector=%s)",
                        mon.get("name", "?"), mon["url"][:80], len(html), mon.get("price_selector") or "auto")
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

    _logger.info("ALERT       | %s | %s", mon.get("name", "?"), title)
    try:
        settings = load_notify_settings(_json_path("items.json"))
        notify_price_alert(settings, title=title, message=message)
    except Exception as e:
        _logger.error("ALERT FAIL  | %s | %s: %s", mon.get("name", "?"), title, str(e)[:200])


def check_single_monitor(monitor_id: str) -> dict:
    mon = get_monitor(monitor_id)
    if not mon:
        raise ValueError("Monitor not found")

    name = mon.get("name", monitor_id[:8])
    _logger.debug("CHECK START | %s | type=%s url=%s", name, mon["type"], mon.get("url", mon.get("flight_origin", ""))[:80])

    try:
        if mon["type"] == "product":
            updates, details = _do_check_product(mon)
        elif mon["type"] == "flight":
            updates, details = _do_check_flight(mon)
        else:
            raise ValueError(f"Unknown monitor type: {mon['type']}")

        updates["consecutive_errors"] = 0
        updates["last_error_msg"] = None
        update_monitor(monitor_id, updates)
        add_event(monitor_id, "check", details)
        _sse_broadcast({"type": "check", "monitor_id": monitor_id})
        _logger.info("CHECK OK    | %s | %s", name, json.dumps({k: v for k, v in details.items() if v is not None}, default=str)[:200])
        return {**mon, **updates}

    except Exception as e:
        err_count = (mon.get("consecutive_errors") or 0) + 1
        err_updates = {
            "consecutive_errors": err_count,
            "last_error_at": datetime.now(timezone.utc).isoformat(),
            "last_error_msg": str(e)[:500],
        }
        update_monitor(monitor_id, err_updates)
        add_event(monitor_id, "error", {"error": str(e)[:500]})
        _logger.error("CHECK FAIL  | %s | error #%d: %s", name, err_count, str(e)[:300])
        raise


@app.route("/api/monitors/<monitor_id>/check", methods=["POST"])
@require_auth
def api_check_monitor(monitor_id):
    mon = get_monitor(monitor_id)
    if mon and not _can_access_monitor(mon):
        return jsonify(ok=False, error="Access denied"), 403
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
        val = m.get("last_price") or m.get("last_flight_price")
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
@require_auth
def api_save_notifications():
    data = request.get_json(force=True)
    save_settings(data)

    from price_monitor.notify_settings import write_notify_config_file
    from price_monitor.db import _json_path
    write_notify_config_file(_json_path("items.json"), data)

    add_audit("update", "settings", "notify", "notification settings", session.get("username", "system"))
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
# DEBUG LOG VIEWER
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/logs")
@require_admin
def api_view_logs():
    """Return the last N lines from the persistent debug log file."""
    lines_requested = min(int(request.args.get("lines", 200)), 2000)
    level_filter = request.args.get("level", "").upper()
    env_dir = os.environ.get("PRICE_MONITOR_DATA_DIR", "").strip()
    log_dir = Path(env_dir) if env_dir else (Path(__file__).resolve().parent.parent / "data")
    log_file = log_dir / "price_monitor.log"
    if not log_file.exists():
        return jsonify(ok=True, lines=[], total=0)
    all_lines = log_file.read_text(encoding="utf-8", errors="replace").strip().split("\n")
    if level_filter:
        all_lines = [ln for ln in all_lines if f"| {level_filter}" in ln]
    tail = all_lines[-lines_requested:]
    tail.reverse()
    return jsonify(ok=True, lines=tail, total=len(all_lines))


# ═══════════════════════════════════════════════════════════════════════
# USER MANAGEMENT (admin only)
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/users")
@require_admin
def api_list_users():
    users = list_users()
    for u in users:
        monitors = list_monitors(created_by=u["username"])
        u["monitor_count"] = len(monitors)
        u["active_monitors"] = sum(1 for m in monitors if m.get("status") == "active")
    return jsonify(users=users)


def _execute_user_deletion(target: dict, deleted_by: str, *, notify: bool = True):
    """Delete a user and all their monitors. Optionally notify via email/ntfy."""
    monitors = list_monitors(created_by=target["username"])

    if notify and monitors:
        mon_names = ", ".join(m.get("name", m["id"][:8]) for m in monitors)
        try:
            _send_alert(
                {"name": f"Account: {target['username']}"},
                "Your monitors have been stopped",
                f"Your account ({target['username']}) has been deleted by an administrator. "
                f"The following monitors were stopped and removed: {mon_names}",
            )
        except Exception as e:
            _logger.warning("DELETE NOTIFY FAIL | user=%s err=%s", target["username"], str(e)[:200])

    for m in monitors:
        delete_monitor(m["id"])

    delete_user(target["id"])
    add_audit("delete_user", "user", target["id"], deleted_by, "admin")
    _logger.info("DELETE USER | user=%s deleted_by=%s monitors=%d", target["username"], deleted_by, len(monitors))
    _add_log(f"User '{target['username']}' deleted by {deleted_by} ({len(monitors)} monitor(s) removed)")


@app.route("/api/users/<user_id>", methods=["DELETE"])
@require_admin
def api_delete_user(user_id):
    users = list_users()
    target = next((u for u in users if u["id"] == user_id), None)
    if not target:
        return jsonify(ok=False, error="User not found"), 404
    if target["username"] == _ADMIN_USERNAME:
        return jsonify(ok=False, error="Cannot delete the primary admin account"), 400
    if target["username"] == session.get("username"):
        return jsonify(ok=False, error="Use 'Delete My Account' to delete your own account"), 400

    is_primary = session.get("username") == _ADMIN_USERNAME

    if not is_primary:
        existing = list_delete_requests(status="pending")
        already = any(r["target_user_id"] == user_id for r in existing)
        if already:
            return jsonify(ok=False, error="A deletion request for this user is already pending"), 409
        create_delete_request({
            "target_user_id": user_id,
            "target_username": target["username"],
            "requested_by": session.get("username", ""),
            "monitor_count": len(list_monitors(created_by=target["username"])),
        })
        _logger.info("DELETE REQ  | target=%s requested_by=%s", target["username"], session.get("username"))
        _add_log(f"Deletion request for '{target['username']}' submitted by {session.get('username')}")
        return jsonify(ok=True, request_submitted=True)

    _execute_user_deletion(target, session.get("username", "admin"))
    return jsonify(ok=True)


@app.route("/api/users/self-delete", methods=["POST"])
@require_auth
def api_self_delete():
    username = session.get("username", "")
    if username == _ADMIN_USERNAME:
        return jsonify(ok=False, error="Primary admin account cannot be deleted"), 400

    data = request.get_json(force=True)
    password = data.get("password", "")
    if not password:
        return jsonify(ok=False, error="Password is required"), 400

    user = verify_user(username, password)
    if not user:
        return jsonify(ok=False, error="Incorrect password"), 401

    _execute_user_deletion(user, username, notify=False)
    session.clear()
    return jsonify(ok=True, redirect="/login")


@app.route("/api/delete-requests")
@require_admin
def api_list_delete_requests():
    if session.get("username") != _ADMIN_USERNAME:
        return jsonify(ok=False, error="Only the primary admin can view deletion requests"), 403
    reqs = list_delete_requests(status="pending")
    return jsonify(requests=reqs)


@app.route("/api/delete-requests/count")
@require_admin
def api_delete_request_count():
    if session.get("username") != _ADMIN_USERNAME:
        return jsonify(count=0)
    reqs = list_delete_requests(status="pending")
    return jsonify(count=len(reqs))


@app.route("/api/delete-requests/mine")
@require_admin
def api_my_delete_requests():
    """Return requests submitted by the current (secondary) admin."""
    username = session.get("username", "")
    all_reqs = list_delete_requests()
    mine = [r for r in all_reqs if r.get("requested_by") == username]
    mine.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return jsonify(requests=mine[:20])


@app.route("/api/delete-requests/mine/ack", methods=["POST"])
@require_admin
def api_ack_my_requests():
    """Mark resolved requests as acknowledged so toasts are not shown again."""
    username = session.get("username", "")
    data = request.get_json(force=True)
    req_ids = data.get("ids", [])
    for rid in req_ids:
        req = get_delete_request(rid)
        if req and req.get("requested_by") == username and req.get("status") != "pending":
            update_delete_request(rid, {"acknowledged": True})
    return jsonify(ok=True)


@app.route("/api/delete-requests/<req_id>/approve", methods=["POST"])
@require_admin
def api_approve_delete_request(req_id):
    if session.get("username") != _ADMIN_USERNAME:
        return jsonify(ok=False, error="Only the primary admin can approve deletion requests"), 403

    req = get_delete_request(req_id)
    if not req:
        return jsonify(ok=False, error="Request not found"), 404
    if req["status"] != "pending":
        return jsonify(ok=False, error="Request is no longer pending"), 400

    users = list_users()
    target = next((u for u in users if u["id"] == req["target_user_id"]), None)
    if not target:
        update_delete_request(req_id, {"status": "approved", "resolved_at": datetime.now(timezone.utc).isoformat(), "acknowledged": False})
        return jsonify(ok=False, error="User no longer exists"), 404

    _execute_user_deletion(target, _ADMIN_USERNAME)
    update_delete_request(req_id, {"status": "approved", "resolved_at": datetime.now(timezone.utc).isoformat(), "acknowledged": False})
    _add_log(f"Deletion request for '{req['target_username']}' approved (requested by {req.get('requested_by', '?')})")
    return jsonify(ok=True)


@app.route("/api/delete-requests/<req_id>/reject", methods=["POST"])
@require_admin
def api_reject_delete_request(req_id):
    if session.get("username") != _ADMIN_USERNAME:
        return jsonify(ok=False, error="Only the primary admin can reject deletion requests"), 403

    req = get_delete_request(req_id)
    if not req:
        return jsonify(ok=False, error="Request not found"), 404
    if req["status"] != "pending":
        return jsonify(ok=False, error="Request is no longer pending"), 400

    update_delete_request(req_id, {"status": "rejected", "resolved_at": datetime.now(timezone.utc).isoformat(), "acknowledged": False})
    _logger.info("DELETE REJ  | target=%s rejected_by=%s", req["target_username"], session.get("username"))
    _add_log(f"Deletion request for '{req['target_username']}' rejected (requested by {req.get('requested_by', '?')})")
    return jsonify(ok=True)


@app.route("/api/users/<user_id>/reset-password", methods=["POST"])
@require_admin
def api_reset_password(user_id):
    from werkzeug.security import generate_password_hash
    data = request.get_json(force=True)
    new_pw = data.get("password", "").strip()
    if len(new_pw) < 8:
        return jsonify(ok=False, error="Password must be at least 8 characters"), 400

    users = list_users()
    target = next((u for u in users if u["id"] == user_id), None)
    if not target:
        return jsonify(ok=False, error="User not found"), 404

    update_user(user_id, {"password_hash": generate_password_hash(new_pw)})
    add_audit("reset_password", "user", user_id, session.get("user", "admin"), "admin")
    _logger.info("RESET PW    | user=%s by=%s", target["username"], session.get("user"))
    _add_log(f"Password reset for '{target['username']}' by admin")
    return jsonify(ok=True)


@app.route("/api/users/<user_id>/role", methods=["POST"])
@require_admin
def api_change_role(user_id):
    if session.get("username") != _ADMIN_USERNAME:
        return jsonify(ok=False, error="Only the primary admin can change roles"), 403

    data = request.get_json(force=True)
    new_role = data.get("role", "").strip().lower()
    if new_role not in ("viewer", "admin"):
        return jsonify(ok=False, error="Invalid role"), 400

    users = list_users()
    target = next((u for u in users if u["id"] == user_id), None)
    if not target:
        return jsonify(ok=False, error="User not found"), 404
    if target["username"] == _ADMIN_USERNAME:
        return jsonify(ok=False, error="Cannot change the primary admin's role"), 400

    update_user(user_id, {"role": new_role})
    add_audit("change_role", "user", user_id, session.get("username", "admin"), "admin")
    _logger.info("CHANGE ROLE | user=%s new_role=%s by=%s", target["username"], new_role, session.get("username"))
    return jsonify(ok=True)


@app.route("/api/users/self/change-password", methods=["POST"])
@require_auth
def api_self_change_password():
    """Allow any user to change their own password."""
    data = request.get_json(force=True)
    current_pw = data.get("current_password", "").strip()
    new_pw = data.get("new_password", "").strip()
    confirm_pw = data.get("confirm_password", "").strip()

    if not current_pw or not new_pw:
        return jsonify(ok=False, error="Current and new passwords are required"), 400
    if len(new_pw) < 8:
        return jsonify(ok=False, error="New password must be at least 8 characters"), 400
    if new_pw != confirm_pw:
        return jsonify(ok=False, error="New passwords do not match"), 400

    username = session.get("username", "")
    user = verify_user(username, current_pw)
    if not user:
        return jsonify(ok=False, error="Current password is incorrect"), 401

    from werkzeug.security import generate_password_hash
    update_user(user["id"], {"password_hash": generate_password_hash(new_pw)})
    add_audit("change_password", "user", user["id"], username, session.get("role", "viewer"))
    _logger.info("CHANGE PW   | user=%s (self)", username)
    return jsonify(ok=True)


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

    if mtype == "flight":
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
    my_thread = threading.current_thread()
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
                elif mon["type"] == "flight":
                    fp = result.get("last_flight_price")
                    _add_log(f"Check {name}: {fp:g}" if fp is not None else f"Check {name}: no price")
            except Exception as e:
                _add_log(f"Check error {mon.get('name', mon['id'])}: {e}")

        intervals = [_smart_interval(m) for m in active if not _is_expired(m)]
        min_interval = min(intervals) if intervals else 60
        if _stop_event.wait(timeout=max(60, min_interval * 60)):
            break
    with _lock:
        if _monitor_thread is my_thread:
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
    add_audit("start", "monitor_loop", "", "", session.get("username", "system"))
    _add_log(f"Monitoring started ({len(active)} active monitors)")
    return jsonify(ok=True, monitoring=True)


@app.route("/api/monitor/stop", methods=["POST"])
@require_admin
def stop_monitor_loop():
    global _monitor_running
    old_thread = None
    with _lock:
        _monitor_running = False
        _stop_event.set()
        old_thread = _monitor_thread
    if old_thread and old_thread.is_alive():
        old_thread.join(timeout=5)
    add_audit("stop", "monitor_loop", "", "", session.get("username", "system"))
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
    is_admin = session.get("role") == "admin"
    username = session.get("username", "")
    if is_admin:
        monitors = list_monitors()
    else:
        monitors = list_monitors(created_by=username)
    return jsonify(
        monitoring=_monitor_running,
        total=len(monitors),
        active=sum(1 for m in monitors if m.get("status") == "active"),
        paused=sum(1 for m in monitors if m.get("status") == "paused"),
        completed=sum(1 for m in monitors if m.get("status") == "completed"),
        expired=sum(1 for m in monitors if m.get("status") == "expired"),
        logs=list(_global_log) if is_admin else [],
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


_ensure_admin_exists()


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
