"""Database abstraction layer.

Uses Supabase (PostgreSQL) when SUPABASE_URL + SUPABASE_KEY are set,
otherwise falls back to local JSON files for development.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_supabase_client = None


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
    mon.setdefault("check_interval_min", 60)

    sb = _get_supabase()
    if sb:
        resp = sb.table("monitors").insert(mon).execute()
        return resp.data[0]

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
        return q.execute().data

    all_m = _read_json("monitors.json")
    if status:
        all_m = [m for m in all_m if m.get("status") == status]
    if monitor_type:
        all_m = [m for m in all_m if m.get("type") == monitor_type]
    if category:
        all_m = [m for m in all_m if m.get("category") == category]
    all_m.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return all_m


def update_monitor(monitor_id: str, updates: dict) -> dict | None:
    sb = _get_supabase()
    if sb:
        resp = sb.table("monitors").update(updates).eq("id", monitor_id).execute()
        return resp.data[0] if resp.data else None

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

    all_m = _read_json("monitors.json")
    before = len(all_m)
    all_m = [m for m in all_m if m["id"] != monitor_id]
    if len(all_m) == before:
        return False
    _write_json("monitors.json", all_m)
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

    _write_json("settings.json", [config])


# ═══════════════════════════════════════════════════════════════════════
# SUPABASE TABLE INIT (run once to create tables)
# ═══════════════════════════════════════════════════════════════════════

SUPABASE_SCHEMA_SQL = """
-- Run this in the Supabase SQL editor to create the required tables.

CREATE TABLE IF NOT EXISTS monitors (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL DEFAULT 'product',
    status TEXT NOT NULL DEFAULT 'active',
    name TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT NOT NULL DEFAULT 'admin',
    category TEXT NOT NULL DEFAULT '',
    check_interval_min INTEGER NOT NULL DEFAULT 60,
    last_checked_at TIMESTAMPTZ,
    next_check_at TIMESTAMPTZ,

    -- product fields
    url TEXT,
    budget DOUBLE PRECISION,
    price_selector TEXT,
    monitor_until TEXT,
    last_price DOUBLE PRECISION,

    -- train fields
    train_number TEXT,
    train_name TEXT,
    from_station TEXT,
    to_station TEXT,
    travel_date TEXT,
    class_code TEXT,
    quota TEXT,
    fare_budget DOUBLE PRECISION,
    last_fare DOUBLE PRECISION,
    last_availability TEXT,

    -- notification dedup
    notified_budget BOOLEAN NOT NULL DEFAULT FALSE,
    notified_available BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    monitor_id TEXT NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_monitor ON events(monitor_id, created_at DESC);

CREATE TABLE IF NOT EXISTS settings (
    id TEXT PRIMARY KEY,
    config JSONB NOT NULL DEFAULT '{}'
);
"""
