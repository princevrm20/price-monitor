"""Optional: real Tk; skips when display / Tcl is unavailable."""
from __future__ import annotations

import pytest


def _tk_available() -> bool:
    try:
        import tkinter as tk

        r = tk.Tk()
        r.withdraw()
        r.update_idletasks()
        r.destroy()
        return True
    except tk.TclError:
        return False


@pytest.mark.skipif(not _tk_available(), reason="Tk display not available")
def test_run_gui_harness():
    from price_monitor.gui_harness import run_gui_harness

    assert run_gui_harness(verbose=False) == 0
