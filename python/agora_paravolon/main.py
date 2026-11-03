# -*- coding: utf-8 -*-
"""Παλιός φάκελος — η αγορά χωρίστηκε σε δύο προγράμματα."""
from __future__ import annotations

import sys

print(
    "Ο φάκελος agora_paravolon δεν χρησιμοποιείται πλέον.\n"
    "  Δικαστήριο:   agora_paravolon_dikastiria  (DIKASTIRIA_AGORA_PARAVOLON)\n"
    "  Κληρονομικές: agora_paravolon_klironomikes (KLIRONOMIKES_AGORA_PARAVOLON)\n"
    "Ενημέρωσε/κάνε restart τον AutomationScheduler (publish\\appsettings.json).",
    file=sys.stderr,
)
raise SystemExit(2)
