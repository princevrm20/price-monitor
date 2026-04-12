"""Database abstraction layer.

Uses Supabase (PostgreSQL) when SUPABASE_URL + SUPABASE_KEY are set,
otherwise falls back to local JSON files for development.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_supabase_client = None
_json_locks: dict[str, threading.Lock] = {}
_json_locks_lock = threading.Lock()


def _get_supabase():
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        return None
    from supabase import create_client
    _supabase_client = create_client(url, key)
    return _supabase_client


def use_supabase() -> bool:
    return _get_supabase() is not None


# ── helpers ──────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_path(name: str) -> Path:
    env = os.environ.get("PRICE_MONITOR_DATA_DIR")
    if env:
        p = Path(env)
    else:
        p = Path(__file__).resolve().parent.parent / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p / name


def _read_json(name: str) -> list[dict]:
    p = _json_path(name)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _file_lock(name: str) -> threading.Lock:
    with _json_locks_lock:
        if name not in _json_locks:
            _json_locks[name] = threading.Lock()
        return _json_locks[name]


def _write_json(name: str, data: list[dict]) -> None:
    p = _json_path(name)
    p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════
# MONITORS CRUD
# ═══════════════════════════════════════════════════════════════════════

def create_monitor(mon: dict) -> dict:
    mon.setdefault("id", str(uuid.uuid4()))
    mon.setdefault("status", "active")
    mon.setdefault("created_at", _now_iso())
    mon.setdefault("created_by", "admin")
    mon.setdefault("category", "")
    mon.setdefault("tags", [])
    mon.setdefault("check_interval_min", 60)
    mon.setdefault("alert_mode", "budget")
    mon.setdefault("alert_drop_percent", None)
    mon.setdefault("highest_price", None)
    mon.setdefault("highest_fare", None)
    mon.setdefault("consecutive_errors", 0)
    mon.setdefault("last_error_at", None)
    mon.setdefault("last_error_msg", None)
    mon.setdefault("comparison_group", "")

    sb = _get_supabase()
    if sb:
        resp = sb.table("monitors").insert(mon).execute()
        return resp.data[0]

    with _file_lock("monitors.json"):
        all_m = _read_json("monitors.json")
        all_m.append(mon)
        _write_json("monitors.json", all_m)
    return mon


def get_monitor(monitor_id: str) -> dict | None:
    sb = _get_supabase()
    if sb:
        resp = sb.table("monitors").select("*").eq("id", monitor_id).execute()
        return resp.data[0] if resp.data else None

    for m in _read_json("monitors.json"):
        if m["id"] == monitor_id:
            return m
    return None


def list_monitors(
    *,
    status: str | None = None,
    monitor_type: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    comparison_group: str | None = None,
    created_by: str | None = None,
) -> list[dict]:
    sb = _get_supabase()
    if sb:
        q = sb.table("monitors").select("*").order("created_at", desc=True)
        if status:
            q = q.eq("status", status)
        if monitor_type:
            q = q.eq("type", monitor_type)
        if category:
            q = q.eq("category", category)
        if tag:
            q = q.contains("tags", [tag])
        if comparison_group:
            q = q.eq("comparison_group", comparison_group)
        if created_by:
            q = q.eq("created_by", created_by)
        return q.execute().data

    all_m = _read_json("monitors.json")
    if status:
        all_m = [m for m in all_m if m.get("status") == status]
    if monitor_type:
        all_m = [m for m in all_m if m.get("type") == monitor_type]
    if category:
        all_m = [m for m in all_m if m.get("category") == category]
    if tag:
        all_m = [m for m in all_m if tag in (m.get("tags") or [])]
    if comparison_group:
        all_m = [m for m in all_m if m.get("comparison_group") == comparison_group]
    if created_by:
        all_m = [m for m in all_m if m.get("created_by") == created_by]
    all_m.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return all_m


def update_monitor(monitor_id: str, updates: dict) -> dict | None:
    sb = _get_supabase()
    if sb:
        resp = sb.table("monitors").update(updates).eq("id", monitor_id).execute()
        return resp.data[0] if resp.data else None

    with _file_lock("monitors.json"):
        all_m = _read_json("monitors.json")
        for m in all_m:
            if m["id"] == monitor_id:
                m.update(updates)
                _write_json("monitors.json", all_m)
                return m
    return None


def delete_monitor(monitor_id: str) -> bool:
    sb = _get_supabase()
    if sb:
        sb.table("monitors").delete().eq("id", monitor_id).execute()
        sb.table("events").delete().eq("monitor_id", monitor_id).execute()
        return True

    with _file_lock("monitors.json"):
        all_m = _read_json("monitors.json")
        before = len(all_m)
        all_m = [m for m in all_m if m["id"] != monitor_id]
        if len(all_m) == before:
            return False
        _write_json("monitors.json", all_m)
    with _file_lock("events.json"):
        all_e = _read_json("events.json")
        all_e = [e for e in all_e if e.get("monitor_id") != monitor_id]
        _write_json("events.json", all_e)
    return True


# ═══════════════════════════════════════════════════════════════════════
# EVENTS (per-monitor log)
# ═══════════════════════════════════════════════════════════════════════

def add_event(monitor_id: str, event_type: str, details: dict | None = None) -> dict:
    ev = {
        "id": str(uuid.uuid4()),
        "monitor_id": monitor_id,
        "event_type": event_type,
        "details": details or {},
        "created_at": _now_iso(),
    }
    sb = _get_supabase()
    if sb:
        resp = sb.table("events").insert(ev).execute()
        return resp.data[0]

    with _file_lock("events.json"):
        all_e = _read_json("events.json")
        all_e.append(ev)
        if len(all_e) > 5000:
            all_e = all_e[-5000:]
        _write_json("events.json", all_e)
    return ev


def get_events(
    monitor_id: str,
    *,
    limit: int = 100,
    event_type: str | None = None,
) -> list[dict]:
    sb = _get_supabase()
    if sb:
        q = sb.table("events").select("*").eq("monitor_id", monitor_id).order("created_at", desc=True).limit(limit)
        if event_type:
            q = q.eq("event_type", event_type)
        return q.execute().data

    all_e = _read_json("events.json")
    filtered = [e for e in all_e if e.get("monitor_id") == monitor_id]
    if event_type:
        filtered = [e for e in filtered if e.get("event_type") == event_type]
    filtered.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return filtered[:limit]


def get_recent_events(limit: int = 50) -> list[dict]:
    sb = _get_supabase()
    if sb:
        return sb.table("events").select("*").order("created_at", desc=True).limit(limit).execute().data

    all_e = _read_json("events.json")
    all_e.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return all_e[:limit]


# ═══════════════════════════════════════════════════════════════════════
# AUDIT LOG
# ═══════════════════════════════════════════════════════════════════════

def add_audit(action: str, target_type: str, target_id: str,
              target_name: str = "", user_role: str = "admin",
              details: dict | None = None) -> dict:
    entry = {
        "id": str(uuid.uuid4()),
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "target_name": target_name,
        "user_role": user_role,
        "details": details or {},
        "created_at": _now_iso(),
    }
    sb = _get_supabase()
    if sb:
        resp = sb.table("audit_log").insert(entry).execute()
        return resp.data[0]

    with _file_lock("audit_log.json"):
        all_a = _read_json("audit_log.json")
        all_a.append(entry)
        if len(all_a) > 2000:
            all_a = all_a[-2000:]
        _write_json("audit_log.json", all_a)
    return entry


def get_audit_log(
    *,
    limit: int = 100,
    action: str | None = None,
    target_id: str | None = None,
) -> list[dict]:
    sb = _get_supabase()
    if sb:
        q = sb.table("audit_log").select("*").order("created_at", desc=True).limit(limit)
        if action:
            q = q.eq("action", action)
        if target_id:
            q = q.eq("target_id", target_id)
        return q.execute().data

    all_a = _read_json("audit_log.json")
    if action:
        all_a = [a for a in all_a if a.get("action") == action]
    if target_id:
        all_a = [a for a in all_a if a.get("target_id") == target_id]
    all_a.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    return all_a[:limit]


# ═══════════════════════════════════════════════════════════════════════
# USERS (registration + login)
# ═══════════════════════════════════════════════════════════════════════

def create_user(user: dict) -> dict:
    from werkzeug.security import generate_password_hash
    user.setdefault("id", str(uuid.uuid4()))
    user["password_hash"] = generate_password_hash(user.pop("password"))
    user.setdefault("role", "viewer")
    user.setdefault("created_at", _now_iso())

    sb = _get_supabase()
    if sb:
        resp = sb.table("users").insert(user).execute()
        return resp.data[0]

    with _file_lock("users.json"):
        all_u = _read_json("users.json")
        all_u.append(user)
        _write_json("users.json", all_u)
    return user


def get_user_by_username(username: str) -> dict | None:
    sb = _get_supabase()
    if sb:
        resp = sb.table("users").select("*").eq("username", username).execute()
        return resp.data[0] if resp.data else None

    for u in _read_json("users.json"):
        if u.get("username", "").lower() == username.lower():
            return u
    return None


def verify_user(username: str, password: str) -> dict | None:
    from werkzeug.security import check_password_hash
    user = get_user_by_username(username)
    if user and check_password_hash(user["password_hash"], password):
        return user
    return None


def list_users() -> list[dict]:
    sb = _get_supabase()
    if sb:
        resp = sb.table("users").select("id, username, role, created_at").order("created_at").execute()
        return resp.data

    return [
        {k: u[k] for k in ("id", "username", "role", "created_at") if k in u}
        for u in _read_json("users.json")
    ]


def count_users() -> int:
    sb = _get_supabase()
    if sb:
        resp = sb.table("users").select("id", count="exact").execute()
        return resp.count or 0
    return len(_read_json("users.json"))


def update_user(user_id: str, updates: dict) -> dict | None:
    sb = _get_supabase()
    if sb:
        resp = sb.table("users").update(updates).eq("id", user_id).execute()
        return resp.data[0] if resp.data else None

    with _file_lock("users.json"):
        all_u = _read_json("users.json")
        for u in all_u:
            if u["id"] == user_id:
                u.update(updates)
                _write_json("users.json", all_u)
                return u
    return None


def delete_user(user_id: str) -> bool:
    sb = _get_supabase()
    if sb:
        sb.table("users").delete().eq("id", user_id).execute()
        return True

    with _file_lock("users.json"):
        all_u = _read_json("users.json")
        before = len(all_u)
        all_u = [u for u in all_u if u["id"] != user_id]
        if len(all_u) == before:
            return False
        _write_json("users.json", all_u)
    return True


# ═══════════════════════════════════════════════════════════════════════
# DELETE REQUESTS (approval workflow)
# ═══════════════════════════════════════════════════════════════════════

def create_delete_request(req: dict) -> dict:
    req.setdefault("id", str(uuid.uuid4()))
    req.setdefault("status", "pending")
    req.setdefault("acknowledged", False)
    req.setdefault("created_at", _now_iso())

    sb = _get_supabase()
    if sb:
        resp = sb.table("delete_requests").insert(req).execute()
        return resp.data[0]

    with _file_lock("delete_requests.json"):
        all_r = _read_json("delete_requests.json")
        all_r.append(req)
        _write_json("delete_requests.json", all_r)
    return req


def list_delete_requests(*, status: str | None = None) -> list[dict]:
    sb = _get_supabase()
    if sb:
        q = sb.table("delete_requests").select("*").order("created_at", desc=True)
        if status:
            q = q.eq("status", status)
        return q.execute().data

    all_r = _read_json("delete_requests.json")
    if status:
        all_r = [r for r in all_r if r.get("status") == status]
    return sorted(all_r, key=lambda r: r.get("created_at", ""), reverse=True)


def get_delete_request(req_id: str) -> dict | None:
    sb = _get_supabase()
    if sb:
        resp = sb.table("delete_requests").select("*").eq("id", req_id).execute()
        return resp.data[0] if resp.data else None

    for r in _read_json("delete_requests.json"):
        if r["id"] == req_id:
            return r
    return None


def update_delete_request(req_id: str, updates: dict) -> dict | None:
    sb = _get_supabase()
    if sb:
        resp = sb.table("delete_requests").update(updates).eq("id", req_id).execute()
        return resp.data[0] if resp.data else None

    with _file_lock("delete_requests.json"):
        all_r = _read_json("delete_requests.json")
        for r in all_r:
            if r["id"] == req_id:
                r.update(updates)
                _write_json("delete_requests.json", all_r)
                return r
    return None


# ═══════════════════════════════════════════════════════════════════════
# SETTINGS (notification config)
# ═══════════════════════════════════════════════════════════════════════

def get_settings() -> dict:
    sb = _get_supabase()
    if sb:
        resp = sb.table("settings").select("*").eq("id", "notify").execute()
        if resp.data:
            return resp.data[0].get("config", {})
        return {}

    data = _read_json("settings.json")
    return data[0] if data else {}


def save_settings(config: dict) -> None:
    sb = _get_supabase()
    if sb:
        sb.table("settings").upsert({"id": "notify", "config": config}).execute()
        return

    with _file_lock("settings.json"):
        _write_json("settings.json", [config])
