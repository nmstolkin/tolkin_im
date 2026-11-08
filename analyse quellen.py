"""
Einmalige Messung: Welche der hinterlegten Quellen liefern ihre Inserate
bereits maschinenlesbar aus?

Hintergrund: Derzeit kostet jede Seite einen KI-Aufruf, um Zimmer, Flaeche und
Miete aus Fliesstext zu lesen. Viele Seiten liefern dieselben Angaben aber
schon strukturiert mit - als schema.org-Auszeichnung (fuer Google), als
RSS-Feed oder in den bekannten Mustern verbreiteter Makler-Plugins. Das
auszulesen ist kostenlos, exakt und kennt kein Kontingent.

Dieses Skript ruft nur ab und zaehlt. Es macht KEINEN einzigen KI-Aufruf und
veraendert keine Daten des Bots.

Aufruf:  python analyse_quellen.py
Ergebnis: quellen-analyse.md (lesbar) und quellen-analyse.json (maschinenlesbar)
"""

import json
import re
import sys
import time
from collections import Counter
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SITES_FILE = "sites.json"
REPORT_MD = "quellen-analyse.md"
REPORT_JSON = "quellen-analyse.json"
TIMEOUT = 25
PAUSE = 0.4

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 ImmoWatcher/1.0"
    )
}

# schema.org-Typen, die eine Immobilie bzw. ein Angebot beschreiben
IMMO_TYPEN = {
    "realestatelisting", "apartment", "house", "singlefamilyresidence",
    "residence", "accommodation", "offer", "product", "place",
    "apartmentcomplex", "suite",
}

# Erkennungsmerkmale verbreiteter Makler-Baukaesten. Wo ein Muster oft genug
# auftaucht, lohnt sich ein eigener Auswerter fuer diesen Typ.
BAUKASTEN = {
    "immomakler":  r"immomakler",
    "frymo":       r"frymo",
    "onOffice":    r"onoffice|myonoffice",
    "immosolve":   r"immosolve",
    "FlowFact":    r"flowfact",
    "justImmo":    r"justimmo",
    "OpenImmo":    r"openimmo",
    "immobilie1":  r"immobilie1",
    "easysquare":  r"easysquare",
    "everreal":    r"everreal",
    "WordPress":   r"wp-content|wp-json",
    "TYPO3":       r"typo3",
}


def finde_jsonld(soup):
    """Gibt die gefundenen schema.org-Typen zurueck, die Immobilien meinen."""
    treffer = set()
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        roh = tag.string or tag.get_text() or ""
        try:
            daten = json.loads(roh)
        except Exception:
            # Manche Seiten haengen mehrere Objekte unverbunden aneinander
            continue
        stapel = [daten]
        while stapel:
            d = stapel.pop()
            if isinstance(d, list):
                stapel.extend(d)
            elif isinstance(d, dict):
                t = d.get("@type")
                for name in ([t] if isinstance(t, str) else (t or [])):
                    if isinstance(name, str) and name.lower() in IMMO_TYPEN:
                        treffer.add(name)
                stapel.extend(v for v in d.values()
                              if isinstance(v, (dict, list)))
    return sorted(treffer)


def finde_microdata(soup):
    treffer = set()
    for tag in soup.find_all(attrs={"itemtype": True}):
        it = (tag.get("itemtype") or "").lower()
        if "schema.org" in it:
            name = it.rstrip("/").rsplit("/", 1)[-1]
            if name in IMMO_TYPEN:
                treffer.add(name)
    return sorted(treffer)


def finde_feeds(soup, basis):
    feeds = []
    for tag in soup.find_all("link", attrs={"rel": True, "type": True}):
        typ = (tag.get("type") or "").lower()
        if "rss" in typ or "atom" in typ:
            href = tag.get("href")
            if href:
                feeds.append(urljoin(basis, href))
    return sorted(set(feeds))


def finde_baukasten(html):
    low = html.lower()
    return sorted(name for name, muster in BAUKASTEN.items()
                  if re.search(muster, low))


def pruefe(url):
    e = {"url": url, "host": urlparse(url).netloc, "fehler": None,
         "jsonld": [], "microdata": [], "feeds": [], "baukasten": [],
         "zeichen": 0}
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        html = r.text
    except Exception as ex:
        e["fehler"] = str(ex)[:160]
        return e

    e["zeichen"] = len(html)
    soup = BeautifulSoup(html, "html.parser")
    e["jsonld"] = finde_jsonld(soup)
    e["microdata"] = finde_microdata(soup)
    e["feeds"] = finde_feeds(soup, url)
    e["baukasten"] = finde_baukasten(html)
    return e


def main():
    sites = json.load(open(SITES_FILE))
    print(f"Prüfe {len(sites)} Quellen – keine KI-Aufrufe, kein Kontingent\n")

    ergebnisse = []
    for i, url in enumerate(sites, 1):
        e = pruefe(url)
        ergebnisse.append(e)
        kennz = []
        if e["jsonld"]:    kennz.append("JSON-LD")
        if e["microdata"]: kennz.append("Microdata")
        if e["feeds"]:     kennz.append(f"{len(e['feeds'])} Feed(s)")
        if e["fehler"]:    kennz.append("FEHLER")
        print(f"[{i}/{len(sites)}] {e['host'][:42]:44} "
              f"{', '.join(kennz) or '—'}")
        time.sleep(PAUSE)

    mit_jsonld = [e for e in ergebnisse if e["jsonld"]]
    mit_micro = [e for e in ergebnisse if e["microdata"] and not e["jsonld"]]
    mit_feed = [e for e in ergebnisse if e["feeds"]]
    fehler = [e for e in ergebnisse if e["fehler"]]
    strukturiert = {e["url"] for e in mit_jsonld} | {e["url"] for e in mit_micro}

    bk = Counter(b for e in ergebnisse for b in e["baukasten"])

    z = []
    z.append("# Quellen-Analyse\n")
    z.append(f"- Geprüft: **{len(ergebnisse)}** Quellen")
    z.append(f"- Nicht erreichbar: **{len(fehler)}**")
    z.append(f"- Mit strukturierten Immobilien-Daten: **{len(strukturiert)}** "
             f"({len(strukturiert)*100//max(1,len(ergebnisse))} %) "
             f"→ hier ist die KI entbehrlich")
    z.append(f"- Mit RSS/Atom-Feed: **{len(mit_feed)}**\n")

    z.append("## Verbreitete Baukästen\n")
    z.append("Wo ein Muster oft auftaucht, lohnt ein eigener Auswerter für diesen Typ.\n")
    for name, n in bk.most_common():
        z.append(f"- {name}: {n}")

    def tabelle(titel, eintraege, spalte):
        z.append(f"\n## {titel} ({len(eintraege)})\n")
        if not eintraege:
            z.append("_keine_")
            return
        for e in eintraege:
            wert = e[spalte]
            wert = ", ".join(wert) if isinstance(wert, list) else wert
            z.append(f"- `{e['host']}` — {wert}")

    tabelle("Strukturierte Daten (JSON-LD)", mit_jsonld, "jsonld")
    tabelle("Strukturierte Daten (Microdata)", mit_micro, "microdata")
    tabelle("RSS/Atom-Feeds", mit_feed, "feeds")
    tabelle("Nicht erreichbar", fehler, "fehler")

    open(REPORT_MD, "w", encoding="utf-8").write("\n".join(z) + "\n")
    json.dump(ergebnisse, open(REPORT_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    print(f"\n{'='*58}")
    print(f"Strukturierte Daten: {len(strukturiert)}/{len(ergebnisse)} Quellen")
    print(f"Feeds:               {len(mit_feed)}")
    print(f"Nicht erreichbar:    {len(fehler)}")
    print(f"\nBericht: {REPORT_MD}")


if __name__ == "__main__":
    sys.exit(main())
