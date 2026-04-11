"""Run Tk GUI callback scenarios (window hidden). Same code paths as clicking in the app."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PRICE_MONITOR_SSL_INSECURE_RETRY", "1")

from price_monitor.gui_harness import run_gui_harness  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run_gui_harness())
