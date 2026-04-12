"""Try different approaches to fetch Myntra from a cloud server."""
import httpx, re, json, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = "https://price-monitor-77fp.onrender.com"
MYNTRA_URL = "https://www.myntra.com/casual-shoes/new-balance/new-balance-men-m1000-sneakers/31236902/buy"
PRODUCT_ID = "31236902"

with httpx.Client(follow_redirects=True, timeout=120) as c:
    c.post(f"{BASE}/login", data={"username": "admin", "password": "vrm20@9837"}, follow_redirects=False)

    # Try 1: Google webcache
    print("=== Try 1: Google Webcache ===")
    r = c.post(f"{BASE}/api/debug-fetch", json={
        "url": f"https://webcache.googleusercontent.com/search?q=cache:{MYNTRA_URL}"
    })
    j = r.json()
    print(f"  Length: {j.get('html_length')}, Price: {j.get('price')}, Title: {j.get('title','')[:80]}")

    # Try 2: Archive.org latest snapshot
    print("\n=== Try 2: Archive.org ===")
    r = c.post(f"{BASE}/api/debug-fetch", json={
        "url": f"https://web.archive.org/web/2/{MYNTRA_URL}"
    })
    j = r.json()
    print(f"  Length: {j.get('html_length')}, Price: {j.get('price')}, Title: {j.get('title','')[:80]}")

    # Try 3: Myntra's sitemap/SEO page (sometimes different)
    print("\n=== Try 3: Myntra mobile user-agent via debug ===")
    # This won't work through debug-fetch since it uses DEFAULT_HEADERS
    # But let's try the product page without /buy
    r = c.post(f"{BASE}/api/debug-fetch", json={
        "url": f"https://www.myntra.com/casual-shoes/new-balance/new-balance-men-m1000-sneakers/{PRODUCT_ID}"
    })
    j = r.json()
    print(f"  Length: {j.get('html_length')}, Price: {j.get('price')}, Title: {j.get('title','')[:80]}")

    # Try 4: Google Shopping / cached search
    print("\n=== Try 4: DuckDuckGo lite ===")
    r = c.post(f"{BASE}/api/debug-fetch", json={
        "url": f"https://lite.duckduckgo.com/lite/?q=site:myntra.com+{PRODUCT_ID}+price"
    })
    j = r.json()
    print(f"  Length: {j.get('html_length')}, Title: {j.get('title','')[:80]}")
    matches = j.get('price_matches', [])
    if matches:
        print(f"  Price matches: {matches}")
