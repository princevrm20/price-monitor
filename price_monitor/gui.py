from __future__ import annotations

import threading
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from price_monitor.deadline import is_past_deadline
from price_monitor.monitor import check_all
from price_monitor.notifier import notify_price_alert
from price_monitor.notify_settings import (
    notify_config_path_for_items,
    notify_settings_from_stored_dict,
    read_notify_config_file,
    write_notify_config_file,
)
from price_monitor.storage import TrackedItem, default_data_path, load_items, save_items


def _required_label_row(parent: ttk.Frame, row: int, text: str, sticky: str = "w") -> None:
    wrap = ttk.Frame(parent)
    wrap.grid(row=row, column=0, sticky=sticky)
    ttk.Label(wrap, text=text).pack(side="left")
    tk.Label(wrap, text=" *", fg="#c62828", font=("TkDefaultFont", 10, "bold")).pack(side="left")


class PriceMonitorApp(tk.Tk):
    def __init__(self, items_path: Path | None = None) -> None:
        super().__init__()
        self.title("Price monitor")
        self.items_path = items_path or default_data_path()
        self._item_id: str | None = None
        self._monitoring = False
        self._after_id: str | None = None
        self._check_busy = False
        self._check_pulse_after: str | None = None

        screen_h = self.winfo_screenheight()
        win_h = min(screen_h - 80, 900)
        self.geometry(f"560x{win_h}")
        self.minsize(520, 480)

        self._canvas = tk.Canvas(self, highlightthickness=0)
        self._vscroll = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._vscroll.set)
        self._vscroll.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._container = ttk.Frame(self._canvas)
        self._canvas_win = self._canvas.create_window(
            (0, 0), window=self._container, anchor="nw"
        )
        self._container.bind("<Configure>", self._on_frame_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self.bind_all("<MouseWheel>", self._on_mousewheel)

        pad = {"padx": 10, "pady": 6}

        intro = ttk.Label(
            self._container,
            text=(
                "Track one product: paste the page link and your max price. You are notified when the price is at or "
                "below that amount. Flipkart, Myntra, and ixigo flight listings are usually parsed with no CSS selector. "
                "If a site blocks downloads (403), save the page as HTML and use a file:///… URL to that file."
            ),
            wraplength=480,
        )
        intro.pack(fill="x", **pad)

        legend = ttk.Frame(self._container)
        legend.pack(fill="x", padx=10, pady=(0, 2))
        tk.Label(legend, text="*", fg="#c62828", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        ttk.Label(legend, text="Required field").pack(side="left", padx=(2, 0))

        frm = ttk.Frame(self._container)
        frm.pack(fill="x", **pad)

        _required_label_row(frm, 0, "Product name")
        self.name_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.name_var, width=52).grid(row=0, column=1, sticky="ew")

        _required_label_row(frm, 1, "Product page URL")
        self.url_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.url_var, width=52).grid(row=1, column=1, sticky="ew")

        _required_label_row(frm, 2, "Budget (max price)")
        self.budget_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.budget_var, width=52).grid(row=2, column=1, sticky="ew")

        ttk.Label(frm, text="Price CSS selector (optional)").grid(row=3, column=0, sticky="nw")
        self.selector_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.selector_var, width=52).grid(row=3, column=1, sticky="ew")
        ttk.Label(
            frm,
            text=(
                "Optional: CSS selector for the price node. Amazon: leave blank for built-in selectors. "
                "Ixigo flight search pages: leave blank (lowest fare from embedded data). "
                "Otherwise try a selector from DevTools, or use a saved HTML file:// URL if the live page fails."
            ),
            font=("TkDefaultFont", 8),
        ).grid(row=4, column=1, sticky="w")

        ttk.Label(frm, text="Monitor until (optional)").grid(row=5, column=0, sticky="w")
        self.until_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.until_var, width=52).grid(row=5, column=1, sticky="ew")
        ttk.Label(
            frm,
            text="YYYY-MM-DD (end of that UTC day) or full ISO datetime. After this, checks stop.",
            font=("TkDefaultFont", 8),
        ).grid(row=6, column=1, sticky="w")

        frm.columnconfigure(1, weight=1)

        self._build_notify_frame(pad)

        row2 = ttk.Frame(self._container)
        row2.pack(fill="x", **pad)
        ttk.Label(row2, text="Auto-check every").pack(side="left")
        self.interval_var = tk.StringVar(value="60")
        ttk.Spinbox(row2, from_=1, to=1440, width=6, textvariable=self.interval_var).pack(side="left", padx=4)
        ttk.Label(row2, text="minutes").pack(side="left")

        user_fr = ttk.Frame(self._container)
        user_fr.pack(fill="x", **pad)
        ttk.Label(user_fr, text="Your name / ID").pack(side="left")
        tk.Label(user_fr, text=" *", fg="#c62828", font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self.user_var = tk.StringVar()
        ttk.Entry(user_fr, textvariable=self.user_var, width=30).pack(side="left", padx=(8, 0))
        ttk.Label(
            user_fr,
            text="(identifies who created this monitoring)",
            font=("TkDefaultFont", 8),
        ).pack(side="left", padx=(6, 0))

        btns = ttk.Frame(self._container)
        btns.pack(fill="x", **pad)
        ttk.Button(btns, text="Save product", command=self._on_save).pack(side="left", padx=(0, 8))
        self.check_btn = ttk.Button(btns, text="Check price now", command=self._on_check_now)
        self.check_btn.pack(side="left", padx=(0, 8))
        self.monitor_btn = ttk.Button(btns, text="Start monitoring", command=self._toggle_monitor)
        self.monitor_btn.pack(side="left", padx=(0, 8))

        status_fr = ttk.LabelFrame(self._container, text="Status")
        status_fr.pack(fill="both", expand=True, **pad)
        self.status_price = ttk.Label(status_fr, text="Last price: —")
        self.status_price.pack(anchor="w", padx=8, pady=(8, 2))
        self.status_time = ttk.Label(status_fr, text="Last check: —")
        self.status_time.pack(anchor="w", padx=8, pady=(0, 4))

        log_btns = ttk.Frame(status_fr)
        log_btns.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Button(log_btns, text="Clear output", command=self._clear_log).pack(side="right")

        self.log = scrolledtext.ScrolledText(status_fr, height=10, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self._load_user_id()
        self._load_form_from_disk()
        self._load_notify_from_file()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_frame_configure(self, _event: object = None) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self._canvas.itemconfigure(self._canvas_win, width=event.width)

    def _on_mousewheel(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _build_notify_frame(self, pad: dict[str, int | str]) -> None:
        nf = ttk.LabelFrame(self._container, text="Notifications (this PC or another device)")
        nf.pack(fill="x", **pad)
        hint = ttk.Label(
            nf,
            text=(
                "Alerts go to every channel you enable below. For a phone: install the ntfy app (ntfy.sh), then enter "
                "the same secret topic here and in the app. Optional: Telegram, WhatsApp (CallMeBot), or a webhook. "
                "Saves to notify.json next to your product file. If you set PRICE_MONITOR_* environment variables, "
                "those override the file at runtime."
            ),
            wraplength=480,
            font=("TkDefaultFont", 8),
        )
        hint.pack(anchor="w", padx=8, pady=(4, 2))

        g = ttk.Frame(nf)
        g.pack(fill="x", padx=8, pady=2)
        self.desktop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            g,
            text="Desktop notifications on this computer",
            variable=self.desktop_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=2)

        self.ntfy_topic_var = tk.StringVar()
        self.ntfy_server_var = tk.StringVar(value="https://ntfy.sh")
        self.telegram_token_var = tk.StringVar()
        self.telegram_chat_var = tk.StringVar()
        self.webhook_var = tk.StringVar()
        self.callme_phone_var = tk.StringVar()
        self.callme_key_var = tk.StringVar()

        r = 1
        ttk.Label(g, text="ntfy topic (secret)").grid(row=r, column=0, sticky="nw", pady=2)
        ttk.Entry(g, textvariable=self.ntfy_topic_var, width=50).grid(row=r, column=1, sticky="ew", pady=2)
        r += 1
        ttk.Label(g, text="ntfy server").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(g, textvariable=self.ntfy_server_var, width=50).grid(row=r, column=1, sticky="ew", pady=2)
        r += 1
        ttk.Label(g, text="Telegram bot token").grid(row=r, column=0, sticky="nw", pady=2)
        tk.Entry(g, textvariable=self.telegram_token_var, show="*", width=48).grid(row=r, column=1, sticky="ew", pady=2)
        r += 1
        ttk.Label(g, text="Telegram chat id").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(g, textvariable=self.telegram_chat_var, width=50).grid(row=r, column=1, sticky="ew", pady=2)
        r += 1
        ttk.Label(g, text="Webhook URL").grid(row=r, column=0, sticky="nw", pady=2)
        ttk.Entry(g, textvariable=self.webhook_var, width=50).grid(row=r, column=1, sticky="ew", pady=2)
        r += 1
        ttk.Label(g, text="CallMeBot phone (+country…)").grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(g, textvariable=self.callme_phone_var, width=50).grid(row=r, column=1, sticky="ew", pady=2)
        r += 1
        ttk.Label(g, text="CallMeBot API key").grid(row=r, column=0, sticky="nw", pady=2)
        tk.Entry(g, textvariable=self.callme_key_var, show="*", width=48).grid(row=r, column=1, sticky="ew", pady=2)
        g.columnconfigure(1, weight=1)

        bf = ttk.Frame(nf)
        bf.pack(fill="x", padx=8, pady=(4, 8))
        ttk.Button(bf, text="Save notification settings", command=self._on_save_notifications).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(bf, text="Test notification", command=self._on_test_notification).pack(side="left")

    def _notify_dict_from_form(self) -> dict[str, object]:
        return {
            "desktop": bool(self.desktop_var.get()),
            "ntfy_topic": self.ntfy_topic_var.get().strip(),
            "ntfy_server": self.ntfy_server_var.get().strip() or "https://ntfy.sh",
            "telegram_bot_token": self.telegram_token_var.get().strip(),
            "telegram_chat_id": self.telegram_chat_var.get().strip(),
            "webhook_url": self.webhook_var.get().strip(),
            "callmebot_phone": self.callme_phone_var.get().strip(),
            "callmebot_apikey": self.callme_key_var.get().strip(),
        }

    def _load_notify_from_file(self) -> None:
        raw = read_notify_config_file(self.items_path)
        if not raw:
            return
        if "desktop" in raw:
            self.desktop_var.set(bool(raw["desktop"]))
        if raw.get("ntfy_topic") is not None:
            self.ntfy_topic_var.set(str(raw.get("ntfy_topic") or ""))
        if raw.get("ntfy_server"):
            self.ntfy_server_var.set(str(raw.get("ntfy_server") or "https://ntfy.sh"))
        if raw.get("telegram_bot_token") is not None:
            self.telegram_token_var.set(str(raw.get("telegram_bot_token") or ""))
        if raw.get("telegram_chat_id") is not None:
            self.telegram_chat_var.set(str(raw.get("telegram_chat_id") or ""))
        if raw.get("webhook_url") is not None:
            self.webhook_var.set(str(raw.get("webhook_url") or ""))
        if raw.get("callmebot_phone") is not None:
            self.callme_phone_var.set(str(raw.get("callmebot_phone") or ""))
        if raw.get("callmebot_apikey") is not None:
            self.callme_key_var.set(str(raw.get("callmebot_apikey") or ""))

    def _on_save_notifications(self) -> None:
        data = self._notify_dict_from_form()
        write_notify_config_file(self.items_path, data)
        p = notify_config_path_for_items(self.items_path)
        self._log(f"Notification settings saved to {p}")

    def _on_test_notification(self) -> None:
        s = notify_settings_from_stored_dict(self._notify_dict_from_form())
        if not s.desktop and not s.any_remote():
            messagebox.showwarning(
                "No channel",
                "Turn on desktop notifications or fill at least one remote field (ntfy topic, Telegram, webhook, or CallMeBot).",
            )
            return
        ok = notify_price_alert(
            s,
            title="Price monitor test",
            message="If you received this on your other device, remote notifications are working.",
        )
        if ok:
            self._log("Test notification: at least one channel succeeded.")
            messagebox.showinfo("Test sent", "At least one notification channel succeeded. Check this PC and your other devices.")
        else:
            self._log("Test notification: no channel succeeded (see terminal for details).")
            messagebox.showerror(
                "Test failed",
                "No channel delivered. Check values, network, and any errors printed in the terminal.",
            )

    def _log(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _user_id_path(self) -> Path:
        return self.items_path.parent / "user_id.txt"

    def _load_user_id(self) -> None:
        p = self._user_id_path()
        if p.exists():
            self.user_var.set(p.read_text(encoding="utf-8").strip())

    def _save_user_id(self) -> None:
        user = self.user_var.get().strip()
        if user:
            p = self._user_id_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(user, encoding="utf-8")

    def _load_form_from_disk(self) -> None:
        items = load_items(self.items_path)
        if not items:
            self._item_id = None
            self._log("No saved product yet. Fill the form and click Save product.")
            return
        it = items[0]
        self._item_id = it.id
        self.name_var.set(it.name)
        self.url_var.set(it.url)
        self.budget_var.set(str(it.budget))
        self.selector_var.set(it.price_selector or "")
        self.until_var.set(it.monitor_until_iso or "")
        self._refresh_status_labels(it)
        if len(items) > 1:
            self._log(f"Note: {len(items)} items exist on disk; this window edits the first one. Save replaces all with this product.")

    def _refresh_status_labels(self, it: TrackedItem) -> None:
        if it.last_price is not None:
            self.status_price.configure(text=f"Last price: {it.last_price:g}  (budget {it.budget:g})")
        else:
            self.status_price.configure(text="Last price: —  (run a check)")
        if it.last_checked_iso:
            self.status_time.configure(text=f"Last check: {it.last_checked_iso}")
        else:
            self.status_time.configure(text="Last check: —")

    def _read_budget(self) -> float | None:
        raw = self.budget_var.get().strip().replace(",", ".")
        try:
            return float(raw)
        except ValueError:
            return None

    def _build_item_from_form(self, preserve: TrackedItem | None) -> TrackedItem | None:
        name = self.name_var.get().strip()
        url = self.url_var.get().strip()
        budget = self._read_budget()
        sel = self.selector_var.get().strip() or None
        until_raw = self.until_var.get().strip() or None
        if not name or not url:
            messagebox.showwarning("Missing info", "Please enter a product name and the product page URL.")
            return None
        if budget is None:
            messagebox.showerror("Budget", "Budget must be a number (for example 99.99).")
            return None
        item_id = preserve.id if preserve else self._item_id or str(uuid.uuid4())
        same = (
            preserve is not None
            and preserve.url == url
            and preserve.price_selector == sel
            and (preserve.monitor_until_iso or None) == (until_raw or None)
            and preserve.budget == budget
        )
        return TrackedItem(
            id=item_id,
            name=name,
            url=url,
            budget=budget,
            price_selector=sel,
            monitor_until_iso=until_raw,
            last_price=preserve.last_price if same else None,
            last_checked_iso=preserve.last_checked_iso if same else None,
            notified_at_budget=preserve.notified_at_budget if same else False,
            deadline_notified=preserve.deadline_notified if same else False,
            last_notify_delivered=preserve.last_notify_delivered if same else None,
        )

    def _on_save(self) -> None:
        existing = load_items(self.items_path)
        preserve = existing[0] if existing else None
        if preserve and preserve.id != self._item_id:
            preserve = None
        item = self._build_item_from_form(preserve)
        if item is None:
            return
        self._item_id = item.id
        save_items([item], self.items_path)
        self._save_user_id()
        self._log("Saved. This app tracks this one product only (replaces any previous list).")

    def _set_check_controls_busy(self, busy: bool) -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        try:
            self.check_btn.configure(state=state)
        except tk.TclError:
            pass

    def _end_check_busy(self) -> None:
        self._check_busy = False
        self._set_check_controls_busy(False)
        if self._check_pulse_after:
            try:
                self.after_cancel(self._check_pulse_after)
            except tk.TclError:
                pass
            self._check_pulse_after = None

    def _check_still_running_hint(self) -> None:
        self._check_pulse_after = None
        if not self._check_busy:
            return
        self._log(
            "Still checking… large Amazon pages can take 30–60s the first time. "
            "If it keeps failing, shorten the URL to https://www.amazon.in/dp/YOURASIN and add a price CSS selector."
        )

    def _on_check_now(self) -> None:
        if self._check_busy:
            return
        existing = load_items(self.items_path)
        preserve = existing[0] if existing else None
        item = self._build_item_from_form(preserve)
        if item is None:
            return
        save_items([item], self.items_path)
        self._item_id = item.id

        self._check_busy = True
        self._set_check_controls_busy(True)
        self._log(
            "Checking… (network + page parse; we stop waiting after 75s and show a timeout message.)"
        )
        self._check_pulse_after = self.after(12_000, self._check_still_running_hint)

        def work_outer() -> None:
            holder: list[tuple[str, object]] = []

            def inner() -> None:
                try:
                    fresh = check_all([item], items_path=self.items_path, propagate=True)
                    save_items(fresh, self.items_path)
                    holder.append(("ok", fresh[0]))
                except Exception as e:  # noqa: BLE001
                    holder.append(("err", e))

            t = threading.Thread(target=inner, daemon=True)
            t.start()
            t.join(75.0)

            def finish() -> None:
                self._end_check_busy()
                if t.is_alive():
                    self._after_check_timeout(item)
                    return
                if not holder:
                    self._after_check_done(item, RuntimeError("Check finished without a result."))
                    return
                kind, payload = holder[0]
                if kind == "ok":
                    self._after_check_done(payload, None)  # type: ignore[arg-type]
                else:
                    self._after_check_done(item, payload)  # type: ignore[arg-type]

            self.after(0, finish)

        threading.Thread(target=work_outer, daemon=True).start()

    def _after_check_timeout(self, item: TrackedItem) -> None:
        msg = (
            "The price check took longer than 75 seconds and was cancelled in the UI.\n\n"
            "Amazon pages are very large; try:\n"
            "• Short URL: https://www.amazon.in/dp/B0DH83Y8RK\n"
            "• Price CSS selector, e.g. .a-price.aok-align-center .a-offscreen (India) or similar on your page."
        )
        self._log("Check timed out (75s). See the dialog for tips.")
        messagebox.showwarning("Check timed out", msg)

    def _after_check_done(self, it: TrackedItem, err: Exception | None) -> None:
        if err:
            self._log(f"Check failed: {err}")
            messagebox.showerror("Check failed", str(err))
            return
        self._refresh_status_labels(it)
        if it.monitor_until_iso and is_past_deadline(it.monitor_until_iso):
            self._log("Monitor-until deadline has passed. Stopping automatic checks.")
            if self._monitoring:
                self._stop_monitor()
            return
        if it.last_price is None:
            self._log(
                "Check finished but no price was found. Try: clear the CSS selector for Amazon / ixigo flights / "
                "many shop JSON-LD pages; use a short Amazon /dp/ASIN link; confirm the page is not sign-in or CAPTCHA. "
                "If the server returns 403, save the page as HTML and put a file:///… path in the URL field. "
                "If TLS errors appear, set PRICE_MONITOR_HTTP_VERIFY=0 (less secure)."
            )
        else:
            rel = "at or below budget" if it.last_price <= it.budget else "above budget"
            self._log(f"Check finished: {it.last_price:g} ({rel}).")
            if it.last_price <= it.budget:
                if it.last_notify_delivered is True:
                    self._log(
                        "Notification: at least one channel succeeded (Windows toast and/or data/notify.json)."
                    )
                elif it.last_notify_delivered is False:
                    self._log(
                        "Notification: no channel succeeded. Set desktop:true in data/notify.json or add ntfy/Telegram."
                    )
                elif it.notified_at_budget:
                    self._log(
                        "Notification: skipped (already alerted for this budget while price stayed at/below it). "
                        "Press Start monitoring to reset alert state, or raise the budget."
                    )

    def _toggle_monitor(self) -> None:
        if self._monitoring:
            self._stop_monitor()
        else:
            self._start_monitor()

    def _start_monitor(self) -> None:
        user = self.user_var.get().strip()
        if not user:
            messagebox.showwarning("Missing info", "Please enter your name or ID before starting monitoring.")
            return

        existing = load_items(self.items_path)
        preserve = existing[0] if existing else None
        item = self._build_item_from_form(preserve)
        if item is None:
            return
        item.notified_at_budget = False
        item.last_notify_delivered = None
        save_items([item], self.items_path)
        self._item_id = item.id
        try:
            mins = int(self.interval_var.get())
        except ValueError:
            mins = 60
        mins = max(1, min(1440, mins))
        self.interval_var.set(str(mins))

        self._save_user_id()
        self._clear_log()
        self._monitoring = True
        self.monitor_btn.configure(text="Stop monitoring")
        self._log(f"--- New monitoring session ---")
        self._log(f"User: {user}")
        self._log(f"Product: {item.name}")
        self._log(f"URL: {item.url}")
        self._log(f"Budget: {item.budget:g}")
        if item.price_selector:
            self._log(f"Selector: {item.price_selector}")
        if item.monitor_until_iso:
            self._log(f"Monitor until: {item.monitor_until_iso}")
        self._log(
            f"Checking every {mins} minutes. "
            "Alert state was reset so you can get one budget notification again."
        )
        self._run_monitor_once_then_reschedule()

    def _stop_monitor(self) -> None:
        self._monitoring = False
        if self._after_id:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        self.monitor_btn.configure(text="Start monitoring")
        self._log("Monitoring stopped.")

    def _monitor_delay_ms(self) -> int:
        try:
            mins = int(self.interval_var.get())
        except ValueError:
            mins = 60
        mins = max(1, min(1440, mins))
        self.interval_var.set(str(mins))
        return mins * 60 * 1000

    def _run_monitor_once_then_reschedule(self) -> None:
        if not self._monitoring:
            return
        delay_ms = self._monitor_delay_ms()

        def work() -> None:
            items = load_items(self.items_path)
            if not items:
                self.after(0, self._monitor_stopped_no_items)
                return
            try:
                fresh = check_all(items, items_path=self.items_path)
                save_items(fresh, self.items_path)
                it = fresh[0]
                self.after(0, lambda: self._monitor_after_success(it, delay_ms))
            except Exception as e:  # noqa: BLE001
                self.after(0, lambda: self._monitor_after_error(e, delay_ms))

        threading.Thread(target=work, daemon=True).start()

    def _monitor_after_success(self, it: TrackedItem, delay_ms: int) -> None:
        if not self._monitoring:
            return
        self._refresh_status_labels(it)
        if it.monitor_until_iso and is_past_deadline(it.monitor_until_iso):
            self._log("Monitor-until deadline has passed. Stopping automatic checks.")
            self._stop_monitor()
            return
        if (
            it.last_price is not None
            and it.last_price <= it.budget
            and it.notified_at_budget
        ):
            if it.last_notify_delivered is True:
                self._log(
                    "Price is at or below your budget and notifications were sent. "
                    "Stopping automatic checks (press Start monitoring to watch again)."
                )
            elif it.last_notify_delivered is False:
                self._log(
                    "Price is at or below budget, but notifications failed or no channel is configured "
                    "(see data/notify.json). Stopping automatic checks."
                )
            else:
                self._log(
                    "Price is at or below budget (already satisfied). Stopping automatic checks."
                )
            self._stop_monitor()
            return
        if it.last_price is not None:
            self._log(f"Scheduled check: {it.last_price:g} (budget {it.budget:g}).")
        else:
            self._log("Scheduled check: could not read a price.")
        self._after_id = self.after(delay_ms, self._run_monitor_once_then_reschedule)

    def _monitor_after_error(self, err: Exception, delay_ms: int) -> None:
        if not self._monitoring:
            return
        self._log(f"Check error: {err}")
        self._after_id = self.after(delay_ms, self._run_monitor_once_then_reschedule)

    def _monitor_stopped_no_items(self) -> None:
        self._log("No item to check; save a product first.")
        self._stop_monitor()

    def _on_close(self) -> None:
        self._stop_monitor()
        self.destroy()


def run_gui(items_path: Path | None = None) -> int:
    app = PriceMonitorApp(items_path=items_path)
    app.mainloop()
    return 0
