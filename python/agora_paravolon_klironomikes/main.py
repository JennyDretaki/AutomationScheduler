# -*- coding: utf-8 -*-
"""Αγορά παραβόλων κληρονομικών (action: KLIRONOMIKES_AGORA_PARAVOLON).

Τρέξιμο:
    py -3 main.py
    py -3 main.py --scheduled
    py -3 main.py DOVALUE\\5.00\\50

Το --scheduled το ξεκινά ο C# AutomationScheduler. Το script διαβάζει τον
    ticket_scheduler_for_automation (μόνο KLIRONOMIKES_AGORA_PARAVOLON) και γράφει
    το υπόλοιπο στο upd_amount (0 στο τέλος, όχι NULL).
"""

from __future__ import annotations

import json
import os
import queue
import re
import tempfile
import threading
from pathlib import Path

# Πριν tkinter/playwright — ώστε να δουλεύει και από C# scheduler (χωρίς start.bat),
# είτε από mapped drive είτε από UNC
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
from tkinter import filedialog, messagebox, ttk

from issuer import run_purchases
from sql_source import (
    connect,
    fetch_companies,
    fetch_due_jobs,
    fetch_waiting_job,
    format_value,
    mark_job_failed,
    mark_job_succeed,
    mark_job_waiting_remaining,
    update_remaining,
)

CONFIG_PATH = HERE / "config.json"
CAPTCHA_FILE = Path(tempfile.gettempdir()) / "paravolo_captcha.png"


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_config(config: dict) -> None:
    stored = {
        key: config[key]
        for key in (
            "create_url",
            "paravolo_code",
            "email",
            "pdf_folder",
            "pdf_name",
            "parallel",
            "work_folder",
            "extractor",
            "sql",
            "mqtt",
        )
        if key in config
    }
    CONFIG_PATH.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_job(text: str) -> tuple[str, str, int]:
    parts = text.strip().split("\\")
    if len(parts) != 3:
        raise ValueError("Η μορφή είναι ΕΤΑΙΡΕΙΑ\\ΠΟΣΟ\\ΤΕΜΑΧΙΑ, για παράδειγμα DOVALUE\\7\\50.")
    company, price, count = parts
    try:
        priced = format_value(price)
    except RuntimeError as failure:
        raise ValueError(str(failure)) from failure
    pieces = int(float(count))
    if pieces < 1:
        raise ValueError("Τα τεμάχια πρέπει να είναι τουλάχιστον 1.")
    return company.strip(), priced, pieces


def amount_choice(text: str) -> str | None:
    try:
        return format_value(text)
    except RuntimeError:
        return None


def count_choice(text: str) -> str | None:
    if not re.fullmatch(r"\d+", text.strip()):
        return None
    pieces = int(text.strip())
    if pieces < 1:
        return None
    return str(pieces)


def ask_step(parent: tk.Misc, step: int, title: str, prompt: str, hint: str, accept) -> str | None:
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.resizable(False, False)
    dialog.transient(parent)
    dialog.attributes("-topmost", True)
    dialog.grab_set()
    result: dict[str, str | None] = {"value": None}

    ttk.Label(dialog, text=f"{step} από 3", font=("Segoe UI", 10)).pack(anchor="w", padx=22, pady=(18, 0))
    ttk.Label(dialog, text=prompt, font=("Segoe UI", 18, "bold")).pack(anchor="w", padx=22, pady=(4, 0))
    ttk.Label(dialog, text=hint, font=("Segoe UI", 11)).pack(anchor="w", padx=22, pady=(2, 0))

    error = tk.StringVar()
    value = tk.StringVar()
    entry = ttk.Entry(dialog, textvariable=value, font=("Segoe UI", 22), justify="center", width=10)
    entry.pack(padx=22, pady=(14, 4))
    tk.Label(dialog, textvariable=error, fg="#a32020", font=("Segoe UI", 10)).pack(padx=22)

    def accept_value(_event=None) -> None:
        chosen = accept(value.get())
        if chosen is None:
            error.set("Αυτή η τιμή δεν γίνεται δεκτή.")
            entry.focus_set()
            return
        result["value"] = chosen
        dialog.destroy()

    def cancel() -> None:
        dialog.destroy()

    buttons = ttk.Frame(dialog)
    buttons.pack(pady=(8, 18))
    ttk.Button(buttons, text="Συνέχεια", command=accept_value).pack(side="left", padx=6)
    ttk.Button(buttons, text="Ακύρωση", command=cancel).pack(side="left", padx=6)
    dialog.bind("<Return>", accept_value)
    dialog.protocol("WM_DELETE_WINDOW", cancel)
    dialog.update_idletasks()
    width = max(dialog.winfo_width(), 360)
    height = dialog.winfo_height()
    x = (dialog.winfo_screenwidth() - width) // 2
    y = (dialog.winfo_screenheight() - height) // 3
    dialog.geometry(f"{width}x{height}+{x}+{y}")
    entry.focus_set()
    parent.wait_window(dialog)
    return result["value"]


class SettingsDialog(tk.Toplevel):
    def __init__(self, master, config: dict, on_save):
        super().__init__(master)
        self.title("Ρυθμίσεις")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.config_data = config
        self.on_save = on_save

        pad = {"padx": 12, "pady": 4}
        frame = ttk.Frame(self, padding=12)
        frame.grid(row=0, column=0)

        self.url = self._row(frame, 0, "Διεύθυνση φόρμας", config["create_url"], 64)
        self.code = self._row(frame, 1, "Κωδικός τύπου", str(config["paravolo_code"]), 8)
        self.email = self._row(frame, 2, "Email", config["email"], 42)
        self.folder = self._row(frame, 3, "Φάκελος PDF", config["pdf_folder"], 42)
        ttk.Button(frame, text="Επιλογή...", command=self._browse).grid(row=3, column=2, padx=(0, 12))

        mqtt = config["mqtt"]
        self.mqtt_on = tk.BooleanVar(value=bool(mqtt.get("enabled", True)))
        ttk.Checkbutton(frame, text="Αποστολή MQTT", variable=self.mqtt_on).grid(
            row=4, column=1, sticky="w", **pad
        )
        self.host = self._row(frame, 5, "MQTT host", str(mqtt["host"]), 24)
        self.port = self._row(frame, 6, "MQTT port", str(mqtt["port"]), 8)
        self.topic_total = self._row(frame, 7, "Topic έναρξης", mqtt["topic_total"], 32)
        self.topic_updated = self._row(frame, 8, "Topic προόδου", mqtt["topic_updated"], 32)

        buttons = ttk.Frame(frame)
        buttons.grid(row=9, column=1, sticky="e", pady=(12, 4))
        ttk.Button(buttons, text="Ακύρωση", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Αποθήκευση", command=self._save).pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda _event: self.destroy())

    def _row(self, frame, row: int, label: str, value: str, width: int) -> tk.StringVar:
        variable = tk.StringVar(value=value)
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=12, pady=4)
        ttk.Entry(frame, textvariable=variable, width=width).grid(row=row, column=1, sticky="ew", padx=12, pady=4)
        return variable

    def _browse(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.folder.get() or str(HERE))
        if folder:
            self.folder.set(folder)

    def _save(self) -> None:
        config = self.config_data
        config["create_url"] = self.url.get().strip()
        config["paravolo_code"] = self.code.get().strip()
        config["email"] = self.email.get().strip()
        config["pdf_folder"] = self.folder.get().strip()
        config["mqtt"]["enabled"] = self.mqtt_on.get()
        config["mqtt"]["host"] = self.host.get().strip()
        try:
            config["mqtt"]["port"] = int(self.port.get().strip())
        except ValueError:
            messagebox.showerror("Ρυθμίσεις", "Η θύρα MQTT πρέπει να είναι αριθμός.", parent=self)
            return
        config["mqtt"]["topic_total"] = self.topic_total.get().strip()
        config["mqtt"]["topic_updated"] = self.topic_updated.get().strip()
        save_config(config)
        self.on_save()
        self.destroy()


class App(tk.Tk):
    def __init__(self, preset: str = "", job_id: int | None = None, store: str | None = None):
        super().__init__()
        self.title("Αγορά παραβόλων κληρονομικών")
        self.geometry("760x700")
        self.minsize(680, 560)
        self.config_data = load_config()
        self.config_data.setdefault("companies", {})
        self.ui_queue: queue.Queue = queue.Queue()
        self.reply_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.running = False
        self.photo = None
        self.scheduler_job_id: int | None = None
        self.scheduler_started = None
        self.scheduler_company: str | None = None
        self.scheduler_price: str | None = None
        self.scheduler_total: int = 0
        self.scheduler_completed: int = 0
        self.requested_job_id = job_id
        self.requested_store = (store or "").strip().lower() or None
        self._progress_lock = threading.Lock()
        try:
            profiles = fetch_companies(self.config_data)
            if profiles:
                self.config_data["companies"] = profiles
        except Exception as error:
            self._company_load_error = str(error)
        else:
            self._company_load_error = ""

        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        header = ttk.Frame(self, padding=(16, 14, 16, 4))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="Αγορά παραβόλων κληρονομικών", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            header,
            text="Ανοίγει το Chrome χωρίς ποντίκι. Σώζει eparavolo.pdf στο C:\\test και το μεταφέρει αυτόματα.",
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(2, 0))

        form = ttk.Frame(self, padding=(16, 10, 16, 4))
        form.grid(row=1, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Γρήγορη εισαγωγή").grid(row=0, column=0, sticky="w", pady=4)
        self.shortcut = tk.StringVar()
        shortcut = ttk.Entry(form, textvariable=self.shortcut)
        shortcut.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=4)
        shortcut.bind("<Return>", self.apply_shortcut)
        tk.Label(form, text="ΕΤΑΙΡΕΙΑ\\ΠΟΣΟ\\ΤΕΜΑΧΙΑ", fg="#5c6570").grid(
            row=1, column=1, sticky="w", padx=(8, 0)
        )

        ttk.Label(form, text="Εταιρεία").grid(row=2, column=0, sticky="w", pady=(10, 4))
        self.company = tk.StringVar()
        self.company_box = ttk.Combobox(form, textvariable=self.company, state="readonly", width=18)
        self.company_box.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(10, 4))
        self.company_box.bind("<<ComboboxSelected>>", lambda _event: self.show_company())

        ttk.Label(form, text="Ποσό €").grid(row=2, column=2, sticky="e", padx=(16, 4))
        self.price = tk.StringVar()
        ttk.Entry(form, textvariable=self.price, width=10).grid(row=2, column=3, sticky="w")

        ttk.Label(form, text="Τεμάχια").grid(row=3, column=2, sticky="e", padx=(16, 4), pady=4)
        self.count = tk.StringVar()
        ttk.Entry(form, textvariable=self.count, width=10).grid(row=3, column=3, sticky="w", pady=4)

        self.details = tk.StringVar()
        ttk.Label(form, textvariable=self.details, justify="left").grid(
            row=3, column=0, columnspan=2, rowspan=3, sticky="nw", pady=4
        )
        self.code_label = tk.StringVar()
        ttk.Label(form, textvariable=self.code_label).grid(row=4, column=2, columnspan=2, sticky="w", pady=(8, 0))

        actions = ttk.Frame(self, padding=(16, 8, 16, 4))
        actions.grid(row=2, column=0, sticky="ew")
        self.start_button = ttk.Button(actions, text="Έναρξη", command=self.start)
        self.start_button.pack(side="left")
        self.load_button = ttk.Button(actions, text="Ανάγνωση SQL", command=self.load_from_sql)
        self.load_button.pack(side="left", padx=(8, 0))
        self.stop_button = ttk.Button(actions, text="Διακοπή", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="Ρυθμίσεις", command=self.open_settings).pack(side="left")
        self.progress = tk.StringVar(value="Έτοιμο")
        ttk.Label(actions, textvariable=self.progress).pack(side="right")

        self.captcha = ttk.LabelFrame(self, text="Captcha", padding=12)
        self.captcha.grid(row=3, column=0, sticky="ew", padx=16, pady=6)
        self.captcha_heading = tk.StringVar()
        ttk.Label(self.captcha, textvariable=self.captcha_heading, font=("Segoe UI", 11)).pack(anchor="w")
        self.captcha_image = ttk.Label(self.captcha)
        self.captcha_image.pack(anchor="w", pady=6)
        row = ttk.Frame(self.captcha)
        row.pack(anchor="w")
        self.captcha_value = tk.StringVar()
        self.captcha_entry = ttk.Entry(row, textvariable=self.captcha_value, font=("Segoe UI", 16), width=8, justify="center")
        self.captcha_entry.pack(side="left")
        self.captcha_entry.bind("<Return>", lambda _event: self.accept_captcha())
        ttk.Button(row, text="Καταχώρηση", command=self.accept_captcha).pack(side="left", padx=8)
        ttk.Button(row, text="Άλλο captcha", command=self.refresh_captcha).pack(side="left")
        self.captcha.grid_remove()

        log_frame = ttk.Frame(self, padding=(16, 4, 16, 12))
        log_frame.grid(row=4, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=12, wrap="word", font=("Consolas", 10), state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.reload_companies()
        self.scheduled = False
        self.exit_code = 0
        if preset == "--scheduled":
            self.scheduled = True
            self.after(200, self._run_scheduled)
        elif preset:
            self.shortcut.set(preset)
            self.apply_shortcut()
        else:
            self.after(50, self.ask_startup)
        self.after(100, self.poll)

    def reload_companies(self) -> None:
        names = list(self.config_data["companies"])
        self.company_box.configure(values=names)
        if self.company.get() not in names:
            self.company.set("")
        self.code_label.set(f"Κωδικός τύπου {self.config_data['paravolo_code']}")
        self.show_company()

    def company_profile(self, company: str) -> tuple[str, dict]:
        for name, profile in self.config_data["companies"].items():
            if name.lower() == company.strip().lower():
                return name, profile
        known = ", ".join(self.config_data["companies"]) or "(καμία)"
        raise ValueError(f"Η εταιρεία δεν υπάρχει στον πίνακα companies. Διαθέσιμες: {known}.")

    def show_company(self) -> None:
        company = self.config_data["companies"].get(self.company.get())
        if not company:
            self.details.set("")
            return
        self.details.set(
            f"Επωνυμία: {company['last_name']}\n"
            f"ΑΦΜ: {company['afm']}\n"
            f"Όνομα στη φόρμα: {company['first_name']}\n"
            f"Πατρώνυμο: {company['father_name']}"
        )

    def apply_shortcut(self, _event=None) -> None:
        text = self.shortcut.get().strip()
        if not text:
            return
        try:
            company, price, count = parse_job(text)
        except ValueError as error:
            self.progress.set(str(error))
            return
        try:
            company, _profile = self.company_profile(company)
        except ValueError as error:
            self.progress.set(str(error))
            return
        self.company.set(company)
        self.price.set(price)
        self.count.set(str(count))
        self.show_company()
        self.progress.set(f"{company}  ·  {price} €  ·  {count} τεμάχια")

    def current_job(self) -> tuple[str, str, int]:
        company, profile = self.company_profile(self.company.get())
        _company, price, count = parse_job(f"{company}\\{self.price.get()}\\{self.count.get()}")
        if len(profile["first_name"]) > 50:
            raise ValueError("Η διεύθυνση στο πεδίο ονόματος ξεπερνά τους 50 χαρακτήρες της φόρμας.")
        if not re.fullmatch(r"\d{9}", profile["afm"]):
            raise ValueError(f"Ο ΑΦΜ της {company} δεν έχει 9 ψηφία.")
        return company, price, count

    def ask_startup(self) -> None:
        names = list(self.config_data["companies"])
        hint = "  ·  ".join(f"{index + 1} {name}" for index, name in enumerate(names)) or "Γράψε το όνομα εταιρείας"

        def accept_company(text: str) -> str | None:
            cleaned = text.strip()
            if cleaned.isdigit():
                index = int(cleaned) - 1
                if 0 <= index < len(names):
                    return names[index]
            for name in names:
                if name.lower() == cleaned.lower():
                    return name
            return None

        choice = ask_step(self, 1, "Εταιρεία", "Εταιρεία", hint, accept_company)
        if choice is None:
            self.deiconify()
            self.lift()
            return
        company = choice

        price = ask_step(self, 2, "Ποσό", "Ποσό", "Το ποσό του πίνακα, π.χ. 3 ή 7.5", amount_choice)
        if price is None:
            self.company.set(company)
            self.show_company()
            self.deiconify()
            self.lift()
            return

        count = ask_step(self, 3, "Πλήθος", "Πλήθος", "Πόσα παράβολα", count_choice)
        if count is None:
            self.company.set(company)
            self.price.set(price)
            self.show_company()
            self.deiconify()
            self.lift()
            return

        self.company.set(company)
        self.price.set(price)
        self.count.set(count)
        self.shortcut.set(f"{company}\\{price}\\{count}")
        self.show_company()
        self.progress.set(f"{company}  ·  {price} €  ·  {count} τεμάχια")
        self.append(f"{choice} → {company}, ποσό {price}, πλήθος {count}.")
        self.deiconify()
        self.lift()
        self.focus_force()

    def load_from_sql(self) -> None:
        self._read_job()

    def start(self) -> None:
        if self.running:
            return
        try:
            company, price, count = self.current_job()
        except ValueError as error:
            self.progress.set(str(error))
            return
        self.running = True
        self.stop_event.clear()
        self._drain(self.reply_queue)
        self.start_button.configure(state="disabled")
        self.load_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.company_box.configure(state="disabled")
        self.progress.set(f"0 από {count}")
        self.append(f"Έναρξη {company}, {count} παράβολα των {price} €.")

        def work() -> None:
            connection = None
            try:
                connection = connect(self.config_data)
                server = self.config_data["sql"]["server"]
                self.ui_queue.put(("log", f"Συνδεδεμένη στον {server}."))
                self._issue(company, price, count)
            except Exception as error:
                self.ui_queue.put(("done", 0, count, str(error)))
            finally:
                if connection is not None:
                    connection.close()

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _read_job(self) -> None:
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self._drain(self.reply_queue)
        self.start_button.configure(state="disabled")
        self.load_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.company_box.configure(state="disabled")
        self.scheduler_job_id = None
        self.scheduler_started = None
        server = self.config_data["sql"]["server"]
        database = self.config_data["sql"]["database"]
        self.progress.set("Σύνδεση στη βάση...")
        target = (
            f"job id={self.requested_job_id}"
            if self.requested_job_id is not None
            else "η επόμενη waiting αγορά"
        )
        self.append(f"Σύνδεση στον {server}/{database} · {target}.")

        def work() -> None:
            connection = None
            try:
                connection, job = fetch_waiting_job(
                    self.config_data, job_id=self.requested_job_id
                )
                self.ui_queue.put(
                    (
                        "job",
                        job.job_id,
                        job.label,
                        job.company,
                        job.price,
                        job.count,
                        job.started,
                        job.profile.as_dict(),
                    )
                )
                self.ui_queue.put(("ready",))
            except Exception as error:
                self.ui_queue.put(("done", 0, 0, str(error)))
            finally:
                if connection is not None:
                    connection.close()

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _run_scheduled(self) -> None:
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.load_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.set("Ανάγνωση εργασιών...")
        self.append("Σύνδεση για παράλληλη αγορά παραβόλων.")

        def work() -> None:
            try:
                if self.requested_job_id is not None:
                    jobs = self._claim_scheduled_jobs()
                else:
                    jobs = fetch_due_jobs(self.config_data)
            except Exception as error:
                self.ui_queue.put(("done", 0, 0, str(error)))
                return

            if not jobs:
                self.ui_queue.put(("done", 0, 0, "Δεν υπάρχει waiting εργασία αγοράς παραβόλων."))
                return

            self.ui_queue.put(("log", f"Αγορά μία τη φορά, {len(jobs)} εργασίες."))
            failures: list[str] = []

            def run_job(job) -> None:
                self.ui_queue.put(("log", f"Εργασία {job.job_id}: {job.label}"))
                completed, error = run_purchases(
                    self.config_data,
                    job.company,
                    job.price,
                    job.count,
                    lambda line: self.ui_queue.put(("log", line)),
                    self._captcha_reply,
                    lambda index, total, job_id=job.job_id: self._remember_remaining(job_id, index, total),
                    self.stop_event.is_set,
                    company_profile=job.profile.as_dict(),
                    slots=1,
                )
                self._close_ticket_job(job.job_id, completed, job.count, error)
                if error:
                    failures.append(error)

            for job in jobs:
                if self.stop_event.is_set():
                    break
                run_job(job)

            bought = sum(job.count for job in jobs)
            summary = failures[0] if failures else None
            self.ui_queue.put(("done", 0 if summary else bought, bought, summary))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _claim_scheduled_jobs(self):
        connection, job = fetch_waiting_job(self.config_data, job_id=self.requested_job_id)
        connection.close()
        return [job]

    def _captcha_reply(self, image: bytes, heading: str):
        self.ui_queue.put(("captcha", image, heading))
        while not self.stop_event.is_set():
            try:
                return self.reply_queue.get(timeout=0.2)
            except queue.Empty:
                continue
        return None

    def _remember_remaining(self, job_id: int, index: int, total: int) -> None:
        self.ui_queue.put(("progress", index, total))
        remaining = total - index
        try:
            with self._progress_lock:
                connection = update_remaining(self.config_data, job_id, remaining)
                connection.close()
            shown = "0" if remaining < 1 else str(remaining)
            self.ui_queue.put(("log", f"Εργασία {job_id}: upd_amount = {shown}"))
        except Exception as failure:
            self.ui_queue.put(("log", str(failure)))

    def _close_ticket_job(self, job_id: int, completed: int, total: int, error: str | None) -> None:
        remaining = max(0, total - completed)
        try:
            if error is None and completed >= total and total > 0:
                connection = mark_job_succeed(self.config_data, job_id)
                connection.close()
                self.ui_queue.put(("log", f"Εργασία {job_id}: status → succeed"))
                return
            # Π.χ. amount=20, κατέβηκαν 14 → upd_amount=6, status=waiting για συνέχεια.
            if remaining > 0:
                connection = mark_job_waiting_remaining(self.config_data, job_id, remaining)
                connection.close()
                self.ui_queue.put(
                    ("log", f"Εργασία {job_id}: status → waiting · upd_amount={remaining}")
                )
                if error:
                    self.ui_queue.put(("log", error))
                return
            connection = mark_job_failed(self.config_data, job_id)
            connection.close()
            self.ui_queue.put(("log", f"Εργασία {job_id}: status → failed"))
            if error:
                self.ui_queue.put(("log", error))
        except Exception as failure:
            self.ui_queue.put(("log", str(failure)))

    def _issue(self, company: str, price: str, count: int) -> None:
        _name, profile = self.company_profile(company)
        if len(profile["first_name"]) > 50:
            raise RuntimeError("Η διεύθυνση στο πεδίο ονόματος ξεπερνά τους 50 χαρακτήρες της φόρμας.")

        def on_log(line: str) -> None:
            self.ui_queue.put(("log", line))

        def on_progress(index: int, total: int) -> None:
            job_id = self.scheduler_job_id
            if job_id is None:
                self.ui_queue.put(("progress", index, total))
                return
            self._remember_remaining(job_id, index, total)

        completed, error = run_purchases(
            self.config_data,
            company,
            price,
            count,
            on_log,
            self._captcha_reply,
            on_progress,
            self.stop_event.is_set,
            company_profile=profile,
        )
        self.ui_queue.put(("done", completed, count, error))

    def stop(self) -> None:
        self.stop_event.set()
        self._drain(self.reply_queue)
        self.reply_queue.put(None)
        self.progress.set("Γίνεται διακοπή...")
        self.stop_button.configure(state="disabled")

    def accept_captcha(self) -> None:
        text = self.captcha_value.get().strip()
        if not re.fullmatch(r"\d{1,5}", text):
            self.progress.set("Το captcha δέχεται μόνο αριθμούς, μέχρι 5 ψηφία.")
            return
        self.captcha.grid_remove()
        self.reply_queue.put(text)

    def refresh_captcha(self) -> None:
        self.captcha.grid_remove()
        self.reply_queue.put("refresh")

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
            elif kind == "captcha":
                self.show_captcha(message[1], message[2])
            elif kind == "job":
                self.show_job(*message[1:])
            elif kind == "ready":
                self.mark_ready()
            elif kind == "done":
                self.finish(*message[1:])
        self.after(100, self.poll)

    def show_job(
        self,
        job_id: int,
        parameters: str,
        company: str,
        price: str,
        count: int,
        started=None,
        profile: dict | None = None,
    ) -> None:
        if profile:
            self.config_data["companies"][company] = profile
            self.reload_companies()
        self.scheduler_job_id = job_id
        self.scheduler_started = started
        self.scheduler_company = company
        self.scheduler_price = price
        self.scheduler_total = count
        self.scheduler_completed = 0
        self.company.set(company)
        self.price.set(price)
        self.count.set(str(count))
        self.shortcut.set(parameters)
        self.show_company()
        self.append(f"Waiting → running · εργασία {job_id}: {parameters}")
        self.progress.set(f"{company}  ·  {price} €  ·  {count} τεμάχια")

    def mark_ready(self) -> None:
        self.running = False
        self.start_button.configure(state="normal")
        self.load_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.company_box.configure(state="readonly")
        self.after(200, self.start)

    def show_captcha(self, image: bytes, heading: str) -> None:
        CAPTCHA_FILE.write_bytes(image)
        self.photo = tk.PhotoImage(file=str(CAPTCHA_FILE))
        self.captcha_image.configure(image=self.photo)
        self.captcha_heading.set(heading + "  —  γράψε τους αριθμούς")
        self.captcha_value.set("")
        self.captcha.grid()
        self.captcha_entry.focus_set()

    def finish(self, completed: int, total: int, error: str | None) -> None:
        self.running = False
        self.captcha.grid_remove()
        self.start_button.configure(state="normal")
        self.load_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.company_box.configure(state="readonly")
        self.scheduler_completed = completed

        if "Δεν υπάρχει waiting" in (error or ""):
            self.append("Δεν υπάρχουν waiting εργασίες αγοράς.")
            self.progress.set("Καμία waiting εργασία")
            if self.scheduled:
                self._finish_scheduled(0)
            return

        if error is None and completed >= total and total > 0:
            self._write_scheduler_outcome(completed, total, remaining=0, failed=False)
            self.progress.set(f"Ολοκληρώθηκαν {completed} από {total}")
            if self.scheduled:
                self._finish_scheduled(0)
            return

        remaining = max(0, total - completed)
        if remaining > 0 and self.scheduler_job_id is not None:
            self._write_scheduler_outcome(completed, total, remaining=remaining, failed=False)
            self.append(error or f"Υπόλοιπο {remaining} από {total}.")
            self.progress.set(f"Υπόλοιπο {remaining} · status waiting")
            if self.scheduled:
                self._finish_scheduled(0)
            return

        if self.scheduler_job_id is not None:
            self._write_scheduler_outcome(completed, total, remaining=0, failed=True)
        if error:
            self.append(error)
            self.progress.set(error.splitlines()[0][:160])
        if self.scheduled:
            self._finish_scheduled(1)

    def _write_scheduler_outcome(
        self,
        completed: int,
        total: int,
        *,
        remaining: int,
        failed: bool,
    ) -> None:
        job_id = self.scheduler_job_id
        if job_id is None:
            return
        del completed, total
        connection = None
        try:
            if remaining > 0:
                connection = mark_job_waiting_remaining(self.config_data, job_id, remaining)
                self.append(f"Εργασία {job_id}: status → waiting · upd_amount={remaining}")
            elif failed:
                connection = mark_job_failed(self.config_data, job_id)
                self.append(f"Εργασία {job_id}: status → failed")
            else:
                connection = mark_job_succeed(
                    self.config_data, job_id, self.scheduler_started
                )
                self.append(f"Εργασία {job_id}: status → succeed")
        except Exception as failure:
            self.append(str(failure))
        finally:
            if connection is not None:
                connection.close()
            self.scheduler_job_id = None
            self.scheduler_started = None
            self.scheduler_company = None
            self.scheduler_price = None

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
            self.progress.set("Οι ρυθμίσεις αλλάζουν όταν δεν τρέχει έκδοση.")
            return
        SettingsDialog(self, self.config_data, self.reload_companies)

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

    @staticmethod
    def _drain(items: queue.Queue) -> None:
        while True:
            try:
                items.get_nowait()
            except queue.Empty:
                return


def main() -> None:
    import sys

    args = list(sys.argv[1:])
    preset = ""
    job_id = None
    store = None

    if "--store" in args:
        index = args.index("--store")
        try:
            store = args[index + 1].strip().lower()
        except IndexError as failure:
            raise SystemExit("Χρήση: --store ticket|automation") from failure
        del args[index : index + 2]

    if "--job-id" in args:
        index = args.index("--job-id")
        try:
            job_id = int(args[index + 1])
        except (IndexError, ValueError) as failure:
            raise SystemExit("Χρήση: --job-id <αριθμός>") from failure
        del args[index : index + 2]

    if args:
        preset = args[0]

    app = App(preset, job_id=job_id, store=store)
    app.mainloop()
    if getattr(app, "scheduled", False):
        raise SystemExit(app.exit_code)


if __name__ == "__main__":
    main()
