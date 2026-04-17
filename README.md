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
- `pandas`

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

## Nutzung

Die Kommandozeilenparameter sind in `fb_archiver.py` definiert:

- `--page` Seitenname, Seiten-ID oder Facebook-URL
- `--access-token` Page Access Token
- `--out` Zielverzeichnis
- `--since` Startdatum im Format `YYYY-MM-DD`
- `--until` Enddatum im Format `YYYY-MM-DD`
- `--no-media` keine Bilder/Videos herunterladen
- `--limit` API-Seitenlimit pro Anfrage

Hilfe anzeigen:

```powershell
python fb_archiver.py --help
```

### Kleiner Testlauf

Ein kurzer Smoke-Test ohne Medien-Downloads:

```powershell
python fb_archiver.py --page "168701373143130" --access-token $env:FB_PAGE_TOKEN --out ".\smoke_MARCHIVUM_2025_01" --since "2025-01-01" --until "2025-01-07" --no-media
```

### Vollständiger Lauf

Ein vollständiger Export ohne feste Datumsgrenzen:

```powershell
python fb_archiver.py --page "168701373143130" --access-token $env:FB_PAGE_TOKEN --out ".\archive_MARCHIVUM"
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

Die vorhandenen Tests decken aktuell vor allem Hilfsfunktionen und Teile des Medien-Downloads ab. Ein echter End-to-End-Lauf gegen Facebook benötigt weiterhin ein gültiges Token und Netzwerkzugriff.

## Grenzen

- Nur Facebook-Seiten, keine privaten Profile oder Gruppen
- Ergebnisse hängen von den verfügbaren Graph-API-Feldern und Token-Rechten ab
- Inbox-/Nachrichtenarchivierung funktioniert nur mit zusätzlichen Rechten
- Medien und manche Sonderobjekte sind nicht immer vollständig abrufbar

## Datenschutz

Kommentare, Reaktionen und besonders Nachrichten können personenbezogene Daten enthalten. Für Archivierung, Zugriff und Weitergabe müssen die rechtlichen und organisatorischen Anforderungen im jeweiligen Umfeld eingehalten werden.
