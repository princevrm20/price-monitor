import re

import httpx

html = httpx.get(
    "https://www.amazon.in/dp/B0DH83Y8RK",
    headers={"User-Agent": "Mozilla/5.0 Chrome/122"},
    timeout=60,
    follow_redirects=True,
    verify=False,
).text
patterns = [
    r'"priceToPay"\s*:\s*\{\s*"amount"\s*:\s*([\d.]+)',
    r'"priceToPay"\s*:\s*\{[^{]*"amount"\s*:\s*([\d.]+)',
]
for p in patterns:
    m = re.search(p, html)
    print(p, "->", m.group(1) if m else None)
