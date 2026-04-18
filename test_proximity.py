import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from price_monitor.monitor import fetch_html
from price_monitor.parser_price import extract_price, extract_original_price

tests = [
    ("Amazon - Armaf", "https://www.amazon.in/dp/B0G6MHP217"),
    ("Amazon - French Avenue", "https://www.amazon.in/dp/B0D8L92LQW"),
    ("Amazon - Lattafa Khamrah", "https://www.amazon.in/Lattafa-Khamrah-Perfume-Collection-Fragrance/dp/B0C1X8621W/"),
    ("PerfumePalace - Marwa", "https://perfumepalace.in/collections/perfumes/products/arabiyat-prestige-marwa-eau-de-parfum-100ml-for-man"),
    ("Ahmed Kaaf", "https://ahmedalmaghribi.co.in/products/kaaf"),
]

for name, url in tests:
    print(f"\n=== {name} ===")
    html = fetch_html(url)
    price = extract_price(html, None, url)
    mrp = extract_original_price(html, url, current_price=price)
    print(f"  Current: {price}")
    print(f"  MRP:     {mrp}")
    if price and mrp and mrp > price:
        print(f"  Off:     {((mrp - price) / mrp * 100):.1f}%")
