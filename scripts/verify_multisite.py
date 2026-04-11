"""CLI entry: same as `python -m price_monitor verify-multisite` (see price_monitor.verify_multisite)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PRICE_MONITOR_SSL_INSECURE_RETRY", "1")

from price_monitor.verify_multisite import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
