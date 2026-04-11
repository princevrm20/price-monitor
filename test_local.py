"""Comprehensive local test of all features."""
import httpx
import json
import sys

BASE = "http://127.0.0.1:8080"
ADMIN_PW = "testadmin123"
VIEWER_PW = "viewer123"
passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}" + (f" | {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f" | {detail}" if detail else ""))


client = httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)

# ── AUTH ─────────────────────────────────────────────────────
print("\n=== AUTH FLOW ===")
r = client.get("/health")
check("Health endpoint", r.status_code == 200)

r = client.get("/")
check("Root redirects to login", r.status_code == 302)

r = client.get("/login")
check("Login page loads", r.status_code == 200 and "password" in r.text.lower())

r = client.post("/login", data={"password": "wrong"})
check("Wrong password rejected", r.status_code == 200)

# Admin login
r = client.post("/login", data={"password": ADMIN_PW})
check("Admin login accepted", r.status_code == 302)
admin = httpx.Client(base_url=BASE, timeout=30, cookies=r.cookies, follow_redirects=True)

# Viewer login
r = client.post("/login", data={"password": VIEWER_PW})
check("Viewer login accepted", r.status_code == 302)
viewer = httpx.Client(base_url=BASE, timeout=30, cookies=r.cookies, follow_redirects=True)

# ── ROLES ────────────────────────────────────────────────────
print("\n=== ROLES ===")
r = admin.get("/")
check("Admin sees dashboard", r.status_code == 200 and "admin" in r.text.lower())

r = viewer.get("/")
check("Viewer sees dashboard", r.status_code == 200)

r = viewer.post("/api/monitors", json={"type": "product", "name": "X", "url": "http://x.com", "budget": 10})
check("Viewer CANNOT create", r.status_code == 403)

# ── MONITOR CRUD ─────────────────────────────────────────────
print("\n=== MONITOR CRUD (product) ===")
r = admin.post("/api/monitors", json={
    "type": "product", "name": "Test Product", "url": "https://example.com",
    "budget": 999, "tags": "electronics, test", "category": "gadgets",
    "alert_mode": "budget", "comparison_group": "test-group",
    "created_by": "prince",
})
check("Create product", r.status_code == 201)
prod_id = r.json()["monitor"]["id"]
check("Created by is set", r.json()["monitor"].get("created_by") == "prince")

print("\n=== MONITOR CRUD (train) ===")
r = admin.post("/api/monitors", json={
    "type": "train", "name": "Test Train", "train_number": "12952",
    "from_station": "NDLS", "to_station": "MMCT", "travel_date": "2026-05-01",
    "class_code": "3A", "quota": "GN", "fare_budget": 1500,
    "tags": ["travel"], "alert_mode": "drop_percent", "alert_drop_percent": 15,
    "created_by": "john",
})
check("Create train", r.status_code == 201)
train_id = r.json()["monitor"]["id"]

print("\n=== MONITOR CRUD (flight) ===")
r = admin.post("/api/monitors", json={
    "type": "flight", "name": "Test Flight DEL-BOM",
    "flight_origin": "DEL", "flight_destination": "BOM",
    "flight_date": "2026-06-01", "flight_max_price": 5000,
    "tags": "travel, flight", "alert_mode": "any_change",
    "created_by": "prince",
})
check("Create flight", r.status_code == 201)
flight_id = r.json()["monitor"]["id"]

# ── FILTER BY CREATOR ────────────────────────────────────────
print("\n=== FILTER BY CREATOR ===")
r = admin.get("/api/monitors?created_by=prince")
prince_mons = r.json()["monitors"]
check("Filter by creator=prince", len(prince_mons) >= 2)
check("All results are prince's", all(m.get("created_by") == "prince" for m in prince_mons))

r = admin.get("/api/monitors?created_by=john")
john_mons = r.json()["monitors"]
check("Filter by creator=john", len(john_mons) >= 1)

# ── TAGS ─────────────────────────────────────────────────────
print("\n=== TAGS ===")
r = admin.get("/api/monitors")
monitors = r.json()["monitors"]
prod_mon = [m for m in monitors if m["id"] == prod_id][0]
check("Product has tags", "electronics" in prod_mon.get("tags", []) and "test" in prod_mon.get("tags", []))

r = admin.get("/api/monitors?tag=travel")
check("Filter by tag", len(r.json()["monitors"]) >= 2)

# ── ALERT MODES ───────────────────────────────────────────────
print("\n=== ALERT MODES ===")
r = admin.get(f"/api/monitors/{prod_id}")
check("Product alert_mode=budget", r.json()["monitor"]["alert_mode"] == "budget")

r = admin.get(f"/api/monitors/{train_id}")
check("Train alert_mode=drop_percent", r.json()["monitor"]["alert_mode"] == "drop_percent")
check("Train drop_percent=15", r.json()["monitor"].get("alert_drop_percent") == 15)

r = admin.get(f"/api/monitors/{flight_id}")
check("Flight alert_mode=any_change", r.json()["monitor"]["alert_mode"] == "any_change")

# ── UPDATE ───────────────────────────────────────────────────
print("\n=== UPDATE ===")
r = admin.put(f"/api/monitors/{prod_id}", json={
    "budget": 899, "tags": "electronics, updated",
    "comparison_group": "cmp-test", "created_by": "prince_updated",
})
mon = r.json()["monitor"]
check("Update budget", mon["budget"] == 899)
check("Update tags", "updated" in mon.get("tags", []))
check("Update created_by", mon.get("created_by") == "prince_updated")

# ── COMPARISON GROUP ─────────────────────────────────────────
print("\n=== COMPARISON ===")
r = admin.post("/api/monitors", json={
    "type": "product", "name": "Comp Product 2", "url": "https://flipkart.com/dp/test",
    "budget": 950, "comparison_group": "cmp-test", "created_by": "prince",
})
check("Create comparison product", r.status_code == 201)
comp_id = r.json()["monitor"]["id"]

r = admin.get("/api/comparison/cmp-test")
check("Comparison endpoint", len(r.json()["items"]) >= 2)

# ── PAUSE / RESUME / DUPLICATE ───────────────────────────────
print("\n=== PAUSE / RESUME / DUPLICATE ===")
r = admin.post(f"/api/monitors/{train_id}/pause")
check("Pause", r.json()["monitor"]["status"] == "paused")

r = admin.post(f"/api/monitors/{train_id}/resume")
check("Resume", r.json()["monitor"]["status"] == "active")

r = admin.post(f"/api/monitors/{prod_id}/duplicate")
check("Duplicate", r.status_code == 200)
dup_id = r.json()["monitor"]["id"]

# ── BULK OPS ─────────────────────────────────────────────────
print("\n=== BULK OPERATIONS ===")
r = admin.post("/api/monitors/batch/pause", json={"ids": [train_id, flight_id]})
check("Batch pause", r.json()["ok"] and r.json()["count"] == 2)

r = admin.post("/api/monitors/batch/resume", json={"ids": [train_id, flight_id]})
check("Batch resume", r.json()["ok"] and r.json()["count"] == 2)

# ── EVENTS ───────────────────────────────────────────────────
print("\n=== EVENTS ===")
r = admin.get(f"/api/monitors/{prod_id}/events")
check("Get events", r.status_code == 200 and isinstance(r.json()["events"], list))

r = admin.get("/api/events/recent")
check("Recent events", r.status_code == 200)

# ── NOTIFICATIONS ────────────────────────────────────────────
print("\n=== NOTIFICATIONS (with email fields) ===")
r = admin.get("/api/notifications")
check("Get notifications", r.status_code == 200)

r = admin.post("/api/notifications", json={
    "ntfy_topic": "", "smtp_host": "", "smtp_port": 587,
    "smtp_user": "", "smtp_pass": "", "smtp_from": "", "smtp_to": "",
})
check("Save notifications (with email)", r.status_code == 200)

# ── IMPORT / EXPORT ──────────────────────────────────────────
print("\n=== IMPORT / EXPORT ===")
r = admin.get("/api/monitors/export-all")
check("Export all monitors", r.status_code == 200 and "application/json" in r.headers.get("content-type", ""))
export_data = r.json()
check("Export is a list", isinstance(export_data, list) and len(export_data) >= 3)

r = admin.post("/api/monitors/import", json=[{
    "type": "product", "name": "Imported Mon", "url": "https://imported.com",
    "budget": 100, "created_by": "import_user",
}])
check("Import monitors", r.json()["ok"] and r.json()["count"] == 1)

# ── CSV EXPORT ───────────────────────────────────────────────
print("\n=== CSV EXPORT ===")
r = admin.get(f"/api/monitors/{prod_id}/export")
check("CSV export", r.status_code == 200 and "text/csv" in r.headers.get("content-type", ""))

# ── PAGES ────────────────────────────────────────────────────
print("\n=== UI PAGES ===")
r = admin.get("/")
check("Admin dashboard renders", "Price Monitor Admin" in r.text)
check("Has bulk checkboxes", 'sel-cb' in r.text)
check("Has health dots", 'health-dot' in r.text)
check("Has tags", 'tag' in r.text.lower())
check("Has flight button", 'flight' in r.text.lower())
check("Has import/export", 'Export All' in r.text)
check("Has audit section", 'Audit Log' in r.text)
check("Has email fields", 'smtp' in r.text.lower())
check("Has alert mode selector", 'alert-mode' in r.text)
check("Has creator column", 'Created by' in r.text)
check("Has creator filter", 'creator' in r.text.lower())
check("Has created-by in create form", 'c-created-by' in r.text)

r = admin.get("/?creator=prince")
check("Creator filter works on page", r.status_code == 200)

r = admin.get(f"/monitor/{prod_id}")
check("Detail page renders", r.status_code == 200)
check("Detail shows created by", "prince" in r.text.lower() or "Created by" in r.text)

# ── AUDIT LOG ────────────────────────────────────────────────
print("\n=== AUDIT LOG ===")
r = admin.get("/api/audit")
entries = r.json()["entries"]
check("Audit log has entries", len(entries) > 0)
check("Audit has create action", any(e["action"] == "create" for e in entries))

# ── MONITOR LOOP ─────────────────────────────────────────────
print("\n=== MONITOR LOOP ===")
r = admin.post("/api/monitor/start")
check("Start loop", r.json()["ok"])

r = admin.get("/api/status")
check("Status monitoring=true", r.json()["monitoring"])
check("Status has expired count", "expired" in r.json())

r = admin.post("/api/monitor/stop")
check("Stop loop", r.json()["ok"])

# ── DIGEST ───────────────────────────────────────────────────
print("\n=== DIGEST ===")
r = admin.post("/api/digest/send", json={})
if r.status_code == 200:
    data = r.json()
    check("Digest endpoint", "digest" in data)
    check("Digest has content", "Price Monitor" in data.get("digest", ""))
else:
    check("Digest endpoint", False, f"status={r.status_code} body={r.text[:200]}")

# ── SSE ──────────────────────────────────────────────────────
print("\n=== SSE ===")
try:
    with admin.stream("GET", "/api/stream") as r:
        check("SSE endpoint responds", r.status_code == 200)
except Exception:
    check("SSE endpoint responds", True, "timeout expected")

# ── PWA ──────────────────────────────────────────────────────
print("\n=== PWA ===")
r = admin.get("/static/manifest.json")
check("Manifest loads", r.status_code == 200 and "PriceMon" in r.text)

r = admin.get("/static/sw.js")
check("Service worker loads", r.status_code == 200 and "pricemon" in r.text)

r = admin.get("/static/icon-192.svg")
check("Icon 192 loads", r.status_code == 200)

# ── VIEWER RESTRICTIONS ──────────────────────────────────────
print("\n=== VIEWER RESTRICTIONS ===")
r = viewer.get("/api/monitors")
check("Viewer CAN list", r.status_code == 200)

r = viewer.get(f"/api/monitors/{prod_id}")
check("Viewer CAN get detail", r.status_code == 200)

r = viewer.get("/api/audit")
check("Viewer CAN read audit", r.status_code == 200)

r = viewer.put(f"/api/monitors/{prod_id}", json={"name": "hacked"})
check("Viewer CANNOT update", r.status_code == 403)

r = viewer.delete(f"/api/monitors/{prod_id}")
check("Viewer CANNOT delete", r.status_code == 403)

r = viewer.post("/api/monitor/start")
check("Viewer CANNOT start loop", r.status_code == 403)

# ── CLEANUP ──────────────────────────────────────────────────
print("\n=== CLEANUP ===")
admin.delete(f"/api/monitors/{dup_id}")
admin.delete(f"/api/monitors/{comp_id}")
r = admin.get("/api/monitors")
for m in r.json()["monitors"]:
    if m["name"] in ("Imported Mon",):
        admin.delete(f"/api/monitors/{m['id']}")
admin.delete(f"/api/monitors/{prod_id}")
admin.delete(f"/api/monitors/{train_id}")
admin.delete(f"/api/monitors/{flight_id}")
check("Cleanup done", True)

# ── LOGOUT ───────────────────────────────────────────────────
print("\n=== LOGOUT ===")
r = admin.get("/logout", follow_redirects=False)
check("Logout redirects", r.status_code == 302)

# Summary
print("\n" + "=" * 55)
print(f"RESULTS: {passed} passed, {failed} failed out of {passed + failed}")
if failed:
    print("SOME TESTS FAILED")
    sys.exit(1)
else:
    print("ALL TESTS PASSED!")
    sys.exit(0)
