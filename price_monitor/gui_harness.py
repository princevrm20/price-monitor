"""Headless-style runs of the real Tk GUI (Save / Check / Monitor callbacks).

Patches ``tkinter.messagebox`` so validation dialogs do not block.

The app schedules ``after()`` from a worker thread after ``check_all``; that works
when the main thread is inside ``mainloop()`` (not only ``update()``), so this
harness uses ``mainloop`` + ``after``-based waits.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import price_monitor.gui as gui_module
from price_monitor.gui import PriceMonitorApp


def _patch_messageboxes() -> None:
    gui_module.messagebox.showwarning = lambda *a, **k: None  # type: ignore[method-assign]
    gui_module.messagebox.showerror = lambda *a, **k: None  # type: ignore[method-assign]
    gui_module.messagebox.showinfo = lambda *a, **k: None  # type: ignore[method-assign]


def _log_text(app: PriceMonitorApp) -> str:
    return app.log.get("1.0", "end")


class _QuickMonitor(PriceMonitorApp):
    """Short delay between monitor cycles so the harness can observe scheduling."""

    def _monitor_delay_ms(self) -> int:  # noqa: D102
        return 200


def run_gui_harness(*, fixtures_dir: Path | None = None, verbose: bool = True) -> int:
    """
    Run several GUI flows against a temp ``items.json`` using one Tk root and ``mainloop``.

    Returns 0 if all scenarios pass, 1 otherwise.
    """
    _patch_messageboxes()

    root_pkg = Path(__file__).resolve().parent.parent
    fx = fixtures_dir or (root_pkg / "tests" / "fixtures")
    ajio = fx / "ajio_product_sample.html"
    if not ajio.is_file():
        if verbose:
            print("SKIP: missing fixture", ajio)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="pm_gui_"))
    items_path = tmp / "items.json"
    failures: list[str] = []

    app = _QuickMonitor(items_path=items_path)
    app.withdraw()

    def fail(msg: str) -> None:
        failures.append(msg)
        if verbose:
            print("FAIL:", msg)
        try:
            app._stop_monitor()
        except Exception:
            pass
        app.quit()

    def finish_ok() -> None:
        if verbose:
            print("GUI_HARNESS_ALL_OK")
        try:
            app._stop_monitor()
        except Exception:
            pass
        app.quit()

    def wait_idle(
        pred,
        on_ok,
        *,
        timeout_s: float = 90.0,
        label: str = "wait",
    ) -> None:
        deadline = time.monotonic() + timeout_s

        def tick() -> None:
            if pred():
                on_ok()
                return
            if time.monotonic() > deadline:
                fail(f"{label}: timeout")
                return
            app.after(40, tick)

        app.after(0, tick)

    def main_sequence() -> None:
        if verbose:
            print("--- invalid_save_ignored ---")
        try:
            app.name_var.set("")
            app.url_var.set("https://example.com")
            app.budget_var.set("10")
            app._on_save()
            assert "Saved. This app tracks" not in _log_text(app)
            if verbose:
                print("  OK")
        except Exception as e:  # noqa: BLE001
            fail(f"invalid_save: {e!r}")
            return

        if verbose:
            print("--- valid_save_writes_disk ---")
        try:
            app.name_var.set("Demo")
            app.url_var.set("https://example.com/p")
            app.budget_var.set("99")
            app._on_save()
            assert "Saved. This app tracks" in _log_text(app)
            if verbose:
                print("  OK")
        except Exception as e:  # noqa: BLE001
            fail(f"valid_save: {e!r}")
            return

        if verbose:
            print("--- file_uri_check_price ---")

        def after_file_check() -> None:
            try:
                assert "1299" in _log_text(app), _log_text(app)[-800:]
                if verbose:
                    print("  OK")
            except Exception as e:  # noqa: BLE001
                fail(f"file_uri: {e!r}")
                return

            if verbose:
                print("--- css_selector_check ---")
            snap2 = tmp / "sel.html"
            snap2.write_text(
                '<!DOCTYPE html><html><body><div class="deal">₹2468</div></body></html>',
                encoding="utf-8",
            )
            app.name_var.set("Selector item")
            app.url_var.set(snap2.as_uri())
            app.budget_var.set("3000")
            app.selector_var.set(".deal")
            app._on_save()
            app._on_check_now()

            def after_sel_check() -> None:
                try:
                    assert "2468" in _log_text(app), _log_text(app)[-800:]
                    if verbose:
                        print("  OK")
                except Exception as e:  # noqa: BLE001
                    fail(f"selector: {e!r}")
                    return

                if verbose:
                    print("--- monitor_then_schedule ---")
                snap3 = tmp / "mon.html"
                shutil.copy(ajio, snap3)
                app.name_var.set("Monitor test")
                app.url_var.set(snap3.as_uri())
                app.budget_var.set("1")
                app.selector_var.set("")
                app.interval_var.set("60")
                app._on_save()
                app._start_monitor()
                start_poll = time.monotonic()

                def poll_monitor() -> None:
                    if "Scheduled check" in _log_text(app):
                        if verbose:
                            print("  OK")
                        app._stop_monitor()
                        finish_ok()
                        return
                    if time.monotonic() > start_poll + 25.0:
                        fail("monitor: timeout " + _log_text(app)[-400:])
                        return
                    app.after(40, poll_monitor)

                app.after(40, poll_monitor)

            wait_idle(lambda: not app._check_busy, after_sel_check, timeout_s=30.0, label="selector check")

        snap = tmp / "snap.html"
        shutil.copy(ajio, snap)
        app.name_var.set("Ajio snapshot")
        app.url_var.set(snap.as_uri())
        app.budget_var.set("5000")
        app.selector_var.set("")
        app._on_save()
        app._on_check_now()
        wait_idle(lambda: not app._check_busy, after_file_check, timeout_s=90.0, label="file check")

    try:
        app.after(0, main_sequence)
        app.mainloop()
    finally:
        try:
            app.destroy()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print("FAILED:", f)
        return 1
    return 0
