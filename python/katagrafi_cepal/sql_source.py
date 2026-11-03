# -*- coding: utf-8 -*-
"""Σύνδεση στη βάση, παράβολα CEPAL, και status του scheduler.

Δεδομένα καταχώρησης: stored procedure dbo.[cepal_fill_web]
(κάθε γραμμή του result set = μία καταχώρηση / fill φόρμας). Indexes όπως στο PAD fill.
Η σύνδεση γίνεται στη βάση από config.json (χωρίς hardcode ονόματος server/DB).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pyodbc

# SP — φέρνει τι θα γίνει fill στη φόρμα CEPAL. Δεν την πειράζουμε από εδώ.
QUERY = "exec [dbo].[cepal_fill_web]"
UPDATE = (
    "UPDATE [dbo].[klironomikes_Paravola] "
    "SET CEPAL_gui = ? "
    "WHERE KodikosParavolou = ?"
)

SCHEDULER_ACTION = "KLIRONOMIKES_KATAGRAFI_CEPAL"
# Ίδιος πίνακας με τον C# AutomationScheduler (όχι Scheduler_for_Automation).
TICKETS = "[dbo].[ticket_scheduler_for_automation]"

CLAIM_SCHEDULER = f"""
UPDATE {TICKETS}
SET [status] = N'running',
    [started] = ?,
    [ended] = NULL
WHERE [id] = ?
  AND LOWER([status]) = N'waiting'
"""

MARK_SCHEDULER_SUCCEED = f"""
UPDATE {TICKETS}
SET [status] = N'succeed',
    [ended] = ?
WHERE [id] = ?
  AND LOWER([status]) = N'running'
"""

MARK_SCHEDULER_FAILED = f"""
UPDATE {TICKETS}
SET [status] = N'failed',
    [ended] = ?
WHERE [id] = ?
  AND LOWER([status]) = N'running'
"""


@dataclass(frozen=True)
class Paravolo:
    path: str
    filename: str
    kodikos: str
    foreas: str
    category: str
    type_name: str
    amount: str
    rf: str
    spv: str
    debtor: str
    tax_id: str
    customer_code: str
    contract: str
    lawyer: str
    publish_date: str


def _odbc_drivers(preferred: str | None = None) -> list[str]:
    """Δοκιμάζει όποιο SQL Server ODBC υπάρχει στο PC (IM002 σε remote χωρίς Native Client)."""
    installed = set(pyodbc.drivers())
    ordered = [
        preferred or "",
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "SQL Server Native Client 11.0",
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


def _cell(row, index: int) -> str:
    if index >= len(row) or row[index] is None:
        return ""
    return str(row[index]).strip()


def _paravolo(row) -> Paravolo:
    # Indexes = PAD %CurrentItem[i]% (ίδια αντιστοίχιση με το fill subflow).
    return Paravolo(
        path=_cell(row, 0),
        filename=_cell(row, 1),
        kodikos=_cell(row, 2),
        foreas=_cell(row, 3),
        category=_cell(row, 4),
        type_name=_cell(row, 5),
        amount=_cell(row, 6),
        rf=_cell(row, 7),
        spv=_cell(row, 8),
        debtor=_cell(row, 10),
        tax_id=_cell(row, 11),
        customer_code=_cell(row, 13),
        contract=_cell(row, 15),
        lawyer=_cell(row, 16),
        publish_date=_cell(row, 20),
    )


def fetch_paravola(connection: pyodbc.Connection) -> list[Paravolo]:
    cursor = connection.cursor()
    try:
        cursor.execute(QUERY)
        rows = cursor.fetchall()
    except pyodbc.Error as failure:
        raise RuntimeError(f"Απέτυχε το cepal_fill_web: {failure}") from failure
    finally:
        cursor.close()
    return [_paravolo(row) for row in rows]


def save_gui(connection: pyodbc.Connection, gui: str, kodikos: str) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(UPDATE, gui, kodikos)
        connection.commit()
        if cursor.rowcount == 0:
            raise RuntimeError(
                f"Η υπόθεση δημιουργήθηκε ({gui}) αλλά δεν βρέθηκε γραμμή "
                f"με κωδικό παραβόλου {kodikos}."
            )
    except pyodbc.Error as failure:
        raise RuntimeError(
            f"Η υπόθεση δημιουργήθηκε ({gui}) αλλά δεν γράφτηκε στη βάση: {failure}"
        ) from failure
    finally:
        cursor.close()


def _now() -> datetime:
    stamp = datetime.now()
    return stamp.replace(microsecond=(stamp.microsecond // 1000) * 1000)


def claim_scheduler_job(connection: pyodbc.Connection, job_id: int | None = None) -> int:
    """waiting → running για KLIRONOMIKES_KATAGRAFI_CEPAL. Επιστρέφει job id."""
    cursor = connection.cursor()
    try:
        if job_id is not None:
            cursor.execute(
                f"""
SELECT [id]
FROM {TICKETS}
WHERE [id] = ?
  AND [action] = ?
  AND LOWER([status]) = N'waiting'
""",
                job_id,
                SCHEDULER_ACTION,
            )
        else:
            cursor.execute(
                f"""
SELECT TOP 1 [id]
FROM {TICKETS}
WHERE [action] = ?
  AND LOWER([status]) = N'waiting'
  AND ([time_to_execute] IS NULL OR [time_to_execute] <= GETDATE())
ORDER BY [id]
""",
                SCHEDULER_ACTION,
            )
        row = cursor.fetchone()
        if row is None:
            if job_id is not None:
                raise RuntimeError(f"Δεν υπάρχει waiting καταχώρηση CEPAL με id={job_id}.")
            raise RuntimeError("Δεν υπάρχει waiting εργασία καταχώρησης CEPAL.")
        claimed_id = int(row[0])
        started = _now()
        cursor.execute(CLAIM_SCHEDULER, started, claimed_id)
        if cursor.rowcount != 1:
            connection.rollback()
            raise RuntimeError(f"Η εργασία {claimed_id} δεν ήταν πλέον waiting.")
        connection.commit()
        return claimed_id
    except pyodbc.Error as failure:
        try:
            connection.rollback()
        except pyodbc.Error:
            pass
        raise RuntimeError(f"Αποτυχία claim scheduler: {failure}") from failure
    finally:
        cursor.close()


def mark_scheduler_succeed(connection: pyodbc.Connection, job_id: int) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(MARK_SCHEDULER_SUCCEED, _now(), job_id)
        if cursor.rowcount != 1:
            connection.rollback()
            raise RuntimeError(
                f"Αποτυχία succeed για {job_id}: η γραμμή δεν ήταν running."
            )
        connection.commit()
    except pyodbc.Error as failure:
        try:
            connection.rollback()
        except pyodbc.Error:
            pass
        raise RuntimeError(f"Αποτυχία succeed για {job_id}: {failure}") from failure
    finally:
        cursor.close()


def mark_scheduler_failed(connection: pyodbc.Connection, job_id: int) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute(MARK_SCHEDULER_FAILED, _now(), job_id)
        if cursor.rowcount != 1:
            connection.rollback()
            raise RuntimeError(
                f"Αποτυχία failed για {job_id}: η γραμμή δεν ήταν running."
            )
        connection.commit()
    except pyodbc.Error as failure:
        try:
            connection.rollback()
        except pyodbc.Error:
            pass
        raise RuntimeError(f"Αποτυχία failed για {job_id}: {failure}") from failure
    finally:
        cursor.close()
