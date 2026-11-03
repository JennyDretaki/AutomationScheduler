# Automation Scheduler — Αγορά παραβόλων & Καταχώρηση CEPAL

Σύστημα αυτοματισμού για αγορά e-παραβόλων (GSIS) και καταχώρησή τους στη CEPAL, με ουρά εργασιών σε SQL Server και κεντρικό C# scheduler.

**Παραγωγικό deploy:** τοπικό share / mapped drive της ομάδας (βλ. εσωτερικό deploy path — όχι στο git).

---

## Επισκόπηση συστήματος

```
┌─────────────────────────────────────────────────────────────────┐
│                  AutomationScheduler.exe (C#)                     │
│  Διαβάζει ticket_scheduler_for_automation · ξεκινά Python jobs   │
└─────────────┬───────────────────────┬───────────────────────────┘
              │                       │
              ▼                       ▼
┌─────────────────────────┐  ┌────────────────────────────────────┐
│ Αγορά παραβόλων         │  │ Καταχώρηση CEPAL                   │
│ (δύο προγράμματα)       │  │ katagrafi_cepal                    │
└─────────────┬───────────┘  └─────────────────┬──────────────────┘
              │                                │
     ┌────────┴────────┐                       │
     ▼                 ▼                       ▼
 Δικαστήριο      Κληρονομικές          cepal_fill_web (SP)
 paravola_       share +               → upload στη CEPAL
 dikastiria      klironomikes_Paravola → CEPAL_gui
```

### Ροές (σε μία πρόταση)

| Ροή | Τι κάνει | Πού καταλήγει |
|-----|----------|----------------|
| **Αγορά δικαστηρίου** | Αγοράζει παράβολα από το GSIS | `C:\test\<εταιρεία>\` + πίνακας `paravola_dikastiria` |
| **Αγορά κληρονομικών** | Αγοράζει παράβολα από το GSIS | Network share + πίνακας `klironomikes_Paravola` (μέσω extractor) |
| **Καταχώρηση CEPAL** | Παίρνει ουρά από SP και ανεβάζει στη CEPAL | Ενημερώνει `klironomikes_Paravola.CEPAL_gui` |

Η **αγορά κληρονομικών** τροφοδοτεί την **καταχώρηση CEPAL**.  
Η **αγορά δικαστηρίου** είναι **ξεχωριστό κύκλωμα** (δικαστήρια) — δεν μπαίνει στην καταχώρηση CEPAL.

---

## Έργα (projects) μέσα στο repo

### 1. `publish/` — Automation Scheduler (C#)

| Αρχείο | Ρόλος |
|--------|--------|
| `AutomationScheduler.exe` | Τρέχει στο RDP/PC ως υπηρεσία-ουρά (στο deploy, όχι στο git) |
| `appsettings.json` | Σύνδεση SQL, Python path, mapping `Action` → φάκελος Python |
| `start-scheduler.bat` | Εκκίνηση του exe |

Ο scheduler:

- Κοιτάει τον πίνακα **`ticket_scheduler_for_automation`**
- Για κάθε ώριμη εργασία (`status=waiting`, `time_to_execute <= now`) ξεκινά το αντίστοιχο Python
- Χαρτογράφηση στο `appsettings.json` → `Scheduler.PythonJobs`:

| `action` | Working directory |
|----------|-------------------|
| `KLIRONOMIKES_KATAGRAFI_CEPAL` | `python/katagrafi_cepal` |
| `KLIRONOMIKES_AGORA_PARAVOLON` | `python/agora_paravolon_klironomikes` |
| `DIKASTIRIA_AGORA_PARAVOLON` | `python/agora_paravolon_dikastiria` |

Ρυθμίσεις σημαντικές:

- `ResumeRunningJobsOnStartup: true` — αν κόπηκε run ενώ `running`, συνεχίζει μετά το restart
- `RetryFailedAfterMinutes: 120` — μετά από fail, επόμενη προσπάθεια σε 2 ώρες
- Καταχώρηση CEPAL: `recursive=1`, `duration=2` (κάθε 2 ώρες)

---

### 2. `python/agora_paravolon_dikastiria` — Αγορά παραβόλων δικαστηρίου

**Action:** `DIKASTIRIA_AGORA_PARAVOLON`

Τι κάνει:

1. Παίρνει εργασία από `ticket_scheduler_for_automation` (εταιρεία, ποσό, πλήθος / `upd_amount`)
2. Ανοίγει Chrome (Playwright) στη φόρμα e-παραβόλου GSIS
3. Συμπληρώνει στοιχεία εταιρείας από τον πίνακα `companies`, λύνει captcha (Windows OCR)
4. Αποθηκεύει PDF → μετονομασία σε `RF….pdf`
5. Μετακινεί στο **`C:\test\<ΕΤΑΙΡΕΙΑ>\`** και **μένει μόνο εκεί**
6. Κάνει **INSERT** στο **`paravola_dikastiria`**

**Δεν** προωθεί στο share κληρονομικών, **δεν** καλεί τον extractor, **δεν** τροφοδοτεί την καταχώρηση CEPAL.

Κύρια αρχεία: `main.py` (UI + `--scheduled`), `issuer.py` (browser flow), `sql_source.py` (tickets + dikastiria), `config.json`, `start.bat`.

---

### 3. `python/agora_paravolon_klironomikes` — Αγορά παραβόλων κληρονομικών

**Action:** `KLIRONOMIKES_AGORA_PARAVOLON`

Τι κάνει:

1. Ίδια λογική αγοράς από GSIS (ουρά tickets + εταιρείες)
2. Αντιγράφει το `RF….pdf` στο **share** κληρονομικών
3. Τρέχει τον **EParavoloExtractor** → εγγραφή στο **`klironomikes_Paravola`**
4. Σβήνει το αντίγραφο από `C:\test`

Αυτή η ροή **γεμίζει** την ουρά που διαβάζει η `cepal_fill_web` → άρα συνδέεται με την καταχώρηση CEPAL.

Κύρια αρχεία: ίδια δομή με δικαστήριο (`main.py`, `issuer.py`, `sql_source.py`, …) με διαφορετικό post-purchase path.

---

### 4. `python/katagrafi_cepal` — Καταχώρηση CEPAL

**Action:** `KLIRONOMIKES_KATAGRAFI_CEPAL`

Τι κάνει:

1. Claim εργασίας στο `ticket_scheduler_for_automation`
2. Καλεί την SP **`cepal_fill_web`** (κάθε γραμμή = μία καταχώρηση στη φόρμα)
3. Ανεβάζει το PDF από το `path\filename` που επιστρέφει η SP
4. Συμπληρώνει τη φόρμα CEPAL (RF, ποσό, οφειλέτης, σύμβαση, κ.λπ.)
5. Γράφει `CEPAL_gui` στο **`klironomikes_Paravola`**

Κύρια αρχεία: `main.py`, `uploader.py`, `sql_source.py`, `config.json`, `start.bat`.

Προγραμματισμός: **μία φορά κάθε 2 ώρες**. Αν διακοπεί ενώ τρέχει, ο scheduler το ξαναπαίρνει στο startup (`ResumeRunningJobsOnStartup`).

---

### 5. `python/agora_paravolon` — Παλιός φάκελος (redirect)

Δεν χρησιμοποιείται πλέον. Το `start.bat` / `main.py` δείχνουν ότι η αγορά χωρίστηκε σε δικαστήριο + κληρονομικές. Κρατιέται μόνο για συμβατότητα παλιών shortcuts.

---

## Πίνακες SQL

Τα ονόματα **server** και **database** μπαίνουν μόνο τοπικά στα `config.json` / `appsettings.json` (στο git υπάρχουν placeholders `YOUR_SQL_SERVER` / `YOUR_DATABASE`).

| Πίνακας | Χρήση |
|---------|--------|
| `ticket_scheduler_for_automation` | Ουρά όλων των jobs (agora + katagrafi). Το `action` ξεχωρίζει τη ροή. |
| `companies` | Επωνυμία, ΑΦΜ, πεδία φόρμας ανά εταιρεία |
| `paravola_dikastiria` | Αγορασμένα παράβολα **δικαστηρίου** |
| `klironomikes_Paravola` | Παράβολα **κληρονομικών** + flags CEPAL (`CEPAL_gui`, …) |
| SP `cepal_fill_web` | Ουρά προς καταχώρηση CEPAL (δεν την αλλάζουμε από το Python) |

Σημαντικά πεδία tickets:

- `action` — ποιο πρόγραμμα τρέχει
- `company`, `value` (ποσό), `amount` / `upd_amount` (πλήθος· στο τέλος αγοράς το `upd_amount` γίνεται `0`)
- `status` — `waiting` / `running` / `succeed` / `failed`
- `recursive`, `duration` — επανάληψη (π.χ. καταχώρηση κάθε 2 ώρες)
- `time_to_execute` — πότε είναι έτοιμο να τρέξει

---

## Κοινό Python runtime

Κοινό για edretaki + developer10:

| Διαδρομή | Περιεχόμενο |
|----------|-------------|
| `python/runtime/` | Python 3.12.7 (portable) |
| `python/.venv/` | Virtualenv + pyodbc, playwright, … |
| `python/ms-playwright/` | Chromium για Playwright |

Εγκατάσταση/ανανέωση: `setup-python.bat`  
Τα `start.bat` των projects χρησιμοποιούν relative paths (`%~dp0`) ώστε να δουλεύουν από mapped drive ή UNC χωρίς hardcode.

---

## Desktop shortcuts (τοπικά)

| Shortcut | Τι ανοίγει |
|----------|------------|
| Αγορά παραβόλων δικαστηρίου | `agora_paravolon_dikastiria` (μόνο `C:\test\<εταιρεία>`) |
| Αγορά παραβόλων κληρονομικών | `agora_paravolon_klironomikes` |
| Καταχώρηση CEPAL | `katagrafi_cepal` |

Ο scheduler καλεί τα ίδια `main.py --scheduled` χωρίς GUI startup wizard.

---

## Πώς τρέχει καθημερινά

1. Στο login / RDP: `install-autostart-logon.bat` ή χειροκίνητα `publish\start-scheduler.bat`
2. Ο C# scheduler κάνει poll (~60s)
3. Όταν υπάρχει waiting ticket με σωστό `action` → ξεκινά το αντίστοιχο Python
4. Το Python ενημερώνει μόνο του το `status` / `upd_amount` / `ended`

Χειροκίνητο τεστ:

```bat
cd python\agora_paravolon_dikastiria
start.bat
```

ή scheduled:

```bat
..\..\.venv\Scripts\python.exe main.py --scheduled --job-id N --store ticket
```

---

## Δομή φακέλων (source στο git)

```
automation-scheduler/
├── README.md
├── .gitignore
├── setup-python.bat
├── install-autostart-logon.bat
├── publish.bat
├── publish/
│   ├── appsettings.json
│   └── start-scheduler.bat
└── python/
    ├── agora_paravolon_dikastiria/
    ├── agora_paravolon_klironomikes/
    ├── katagrafi_cepal/
    └── agora_paravolon/          # deprecated redirect
```

Στο **παραγωγικό** `P:\` υπάρχουν επιπλέον (όχι στο git): `.venv`, `runtime`, `ms-playwright`, `chrome-profile`, `logs`, και τα binaries του `publish\`.

---

## Σημειώσεις συντήρησης

- **Ίδια εταιρεία** μπορεί να έχει jobs και στα δύο actions — ο διαχωρισμός γίνεται **μόνο από το `action`**, όχι από το όνομα εταιρείας.
- Μην αλλάζετε την SP `cepal_fill_web` από τα Python scripts.
- Μετά από αλλαγή `appsettings.json` χρειάζεται **restart** του `AutomationScheduler.exe`.
- Ο παλιός φάκελος `agora_paravolon` δεν πρέπει να είναι WorkingDirectory στο scheduler.
