# fb_archiver

`fb_archiver.py` ist ein Facebook-Archivierungswerkzeug auf Basis der offiziellen Graph API. Es archiviert Inhalte einer Facebook-Seite in einer einfachen Verzeichnisstruktur und schreibt sowohl maschinenlesbare Rohdaten (`.jsonl`) als auch tabellarische Übersichten (`.csv`).

Der Fokus liegt auf Seiteninhalten, nicht auf Profilen oder Gruppen. Das Tool eignet sich für Archivierungs- und Dokumentationszwecke, wenn für die betroffene Seite ein gültiger `Page Access Token` mit den nötigen Rechten vorliegt.

## Funktionsumfang

- Beiträge einer Facebook-Seite inklusive Metadaten abrufen
- Kommentare inklusive Replies rekursiv erfassen
- Reaktionssummen und detaillierte Reaktionen speichern
- Medien aus Posts herunterladen, soweit direkt verfügbar
- Alben und Fotos archivieren
- Events archivieren
- Live-Videos archivieren
- Optional Inbox-Konversationen und Nachrichten abrufen, wenn das Token zusätzliche Rechte hat
- Prüfsummen und ein kleines Archiv-Manifest erzeugen

## Voraussetzungen

- Python `3.9+`
- Ein gültiger `Page Access Token`
- Installierte Abhängigkeiten aus `requirements.txt`

Abhängigkeiten:

- `requests`
- `python-dateutil`
- `tqdm`

## Installation

Empfohlen ist eine virtuelle Umgebung:

```powershell
cd C:\fb_archiver\fb_archiver
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Ohne virtuelle Umgebung geht es ebenfalls:

```powershell
cd C:\fb_archiver\fb_archiver
python -m pip install -r requirements.txt
```

## Access Token

Das Script erwartet einen `Page Access Token`, nicht nur einen normalen Nutzer-Token. Der Token muss mindestens die für Lesezugriffe benötigten `pages_read_*`-Rechte haben.

Standardmäßig verwendet das Tool Graph API `v26.0`. Diese Version wurde im Juli 2026 veröffentlicht und ist am 19. August 2026 die aktuelle Graph-API-Version. Die Version kann bei Bedarf per `FB_GRAPH_API_VERSION` oder `--graph-api-version` überschrieben werden.

Zusätzliche Rechte:

- Für Inbox- und Nachrichtenarchivierung: `pages_messaging`
- Je nach Setup zusätzlich: `pages_manage_metadata`

Praktisch ist es, den Token nur für die aktuelle PowerShell-Session als Umgebungsvariable zu setzen:

```powershell
$env:FB_PAGE_TOKEN='DEIN_PAGE_ACCESS_TOKEN'
```

Prüfen:

```powershell
if ($env:FB_PAGE_TOKEN) { 'FB_PAGE_TOKEN ist gesetzt' } else { 'FB_PAGE_TOKEN ist nicht gesetzt' }
```

Entfernen:

```powershell
Remove-Item Env:FB_PAGE_TOKEN
```

### Token verschluesselt speichern

Der Token kann ausserhalb des Repositories mit Windows DPAPI verschluesselt
gespeichert werden. Er wird dabei nicht als Klartext in der PowerShell-Historie
abgelegt:

```powershell
.\save_fb_token.ps1
```

Das Script fragt den Token verdeckt ab und speichert ihn unter
`%LOCALAPPDATA%\fb_archiver\fb_page_token.xml`. Die Datei kann nur vom selben
Windows-Benutzer auf demselben Computer entschluesselt werden. Ein eingegebener
User Token wird fuer die MARCHIVUM-Seite automatisch in einen Page Token
umgewandelt; vor dem Speichern wird der Zugriff auf den Posts-Endpunkt geprueft.

Anschliessend kann der Archiver ueber den sicheren Starter ausgefuehrt werden:

```powershell
.\run_fb_archiver.ps1 --page "168701373143130" --out ".\archive_MARCHIVUM_GESAMT"
```

Der Starter setzt `FB_PAGE_TOKEN` nur fuer den gestarteten Prozess. Zum Loeschen
des gespeicherten Tokens:

```powershell
Remove-Item "$env:LOCALAPPDATA\fb_archiver\fb_page_token.xml"
```

## Nutzung

Die Kommandozeilenparameter sind in `fb_archiver.py` definiert:

- `--page` Seitenname, Seiten-ID oder Facebook-URL
- `--access-token` Page Access Token; optional, wenn `FB_PAGE_TOKEN` gesetzt ist
- `--out` Zielverzeichnis
- `--since` Startdatum im Format `YYYY-MM-DD`
- `--until` einschließlich Enddatum im Format `YYYY-MM-DD`
- `--graph-api-version` Graph API-Version, standardmäßig `v26.0`
- `--no-media` keine Bilder/Videos herunterladen
- `--overwrite` vorhandene Jahresordner ausdrücklich ersetzen
- `--limit` API-Seitenlimit pro Anfrage

Hilfe anzeigen:

```powershell
python fb_archiver.py --help
```

API-Version explizit überschreiben:

```powershell
python fb_archiver.py --page "168701373143130" --graph-api-version "v26.0" --out ".\archive_MARCHIVUM"
```

### Kleiner Testlauf

Ein kurzer Smoke-Test ohne Medien-Downloads:

```powershell
python fb_archiver.py --page "168701373143130" --out ".\smoke_MARCHIVUM_2025_01" --since "2025-01-01" --until "2025-01-07" --no-media
```

### MARCHIVUM: Jahr 2025

Die bekannte numerische Seiten-ID ist robuster als der veränderbare Seitenname. Mit gesetztem `FB_PAGE_TOKEN` lautet der Aufruf:

```powershell
python .\fb_archiver.py --page "168701373143130" --since "2025-01-01" --until "2025-12-31" --out ".\archive_MARCHIVUM_2025"
```

Mit der gebauten Windows-Datei lautet derselbe Aufruf:

```powershell
.\dist\fb_archiver.exe --page "168701373143130" --since "2025-01-01" --until "2025-12-31" --out ".\archive_MARCHIVUM_2025"
```

Die Profilseiten-URL wird ebenfalls akzeptiert:

```powershell
python .\fb_archiver.py --page "https://www.facebook.com/MARCHIVUMMannheim" --since "2025-01-01" --until "2025-12-31" --out ".\archive_MARCHIVUM_2025"
```

Ein nicht leerer Ordner wie `archive_MARCHIVUM_2025\2025` wird standardmäßig nicht verändert. Für einen bewusst vollständigen Neuaufbau kann `--overwrite` ergänzt werden. Dabei wird nur der betroffene Jahresordner ersetzt.

### Vollständiger Lauf

Ein vollständiger Export ohne feste Datumsgrenzen:

```powershell
python fb_archiver.py --page "168701373143130" --out ".\archive_MARCHIVUM"
```

## Wichtige Besonderheiten

### Jahresweise Ausgabe

Der Einstiegspunkt ruft `run_split_by_years()` auf. Das Tool verarbeitet die Daten deshalb jahresweise und legt unterhalb des Zielordners pro Jahr einen eigenen Unterordner an.

Beispiel:

```text
archive_MARCHIVUM/
  2024/
  2025/
  2026/
```

Wenn `--since` und `--until` innerhalb desselben Jahres liegen, entsteht trotzdem ein Jahresordner für genau dieses Jahr.

### Seitenname vs. Seiten-ID

`--page` akzeptiert Namen, IDs und URLs. In der Praxis ist eine numerische Facebook-Seiten-ID oder eine vollständige Facebook-URL robuster als ein bloßer Seitenname, weil Seitennamen nicht immer eindeutig oder direkt über die Graph API auflösbar sind.

### Medien

Medien-Downloads sind `best effort`. Nicht jedes Bild oder Video ist direkt abrufbar. Das Tool protokolliert Warnungen und Quellen im Manifest.

## Ausgabe

Die Ausgabestruktur wird im Archiv zusätzlich noch einmal als `README.txt` beschrieben. Typischerweise sieht sie so aus:

```text
archive_Marchivum/
  2025/
    data/
      posts.jsonl
      posts.csv
      comments.jsonl
      comments.csv
      reactions.jsonl
      reactions.csv
      conversations.jsonl
      conversations.csv
      messages.jsonl
      messages.csv
      albums.jsonl
      albums.csv
      photos.jsonl
      photos.csv
      events.jsonl
      events.csv
      live_videos.jsonl
      live_videos.csv
    media/
      images/
      videos/
    manifests/
      checksums.sha256
      sources.txt
    README.txt
```

Wichtige Dateien:

- `data/*.jsonl` Rohdaten für weitere Verarbeitung
- `data/*.csv` übersichtliche Tabellenexporte
- `manifests/checksums.sha256` Prüfsummen aller erzeugten Dateien
- `manifests/sources.txt` API-Basis, Parameter und Warnungen
- `README.txt` Beschreibung des jeweiligen Exports

## Entwicklung und Tests

Vorhandene Tests laufen mit `pytest`:

```powershell
python -m pytest -q
```

Die Tests decken Datums- und CLI-Logik, API-Retries und Token-Redaktion, Medien-Downloads sowie einen simulierten vollständigen Archivlauf ab. Ein echter End-to-End-Lauf gegen Facebook benötigt weiterhin ein gültiges Token und Netzwerkzugriff.

Für Entwicklung und den Bau der Windows-Datei werden die zusätzlichen Werkzeuge separat installiert:

```powershell
python -m pip install -r requirements-dev.txt
python -m PyInstaller --clean --noconfirm fb_archiver.spec
```

Die ausführbare Datei liegt anschließend unter `dist\fb_archiver.exe`.

## Grenzen

- Nur Facebook-Seiten, keine privaten Profile oder Gruppen
- Ergebnisse hängen von den verfügbaren Graph-API-Feldern und Token-Rechten ab
- Inbox-/Nachrichtenarchivierung funktioniert nur mit zusätzlichen Rechten
- Medien und manche Sonderobjekte sind nicht immer vollständig abrufbar

## Datenschutz

Kommentare, Reaktionen und besonders Nachrichten können personenbezogene Daten enthalten. Für Archivierung, Zugriff und Weitergabe müssen die rechtlichen und organisatorischen Anforderungen im jeweiligen Umfeld eingehalten werden.
