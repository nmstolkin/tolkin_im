# Wohnungssuche Berlin

Überwacht automatisch 123 Hausverwaltungs-/Makler-Webseiten in Berlin auf neue
Mietwohnungsinserate und schickt bei Treffern eine Push-Benachrichtigung ans
iPhone.

**Aktuelle Suchkriterien** (in `scrape.py`, ganz oben im Abschnitt `CRITERIA`):
- Ab 3 Zimmer
- Ab 85 qm
- Kaltmiete zwischen 1.500 € und 2.300 €
- Bezirk: Mitte, Prenzlauer Berg, Charlottenburg

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
1. Einen Anthropic-API-Key erstellen: [console.anthropic.com](https://console.anthropic.com)
   → "API Keys" → neuen Key erstellen. (Für die Größenordnung von ~120
   Seiten, 3x täglich geprüft, liegen die Kosten typischerweise im
   niedrigen einstelligen Euro-Bereich pro Monat, da dank Change-Detection
   nur bei tatsächlichen Seitenänderungen ein API-Aufruf erfolgt.)
2. Im GitHub-Repository: **Settings → Secrets and variables → Actions →
   New repository secret**
3. Zwei Secrets anlegen:
   - `ANTHROPIC_API_KEY` → dein API-Key aus Schritt 1
   - `NTFY_TOPIC` → dein Topic-Name aus Schritt 2 (z. B. `immo-berlin-x7f2k9`)

### Schritt 4: Die App auf deinem iPhone einrichten
1. Im Repository: **Settings → Pages** → unter "Build and deployment" als
   Quelle **"Deploy from a branch"** wählen, Branch `main`, Ordner `/docs` →
   Speichern.
2. GitHub zeigt dir nach kurzer Zeit eine URL wie
   `https://DEIN-NAME.github.io/immo-watcher/`. Diese im Safari auf dem
   iPhone öffnen.
3. Unten in Safari auf das **Teilen-Symbol** tippen → **"Zum Home-Bildschirm"**.
4. Fertig – ab jetzt hast du ein eigenes "Wohnungssuche Berlin"-App-Icon auf deinem
   Homescreen. Es öffnet sich im Vollbild wie eine normale App und zeigt dir
   alle bisher gefundenen, passenden Inserate.

> Hinweis: GitHub Pages ist bei kostenlosen Accounts nur für **öffentliche**
> Repositories automatisch nutzbar. Da in `docs/data.json` nur Wohnungsdaten
> (keine persönlichen Daten von dir) stehen, ist ein öffentliches Repository
> hierfür unproblematisch. Falls dir das trotzdem unangenehm ist, sag
> Bescheid – dann richten wir es alternativ über ein privates Repo mit
> GitHub Pages (kostenpflichtiger Plan) oder einen anderen kostenlosen
> Static-Hosting-Dienst ein.

### Schritt 5: Workflow aktivieren
1. Im Repository auf den Tab **"Actions"** klicken.
2. Falls gefragt: Workflows aktivieren.
3. Der Workflow "Wohnungssuche Berlin" läuft ab jetzt automatisch 3x täglich (siehe
   Zeiten in `.github/workflows/check.yml`). Du kannst ihn zusätzlich
   jederzeit manuell anstoßen: Actions → "Wohnungssuche Berlin" → "Run workflow".

Das war's – ab jetzt bekommst du automatisch eine Push-Nachricht, sobald ein
neues, passendes Inserat gefunden wird.

---

## 2. Wie es funktioniert

1. Für jede der 123 URLs wird die Seite abgerufen.
2. Hat sich der Seiteninhalt seit dem letzten Lauf **nicht** verändert, wird
   die Seite übersprungen (spart API-Kosten und Zeit).
3. Bei Änderungen wird der Text an die Claude API geschickt, die daraus die
   einzelnen Inserate (Adresse, Zimmer, Fläche, Miete, Bezirk) strukturiert
   herausliest – das funktioniert unabhängig davon, wie unterschiedlich die
   Webseiten aufgebaut sind.
4. Jedes Inserat wird mit bereits bekannten Inseraten abgeglichen (Datei
   `data/seen_listings.json`), damit nichts doppelt gemeldet wird.
5. Neue Inserate werden gegen deine Kriterien geprüft. Bei Treffer: Push
   über ntfy **und** ein neuer Eintrag in der Homescreen-App (`docs/data.json`),
   die du jederzeit öffnen kannst, um alle bisherigen Treffer zu sehen.

Der Status (bekannte Inserate, Seiten-Hashes) wird nach jedem Lauf automatisch
zurück ins Repository committet, damit der nächste Lauf darauf aufbauen kann.

---

## 3. Bekannte Einschränkungen

- **JavaScript-lastige Seiten**: Einige Websites bauen ihre Angebotsliste
  erst per JavaScript im Browser auf (z. B. Seiten mit `#/list1` in der
  URL). Diese liefern dem Skript unter Umständen eine leere Hülle statt der
  echten Inserate. Falls du bei bestimmten Seiten merkst, dass nie etwas
  gemeldet wird, sag mir Bescheid – für diese Fälle kann man auf einen
  "echten Browser" (Playwright) umsteigen, das ist aufwändiger, aber lösbar.
- **ImmoScout24-Links ausgeschlossen**: Die beiden Links der Form
  `portal.immobilienscout24.de/ergebnisliste/...?sid=...` aus deiner Liste
  wurden nicht aufgenommen, da es sich um an deinen persönlichen Login
  gebundene, ablaufende Session-Links handelt, die ein Skript ohne dein
  Passwort nicht abrufen kann. Für ImmoScout24 empfiehlt sich stattdessen
  der **native E-Mail-Alarm von ImmoScout24 selbst** (eigene gespeicherte
  Suche mit Benachrichtigung) – der öffentliche Anbieter-Profil-Link
  (`leible-und-cie`) ist dagegen enthalten.
- **Kauf-Angebote ausgeschlossen**: Zwei Links, die erkennbar nur
  Kaufangebote zeigten, wurden nicht aufgenommen.
- Die Bezirks-Erkennung ist Best-Effort: Wenn auf einer Seite kein Bezirk
  erkennbar ist, wird das Inserat trotzdem gemeldet (lieber ein
  Fehlalarm als ein verpasstes Angebot) – du kannst das bei Bedarf strenger
  einstellen.

## 4. Kriterien oder URL-Liste anpassen

- **Kriterien ändern**: `scrape.py` öffnen, den Block `CRITERIA` oben
  anpassen (Zimmer, Fläche, Miete, Bezirke), Datei committen – fertig.
- **URLs hinzufügen/entfernen**: `sites.json` bearbeiten (einfaches
  JSON-Array mit URLs).

## 5. Lokal testen (optional, für technisch Interessierte)

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="dein-key"
export NTFY_TOPIC="dein-topic"
python scrape.py
```
