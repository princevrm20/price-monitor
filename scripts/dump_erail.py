import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PRICE_MONITOR_SSL_INSECURE_RETRY", "1")

from price_monitor.monitor import fetch_html  # noqa: E402

h = fetch_html("https://erail.in/trains-between-stations/jammu-tawi-JT/kanpur-central-CNB")
Path("scripts/erail_snip.html").write_text(h[:12000], encoding="utf-8")
print("wrote scripts/erail_snip.html", len(h))
