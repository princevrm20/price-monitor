from pathlib import Path

import httpx
from bs4 import BeautifulSoup

u = "https://www.amazon.in/dp/B0DH83Y8RK"
h = {"User-Agent": "Mozilla/5.0 Chrome/122", "Accept-Language": "en-IN,en;q=0.9"}
html = httpx.get(u, headers=h, timeout=60, follow_redirects=True, verify=False).text
out = []
out.append(f"len={len(html)}")
for needle in (
    b"priceToPay",
    b"apexPriceToPay",
    b"buyingPrice",
    b"displayAmount",
    b"a-color-price",
):
    out.append(f"{needle!r} idx={html.encode('utf-8', errors='ignore').find(needle)}")

soup = BeautifulSoup(html, "lxml")
sels = [
    ".a-price.aok-align-center .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
    "#apex_desktop .a-price .a-offscreen",
    ".reinventPricePriceToPayMargin .a-offscreen",
    "#corePrice_feature_div .a-price .a-offscreen",
    "#ppd .a-offscreen",
    "span.a-price.a-text-price .a-offscreen",
    "#mobile-price .a-offscreen",
    "#olp-upd-new-used-fsx-price .a-color-price",
]
for s in sels:
    n = soup.select_one(s)
    t = n.get_text(" ", strip=True) if n else ""
    out.append(f"{s} -> ascii={t.encode('ascii', errors='replace')!r}")

Path("scripts/probe_out.txt").write_text("\n".join(out), encoding="utf-8")
print("wrote scripts/probe_out.txt")
