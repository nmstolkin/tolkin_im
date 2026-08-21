#!/usr/bin/env python3
"""
Immo Watcher
------------
Ruft eine Liste von Hausverwaltungs-/Makler-Webseiten ab, erkennt neue
Wohnungsinserate per KI-Extraktion (Claude API), filtert sie nach den
gewünschten Kriterien und schickt bei Treffern eine Push-Benachrichtigung
ans Handy (via ntfy.sh).

Speichert Status (bekannte Inserate + Seiten-Hashes) in data/, damit
zwischen den Läufen (z.B. via GitHub Actions) nichts doppelt gemeldet wird.
"""

import os
import re
import json
import time
import hashlib
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

DATA_DIR = "data"
SEEN_FILE = os.path.join(DATA_DIR, "seen_listings.json")
HASH_FILE = os.path.join(DATA_DIR, "page_hashes.json")
ERROR_LOG = os.path.join(DATA_DIR, "last_errors.json")
SITES_FILE = "sites.json"

# Datei, die die Homescreen-App (docs/index.html) anzeigt
APP_DATA_FILE = "data.json"
MAX_MATCHES_KEPT = 200  # ältere Treffer werden aus der App-Ansicht ausgeblendet

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

MODEL = "claude-sonnet-4-6"
MAX_TEXT_CHARS = 15000  # wie viel Seitentext wir maximal an die API schicken
REQUEST_TIMEOUT = 25
SLEEP_BETWEEN_SITES = 1.5  # kleine Pause, um nicht wie ein aggressiver Bot zu wirken

CRITERIA = {
    "min_rooms": 3,
    "min_size_qm": 85,
    "min_rent": 1500,
    "max_rent": 2300,
    # Freitext-Fragmente, die im erkannten Bezirk/Adresse vorkommen dürfen
    "districts": ["mitte", "prenzlauer berg", "prenzlauer", "charlottenburg"],
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 ImmoWatcher/1.0"
    )
}

EXTRACTION_PROMPT = """Du bekommst den Textinhalt einer Immobilien-Angebotsseite (URL: {url}).

Extrahiere ALLE einzelnen Mietwohnungsinserate, die auf dieser Seite sichtbar sind,
als JSON-Array. Ignoriere Navigation, Footer, allgemeine Werbetexte und Angebote
zum KAUF (nur Miete/Vermietung zählt).

Jedes Objekt im Array soll folgende Felder haben (wenn ein Wert nicht auf der
Seite steht: null):
- "title": kurze Bezeichnung / Adresse des Inserats
- "rooms": Zimmeranzahl als Zahl
- "size_qm": Wohnfläche in Quadratmetern als Zahl
- "rent": Kaltmiete in Euro als Zahl (nur die Zahl, ohne Symbol)
- "district": Stadtteil/Bezirk, falls erkennbar
- "url": Direktlink zum Inserat, falls vorhanden (sonst null)

Antworte AUSSCHLIESSLICH mit dem JSON-Array. Kein einleitender Text, keine
Markdown-Codeblöcke, keine Erklärung. Falls keine Inserate erkennbar sind,
antworte mit einem leeren Array: []

Seiteninhalt:
{text}
"""


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.text, None
    except Exception as e:
        return None, str(e)


def extract_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "svg", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text[:MAX_TEXT_CHARS]


def call_claude_extract(url, text):
    if not text or len(text) < 50:
        return []

    prompt = EXTRACTION_PROMPT.format(url=url, text=text)
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 2500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()
    raw = re.sub(r"^```(json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()

    try:
        listings = json.loads(raw)
        if isinstance(listings, list):
            return listings
        return []
    except Exception:
        return []


def matches_criteria(listing):
    rooms = listing.get("rooms")
    size = listing.get("size_qm")
    rent = listing.get("rent")
    district = (listing.get("district") or "").lower()
    title = (listing.get("title") or "").lower()

    if isinstance(rooms, (int, float)) and rooms < CRITERIA["min_rooms"]:
        return False
    if isinstance(size, (int, float)) and size < CRITERIA["min_size_qm"]:
        return False
    if isinstance(rent, (int, float)):
        if not (CRITERIA["min_rent"] <= rent <= CRITERIA["max_rent"]):
            return False

    # Bezirk: wenn erkennbar, muss er zu den gewünschten passen.
    # Wenn kein Bezirk erkannt wurde, lassen wir das Inserat lieber durch,
    # als ein potenziell passendes Angebot zu verpassen (lieber ein
    # False Positive als ein verpasstes Inserat).
    haystack = f"{district} {title}"
    if district:
        if not any(d in haystack for d in CRITERIA["districts"]):
            return False

    return True


def listing_key(listing, site_url):
    """Eindeutiger Schlüssel, um ein Inserat wiederzuerkennen."""
    key = listing.get("url") or listing.get("title")
    if not key:
        # Fallback: Hash aus allen Feldern
        key = json.dumps(listing, sort_keys=True)
    return f"{site_url}::{key}"


def source_name(site_url):
    host = urlparse(site_url).netloc
    host = re.sub(r"^(www\.|portal\.)", "", host)
    return host


def update_app_data(new_matches):
    """Schreibt die aktuellen Treffer in docs/data.json, das die Homescreen-App anzeigt."""
    if not new_matches:
        # trotzdem den Zeitstempel aktualisieren, damit die App weiß, dass geprüft wurde
        existing = load_json(APP_DATA_FILE, {"matches": []})
    else:
        existing = load_json(APP_DATA_FILE, {"matches": []})

    existing_matches = existing.get("matches", [])
    existing_keys = {m.get("key") for m in existing_matches}

    for m in new_matches:
        if m["key"] not in existing_keys:
            existing_matches.append(m)
            existing_keys.add(m["key"])

    # neueste zuerst, auf maximale Anzahl begrenzen
    existing_matches.sort(key=lambda m: m.get("found_at", ""), reverse=True)
    existing_matches = existing_matches[:MAX_MATCHES_KEPT]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "criteria": {
            "min_rooms": CRITERIA["min_rooms"],
            "min_size_qm": CRITERIA["min_size_qm"],
            "min_rent": CRITERIA["min_rent"],
            "max_rent": CRITERIA["max_rent"],
            "districts": ["Mitte", "Prenzlauer Berg", "Charlottenburg"],
        },
        "matches": existing_matches,
    }
    save_json(APP_DATA_FILE, payload)


def notify(listing, site_url):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC nicht gesetzt, überspringe Benachrichtigung.")
        return

    title = listing.get("title") or "Neues Inserat gefunden"
    parts = []
    if listing.get("rooms"):
        parts.append(f"{listing['rooms']} Zimmer")
    if listing.get("size_qm"):
        parts.append(f"{listing['size_qm']} qm")
    if listing.get("rent"):
        parts.append(f"{listing['rent']} € Kaltmiete")
    if listing.get("district"):
        parts.append(str(listing["district"]))
    body = " | ".join(str(p) for p in parts) if parts else "Details auf der Seite prüfen"

    link = listing.get("url") or site_url

    try:
        requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Click": link,
                "Priority": "high",
                "Tags": "house",
            },
            timeout=15,
        )
    except Exception as e:
        print(f"Benachrichtigung fehlgeschlagen: {e}")


# ---------------------------------------------------------------------------
# Hauptlauf
# ---------------------------------------------------------------------------

def main():
    if not ANTHROPIC_API_KEY:
        print("FEHLER: ANTHROPIC_API_KEY ist nicht gesetzt.", file=sys.stderr)
        sys.exit(1)

    sites = load_json(SITES_FILE, [])
    seen = load_json(SEEN_FILE, {})       # {site_url: [listing_key, ...]}
    hashes = load_json(HASH_FILE, {})     # {site_url: content_hash}
    errors = {}

    total_new_matches = 0
    app_matches = []  # Treffer für die Homescreen-App (docs/data.json)

    for i, url in enumerate(sites, 1):
        print(f"[{i}/{len(sites)}] {url}")
        html, err = fetch(url)

        if err:
            print(f"  -> Fehler beim Abrufen: {err}")
            errors[url] = err
            continue

        content_hash = hashlib.sha256(html.encode("utf-8", "ignore")).hexdigest()
        if hashes.get(url) == content_hash:
            print("  -> keine Änderung seit letztem Lauf, überspringe.")
            continue
        hashes[url] = content_hash

        text = extract_text(html)

        try:
            listings = call_claude_extract(url, text)
        except Exception as e:
            print(f"  -> Extraktion fehlgeschlagen: {e}")
            errors[url] = f"Extraktion fehlgeschlagen: {e}"
            continue

        print(f"  -> {len(listings)} Inserat(e) erkannt")

        known = set(seen.get(url, []))
        new_on_this_site = 0

        for listing in listings:
            key = listing_key(listing, url)
            if key in known:
                continue
            known.add(key)
            new_on_this_site += 1

            if matches_criteria(listing):
                print(f"     TREFFER: {listing.get('title')}")
                notify(listing, url)
                app_matches.append({
                    "key": key,
                    "title": listing.get("title"),
                    "rooms": listing.get("rooms"),
                    "size_qm": listing.get("size_qm"),
                    "rent": listing.get("rent"),
                    "district": listing.get("district"),
                    "url": listing.get("url") or url,
                    "site_url": url,
                    "source_name": source_name(url),
                    "found_at": datetime.now(timezone.utc).isoformat(),
                })
                total_new_matches += 1
                time.sleep(0.5)

        if new_on_this_site:
            print(f"  -> {new_on_this_site} davon neu seit letztem Lauf")

        seen[url] = list(known)
        time.sleep(SLEEP_BETWEEN_SITES)

    save_json(SEEN_FILE, seen)
    save_json(HASH_FILE, hashes)
    save_json(ERROR_LOG, errors)
    update_app_data(app_matches)

    print()
    print(f"Fertig. {total_new_matches} neue(s) passende(s) Inserat(e) gemeldet.")
    if errors:
        print(f"{len(errors)} Seite(n) konnten nicht abgerufen werden (siehe {ERROR_LOG}).")


if __name__ == "__main__":
    main()
