# Wohnungssuche Berlin

Überwacht automatisch 123 Hausverwaltungs-/Makler-Webseiten in Berlin auf neue
Mietwohnungsinserate und schickt bei Treffern eine Push-Benachrichtigung ans
iPhone.

**Suchkriterien** stehen in `config.json` und lassen sich auch in der App
ändern (Zimmer, Fläche, Kaltmiete, Gebiete, eigenes Gebiet auf der Karte).
Vorgaben und Kiez-Listen stehen in `kriterien.py`.

Das Skript läuft nicht auf deinem iPhone (das lässt iOS technisch nicht zu),
sondern automatisch in der Cloud (kostenlos über GitHub Actions) und schickt
dir eine Push-Nachricht, sobald etwas Passendes gefunden wurde.

---

## 1. Einmaliges Setup (ca. 15 Minuten)

### Schritt 1: GitHub-Repository anlegen
1. Auf [github.com](https://github.com) kostenlos registrieren, falls noch
   nicht vorhanden.
2. Neues **privates** Repository anlegen, z. B. `immo-watcher`.
3. Alle Dateien aus diesem Ordner in das Repository hochladen (per
   Drag&Drop im Browser über "Add file → Upload files", oder per `git push`,
   falls du mit Git vertraut bist).

### Schritt 2: ntfy-App installieren (Push-Benachrichtigungen)
1. Im App Store **"ntfy"** installieren (kostenlos,
   https://apps.apple.com/app/ntfy/id1625396347).
2. App öffnen → auf das "+" tippen → ein **eigenes, schwer erratbares
   Thema** (Topic) vergeben, z. B. `immo-berlin-x7f2k9`. Jeder, der den
   Topic-Namen kennt, kann Nachrichten mitlesen, deshalb keinen einfachen
   Namen wie "wohnung" wählen.
3. Diesen Topic-Namen brauchst du gleich in Schritt 3.

### Schritt 3: API-Key & Secrets hinterlegen
1. Einen kostenlosen Gemini-API-Key erstellen
   ([aistudio.google.com](https://aistudio.google.com) → "Get API key").
   Alternativ Anthropic ([console.anthropic.com](https://console.anthropic.com));
   dann in den Workflows `AI_PROVIDER: "anthropic"` setzen.
2. Im GitHub-Repository: **Settings → Secrets and variables → Actions →
   New repository secret**
3. Zwei Secrets anlegen:
   - `GEMINI_API_KEY` (oder `ANTHROPIC_API_KEY`) → dein API-Key aus Schritt 1
   - `NTFY_TOPIC` → dein Topic-Name aus Schritt 2 (z. B. `immo-berlin-x7f2k9`)

### Schritt 4: Die App auf deinem iPhone einrichten
1. Im Repository: **Settings → Pages** → unter "Build and deployment" als
   Quelle **"Deploy from a branch"** wählen, Branch `main`, Ordner `/ (root)` →
   Speichern.
2. GitHub zeigt dir nach kurzer Zeit eine URL wie
   `https://DEIN-NAME.github.io/immo-watcher/`. Diese im Safari auf dem
   iPhone öffnen.
3. Unten in Safari auf das **Teilen-Symbol** tippen → **"Zum Home-Bildschirm"**.
4. Fertig – ab jetzt hast du ein eigenes "Wohnungssuche Berlin"-App-Icon auf deinem
   Homescreen. Es öffnet sich im Vollbild wie eine normale App und zeigt dir
   alle bisher gefundenen, passenden Inserate.

> Hinweis: GitHub Pages ist bei kostenlosen Accounts nur für **öffentliche**
> Repositories automatisch nutzbar. Da in `data.json` nur Wohnungsdaten
> (keine persönlichen Daten von dir) stehen, ist ein öffentliches Repository
> hierfür unproblematisch. Falls dir das trotzdem unangenehm ist, sag
> Bescheid – dann richten wir es alternativ über ein privates Repo mit
> GitHub Pages (kostenpflichtiger Plan) oder einen anderen kostenlosen
> Static-Hosting-Dienst ein.

### Schritt 5: Workflow aktivieren
1. Im Repository auf den Tab **"Actions"** klicken.
2. Falls gefragt: Workflows aktivieren.
3. Ab jetzt laufen automatisch: der volle Lauf (alle Quellen, 2× täglich,
   `.github/workflows/check.yml`) und der Schnell-Lauf (ertragreichste
   Quellen, alle 2 Stunden, `.github/workflows/check-fast.yaml`). Du kannst ihn zusätzlich
   jederzeit manuell anstoßen: Actions → "Wohnungssuche Berlin" → "Run workflow".

Das war's – ab jetzt bekommst du automatisch eine Push-Nachricht, sobald ein
neues, passendes Inserat gefunden wird.

---

## 2. Wie es funktioniert

1. Alle Quellen werden **parallel** abgerufen (8 gleichzeitig). Die
   Auswertung beginnt, sobald die erste Seite da ist.
2. Je Quelle wird gemerkt, **wie** sie zuletzt erfolgreich ausgelesen wurde
   (`data/site_cache.json`): direkt, über eine Unterseite, über einen Feed
   oder per Browser. Der nächste Lauf geht direkt dorthin.
3. Auswertung, billig vor teuer:
   1. **Strukturierte Daten** in der Seite (schema.org JSON-LD/Microdata,
      eingebettetes JSON) – kostenlos und exakt.
   2. **Inserats-Feeds** (RSS/Atom), nur wenn die Einträge Zimmer/Fläche
      enthalten. Blog- und Kommentar-Feeds werden ignoriert.
   3. **Ergebnis-Cache**: Ist der Seitentext unverändert, wird das letzte
      Ergebnis wiederverwendet (max. 24 h, leere Ergebnisse max. 8 h).
   4. **Vorfilter**: Seiten ohne Preis-/Flächenangaben brauchen keine KI.
   5. **KI-Extraktion** (Gemini mit festem JSON-Schema). Abgeschnittene
      Antworten werden teilweise gerettet statt verworfen.
4. Liefert die Seite nichts: vermutete Angebots-Unterseite, dann echter
   Browser (Playwright). Im Browser werden die JSON-Daten mitgelesen, die
   Makler-Widgets nachladen – oft ohne KI auswertbar.
5. Jedes Inserat wird gegen die Kriterien geprüft. Ein Treffer wird
   **sofort** gepusht (bei einem einzelnen Treffer öffnet der Tap direkt
   das Inserat), nicht erst am Ende des Laufs.
6. Knapp verfehlte Inserate (ein Kriterium knapp daneben, oder Lage ohne
   Adresse nicht prüfbar) landen im Reiter "Knapp verfehlt".

Bereits gemeldete Inserate stehen in `data/gemeldet.json` – auch
Dubletten über mehrere Portale werden so nicht erneut gemeldet.

## 3. Einstellungen in `config.json`

| Schlüssel | Bedeutung |
|---|---|
| `criteria.*` | Zimmer, Fläche, Miete, Gebiete, Radien, Toleranzen (auch per App) |
| `criteria.nebenkosten_pro_qm` | Für Inserate mit nur Warmmiete: geschätzte Nebenkosten (Vorgabe 3,00 €/m²) |
| `ki_cache` | Ergebnis-Cache an/aus (Vorgabe: an) |
| `ki_cache_stunden` / `ki_cache_leer_stunden` | Höchstalter des Caches (24 / 8 h) |
| `cooldown_enabled` | Dauerhaft fehlerhafte Quellen nur alle 3 Wochen prüfen |

## 4. Bekannte Einschränkungen

- **ImmoScout24-Suchlinks** sind an einen Login gebunden und nicht abrufbar.
  Dafür besser den E-Mail-Alarm von ImmoScout24 selbst nutzen.
- **Lage in Charlottenburg, Viktoria-Luise und am Spreeufer** lässt sich nur
  mit Straßenangabe prüfen. Ohne Adresse landet ein sonst passendes Inserat
  unter "Knapp verfehlt" (Grund: Lage prüfen).
- Die Warmmiete-Schätzung ist grob. Solche Inserate zeigt die App mit
  "€ warm" und "kalt ca.".

## 5. Lokal testen

```bash
pip install -r requirements-dev.txt
python -m pytest -q tests                 # Filter und Parser, ohne Netz

export GEMINI_API_KEY="dein-key" AI_PROVIDER=gemini NTFY_TOPIC="dein-topic"
python scrape.py

AI_PROVIDER=none python scrape.py         # Trockenlauf ohne KI
DEBUG_EMPTY=1 python scrape.py            # Text leerer Quellen ins Log
```

## 6. Dateien

| Datei | Inhalt |
|---|---|
| `scrape.py` | Ablauf, Status, App-Daten, Benachrichtigungen |
| `abruf.py` | HTTP (parallel), Browser, Link-Navigation |
| `extraktion.py` | Strukturierte Daten, Feeds, Vorfilter, KI |
| `kriterien.py` | Kriterien, Lage, Geocoding |
| `util.py` | Dateien, Zeitbudget, Rate-Limiter |
| `tests/` | Tests (laufen automatisch bei Code-Änderungen) |
