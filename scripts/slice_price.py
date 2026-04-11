import httpx

html = httpx.get(
    "https://www.amazon.in/dp/B0DH83Y8RK",
    headers={"User-Agent": "Mozilla/5.0 Chrome/122"},
    timeout=60,
    follow_redirects=True,
    verify=False,
).text
i = html.find("priceToPay")
from pathlib import Path

Path("scripts/slice_out.txt").write_text(html[i : i + 800], encoding="utf-8")
print("idx", i, "wrote scripts/slice_out.txt")
