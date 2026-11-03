# -*- coding: utf-8 -*-
"""Κατάσταση αγοράς παραβόλων κληρονομικών στον ticket_scheduler_for_automation.

Action: KLIRONOMIKES_AGORA_PARAVOLON.
Το πλήθος που αγοράζεται είναι το upd_amount όταν δεν είναι null/0.
Αλλιώς είναι το amount. Το amount δεν ξαναγράφεται.
Το upd_amount κατεβαίνει όσο προχωρά η αγορά και γίνεται 0 στο τέλος (όχι NULL).
Τα στοιχεία εταιρείας έρχονται από τον πίνακα companies, χωρίς διάκριση πεζών/κεφαλαίων.
Η καταχώρηση PDF γίνεται από τον extractor στο [dbo].[klironomikes_Paravola].
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pyodbc

ACTION = "KLIRONOMIKES_AGORA_PARAVOLON"
TICKETS = "[dbo].[ticket_scheduler_for_automation]"
COMPANIES = "[dbo].[companies]"

CLAIM_JOB = f"""
UPDATE {TICKETS}
SET [status] = N'running',
    [started] = ?,
    [ended] = NULL
WHERE [id] = ?
  AND LOWER([status]) = N'waiting'
"""

UPDATE_UPD_AMOUNT = f"""
UPDATE {TICKETS}
SET [upd_amount] = ?
WHERE [id] = ?
  AND LOWER([status]) = N'running'
"""

MARK_SUCCEED = f"""
UPDATE {TICKETS}
SET [status] = N'succeed',
    [ended] = ?,
    [upd_amount] = 0
WHERE [id] = ?
  AND LOWER([status]) = N'running'
"""

PARAVOLA_DIKASTIRIA = "[dbo].[paravola_dikastiria]"

EXISTS_DIKASTIRIA = f"""
SELECT COUNT(1) FROM {PARAVOLA_DIKASTIRIA} WHERE [KodikosPliromis] = ?
"""

INSERT_DIKASTIRIA = f"""
INSERT INTO {PARAVOLA_DIKASTIRIA}
([KodikosPliromis],[KodikosParavolou],[Foreas],[KatigoriaParavolou],[TyposParavolou],
 [Poso],[Afm],[for_legal_action],[Publish_DATE],[used_on_date],[id_editor_dikastiria],[dt_insert])
VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
"""

MARK_WAITING = f"""
UPDATE {TICKETS}
SET [status] = N'waiting',
    [upd_amount] = ?,
    [ended] = ?,
    [time_to_execute] = ?
WHERE [id] = ?
  AND LOWER([status]) = N'running'
"""

MARK_FAILED = f"""
UPDATE {TICKETS}
SET [status] = N'failed',
    [ended] = ?
WHERE [id] = ?
  AND LOWER([status]) = N'running'
"""


@dataclass(frozen=True)
class CompanyProfile:
    company: str
    last_name: str
    first_name: str
    father_name: str
    afm: str

    def as_dict(self) -> dict:
        return {
            "last_name": self.last_name,
            "first_name": self.first_name,
            "father_name": self.father_name,
            "afm": self.afm,
        }


@dataclass(frozen=True)
class RunningJob:
    job_id: int
    company: str
    price: str
    count: int
    profile: CompanyProfile
    started: datetime | None = None

    @property
    def label(self) -> str:
        return f"{self.company}\\{self.price}\\{self.count}"


def _odbc_drivers(preferred: str | None = None) -> list[str]:
    """Δοκιμάζει όποιο SQL Server ODBC υπάρχει στο PC (IM002 σε remote χωρίς Native Client).

    Προτεραιότητα σε Driver 17/18 — ο παλιός «SQL Server» συχνά δίνει HYC00 στο SQLBindParameter.
    """
    installed = set(pyodbc.drivers())
    preferred_text = (preferred or "").strip()
    ordered = [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "SQL Server Native Client 11.0",
        preferred_text,
        "SQL Server",
    ]
    seen: set[str] = set()
    drivers: list[str] = []
    for name in ordered:
        text = (name or "").strip()
        if not text or text in seen or text not in installed:
            continue
        seen.add(text)
        drivers.append(text)
    return drivers


def connect(config: dict) -> pyodbc.Connection:
    sql = config["sql"]
    timeout = int(sql.get("timeout", 30))
    drivers = _odbc_drivers(sql.get("driver"))
    if not drivers:
        raise RuntimeError(
            f"Αποτυχία σύνδεσης στον {sql['server']}: δεν βρέθηκε ODBC driver για SQL Server. "
            "Εγκατάστησε ODBC Driver 17/18 for SQL Server."
        )
    errors: list[str] = []
    for driver in drivers:
        connection_string = (
            f"DRIVER={{{driver}}};"
            f"SERVER={sql['server']};"
            f"DATABASE={sql['database']};"
            "Trusted_Connection=yes;"
            "TrustServerCertificate=yes;"
        )
        try:
            return pyodbc.connect(connection_string, timeout=timeout)
        except pyodbc.Error as failure:
            errors.append(f"{driver}: {failure}")
    detail = " | ".join(errors)
    raise RuntimeError(f"Αποτυχία σύνδεσης στον {sql['server']}: {detail}")


def connection_alive(connection: pyodbc.Connection) -> bool:
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        return True
    except pyodbc.Error:
        return False


def open_connection(config: dict, current: pyodbc.Connection | None = None) -> pyodbc.Connection:
    if current is not None and connection_alive(current):
        return current
    if current is not None:
        try:
            current.close()
        except pyodbc.Error:
            pass
    return connect(config)


def _now() -> datetime:
    return datetime.now().replace(microsecond=(datetime.now().microsecond // 1000) * 1000)


def format_value(value) -> str:
    """Παίρνει το ποσό του πίνακα και το γυρίζει σε μορφή {value}.00, π.χ. 3 → 3.00."""
    text = str(value).strip().replace(",", ".")
    if not text or text.lower() == "none":
        raise RuntimeError("Το ποσό (value) είναι κενό.")
    try:
        amount = float(text)
    except ValueError as failure:
        raise RuntimeError(f"Το ποσό δεν διαβάζεται: {value}") from failure
    if amount <= 0:
        raise RuntimeError(f"Το ποσό πρέπει να είναι μεγαλύτερο του μηδενός: {value}")
    return f"{amount:.2f}"


def _count_or_none(value) -> int | None:
    """Το 0 και το NULL σημαίνουν «δεν υπάρχει upd_amount»."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "none":
        return None
    try:
        number = int(float(text.replace(",", ".")))
    except ValueError as failure:
        raise RuntimeError(f"Το πλήθος δεν διαβάζεται: {value}") from failure
    if number == 0:
        return None
    if number < 0:
        raise RuntimeError(f"Το πλήθος δεν μπορεί να είναι αρνητικό: {value}")
    return number


def pieces_to_buy(amount, upd_amount) -> int:
    """Αν το upd_amount δεν είναι null, αγοράζεται εκείνο το πλήθος. Αλλιώς το amount."""
    updated = _count_or_none(upd_amount)
    if updated is not None:
        return updated
    original = _count_or_none(amount)
    if original is None:
        raise RuntimeError("Δεν υπάρχει πλήθος: το amount είναι κενό και το upd_amount είναι null.")
    return original


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _company_from_row(row) -> CompanyProfile:
    company = _text(row.company)
    if not company:
        raise RuntimeError("Η εταιρεία στον πίνακα companies είναι κενή.")
    return CompanyProfile(
        company=company,
        last_name=_text(row.last_name),
        first_name=_text(row.first_name),
        father_name=_text(row.father_name),
        afm=_text(row.afm),
    )


def fetch_company(cursor, company: str) -> CompanyProfile:
    cursor.execute(
        f"""
SELECT TOP 1 [company], [last_name], [first_name], [father_name], [afm]
FROM {COMPANIES}
WHERE LOWER(LTRIM(RTRIM([company]))) = LOWER(LTRIM(RTRIM(?)))
""",
        company,
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError(
            f"Δεν βρέθηκε η εταιρεία «{company}» στον πίνακα companies "
            "(η σύγκριση δεν κοιτάζει πεζά/κεφαλαία)."
        )
    return _company_from_row(row)


def fetch_companies(config: dict) -> dict[str, dict]:
    """Όλες οι εταιρείες, με το όνομα όπως είναι γραμμένο στον πίνακα companies."""
    connection = connect(config)
    try:
        cursor = connection.cursor()
        cursor.execute(
            f"""
SELECT [company], [last_name], [first_name], [father_name], [afm]
FROM {COMPANIES}
WHERE [company] IS NOT NULL AND LTRIM(RTRIM([company])) <> N''
ORDER BY [company]
"""
        )
        profiles: dict[str, dict] = {}
        for row in cursor.fetchall():
            profile = _company_from_row(row)
            profiles[profile.company] = profile.as_dict()
        return profiles
    except pyodbc.Error as failure:
        raise RuntimeError(f"Το SQL των εταιρειών απέτυχε: {failure}") from failure
    finally:
        connection.close()


def _job_from_columns(cursor, columns: dict) -> RunningJob:
    company_name = _text(columns.get("company"))
    if not company_name:
        raise RuntimeError(f"Η εργασία {columns.get('id')} δεν έχει εταιρεία.")
    price = format_value(columns.get("value"))
    count = pieces_to_buy(columns.get("amount"), columns.get("upd_amount"))
    profile = fetch_company(cursor, company_name)
    return RunningJob(
        job_id=int(columns["id"]),
        company=profile.company,
        price=price,
        count=count,
        profile=profile,
    )


def _clear_zero_upd_amount(cursor, job_id: int, upd_amount) -> None:
    """Κρατάει το upd_amount = 0 (όχι NULL) όταν η γραμμή έχει ήδη μηδέν."""
    number = _count_or_none(upd_amount)
    raw_is_zero = False
    if upd_amount is not None and str(upd_amount).strip() not in {"", "None"}:
        try:
            raw_is_zero = float(str(upd_amount).replace(",", ".")) == 0
        except ValueError:
            raw_is_zero = False
    if number is None and raw_is_zero:
        cursor.execute(UPDATE_UPD_AMOUNT, 0, job_id)


def fetch_waiting_job(
    config: dict,
    current: pyodbc.Connection | None = None,
    job_id: int | None = None,
) -> tuple[pyodbc.Connection, RunningJob]:
    """Κλειδώνει μία waiting γραμμή του ticket_scheduler_for_automation."""
    connection = open_connection(config, current)
    if job_id is not None:
        query = f"""
SELECT [id], [action], [company], [value], [amount], [upd_amount], [status], [type]
FROM {TICKETS}
WHERE [id] = ?
  AND [action] = ?
  AND LOWER([status]) = N'waiting'
"""
        params: tuple = (job_id, ACTION)
    else:
        query = f"""
SELECT TOP 1
[id], [action], [company], [value], [amount], [upd_amount], [status], [type]
FROM {TICKETS}
WHERE [action] = ?
  AND LOWER([status]) = N'waiting'
  AND [company] IS NOT NULL
  AND LTRIM(RTRIM([company])) <> N''
  AND [value] IS NOT NULL
  AND ([time_to_execute] IS NULL OR [time_to_execute] <= GETDATE())
ORDER BY [time_to_execute], [id]
"""
        params = (ACTION,)
    try:
        cursor = connection.cursor()
        cursor.execute(query, params)
        row = cursor.fetchone()
        if row is None:
            if job_id is not None:
                raise RuntimeError(f"Δεν υπάρχει waiting εργασία αγοράς με id={job_id}.")
            raise RuntimeError("Δεν υπάρχει waiting εργασία αγοράς παραβόλων.")
        columns = {column[0].lower(): value for column, value in zip(cursor.description, row)}
        job = _job_from_columns(cursor, columns)
        started = _now()
        cursor.execute(CLAIM_JOB, started, job.job_id)
        if cursor.rowcount != 1:
            connection.rollback()
            raise RuntimeError(f"Η εργασία {job.job_id} δεν ήταν πλέον waiting.")
        _clear_zero_upd_amount(cursor, job.job_id, columns.get("upd_amount"))
        connection.commit()
        job = RunningJob(
            job.job_id, job.company, job.price, job.count, job.profile, started
        )
    except pyodbc.Error as failure:
        try:
            connection.rollback()
        except pyodbc.Error:
            pass
        connection.close()
        raise RuntimeError(f"Το SQL απέτυχε: {failure}") from failure
    except Exception:
        try:
            connection.rollback()
        except pyodbc.Error:
            pass
        connection.close()
        raise
    return connection, job


def fetch_due_jobs(config: dict) -> list[RunningJob]:
    """Κλειδώνει κάθε ώριμη waiting αγορά. Όσες πρόλαβε άλλος να πάρει, παραλείπονται."""
    connection = open_connection(config)
    jobs: list[RunningJob] = []
    try:
        cursor = connection.cursor()
        cursor.execute(
            f"""
SELECT [id]
FROM {TICKETS}
WHERE [action] = ?
  AND LOWER([status]) = N'waiting'
  AND [company] IS NOT NULL
  AND LTRIM(RTRIM([company])) <> N''
  AND [value] IS NOT NULL
  AND ([time_to_execute] IS NULL OR [time_to_execute] <= GETDATE())
ORDER BY [time_to_execute], [id]
""",
            ACTION,
        )
        ids = [int(row[0]) for row in cursor.fetchall()]
    except pyodbc.Error as failure:
        connection.close()
        raise RuntimeError(f"Το SQL απέτυχε: {failure}") from failure
    finally:
        try:
            connection.close()
        except pyodbc.Error:
            pass

    for job_id in ids:
        try:
            connection, job = fetch_waiting_job(config, job_id=job_id)
            connection.close()
            jobs.append(job)
        except RuntimeError:
            continue
    return jobs


def update_remaining(
    config: dict,
    job_id: int,
    remaining: int,
    current: pyodbc.Connection | None = None,
) -> pyodbc.Connection:
    """Γράφει το υπόλοιπο στο upd_amount. Στο 0 μένει 0 (όχι NULL). Το amount δεν πειράζεται."""
    connection = open_connection(config, current)
    value = 0 if remaining < 1 else int(remaining)
    try:
        cursor = connection.cursor()
        cursor.execute(UPDATE_UPD_AMOUNT, value, job_id)
        connection.commit()
    except pyodbc.Error as failure:
        try:
            connection.rollback()
        except pyodbc.Error:
            pass
        connection.close()
        raise RuntimeError(f"Αποτυχία ενημέρωσης upd_amount για {job_id}: {failure}") from failure
    return connection


def mark_job_succeed(
    config: dict,
    job_id: int,
    started: datetime | None = None,
    current: pyodbc.Connection | None = None,
) -> pyodbc.Connection:
    del started
    connection = open_connection(config, current)
    ended = _now()
    try:
        cursor = connection.cursor()
        cursor.execute(MARK_SUCCEED, ended, job_id)
        connection.commit()
    except pyodbc.Error as failure:
        try:
            connection.rollback()
        except pyodbc.Error:
            pass
        connection.close()
        raise RuntimeError(f"Αποτυχία succeed για {job_id}: {failure}") from failure
    return connection


def mark_job_waiting_remaining(
    config: dict,
    job_id: int,
    remaining: int,
    current: pyodbc.Connection | None = None,
) -> pyodbc.Connection:
    connection = open_connection(config, current)
    now = _now()
    value = 0 if remaining < 1 else int(remaining)
    try:
        cursor = connection.cursor()
        cursor.execute(MARK_WAITING, value, now, now, job_id)
        connection.commit()
    except pyodbc.Error as failure:
        try:
            connection.rollback()
        except pyodbc.Error:
            pass
        connection.close()
        raise RuntimeError(f"Αποτυχία requeue waiting για {job_id}: {failure}") from failure
    return connection


def paravolo_dikastiria_exists(config: dict, kodikos_pliromis: str) -> bool:
    connection = connect(config)
    try:
        cursor = connection.cursor()
        cursor.execute(EXISTS_DIKASTIRIA, kodikos_pliromis)
        return int(cursor.fetchone()[0]) > 0
    except pyodbc.Error as failure:
        raise RuntimeError(f"Έλεγχος διπλότυπου στο paravola_dikastiria απέτυχε: {failure}") from failure
    finally:
        connection.close()


def _sql_date(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    return text[:10]


def _sql_datetime(value) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return str(value).strip()


def insert_paravolo_dikastiria(config: dict, row: dict) -> None:
    """Εισάγει μία γραμμή στο [dbo].[paravola_dikastiria] (όλες οι εταιρείες)."""
    required = (
        "KodikosPliromis",
        "KodikosParavolou",
        "Foreas",
        "KatigoriaParavolou",
        "TyposParavolou",
        "Poso",
        "Afm",
        "Publish_DATE",
    )
    for key in required:
        if not str(row.get(key) or "").strip():
            raise RuntimeError(f"Λείπει υποχρεωτικό πεδίο για paravola_dikastiria: {key}")

    publish = _sql_date(row["Publish_DATE"])
    inserted = _sql_datetime(row.get("dt_insert") or _now())
    used = row.get("used_on_date")
    editor = row.get("id_editor_dikastiria")
    # NULL ως SQL literal — αποφεύγει HYC00 SQLBindParameter στον παλιό ODBC «SQL Server».
    used_sql = "NULL" if used is None else "?"
    editor_sql = "NULL" if editor is None else "?"
    insert_sql = f"""
INSERT INTO {PARAVOLA_DIKASTIRIA}
([KodikosPliromis],[KodikosParavolou],[Foreas],[KatigoriaParavolou],[TyposParavolou],
 [Poso],[Afm],[for_legal_action],[Publish_DATE],[used_on_date],[id_editor_dikastiria],[dt_insert])
VALUES (?,?,?,?,?,?,?,?,?,{used_sql},{editor_sql},?)
"""
    params: list = [
        str(row["KodikosPliromis"]),
        str(row["KodikosParavolou"]),
        str(row["Foreas"]),
        str(row["KatigoriaParavolou"]),
        str(row["TyposParavolou"]),
        str(row["Poso"]),
        str(row["Afm"]).strip(),
        str(row.get("for_legal_action") or ""),
        publish,
    ]
    if used is not None:
        params.append(_sql_datetime(used) if not isinstance(used, str) else used)
    if editor is not None:
        params.append(int(editor))
    params.append(inserted)

    connection = connect(config)
    try:
        cursor = connection.cursor()
        cursor.execute(EXISTS_DIKASTIRIA, row["KodikosPliromis"])
        if int(cursor.fetchone()[0]) > 0:
            raise RuntimeError(
                f"Υπάρχει ήδη στο paravola_dikastiria το RF {row['KodikosPliromis']}."
            )
        cursor.execute(insert_sql, params)
        connection.commit()
    except pyodbc.Error as failure:
        try:
            connection.rollback()
        except pyodbc.Error:
            pass
        raise RuntimeError(f"Αποτυχία εισαγωγής στο paravola_dikastiria: {failure}") from failure
    finally:
        connection.close()


def mark_job_failed(
    config: dict,
    job_id: int,
    current: pyodbc.Connection | None = None,
) -> pyodbc.Connection:
    connection = open_connection(config, current)
    ended = _now()
    try:
        cursor = connection.cursor()
        cursor.execute(MARK_FAILED, ended, job_id)
        connection.commit()
    except pyodbc.Error as failure:
        try:
            connection.rollback()
        except pyodbc.Error:
            pass
        connection.close()
        raise RuntimeError(f"Αποτυχία failed για {job_id}: {failure}") from failure
    return connection
