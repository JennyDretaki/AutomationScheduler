# -*- coding: utf-8 -*-
"""Καταχώρηση παραβόλων στη CEPAL.

Τρέξιμο:
    py -3 main.py
    py -3 main.py --scheduled --job-id N   # από C# scheduler· γράφει status μόνο του
"""

from __future__ import annotations

import json
import os
import queue
import threading
from pathlib import Path

# Πριν tkinter/playwright — C# scheduler καλεί main.py απευθείας (χωρίς start.bat).
HERE = Path(__file__).resolve().parent
_PYTHON_ROOT = HERE.parent
_BROWSERS = _PYTHON_ROOT / "ms-playwright"
_TCL = _PYTHON_ROOT / "runtime" / "tcl" / "tcl8.6"
_TK = _PYTHON_ROOT / "runtime" / "tcl" / "tk8.6"
if _BROWSERS.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_BROWSERS))
if _TCL.is_dir():
    os.environ.setdefault("TCL_LIBRARY", str(_TCL))
if _TK.is_dir():
    os.environ.setdefault("TK_LIBRARY", str(_TK))

import tkinter as tk
from tkinter import messagebox, ttk

from sql_source import (
    claim_scheduler_job,
    connect,
    mark_scheduler_failed,
    mark_scheduler_succeed,
)
from uploader import Uploader

CONFIG_PATH = HERE / "config.json"


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class SettingsDialog(tk.Toplevel):
    def __init__(self, master, config: dict, on_save):
        super().__init__(master)
        self.title("Ρυθμίσεις")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.config_data = config
        self.on_save = on_save

        frame = ttk.Frame(self, padding=12)
        frame.grid(row=0, column=0)

        self.url = self._row(frame, 0, "Διεύθυνση σύνδεσης", config["login_url"], 64)
        self.email = self._row(frame, 1, "Email", config["email"], 42)
        self.password = self._row(frame, 2, "Password", config.get("password", ""), 42, secret=True)

        mqtt = config["mqtt"]
        self.mqtt_on = tk.BooleanVar(value=bool(mqtt.get("enabled", True)))
        ttk.Checkbutton(frame, text="Αποστολή MQTT", variable=self.mqtt_on).grid(
            row=3, column=1, sticky="w", padx=12, pady=4
        )
        self.host = self._row(frame, 4, "MQTT host", str(mqtt["host"]), 24)
        self.port = self._row(frame, 5, "MQTT port", str(mqtt["port"]), 8)
        self.topic_total = self._row(frame, 6, "Topic έναρξης", mqtt["topic_total"], 32)
        self.topic_uploaded = self._row(frame, 7, "Topic προόδου", mqtt["topic_uploaded"], 32)

        buttons = ttk.Frame(frame)
        buttons.grid(row=8, column=1, sticky="e", pady=(12, 4))
        ttk.Button(buttons, text="Ακύρωση", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Αποθήκευση", command=self._save).pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda _event: self.destroy())

    def _row(self, frame, row: int, label: str, value: str, width: int, secret: bool = False) -> tk.StringVar:
        variable = tk.StringVar(value=value)
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=12, pady=4)
        entry = ttk.Entry(frame, textvariable=variable, width=width)
        if secret:
            entry.configure(show="*")
        entry.grid(row=row, column=1, sticky="ew", padx=12, pady=4)
        return variable

    def _save(self) -> None:
        config = self.config_data
        config["login_url"] = self.url.get().strip()
        config["email"] = self.email.get().strip()
        config["password"] = self.password.get()
        config["mqtt"]["enabled"] = self.mqtt_on.get()
        config["mqtt"]["host"] = self.host.get().strip()
        try:
            config["mqtt"]["port"] = int(self.port.get().strip())
        except ValueError:
            messagebox.showerror("Ρυθμίσεις", "Η θύρα MQTT πρέπει να είναι αριθμός.", parent=self)
            return
        config["mqtt"]["topic_total"] = self.topic_total.get().strip()
        config["mqtt"]["topic_uploaded"] = self.topic_uploaded.get().strip()
        save_config(config)
        self.on_save()
        self.destroy()


class App(tk.Tk):
    def __init__(self, scheduled: bool = False, job_id: int | None = None):
        super().__init__()
        self.scheduled = scheduled
        self.requested_job_id = job_id
        self.exit_code = 0
        self.title("Καταχώρηση CEPAL")
        self.geometry("760x560")
        self.minsize(680, 480)
        self.config_data = load_config()
        self.ui_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.running = False
        self.scheduler_job_id: int | None = None

        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self, padding=(16, 14, 16, 4))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="Καταχώρηση CEPAL", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            header,
            text="Παίρνει από τη βάση όσα παράβολα δεν έχουν ανέβει. Αν ένα δεν καταχωρηθεί, κρατάει το RF και τον λόγο και συνεχίζει.",
            font=("Segoe UI", 10),
            wraplength=700,
        ).pack(anchor="w", pady=(2, 0))

        actions = ttk.Frame(self, padding=(16, 8, 16, 4))
        actions.grid(row=1, column=0, sticky="ew")
        self.start_button = ttk.Button(actions, text="Έναρξη", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="Διακοπή", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="Ρυθμίσεις", command=self.open_settings).pack(side="left")
        self.progress = tk.StringVar(value="Έτοιμο")
        ttk.Label(actions, textvariable=self.progress).pack(side="right")

        log_frame = ttk.Frame(self, padding=(16, 4, 16, 12))
        log_frame.grid(row=2, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=16, wrap="word", font=("Consolas", 10), state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self.poll)
        if self.scheduled:
            self.after(200, self.start)

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.set("Σύνδεση στη βάση...")
        self.append(f"Σύνδεση στον {self.config_data['sql']['server']}.")

        def work() -> None:
            connection = None
            job_id = None
            try:
                connection = connect(self.config_data)
                server = self.config_data["sql"]["server"]
                self.ui_queue.put(("log", f"Συνδεδεμένη στον {server}."))
                if self.scheduled:
                    job_id = claim_scheduler_job(connection, self.requested_job_id)
                    self.scheduler_job_id = job_id
                    self.ui_queue.put(("log", f"Waiting → running · εργασία {job_id}"))
                uploader = Uploader(
                    self.config_data,
                    lambda line: self.ui_queue.put(("log", line)),
                    lambda index, total: self.ui_queue.put(("progress", index, total)),
                    lambda: self.ui_queue.put(("empty",)),
                    self.stop_event.is_set,
                )
                uploaded, failed, error = uploader.run(connection)
                self.ui_queue.put(("done", uploaded, uploader.total, failed, error, job_id))
            except Exception as error:
                self.ui_queue.put(("done", 0, 0, 0, str(error), job_id))
            finally:
                if connection is not None:
                    connection.close()

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.progress.set("Γίνεται διακοπή...")
        self.stop_button.configure(state="disabled")

    def poll(self) -> None:
        while True:
            try:
                message = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            kind = message[0]
            if kind == "log":
                self.append(message[1])
            elif kind == "progress":
                self.progress.set(f"{message[1]} από {message[2]}")
            elif kind == "empty":
                if not self.scheduled:
                    self.show_empty()
            elif kind == "done":
                self.finish(*message[1:])
        self.after(100, self.poll)

    def show_empty(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("ΠΡΟΣΟΧΗ")
        dialog.resizable(False, False)
        dialog.attributes("-topmost", True)
        dialog.transient(self)
        ttk.Label(
            dialog,
            text="Δεν υπάρχει κάτι για καταχώρηση στη CEPAL",
            font=("Segoe UI", 12),
        ).pack(padx=28, pady=24)
        dialog.update_idletasks()
        width = max(dialog.winfo_width(), 460)
        height = dialog.winfo_height()
        x = (dialog.winfo_screenwidth() - width) // 2
        y = (dialog.winfo_screenheight() - height) // 3
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.after(int(self.config_data.get("empty_message_ms", 3000)), dialog.destroy)

    def finish(
        self,
        uploaded: int,
        total: int,
        failed: int,
        error: str | None,
        job_id: int | None = None,
    ) -> None:
        """Κλείνει το UI και το scheduler status.

        Συμβόλαιο (--scheduled):
        - κρίσιμο error / διακοπή με error → failed
        - άδεια λίστα ή ολοκλήρωση (έστω με failed επιμέρους RF) → succeed
        """
        self.running = False
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        job_id = job_id if job_id is not None else self.scheduler_job_id

        if error:
            self.append(error)
            self.progress.set(error.splitlines()[0][:160])
            self._close_scheduler(job_id, success=False)
            if self.scheduled:
                self._finish_scheduled(1)
            return
        if total == 0:
            self.progress.set("Δεν υπάρχει κάτι για καταχώρηση")
            self._close_scheduler(job_id, success=True)
            if self.scheduled:
                self._finish_scheduled(0)
            return
        self.progress.set(f"Καταχωρήθηκαν {uploaded} από {total}. Δεν ανέβηκαν {failed}.")
        # Μερική επιτυχία (failed > 0) → succeed, όπως συμφωνήθηκε.
        self._close_scheduler(job_id, success=True)
        if self.scheduled:
            self._finish_scheduled(0)

    def _close_scheduler(self, job_id: int | None, *, success: bool) -> None:
        if job_id is None:
            return
        connection = None
        try:
            connection = connect(self.config_data)
            if success:
                mark_scheduler_succeed(connection, job_id)
                self.append(f"Εργασία {job_id}: status → succeed")
            else:
                mark_scheduler_failed(connection, job_id)
                self.append(f"Εργασία {job_id}: status → failed")
        except Exception as failure:
            self.append(str(failure))
        finally:
            if connection is not None:
                connection.close()
            self.scheduler_job_id = None

    def _finish_scheduled(self, code: int) -> None:
        self.exit_code = code
        self.after(400, self.destroy)

    def append(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def open_settings(self) -> None:
        if self.running:
            self.progress.set("Οι ρυθμίσεις αλλάζουν όταν δεν τρέχει καταχώρηση.")
            return
        SettingsDialog(self, self.config_data, lambda: None)

    def close(self) -> None:
        if self.scheduled and self.exit_code == 0 and self.running:
            self.exit_code = 1
        if self.running:
            self.stop()
            self.after(400, self._close_when_idle)
            return
        self.destroy()

    def _close_when_idle(self) -> None:
        if self.worker and self.worker.is_alive():
            self.after(400, self._close_when_idle)
            return
        self.destroy()


def main() -> None:
    import sys

    args = list(sys.argv[1:])
    job_id = None
    if "--job-id" in args:
        index = args.index("--job-id")
        try:
            job_id = int(args[index + 1])
        except (IndexError, ValueError) as failure:
            raise SystemExit("Χρήση: --job-id <αριθμός>") from failure
        del args[index : index + 2]
    scheduled = "--scheduled" in args
    app = App(scheduled=scheduled, job_id=job_id)
    app.mainloop()
    if scheduled:
        raise SystemExit(app.exit_code)


if __name__ == "__main__":
    main()
