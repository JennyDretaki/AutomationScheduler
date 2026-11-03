# -*- coding: utf-8 -*-
"""Συμπληρώνει τη δημόσια φόρμα e-παραβόλου μέσα από το Chrome, χωρίς ποντίκι."""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from sql_source import format_value, insert_paravolo_dikastiria

TEST_ROOT = Path(r"C:\test")
SHARE_ROOT = Path(r"YOUR_SHARE_ROOT\ΠΑΡΑΒΟΛΑ ΠΡΟΣ ΠΛΗΡΩΜΗ")
_LOG_LOCK = threading.Lock()
_PROFILE_IGNORE = shutil.ignore_patterns(
    "Singleton*",
    "lockfile",
    "Lock",
    "*.lock",
    "Cache",
    "Code Cache",
    "GPUCache",
    "Crashpad",
    "GrShaderCache",
    "ShaderCache",
    "Service Worker",
)

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright


def run_command(*args, **kwargs) -> subprocess.CompletedProcess:
    """Τρέχει εντολή χωρίς να σκάει σε ελληνικά Windows από encoding της κονσόλας."""
    kwargs.setdefault("check", False)
    if kwargs.get("capture_output") or kwargs.get("stdout") or kwargs.get("stderr"):
        kwargs.setdefault("text", True)
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return subprocess.run(*args, **kwargs)

PRICE_SELECTORS = (
    "#inputPrice",
    "#priceVariable",
    "input[name='inputPrice']",
    "input[name='priceVariable']",
)

SUBMIT = "input[value='Υποβολή Αιτήματος']"
CONFIRM = "input[value='Οριστική Υποβολή Αιτήματος'], button:has-text('Οριστική Υποβολή Αιτήματος')"
PRINT = "input[value*='Εκτύπωση'], button:has-text('Εκτύπωση'), a:has-text('Εκτύπωση')"


class Stopped(Exception):
    pass


def _company_from_config(config: dict, company_code: str) -> dict:
    companies = config.get("companies") or {}
    for name, profile in companies.items():
        if name.lower() == company_code.strip().lower():
            return profile
    raise RuntimeError(f"Δεν βρέθηκαν στοιχεία για την εταιρεία {company_code}.")


def parallel_slots(config: dict, count: int) -> int:
    try:
        requested = int(config.get("parallel", 1))
    except (TypeError, ValueError):
        requested = 1
    if requested < 1:
        requested = 1
    return max(1, min(count, requested))


def euro_folder_name(price: str) -> str:
    amount = float(price)
    if amount == int(amount):
        return f"{int(amount)}euro"
    return f"{price}euro"


def clone_chrome_profile(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    shutil.copytree(source, destination, ignore=_PROFILE_IGNORE, dirs_exist_ok=True)
    default = destination / "Default"
    default.mkdir(parents=True, exist_ok=True)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        for folder in (destination, default):
            lock = folder / name
            if lock.exists():
                try:
                    lock.unlink()
                except OSError:
                    pass


class Issuer:
    def __init__(self, config: dict, company_code: str, price: str, count: int,
                 on_log, on_captcha, on_progress, should_stop,
                 company_profile: dict | None = None,
                 chrome_profile: Path | None = None,
                 publish_mqtt: bool = True):
        self.config = config
        self.company_code = company_code
        self.company = company_profile or _company_from_config(config, company_code)
        self.price = format_value(price)
        self.count = count
        self.on_log = on_log
        self.on_captcha = on_captcha
        self.on_progress = on_progress
        self.should_stop = should_stop
        self.publish_mqtt = publish_mqtt
        self.log_path = Path(__file__).resolve().parent / "logs"
        self.mqtt_warned = False
        self.chrome_profile = chrome_profile or (Path(__file__).resolve().parent / "chrome-profile")
        # work_folder πρέπει να είναι τοπικό στο PC (όχι C:\Users\άλλου). Fallback αν αποτύχει.
        candidates = [
            Path(str(config.get("work_folder") or "")),
            Path(r"C:\test\work"),
            Path(__file__).resolve().parent / "work",
        ]
        work = None
        last_error: Exception | None = None
        for candidate in candidates:
            if not str(candidate).strip() or str(candidate) in {".", ""}:
                continue
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                work = candidate
                break
            except OSError as failure:
                last_error = failure
        if work is None:
            raise RuntimeError(
                f"Δεν δημιουργήθηκε work folder (δοκίμασε C:\\test\\work): {last_error}"
            ) from last_error
        self.work_folder = work
        stamp = f"{os.getpid()}-{id(self)}"
        self.captcha_image = work / f"captcha-{stamp}.png"
        self.captcha_text = work / f"ocr-{stamp}.txt"
        name = str(config.get("pdf_name", "eparavolo.pdf")).strip() or "eparavolo.pdf"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        self.pdf_name = name
        self.extractor = dict(config.get("extractor", {}))

    def run(self) -> tuple[int, str | None]:
        self._completed = 0
        error = None
        try:
            self._check_stop()
            self.log(
                f"{self.company_code}: {self.count} παράβολα των {self.price} €, "
                f"κωδικός {self.config['paravolo_code']}."
            )
            if self.publish_mqtt:
                self.publish(self._mqtt_topic("topic_total"), str(self.count))
            self._issue_all()
            if self._completed == self.count:
                self.log(f"Ολοκληρώθηκαν και τα {self.count}.")
        except Stopped:
            error = f"Διακόπηκε μετά από {self._completed} από {self.count}."
            self.log(error)
        except Exception as failure:
            error = str(failure)
            self.log(f"Σφάλμα: {error}")
            if self._completed:
                self.log(f"Πρόλαβαν να εκδοθούν {self._completed} από {self.count}.")
        return self._completed, error

    def _issue_all(self) -> int:
        profile = self.chrome_profile
        profile.mkdir(exist_ok=True)
        self._prepare_chrome_downloads(profile, Path(self.config["pdf_folder"]))
        playwright = sync_playwright().start()
        context = None
        try:
            context = self._open_chrome(playwright, profile)
            page = None
            for index in range(1, self.count + 1):
                self._check_stop()
                self._bought = False
                context, page = self._chrome_for_next(playwright, profile, context, page)
                page = self._issue_one(context, page, index)
                self._completed = index
                self.on_progress(self._completed, self.count)
                self.publish(self._mqtt_topic("topic_updated"), str(index))
                self.log(f"Ολοκληρώθηκε το {index} από {self.count}.")
        finally:
            if self._completed == self.count:
                self.log("Ολοκληρώθηκαν όλα τα παράβολα. Τώρα κλείνει το Chrome.")
            self._close_chrome(context, playwright)
        return self._completed

    def _open_chrome(self, playwright, profile: Path) -> BrowserContext:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel="chrome",
                headless=False,
                accept_downloads=False,
                no_viewport=True,
                args=[
                    "--start-minimized",
                ],
            )
        except Exception as failure:
            text = str(failure)
            if "chrome" in text.lower() or "executable" in text.lower():
                raise RuntimeError("Δεν βρέθηκε εγκατεστημένο το Google Chrome.") from failure
            raise
        context.add_init_script(
            "window.print = () => { window.__printRequested = true; };"
            "window.close = () => {};"
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(20000)
        self._minimize(page)
        self._close_blank_tabs(context, page)
        return context

    def _close_blank_tabs(self, context: BrowserContext, keep: Page) -> None:
        for item in list(context.pages):
            if item == keep or item.is_closed():
                continue
            try:
                url = item.url or ""
            except Exception:
                continue
            if url in ("", "about:blank", "chrome://newtab/"):
                try:
                    item.close()
                except Exception:
                    pass

    def _chrome_for_next(self, playwright, profile: Path,
                         context: BrowserContext | None, page: Page | None) -> tuple[BrowserContext, Page]:
        if self._browser_alive(context):
            try:
                return context, self._working_page(context, page)
            except Exception as failure:
                if not self._browser_closed(failure):
                    raise
        self.log("Το Chrome έκλεισε. Ανοίγω νέο και συνεχίζω.")
        self._close_context(context)
        context = self._open_chrome(playwright, profile)
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(20000)
        return context, page

    def _browser_closed(self, failure: Exception) -> bool:
        text = str(failure).lower()
        return "has been closed" in text or "target closed" in text or "browser has been closed" in text

    def _browser_alive(self, context: BrowserContext | None) -> bool:
        if context is None:
            return False
        try:
            browser = context.browser
            if browser is not None and not browser.is_connected():
                return False
            for item in context.pages:
                if self._page_usable(item):
                    return True
            return False
        except Exception:
            return False

    def _close_context(self, context: BrowserContext | None) -> None:
        if context is None:
            return
        try:
            context.close()
        except Exception:
            pass

    def _close_chrome(self, context: BrowserContext | None, playwright) -> None:
        self._close_context(context)
        try:
            playwright.stop()
        except Exception:
            pass

    def _working_page(self, context: BrowserContext, page: Page | None) -> Page:
        if self._page_usable(page):
            return page
        if page is not None:
            self.log("Η προηγούμενη καρτέλα είχε κλείσει. Συνεχίζω στο ίδιο Chrome.")
        for candidate in context.pages:
            if self._page_usable(candidate):
                return candidate
        self.log("Δεν έμεινε ανοιχτή καρτέλα. Ανοίγω νέα στο ίδιο Chrome.")
        if not self._can_open_page(context):
            raise RuntimeError("Target page, context or browser has been closed")
        opened = context.new_page()
        opened.set_default_timeout(20000)
        return opened

    def _can_open_page(self, context: BrowserContext) -> bool:
        try:
            browser = context.browser
            if browser is not None and not browser.is_connected():
                return False
            context.pages
            return True
        except Exception:
            return False

    def _page_usable(self, page: Page | None) -> bool:
        if page is None:
            return False
        try:
            if page.is_closed():
                return False
            page.evaluate("1")
            return True
        except Exception:
            return False

    def _issue_one(self, context: BrowserContext, page: Page, index: int) -> Page:
        self.log(f"Παράβολο {index} από {self.count}.")
        page = self._working_page(context, page)
        self._open_form(page)
        page = self._working_page(context, page)
        self._fill(page)
        self._submit(page, index)
        page = self._working_page(context, page)
        self._confirm(page)
        page = self._working_page(context, page)
        folder = Path(self.config["pdf_folder"])
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / self.pdf_name
        self._more_after = index < self.count
        try:
            page = self._save_receipt(context, page, dest)
        except Exception as failure:
            self._discard_partial_downloads(folder)
            raise RuntimeError(f"Η αποθήκευση σταμάτησε: {failure}") from failure
        if not dest.is_file():
            self._discard_partial_downloads(folder)
            raise RuntimeError("Το κουμπί Εκτύπωση Παραβόλου δεν έγραψε PDF.")
        try:
            renamed = self._rename_to_rf(dest)
            renamed = self._publish_pdf(renamed)
            renamed = self._keep_in_company_folder(renamed)
        except Exception as failure:
            self.log(f"Η μετονομασία/αποθήκευση απέτυχε: {failure}")
            renamed = dest
        if renamed.name.lower() == self.pdf_name.lower() or not renamed.name.upper().startswith("RF"):
            raise RuntimeError(f"Το {renamed.name} δεν πήρε κωδικό RF.")
        if not self._save_to_dikastiria(renamed):
            raise RuntimeError(f"Το {renamed.name} δεν καταχωρήθηκε στο paravola_dikastiria.")
        # Δικαστήριο: το PDF μένει ΜΟΝΟ στο C:\test\<εταιρεία> — χωρίς share κληρονομικών.
        return page

    def _minimize(self, page: Page) -> None:
        try:
            session = page.context.new_cdp_session(page)
            window = session.send("Browser.getWindowForTarget")
            session.send(
                "Browser.setWindowBounds",
                {"windowId": window["windowId"], "bounds": {"windowState": "minimized"}},
            )
        except Exception as failure:
            self.log(f"Το παράθυρο δεν ελαχιστοποιήθηκε: {failure}")

    def _open_form(self, page: Page) -> None:
        new_button = page.locator("input[value='Νέο παράβολο'], button:has-text('Νέο παράβολο')")
        if new_button.count() and new_button.first.is_visible():
            new_button.first.evaluate("element => element.click()")
            page.wait_for_load_state("domcontentloaded")
        else:
            page.goto(self.config["create_url"], wait_until="domcontentloaded", timeout=30000)
        self._minimize(page)
        if page.locator("#paravCode").count() == 0:
            self.log("Δεν είσαι συνδεδεμένη στη σελίδα. Κάνε σύνδεση στο Chrome. Περιμένω μέχρι να ανοίξει η φόρμα.")
            try:
                page.locator("#paravCode").wait_for(state="visible", timeout=180000)
            except PlaywrightTimeout as failure:
                raise RuntimeError("Η σύνδεση στη σελίδα δεν ολοκληρώθηκε.") from failure
        grant = page.locator("input[value='Χορήγηση Παραβόλου']")
        if grant.count() and grant.first.is_visible():
            grant.first.evaluate("element => element.click()")
            page.wait_for_load_state("domcontentloaded")
        page.locator("#paravCode").wait_for(state="visible")

    def _fill(self, page: Page) -> None:
        code = str(self.config["paravolo_code"])
        self._fill_field(page, "#paravCode", code)
        page.locator("#searchByCode").evaluate("element => element.click()")
        page.wait_for_function(
            """() => {
                const type = document.querySelector('#typeId');
                const waiting = document.body.innerText.includes('Παρακαλώ Περιμένετε');
                return !waiting && type && type.value;
            }""",
            timeout=20000,
        )
        selected = page.locator("#typeId option:checked").inner_text().strip()
        self.log(f"Τύπος: {selected}")

        try:
            page.wait_for_function(
                """() => {
                    const price = document.querySelector('#inputPrice, #priceVariable');
                    return price && price.offsetParent !== null;
                }""",
                timeout=15000,
            )
        except PlaywrightTimeout:
            detail = ""
            errors = page.locator(".error-message")
            if errors.count():
                detail = errors.first.inner_text().strip()
            raise RuntimeError(detail or "Η σελίδα δεν έδειξε πεδίο ποσού για αυτόν τον κωδικό.")

        selector = self._price_selector(page)
        self._fill_field(page, selector, self.price)
        hidden = page.locator("#paravPrice")
        if hidden.count():
            hidden.evaluate("(element, value) => { element.value = value; }", self.price)

        self._fill_field(page, "#afmid", self.company["afm"])
        self._fill_field(page, "#lastNameid", self.company["last_name"])
        self._fill_if_visible(page, "#firstNameid", self.company["first_name"])
        self._fill_if_visible(page, "#fatherNameid", self.company["father_name"])
        self._fill_field(page, "#emailid", self.config["email"])
        self._fill_field(page, "#emailConfirdid", self.config["email"])

    def _submit(self, page: Page, index: int) -> None:
        heading = f"{index} από {self.count}  ·  {self.company_code}  ·  {self.price} €"
        for attempt in range(1, 6):
            self._check_stop()
            code = self._ask_captcha(page, heading)
            self._fill_field(page, "#resultN", code)
            self.captcha_text.unlink(missing_ok=True)
            self.captcha_image.unlink(missing_ok=True)
            page.locator(SUBMIT).first.evaluate("element => element.click()")
            page.wait_for_timeout(1000)
            page.wait_for_load_state("domcontentloaded")
            text = page.locator("body").inner_text()
            if "Λάθος εισαγωγή" in text:
                self.log(f"Λάθος captcha, προσπάθεια {attempt} από 5.")
                last_name = page.locator("#lastNameid")
                if last_name.count() == 0 or not last_name.input_value().strip():
                    self._fill(page)
                continue
            self._bought = True
            if page.locator(CONFIRM).count() or page.locator(PRINT).count():
                return
            errors = [
                item.strip()
                for item in page.locator(".server-error-message, .error-message").all_inner_texts()
                if item.strip()
            ]
            if errors:
                raise RuntimeError("Η σελίδα επέστρεψε: " + " | ".join(errors))
            return
        raise RuntimeError("Το captcha απορρίφθηκε 5 φορές.")

    def _ask_captcha(self, page: Page, heading: str) -> str:
        del heading
        image = page.locator("#captchaImage")
        image.wait_for(state="visible")
        self.captcha_image.parent.mkdir(parents=True, exist_ok=True)

        for attempt in range(1, 4):
            self._check_stop()
            self.captcha_image.write_bytes(image.screenshot())
            self.log(f"Captcha για Windows OCR, δοκιμή {attempt}.")
            code = self._windows_ocr()
            if code:
                self.log(f"Windows OCR: {code}")
                return code
            if attempt < 3:
                self.log("Το Windows OCR δεν έβγαλε αριθμό. Ζητάω νέο captcha.")
                image.evaluate(
                    "element => { element.src = element.src.split('?')[0] + '?' + Date.now(); }"
                )
                page.wait_for_timeout(900)
                image = page.locator("#captchaImage")
                image.wait_for(state="visible")
        raise RuntimeError("Το Windows OCR δεν διάβασε το captcha.")

    def _windows_ocr(self) -> str:
        script = Path(__file__).resolve().parent / "read_captcha.ps1"
        preprocess = Path(__file__).resolve().parent / "preprocess_captcha.ps1"
        if not script.is_file():
            return ""

        for mode in ("plain", "threshold", "invert"):
            variant = self.captcha_image.with_name(f"captcha_ocr_{mode}.png")
            if preprocess.is_file():
                run_command(
                    [
                        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(preprocess),
                        "-SourcePath", str(self.captcha_image),
                        "-DestinationPath", str(variant),
                        "-Mode", mode,
                    ],
                    timeout=30,
                    capture_output=True,
                )
            else:
                self._enlarge_copy(self.captcha_image, variant)

            if self.captcha_text.exists():
                self.captcha_text.unlink()
            completed = run_command(
                [
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(script),
                    "-ImagePath", str(variant),
                    "-ResultPath", str(self.captcha_text),
                ],
                timeout=40,
                capture_output=True,
            )
            digits = self._digits_from_file(remove=False)
            variant.unlink(missing_ok=True)
            if digits:
                self.log(f"Windows OCR ({mode}): {digits}")
                return digits
            detail = (completed.stderr or completed.stdout or "").strip()
            if detail:
                self.log(f"Windows OCR ({mode}): κενό. {detail[:200]}")

        return ""

    def _enlarge_copy(self, source: Path, destination: Path) -> None:
        command = (
            "Add-Type -AssemblyName System.Drawing; "
            f"$src = [System.Drawing.Image]::FromFile('{source}'); "
            "$w = [Math]::Max(300, $src.Width * 6); "
            "$h = [Math]::Max(100, $src.Height * 6); "
            "$dst = New-Object System.Drawing.Bitmap $w, $h; "
            "$g = [System.Drawing.Graphics]::FromImage($dst); "
            "$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor; "
            "$g.Clear([System.Drawing.Color]::White); "
            "$g.DrawImage($src, 0, 0, $w, $h); "
            "$src.Dispose(); $g.Dispose(); "
            f"$dst.Save('{destination}'); $dst.Dispose()"
        )
        run_command(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            timeout=20,
            capture_output=True,
        )

    def _digits_from_file(self, remove: bool = False) -> str:
        if not self.captcha_text.exists():
            return ""
        raw = self.captcha_text.read_text(encoding="utf-8", errors="ignore")
        digits = self._normalize_digits(raw)
        if remove and digits:
            self.captcha_text.unlink(missing_ok=True)
        return digits

    def _normalize_digits(self, text: str) -> str:
        mapped = []
        for character in text.upper():
            if character.isdigit():
                mapped.append(character)
            elif character in "OQD":
                mapped.append("0")
            elif character in "IL|":
                mapped.append("1")
            elif character in "Z":
                mapped.append("2")
            elif character in "S":
                mapped.append("5")
            elif character in "B":
                mapped.append("8")
            elif character in "G":
                mapped.append("6")
        return "".join(mapped)[:5]

    def _confirm(self, page: Page) -> None:
        button = page.locator(CONFIRM)
        if button.count() == 0:
            return
        button.first.wait_for(state="visible", timeout=20000)
        button.first.evaluate("element => element.click()")
        page.wait_for_load_state("domcontentloaded")
        yes = page.locator(".ui-dialog-buttonset button:has-text('Ναι')")
        if yes.count() and yes.first.is_visible():
            yes.first.evaluate("element => element.click()")
            page.wait_for_load_state("domcontentloaded")

    def _prepare_chrome_downloads(self, profile: Path, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        prefs_path = profile / "Default" / "Preferences"
        if not prefs_path.is_file():
            return
        data = json.loads(prefs_path.read_text(encoding="utf-8"))
        download = data.setdefault("download", {})
        download["default_directory"] = str(folder)
        download["prompt_for_download"] = False
        download["directory_upgrade"] = True
        data.setdefault("plugins", {})["always_open_pdf_externally"] = False
        prefs_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _save_receipt(self, context: BrowserContext, page: Page, dest: Path) -> Page:
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = self._working_page(context, page)
        if not self._download_official_pdf(target, dest):
            raise RuntimeError("Το κουμπί Εκτύπωση Παραβόλου δεν έγραψε PDF στο C:\\test.")
        self.log(f"Αποθηκεύτηκε το παράβολο στο {dest}")
        if getattr(self, "_more_after", False) and self._page_usable(target):
            target = self._return_to_start(context, target)
        return target

    def _return_to_start(self, context: BrowserContext, page: Page | None) -> Page:
        if not self._page_usable(page):
            self.log("Το Chrome έκλεισε μόνο του. Το επόμενο παράβολο ανοίγει σε νέο Chrome.")
            return page
        new_button = page.locator("input[value='Νέο παράβολο'], button:has-text('Νέο παράβολο')")
        if new_button.count() and new_button.first.is_visible():
            self.log("Επιστροφή με Νέο παράβολο.")
            try:
                new_button.first.evaluate("element => element.click()")
                page.wait_for_load_state("domcontentloaded")
            except Exception:
                self.log("Το Chrome έκλεισε. Το επόμενο παράβολο ανοίγει σε νέο Chrome.")
            return page
        self.log("Επιστροφή στη σελίδα έναρξης.")
        try:
            page.goto(self.config["create_url"], wait_until="domcontentloaded", timeout=30000)
        except Exception:
            self.log("Το Chrome έκλεισε. Το επόμενο παράβολο ανοίγει σε νέο Chrome.")
        return page

    def _pdf_without_leaving(self, page: Page, dest: Path) -> bool:
        """Κατεβάζει το PDF μέσα από τη σελίδα, χωρίς να την κλείσει ή να την αλλάξει."""
        try:
            payload = page.evaluate(
                """async () => {
                    const form = document.querySelector('#paravolaActionForm');
                    if (!form) return null;
                    const params = new URLSearchParams();
                    for (const element of form.elements) {
                        if (!element.name || element.disabled) continue;
                        if ((element.type === 'checkbox' || element.type === 'radio') && !element.checked) continue;
                        params.append(element.name, element.value);
                    }
                    const button = document.querySelector('#normalPrint');
                    if (button && button.name) params.set(button.name, button.value);
                    const response = await fetch(form.action, {
                        method: 'POST',
                        credentials: 'include',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: params.toString()
                    });
                    const bytes = new Uint8Array(await response.arrayBuffer());
                    let binary = '';
                    for (let index = 0; index < bytes.length; index += 4096) {
                        binary += String.fromCharCode(...bytes.subarray(index, index + 4096));
                    }
                    return {
                        status: response.status,
                        type: response.headers.get('content-type') || '',
                        b64: btoa(binary)
                    };
                }"""
            )
        except Exception as failure:
            self.log(f"Η εκτύπωση χωρίς αλλαγή σελίδας δεν έγινε: {failure}")
            return False
        if not payload or not payload.get("b64"):
            return False
        data = base64.b64decode(payload["b64"])
        if not data.startswith(b"%PDF-"):
            self.log(
                "Η σελίδα έμεινε ανοιχτή, αλλά η εκτύπωση δεν έδωσε PDF "
                f"({payload.get('status')}, {payload.get('type')})."
            )
            return False
        self._store_pdf(dest, data)
        self.log(f"Το {dest.name} αποθηκεύτηκε και η σελίδα έμεινε ανοιχτή.")
        return True

    def _download_official_pdf(self, page: Page, dest: Path) -> bool:
        button = page.locator("#normalPrint")
        if button.count() == 0:
            button = page.locator("button:has-text('Εκτύπωση Παραβόλου')")
        if button.count() == 0:
            self.log("Δεν βρέθηκε το κουμπί Εκτύπωση Παραβόλου.")
            return False
        self.log("Πατάω Εκτύπωση Παραβόλου.")
        if self._pdf_without_leaving(page, dest):
            return True
        folder = dest.parent
        started = time.time()
        before = {item.resolve(): item.stat().st_mtime for item in folder.glob("*.pdf")}
        self.log("Περιμένω να αποθηκευτεί το PDF. Η σελίδα δεν αλλάζει ακόμα.")
        try:
            session = page.context.new_cdp_session(page)
            session.send(
                "Browser.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": str(folder), "eventsEnabled": True},
            )
        except Exception as failure:
            self.log(f"Ο φάκελος λήψεων δεν ορίστηκε: {failure}")
        try:
            page.evaluate(
                """() => {
                    const form = document.querySelector('#paravolaActionForm');
                    if (!form) return;
                    let frame = document.getElementById('pdfFrame');
                    if (!frame) {
                        frame = document.createElement('iframe');
                        frame.id = 'pdfFrame';
                        frame.name = 'pdfFrame';
                        frame.style.display = 'none';
                        document.body.appendChild(frame);
                    }
                    Object.defineProperty(form, 'target', {
                        configurable: true,
                        get() { return 'pdfFrame'; },
                        set() {}
                    });
                    form.setAttribute('target', 'pdfFrame');
                }"""
            )
        except Exception as failure:
            self.log(f"Η εκτύπωση δεν έμεινε στην ίδια σελίδα: {failure}")
        try:
            button.first.evaluate("element => element.click()")
        except Exception as failure:
            self.log(f"Το κουμπί δεν πατήθηκε: {failure}")
            return False
        if self._page_usable(page):
            self.log("Η σελίδα έμεινε ανοιχτή μετά την εκτύπωση.")
        else:
            self.log("Η σελίδα έκλεισε μόλις πατήθηκε η εκτύπωση.")
        seen: dict[Path, tuple[int, float]] = {}
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            self._check_stop()
            partial = self._pending_partial(folder, started)
            if partial is None:
                fresh = self._fresh_pdf(folder, before, started)
                if fresh is not None and self._settled(fresh, seen) and self._complete_pdf(fresh):
                    if fresh.resolve() != dest.resolve():
                        self._replace_pdf(dest)
                        shutil.copyfile(fresh, dest)
                    self._stamp_created_today(dest)
                    self.log(f"Το κουμπί έσωσε το {dest.name}.")
                    return self._valid_paravolo_pdf(dest)
            elif self._settled(partial, seen) and self._complete_pdf(partial):
                if self._take_partial(partial, dest) and not partial.exists():
                    self.log(f"Το κουμπί έσωσε το {dest.name}.")
                    return self._valid_paravolo_pdf(dest)
            time.sleep(0.5)
        partial = self._pending_partial(folder, started)
        if partial is not None and self._complete_pdf(partial) and self._take_partial(partial, dest):
            if not partial.exists():
                self.log(f"Το κουμπί έσωσε το {dest.name}.")
                return self._valid_paravolo_pdf(dest)
        self._discard_partial_downloads(folder)
        self.log("Το κουμπί Εκτύπωση Παραβόλου δεν έγραψε PDF στο C:\\test.")
        return False

    def _pending_partial(self, folder: Path, started: float) -> Path | None:
        newest = None
        for item in folder.glob("*.crdownload"):
            try:
                modified = item.stat().st_mtime
            except OSError:
                continue
            if modified < started - 1:
                continue
            if newest is None or modified > newest.stat().st_mtime:
                newest = item
        return newest

    def _settled(self, path: Path, seen: dict) -> bool:
        try:
            stat = path.stat()
        except OSError:
            return False
        current = (stat.st_size, stat.st_mtime_ns)
        previous = seen.get(path.resolve())
        seen[path.resolve()] = current
        return previous == current and stat.st_size >= 1000

    def _complete_pdf(self, path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                head = handle.read(5)
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 2048))
                tail = handle.read()
        except OSError:
            return False
        return head == b"%PDF-" and b"%%EOF" in tail

    def _take_partial(self, partial: Path, dest: Path) -> bool:
        """Το Chrome τελείωσε το PDF αλλά το άφησε .crdownload. Γίνεται κανονικό PDF και σβήνεται το προσωρινό."""
        try:
            data = partial.read_bytes()
        except OSError:
            return False
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
            return False
        self._replace_pdf(dest)
        dest.write_bytes(data)
        self._stamp_created_today(dest)
        try:
            partial.unlink()
        except OSError:
            return False
        return True

    def _discard_partial_downloads(self, folder: Path) -> None:
        for item in folder.glob("*.crdownload"):
            try:
                item.unlink()
            except OSError as failure:
                self.log(f"Δεν σβήστηκε το προσωρινό {item.name}: {failure}")

    def _fresh_pdf(self, folder: Path, before: dict, started: float) -> Path | None:
        newest = None
        for item in folder.glob("*.pdf"):
            try:
                modified = item.stat().st_mtime
                size = item.stat().st_size
            except OSError:
                continue
            if modified < started - 1 or modified <= before.get(item.resolve(), 0) or size < 1000:
                continue
            if item.read_bytes()[:4] != b"%PDF":
                continue
            if newest is None or modified > newest.stat().st_mtime:
                newest = item
        return newest

    def _valid_paravolo_pdf(self, dest: Path) -> bool:
        if not dest.is_file() or dest.stat().st_size < 1000:
            self.log("Το αρχείο από το κουμπί είναι κενό.")
            return False
        if not dest.read_bytes().startswith(b"%PDF"):
            self.log("Το αρχείο από το κουμπί δεν είναι PDF παραβόλου.")
            return False
        return True

    def _post_pdf_form(self, page: Page, dest: Path) -> bool:
        payload = page.evaluate(
            """() => {
                const form = document.querySelector('#paravolaActionForm');
                if (!form) return null;
                const fields = {};
                for (const element of form.elements) {
                    if (element.name) fields[element.name] = element.value;
                }
                if ('printParavoloForForea' in fields) fields.printParavoloForForea = '';
                return {action: form.action, fields};
            }"""
        )
        if not payload:
            return False
        response = page.context.request.post(payload["action"], form=payload["fields"])
        body = response.body()
        if not response.ok or not body.startswith(b"%PDF"):
            kind = response.headers.get("content-type", "")
            self.log(f"Το downloadPdf.htm δεν έδωσε PDF ({response.status}, {kind}).")
            return False
        self._store_pdf(dest, body)
        return True

    def _write_pdf(self, page: Page, dest: Path) -> None:
        session = page.context.new_cdp_session(page)
        result = session.send("Page.printToPDF", {"printBackground": True, "preferCSSPageSize": True})
        self._store_pdf(dest, base64.b64decode(result["data"]))

    def _store_pdf(self, dest: Path, data: bytes) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._replace_pdf(dest)
        dest.write_bytes(data)
        self._stamp_created_today(dest)

    def _store_download(self, download, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._replace_pdf(dest)
        source = None
        try:
            source = download.path()
        except Exception:
            source = None
        if source and Path(source).is_file():
            shutil.copyfile(source, dest)
        else:
            download.save_as(str(dest))
        self._stamp_created_today(dest)

    def _replace_pdf(self, dest: Path) -> None:
        if not dest.exists():
            return
        try:
            dest.unlink()
        except OSError as failure:
            self.log(f"Το προηγούμενο PDF δεν σβήστηκε: {failure}")

    def _stamp_created_today(self, dest: Path) -> None:
        """Το EParavoloExtractor αγνοεί PDF που δεν δημιουργήθηκαν σήμερα."""
        if not dest.is_file():
            return
        command = (
            f"$item = Get-Item -LiteralPath '{dest}'; "
            "$now = Get-Date; "
            "$item.CreationTime = $now; "
            "$item.LastWriteTime = $now"
        )
        completed = run_command(
            ["powershell", "-NoProfile", "-Command", command],
            timeout=20,
            capture_output=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            self.log(f"Η ημερομηνία δημιουργίας του PDF δεν ενημερώθηκε. {detail[:200]}")

    def _rename_to_rf(self, pdf_path: Path) -> Path:
        if not pdf_path.is_file():
            self.log(f"Δεν υπάρχει το {pdf_path.name} για μετονομασία.")
            return pdf_path
        codes = self._rf_codes_from_pdf(pdf_path)
        if not codes:
            self.log("Δεν βρέθηκε RF στο PDF. Μένει το όνομα eparavolo.pdf.")
            return pdf_path
        target = pdf_path.with_name(f"{codes[0]}.pdf")
        if target.exists() and target.resolve() != pdf_path.resolve():
            self.log(f"Υπάρχει ήδη το {target.name}. Δεν αντικαθίσταται.")
            return pdf_path
        pdf_path.replace(target)
        self._stamp_created_today(target)
        self.log(f"Το {pdf_path.name} μετονομάστηκε σε {target.name}.")
        return target

    def _rf_codes_from_pdf(self, path: Path) -> list[str]:
        if not path.is_file():
            return []
        exe = Path(str(self.extractor.get("exe", "")))
        dll = Path(str(self.extractor.get("workdir") or exe.parent)) / "UglyToad.PdfPig.dll"
        if not dll.is_file():
            return []
        script = (
            f"Add-Type -Path '{dll}'; "
            f"$doc = [UglyToad.PdfPig.PdfDocument]::Open('{path}'); "
            "$sb = New-Object System.Text.StringBuilder; "
            "foreach ($page in $doc.GetPages()) { [void]$sb.AppendLine($page.Text) }; "
            "$doc.Dispose(); "
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Write-Output $sb.ToString()"
        )
        completed = run_command(
            ["powershell", "-NoProfile", "-Command", script],
            timeout=40,
            capture_output=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            self.log(f"Δεν διαβάστηκε ο κωδικός RF από το PDF. {detail[:200]}")
            return []
        return self._rf_codes_from_text(completed.stdout or "")

    def _rf_codes_from_text(self, text: str) -> list[str]:
        found: list[str] = []
        for match in re.finditer(r"RF(?:[\s\u00a0]*\d){20,25}", text, re.IGNORECASE):
            compact = re.sub(r"\s+", "", match.group(0)).upper()
            if compact not in found:
                found.append(compact)
        return found

    def _should_copy_to_share(self) -> bool:
        """Δικαστήριο: ποτέ προώθηση στο share κληρονομικών."""
        return False

    def _company_test_folder(self) -> Path:
        """C:\\test\\<εταιρεία> — βασικός προορισμός PDF."""
        raw = (self.company_code or "UNKNOWN").strip() or "UNKNOWN"
        safe = re.sub(r'[<>:"/\\|?*]', "_", raw).strip(" .") or "UNKNOWN"
        folder = TEST_ROOT / safe
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _publish_pdf(self, pdf_path: Path) -> Path:
        """Από slot/temp φάκελο → C:\\test (ρίζα), πριν μπει στον φάκελο εταιρείας."""
        handoff = self.config.get("handoff_folder") or str(TEST_ROOT)
        folder = Path(handoff)
        if pdf_path.parent.resolve() == folder.resolve():
            return pdf_path
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / pdf_path.name
        shutil.copy2(pdf_path, target)
        self._stamp_created_today(target)
        try:
            pdf_path.unlink()
        except OSError:
            pass
        self.log(f"Το {target.name} πήγε στο {folder}.")
        return target

    def _keep_in_company_folder(self, pdf_path: Path) -> Path:
        """Μετακινεί το μετονομασμένο RF.pdf στο C:\\test\\<εταιρεία>\\ και το αφήνει εκεί."""
        if not pdf_path.is_file():
            raise RuntimeError(f"Δεν υπάρχει το {pdf_path.name} για αποθήκευση.")
        folder = self._company_test_folder()
        target = folder / pdf_path.name
        if pdf_path.resolve() == target.resolve():
            self.log(f"Το {pdf_path.name} είναι ήδη στο {folder}.")
            return pdf_path
        if target.exists():
            raise RuntimeError(f"Υπάρχει ήδη το {target.name} στο {folder}.")
        shutil.move(str(pdf_path), str(target))
        self._stamp_created_today(target)
        self.log(f"Το {target.name} αποθηκεύτηκε στο {folder}.")
        return target

    def _share_folder(self) -> Path:
        euro = SHARE_ROOT / euro_folder_name(self.price)
        wanted = self.company_code.strip()
        if euro.is_dir():
            for child in euro.iterdir():
                if child.is_dir() and child.name.lower() == wanted.lower():
                    return child
        folder = euro / wanted
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as failure:
            raise RuntimeError(
                f"Δεν υπάρχει φάκελος για εταιρεία {wanted} και ποσό {self.price} ({folder})."
            ) from failure
        self.log(f"Φάκελος παραβόλων (share): {folder}")
        return folder

    def _copy_to_share(self, pdf_path: Path) -> Path:
        """Αντίγραφο στο παλιό network share· το αρχείο μένει και στο C:\\test\\<εταιρεία>."""
        if not pdf_path.is_file():
            raise RuntimeError(f"Δεν υπάρχει το {pdf_path.name} για προώθηση στο share.")
        folder = self._share_folder()
        if not folder.is_dir():
            raise RuntimeError(f"Δεν βρέθηκε ο φάκελος {folder}")
        target = folder / pdf_path.name
        shutil.copy2(pdf_path, target)
        if not target.is_file() or target.stat().st_size < 1000:
            raise RuntimeError(f"Το {pdf_path.name} δεν γράφτηκε στο {folder}")
        self.log(f"Το {pdf_path.name} αντιγράφηκε και στο {folder}")
        return target

    def _parse_paravolo_pdf(self, pdf_path: Path) -> dict:
        """Διαβάζει τα πεδία του PDF μέσω EParavoloParser (ίδιο με τον extractor)."""
        exe = Path(str(self.extractor.get("exe", "")))
        workdir = Path(str(self.extractor.get("workdir") or exe.parent))
        pig = workdir / "UglyToad.PdfPig.dll"
        if not exe.is_file() or not pig.is_file():
            raise RuntimeError(f"Λείπει ο parser παραβόλου: {exe}")
        out_json = self.work_folder / f"paravolo-parse-{os.getpid()}-{time.time_ns()}.json"
        pdf_literal = str(pdf_path).replace("'", "''")
        exe_literal = str(exe).replace("'", "''")
        pig_literal = str(pig).replace("'", "''")
        out_literal = str(out_json).replace("'", "''")
        script = (
            f"Add-Type -Path '{pig_literal}'; "
            f"$asm = [System.Reflection.Assembly]::LoadFrom('{exe_literal}'); "
            "$parser = $asm.GetType('EParavoloExtractor.EParavoloParser'); "
            f"$r = $parser.GetMethod('Parse').Invoke($null, @('{pdf_literal}')); "
            "$o = [ordered]@{"
            "KodikosPliromis = [string]$r.KodikosPliromis; "
            "KodikosParavolou = [string]$r.KodikosParavolou; "
            "Foreas = [string]$r.Foreas; "
            "KatigoriaParavolou = [string]$r.KatigoriaParavolou; "
            "TyposParavolou = [string]$r.TyposParavolou; "
            "Poso = [string]$r.PosoNumeric; "
            "Afm = [string]$r.Afm"
            "}; "
            "$json = $o | ConvertTo-Json -Compress; "
            f"$utf8 = New-Object System.Text.UTF8Encoding $false; "
            f"[System.IO.File]::WriteAllText('{out_literal}', $json, $utf8)"
        )
        try:
            completed = run_command(
                ["powershell", "-NoProfile", "-Command", script],
                timeout=60,
                capture_output=True,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(f"Αποτυχία ανάγνωσης PDF: {detail[:300]}")
            if not out_json.is_file():
                raise RuntimeError("Ο parser PDF δεν έγραψε JSON.")
            data = json.loads(out_json.read_text(encoding="utf-8"))
        finally:
            try:
                out_json.unlink()
            except OSError:
                pass
        parsed = {
            key: ("" if value is None else str(value).strip())
            for key, value in data.items()
        }
        if not parsed.get("KatigoriaParavolou"):
            # Μερικά PDF δεν δίνουν κατηγορία στον parser· για 1375 είναι σταθερή.
            code = str(self.config.get("paravolo_code", "")).strip()
            if code == "1375" or "[ 1375 ]" in parsed.get("TyposParavolou", ""):
                parsed["KatigoriaParavolou"] = "Ένσημο ΤΑ.Χ.ΔΙ.Κ. (Μεγαρόσημο)"
        if not parsed.get("Foreas"):
            parsed["Foreas"] = "Δικαιοσύνης"
        if not parsed.get("Afm"):
            parsed["Afm"] = str(self.company.get("afm") or "").strip()
        if not parsed.get("Poso"):
            parsed["Poso"] = self.price.replace(".", ",")
        return parsed

    def _save_to_dikastiria(self, pdf_path: Path) -> bool:
        """Καταχώρηση στο [dbo].[paravola_dikastiria] για όλες τις εταιρείες (και CEPAL/DOVALUE)."""
        if not pdf_path.is_file():
            self.log(f"Δεν υπάρχει το {pdf_path.name} για καταχώρηση.")
            return False
        self._stamp_created_today(pdf_path)
        try:
            parsed = self._parse_paravolo_pdf(pdf_path)
        except Exception as failure:
            self.log(str(failure))
            return False
        rf = re.sub(r"\s+", "", parsed.get("KodikosPliromis", "")).upper()
        kodikos = re.sub(r"\s+", "", parsed.get("KodikosParavolou", ""))
        if not rf.upper().startswith("RF"):
            self.log(f"Δεν βρέθηκε έγκυρο RF στο {pdf_path.name}.")
            return False
        try:
            publish = datetime.fromtimestamp(pdf_path.stat().st_ctime).date()
        except OSError:
            publish = datetime.now().date()
        row = {
            "KodikosPliromis": rf,
            "KodikosParavolou": kodikos,
            "Foreas": parsed.get("Foreas", ""),
            "KatigoriaParavolou": parsed.get("KatigoriaParavolou", ""),
            "TyposParavolou": parsed.get("TyposParavolou", ""),
            "Poso": parsed.get("Poso", ""),
            "Afm": parsed.get("Afm", ""),
            "for_legal_action": "",
            "Publish_DATE": publish,
            "used_on_date": None,
        }
        try:
            insert_paravolo_dikastiria(self.config, row)
        except Exception as failure:
            self.log(str(failure))
            return False
        self.log(f"Το {pdf_path.name} καταχωρήθηκε στο paravola_dikastiria · μένει στο {pdf_path.parent}.")
        return True

    def _price_selector(self, page: Page) -> str:
        for selector in PRICE_SELECTORS:
            locator = page.locator(selector)
            if locator.count() and locator.first.is_visible():
                return selector
        raise RuntimeError("Δεν βρέθηκε το πεδίο του ποσού.")

    def _fill_field(self, page: Page, selector: str, value: str) -> None:
        locator = page.locator(selector).first
        locator.wait_for(state="visible")
        locator.fill(value)

    def _fill_if_visible(self, page: Page, selector: str, value: str) -> None:
        locator = page.locator(selector)
        if locator.count() and locator.first.is_visible():
            locator.first.fill(value)

    def _check_stop(self) -> None:
        if self.should_stop():
            raise Stopped()

    def _mqtt_topic(self, key: str) -> str:
        mqtt = self.config["mqtt"]
        template = str(mqtt.get(key, "{company}/agores"))
        return template.replace("{company}", self.company_code)

    def publish(self, topic: str, message: str) -> None:
        if not self.publish_mqtt:
            return
        mqtt = self.config["mqtt"]
        if not mqtt.get("enabled", False):
            return
        command = [
            "mosquitto_pub",
            "-h", str(mqtt["host"]),
            "-p", str(mqtt["port"]),
            "-t", topic,
            "-m", message,
        ]
        try:
            completed = run_command(command, capture_output=True, timeout=15)
        except FileNotFoundError:
            if not self.mqtt_warned:
                self.log("Το mosquitto_pub δεν βρέθηκε. Το MQTT παραλείπεται.")
                self.mqtt_warned = True
            return
        except subprocess.TimeoutExpired:
            self.log(f"Το MQTT στο {topic} δεν απάντησε.")
            return
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            self.log(f"Το MQTT στο {topic} απέτυχε: {detail}")

    def log(self, message: str) -> None:
        line = f"{datetime.now():%H:%M:%S}  {message}"
        self.log_path.mkdir(exist_ok=True)
        with _LOG_LOCK:
            with (self.log_path / f"{datetime.now():%Y-%m-%d}.log").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        self.on_log(line)


def _split_counts(total: int, slots: int) -> list[int]:
    base, extra = divmod(total, slots)
    return [base + (1 if index < extra else 0) for index in range(slots)]


def run_purchases(
    config: dict,
    company_code: str,
    price: str,
    count: int,
    on_log,
    on_captcha,
    on_progress,
    should_stop,
    company_profile: dict | None = None,
    slots: int | None = None,
    isolate_profile: bool = False,
) -> tuple[int, str | None]:
    """Αγοράζει τα παράβολα. Πάνω από ένα slot ανοίγει ξεχωριστό Chrome το καθένα."""
    workers = slots if slots is not None else parallel_slots(config, count)
    workers = max(1, min(count, workers))
    if workers == 1 and not isolate_profile:
        issuer = Issuer(
            config,
            company_code,
            price,
            count,
            on_log,
            on_captcha,
            on_progress,
            should_stop,
            company_profile=company_profile,
        )
        return issuer.run()

    profile_root = Path(__file__).resolve().parent / "chrome-profile"
    run_root = Path(__file__).resolve().parent / "chrome-runs" / f"{os.getpid()}-{time.time_ns()}"
    handoff = config.get("pdf_folder", r"C:\test")
    sizes = _split_counts(count, workers)
    done = [0] * workers
    errors: list[str] = []
    lock = threading.Lock()
    clones: list[Path] = []

    speaker = Issuer(
        config,
        company_code,
        price,
        count,
        on_log,
        on_captcha,
        lambda *_args: None,
        should_stop,
        company_profile=company_profile,
    )
    speaker.publish(speaker._mqtt_topic("topic_total"), str(count))

    def progress(slot: int, index: int, _slot_total: int) -> None:
        with lock:
            done[slot] = index
            overall = sum(done)
        on_progress(overall, count)
        speaker.publish(speaker._mqtt_topic("topic_updated"), str(overall))

    def work(slot: int, size: int) -> None:
        if size < 1:
            return
        slot_dir = run_root / f"slot-{slot}"
        slot_dir.mkdir(parents=True, exist_ok=True)
        clone = slot_dir / "chrome-profile"
        try:
            clone_chrome_profile(profile_root, clone)
        except Exception as failure:
            with lock:
                errors.append(f"Το Chrome {slot + 1} δεν αντιγράφηκε: {failure}")
            return
        clones.append(clone)
        child_config = copy.deepcopy(config)
        child_config["pdf_folder"] = str(slot_dir)
        child_config["handoff_folder"] = handoff
        child_config["work_folder"] = str(slot_dir)

        def on_slot_progress(index: int, total: int) -> None:
            progress(slot, index, total)

        issuer = Issuer(
            child_config,
            company_code,
            price,
            size,
            on_log,
            on_captcha,
            on_slot_progress,
            should_stop,
            company_profile=company_profile,
            chrome_profile=clone,
            publish_mqtt=False,
        )
        _completed, error = issuer.run()
        if error:
            with lock:
                errors.append(error)

    threads = [
        threading.Thread(target=work, args=(slot, size), daemon=True)
        for slot, size in enumerate(sizes)
        if size > 0
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    shutil.rmtree(run_root, ignore_errors=True)
    completed = sum(done)
    if not errors:
        return completed, None
    if should_stop():
        return completed, f"Διακόπηκε μετά από {completed} από {count}."
    return completed, errors[0]
