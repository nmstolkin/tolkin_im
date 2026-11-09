"""Kleine gemeinsame Hilfsfunktionen (Dateien, Zeit, Zeitbudget)."""

import json
import os
import time
from datetime import datetime, timezone


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data, kompakt=False):
    """Schreibt atomar (erst in eine Temp-Datei, dann umbenennen) - ein
    Abbruch mitten im Schreiben hinterlaesst so nie eine halbe JSON-Datei."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if kompakt:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def hours_since(iso_timestamp):
    if not iso_timestamp:
        return 999999
    try:
        then = datetime.fromisoformat(iso_timestamp)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - then).total_seconds() / 3600
    except Exception:
        return 999999


# ---------------------------------------------------------------------------
# Zeitbudget. GitHub bricht Jobs nach 6 Stunden ersatzlos ab - dann waere
# alles verloren. Wir hoeren vorher freiwillig auf und sichern das Erreichte.
# ---------------------------------------------------------------------------
try:
    RUN_BUDGET_MINUTES = float(os.environ.get("RUN_BUDGET_MINUTES", "150"))
except ValueError:
    print("RUN_BUDGET_MINUTES unbrauchbar, nutze 150 Minuten.")
    RUN_BUDGET_MINUTES = 150.0
_run_started = time.time()


def minuten_seit_start():
    return (time.time() - _run_started) / 60


def budget_exceeded():
    return minuten_seit_start() >= RUN_BUDGET_MINUTES


class RateLimiter:
    """Haelt einen Mindestabstand zwischen zwei Aufrufen ein - gemessen ab
    dem Start des letzten Aufrufs. Frueher wurde die Pause ZUSAETZLICH zur
    Antwortzeit abgewartet; dauerte ein Aufruf 5 s, waren es 9,5 s je Seite."""

    def __init__(self, min_abstand_s):
        self.min_abstand_s = min_abstand_s
        self._letzter = 0.0

    def warte(self):
        rest = self.min_abstand_s - (time.time() - self._letzter)
        if rest > 0:
            time.sleep(rest)
        self._letzter = time.time()
