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
import math
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin

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

# Dateien, die die Homescreen-App direkt anzeigt (liegen im Repo-Root,
# neben index.html, damit GitHub Pages sie ausliefern kann)
APP_DATA_FILE = "data.json"
STATUS_FILE = "site-status.json"
MAX_MATCHES_KEPT = 300  # ältere Treffer werden aus der App-Ansicht ausgeblendet

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

# "anthropic" (paid, sehr günstig, Batch API) oder "gemini" (kostenloser Tarif,
# dafür mit Rate-Limits statt Kosten). Umschaltbar über die Umgebungsvariable
# AI_PROVIDER, z.B. im GitHub-Actions-Workflow gesetzt.
AI_PROVIDER = os.environ.get("AI_PROVIDER", "anthropic").strip().lower()

MODEL = "claude-haiku-4-5-20251001"  # deutlich günstiger, für strukturierte Text-Extraktion ausreichend
GEMINI_MODEL = "gemini-2.5-flash-lite"  # kostenloser Tarif, für diese Aufgabe ausreichend
GEMINI_RATE_LIMIT_DELAY = 4.5        # Sekunden zwischen Gemini-Aufrufen, um im Free-Tier-RPM-Limit zu bleiben
MAX_TEXT_CHARS = 8000    # kürzerer Seitentext -> weniger Input-Tokens pro Aufruf
REQUEST_TIMEOUT = 25
SLEEP_BETWEEN_SITES = 1.5  # kleine Pause, um nicht wie ein aggressiver Bot zu wirken
SLEEP_BETWEEN_PAGES = 1.0  # Pause zwischen Folgeseiten derselben Website

MAX_EXTRA_PAGES = 3       # zusätzlich zur ersten Seite max. 3 Folgeseiten laden
MAX_DISCOVERY_TRIES = 1   # wie viele vermutete Unterseiten bei 0 Treffern probiert werden

# "Aussichtslos"-Erkennung: nach wie vielen aufeinanderfolgenden erfolglosen
# Läufen (Fehler oder 0 Inserate) eine Seite als vermutlich dauerhaft tot
# markiert wird, und wie selten sie danach noch (kostenpflichtig) neu geprüft wird.
HOPELESS_THRESHOLD = 5
HOPELESS_RECHECK_HOURS = 24 * 21  # ca. alle 3 Wochen erneut versuchen

# Textfragmente, an denen "nächste Seite"-Links erkannt werden
NEXT_PAGE_TEXTS = {"weiter", "nächste", "nächste seite", "next", "vor", "›", "»", ">"}

# Schlüsselwörter, mit denen eine vermutete Angebots-Unterseite gesucht wird
# (höherer Wert = wahrscheinlicher die richtige Seite)
LISTING_LINK_KEYWORDS = [
    ("mieten", 3), ("miete", 3), ("vermietung", 3), ("mietangebote", 3),
    ("wohnungsangebote", 3), ("wohnungen", 2), ("angebote", 2),
    ("objekte", 2), ("immobilien", 1),
]

CRITERIA = {
    "min_rooms": 3,
    "min_size_qm": 80,
    "min_rent": 1200,
    "max_rent": 2100,
    # Freitext-Fragmente als Rückfalloption, falls keine PLZ erkannt wurde
    # (Wortgrenzen-Suche, siehe matches_criteria)
    "districts": ["mitte", "prenzlauer berg", "prenzlauer", "charlottenburg"],
}

# Präzise Standortfilterung über Postleitzahlen (zuverlässiger als Bezirks-
# namen, die uneinheitlich benannt werden). Ortsteil-Zuordnung nach den
# offiziellen Berliner PLZ-Grenzen:
PLZ_MITTE = {"10115", "10117", "10119", "10178", "10179"}
PLZ_PRENZLAUER_BERG = {"10405", "10407", "10409", "10435", "10437", "10439"}
ALLOWED_PLZ = PLZ_MITTE | PLZ_PRENZLAUER_BERG

# Charlottenburg wird NICHT über PLZ, sondern über einen echten Umkreis von
# 1 km um den Savignyplatz gefiltert (PLZ-Gebiete sind dafür viel zu groß).
# Nur PLZ, die überhaupt in Frage kommen, lösen den (kostenlosen, aber
# ratenlimitierten) Geocoding-Aufruf aus - spart unnötige Anfragen.
CHARLOTTENBURG_CANDIDATE_PLZ = {
    "10585", "10587", "10589", "10623", "10625", "10627", "10629",
    "10707", "10709",  # angrenzendes Wilmersdorf, falls Grenzfall
}
SAVIGNYPLATZ_COORDS = (52.5049, 13.3225)
CHARLOTTENBURG_RADIUS_KM = 1.0

# Tiergarten/Moabit: nur Wohnungen unmittelbar an der Spree. Auch hier zu
# grob für PLZ-Whitelisting - stattdessen Abstand zum Flusslauf berechnen.
# Die Referenzpunkte sind eine grobe, von Hand geschätzte Näherung des
# Spreeverlaufs durch diesen Abschnitt (keine vermessenen GIS-Daten) -
# bei Bedarf gerne nachjustieren, falls Ergebnisse unplausibel wirken.
TIERGARTEN_MOABIT_CANDIDATE_PLZ = {"10551", "10553", "10555", "10557", "10559", "10785"}
SPREE_REFERENCE_POINTS = [
    (52.5210, 13.3230),  # Grenze zu Charlottenburg (Schlossbrücke-Bereich)
    (52.5250, 13.3350),  # Westhafen / Beusselstraße
    (52.5290, 13.3450),  # Moabit, nördlicher Bogen
    (52.5300, 13.3580),  # Sandkrugbrücke / Invalidenstraße
    (52.5260, 13.3680),  # Höhe Hauptbahnhof
    (52.5195, 13.3745),  # Spreebogen / Regierungsviertel
    (52.5175, 13.3800),  # Richtung Reichstag / Übergang zu Mitte
]
SPREE_PROXIMITY_KM = 0.25  # "unmittelbar an der Spree"

GEOCODE_RATE_LIMIT_SECONDS = 1.1  # Nominatim-Nutzungsregeln: max. 1 Anfrage/Sekunde
_geocode_cache = {}

# True = Inserate ganz ohne erkennbare PLZ/Bezirk werden ausgefiltert
# (präziser, kann aber vereinzelt echte Treffer ohne erkannte Lage kosten).
# False = solche Inserate werden durchgelassen (bisheriges, großzügigeres Verhalten).
STRICT_LOCATION_FILTER = True

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

WICHTIG - nimm NUR aktuell verfügbare Angebote auf. Viele Hausverwaltungs-Seiten
zeigen zusätzlich "Referenzobjekte" oder "Referenzen" - das sind Wohnungen, die
bereits vermietet wurden und nur als Beispiel/Erfolgsgeschichte/Portfolio gezeigt
werden. Diese NICHT aufnehmen. Erkennungsmerkmale für solche Nicht-Angebote:
- Überschriften/Bereiche wie "Referenzen", "Referenzobjekte", "Erfolgreich
  vermietet", "Unsere Projekte", "Portfolio", "Beispielobjekte"
- Explizite Kennzeichnung als "vermietet", "reserviert", "nicht mehr verfügbar",
  "bereits vergeben", "ausverkauft"
- Fehlender Bezug zu einer aktuellen Bewerbung/Kontaktaufnahme (z.B. kein
  "Jetzt bewerben"/"Kontakt aufnehmen"-Bezug, sondern reine Rückschau)

Im Zweifel (nicht eindeutig erkennbar, ob aktuell verfügbar oder nur Referenz):
trotzdem aufnehmen, aber "status" auf "unklar" setzen.

Jedes Objekt im Array soll folgende Felder haben (wenn ein Wert nicht auf der
Seite steht: null):
- "title": kurze Bezeichnung / Adresse des Inserats
- "rooms": Zimmeranzahl als Zahl
- "size_qm": Wohnfläche in Quadratmetern als Zahl
- "rent": Kaltmiete in Euro als Zahl (nur die Zahl, ohne Symbol)
- "district": Stadtteil/Bezirk, falls erkennbar
- "plz": 5-stellige Postleitzahl, falls im Text erkennbar (sonst null)
- "street": Straße und Hausnummer, falls im Text erkennbar (sonst null)
- "url": Direktlink zum Inserat, falls vorhanden (sonst null)
- "status": "verfuegbar" (aktuell zu vermieten), "vermietet" (bereits vergeben/
  Referenz) oder "unklar"

Antworte AUSSCHLIESSLICH mit dem JSON-Array. Kein einleitender Text, keine
Markdown-Codeblöcke, keine Erklärung. Falls keine Inserate erkennbar sind,
antworte mit einem leeren Array: []

Seiteninhalt:
{text}
"""


REFERENCE_KEYWORDS = [
    "referenzobjekt", "referenzobjekte", "referenz ", "referenzen",
    "erfolgreich vermietet", "bereits vermietet", "bereits vergeben",
    "vermietet zum", "unsere projekte", "portfolio", "beispielobjekt",
    "erfolgsgeschichte", "abgeschlossenes projekt", "nicht mehr verfügbar",
    "ausverkauft",
]


def looks_like_reference(listing):
    """Sicherheitsnetz zusätzlich zum Prompt: erkennt Referenz-/Vermietet-Objekte
    anhand von Status-Feld und Schlüsselwörtern in Titel/Bezirk."""
    status = (listing.get("status") or "").lower()
    if "vermietet" in status:
        return True
    haystack = f"{listing.get('title') or ''} {listing.get('district') or ''}".lower()
    return any(kw in haystack for kw in REFERENCE_KEYWORDS)


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


_resolved_gemini_model = None  # wird bei Bedarf einmal pro Lauf ermittelt und gecacht


def resolve_gemini_model():
    """Fragt bei Google die aktuell verfügbaren Modelle ab und wählt ein
    passendes 'flash'-Modell, das generateContent unterstützt. Wird nur
    aufgerufen, wenn das fest hinterlegte Modell (404) nicht mehr existiert -
    macht das Skript robust gegen Google-seitige Modell-Umbenennungen."""
    global _resolved_gemini_model
    if _resolved_gemini_model:
        return _resolved_gemini_model

    print("  Gemini-Modell nicht gefunden, frage aktuell verfügbare Modelle ab …")
    resp = requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": GEMINI_API_KEY},
        timeout=30,
    )
    resp.raise_for_status()
    models = resp.json().get("models", [])

    candidates = [
        m["name"].split("/")[-1] for m in models
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]
    # "flash-lite" bevorzugen (schnell, für unsere einfache Extraktion
    # ausreichend, meist großzügigster Free-Tier), sonst irgendein "flash"
    pick = next((c for c in candidates if "flash-lite" in c), None) \
        or next((c for c in candidates if "flash" in c), None) \
        or (candidates[0] if candidates else None)

    if not pick:
        raise RuntimeError("Kein nutzbares Gemini-Modell mit generateContent gefunden.")

    print(f"  -> verwende stattdessen: {pick}")
    _resolved_gemini_model = pick
    return pick


def call_gemini_extract(url, text, _retry=0, _model=None):
    """Extraktion über die kostenlose Gemini API (statt Anthropic)."""
    if not text or len(text) < 50:
        return []

    model = _model or GEMINI_MODEL
    prompt = EXTRACTION_PROMPT.format(url=url, text=text)
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"content-type": "application/json"},
        params={"key": GEMINI_API_KEY},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 2500},
        },
        timeout=60,
    )

    # Modell existiert nicht (mehr) -> aktuell verfügbares Modell ermitteln und erneut versuchen
    if resp.status_code == 404 and _retry < 1:
        new_model = resolve_gemini_model()
        return call_gemini_extract(url, text, _retry=_retry + 1, _model=new_model)

    # Free-Tier-Rate-Limit getroffen (429) -> kurz warten, einmal erneut versuchen
    if resp.status_code == 429 and _retry < 3:
        wait = 20 * (_retry + 1)
        print(f"    Gemini Rate-Limit erreicht, warte {wait}s …")
        time.sleep(wait)
        return call_gemini_extract(url, text, _retry=_retry + 1, _model=model)

    resp.raise_for_status()
    data = resp.json()
    try:
        candidates = data.get("candidates", [])
        raw = "".join(
            p.get("text", "") for p in candidates[0]["content"]["parts"]
        ) if candidates else ""
    except (KeyError, IndexError):
        raw = ""

    return _parse_extraction_text(raw)


def extract_listings(url, text):
    """Einheitlicher Einstiegspunkt für die Extraktion, unabhängig vom
    gewählten Anbieter (AI_PROVIDER: 'anthropic' oder 'gemini')."""
    if AI_PROVIDER == "gemini":
        listings = call_gemini_extract(url, text)
        time.sleep(GEMINI_RATE_LIMIT_DELAY)  # Free-Tier-RPM-Limit einhalten
        return listings
    return call_claude_extract(url, text)


def find_next_page_url(html, current_url):
    """Sucht einen 'nächste Seite'-Link auf der aktuellen Seite."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(current_url).netloc

    # 1) rel="next" ist der eindeutigste Fall
    rel_next = soup.find("a", rel=lambda v: v and "next" in v)
    if rel_next and rel_next.get("href"):
        candidate = urljoin(current_url, rel_next["href"])
        if urlparse(candidate).netloc == base_host and candidate != current_url:
            return candidate

    # 2) Linktext, der auf "weiter/nächste Seite" hindeutet
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        classes = " ".join(a.get("class", [])).lower()
        aria = (a.get("aria-label") or "").lower()
        if text in NEXT_PAGE_TEXTS or "next" in classes or "next" in aria:
            candidate = urljoin(current_url, a["href"])
            if urlparse(candidate).netloc == base_host and candidate != current_url:
                return candidate

    return None


def find_listing_subpage(html, current_url):
    """Sucht bei 0 erkannten Inseraten eine vermutete Angebots-Unterseite."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(current_url).netloc

    best_url, best_score = None, 0
    seen_candidates = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("#") or href.lower().startswith("mailto:") or href.lower().startswith("tel:"):
            continue
        candidate = urljoin(current_url, href)
        parsed = urlparse(candidate)
        if parsed.netloc != base_host or candidate == current_url:
            continue
        if candidate in seen_candidates:
            continue
        seen_candidates.add(candidate)

        text = a.get_text(strip=True).lower()
        haystack = f"{text} {candidate.lower()}"
        score = sum(weight for kw, weight in LISTING_LINK_KEYWORDS if kw in haystack)
        # kürzere Pfade (weniger tief verschachtelt) leicht bevorzugen
        score -= parsed.path.count("/") * 0.1

        if score > best_score:
            best_score, best_url = score, candidate

    return best_url if best_score > 0 else None


def hours_since(iso_timestamp):
    if not iso_timestamp:
        return 999999
    try:
        then = datetime.fromisoformat(iso_timestamp)
        return (datetime.now(timezone.utc) - then).total_seconds() / 3600
    except Exception:
        return 999999


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geocode_address(query):
    """Kostenloses Geocoding über OpenStreetMap/Nominatim. Gibt (lat, lon)
    oder None zurück. Ergebnisse werden pro Lauf gecacht, Anfragen werden
    gemäß Nominatim-Nutzungsregeln auf max. 1/Sekunde gedrosselt."""
    if not query:
        return None
    if query in _geocode_cache:
        return _geocode_cache[query]

    time.sleep(GEOCODE_RATE_LIMIT_SECONDS)
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "de"},
            headers={"User-Agent": "ImmoWatcher/1.0 (privates Wohnungssuche-Tool)"},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            _geocode_cache[query] = None
            return None
        coords = (float(results[0]["lat"]), float(results[0]["lon"]))
        _geocode_cache[query] = coords
        return coords
    except Exception as e:
        print(f"    Geocoding fehlgeschlagen für '{query}': {e}")
        _geocode_cache[query] = None
        return None


def _geocode_query_for_listing(listing):
    street = (listing.get("street") or "").strip()
    title = (listing.get("title") or "").strip()
    plz = str(listing.get("plz") or "").strip()
    parts = [street or title, plz, "Berlin", "Germany"]
    return ", ".join(p for p in parts if p)


def is_within_charlottenburg_radius(listing):
    """Prüft per Geocoding, ob ein Inserat innerhalb von
    CHARLOTTENBURG_RADIUS_KM um den Savignyplatz liegt."""
    coords = geocode_address(_geocode_query_for_listing(listing))
    if not coords:
        return False  # nicht auffindbar -> im Zweifel nicht aufnehmen (präzise statt großzügig)

    distance = haversine_km(*coords, *SAVIGNYPLATZ_COORDS)
    return distance <= CHARLOTTENBURG_RADIUS_KM


def distance_to_spree_km(lat, lon):
    """Kürzester Abstand eines Punkts zum (grob angenäherten) Spreeverlauf,
    berechnet über Punkt-zu-Strecke-Distanz auf einer lokalen, ebenen
    Projektion (für Stadt-Maßstab genau genug)."""
    ref_lat = 52.52

    def to_xy(la, lo):
        x = math.radians(lo) * 6371000.0 * math.cos(math.radians(ref_lat))
        y = math.radians(la) * 6371000.0
        return x, y

    def point_segment_distance_m(px, py, ax, ay, bx, by):
        abx, aby = bx - ax, by - ay
        ab_len2 = abx * abx + aby * aby
        if ab_len2 == 0:
            return math.hypot(px - ax, py - ay)
        t = max(0, min(1, ((px - ax) * abx + (py - ay) * aby) / ab_len2))
        cx, cy = ax + t * abx, ay + t * aby
        return math.hypot(px - cx, py - cy)

    px, py = to_xy(lat, lon)
    pts_xy = [to_xy(la, lo) for la, lo in SPREE_REFERENCE_POINTS]
    best_m = min(
        point_segment_distance_m(px, py, pts_xy[i][0], pts_xy[i][1], pts_xy[i + 1][0], pts_xy[i + 1][1])
        for i in range(len(pts_xy) - 1)
    )
    return best_m / 1000.0


def is_within_spree_proximity(listing):
    """Prüft per Geocoding, ob ein Inserat in Tiergarten/Moabit unmittelbar
    (< SPREE_PROXIMITY_KM) an der Spree liegt."""
    coords = geocode_address(_geocode_query_for_listing(listing))
    if not coords:
        return False

    return distance_to_spree_km(*coords) <= SPREE_PROXIMITY_KM


def matches_criteria(listing):
    if looks_like_reference(listing):
        return False

    rooms = listing.get("rooms")
    size = listing.get("size_qm")
    rent = listing.get("rent")

    if isinstance(rooms, (int, float)) and rooms < CRITERIA["min_rooms"]:
        return False
    if isinstance(size, (int, float)) and size < CRITERIA["min_size_qm"]:
        return False
    if isinstance(rent, (int, float)):
        if not (CRITERIA["min_rent"] <= rent <= CRITERIA["max_rent"]):
            return False

    # Standort: PLZ ist das präzise Signal, wenn vorhanden. Nur als Fallback
    # (keine PLZ erkannt) wird auf den Bezirksnamen ausgewichen - mit
    # Wortgrenzen statt reiner Teilstring-Suche, um Fehltreffer wie "Mitte"
    # in unpassendem Kontext zu vermeiden.
    plz = str(listing.get("plz") or "").strip()
    district = (listing.get("district") or "").lower()
    title_lower = (listing.get("title") or "").lower()
    haystack = f"{district} {title_lower}"
    looks_like_charlottenburg = (
        plz in CHARLOTTENBURG_CANDIDATE_PLZ or re.search(r"\bcharlottenburg\b", haystack)
    )
    looks_like_tiergarten_moabit = (
        plz in TIERGARTEN_MOABIT_CANDIDATE_PLZ or re.search(r"\b(tiergarten|moabit)\b", haystack)
    )

    if looks_like_charlottenburg:
        # Charlottenburg: nicht per PLZ, sondern per echtem 1km-Umkreis um
        # den Savignyplatz prüfen (PLZ-Gebiete sind dafür zu groß)
        if not is_within_charlottenburg_radius(listing):
            return False
    elif looks_like_tiergarten_moabit:
        # Tiergarten/Moabit: nur zulassen, wenn unmittelbar an der Spree
        if not is_within_spree_proximity(listing):
            return False
    elif plz:
        if plz not in ALLOWED_PLZ:
            return False
    else:
        district_match = any(
            re.search(rf"\b{re.escape(d)}\b", haystack) for d in CRITERIA["districts"]
        )
        if not district_match:
            if STRICT_LOCATION_FILTER:
                return False
            # sonst: ohne erkennbare Lage trotzdem durchlassen (unschärfer)

    return True


def _normalize_key_part(v):
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v).strip().lower())


def listing_key(listing, site_url):
    """Eindeutiger, möglichst stabiler Schlüssel, um ein Inserat über mehrere
    Läufe hinweg wiederzuerkennen (wichtig für 'gesehen'-Tracking und für
    dauerhaftes Ausblenden in der App)."""
    url = listing.get("url")
    if url:
        return f"{site_url}::{_normalize_key_part(url)}"

    # Kein Direktlink vorhanden: aus strukturierten, stabilen Feldern einen
    # Schlüssel bauen statt aus dem freien Titel-Text - der wird von der KI
    # bei jedem Lauf leicht unterschiedlich formuliert und würde sonst bei
    # jedem Lauf einen neuen Schlüssel erzeugen (Inserat gilt fälschlich als
    # "neu", Ausblenden greift nicht mehr).
    structured_parts = [
        _normalize_key_part(listing.get("rooms")),
        _normalize_key_part(listing.get("size_qm")),
        _normalize_key_part(listing.get("rent")),
        _normalize_key_part(listing.get("plz") or listing.get("district")),
    ]
    if any(structured_parts):
        return f"{site_url}::{'|'.join(structured_parts)}"

    # Letzter Fallback: normalisierter Titel (zumindest Groß/Kleinschreibung
    # und doppelte Leerzeichen werden ausgeglichen)
    return f"{site_url}::{_normalize_key_part(listing.get('title'))}"


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


def update_site_status(status, url, ok, error=None, listing_count=None, suggested_url=None, pages_checked=None, skipped=False):
    now_iso = datetime.now(timezone.utc).isoformat()
    entry = status.get(url, {})

    if skipped:
        # Aussichtslose Seite im Cooldown: nur Zeitstempel touchen, keine
        # Zähler verändern, keine API-Kosten verursacht.
        entry["source_name"] = source_name(url)
        entry["last_checked"] = now_iso
        status[url] = entry
        return

    entry["source_name"] = source_name(url)
    entry["last_checked"] = now_iso
    if ok:
        entry["status"] = "ok"
        entry["error"] = None
        entry["last_success"] = now_iso
    else:
        entry["status"] = "error"
        entry["error"] = error
        entry.setdefault("last_success", None)
    if listing_count is not None:
        entry["last_listing_count"] = listing_count
        entry["last_listing_count_at"] = now_iso
    entry.setdefault("last_listing_count", None)
    if suggested_url is not None:
        entry["suggested_url"] = suggested_url
    if pages_checked is not None:
        entry["pages_checked"] = pages_checked

    had_success = ok and (listing_count or 0) > 0
    if had_success:
        entry["consecutive_bad_runs"] = 0
        entry["hopeless"] = False
    else:
        entry["consecutive_bad_runs"] = entry.get("consecutive_bad_runs", 0) + 1
        entry["hopeless"] = entry["consecutive_bad_runs"] >= HOPELESS_THRESHOLD

    status[url] = entry


BATCH_POLL_INTERVAL = 15        # Sekunden zwischen Status-Abfragen
BATCH_MAX_WAIT_SECONDS = 20 * 60  # maximal 20 Minuten auf den Batch warten


def _parse_extraction_text(raw):
    raw = (raw or "").strip()
    raw = re.sub(r"^```(json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    try:
        listings = json.loads(raw)
        return listings if isinstance(listings, list) else []
    except Exception:
        return []


def submit_batch(items):
    """items: Liste von (custom_id, url, text). Reicht einen Message-Batch ein
    und gibt die Batch-ID zurück (50% günstiger als einzelne Aufrufe)."""
    requests_payload = [
        {
            "custom_id": cid,
            "params": {
                "model": MODEL,
                "max_tokens": 2500,
                "messages": [{"role": "user", "content": EXTRACTION_PROMPT.format(url=url, text=text)}],
            },
        }
        for cid, url, text in items
    ]
    resp = requests.post(
        "https://api.anthropic.com/v1/messages/batches",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={"requests": requests_payload},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def wait_for_batch(batch_id):
    """Pollt den Batch-Status bis 'ended' oder Timeout. Gibt results_url oder None zurück."""
    waited = 0
    while waited < BATCH_MAX_WAIT_SECONDS:
        resp = requests.get(
            f"https://api.anthropic.com/v1/messages/batches/{batch_id}",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        status_str = data.get("processing_status")
        print(f"  Batch-Status: {status_str} (nach {waited}s)")
        if status_str == "ended":
            return data.get("results_url")
        time.sleep(BATCH_POLL_INTERVAL)
        waited += BATCH_POLL_INTERVAL
    return None


def fetch_batch_results(results_url):
    """Lädt die JSONL-Ergebnisse eines fertigen Batches.
    Gibt {custom_id: listings-Liste oder None (=Fehler)} zurück."""
    resp = requests.get(
        results_url,
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
        timeout=120,
    )
    resp.raise_for_status()
    out = {}
    for line in resp.text.strip().split("\n"):
        if not line.strip():
            continue
        row = json.loads(line)
        cid = row.get("custom_id")
        result = row.get("result", {})
        if result.get("type") == "succeeded":
            message = result.get("message", {})
            raw = "".join(
                b.get("text", "") for b in message.get("content", []) if b.get("type") == "text"
            )
            out[cid] = _parse_extraction_text(raw)
        else:
            out[cid] = None
    return out


def run_batch_extraction(items):
    """items: Liste von (custom_id, url, text). Führt die Extraktion per Batch API
    durch (50% günstiger). Fällt bei Problemen automatisch auf einzelne,
    synchrone Aufrufe zurück, damit ein Lauf nie komplett scheitert."""
    if not items:
        return {}

    try:
        print(f"Reiche Batch mit {len(items)} Anfrage(n) ein …")
        batch_id = submit_batch(items)
        results_url = wait_for_batch(batch_id)
        if results_url:
            results = fetch_batch_results(results_url)
            # Für alle, die im Batch fehlgeschlagen sind, synchron nachholen
            missing = [(cid, u, t) for cid, u, t in items if results.get(cid) is None]
            if missing:
                print(f"  {len(missing)} Batch-Eintrag/Einträge fehlgeschlagen, hole einzeln nach …")
                for cid, u, t in missing:
                    try:
                        results[cid] = call_claude_extract(u, t)
                    except Exception as e:
                        print(f"    -> weiterhin fehlgeschlagen ({u}): {e}")
                        results[cid] = []
            return results
        else:
            print("  Batch-Timeout erreicht, hole alle Einträge stattdessen einzeln ab …")
    except Exception as e:
        print(f"  Batch-Verarbeitung fehlgeschlagen ({e}), hole alle Einträge stattdessen einzeln ab …")

    # Fallback: klassische synchrone Einzelaufrufe (voller Preis, aber zuverlässig)
    results = {}
    for cid, u, t in items:
        try:
            results[cid] = call_claude_extract(u, t)
        except Exception as e:
            print(f"    -> Extraktion fehlgeschlagen ({u}): {e}")
            results[cid] = []
    return results


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
    if AI_PROVIDER == "gemini":
        if not GEMINI_API_KEY:
            print("FEHLER: AI_PROVIDER=gemini, aber GEMINI_API_KEY ist nicht gesetzt.", file=sys.stderr)
            sys.exit(1)
    else:
        if not ANTHROPIC_API_KEY:
            print("FEHLER: ANTHROPIC_API_KEY ist nicht gesetzt.", file=sys.stderr)
            sys.exit(1)

    sites = load_json(SITES_FILE, [])
    seen = load_json(SEEN_FILE, {})       # {site_url: [listing_key, ...]}
    hashes = load_json(HASH_FILE, {})     # {site_url: content_hash}
    status = load_json(STATUS_FILE, {})   # {site_url: {status, error, last_checked, last_success}}
    status = {u: status[u] for u in sites if u in status}  # entfernte URLs aufräumen
    errors = {}

    total_new_matches = 0
    app_matches = []  # Treffer für die Homescreen-App (docs/data.json)

    # ---- Phase 1: alle Seiten abrufen (kostenlos) und entscheiden, wer eine
    # Extraktion braucht. Wird gesammelt statt sofort einzeln an die API zu
    # schicken, damit alle Erst-Extraktionen zusammen als EIN Batch (50%
    # günstiger) verschickt werden können. ----
    pending = {}  # url -> {"html": ..., "prev_entry": ...}
    batch_items = []  # (custom_id, url, text)

    print("Phase 1/2: Seiten abrufen …")
    for i, url in enumerate(sites, 1):
        print(f"[{i}/{len(sites)}] {url}")
        html, err = fetch(url)

        if err:
            print(f"  -> Fehler beim Abrufen: {err}")
            errors[url] = err
            prev_entry = status.get(url, {})
            if prev_entry.get("hopeless") and hours_since(prev_entry.get("last_checked")) < HOPELESS_RECHECK_HOURS:
                update_site_status(status, url, ok=False, skipped=True)
            else:
                update_site_status(status, url, ok=False, error=err)
            continue

        prev_entry = status.get(url, {})
        if prev_entry.get("hopeless") and hours_since(prev_entry.get("last_checked")) < HOPELESS_RECHECK_HOURS:
            print("  -> als aussichtslos markiert, überspringe (Cooldown aktiv, keine API-Kosten).")
            update_site_status(status, url, ok=True, skipped=True)
            continue

        content_hash = hashlib.sha256(html.encode("utf-8", "ignore")).hexdigest()
        prev_count = prev_entry.get("last_listing_count")
        if hashes.get(url) == content_hash and prev_count:
            print("  -> keine Änderung seit letztem Lauf, überspringe.")
            update_site_status(status, url, ok=True, skipped=True)
            continue

        hashes[url] = content_hash
        pending[url] = {"html": html, "prev_entry": prev_entry}
        batch_items.append((url, url, extract_text(html)))

    # ---- Phase 2: gesammelte Erst-Extraktionen als EIN Batch verschicken ----
    print(f"\nPhase 2/2: {len(batch_items)} Seite(n) extrahieren (Anbieter: {AI_PROVIDER}) …")
    if AI_PROVIDER == "gemini":
        # Kein Vorteil durch Batch-Verarbeitung im kostenlosen Tarif -> einfach
        # nacheinander abfragen, mit Pause zur Einhaltung des Rate-Limits.
        batch_results = {}
        for i, (cid, u, t) in enumerate(batch_items, 1):
            print(f"  [{i}/{len(batch_items)}] {u}")
            batch_results[cid] = call_gemini_extract(u, t)
            time.sleep(GEMINI_RATE_LIMIT_DELAY)
    else:
        batch_results = run_batch_extraction(batch_items)

    for i, (url, info) in enumerate(pending.items(), 1):
        print(f"[{i}/{len(pending)}] Verarbeite Ergebnis: {url}")
        html = info["html"]
        listings = batch_results.get(url) or []

        pages_checked = 1
        suggested_url = info["prev_entry"].get("suggested_url")
        current_html, current_page_url = html, url

        # Unterseiten-Suche: nichts gefunden -> vermutete richtige Seite ausprobieren
        # (einzelner Aufruf, da abhängig vom Batch-Ergebnis - betrifft nur einen
        # kleinen Teil der Seiten und lohnt daher keinen eigenen Batch-Umweg)
        if not listings:
            candidate = find_listing_subpage(html, url)
            if candidate:
                sub_html, sub_err = fetch(candidate)
                pages_checked += 1
                if sub_html:
                    sub_listings = []
                    try:
                        sub_listings = extract_listings(candidate, extract_text(sub_html))
                    except Exception as e:
                        print(f"  -> Extraktion der Unterseite fehlgeschlagen: {e}")
                    if sub_listings:
                        print(f"  -> Unterseite gefunden mit Inseraten: {candidate}")
                        listings = sub_listings
                        suggested_url = candidate
                        current_html, current_page_url = sub_html, candidate
                time.sleep(SLEEP_BETWEEN_PAGES)

        # Folgeseiten: solange ein "weiter"-Link existiert und noch Inserate kommen
        extra_pages = 0
        seen_page_urls = {url, current_page_url}
        next_url = find_next_page_url(current_html, current_page_url) if listings else None
        while next_url and extra_pages < MAX_EXTRA_PAGES and next_url not in seen_page_urls:
            seen_page_urls.add(next_url)
            time.sleep(SLEEP_BETWEEN_PAGES)
            page_html, page_err = fetch(next_url)
            if not page_html:
                break
            pages_checked += 1
            extra_pages += 1
            try:
                page_listings = extract_listings(next_url, extract_text(page_html))
            except Exception as e:
                print(f"  -> Extraktion der Folgeseite fehlgeschlagen: {e}")
                break
            if not page_listings:
                break  # vermutlich Ende der Liste erreicht
            print(f"  -> Folgeseite {extra_pages} geladen: {len(page_listings)} weitere Inserat(e)")
            listings = listings + page_listings
            current_html = page_html
            next_url = find_next_page_url(page_html, next_url)

        print(f"  -> {len(listings)} Inserat(e) erkannt (über {pages_checked} Seite(n))")
        update_site_status(
            status, url, ok=True, listing_count=len(listings),
            suggested_url=suggested_url, pages_checked=pages_checked,
        )

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
                    "plz": listing.get("plz"),
                    "street": listing.get("street"),
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

    save_json(SEEN_FILE, seen)
    save_json(HASH_FILE, hashes)
    save_json(ERROR_LOG, errors)
    save_json(STATUS_FILE, status)
    update_app_data(app_matches)

    print()
    print(f"Fertig. {total_new_matches} neue(s) passende(s) Inserat(e) gemeldet.")
    if errors:
        print(f"{len(errors)} Seite(n) konnten nicht abgerufen werden (siehe {ERROR_LOG}).")


if __name__ == "__main__":
    main()
