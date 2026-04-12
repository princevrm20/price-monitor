"""Test baseline name detection on live deployment."""
import httpx, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = "https://price-monitor-77fp.onrender.com"

with httpx.Client(follow_redirects=True, timeout=180) as c:
    c.post(f"{BASE}/login", data={"username": "admin", "password": "vrm20@9837"}, follow_redirects=False)

    tests = [
        ("my birthday gift", "https://www.amazon.in/Odyssey-Homme-Perfume-Body-Spray/dp/B0G6MHP217/", False),
        ("random label 123", "https://www.myntra.com/casual-shoes/new+balance/new-balance-men-m1000-sneakers/36427666/buy", False),
        ("Nike shoes I want", "https://www.myntra.com/sports-shoes/nike/nike-men-downshifter-13-running-shoes/27234188/buy", True),
    ]

    for name, url, expect_issue in tests:
        label = "SHOULD FLAG" if expect_issue else "SHOULD STAY ACTIVE"
        print(f"\n=== {label}: \"{name}\" ===")
        r = c.post(f"{BASE}/api/monitors", json={
            "type": "product", "name": name, "url": url, "budget": "99999",
        })
        j = r.json()
        if not j.get("ok"):
            print(f"  CREATE ERROR: {j.get('error')}")
            continue
        mon_id = j["monitor"]["id"]

        # First check - sets baseline
        r = c.post(f"{BASE}/api/monitors/{mon_id}/check", timeout=180)
        if r.status_code != 200:
            print(f"  CHECK ERROR: {r.status_code} {r.text[:200]}")
            c.delete(f"{BASE}/api/monitors/{mon_id}")
            continue
        m = r.json().get("monitor", {})
        print(f"  Price:         {m.get('last_price')}")
        print(f"  Detected:      {m.get('detected_name','')[:60]}")
        print(f"  Baseline:      {m.get('baseline_name','')[:60] if m.get('baseline_name') else '(not set)'}")
        print(f"  Sold out:      {m.get('sold_out')}")
        print(f"  Status:        {m.get('status')}")

        ok = (m.get('status') == 'active') != expect_issue
        print(f"  RESULT:        {'PASS' if ok else 'FAIL'}")

        c.delete(f"{BASE}/api/monitors/{mon_id}")

    print("\nDone!")
