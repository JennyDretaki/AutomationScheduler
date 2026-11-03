# -*- coding: utf-8 -*-
"""Ανεβάζει στη CEPAL τα παράβολα από την SP cepal_fill_web (1 γραμμή = 1 καταχώρηση)."""

from __future__ import annotations

import csv
import ctypes
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import sync_playwright

from sql_source import Paravolo, fetch_paravola, save_gui

HEX_ID = re.compile(r"^[a-f0-9]{24}$")
MQTT_FALLBACK = Path(r"C:\Program Files\mosquitto\mosquitto_pub.exe")


def screen_size() -> tuple[int, int]:
    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()
    return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))


def run_command(*args, **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("check", False)
    if kwargs.get("capture_output") or kwargs.get("stdout") or kwargs.get("stderr"):
        kwargs.setdefault("text", True)
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return subprocess.run(*args, **kwargs)


class Stopped(Exception):
    pass


class Uploader:
    def __init__(self, config: dict, on_log, on_progress, on_empty, should_stop):
        self.config = config
        self.on_log = on_log
        self.on_progress = on_progress
        self.on_empty = on_empty
        self.should_stop = should_stop
        self.log_path = Path(__file__).resolve().parent / "logs"
        self.failures_path = self.log_path / "apotixies.csv"
        self.mqtt_warned = False
        self.list_url = ""
        self.total = 0
        self.uploaded = 0
        self.failed = 0

    def run(self, connection) -> tuple[int, int, str | None]:
        error = None
        try:
            self._check_stop()
            rows = fetch_paravola(connection)
            self.total = len(rows)
            self.publish(self.config["mqtt"]["topic_total"], str(self.total))
            self.log(f"Παράβολα προς καταχώρηση: {self.total}.")
            if not rows:
                self.on_empty()
                return 0, 0, None
            self._upload_all(connection, rows)
            self.log(
                f"Καταχωρήθηκαν {self.uploaded} από {self.total}. "
                f"Δεν ανέβηκαν {self.failed}."
            )
            if self.failed:
                self.log(f"Οι λόγοι είναι στο {self.failures_path}.")
        except Stopped:
            error = (
                f"Διακόπηκε. Καταχωρήθηκαν {self.uploaded}, "
                f"δεν ανέβηκαν {self.failed}."
            )
            self.log(error)
        except Exception as failure:
            error = str(failure)
            self.log(f"Σφάλμα: {error}")
        return self.uploaded, self.failed, error

    def _upload_all(self, connection, rows: list[Paravolo]) -> None:
        profile = Path(__file__).resolve().parent / "chrome-profile"
        profile.mkdir(exist_ok=True)
        width, height = screen_size()
        playwright = sync_playwright().start()
        context = None
        try:
            context = self._open_chrome(playwright, profile, width, height)
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(20000)
            self._login(page)
            self._enter_mira(page)
            for index, item in enumerate(rows, start=1):
                self._check_stop()
                self.on_progress(index, self.total)
                label = item.rf or item.kodikos or "(χωρίς RF)"
                self.log(f"Παράβολο {index} από {self.total}, RF {label}.")
                try:
                    self._one(page, connection, item)
                except Stopped:
                    raise
                except Exception as failure:
                    self.failed += 1
                    reason = str(failure).strip() or failure.__class__.__name__
                    self._note_failure(item, reason)
                    self.log(f"Δεν ανέβηκε το RF {label}: {reason}")
                    continue
                self.uploaded += 1
                self.publish(self.config["mqtt"]["topic_uploaded"], str(self.uploaded))
                self.log(f"Ολοκληρώθηκε το {index} από {self.total}.")
        finally:
            self._close_chrome(context, playwright)

    def _one(self, page: Page, connection, item: Paravolo) -> None:
        # Η φόρμα CEPAL είναι στα Αγγλικά (Select agency, Submit, …).
        full = self._archive_file(item)
        self._check_stop()
        self._open_case(page)
        self._upload(page, full)
        self._wait_payment_code(page)
        self._fill_named(page, "payment_code", item.kodikos)
        self._choose(page, "Select agency", item.foreas, label="Agency")
        self._fill_named(page, "payment_category", item.category)
        self._fill_named(page, "payment_type", item.type_name)
        self._fill_named(page, "payment_amount", item.amount.replace(",", "."))
        self._fill_named(page, "creditor_reference", item.rf)
        self._choose(page, "Select portfolio", item.spv, label="Portfolio")
        self._fill_named(page, "debtor_full_name", item.debtor)
        self._fill_named(page, "tax_id", item.tax_id)
        self._fill_named(page, "customer_code", item.customer_code)
        self._fill_named(page, "contract_number", item.contract)
        self._fill_named(page, "collaborating_lawyer", item.lawyer)
        self._fill_date(page, item.publish_date)
        self._check_stop()
        submit = page.get_by_role("button", name=re.compile(r"^(Submit|Υποβολή)$", re.I))
        if submit.count() == 0:
            raise RuntimeError("Δεν βρέθηκε το κουμπί Submit.")
        submit.first.click()
        page.wait_for_timeout(2000)
        problem = self._form_error(page)
        if problem:
            raise RuntimeError(problem)
        gui = self._read_gui(page)
        if not gui:
            problem = self._form_error(page)
            raise RuntimeError(problem or "Μετά την υποβολή δεν εμφανίστηκε κωδικός υπόθεσης.")
        save_gui(connection, gui, item.kodikos)
        self.log(f"Καταχωρήθηκε το RF {item.rf or item.kodikos}. Κωδικός υπόθεσης {gui}.")

    def _archive_file(self, item: Paravolo) -> Path:
        if not item.path:
            raise RuntimeError("Δεν υπάρχει διαδρομή αρχειοθήκης για αυτό το παράβολο.")
        if not item.filename:
            raise RuntimeError("Δεν υπάρχει όνομα αρχείου για αυτό το παράβολο.")
        full = Path(item.path) / item.filename
        try:
            exists = full.is_file()
        except OSError as failure:
            raise RuntimeError(f"Δεν διαβάστηκε το αρχείο της αρχειοθήκης ({full}): {failure}") from failure
        if not exists:
            raise RuntimeError(f"Δεν βρέθηκε το αρχείο στην αρχειοθήκη: {full}")
        return full

    def _upload(self, page: Page, full: Path) -> None:
        self.log(f"Ανέβασμα {full.name}.")
        file_input = page.locator("input[type='file']")
        sent = False
        if file_input.count() == 0:
            try:
                with page.expect_file_chooser(timeout=8000) as chooser:
                    page.get_by_text("Upload files", exact=False).first.click()
                chooser.value.set_files(str(full))
                sent = True
            except Exception as failure:
                file_input = page.locator("input[type='file']")
                if file_input.count() == 0:
                    raise RuntimeError(f"Δεν ανέβηκε το αρχείο στην αρχειοθήκη: {failure}") from failure
        if not sent:
            file_input.first.set_input_files(str(full))
        page.wait_for_timeout(1000)
        problem = self._form_error(page)
        if problem:
            raise RuntimeError(f"Η αρχειοθήκη δεν δέχτηκε το αρχείο: {problem}")

    def _field_selector(self, name: str, visible_only: bool = False) -> str:
        names = (f"case.{name}", name)
        parts = []
        for field_name in names:
            if visible_only:
                parts.append(f"input[name='{field_name}']:visible")
                parts.append(f"textarea[name='{field_name}']:visible")
            else:
                parts.append(f"input[name='{field_name}']")
                parts.append(f"textarea[name='{field_name}']")
        return ", ".join(parts)

    def _fill_named(self, page: Page, name: str, value: str) -> None:
        locator = page.locator(self._field_selector(name, visible_only=True))
        if locator.count() == 0:
            locator = page.locator(self._field_selector(name))
        if locator.count() == 0:
            raise RuntimeError(f"Δεν βρέθηκε το πεδίο {name}. {self._field_names(page)}")
        target = locator.first
        target.click()
        target.fill(value)

    def _fill_date(self, page: Page, value: str) -> None:
        if not value:
            raise RuntimeError("Λείπει η ημερομηνία έκδοσης πληρωμής.")
        field = page.locator(
            "input[name='case.issuedate']:visible, "
            "input[placeholder='Select a date']:visible, "
            "input[placeholder='Επιλέξτε ημερομηνία']:visible"
        )
        if field.count() == 0:
            field = page.locator(
                "input[name='case.issuedate'], "
                "input[placeholder='Select a date'], "
                "input[placeholder='Επιλέξτε ημερομηνία']"
            )
        if field.count() == 0:
            raise RuntimeError("Δεν βρέθηκε το πεδίο ημερομηνίας έκδοσης.")
        field.first.click()
        field.first.fill(value)
        page.keyboard.press("Escape")

    def _wait_payment_code(self, page: Page) -> None:
        deadline = time.time() + 20
        while time.time() < deadline:
            self._check_stop()
            field = page.locator(self._field_selector("payment_code", visible_only=True))
            if field.count():
                return
            if page.locator(self._field_selector("payment_code")).count():
                return
            page.wait_for_timeout(300)
        raise RuntimeError(
            f"Δεν εμφανίστηκε το πεδίο payment_code. {self._field_names(page)}"
        )

    def _choose(self, page: Page, prompt: str, value: str, label: str | None = None) -> None:
        """Combobox (π.χ. Select agency): click → type → κλικ στην option."""
        cleaned = value.strip()
        if not cleaned:
            raise RuntimeError(f"Κενή τιμή για «{prompt}».")
        self.log(f"Επιλογή «{label or prompt}»: {cleaned}")

        trigger = None
        if label:
            trigger = page.get_by_role("combobox", name=re.compile(re.escape(label), re.I))
            if trigger.count() == 0:
                trigger = page.get_by_label(re.compile(re.escape(label), re.I))
        if trigger is None or trigger.count() == 0:
            trigger = page.get_by_role("combobox", name=re.compile(re.escape(prompt), re.I))
        if trigger.count() == 0:
            trigger = page.get_by_role("button", name=re.compile(re.escape(prompt), re.I))
        if trigger.count() == 0:
            trigger = page.get_by_text(prompt, exact=True)
        if trigger.count() == 0:
            raise RuntimeError(f"Δεν βρέθηκε το dropdown «{prompt}». {self._field_names(page)}")

        target = trigger.first
        try:
            target.scroll_into_view_if_needed()
        except Exception:
            pass
        target.click(force=True)
        page.wait_for_timeout(500)
        page.keyboard.type(cleaned, delay=25)
        page.wait_for_timeout(500)

        option = page.get_by_role("option", name=re.compile(re.escape(cleaned), re.I))
        if option.count() == 0:
            option = page.locator("[role='option']").filter(
                has_text=re.compile(re.escape(cleaned), re.I)
            )
        if option.count() == 0:
            if page.locator("[role='option']:visible, [role='listbox']:visible").count():
                page.keyboard.press("Enter")
                page.wait_for_timeout(300)
                return
            raise RuntimeError(f"Δεν βρέθηκε η επιλογή «{cleaned}» στο «{prompt}».")
        option.first.click(force=True)
        page.wait_for_timeout(300)

    def _read_gui(self, page: Page) -> str:
        flow_id = str(self.config.get("flow_id") or "")
        deadline = time.time() + 15
        while time.time() < deadline:
            self._check_stop()
            found = self._gui_on_page(page, flow_id)
            if found:
                return found
            page.wait_for_timeout(500)
        return ""

    def _gui_on_page(self, page: Page, flow_id: str) -> str:
        for part in page.url.split("/"):
            token = part.split("?")[0].strip()
            if HEX_ID.match(token) and token != flow_id:
                return token
        try:
            texts = page.locator("p").all_inner_texts()
        except Exception:
            return ""
        for text in texts:
            token = text.strip()
            if HEX_ID.match(token) and token != flow_id:
                return token
        return ""

    def _form_error(self, page: Page) -> str:
        chunks = []
        alerts = page.locator("[role='alert']:visible")
        count = min(alerts.count(), 4)
        for index in range(count):
            text = " ".join(alerts.nth(index).inner_text().split())
            if text:
                chunks.append(text)
        return " | ".join(chunks)

    def _field_names(self, page: Page) -> str:
        try:
            names = page.locator("input, textarea").evaluate_all(
                "els => els.map(el => el.getAttribute('name')).filter(Boolean)"
            )
        except Exception:
            return ""
        if not names:
            return "Η φόρμα δεν έχει ονόματα πεδίων."
        return "Πεδία φόρμας: " + ", ".join(names[:40])

    def _login(self, page: Page) -> None:
        # 1) Αν υπάρχει ήδη session στο chrome-profile, μην ξανακάνεις login.
        try:
            page.goto(self._case_url(), wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(800)
            if "login" not in page.url.lower() and page.locator(
                self._field_selector("payment_code")
            ).count():
                self.log("Σύνδεση στη CEPAL (υπάρχουσα session).")
                return
        except Exception:
            pass

        urls = []
        for url in (
            self.config.get("login_url"),
            "https://cepal.fragma.aiflows.app/login/",
            "https://cepal.aiflows.app/login/",
        ):
            if url and url not in urls:
                urls.append(url)
        last = ""
        opened = False
        for url in urls:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1000)
                if page.locator("input[type='password']").count():
                    opened = True
                    break
            except Exception as failure:
                last = str(failure)
                self.log(f"Δεν άνοιξε το {url}.")
        if not opened:
            raise RuntimeError(f"Δεν άνοιξε η σελίδα σύνδεσης. {last}".strip())
        email = str(self.config.get("email") or "").strip()
        password = str(self.config.get("password") or "")
        if not email or not password:
            raise RuntimeError("Συμπλήρωσε email και password στις ρυθμίσεις.")

        self._type_into(page, "input[name='email'], input[type='email']", email)
        self._type_into(page, "input[type='password']", password)
        page.wait_for_timeout(300)

        login_btn = page.get_by_role("button", name=re.compile(r"^Login$", re.I))
        if login_btn.count():
            login_btn.first.click()
        elif page.locator("button[type='submit']").count():
            page.locator("button[type='submit']").first.click()
        else:
            page.get_by_role("button", name=re.compile(r"Sign in|Login", re.I)).first.click()

        try:
            page.wait_for_function(
                "() => !location.pathname.toLowerCase().includes('login')",
                timeout=15000,
            )
        except Exception:
            # Το site συχνά απορρίπτει automations (reCAPTCHA) ακόμα κι αν ο κωδικός
            # είναι σωστός χειροκίνητα. Περιμένουμε Login στο ανοιχτό Chrome.
            self.log(
                "Αυτόματο login απέτυχε. Κάνε Login χειροκίνητα στο ανοιχτό Chrome "
                "(έχεις έως 3 λεπτά)…"
            )
            try:
                page.wait_for_function(
                    "() => !location.pathname.toLowerCase().includes('login')",
                    timeout=180000,
                )
            except Exception:
                raise RuntimeError(
                    "Δεν ολοκληρώθηκε η σύνδεση στη CEPAL. "
                    "Κάνε Login χειροκίνητα στο παράθυρο Chrome της καταχώρησης."
                )
        self.log("Σύνδεση στη CEPAL.")

    def _type_into(self, page: Page, selector: str, value: str) -> None:
        """Γέμισμα πεδίου όπως χρήστης + React native value setter."""
        field = page.locator(selector).first
        field.wait_for(state="visible", timeout=10000)
        field.click()
        # Ctrl+A + Backspace καθαρίζει σωστά σε React
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.type(value, delay=40)
        field.evaluate(
            """(el, value) => {
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )

    def _enter_mira(self, page: Page) -> None:
        link = page.locator("a", has_text="Mira")
        if link.count() == 0:
            return
        link.first.click()
        page.wait_for_timeout(2000)
        self.log("Άνοιγμα Mira.")

    def _case_url(self) -> str:
        url = str(self.config.get("case_url") or "").strip()
        if url:
            return url
        flow_id = self.config.get("flow_id") or "68af038088fc31c2c10a41e2"
        return f"https://cepal.aiflows.app/flows/{flow_id}/cases/new-submission"

    def _open_case(self, page: Page) -> None:
        url = self._case_url()
        self.log(f"Άνοιγμα {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if "login" in page.url.lower():
            raise RuntimeError("Η σελίδα καταχώρησης ζήτησε ξανά σύνδεση.")
        page.locator(self._field_selector("payment_code", visible_only=True)).first.wait_for(
            state="visible", timeout=20000
        )

    def _note_failure(self, item: Paravolo, reason: str) -> None:
        self.log_path.mkdir(exist_ok=True)
        new_file = not self.failures_path.exists()
        with self.failures_path.open("a", encoding="utf-8", newline="") as handle:
            if new_file:
                handle.write("\ufeff")
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(["Ημερομηνία", "RF", "Κωδικός Παραβόλου", "Λόγος"])
            writer.writerow([
                datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                item.rf,
                item.kodikos,
                reason,
            ])

    def _open_chrome(self, playwright, profile: Path, width: int, height: int) -> BrowserContext:
        args = [
            "--start-maximized",
            f"--window-size={width},{height}",
            "--window-position=0,0",
        ]
        common = dict(
            user_data_dir=str(profile),
            headless=False,
            accept_downloads=False,
            no_viewport=True,
            args=args,
        )
        try:
            context = playwright.chromium.launch_persistent_context(
                channel="chrome",
                **common,
            )
        except Exception as failure:
            text = str(failure).lower()
            if "in use" in text or "lock" in text or "singleton" in text:
                raise RuntimeError(
                    "Το Chrome profile της καταχώρησης χρησιμοποιείται ήδη "
                    "(τρέχει άλλο instance). Περίμενε να τελειώσει το προηγούμενο."
                ) from failure
            self.log("Το Google Chrome channel απέτυχε· δοκιμή με Playwright Chromium.")
            try:
                context = playwright.chromium.launch_persistent_context(**common)
            except Exception as second:
                raise RuntimeError(
                    "Δεν άνοιξε browser για την καταχώρηση CEPAL. "
                    f"Λεπτομέρειες: {second}"
                ) from second
        context.add_init_script("window.close = () => {};")
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(20000)
        self._maximize(page, width, height)
        return context

    def _maximize(self, page: Page, width: int, height: int) -> None:
        try:
            session = page.context.new_cdp_session(page)
            window = session.send("Browser.getWindowForTarget")
            session.send(
                "Browser.setWindowBounds",
                {"windowId": window["windowId"], "bounds": {"windowState": "normal"}},
            )
            session.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window["windowId"],
                    "bounds": {"left": 0, "top": 0, "width": width, "height": height},
                },
            )
            session.send(
                "Browser.setWindowBounds",
                {"windowId": window["windowId"], "bounds": {"windowState": "maximized"}},
            )
        except Exception as failure:
            self.log(f"Το παράθυρο δεν μεγιστοποιήθηκε: {failure}")

    def _close_chrome(self, context: BrowserContext | None, playwright) -> None:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        try:
            playwright.stop()
        except Exception:
            pass

    def _check_stop(self) -> None:
        if self.should_stop():
            raise Stopped()

    def publish(self, topic: str, message: str) -> None:
        mqtt = self.config["mqtt"]
        if not mqtt.get("enabled", False):
            return
        commands = [
            ["mosquitto_pub"],
        ]
        if MQTT_FALLBACK.is_file():
            commands.append([str(MQTT_FALLBACK)])
        completed = None
        for command in commands:
            full = command + [
                "-h", str(mqtt["host"]),
                "-p", str(mqtt["port"]),
                "-t", topic,
                "-m", message,
            ]
            try:
                completed = run_command(full, capture_output=True, timeout=15)
                break
            except FileNotFoundError:
                completed = None
            except subprocess.TimeoutExpired:
                self.log(f"Το MQTT στο {topic} δεν απάντησε.")
                return
        if completed is None:
            if not self.mqtt_warned:
                self.log("Το mosquitto_pub δεν βρέθηκε. Το MQTT παραλείπεται.")
                self.mqtt_warned = True
            return
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            self.log(f"Το MQTT στο {topic} απέτυχε: {detail}")

    def log(self, message: str) -> None:
        line = f"{datetime.now():%H:%M:%S}  {message}"
        self.log_path.mkdir(exist_ok=True)
        with (self.log_path / f"{datetime.now():%Y-%m-%d}.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        self.on_log(line)
