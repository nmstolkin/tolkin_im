"""
Suchkriterien, Lageprüfung und Geocoding.

Alle Werte hier sind Vorgaben. config.json (auch aus der App änderbar)
überschreibt sie zur Laufzeit über wende_config_an().
"""

import math
import re
import time

import requests

from util import load_json, save_json, now_iso, hours_since

GEOCODE_CACHE_FILE = "data/geocode_cache.json"

CRITERIA = {
    "min_rooms": 3,
    "min_size_qm": 80,
    "min_rent": 1200,
    "max_rent": 2100,
    # Kiez-, Platz- und Quartiersnamen. Berliner Inserate benennen die Lage
    # fast nie nach dem Ortsteil ("Prenzlauer Berg"), sondern nach dem Kiez
    # ("Winskiez", "Kollwitzplatz"). Ohne diese Liste fallen sie durchs Raster.
    "kiez_hints": {
        "mitte": [
            "rosenthaler platz", "hackescher markt", "hackesche höfe",
            "museumsinsel", "spandauer vorstadt", "scheunenviertel",
            "nikolaiviertel", "alexanderplatz", "gendarmenmarkt",
            "oranienburger", "torstraße", "torstrasse", "monbijou",
            "chausseestraße", "chausseestrasse", "weinmeister",
        ],
        "prenzlauer_berg": [
            "kollwitzkiez", "kollwitzplatz", "kollwitzstraße", "kollwitzstrasse",
            "helmholtzplatz", "helmholtzkiez", "winskiez", "winsviertel",
            "bötzowviertel", "boetzowviertel", "mauerpark", "kastanienallee",
            "schönhauser allee", "schoenhauser allee", "rykestraße",
            "rykestrasse", "arnimplatz", "thälmannpark", "thaelmannpark",
            "gleimviertel", "falkplatz", "zionskirchplatz", "prenzlberg",
            "prenzl'berg", "prenzl´berg",
        ],
        "charlottenburg": [
            "savignyplatz", "savigny", "mommsenkiez", "mommsenstraße",
            "mommsenstrasse", "stuttgarter platz", "stuttgarter-platz",
            "kantstraße", "kantstrasse", "ludwigkirch", "richard-wagner-platz",
        ],
        "viktoria_luise": [
            "viktoria-luise-platz", "viktoria luise", "viktoria-luise",
            "bayerisches viertel", "bayerischen viertel", "bayerischer platz",
            "bayerischen platz", "winterfeldtplatz",
            "winterfeldtstraße", "winterfeldtstrasse", "nollendorfplatz",
            "barbarossaplatz", "barbarossastraße", "barbarossastrasse",
            "motzstraße", "motzstrasse", "regensburger straße",
            "regensburger strasse", "münchener straße", "muenchener strasse",
        ],
        "tiergarten_moabit_spree": [
            "europacity", "heidestraße", "heidestrasse", "nordhafen",
            "spreebogen", "hauptbahnhof", "lehrter", "alt-moabit",
            "stephankiez", "hansaviertel", "wasserstadt", "bellevue",
        ],
    },
    "areas": {
        "mitte": True,
        "prenzlauer_berg": True,
        "charlottenburg": True,
        "tiergarten_moabit_spree": True,
        "viktoria_luise": True,
    },
    "custom_area": {"enabled": False, "points": []},
    "allow_wbs": False,
    "allow_furnished": False,
    "allow_temporary": False,
    "allow_shared": False,
}

PLZ_MITTE = {"10115", "10117", "10119", "10178", "10179"}
PLZ_PRENZLAUER_BERG = {"10405", "10407", "10409", "10435", "10437", "10439"}

CHARLOTTENBURG_CANDIDATE_PLZ = {
    "10585", "10587", "10589", "10623", "10625", "10627", "10629",
    "10707", "10709",
}
SAVIGNYPLATZ_COORDS = (52.5049, 13.3225)

MITTE_CENTER = (52.5200, 13.4000)
MITTE_RADIUS_KM = 1.8
PRENZLAUER_BERG_CENTER = (52.5390, 13.4245)
PRENZLAUER_BERG_RADIUS_KM = 1.8
CHARLOTTENBURG_RADIUS_KM = 1.2

TIERGARTEN_MOABIT_CANDIDATE_PLZ = {"10551", "10553", "10555", "10557", "10559", "10785"}
SPREE_REFERENCE_POINTS = [
    (52.5210, 13.3230), (52.5250, 13.3350), (52.5290, 13.3450),
    (52.5300, 13.3580), (52.5260, 13.3680), (52.5195, 13.3745),
    (52.5175, 13.3800),
]
SPREE_PROXIMITY_KM = 0.25

VIKTORIA_LUISE_COORDS = (52.4958, 13.3415)
VIKTORIA_LUISE_RADIUS_KM = 1.0
VIKTORIA_LUISE_CANDIDATE_PLZ = {
    "10777", "10779", "10781", "10783", "10825", "10827", "10789",
}

STRICT_LOCATION_FILTER = True

# Knapp-verfehlt-Toleranzen
NEAR_MISS_SIZE_TOLERANCE = 0.12
NEAR_MISS_RENT_TOLERANCE = 0.12
NEAR_MISS_ROOMS_TOLERANCE = 0.5

# Steht nur eine Warmmiete im Inserat, wird die Kaltmiete geschaetzt.
# 3,00 EUR/qm entspricht grob Betriebs- plus Heizkosten im Berliner Altbau.
# Ohne Flaechenangabe: pauschal 20 % der Warmmiete.
NEBENKOSTEN_PRO_QM = 3.0
NEBENKOSTEN_ANTEIL_OHNE_QM = 0.20

GEOCODE_RATE_LIMIT_SECONDS = 1.1  # Nominatim-Nutzungsregeln: max. 1 Anfrage/Sekunde
GEOCODE_MISS_RETRY_DAYS = 30
GEOCODE_USER_AGENT = "ImmoWatcher/1.1 (privates Wohnungssuche-Tool)"
_geocode_cache = {}

# config.json-Schluessel -> Modulkonstante
FEINWERTE = {
    "charlottenburg_radius_km": "CHARLOTTENBURG_RADIUS_KM",
    "viktoria_luise_radius_km": "VIKTORIA_LUISE_RADIUS_KM",
    "spree_abstand_km": "SPREE_PROXIMITY_KM",
    "mitte_radius_km": "MITTE_RADIUS_KM",
    "prenzlauer_berg_radius_km": "PRENZLAUER_BERG_RADIUS_KM",
    "knapp_groesse_toleranz": "NEAR_MISS_SIZE_TOLERANCE",
    "knapp_miete_toleranz": "NEAR_MISS_RENT_TOLERANCE",
    "nebenkosten_pro_qm": "NEBENKOSTEN_PRO_QM",
}


def wende_config_an(cfg_criteria):
    """Uebernimmt Werte aus config.json["criteria"]. Gibt eine Liste der
    geaenderten Feineinstellungen zurueck (fuers Log)."""
    for key in ("min_rooms", "min_size_qm", "min_rent", "max_rent"):
        if key in cfg_criteria:
            CRITERIA[key] = cfg_criteria[key]
    for area_key in CRITERIA["areas"]:
        if area_key in cfg_criteria.get("areas", {}):
            CRITERIA["areas"][area_key] = bool(cfg_criteria["areas"][area_key])
    for flag in ("allow_wbs", "allow_furnished", "allow_temporary", "allow_shared"):
        if flag in cfg_criteria:
            CRITERIA[flag] = bool(cfg_criteria[flag])
    if "custom_area" in cfg_criteria:
        CRITERIA["custom_area"] = cfg_criteria["custom_area"] or {}

    geaendert = []
    for schluessel, konstante in FEINWERTE.items():
        if schluessel in cfg_criteria:
            try:
                globals()[konstante] = float(cfg_criteria[schluessel])
                geaendert.append(f"{schluessel}={cfg_criteria[schluessel]}")
            except (TypeError, ValueError):
                print(f"  Wert für {schluessel} unbrauchbar, Vorgabe bleibt.")
    return geaendert


# ---------------------------------------------------------------------------
# Geometrie
# ---------------------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def distance_to_spree_km(lat, lon):
    """Kürzester Abstand zum (grob angenäherten) Spreeverlauf."""
    ref_lat = 52.52

    def to_xy(la, lo):
        return (math.radians(lo) * 6371000.0 * math.cos(math.radians(ref_lat)),
                math.radians(la) * 6371000.0)

    def seg_dist(px, py, ax, ay, bx, by):
        abx, aby = bx - ax, by - ay
        ab_len2 = abx * abx + aby * aby
        if ab_len2 == 0:
            return math.hypot(px - ax, py - ay)
        t = max(0, min(1, ((px - ax) * abx + (py - ay) * aby) / ab_len2))
        return math.hypot(px - (ax + t * abx), py - (ay + t * aby))

    px, py = to_xy(lat, lon)
    pts = [to_xy(la, lo) for la, lo in SPREE_REFERENCE_POINTS]
    return min(seg_dist(px, py, *pts[i], *pts[i + 1]) for i in range(len(pts) - 1)) / 1000.0


def point_in_polygon(lat, lon, polygon_points):
    if len(polygon_points) < 3:
        return False
    inside = False
    n = len(polygon_points)
    j = n - 1
    for i in range(n):
        lat_i, lon_i = polygon_points[i]
        lat_j, lon_j = polygon_points[j]
        if ((lon_i > lon) != (lon_j > lon)) and \
                (lat < (lat_j - lat_i) * (lon - lon_i) / (lon_j - lon_i + 1e-15) + lat_i):
            inside = not inside
        j = i
    return inside


# ---------------------------------------------------------------------------
# Geocoding (OpenStreetMap: Nominatim, Rueckfall Photon)
# ---------------------------------------------------------------------------

def load_geocode_cache():
    global _geocode_cache
    raw = load_json(GEOCODE_CACHE_FILE, {})
    _geocode_cache = {}
    abgelaufen = 0
    for k, v in raw.items():
        if isinstance(v, list):
            _geocode_cache[k] = tuple(v)
        elif isinstance(v, dict) and "miss_at" in v:
            if hours_since(v["miss_at"]) < GEOCODE_MISS_RETRY_DAYS * 24:
                _geocode_cache[k] = v
            else:
                abgelaufen += 1
    treffer = sum(1 for v in _geocode_cache.values() if isinstance(v, tuple))
    print(f"Geocoding-Cache: {treffer} Adresse(n), "
          f"{len(_geocode_cache) - treffer} Fehlanzeige(n)"
          + (f", {abgelaufen} erneut zu prüfen" if abgelaufen else ""))


def save_geocode_cache():
    save_json(GEOCODE_CACHE_FILE, {
        k: (list(v) if isinstance(v, tuple) else v) for k, v in _geocode_cache.items()
    })


def geocode_photon(query):
    try:
        resp = requests.get(
            "https://photon.komoot.io/api/",
            params={"q": query, "limit": 1, "lang": "de"},
            headers={"User-Agent": GEOCODE_USER_AGENT}, timeout=15,
        )
        resp.raise_for_status()
        treffer = (resp.json() or {}).get("features") or []
        if not treffer:
            return None
        lon, lat = treffer[0]["geometry"]["coordinates"][:2]
        return (float(lat), float(lon))
    except Exception as e:
        print(f"    Auch Zweitdienst gestört für '{query}': {e}")
        return None


def geocode_address(query):
    """(lat, lon) oder None. Stoerungen werden NICHT gecacht, nur saubere
    Fehlanzeigen (mit Ablaufdatum)."""
    if not query:
        return None
    if query in _geocode_cache:
        t = _geocode_cache[query]
        return t if isinstance(t, tuple) else None

    time.sleep(GEOCODE_RATE_LIMIT_SECONDS)
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "de"},
            headers={"User-Agent": GEOCODE_USER_AGENT}, timeout=15,
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            _geocode_cache[query] = {"miss_at": now_iso()}
            return None
        coords = (float(results[0]["lat"]), float(results[0]["lon"]))
        _geocode_cache[query] = coords
        return coords
    except Exception as e:
        coords = geocode_photon(query)
        if coords:
            _geocode_cache[query] = coords
            return coords
        print(f"    Geocoding gestört für '{query}' ({e}) – nächster Lauf versucht es erneut.")
        return None


# Ein Titel ist nur dann eine brauchbare Geocoding-Anfrage, wenn er nach
# Adresse aussieht. Frueher wurde jeder Titel ("Traumhafte 3-Zimmer-Wohnung
# mit Balkon") geocodiert - das ergibt zufaellige Koordinaten, und davon
# hingen Charlottenburg, Viktoria-Luise und das Spreeufer komplett ab.
_ADRESSE = re.compile(
    r"\b(?:[A-ZÄÖÜ][a-zäöüß\-]+\s+)?[A-Za-zÄÖÜäöüß\-]*(?:stra(?:ß|ss)e|str\.|allee|platz|"
    r"weg|damm|ufer|ring|chaussee|promenade|zeile|steig|gasse|markt)\s*\d{1,3}\s?[a-z]?\b",
    re.IGNORECASE)


def geocode_query(listing):
    street = (listing.get("street") or "").strip()
    if not street:
        m = _ADRESSE.search(listing.get("title") or "")
        street = m.group(0) if m else ""
    if not street:
        return None
    plz = str(listing.get("plz") or "").strip()
    return ", ".join(p for p in (street, plz, "Berlin", "Germany") if p)


def _koordinate_im_gebiet(coords, areas):
    lat, lon = coords
    if areas.get("mitte", True) and haversine_km(lat, lon, *MITTE_CENTER) <= MITTE_RADIUS_KM:
        return True
    if areas.get("prenzlauer_berg", True) and \
            haversine_km(lat, lon, *PRENZLAUER_BERG_CENTER) <= PRENZLAUER_BERG_RADIUS_KM:
        return True
    if areas.get("charlottenburg", True) and \
            haversine_km(lat, lon, *SAVIGNYPLATZ_COORDS) <= CHARLOTTENBURG_RADIUS_KM:
        return True
    if areas.get("tiergarten_moabit_spree", True) and \
            distance_to_spree_km(lat, lon) <= SPREE_PROXIMITY_KM:
        return True
    if areas.get("viktoria_luise", True) and \
            haversine_km(lat, lon, *VIKTORIA_LUISE_COORDS) <= VIKTORIA_LUISE_RADIUS_KM:
        return True
    ca = CRITERIA.get("custom_area") or {}
    if ca.get("enabled") and len(ca.get("points") or []) >= 3:
        if point_in_polygon(lat, lon, ca["points"]):
            return True
    return False


# ---------------------------------------------------------------------------
# Texterkennung
# ---------------------------------------------------------------------------

_NOT_MITTE = re.compile(
    r"\b(zehlendorf|steglitz|spandau|pankow|neukölln|neukoelln|köpenick|koepenick|"
    r"lichtenberg|reinickendorf|tempelhof|schöneberg|schoeneberg|marzahn|hellersdorf|"
    r"wilmersdorf|friedrichshain|kreuzberg|treptow|weißensee|weissensee|buch|"
    r"hohenschönhausen|hohenschoenhausen|stadt)[\s\-]?mitte\b"
)


def mentions_area(area_key, haystack):
    for hint in CRITERIA.get("kiez_hints", {}).get(area_key, []):
        if hint in haystack:
            return True
    if area_key == "mitte":
        if _NOT_MITTE.search(haystack):
            return False
        return bool(re.search(r"\bmitte\b", haystack))
    if area_key == "prenzlauer_berg":
        return bool(re.search(r"\bprenzlauer\b", haystack))
    if area_key == "charlottenburg":
        return bool(re.search(r"\bcharlottenburg\b", haystack))
    if area_key == "tiergarten_moabit_spree":
        return bool(re.search(r"\b(tiergarten|moabit)\b", haystack))
    return False  # viktoria_luise: nur Kiez-/Platznamen, nie "Schöneberg" allgemein


REFERENCE_KEYWORDS = [
    "referenzobjekt", "referenzobjekte", "referenz ", "referenzen",
    "erfolgreich vermietet", "bereits vermietet", "bereits vergeben",
    "vermietet zum", "unsere projekte", "portfolio", "beispielobjekt",
    "erfolgsgeschichte", "abgeschlossenes projekt", "nicht mehr verfügbar",
    "ausverkauft",
]

_COMMERCIAL = re.compile(
    r"\b(gewerbe|gewerbeeinheit|gewerbefl(ä|ae)che|b(ü|ue)ro|b(ü|ue)rofl(ä|ae)che|"
    r"praxis|praxisfl(ä|ae)che|ladenfl(ä|ae)che|ladengesch(ä|ae)ft|laden|halle|"
    r"lagerfl(ä|ae)che|atelierfl(ä|ae)che|stellplatz|tiefgarage|garage|"
    r"duplex[- ]?stellplatz|tiefgaragenstellplatz|garagenstellplatz|"
    r"pkw[- ]?stellplatz)\b"
)
_FURNISHED = re.compile(r"\b(m(ö|oe)bliert|vollm(ö|oe)bliert|teilm(ö|oe)bliert|furnished|"
                        r"all[- ]inclusive|serviced apartment)\b")
# "befristet" nur mit Wortgrenze davor - sonst matcht "UNbefristet".
_TEMPORARY = re.compile(r"(wohnen auf zeit|auf zeit\b|zwischenmiete|zwischenvermietung|"
                        r"\bbefristet|\bzeitmietvertrag|max\.? \d+ monate|"
                        r"f(ü|ue)r \d+ monate\b|temporary rental|short[- ]?term)")
_SHARED = re.compile(r"\b(wg[- ]zimmer|wg zimmer|zimmer in (einer |der )?wg|"
                     r"shared (apartment|flat)|wohngemeinschaft|room in)\b")
_WBS = re.compile(r"\b(wbs|wohnberechtigungsschein)\b")
_WBS_VERNEINT = re.compile(r"(kein|keine|ohne|nicht)[\s\-]*(\w+[\s\-]+)?$")


def _wbs_im_text(text):
    """'WBS erforderlich' ja - 'kein WBS nötig', 'ohne WBS', 'WBS-frei' nein."""
    for m in _WBS.finditer(text):
        davor = text[max(0, m.start() - 20):m.start()]
        danach = text[m.end():m.end() + 8]
        if _WBS_VERNEINT.search(davor) or re.match(r"[\s\-]*(frei|nicht)", danach):
            continue
        return True
    return False
_RESIDENTIAL = re.compile(
    r"(wohnung|wohnungen|zimmer\b|\bzi\.|\bwhg\b|maisonette|penthouse|"
    r"apartment|appartement|wohntraum|townhouse|haus\b|reihenhaus|"
    r"\bwohnen\b|dachgeschoss|altbau|neubau|loft\b)"
)
# (Frueher fehlten hier die zusammengesetzten Formen - "Wohnung mit
# Tiefgaragenstellplatz" wurde deshalb als Gewerbe aussortiert.)
_NEBENSACHE = re.compile(r"\b(stellplatz|tiefgarage|garage|duplex[- ]?stellplatz|"
                         r"tiefgaragenstellplatz|garagenstellplatz|pkw[- ]?stellplatz)\b")


def _hay(listing):
    return f"{listing.get('title') or ''} {listing.get('district') or ''}".lower()


def looks_like_reference(listing):
    if "vermietet" in (listing.get("status") or "").lower():
        return True
    return any(kw in _hay(listing) for kw in REFERENCE_KEYWORDS)


def looks_commercial(listing):
    hay = _hay(listing)
    if not _COMMERCIAL.search(hay):
        return False
    if not _COMMERCIAL.search(_NEBENSACHE.sub(" ", hay)) and _RESIDENTIAL.search(hay):
        return False
    return True


# Die Merkmale kommen jetzt primaer von der KI (Feld im Schema), die den
# ganzen Inseratstext sieht. Die Regex auf Titel/Bezirk bleibt als Netz.
def looks_furnished(listing):
    return listing.get("moebliert") is True or bool(_FURNISHED.search(_hay(listing)))


def looks_temporary(listing):
    return listing.get("befristet") is True or bool(_TEMPORARY.search(_hay(listing)))


def looks_shared(listing):
    return listing.get("wg") is True or bool(_SHARED.search(_hay(listing)))


def needs_wbs(listing):
    # "wbs_required" = altes Feldformat, "wbs" = neues KI-Schema
    return listing.get("wbs") is True or listing.get("wbs_required") is True \
        or _wbs_im_text(_hay(listing))


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def effektive_kaltmiete(listing):
    """(Kaltmiete, geschaetzt?) - bei reiner Warmmiete wird geschaetzt.
    Frueher liefen solche Inserate ganz ohne Mietpruefung durch."""
    rent = _num(listing.get("rent"))
    if rent:
        return rent, False
    warm = _num(listing.get("rent_warm"))
    if not warm:
        return None, False
    size = _num(listing.get("size_qm"))
    nk = size * NEBENKOSTEN_PRO_QM if size else warm * NEBENKOSTEN_ANTEIL_OHNE_QM
    return round(max(warm - nk, 0)), True


def has_enough_data(listing):
    return _num(listing.get("rooms")) is not None or _num(listing.get("size_qm")) is not None


# ---------------------------------------------------------------------------
# Lage
# ---------------------------------------------------------------------------

def pruefe_lage(listing):
    """'ok', 'lage' (sicher ausserhalb) oder 'lage_unklar' (moeglicherweise
    im Gebiet, aber ohne Adresse nicht pruefbar). 'lage_unklar' wandert in
    die Knapp-verfehlt-Liste, statt still zu verschwinden."""
    areas = CRITERIA.get("areas", {})
    plz = str(listing.get("plz") or "").strip()
    haystack = f"{(listing.get('district') or '').lower()} {(listing.get('title') or '').lower()}"

    allowed_plz = set()
    if areas.get("mitte", True):
        allowed_plz |= PLZ_MITTE
    if areas.get("prenzlauer_berg", True):
        allowed_plz |= PLZ_PRENZLAUER_BERG
    if plz and plz in allowed_plz:
        return "ok"

    # Gebiete, die nur ueber Umkreis/Abstand pruefbar sind
    radius_kandidat = (
        (areas.get("charlottenburg", True)
         and (plz in CHARLOTTENBURG_CANDIDATE_PLZ or mentions_area("charlottenburg", haystack)))
        or (areas.get("tiergarten_moabit_spree", True)
            and (plz in TIERGARTEN_MOABIT_CANDIDATE_PLZ
                 or mentions_area("tiergarten_moabit_spree", haystack)))
        or (areas.get("viktoria_luise", True)
            and (plz in VIKTORIA_LUISE_CANDIDATE_PLZ or mentions_area("viktoria_luise", haystack)))
    )
    custom_aktiv = (CRITERIA.get("custom_area") or {}).get("enabled") and \
        len((CRITERIA.get("custom_area") or {}).get("points") or []) >= 3

    # Ohne PLZ: Ortsteil-/Kieznamen fuer Mitte und Prenzlauer Berg genuegen
    if not plz and not radius_kandidat:
        if any(mentions_area(a, haystack) for a in ("mitte", "prenzlauer_berg")
               if areas.get(a, True)):
            return "ok"

    query = geocode_query(listing)
    if radius_kandidat or (not plz and query) or custom_aktiv:
        coords = geocode_address(query) if query else None
        if coords:
            return "ok" if _koordinate_im_gebiet(coords, areas) else "lage"
        if radius_kandidat:
            return "lage_unklar"

    if not plz and not STRICT_LOCATION_FILTER:
        return "ok"
    return "lage"


# ---------------------------------------------------------------------------
# Gesamtpruefung
# ---------------------------------------------------------------------------

def matches_criteria(listing):
    """(passt, gruende). Alle verletzten Kriterien werden gesammelt."""
    if looks_like_reference(listing):
        return False, ["referenzobjekt"]
    if looks_commercial(listing):
        return False, ["gewerbe"]
    if not has_enough_data(listing):
        return False, ["keine_daten"]
    if looks_shared(listing) and not CRITERIA.get("allow_shared", False):
        return False, ["wg_zimmer"]
    if looks_furnished(listing) and not CRITERIA.get("allow_furnished", False):
        return False, ["moebliert"]
    if looks_temporary(listing) and not CRITERIA.get("allow_temporary", False):
        return False, ["zeitmiete"]
    if needs_wbs(listing) and not CRITERIA.get("allow_wbs", False):
        return False, ["wbs"]

    gruende = []
    rooms = _num(listing.get("rooms"))
    size = _num(listing.get("size_qm"))
    rent, _ = effektive_kaltmiete(listing)

    if rooms is not None and rooms < CRITERIA["min_rooms"]:
        gruende.append("zimmer")
    if size is not None and size < CRITERIA["min_size_qm"]:
        gruende.append("groesse")
    if rent is not None and not (CRITERIA["min_rent"] <= rent <= CRITERIA["max_rent"]):
        gruende.append("miete")

    # Lage nur pruefen (Geocoding kostet ~1 s), wenn sonst alles passt oder
    # hoechstens ein Kriterium knapp verfehlt ist - sonst ist sie egal.
    if not gruende or (len(gruende) == 1 and _ist_knapp(listing, gruende[0])):
        lage = pruefe_lage(listing)
        if lage != "ok":
            gruende.append(lage)
    else:
        gruende.append("lage_ungeprueft")

    return (len(gruende) == 0), gruende


def _ist_knapp(listing, grund):
    if grund == "groesse":
        size = _num(listing.get("size_qm"))
        return size is not None and size >= CRITERIA["min_size_qm"] * (1 - NEAR_MISS_SIZE_TOLERANCE)
    if grund == "miete":
        rent, _ = effektive_kaltmiete(listing)
        return rent is not None and \
            CRITERIA["max_rent"] < rent <= CRITERIA["max_rent"] * (1 + NEAR_MISS_RENT_TOLERANCE)
    if grund == "zimmer":
        rooms = _num(listing.get("rooms"))
        return rooms is not None and rooms >= CRITERIA["min_rooms"] - NEAR_MISS_ROOMS_TOLERANCE
    if grund == "lage_unklar":
        return True
    return False


def is_near_miss(listing, gruende):
    """Genau EIN Kriterium knapp verfehlt - oder Lage nicht pruefbar."""
    return len(gruende) == 1 and _ist_knapp(listing, gruende[0])
