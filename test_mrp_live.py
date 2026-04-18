import httpx, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = "https://price-monitor-77fp.onrender.com"

with httpx.Client(follow_redirects=True, timeout=180) as c:
    c.post(f"{BASE}/login", data={"username": "admin", "password": "vrm20@9837"})

    r = c.get(f"{BASE}/api/monitors")
    data = r.json()
    monitors = data if isinstance(data, list) else data.get("monitors", [])

    targets = ["arabiyat prestige marwa", "armaf odyssey homme", "french avenue brun", "lattafa khamrah"]
    for mon in monitors:
        if not isinstance(mon, dict):
            continue
        if mon.get("name", "").lower() in targets:
            mid = mon["id"]
            name = mon["name"]
            print(f"\n=== {name} ===")
            r = c.post(f"{BASE}/api/monitors/{mid}/check", timeout=180)
            if r.status_code == 200:
                m = r.json().get("monitor", {})
                print(f"  Current price:  {m.get('last_price')}")
                print(f"  Original/MRP:   {m.get('original_price')}")
                print(f"  Highest seen:   {m.get('highest_price')}")
                if m.get('original_price') and m.get('last_price'):
                    drop = ((m['original_price'] - m['last_price']) / m['original_price']) * 100
                    print(f"  Drop from MRP:  {drop:.1f}%")
                elif m.get('last_price'):
                    print(f"  Drop from MRP:  N/A (no MRP found on page)")
            else:
                print(f"  ERROR: {r.status_code} - {r.text[:200]}")

    print("\nDone!")
