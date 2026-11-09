#!/usr/bin/env python3
"""
Immo Watcher
------------
Ruft Hausverwaltungs-/Makler-Webseiten ab, erkennt Wohnungsinserate,
filtert sie nach den Kriterien und meldet Treffer SOFORT per Push (ntfy).

Aufbau:
  scrape.py      Ablauf, Status, App-Daten, Benachrichtigungen (diese Datei)
  abruf.py       HTTP (parallel), Browser-Rendering, Link-Navigation
  extraktion.py  Strukturierte Daten, Feeds, Vorfilter, KI-Extraktion
  kriterien.py   Suchkriterien, Lage, Geocoding
  util.py        Dateien, Zeitbudget, Rate-Limiter

Je Quelle wird gemerkt, WIE sie zuletzt erfolgreich ausgelesen wurde
(direkt, Unterseite, Feed, Browser) und WAS dabei herauskam. Ist der
Seitentext unveraendert, wird das Ergebnis wiederverwendet - ohne KI-Aufruf.
"""

import os
import re
import sys
import time
from urllib.parse import urlparse, urljoin
from statistics import median
from datetime import datetime

import requests

import abruf
import extraktion
import kriterien
import util
from util import load_json, save_json, now_iso, hours_since, budget_exceeded

# ---------------------------------------------------------------------------
# Dateien und Grenzwerte
# ---------------------------------------------------------------------------
DATA_DIR = "data"
SEEN_FILE = os.path.join(DATA_DIR, "seen_listings.json")
SITE_CACHE_FILE = os.path.join(DATA_DIR, "site_cache.json")   # Strategie + letztes Ergebnis je Quelle
GEMELDET_FILE = os.path.join(DATA_DIR, "gemeldet.json")       # alle je gepushten Inserats-Schluessel
ERROR_LOG = os.path.join(DATA_DIR, "last_errors.json")
HEARTBEAT_FILE = os.path.join(DATA_DIR, "heartbeat.json")
STATS_FILE = "run-stats.json"
APP_DATA_FILE = "data.json"
STATUS_FILE = "site-status.json"
CONFIG_FILE = "config.json"
SITES_FILE = os.environ.get("SITES_FILE", "sites.json")

HEARTBEAT_DAYS = 7
DEFAULT_COOLDOWN_ENABLED = False
MAX_MATCHES_KEPT = 300
MAX_SEEN_PER_SITE = 400
MAX_GEMELDET = 5000
MAX_NEAR_MISS_GONE_DAYS = 14
MAX_NEAR_MISSES = 120
PLAYWRIGHT_GIVE_UP_AFTER = 4
PLAYWRIGHT_RETRY_EVERY = 10
HOPELESS_THRESHOLD = 5
HOPELESS_RECHECK_HOURS = 24 * 21
MAX_EXTRA_PAGES = 2
SLEEP_BETWEEN_PAGES = 1.0
# Wie lange ein Ergebnis bei unveraendertem Seitentext wiederverwendet wird.
# Leere Ergebnisse kuerzer, weil JS-Seiten ihre Huelle nicht aendern, auch
# wenn neue Inserate nachgeladen werden.
KI_CACHE_STUNDEN = 24
KI_CACHE_LEER_STUNDEN = 8

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
APP_URL = os.environ.get("APP_URL")
DEBUG_EMPTY = os.environ.get("DEBUG_EMPTY", "").strip() == "1"
DEBUG_TEXT_CHARS = 1200

# Laufzustand (wird in _run befuellt)
S = {}


# ---------------------------------------------------------------------------
# Schluessel, Datensaetze
# ---------------------------------------------------------------------------

def _normalize_key_part(v):
    if v is None:
        return ""
    s = re.sub(r"\s+", " ", str(v).strip().lower())
    if re.fullmatch(r"-?\d+(?:[.,]\d+)?", s):
        try:
            f = float(s.replace(",", "."))
            return str(int(f)) if f == int(f) else f"{f:.1f}"
        except ValueError:
            pass
    return s


def listing_key(listing, site_url):
    """Stabiler Schluessel je Inserat (unveraendert gegenueber der alten
    Version, damit bereits Gemeldetes nicht erneut gemeldet wird)."""
    url = listing.get("url")
    if url:
        return f"{site_url}::{_normalize_key_part(url)}"
    teile = [
        _normalize_key_part(listing.get("rooms")),
        _normalize_key_part(listing.get("size_qm")),
        _normalize_key_part(listing.get("rent")),
        _normalize_key_part(listing.get("plz") or listing.get("district")),
    ]
    if any(teile):
        return f"{site_url}::{'|'.join(teile)}"
    return f"{site_url}::{_normalize_key_part(listing.get('title'))}"


def source_name(site_url):
    return re.sub(r"^(www\.|portal\.)", "", urlparse(site_url).netloc)


def build_match_record(key, listing, site_url):
    size_v = listing.get("size_qm")
    rent_v = listing.get("rent")
    kalt, geschaetzt = kriterien.effektive_kaltmiete(listing)
    ppsqm_basis = rent_v or (kalt if geschaetzt else None)
    rec = {
        "key": key,
        "title": listing.get("title"),
        "rooms": listing.get("rooms"),
        "size_qm": size_v,
        "rent": rent_v,
        "price_per_sqm": round(ppsqm_basis / size_v, 1) if ppsqm_basis and size_v else None,
        "district": listing.get("district"),
        "plz": listing.get("plz"),
        "street": listing.get("street"),
        "url": urljoin(site_url, listing["url"]) if listing.get("url") else site_url,
        "site_url": site_url,
        "source_name": source_name(site_url),
        "found_at": now_iso(),
    }
    if listing.get("rent_warm"):
        rec["rent_warm"] = listing["rent_warm"]
    if geschaetzt:
        rec["rent_kalt_geschaetzt"] = kalt
    if listing.get("frei_ab"):
        rec["frei_ab"] = listing["frei_ab"]
    return rec


def duplicate_signature(m):
    rent = m.get("rent") or m.get("rent_kalt_geschaetzt")
    size = m.get("size_qm")
    plz = str(m.get("plz") or "").strip()
    if not isinstance(rent, (int, float)) or not isinstance(size, (int, float)) or not plz:
        return None
    return f"{round(rent / 25) * 25}|{round(size / 5) * 5}|{plz}"


def _alle_keys(m):
    return [m.get("key")] + list(m.get("alias_keys") or [])


# ---------------------------------------------------------------------------
# App-Daten (data.json)
# ---------------------------------------------------------------------------

def compute_time_online_stats(matches):
    per_source = {}
    for m in matches:
        if m.get("still_listed") is not False:
            continue
        f, g = m.get("found_at"), m.get("gone_since")
        try:
            hours = (datetime.fromisoformat(g) - datetime.fromisoformat(f)).total_seconds() / 3600
        except Exception:
            continue
        if hours > 0:
            per_source.setdefault(m.get("source_name") or "?", []).append(hours)
    return {s: {"median_hours": round(median(v), 1), "samples": len(v)}
            for s, v in per_source.items() if len(v) >= 2}


def _markiere_gelistet(eintraege, seen_keys):
    jetzt = now_iso()
    for m in eintraege:
        if any(k in seen_keys for k in _alle_keys(m)):
            m["still_listed"] = True
            m.pop("gone_since", None)
        elif m.get("still_listed") is not False:
            m["still_listed"] = False
            m.setdefault("gone_since", jetzt)


def update_app_data(new_matches, seen_keys_this_run=None, near_misses=None):
    """Fuegt Treffer hinzu, markiert Verschwundenes (nur Volllauf) und
    fuehrt Dubletten ueber Quellen zusammen. Wird nach JEDER Quelle mit
    Treffern aufgerufen, damit ein Abbruch nichts doppelt meldet."""
    existing = load_json(APP_DATA_FILE, {"matches": []})
    matches = existing.get("matches", [])
    keys = {k for m in matches for k in _alle_keys(m)}
    for m in new_matches:
        if m["key"] not in keys:
            matches.append(m)
            keys.add(m["key"])

    if seen_keys_this_run:
        _markiere_gelistet(matches, seen_keys_this_run)

    # Dubletten: Schluessel des Duplikats als alias_keys am Haupteintrag
    # merken. Frueher fiel das Duplikat ganz heraus - sein Schluessel galt
    # beim naechsten Lauf als ungemeldet, und dieselbe Wohnung wurde im
    # Wechsel immer wieder als "neu" gepusht.
    by_sig, deduped = {}, []
    for m in sorted(matches, key=lambda x: x.get("found_at", ""), reverse=True):
        sig = duplicate_signature(m)
        if sig and sig in by_sig:
            p = by_sig[sig]
            name = m.get("source_name")
            if name and name != p.get("source_name") and name not in p.setdefault("also_on", []):
                p["also_on"].append(name)
            for k in _alle_keys(m):
                if k and k != p["key"] and k not in p.setdefault("alias_keys", []):
                    p["alias_keys"].append(k)
            if m.get("still_listed") is True:
                p["still_listed"] = True
                p.pop("gone_since", None)
            continue
        if sig:
            by_sig[sig] = m
        deduped.append(m)

    near = existing.get("near_misses", [])
    if near_misses is not None or seen_keys_this_run:
        near = merge_near_misses(near, near_misses or [], seen_keys_this_run)

    save_json(APP_DATA_FILE, {
        "generated_at": now_iso(),
        "criteria": {
            "min_rooms": kriterien.CRITERIA["min_rooms"],
            "min_size_qm": kriterien.CRITERIA["min_size_qm"],
            "min_rent": kriterien.CRITERIA["min_rent"],
            "max_rent": kriterien.CRITERIA["max_rent"],
            "areas": [a for a, an in kriterien.CRITERIA["areas"].items() if an],
        },
        "matches": deduped[:MAX_MATCHES_KEPT],
        "near_misses": near,
        "time_online": compute_time_online_stats(deduped),
    })


def merge_near_misses(existing, new_items, seen_keys_this_run=None):
    keys = {m.get("key") for m in existing}
    for m in new_items:
        if m.get("key") not in keys:
            existing.append(m)
            keys.add(m.get("key"))
    if seen_keys_this_run:
        _markiere_gelistet(existing, seen_keys_this_run)
        existing = [m for m in existing if m.get("still_listed") is not False
                    or hours_since(m.get("gone_since")) <= MAX_NEAR_MISS_GONE_DAYS * 24]
    existing.sort(key=lambda m: m.get("found_at", ""), reverse=True)
    return existing[:MAX_NEAR_MISSES]


# ---------------------------------------------------------------------------
# Status je Quelle
# ---------------------------------------------------------------------------

def update_site_status(status, url, ok, error=None, listing_count=None, suggested_url=None,
                       pages_checked=None, skipped=False, extra=None):
    now = now_iso()
    entry = status.get(url, {})
    entry["source_name"] = source_name(url)
    entry["last_checked"] = now
    if skipped:
        status[url] = entry
        return
    if ok:
        entry.update(status="ok", error=None, last_success=now)
        entry["consecutive_bad_runs"] = 0
        entry["hopeless"] = False
    else:
        entry.update(status="error", error=error)
        entry.setdefault("last_success", None)
        entry["consecutive_bad_runs"] = entry.get("consecutive_bad_runs", 0) + 1
        entry["hopeless"] = entry["consecutive_bad_runs"] >= HOPELESS_THRESHOLD
    if listing_count is not None:
        entry["last_listing_count"] = listing_count
        entry["last_listing_count_at"] = now
    entry.setdefault("last_listing_count", None)
    if suggested_url is not None:
        entry["suggested_url"] = suggested_url
    if pages_checked is not None:
        entry["pages_checked"] = pages_checked
    if extra:
        entry.update(extra)
    status[url] = entry


# ---------------------------------------------------------------------------
# Benachrichtigungen
# ---------------------------------------------------------------------------

def push(title, body, priority="default", tags="house", click=None, app_button=False):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC nicht gesetzt, überspringe Benachrichtigung.")
        return False
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags}
    if click or APP_URL:
        headers["Click"] = click or APP_URL
    if app_button and APP_URL:
        headers["Actions"] = f"view, In App ansehen, {APP_URL}"
    try:
        requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=body.encode("utf-8"),
                      headers=headers, timeout=15)
        return True
    except Exception as e:
        print(f"Benachrichtigung fehlgeschlagen: {e}")
        return False


def _kurzinfo(m):
    teile = []
    if m.get("rooms"):
        teile.append(f"{m['rooms']} Zi.")
    if m.get("size_qm"):
        teile.append(f"{m['size_qm']} m²")
    if m.get("rent"):
        teile.append(f"{m['rent']:,.0f} €".replace(",", "."))
    elif m.get("rent_warm"):
        teile.append(f"{m['rent_warm']:,.0f} € warm".replace(",", "."))
    return ", ".join(teile)


def melde_sofort(treffer):
    """Push direkt nach der Quelle, nicht erst am Laufende. Inserate stehen
    oft nur wenige Stunden online - jede Minute zaehlt."""
    if not treffer:
        return False
    if len(treffer) == 1:
        m = treffer[0]
        title = f"Neue Wohnung: {_kurzinfo(m)}" if _kurzinfo(m) else "Neue Wohnung"
        body = f"{m.get('title') or 'Wohnungsinserat'}\n{m.get('district') or m.get('plz') or ''}" \
               f" · {m.get('source_name')}"
        if m.get("frei_ab"):
            body += f"\nfrei ab {m['frei_ab']}"
        ok = push(title, body.strip(), priority="high", click=m.get("url"), app_button=True)
    else:
        title = f"{len(treffer)} neue Wohnungen bei {treffer[0].get('source_name')}"
        body = "\n".join(f"{m.get('title') or 'Inserat'} – {_kurzinfo(m)}" for m in treffer[:6])
        if len(treffer) > 6:
            body += f"\n… und {len(treffer) - 6} weitere"
        ok = push(title, body, priority="high")
    if ok:
        save_json(HEARTBEAT_FILE, {"last_notification_at": now_iso()})
    return ok


def maybe_send_heartbeat(stats):
    hb = load_json(HEARTBEAT_FILE, {})
    if hours_since(hb.get("last_notification_at")) < HEARTBEAT_DAYS * 24:
        return
    body = (f"Der Bot läuft.\n{stats['sites_total']} Quellen geprüft, "
            f"{stats['listings_seen']} Inserate gesehen.\n"
            f"Seit {HEARTBEAT_DAYS} Tagen kein passender Treffer.")
    if push("Wohnungssuche: Lebenszeichen", body, priority="low", tags="heartbeat"):
        save_json(HEARTBEAT_FILE, {"last_notification_at": now_iso()})


# ---------------------------------------------------------------------------
# Auswertung einer Quelle
# ---------------------------------------------------------------------------

def _neu(site, page_url, html, strategie, ziel_url=None, xhr=None, gerendert=False):
    return {"site": site, "page_url": page_url, "html": html, "strategie": strategie,
            "ziel_url": ziel_url, "xhr": xhr or [], "gerendert": gerendert,
            "listings": None, "quelle": None, "hash": None, "text": None, "seiten": 1}


def auswerten(r, mit_cache=True, batch_erlaubt=False):
    """Eine Seite auswerten: strukturiert -> Feed -> Cache -> Vorfilter -> KI."""
    gefunden, art = extraktion.strukturierte_inserate(r["html"], r["page_url"], r["xhr"])
    if gefunden:
        r.update(listings=gefunden, quelle=art)
        return r

    if r["strategie"] == "direkt" and not r["gerendert"]:
        for feed_url in extraktion.finde_inserats_feeds(r["html"], r["page_url"])[:2]:
            xml, _ = abruf.fetch(feed_url)
            items = extraktion.inserate_aus_feed(xml, feed_url) if xml else []
            if items:
                r.update(listings=items, quelle="feed", strategie="feed", ziel_url=feed_url)
                return r

    text = extraktion.extract_text(r["html"])
    r["text"], r["hash"] = text, extraktion.text_hash(text)

    c = S["cache"].get(r["site"]) or {}
    if mit_cache and S["ki_cache"] and c.get("hash") == r["hash"] \
            and c.get("page_url") == r["page_url"] and isinstance(c.get("inserate"), list):
        grenze = S["cache_h"] if c["inserate"] else S["cache_leer_h"]
        if hours_since(c.get("at")) < grenze:
            r.update(listings=c["inserate"], quelle="cache", seiten=c.get("seiten", 1),
                     aus_cache=True, strategie=c.get("strategie", r["strategie"]),
                     ziel_url=c.get("ziel_url"))
            return r

    if extraktion.sieht_leer_aus(text):
        r.update(listings=[], quelle="vorfilter")
        return r

    if batch_erlaubt and extraktion.AI_PROVIDER == "anthropic":
        r["ki_offen"] = True
        return r
    r.update(listings=extraktion.extract_listings(r["page_url"], text), quelle="ki")
    return r


def stufe1(site, abruf_url, html):
    """Primaerseite nach gemerkter Strategie auswerten."""
    c = S["cache"].get(site) or {}
    strategie = c.get("strategie", "direkt")
    ziel = c.get("ziel_url")
    if abruf_url != site and strategie in ("feed", "unterseite"):
        if strategie == "feed":
            items = extraktion.inserate_aus_feed(html, abruf_url)
            r = _neu(site, abruf_url, html, "feed", ziel)
            if items:
                r.update(listings=items, quelle="feed")
                return r
            html, _ = abruf.fetch(site)  # Feed leer -> normale Seite
            if not html:
                r.update(listings=[], quelle="fehler")
                return r
            return auswerten(_neu(site, site, html, "direkt"), batch_erlaubt=True)
        return auswerten(_neu(site, abruf_url, html, "unterseite", ziel), batch_erlaubt=True)

    if strategie == "browser":
        ziel_url = ziel or site
        gerendert, xhr = abruf.fetch_rendered(ziel_url)
        if gerendert:
            r = _neu(site, ziel_url, gerendert, "browser", ziel, xhr=xhr, gerendert=True)
            r["seiten"] = 2
            return auswerten(r, batch_erlaubt=False)
    return auswerten(_neu(site, site, html, "direkt"), batch_erlaubt=True)


def kaskade(r):
    """Primaerseite lieferte nichts: Direktseite -> Unterseite -> Browser.
    Nur hier entstehen Zusatzkosten; was klappt, wird als Strategie gemerkt."""
    site = r["site"]
    seiten = r["seiten"]
    leer_ref = r  # wessen Hash bei leerem Ergebnis gespeichert wird
    prev = S["status"].get(site) or {}

    if r["page_url"] != site or r["strategie"] not in ("direkt",):
        html, _ = abruf.fetch(site)
        seiten += 1
        if html:
            t = auswerten(_neu(site, site, html, "direkt"), mit_cache=False)
            leer_ref = t
            if t["listings"]:
                t["seiten"] = seiten
                return t, None
        direkt_html = html
    else:
        direkt_html = r["html"]

    if direkt_html:
        kandidat = abruf.find_listing_subpage(direkt_html, site)
        if kandidat and kandidat != r["page_url"]:
            time.sleep(SLEEP_BETWEEN_PAGES)
            html, _ = abruf.fetch(kandidat)
            seiten += 1
            if html:
                t = auswerten(_neu(site, kandidat, html, "unterseite", kandidat), mit_cache=False)
                if t["listings"]:
                    print(f"  -> Unterseite mit Inseraten: {kandidat}")
                    t["seiten"] = seiten
                    return t, None

    # Browser: teuerster Schritt. Quellen, bei denen er wiederholt nichts
    # brachte, nur noch jeden PLAYWRIGHT_RETRY_EVERY-ten Lauf.
    # (Frueher wurde hier versehentlich der Zaehler einer ANDEREN Quelle gelesen.)
    pw_fehl = prev.get("playwright_vergeblich", 0)
    pw_versuch = not r["gerendert"] and (
        pw_fehl < PLAYWRIGHT_GIVE_UP_AFTER or pw_fehl % PLAYWRIGHT_RETRY_EVERY == 0)
    pw_ergebnis = None
    if pw_versuch and not budget_exceeded():
        gerendert, xhr = abruf.fetch_rendered(site)
        if gerendert:
            seiten += 1
            t = auswerten(_neu(site, site, gerendert, "browser", xhr=xhr, gerendert=True),
                          mit_cache=False)
            if t["listings"]:
                print(f"  -> per Browser gefunden ({t['quelle']}): {len(t['listings'])} Inserat(e)")
                t["seiten"] = seiten
                return t, 0
        pw_ergebnis = pw_fehl + 1
    elif not r["gerendert"]:
        S["zaehler"]["browser_uebersprungen"] += 1

    leer_ref = dict(leer_ref, listings=[], strategie="direkt", ziel_url=None, seiten=seiten)
    return leer_ref, pw_ergebnis


def folgeseiten(r):
    """'Weiter'-Links verfolgen (nur bei normal abgerufenen HTML-Seiten)."""
    if not r["listings"] or r.get("aus_cache") or r["quelle"] == "feed" or r["gerendert"]:
        return r
    listings = list(r["listings"])
    gesehen = {r["site"], r["page_url"]}
    html, url = r["html"], r["page_url"]
    for _ in range(MAX_EXTRA_PAGES):
        next_url = abruf.find_next_page_url(html, url)
        if not next_url or next_url in gesehen or budget_exceeded():
            break
        gesehen.add(next_url)
        time.sleep(SLEEP_BETWEEN_PAGES)
        page_html, _ = abruf.fetch(next_url)
        if not page_html:
            break
        r["seiten"] += 1
        t = auswerten(_neu(r["site"], next_url, page_html, r["strategie"]), mit_cache=False)
        if not t["listings"]:
            break
        print(f"  -> Folgeseite: {len(t['listings'])} weitere Inserat(e)")
        listings += t["listings"]
        html, url = page_html, next_url
    r["listings"] = listings
    return r


def abschliessen(r):
    """Kaskade, Folgeseiten, Cache, Kriterien, Sofort-Push, Status."""
    site = r["site"]
    pw_neu = None
    if not r["listings"] and not r.get("aus_cache") and r["quelle"] != "fehler":
        r, pw_neu = kaskade(r)
    r = folgeseiten(r)

    listings = [l for l in (extraktion.normalisiere_inserat(x) for x in (r["listings"] or [])) if l]
    S["zaehler"][r["quelle"] or "?"] += 1
    print(f"  -> {len(listings)} Inserat(e) [{r['quelle']}, {r['strategie']}, {r['seiten']} Seite(n)]")

    if DEBUG_EMPTY and not listings and r.get("text"):
        print(f"  [DEBUG] Text ({len(r['text'])} Zeichen) von {r['page_url']}:")
        print("  [DEBUG] " + (r["text"][:DEBUG_TEXT_CHARS] or "(LEER)"))

    alt = S["cache"].get(site) or {}
    S["cache"][site] = {
        "strategie": r["strategie"],
        "ziel_url": r.get("ziel_url"),
        "page_url": r["page_url"],
        "hash": r.get("hash"),
        "inserate": listings,
        "seiten": r["seiten"],
        "quelle": r["quelle"],
        # bei Wiederverwendung das urspruengliche Datum behalten, sonst
        # wuerde die Hoechstdauer nie ablaufen
        "at": alt.get("at") if r.get("aus_cache") else now_iso(),
    }

    extra = {"strategie": r["strategie"], "quelle": r["quelle"]}
    pw_alt = (S["status"].get(site) or {}).get("playwright_vergeblich", 0)
    if r["strategie"] == "browser" and listings:
        extra["playwright_vergeblich"] = 0
    elif pw_neu is not None:
        extra["playwright_vergeblich"] = pw_neu
    elif pw_alt:
        extra["playwright_vergeblich"] = pw_alt
    update_site_status(S["status"], site, ok=True, listing_count=len(listings),
                       suggested_url=r.get("ziel_url") if r["strategie"] == "unterseite" else None,
                       pages_checked=r["seiten"], extra=extra)

    pruefe_inserate(site, listings)


def pruefe_inserate(site, listings):
    bekannt_liste = list(S["seen"].get(site, []))
    known = set(bekannt_liste)
    neu_hier = []
    for listing in listings:
        key = listing_key(listing, site)
        S["seen_keys"].add(key)
        S["listings_seen"] += 1
        if key not in known:
            known.add(key)
            bekannt_liste.append(key)

        # Kriterien auf JEDES Inserat (nicht nur neue) - sonst greift eine
        # Lockerung der Kriterien nie fuer laengst bekannte Wohnungen.
        passt, gruende = kriterien.matches_criteria(listing)
        if not passt:
            for g in gruende:
                if g != "lage_ungeprueft":
                    S["rejected"][g] = S["rejected"].get(g, 0) + 1
            if kriterien.is_near_miss(listing, gruende):
                rec = build_match_record(key, listing, site)
                rec["miss_reason"] = gruende[0]
                S["near"].append(rec)
            continue
        if key not in S["gemeldet"]:
            print(f"     TREFFER: {listing.get('title')}")
            neu_hier.append(build_match_record(key, listing, site))

    S["seen"][site] = bekannt_liste[-MAX_SEEN_PER_SITE:]

    if neu_hier:
        # Erst speichern, dann pushen waere sicherer gegen Doppelmeldungen,
        # aber ein verpasster Push ist schlimmer als ein doppelter.
        melde_sofort(neu_hier)
        for m in neu_hier:
            S["gemeldet"][m["key"]] = m["found_at"]
        S["treffer"].extend(neu_hier)
        update_app_data(neu_hier)
        _speichere_gemeldet()


def _speichere_gemeldet():
    eintraege = sorted(S["gemeldet"].items(), key=lambda kv: kv[1] or "")[-MAX_GEMELDET:]
    save_json(GEMELDET_FILE, dict(eintraege), kompakt=True)


def _lade_gemeldet():
    g = load_json(GEMELDET_FILE, None)
    if isinstance(g, dict):
        return g
    # Erster Lauf mit der neuen Version: aus data.json uebernehmen
    app = load_json(APP_DATA_FILE, {})
    g = {}
    for m in app.get("matches", []):
        for k in _alle_keys(m):
            if k:
                g[k] = m.get("found_at")
    return g


# ---------------------------------------------------------------------------
# Hauptlauf
# ---------------------------------------------------------------------------

def _lade_config():
    config = load_json(CONFIG_FILE, {})
    cfg = config.get("criteria", {})
    geaendert = kriterien.wende_config_an(cfg)
    if geaendert:
        print("Feineinstellungen: " + ", ".join(geaendert))
    c = kriterien.CRITERIA
    print(f"Aktive Kriterien: {c['min_rooms']}+ Zimmer, {c['min_size_qm']}+ qm, "
          f"{c['min_rent']}-{c['max_rent']} €, Gebiete: "
          f"{[a for a, an in c['areas'].items() if an]}")
    S["cooldown"] = bool(config.get("cooldown_enabled", DEFAULT_COOLDOWN_ENABLED))
    S["ki_cache"] = bool(config.get("ki_cache", True))
    S["cache_h"] = float(config.get("ki_cache_stunden", KI_CACHE_STUNDEN))
    S["cache_leer_h"] = float(config.get("ki_cache_leer_stunden", KI_CACHE_LEER_STUNDEN))
    print(f"Aussichtslos-Pause: {'AN' if S['cooldown'] else 'AUS'} · "
          f"Ergebnis-Cache: {'AN' if S['ki_cache'] else 'AUS'} "
          f"({S['cache_h']:.0f} h / leer {S['cache_leer_h']:.0f} h)")


def main():
    try:
        _run()
    finally:
        abruf.close_playwright()


def _run():
    from collections import Counter
    p = extraktion.AI_PROVIDER
    if p == "gemini" and not extraktion.GEMINI_API_KEY:
        sys.exit("FEHLER: AI_PROVIDER=gemini, aber GEMINI_API_KEY fehlt.")
    if p == "anthropic" and not extraktion.ANTHROPIC_API_KEY:
        sys.exit("FEHLER: ANTHROPIC_API_KEY fehlt.")
    if p == "none":
        print("TROCKENLAUF: keine KI-Aufrufe (AI_PROVIDER=none).")

    sites = load_json(SITES_FILE, [])
    status = load_json(STATUS_FILE, {})
    if SITES_FILE == "sites.json":
        status = {u: status[u] for u in sites if u in status}
    S.update(
        seen=load_json(SEEN_FILE, {}), status=status, cache=load_json(SITE_CACHE_FILE, {}),
        gemeldet=_lade_gemeldet(), seen_keys=set(), listings_seen=0, rejected={},
        near=[], treffer=[], zaehler=Counter(),
    )
    errors = {}
    kriterien.load_geocode_cache()
    _lade_config()
    print(f"Bereits gemeldete Inserate: {len(S['gemeldet'])}")

    # Aussichtslose Quellen gar nicht erst abrufen (nur mit Cooldown)
    auftraege, uebersprungen = [], 0
    for site in sites:
        st = status.get(site, {})
        if S["cooldown"] and st.get("hopeless") and \
                hours_since(st.get("last_checked")) < HOPELESS_RECHECK_HOURS:
            update_site_status(status, site, ok=False, skipped=True)
            S["seen_keys"].update(listing_key(l, site) for l in
                                  (S["cache"].get(site) or {}).get("inserate", []))
            uebersprungen += 1
            continue
        c = S["cache"].get(site) or {}
        abruf_url = c.get("ziel_url") if c.get("strategie") in ("feed", "unterseite") \
            and c.get("ziel_url") else site
        auftraege.append((site, abruf_url))
    if uebersprungen:
        print(f"{uebersprungen} aussichtslose Quelle(n) pausiert.")

    batch = []
    verarbeitet = 0
    print(f"Rufe {len(auftraege)} Quelle(n) ab ({abruf.PARALLEL_ABRUFE} parallel) …")
    for i, (site, abruf_url, html, err) in enumerate(abruf.abruf_parallel(auftraege), 1):
        if budget_exceeded():
            print(f"ZEITGRENZE nach {util.RUN_BUDGET_MINUTES:.0f} Min erreicht - "
                  f"{len(auftraege) - i + 1} Quelle(n) bleiben diesmal offen.")
            break
        verarbeitet += 1
        print(f"[{i}/{len(auftraege)}] {site}")
        if err and abruf_url != site:
            # gemerkte Unterseite/Feed tot -> normale Seite versuchen
            html, err = abruf.fetch(site)
            abruf_url = site
            S["cache"].pop(site, None)
        if err:
            print(f"  -> Fehler beim Abrufen: {err}")
            errors[site] = err
            update_site_status(status, site, ok=False, error=err)
            # Voruebergehende Stoerung: zuletzt bekannte Inserate gelten als
            # weiterhin gelistet - sonst erscheinen sie in der App als "weg".
            S["seen_keys"].update(listing_key(l, site) for l in
                                  (S["cache"].get(site) or {}).get("inserate", []))
            continue
        try:
            r = stufe1(site, abruf_url, html)
            if r.get("ki_offen"):
                batch.append(r)
                continue
            abschliessen(r)
        except Exception as e:  # eine kaputte Quelle darf den Lauf nicht beenden
            print(f"  -> Unerwarteter Fehler, Quelle übersprungen: {e!r}")
            errors[site] = f"intern: {e!r}"

    if batch:
        print(f"\nAnthropic-Batch: {len(batch)} Seite(n) …")
        ergebnisse = extraktion.run_batch_extraction(
            [(str(n), r["page_url"], r["text"]) for n, r in enumerate(batch)])
        for n, r in enumerate(batch):
            if budget_exceeded():
                break
            r.update(listings=ergebnisse.get(str(n)) or [], quelle="ki")
            print(f"Batch-Ergebnis: {r['site']}")
            try:
                abschliessen(r)
            except Exception as e:
                print(f"  -> Unerwarteter Fehler: {e!r}")

    save_json(SEEN_FILE, S["seen"])
    save_json(SITE_CACHE_FILE, S["cache"], kompakt=True)
    save_json(ERROR_LOG, errors)
    save_json(STATUS_FILE, status)
    _speichere_gemeldet()
    kriterien.save_geocode_cache()

    vollstaendig = verarbeitet == len(auftraege) and not budget_exceeded()
    is_full_run = SITES_FILE == "sites.json" and vollstaendig
    if not vollstaendig:
        print("Hinweis: Lauf unvollständig - 'nicht mehr gelistet' wird nicht bewertet.")
    update_app_data(S["treffer"], S["seen_keys"] if is_full_run else None, S["near"])

    z = S["zaehler"]
    stats = {
        "generated_at": now_iso(),
        "listings_seen": S["listings_seen"],
        "matches": len(S["treffer"]),
        "rejected": S["rejected"],
        "rejected_total": sum(S["rejected"].values()),
        "sites_total": len(sites),
        "sites_with_errors": len(errors),
        "ohne_ki": sum(z[k] for k in ("json-ld", "microdata", "eingebettetes json",
                                      "xhr-json", "feed")),
        "aus_cache": z["cache"],
        "vorfilter": z["vorfilter"],
        "ki_aufrufe": extraktion.ki_aufrufe,
        "ki_abgeschnitten": extraktion.ki_abgeschnitten,
        "quellen_nach_art": dict(z),
        "near_misses": len(S["near"]),
        "quota_exhausted": extraktion.quota_exhausted,
        "laufzeit_min": round(util.minuten_seit_start(), 1),
        "run_type": "voll" if is_full_run else "schnell",
    }
    stats_all = load_json(STATS_FILE, {})
    if "listings_seen" in stats_all:
        stats_all = {"voll": stats_all}
    stats_all[stats["run_type"]] = stats
    save_json(STATS_FILE, stats_all)

    if extraktion.quota_exhausted:
        push("Wohnungssuche: Lauf unvollständig",
             "Das Gemini-Tageskontingent war erschöpft. Nicht alle Quellen wurden "
             "ausgewertet.", priority="high", tags="warning")
    else:
        maybe_send_heartbeat(stats)

    print()
    print(f"Fertig in {stats['laufzeit_min']} Min. {len(S['treffer'])} neue(r) Treffer gemeldet.")
    print(f"KI-Aufrufe: {extraktion.ki_aufrufe} · aus Cache: {z['cache']} · "
          f"Vorfilter: {z['vorfilter']} · ohne KI: {stats['ohne_ki']}"
          + (f" · abgeschnitten: {extraktion.ki_abgeschnitten}" if extraktion.ki_abgeschnitten else ""))
    print(f"{S['listings_seen']} Inserat(e) erkannt. Verworfen nach Grund:")
    for g, n in sorted(S["rejected"].items(), key=lambda x: -x[1]):
        print(f"   {g}: {n}")
    if errors:
        print(f"{len(errors)} Quelle(n) mit Fehlern (siehe {ERROR_LOG}).")


if __name__ == "__main__":
    main()
