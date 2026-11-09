"""
Seitenabruf: einfache HTTP-Abrufe (parallel), Browser-Rendering mit
Playwright (inkl. Mitschnitt der JSON-Datenabfragen) und Link-Navigation
(Folgeseiten, vermutete Angebots-Unterseite).
"""

import json
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from util import budget_exceeded

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 ImmoWatcher/1.1"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
}
# (Verbindungsaufbau, Lesen). Tote Server scheitern so nach 8 s statt 25 s.
REQUEST_TIMEOUT = (8, 25)
PARALLEL_ABRUFE = 8

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
_lokal = threading.local()


def _session():
    """Eine Session je Thread (requests.Session ist nicht threadsicher).
    Wiederverwendete Verbindungen sparen TLS-Handshakes bei Folgeseiten."""
    s = getattr(_lokal, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        retry = Retry(total=1, connect=1, read=0, backoff_factor=1.5,
                      status_forcelist=(502, 503, 504), allowed_methods=("GET",))
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=4)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _lokal.session = s
    return s


MAX_ABRUF_SEKUNDEN = 45       # Gesamtdauer je Abruf (Timeout gilt sonst nur je Datenpaket)
MAX_ABRUF_BYTES = 8_000_000


def _verbindung_kappen(r):
    """Schliesst die Leitung von aussen - ein blockiertes Lesen kehrt dann
    sofort zurueck. Anders laesst sich ein tropfender Server nicht stoppen."""
    kandidaten = []
    try:
        conn = getattr(r.raw, "_connection", None) or getattr(r.raw, "connection", None)
        kandidaten.append(getattr(conn, "sock", None))
    except Exception:
        pass
    try:
        # Ohne Content-Length gibt http.client die Leitung an die Antwort ab;
        # dann haengt der Socket nur noch an deren Lese-Objekt.
        kandidaten.append(r.raw._fp.fp.raw._sock)
    except Exception:
        pass
    for sock in kandidaten:
        if sock is None:
            continue
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
    try:
        r.close()
    except Exception:
        pass


def fetch(url):
    """HTTP-Abruf mit harter Gesamtdauer. Der requests-Timeout gilt nur je
    gelesenem Datenpaket - ein Server, der alle 20 s ein Byte schickt, hielte
    den Abruf sonst beliebig lange fest."""
    start = time.time()
    try:
        with _session().get(url, timeout=REQUEST_TIMEOUT, stream=True) as r:
            r.raise_for_status()
            rest = max(1.0, MAX_ABRUF_SEKUNDEN - (time.time() - start))
            wecker = threading.Timer(rest, _verbindung_kappen, args=(r,))
            wecker.daemon = True
            wecker.start()
            try:
                teile, groesse = [], 0
                for teil in r.iter_content(65536):
                    teile.append(teil)
                    groesse += len(teil)
                    if groesse > MAX_ABRUF_BYTES:
                        break
            except Exception:
                if time.time() - start >= MAX_ABRUF_SEKUNDEN - 0.5:
                    return None, f"Abruf nach {MAX_ABRUF_SEKUNDEN}s abgebrochen (Server zu langsam)"
                raise
            finally:
                wecker.cancel()
            if time.time() - start >= MAX_ABRUF_SEKUNDEN - 0.5:
                return None, f"Abruf nach {MAX_ABRUF_SEKUNDEN}s abgebrochen (Server zu langsam)"
            r._content = b"".join(teile)
            r._content_consumed = True
            # requests raet bei fehlender Zeichensatzangabe ISO-8859-1 - deutsche
            # Seiten liefern dann "WohnflÃ¤che". Im Zweifel den Inhalt fragen.
            if (r.encoding or "").lower() in ("", "iso-8859-1") and "charset" not in \
                    (r.headers.get("content-type") or "").lower():
                r.encoding = r.apparent_encoding
            return r.text, None
    except Exception as e:
        return None, str(e)


def abruf_parallel(auftraege, workers=PARALLEL_ABRUFE):
    """auftraege: Liste von (site_url, abruf_url). Liefert (site_url,
    abruf_url, html, fehler) in der Reihenfolge, in der die Abrufe FERTIG
    werden - die Auswertung beginnt also schon, waehrend der Rest noch laedt.
    Nur die Abrufe laufen parallel; Auswertung, KI und Browser bleiben im
    Hauptthread (Playwright-Sync-API und Rate-Limits verlangen das)."""
    ex = ThreadPoolExecutor(max_workers=workers)
    futures = {ex.submit(fetch, abruf): (site, abruf) for site, abruf in auftraege}
    try:
        for fut in as_completed(futures):
            site, abruf = futures[fut]
            html, err = fut.result()
            yield site, abruf, html, err
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# Playwright
# ---------------------------------------------------------------------------
_pw = None  # (playwright, browser, context)
browser_deaktiviert = False  # nach einem haengenden Browser fuer den Rest des Laufs aus
im_browser = False           # True, solange fetch_rendered arbeitet
_BLOCKIERT = {"image", "media", "font"}
_MAX_JSON_BYTES = 3_000_000
# JS-Pruefung: stehen schon genug Inseratsmerkmale auf der Seite?
_JS_GENUG_INHALT = """() => {
  const t = document.body ? document.body.innerText : '';
  return (t.match(/€|m²|\\bqm\\b|Zimmer|Kaltmiete/gi) || []).length >= 4;
}"""


def _browser_context():
    global _pw
    if _pw is not None:
        return _pw[2]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  Playwright nicht installiert, JS-Fallback nicht verfügbar.")
        return None
    try:
        p = sync_playwright().start()
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=HEADERS["User-Agent"], locale="de-DE")
        # Jeder Browser-Befehl bekommt eine Obergrenze - sonst kann z. B.
        # page.content() auf einer kaputten Seite unbegrenzt warten.
        ctx.set_default_timeout(15000)
        ctx.set_default_navigation_timeout(25000)
        # Bilder, Videos und Schriften brauchen wir nicht - sie machen den
        # Grossteil der Ladezeit aus.
        ctx.route("**/*", lambda route: route.abort()
                  if route.request.resource_type in _BLOCKIERT else route.continue_())
        _pw = (p, browser, ctx)
        return ctx
    except Exception as e:
        print(f"  Playwright-Browser konnte nicht gestartet werden: {e}")
        return None


def browser_abschalten():
    """Nach einem haengenden Browser: Referenz verwerfen und fuer den Rest
    des Laufs keinen neuen starten (der Playwright-Zustand ist nach einer
    Unterbrechung nicht mehr verlaesslich)."""
    global _pw, browser_deaktiviert, im_browser
    if not im_browser:
        return False
    browser_deaktiviert = True
    im_browser = False
    _pw = None
    return True


def close_playwright():
    global _pw
    if _pw is not None:
        p, browser, ctx = _pw
        for f in (ctx.close, browser.close, p.stop):
            try:
                f()
            except Exception:
                pass
        _pw = None


def fetch_rendered(url):
    """Rendert die Seite im Browser. Gibt (html, json_antworten) zurueck.

    json_antworten: alle JSON-Daten, die die Seite per XHR/fetch nachlaedt.
    Bei Makler-Widgets stehen die Inserate dort bereits vollstaendig und
    strukturiert drin - auslesen kostet keinen KI-Aufruf."""
    global im_browser
    if budget_exceeded() or browser_deaktiviert:
        return None, []
    im_browser = True
    try:
        return _rendern(url)
    finally:
        im_browser = False


def _rendern(url):
    ctx = _browser_context()
    if not ctx:
        return None, []
    page = None
    fertig = []
    try:
        page = ctx.new_page()
        # Nur ABGESCHLOSSENE Datenabfragen merken. Frueher wurde jede Antwort
        # gemerkt und spaeter ihr Inhalt gelesen - bei einer offenen
        # Dauerverbindung (Live-Updates, Chat-Widget) wartet das ewig.
        page.on("requestfinished", lambda req: fertig.append(req)
                if req.resource_type in ("xhr", "fetch") else None)
        page.goto(url, timeout=25000, wait_until="domcontentloaded")
        # Statt fester Wartezeit: warten, bis Preise/Flaechen auftauchen.
        try:
            page.wait_for_function(_JS_GENUG_INHALT, timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(800)  # Rest nachladen lassen
        html = page.content()
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                inner = frame.content()
            except Exception:
                continue
            if inner and len(inner) > 500:
                html += "\n<!-- eingebetteter Rahmen -->\n" + inner

        daten = []
        for req in fertig[:40]:
            try:
                r = req.response()
                if not r or "json" not in (r.headers.get("content-type") or ""):
                    continue
                laenge = int(r.headers.get("content-length") or 0)
                if laenge > _MAX_JSON_BYTES:
                    continue
                body = r.body()
                if len(body) > _MAX_JSON_BYTES:
                    continue
                daten.append(json.loads(body))
            except Exception:
                continue
        return html, daten
    except Exception as e:
        print(f"    Playwright-Rendering fehlgeschlagen für {url}: {e}")
        return None, []
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Link-Navigation
# ---------------------------------------------------------------------------
NEXT_PAGE_TEXTS = {"weiter", "nächste", "nächste seite", "next", "vor", "›", "»", ">"}
LISTING_LINK_KEYWORDS = [
    ("mieten", 3), ("miete", 3), ("vermietung", 3), ("mietangebote", 3),
    ("wohnungsangebote", 3), ("wohnungen", 2), ("angebote", 2),
    ("objekte", 2), ("immobilien", 1),
]


def find_next_page_url(html, current_url):
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(current_url).netloc
    rel_next = soup.find("a", rel=lambda v: v and "next" in v)
    if rel_next and rel_next.get("href"):
        c = urljoin(current_url, rel_next["href"])
        if urlparse(c).netloc == base_host and c != current_url:
            return c
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        classes = " ".join(a.get("class", [])).lower()
        aria = (a.get("aria-label") or "").lower()
        if text in NEXT_PAGE_TEXTS or "next" in classes or "next" in aria:
            c = urljoin(current_url, a["href"])
            if urlparse(c).netloc == base_host and c != current_url:
                return c
    return None


def find_listing_subpage(html, current_url):
    """Sucht bei 0 erkannten Inseraten eine vermutete Angebots-Uebersicht."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(current_url).netloc
    best_url, best_score = None, 0
    gesehen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("#") or href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        c = urljoin(current_url, href)
        parsed = urlparse(c)
        if parsed.netloc != base_host or c == current_url or c in gesehen:
            continue
        gesehen.add(c)
        text = a.get_text(strip=True).lower()
        haystack = f"{text} {c.lower()}"
        score = sum(w for kw, w in LISTING_LINK_KEYWORDS if kw in haystack)
        segments = [s for s in parsed.path.rstrip("/").split("/") if s]
        if any(re.fullmatch(r"[\d_\-]{5,}", s) for s in segments):
            score -= 5
        if any(len(s) > 40 for s in segments):
            score -= 4
        if re.search(r"\d+[\.,]?\d*\s*(zimmer|zi\.|m²|qm)", text):
            score -= 4
        if re.search(r"\b(b(ue|ü)ro|gewerbe|praxis|laden|halle|stellplatz|garage|kaufen|kauf)\b",
                     haystack):
            score -= 5
        score -= max(0, len(segments) - 1) * 0.6
        if score > best_score:
            best_score, best_url = score, c
    return best_url if best_score > 0 else None
