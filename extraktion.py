"""
Inserate aus Seiten gewinnen.

Reihenfolge (billig -> teuer):
  1. Strukturierte Daten in der Seite: schema.org JSON-LD, Microdata,
     eingebettetes JSON (z. B. __NEXT_DATA__), mitgeschnittene XHR-Antworten
  2. Inserats-Feeds (RSS/Atom), sofern sie wirklich Inserate enthalten
  3. Vorfilter: Seiten ohne Preis-/Flaechenangaben brauchen keine KI
  4. KI-Extraktion (Gemini mit festem JSON-Schema, oder Anthropic)
"""

import hashlib
import json
import os
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from util import RateLimiter

# ---------------------------------------------------------------------------
# Konfiguration KI
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# "gemini", "anthropic" oder "none" (Trockenlauf ohne KI, z. B. zum Testen)
AI_PROVIDER = os.environ.get("AI_PROVIDER", "anthropic").strip().lower()

MODEL = "claude-haiku-4-5-20251001"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_MIN_ABSTAND_S = 4.5   # Free-Tier-RPM-Limit
GEMINI_TIMEOUT = 90
GEMINI_MAX_TIMEOUT_RETRIES = 2
QUOTA_EXHAUSTED_AFTER = 5
MAX_OUTPUT_TOKENS = 8192     # vorher 2500: lange Listen brachen mitten im JSON ab
MAX_TEXT_CHARS = 12000

_gemini_takt = RateLimiter(GEMINI_MIN_ABSTAND_S)
_resolved_gemini_model = None
_schema_abgelehnt = False
quota_exhausted = False
_consecutive_rate_limited = 0
ki_aufrufe = 0               # fuer die Lauf-Statistik
ki_abgeschnitten = 0         # Antworten, die am Token-Limit endeten

EXTRACTION_PROMPT = """Du bekommst den Textinhalt einer Immobilien-Angebotsseite (URL: {url}).

Extrahiere ALLE einzelnen Mietwohnungsinserate, die auf dieser Seite sichtbar sind.
Ignoriere Navigation, Footer, Werbetexte und Angebote zum KAUF (nur Miete zählt).

Nimm NUR aktuell verfügbare Angebote auf. "Referenzobjekte", "Referenzen",
"Erfolgreich vermietet", "Portfolio", "Unsere Projekte" sowie alles, was als
"vermietet", "reserviert" oder "vergeben" gekennzeichnet ist, NICHT aufnehmen.
Im Zweifel aufnehmen und "status" auf "unklar" setzen.

Felder je Inserat (nicht angegeben -> null):
- title: kurze Bezeichnung / Adresse
- rooms: Zimmeranzahl als Zahl (2,5 -> 2.5)
- size_qm: Wohnfläche in m² als Zahl
- rent: NUR die Kaltmiete (Nettokalt/Grundmiete) in Euro als Zahl. Steht nur
  eine Warm-/Brutto-/Gesamtmiete da: rent = null und den Betrag in rent_warm.
- rent_warm: Warmmiete/Gesamtmiete in Euro, falls angegeben
- district: Ortsteil/Bezirk/Kiez
- plz: 5-stellige Postleitzahl als Text
- street: Straße und Hausnummer
- url: Direktlink zum Inserat (so wie er auf der Seite steht)
- status: "verfuegbar", "vermietet" oder "unklar"
- wbs: true, wenn ein Wohnberechtigungsschein nötig ist; false, wenn ausdrücklich nicht
- moebliert: true bei möblierter Vermietung
- befristet: true bei Zeitmiete/befristetem Vertrag/Wohnen auf Zeit
- wg: true bei WG-Zimmer
- frei_ab: Bezugstermin als Text, z. B. "01.12.2026" oder "sofort"

Antworte ausschließlich mit einem kompakten JSON-Array ohne Einrückung.
Keine Inserate erkennbar: []

Seiteninhalt:
{text}
"""

_NULLBAR = lambda t: {"type": t, "nullable": True}  # noqa: E731
GEMINI_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "title": _NULLBAR("STRING"),
            "rooms": _NULLBAR("NUMBER"),
            "size_qm": _NULLBAR("NUMBER"),
            "rent": _NULLBAR("NUMBER"),
            "rent_warm": _NULLBAR("NUMBER"),
            "district": _NULLBAR("STRING"),
            "plz": _NULLBAR("STRING"),
            "street": _NULLBAR("STRING"),
            "url": _NULLBAR("STRING"),
            "status": {"type": "STRING", "enum": ["verfuegbar", "vermietet", "unklar"]},
            "wbs": _NULLBAR("BOOLEAN"),
            "moebliert": _NULLBAR("BOOLEAN"),
            "befristet": _NULLBAR("BOOLEAN"),
            "wg": _NULLBAR("BOOLEAN"),
            "frei_ab": _NULLBAR("STRING"),
        },
        "required": ["title", "status"],
    },
}


# ---------------------------------------------------------------------------
# Zahlen
# ---------------------------------------------------------------------------

def zahl_aus_text(s):
    """Liest eine Zahl und erkennt deutsche wie englische Schreibweise.
    '1.890,00' -> 1890.0, '1890.00' -> 1890.0, '1.890' -> 1890.0, '2,5' -> 2.5"""
    m = re.search(r"-?\d[\d.,]*", s or "")
    if not m:
        return None
    roh = m.group(0).rstrip(".,")
    hat_punkt, hat_komma = "." in roh, "," in roh
    if hat_punkt and hat_komma:
        if roh.rfind(",") > roh.rfind("."):
            roh = roh.replace(".", "").replace(",", ".")
        else:
            roh = roh.replace(",", "")
    elif hat_komma:
        roh = roh.replace(",", "." if re.search(r",\d{1,2}$", roh) else "")
    elif hat_punkt:
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", roh):
            roh = roh.replace(".", "")
    try:
        return float(roh)
    except ValueError:
        return None


def _wert_zahl(wert):
    """Zahl aus den ueblichen JSON-Schreibweisen (Zahl, Text, {value: ..})."""
    if isinstance(wert, bool):
        return None
    if isinstance(wert, (int, float)):
        return float(wert)
    if isinstance(wert, dict):
        for k in ("value", "minValue", "price", "amount", "betrag", "wert"):
            if k in wert:
                return _wert_zahl(wert[k])
        return None
    if isinstance(wert, str):
        return zahl_aus_text(wert)
    return None


_PLZ_BERLIN = re.compile(r"\b1[0-4]\d{3}\b")
_GRENZEN = {"rooms": (0.5, 20), "size_qm": (8, 1000), "rent": (100, 30000),
            "rent_warm": (100, 30000)}


def _bool(v):
    if v is True or v is False:
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "ja", "yes", "1"):
            return True
        if s in ("false", "nein", "no", "0"):
            return False
    return None


def normalisiere_inserat(listing):
    """Bringt jedes Inserat (egal aus welcher Quelle) in ein festes Format.

    Wichtig: Kamen Zahlen als Text ("3,5"), schlugen frueher alle
    isinstance-Pruefungen fehl - das Kriterium galt dann als 'unbekannt'
    und das Inserat rutschte ungeprueft durch."""
    if not isinstance(listing, dict):
        return None
    out = dict(listing)
    for k, (lo, hi) in _GRENZEN.items():
        v = _wert_zahl(out.get(k))
        if v is not None and not (lo <= v <= hi):
            v = None  # unplausibel (z. B. Kaufpreis statt Miete)
        if v is not None and v == int(v):
            v = int(v)
        out[k] = v
    for k in ("title", "district", "street", "url", "frei_ab", "status"):
        v = out.get(k)
        out[k] = re.sub(r"\s+", " ", v).strip() if isinstance(v, str) and v.strip() else None
    plz = str(out.get("plz") or "")
    m = _PLZ_BERLIN.search(plz) or _PLZ_BERLIN.search(
        f"{out.get('street') or ''} {out.get('title') or ''} {out.get('district') or ''}")
    out["plz"] = m.group(0) if m else None
    for k in ("wbs", "moebliert", "befristet", "wg"):
        out[k] = _bool(out.get(k))
    if out.get("wbs_required") is not None:
        out["wbs"] = out["wbs"] or _bool(out.pop("wbs_required"))
    out["status"] = (out.get("status") or "unklar").lower()
    return out


def ist_brauchbar(listings):
    """Strukturierte Treffer nur verwenden, wenn sie Kenngroessen haben -
    manche Seiten zeichnen nur Name und Preis aus. Dann lieber die KI."""
    if not listings:
        return False
    mit_daten = sum(1 for l in listings if l.get("rooms") or l.get("size_qm"))
    return mit_daten >= max(1, len(listings) / 2)


def _dedupe(listings):
    gesehen, raus = set(), []
    for e in listings:
        k = (e.get("title"), e.get("rooms"), e.get("size_qm"), e.get("rent"), e.get("url"))
        if k not in gesehen:
            gesehen.add(k)
            raus.append(e)
    return raus


# ---------------------------------------------------------------------------
# Text fuer die KI
# ---------------------------------------------------------------------------
_NOISE_ATTR = re.compile(
    r"(cookie|consent|gdpr|datenschutzhinweis|cmp-|navbar|main-?navigation|"
    r"mainmenu|hauptmenu|megamenu|breadcrumb|skip-link|social-|newsletter|"
    r"site-footer|page-footer|site-header|topbar)"
)
_INSERAT_SIGNAL = re.compile(r"(€|EUR\b|m²|\bqm\b|\bZimmer\b|\bZi\.|Kaltmiete|Wohnfläche)",
                             re.IGNORECASE)
_FENSTER = 2000


def extract_text(html):
    soup = BeautifulSoup(html, "html.parser")
    # "form" und "iframe" bewusst NICHT entfernen (Makler-Widgets).
    for tag in soup(["script", "style", "nav", "footer", "svg", "noscript", "template"]):
        tag.decompose()
    for tag in soup.find_all(attrs={"class": _NOISE_ATTR}):
        tag.decompose()
    for tag in soup.find_all(attrs={"id": _NOISE_ATTR}):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return kuerze_auf_inserate(re.sub(r"\s+", " ", text))


def kuerze_auf_inserate(text):
    """Waehlt bei ueberlangen Seiten den Abschnitt mit der hoechsten Dichte
    an Inseratsmerkmalen, statt vorne abzuschneiden."""
    if len(text) <= MAX_TEXT_CHARS:
        return text
    dichten = [len(_INSERAT_SIGNAL.findall(text[s:s + _FENSTER]))
               for s in range(0, len(text), _FENSTER)]
    if not any(dichten):
        return text[:MAX_TEXT_CHARS]
    breite = max(1, MAX_TEXT_CHARS // _FENSTER)
    beste, start = -1, 0
    for i in range(max(1, len(dichten) - breite + 1)):
        s = sum(dichten[i:i + breite])
        if s > beste:
            beste, start = s, i
    return text[start * _FENSTER:start * _FENSTER + MAX_TEXT_CHARS]


def text_hash(text):
    return hashlib.sha256((text or "").encode("utf-8", "ignore")).hexdigest()[:20]


_KEINE_ANGEBOTE = re.compile(
    r"(derzeit|aktuell|momentan|zurzeit|zur zeit)\s+(leider\s+)?(keine|kein)\s+"
    r"(freien\s+|verfügbaren\s+|aktuellen\s+)?(wohnungen|angebote|objekte|mietangebote|"
    r"immobilien|mietobjekte|wohnungsangebote)", re.IGNORECASE)


def sieht_leer_aus(text):
    """Vorfilter: Lohnt ein KI-Aufruf ueberhaupt? Eine Seite mit weniger als
    drei Preis-/Flaechen-/Zimmerangaben enthaelt keine Inseratsliste. Steht
    ausdruecklich 'derzeit keine Angebote' da, reichen auch ein paar mehr."""
    signale = len(_INSERAT_SIGNAL.findall(text or ""))
    if signale < 3:
        return True
    return signale < 8 and bool(_KEINE_ANGEBOTE.search(text))


# ---------------------------------------------------------------------------
# Strukturierte Daten: JSON-LD + Microdata
# ---------------------------------------------------------------------------
_LD_IMMO_TYPEN = {
    "realestatelisting", "apartment", "house", "singlefamilyresidence",
    "residence", "accommodation", "suite", "apartmentcomplex",
}
_LD_SCHWACH = {"product", "place", "offer", "individualproduct"}
_WOHN_WORTE = re.compile(r"wohnung|zimmer|apartment|maisonette|penthouse|\bloft\b|altbau",
                         re.IGNORECASE)
_RE_ZIMMER = re.compile(r"(\d{1,2}(?:[.,]5)?)\s*(?:-\s*)?(?:zimmer|zi\.|raum|räume|rooms?)\b",
                        re.IGNORECASE)
_RE_QM = re.compile(r"(\d{2,4}(?:[.,]\d{1,2})?)\s*(?:m²|m2|qm|quadratmeter)", re.IGNORECASE)
_RE_KALT = re.compile(r"(?:kaltmiete|nettokaltmiete|grundmiete|miete kalt|kalt)\D{0,25}"
                      r"(\d{1,2}\.?\d{3}(?:,\d{2})?|\d{3,4}(?:,\d{2})?)", re.IGNORECASE)
_RE_WARM = re.compile(r"(?:warmmiete|gesamtmiete|bruttomiete|miete warm|warm)\D{0,25}"
                      r"(\d{1,2}\.?\d{3}(?:,\d{2})?|\d{3,4}(?:,\d{2})?)", re.IGNORECASE)
_RE_KAUF = re.compile(r"\b(kaufpreis|zu verkaufen|eigentumswohnung|kaufen)\b", re.IGNORECASE)


def _werte_aus_freitext(text):
    """Zimmer/Flaeche/Miete per Regex aus Freitext (Feed, Beschreibung)."""
    out = {}
    m = _RE_ZIMMER.search(text)
    if m:
        out["rooms"] = zahl_aus_text(m.group(1))
    m = _RE_QM.search(text)
    if m:
        out["size_qm"] = zahl_aus_text(m.group(1))
    m = _RE_KALT.search(text)
    if m:
        out["rent"] = zahl_aus_text(m.group(1))
    m = _RE_WARM.search(text)
    if m:
        out["rent_warm"] = zahl_aus_text(m.group(1))
    return out


def _ld_objekte(daten):
    """Alle Objekte in Dokumentreihenfolge (Breitensuche)."""
    from collections import deque
    stapel, raus = deque([daten]), []
    while stapel:
        d = stapel.popleft()
        if isinstance(d, list):
            stapel.extend(d)
        elif isinstance(d, dict):
            raus.append(d)
            stapel.extend(v for v in d.values() if isinstance(v, (dict, list)))
    return raus


def _ld_typen(d):
    t = d.get("@type")
    if isinstance(t, str):
        return {t.lower()}
    if isinstance(t, list):
        return {x.lower() for x in t if isinstance(x, str)}
    return set()


def _schema_org_eintrag(d, basis_url):
    """Ein schema.org-Objekt (aus JSON-LD oder Microdata) -> Inserat oder None."""
    typen = _ld_typen(d)
    angebot = d if "offer" in typen else (d.get("offers") or {})
    if isinstance(angebot, list):
        angebot = angebot[0] if angebot else {}
    if not isinstance(angebot, dict):
        angebot = {}

    name = d.get("name") or angebot.get("name")
    name = name if isinstance(name, str) else None
    beschreibung = d.get("description") if isinstance(d.get("description"), str) else ""
    freitext = f"{name or ''} {beschreibung}"

    ist_immo = bool(typen & _LD_IMMO_TYPEN)
    hat_merkmale = any(k in d for k in ("numberOfRooms", "floorSize", "numberOfBedrooms"))
    # Neu: Product/Place/Offer zaehlen, wenn Name/Beschreibung eine Wohnung
    # beschreiben (mehrere Quellen zeichnen ihre Inserate nur so aus).
    schwach = bool(typen & _LD_SCHWACH) and bool(_WOHN_WORTE.search(freitext))
    if not (ist_immo or hat_merkmale or schwach):
        return None
    if _RE_KAUF.search(freitext) and not re.search(r"miete", freitext, re.IGNORECASE):
        return None

    adresse = d.get("address") or {}
    if isinstance(adresse, list):
        adresse = adresse[0] if adresse else {}
    if isinstance(adresse, str):
        adresse = {"streetAddress": adresse}
    if not isinstance(adresse, dict):
        adresse = {}

    aus_text = _werte_aus_freitext(freitext)
    eintrag = {
        "title": name,
        "rooms": _wert_zahl(d.get("numberOfRooms")) or _wert_zahl(d.get("numberOfBedrooms"))
        or aus_text.get("rooms"),
        "size_qm": _wert_zahl(d.get("floorSize")) or aus_text.get("size_qm"),
        "rent": _wert_zahl(angebot.get("price")) or _wert_zahl(d.get("price"))
        or aus_text.get("rent"),
        "rent_warm": aus_text.get("rent_warm"),
        "plz": str(adresse.get("postalCode") or "").strip() or None,
        "district": adresse.get("addressLocality") or adresse.get("addressRegion"),
        "street": adresse.get("streetAddress"),
        "url": d.get("url") or angebot.get("url") or basis_url,
        "status": "verfuegbar",
    }
    if eintrag["rooms"] or eintrag["size_qm"] or eintrag["rent"]:
        return eintrag
    return None


def extract_jsonld_listings(soup, basis_url):
    kandidaten = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            kandidaten.extend(_ld_objekte(json.loads(tag.string or tag.get_text() or "")))
        except Exception:
            continue
    return [e for e in (_schema_org_eintrag(d, basis_url) for d in kandidaten) if e]


def extract_microdata_listings(soup, basis_url):
    """schema.org-Microdata (itemscope/itemprop) - zwei Quellen nutzen nur das."""
    raus = []
    for el in soup.find_all(attrs={"itemscope": True}):
        typ = (el.get("itemtype") or "").rsplit("/", 1)[-1]
        if not typ or typ.lower() not in (_LD_IMMO_TYPEN | _LD_SCHWACH):
            continue
        d = {"@type": typ, "address": {}}
        for p in el.find_all(attrs={"itemprop": True}):
            name = p["itemprop"].split()[0]
            wert = p.get("content") or p.get("href") or p.get("src") \
                or p.get_text(" ", strip=True)
            if name in ("postalCode", "streetAddress", "addressLocality", "addressRegion"):
                d["address"].setdefault(name, wert)
            else:
                d.setdefault(name, wert)
        e = _schema_org_eintrag(d, basis_url)
        if e:
            raus.append(e)
    return raus


# ---------------------------------------------------------------------------
# Generisches JSON (eingebettet oder per XHR nachgeladen)
# ---------------------------------------------------------------------------
def _n(k):
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


_JSON_FELDER = {
    "rooms": {"rooms", "zimmer", "anzahlzimmer", "numberofrooms", "roomcount",
              "roomscount", "noofrooms", "zimmeranzahl", "nrooms", "roomsnumber"},
    "size_qm": {"livingspace", "wohnflaeche", "wohnflache", "livingarea", "flaeche",
                "squaremeters", "sqm", "floorsize", "size", "area", "wohnflaecheca",
                "livingspacesize"},
    "rent": {"coldrent", "kaltmiete", "nettokaltmiete", "baserent", "basicrent", "rentcold",
             "grundmiete", "netrent", "netcoldrent", "rent", "miete", "price", "preis"},
    "rent_warm": {"warmrent", "warmmiete", "totalrent", "gesamtmiete", "bruttomiete",
                  "rentwarm", "grossrent"},
    "plz": {"zip", "zipcode", "plz", "postalcode", "postcode"},
    "street": {"street", "strasse", "streetaddress", "addressstreet", "streetname"},
    "hausnr": {"housenumber", "hausnummer", "streetnumber"},
    "district": {"district", "ortsteil", "bezirk", "quarter", "neighbourhood",
                 "neighborhood", "regionaladdition"},
    "title": {"title", "name", "headline", "objekttitel", "titel", "label"},
    "url": {"url", "link", "detailurl", "href", "permalink", "exposeurl", "detaillink",
            "deeplink"},
}
_FELD_VON = {schl: feld for feld, schl_set in _JSON_FELDER.items() for schl in schl_set}
_VERSCHACHTELT = {"address", "adresse", "location", "price", "prices", "preise", "details",
                  "features", "geo", "estate", "attributes", "properties", "costs", "kosten",
                  "realestate", "object", "objekt", "data"}
_KAUF_WERTE = re.compile(r"\b(kauf|buy|sale|purchase)\b", re.IGNORECASE)


def _flach(d, tiefe=0):
    """Dict plus Felder aus typischen Unterobjekten (address, price, ...)."""
    out = {}
    for k, v in d.items():
        nk = _n(k)
        if isinstance(v, dict) and tiefe < 2 and nk in _VERSCHACHTELT:
            for k2, v2 in _flach(v, tiefe + 1).items():
                out.setdefault(k2, v2)
        elif not isinstance(v, (dict, list)) or nk in _FELD_VON:
            out.setdefault(nk, v)
    return out


def inserate_aus_json(daten, basis_url):
    """Durchsucht beliebiges JSON nach Objekten, die wie ein Inserat aussehen
    (Zimmer oder Flaeche UND Miete oder Adresse). Deckt Makler-Widgets ab,
    deren Inserate als JSON in der Seite stehen oder nachgeladen werden."""
    raus = []
    for d in _ld_objekte(daten):
        flach = _flach(d)
        werte = {}
        for k, v in flach.items():
            feld = _FELD_VON.get(k)
            if feld and feld not in werte and v not in (None, "", [], {}):
                werte[feld] = v
        hat_kenn = _wert_zahl(werte.get("rooms")) or _wert_zahl(werte.get("size_qm"))
        hat_bezug = _wert_zahl(werte.get("rent")) or _wert_zahl(werte.get("rent_warm")) \
            or any(isinstance(werte.get(k), (str, int)) for k in ("plz", "street"))
        if not (hat_kenn and hat_bezug):
            continue
        if any(isinstance(v, str) and _KAUF_WERTE.search(v)
               for k, v in flach.items() if "market" in k or "vermarktung" in k or k == "type"):
            continue
        strasse = werte.get("street") if isinstance(werte.get("street"), str) else None
        if strasse and werte.get("hausnr") and isinstance(werte["hausnr"], (str, int)):
            strasse = f"{strasse} {werte['hausnr']}"
        url = werte.get("url") if isinstance(werte.get("url"), str) else None
        eintrag = {
            "title": werte.get("title") if isinstance(werte.get("title"), str) else strasse,
            "rooms": _wert_zahl(werte.get("rooms")),
            "size_qm": _wert_zahl(werte.get("size_qm")),
            "rent": _wert_zahl(werte.get("rent")),
            "rent_warm": _wert_zahl(werte.get("rent_warm")),
            "plz": str(werte.get("plz")) if werte.get("plz") else None,
            "street": strasse,
            "district": werte.get("district") if isinstance(werte.get("district"), str) else None,
            "url": urljoin(basis_url, url) if url and not url.startswith(("mailto:", "tel:"))
            else basis_url,
            "status": "verfuegbar",
        }
        # Kaufpreise sind keine Miete
        if eintrag["rent"] and eintrag["rent"] > 30000:
            continue
        raus.append(eintrag)
    return raus


def eingebettetes_json(soup):
    """JSON-Bloecke in <script type="application/json"> (z. B. __NEXT_DATA__)."""
    daten = []
    for tag in soup.find_all("script", attrs={"type": "application/json"}):
        roh = tag.string or tag.get_text() or ""
        if len(roh) < 50 or len(roh) > 5_000_000:
            continue
        try:
            daten.append(json.loads(roh))
        except Exception:
            continue
    return daten


def strukturierte_inserate(html, basis_url, xhr_daten=None):
    """Alle KI-freien Wege in einem. Gibt eine Liste (evtl. leer) zurueck;
    nur 'brauchbare' Ergebnisse (mit Kenngroessen) werden geliefert."""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return [], None
    for art, finder in (
        ("json-ld", lambda: extract_jsonld_listings(soup, basis_url)),
        ("microdata", lambda: extract_microdata_listings(soup, basis_url)),
        ("eingebettetes json", lambda: [e for d in eingebettetes_json(soup)
                                        for e in inserate_aus_json(d, basis_url)]),
        ("xhr-json", lambda: [e for d in (xhr_daten or [])
                              for e in inserate_aus_json(d, basis_url)]),
    ):
        try:
            gefunden = [normalisiere_inserat(e) for e in finder()]
        except Exception as e:
            print(f"    ({art}-Auswertung fehlgeschlagen: {e})")
            continue
        gefunden = _dedupe([e for e in gefunden if e])
        if ist_brauchbar(gefunden):
            return gefunden, art
    return [], None


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------
# Von 36 gefundenen Feeds sind die allermeisten WordPress-Blog- oder
# Kommentar-Feeds ohne Inserate. Nur Feeds, deren Adresse nach Objekten
# klingt, werden versucht - und nur verwendet, wenn die Eintraege
# tatsaechlich Zimmer-/Flaechenangaben enthalten.
_FEED_TYPEN = ("application/rss+xml", "application/atom+xml", "application/feed+json")
_FEED_INSERAT_URL = re.compile(r"(objekt|immobil|miet|angebot|propert|estate|expose|"
                               r"\bjea\b|component/jea|listing|wohnung)", re.IGNORECASE)


def finde_inserats_feeds(html, basis_url):
    soup = BeautifulSoup(html or "", "html.parser")
    raus = []
    for link in soup.find_all("link", attrs={"rel": True, "href": True}):
        rel = " ".join(link.get("rel") or []).lower()
        typ = (link.get("type") or "").lower()
        if "alternate" in rel and typ in _FEED_TYPEN:
            url = urljoin(basis_url, link["href"])
            if "comments" not in url and _FEED_INSERAT_URL.search(url):
                raus.append(url)
    return list(dict.fromkeys(raus))


def inserate_aus_feed(xml, basis_url):
    # Der HTML-Parser behandelt <link> als leeres Element und verliert den
    # RSS-Link dahinter - deshalb vorher umbenennen (Atom-<link href> bleibt).
    xml = re.sub(r"<(/?)link>", r"<\1feedlink>", xml or "")
    soup = BeautifulSoup(xml, "html.parser")
    eintraege = soup.find_all("item") or soup.find_all("entry")
    raus = []
    for it in eintraege:
        titel = it.find("title").get_text(" ", strip=True) if it.find("title") else ""
        link_el = it.find("feedlink") or it.find("link")
        link = (link_el.get("href") or link_el.get_text(strip=True)) if link_el else None
        beschr = ""
        for tagname in ("description", "summary", "content", "content:encoded"):
            el = it.find(tagname)
            if el:
                beschr += " " + BeautifulSoup(el.get_text(" "), "html.parser").get_text(" ", strip=True)
        text = f"{titel} {beschr}"
        if _RE_KAUF.search(text) and "miete" not in text.lower():
            continue
        e = {"title": titel or None, "url": urljoin(basis_url, link) if link else basis_url,
             "status": "verfuegbar", **_werte_aus_freitext(text)}
        m = _PLZ_BERLIN.search(text)
        if m:
            e["plz"] = m.group(0)
        raus.append(normalisiere_inserat(e))
    raus = [e for e in raus if e]
    return raus if ist_brauchbar(raus) else []


# ---------------------------------------------------------------------------
# KI
# ---------------------------------------------------------------------------

def _rette_json_array(raw):
    """Parst die KI-Antwort. Endet sie mitten im JSON (Token-Limit), werden
    die vollstaendigen Eintraege gerettet, statt die ganze Seite zu verlieren."""
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    if not raw:
        return []
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            for v in d.values():
                if isinstance(v, list):
                    return v
            return []
        return d if isinstance(d, list) else []
    except Exception:
        pass
    start = raw.find("[")
    if start < 0:
        return []
    ende = len(raw)
    for _ in range(200):
        ende = raw.rfind("}", start, ende)
        if ende < 0:
            break
        try:
            d = json.loads(raw[start:ende + 1] + "]")
            if isinstance(d, list):
                print(f"    (abgeschnittene KI-Antwort: {len(d)} Eintrag/Einträge gerettet)")
                return d
        except Exception:
            pass
    print("    WARNUNG: KI-Antwort nicht lesbar - Seite liefert diesmal 0 Inserate.")
    return []


def resolve_gemini_model():
    global _resolved_gemini_model
    if _resolved_gemini_model:
        return _resolved_gemini_model
    print("  Gemini-Modell nicht gefunden, frage verfügbare Modelle ab …")
    resp = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                        params={"key": GEMINI_API_KEY}, timeout=30)
    resp.raise_for_status()
    kandidaten = [m["name"].split("/")[-1] for m in resp.json().get("models", [])
                  if "generateContent" in m.get("supportedGenerationMethods", [])]
    pick = next((c for c in kandidaten if "flash-lite" in c), None) \
        or next((c for c in kandidaten if "flash" in c), None) \
        or (kandidaten[0] if kandidaten else None)
    if not pick:
        raise RuntimeError("Kein nutzbares Gemini-Modell gefunden.")
    print(f"  -> verwende stattdessen: {pick}")
    _resolved_gemini_model = pick
    return pick


def call_gemini_extract(url, text, _retry=0, _model=None, _timeout_retry=0):
    global _consecutive_rate_limited, quota_exhausted, _schema_abgelehnt
    global ki_aufrufe, ki_abgeschnitten
    if not text or len(text) < 50 or quota_exhausted:
        return []

    model = _model or _resolved_gemini_model or GEMINI_MODEL
    gen_cfg = {"temperature": 0, "maxOutputTokens": MAX_OUTPUT_TOKENS}
    if not _schema_abgelehnt:
        # Festes Antwortformat: kein Markdown, keine kaputten Anfuehrungszeichen,
        # alle Felder mit dem richtigen Typ.
        gen_cfg["responseMimeType"] = "application/json"
        gen_cfg["responseSchema"] = GEMINI_SCHEMA

    _gemini_takt.warte()
    ki_aufrufe += 1
    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"content-type": "application/json"},
            params={"key": GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": EXTRACTION_PROMPT.format(url=url, text=text)}]}],
                  "generationConfig": gen_cfg},
            timeout=GEMINI_TIMEOUT,
        )
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        if _timeout_retry < GEMINI_MAX_TIMEOUT_RETRIES:
            wait = 10 * (_timeout_retry + 1)
            print(f"    Gemini-Anfrage fehlgeschlagen ({e.__class__.__name__}), warte {wait}s …")
            time.sleep(wait)
            return call_gemini_extract(url, text, _retry, model, _timeout_retry + 1)
        print(f"    Gemini-Anfrage endgültig fehlgeschlagen für {url}: {e}")
        return []

    if resp.status_code == 404 and _retry < 1:
        return call_gemini_extract(url, text, _retry + 1, resolve_gemini_model())
    if resp.status_code == 400 and not _schema_abgelehnt:
        # Meist: Modell kennt das Schema-Format nicht. Einmal ohne versuchen.
        print(f"    Gemini: HTTP 400 mit Antwortschema ({resp.text[:160]!r}) - weiter ohne Schema.")
        _schema_abgelehnt = True
        return call_gemini_extract(url, text, _retry, model)
    if resp.status_code == 429:
        if _retry < 2:
            wait = 20 * (_retry + 1)
            print(f"    Gemini Rate-Limit erreicht, warte {wait}s …")
            time.sleep(wait)
            return call_gemini_extract(url, text, _retry + 1, model)
        _consecutive_rate_limited += 1
        if _consecutive_rate_limited >= QUOTA_EXHAUSTED_AFTER:
            quota_exhausted = True
            print("    Kontingent offenbar erschöpft – weitere Extraktionen werden übersprungen.")
        return []
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        print(f"    Gemini-Anfrage fehlgeschlagen für {url}: {e}")
        return []

    _consecutive_rate_limited = 0
    data = resp.json()
    kandidaten = data.get("candidates") or []
    if not kandidaten:
        return []
    k0 = kandidaten[0]
    if k0.get("finishReason") == "MAX_TOKENS":
        ki_abgeschnitten += 1
    raw = "".join(p.get("text", "") for p in (k0.get("content") or {}).get("parts", []))
    return _rette_json_array(raw)


def _anthropic_headers():
    return {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
            "content-type": "application/json"}


def call_claude_extract(url, text):
    global ki_aufrufe
    if not text or len(text) < 50:
        return []
    ki_aufrufe += 1
    resp = requests.post(
        "https://api.anthropic.com/v1/messages", headers=_anthropic_headers(),
        json={"model": MODEL, "max_tokens": MAX_OUTPUT_TOKENS,
              "messages": [{"role": "user", "content": EXTRACTION_PROMPT.format(url=url, text=text)}]},
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return _rette_json_array(raw)


def extract_listings(url, text):
    """Einheitlicher Einstieg (synchron)."""
    if AI_PROVIDER == "none":
        return []
    if AI_PROVIDER == "gemini":
        return call_gemini_extract(url, text)
    try:
        return call_claude_extract(url, text)
    except Exception as e:
        print(f"    Anthropic-Anfrage fehlgeschlagen für {url}: {e}")
        return []


# --- Anthropic Batch API (50 % guenstiger) ---------------------------------
BATCH_POLL_INTERVAL = 15
BATCH_MAX_WAIT_SECONDS = 20 * 60


def run_batch_extraction(items):
    """items: Liste von (custom_id, url, text) -> {custom_id: listings}."""
    global ki_aufrufe
    if not items:
        return {}
    try:
        print(f"Reiche Batch mit {len(items)} Anfrage(n) ein …")
        resp = requests.post(
            "https://api.anthropic.com/v1/messages/batches", headers=_anthropic_headers(),
            json={"requests": [{
                "custom_id": cid,
                "params": {"model": MODEL, "max_tokens": MAX_OUTPUT_TOKENS,
                           "messages": [{"role": "user",
                                         "content": EXTRACTION_PROMPT.format(url=u, text=t)}]},
            } for cid, u, t in items]},
            timeout=60,
        )
        resp.raise_for_status()
        batch_id = resp.json()["id"]
        ki_aufrufe += len(items)
        waited, results_url = 0, None
        while waited < BATCH_MAX_WAIT_SECONDS:
            r = requests.get(f"https://api.anthropic.com/v1/messages/batches/{batch_id}",
                             headers=_anthropic_headers(), timeout=30)
            r.raise_for_status()
            info = r.json()
            print(f"  Batch-Status: {info.get('processing_status')} (nach {waited}s)")
            if info.get("processing_status") == "ended":
                results_url = info.get("results_url")
                break
            time.sleep(BATCH_POLL_INTERVAL)
            waited += BATCH_POLL_INTERVAL
        if results_url:
            r = requests.get(results_url, headers=_anthropic_headers(), timeout=120)
            r.raise_for_status()
            out = {}
            for line in r.text.strip().split("\n"):
                if not line.strip():
                    continue
                row = json.loads(line)
                res = row.get("result", {})
                if res.get("type") == "succeeded":
                    raw = "".join(b.get("text", "") for b in res.get("message", {}).get("content", [])
                                  if b.get("type") == "text")
                    out[row.get("custom_id")] = _rette_json_array(raw)
            for cid, u, t in items:
                if cid not in out:
                    out[cid] = extract_listings(u, t)
            return out
        print("  Batch-Timeout, hole einzeln ab …")
    except Exception as e:
        print(f"  Batch fehlgeschlagen ({e}), hole einzeln ab …")
    return {cid: extract_listings(u, t) for cid, u, t in items}
