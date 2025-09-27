
def detect_first_post_date(page: str, token: str) -> str:
    """Fragt das erste Post-Datum einer Seite ab."""
    import requests
    GRAPH = "https://graph.facebook.com/v19.0"
    url = f"{GRAPH}/{page}/posts"
    params = {
        "access_token": token,
        "fields": "created_time",
        "limit": 1,
        "until": "2012-01-01"  # sicherstellen, dass auch ältere Posts berücksichtigt werden
    }
    r = requests.get(url, params=params, timeout=60)
    if r.status_code == 200:
        data = r.json().get("data", [])
        if data:
            return data[-1]["created_time"].split("T")[0]
    # Fallback: 2010-01-01
    return "2010-01-01"


#!/usr/bin/env python3
"""
fb_archiver.py — Graph-API-basiertes Facebook-Archiv-Tool für Seiten (Pages)

FUNKTION
- Holt Beiträge (Posts) einer Facebook-Seite inkl. zentraler Metadaten
- Holt Reaktionen (nur Summen), Shares (Summe), Kommentare (Text, Autor, Zeit)
- Unterstützt verschachtelte Kommentare (Replies) für vollständige Diskussionsthreads
- Holt optional Inbox-Konversationen & Nachrichten (sofern Access Token die Rechte hat)
- CSV-Ausgaben enthalten zusätzliche Struktur-Infos (z. B. depth, parent_id)
- Sichert Medien (Bilder/Videos), soweit über die API/Permalinks zugänglich (best effort)
- Schreibt alles als JSONL (maschinell) + CSV (Übersicht) + Checksums
- Legt eine einfache, archivfreundliche Verzeichnisstruktur an

WICHTIG
- Arbeitet mit der offiziellen Facebook Graph API.
- Archiviert öffentliche Inhalte einer Facebook-Seite, für die ihr berechtigt seid.
- Nachrichten/Inbox werden nur archiviert, wenn das Token die Scopes `pages_messaging` (und ggf. `pages_manage_metadata`) besitzt.
- Private Profile/Gruppen sind nicht unterstützt (API-Beschränkung).

VORAUSSETZUNGEN
- Python 3.9+
- pip install -r requirements.txt
  (requests, python-dateutil, tqdm, pandas)
- Facebook-App + Page-Access-Token mit den nötigen Scopes

BEISPIEL
    python fb_archiver.py \
        --page "StadtMannheim" \
        --access-token "EAAG..." \
        --since "2020-01-01" \
        --until "2025-12-31" \
        --out ./archive_Marchivum

AUSGABE
archive_Marchivum/
  ├─ data/
  │   ├─ posts.jsonl, posts.csv
  │   ├─ comments.jsonl, comments.csv
  │   ├─ conversations.jsonl, conversations.csv   # Inbox-Daten (falls Token-Rechte vorhanden)
  │   └─ messages.jsonl, messages.csv             # Nachrichten (falls Token-Rechte vorhanden)
  ├─ media/ (images/..., videos/...)
  ├─ manifests/
  │   ├─ checksums.sha256
  │   └─ sources.txt
  └─ README.txt

DSGVO-HINWEIS
- Kommentare und Nachrichten enthalten personenbezogene Daten:
  Zugriff im Archiv ggf. mit Sperrfristen regeln.
"""


import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from tqdm import tqdm
import pandas as pd

# Optionaler Fallback, falls python-dateutil fehlt
try:
    from dateutil import parser as dtparse
except Exception:
    import datetime as _dt

    class _FallbackParser:
        @staticmethod
        def parse(s: str):
            # Versuche ISO 8601, sonst YYYY-MM-DD, sonst jetzt()
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    return _dt.datetime.strptime(s, fmt)
                except Exception:
                    pass
            try:
                return _dt.datetime.fromisoformat(s)  # Python 3.11: robust
            except Exception:
                return _dt.datetime.now(timezone.utc)

    dtparse = _FallbackParser()

GRAPH = "https://graph.facebook.com/v19.0"


@dataclass
class PageInfo:
    id: str
    name: str
    link: Optional[str]


def iso8601(dt: Optional[str]) -> Optional[str]:
    if not dt:
        return None
    try:
        return dtparse.parse(dt).isoformat()
    except Exception:
        return dt


def safe_filename(name: str) -> str:
    name = name.strip().replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name)[:200]


class FacebookArchiver:
    def __init__(
        self,
        page: str,
        access_token: str,
        outdir: str,
        since: Optional[str] = None,
        until: Optional[str] = None,
        media: bool = True,
        limit: int = 100,
    ):
        self.page = page
        self.token = access_token
        self.outdir = outdir
        self.since = since
        self.until = until
        self.media = media
        self.limit = limit

        os.makedirs(self.outdir, exist_ok=True)
        os.makedirs(self.path("data"), exist_ok=True)
        os.makedirs(self.path("media/images"), exist_ok=True)
        os.makedirs(self.path("media/videos"), exist_ok=True)
        os.makedirs(self.path("manifests"), exist_ok=True)

        self.session = requests.Session()
        self.session.params = {"access_token": self.token}
        self.page_info: Optional[PageInfo] = None

    def path(self, *parts) -> str:
        return os.path.join(self.outdir, *parts)

    def graph_get(self, endpoint: str, params: Dict) -> Dict:
        url = f"{GRAPH}/{endpoint.lstrip('/')}"
        retries = 0
        max_retries = 10  # maximal 10 Wiederholungen

        while True:
            r = self.session.get(url, params=params, timeout=60)
            if r.status_code == 200:
                return r.json()

            # Bei temporären Fehlern: wiederholen
            if r.status_code in (429, 500, 502, 503, 504):
                retries += 1
                if retries > max_retries:
                    raise RuntimeError(
                        f"Graph API error {r.status_code} after {max_retries} retries: {r.text}"
                    )

                retry_after = int(r.headers.get("Retry-After", "5"))
                wait_time = min(retry_after, 30)
                print(
                    f"[WARN] Graph API {r.status_code}, retry {retries}/{max_retries} in {wait_time}s ..."
                )
                time.sleep(wait_time)
                continue

            # Alle anderen Fehler sofort abbrechen
            raise RuntimeError(f"Graph API error {r.status_code}: {r.text}")

    def get_page_info(self) -> PageInfo:
        data = self.graph_get(self.page, {"fields": "id,name,link"})
        self.page_info = PageInfo(
            id=data["id"], name=data.get("name", self.page), link=data.get("link")
        )
        return self.page_info

    def iter_posts(self) -> Iterable[Dict]:
        fields = [
            "id",
            "created_time",
            "message",
            "permalink_url",
            "updated_time",
            "reactions.limit(0).summary(total_count)"
        ]
        params = {"fields": ",".join(fields), "limit": self.limit}
        if self.since:
            params["since"] = int(dtparse.parse(self.since).timestamp())
        if self.until:
            params["until"] = int(dtparse.parse(self.until).timestamp())

        endpoint = f"{self.page_info.id}/posts"
        next_url = None
        with tqdm(desc="Posts", unit="post") as bar:
            while True:
                if next_url:
                    retries = 0
                    while True:
                        r = self.session.get(next_url, timeout=60)
                        if r.status_code == 200:
                            data = r.json()
                            break
                        if r.status_code in (429, 500, 502, 503, 504):
                            retries += 1
                            if retries > 5:
                                raise RuntimeError(
                                    f"Graph paging error {r.status_code} after retries: {r.text}"
                                )
                            wait_time = min(int(r.headers.get("Retry-After", "5")), 30)
                            print(
                                f"[WARN] Paging error {r.status_code}, retry {retries}/5 in {wait_time}s ..."
                            )
                            time.sleep(wait_time)
                            continue
                        raise RuntimeError(
                            f"Graph paging error {r.status_code}: {r.text}"
                        )
                else:
                    data = self.graph_get(endpoint, params)

                posts = data.get("data", [])
                for p in posts:
                    bar.update(1)
                    yield p
                paging = data.get("paging", {})
                next_url = paging.get("next")
                if not next_url:
                    break

    
    def get_post_details(self, post_id: str) -> Dict:
        """Lädt Zusatzdetails zu einem einzelnen Post (z.B. attachments, story, shares)."""
        fields = [
            "status_type",
            "story",
            "shares",
            "is_published",
            "attachments{media_type,media,target,unshimmed_url,url,description,title}"
        ]
        try:
            data = self.graph_get(post_id, {"fields": ",".join(fields)})
            return data
        except Exception as e:
            self.append_sources_manifest(f"WARN details for {post_id}: {e}")
            return {}
    def get_comments_for_post(
        self, post_or_comment_id: str, depth: int = 0, parent: Optional[str] = None
    ) -> Iterable[Dict]:
        fields = [
            "id",
            "created_time",
            "message",
            "permalink_url",
            "from{id,name}",
            "comment_count",
            "parent{id}",
            "like_count",
        ]
        params = {"fields": ",".join(fields), "limit": 100}
        endpoint = f"{post_or_comment_id}/comments"
        next_url = None
        while True:
            if next_url:
                r = self.session.get(next_url, timeout=60)
                if r.status_code != 200:
                    raise RuntimeError(f"Graph paging error {r.status_code}: {r.text}")
                data = r.json()
            else:
                data = self.graph_get(endpoint, params)
            for c in data.get("data", []):
                c["depth"] = depth
                c["parent_id"] = parent
                yield c
                # Rekursiv Replies holen
                if c.get("comment_count") and int(c.get("comment_count")) > 0:
                    sub_id = c.get("id")
                    yield from self.get_comments_for_post(
                        sub_id, depth=depth + 1, parent=sub_id
                    )
            paging = data.get("paging", {})
            next_url = paging.get("next")
            if not next_url:
                break

    def get_conversations(self) -> Iterable[Dict]:
        """Liefert alle Inbox-Konversationen der Seite zurück."""
        endpoint = f"{self.page_info.id}/conversations"
        params = {"fields": "id,updated_time,link,participants"}
        next_url = None
        while True:
            if next_url:
                r = self.session.get(next_url, timeout=60)
                if r.status_code != 200:
                    raise RuntimeError(f"Graph paging error {r.status_code}: {r.text}")
                data = r.json()
            else:
                data = self.graph_get(endpoint, params)
            for conv in data.get("data", []):
                yield conv
            paging = data.get("paging", {})
            next_url = paging.get("next")
            if not next_url:
                break

    def get_messages(self, conversation_id: str) -> Iterable[Dict]:
        """Liefert alle Nachrichten einer Konversation."""
        endpoint = f"{conversation_id}/messages"
        params = {"fields": "id,from,to,message,created_time,attachments"}
        next_url = None
        while True:
            if next_url:
                r = self.session.get(next_url, timeout=60)
                if r.status_code != 200:
                    raise RuntimeError(f"Graph paging error {r.status_code}: {r.text}")
                data = r.json()
            else:
                data = self.graph_get(endpoint, params)
            for msg in data.get("data", []):
                yield msg
            paging = data.get("paging", {})
            next_url = paging.get("next")
            if not next_url:
                break

    def download_media_from_post(self, post: Dict) -> List[Tuple[str, str]]:
        """Gibt Liste (local_path, source_url) zurück für gespeicherte Dateien."""
        saved: List[Tuple[str, str]] = []
        atts = (post.get("attachments") or {}).get("data") or []
        for a in atts:
            mtype = (a.get("media_type") or "").lower()
            media = a.get("media") or {}
            src = None
            subdir = "images"
            if mtype in ("photo", "image"):
                src = (
                    (media.get("image") or {}).get("src")
                    or a.get("unshimmed_url")
                    or a.get("url")
                )
                subdir = "images"
            elif mtype in ("video", "native_video"):
                # Versuch, Video direkt über /videos zu holen
                self.download_video_from_post(post.get("id"))
                continue
            else:
                # Links/Alben etc. überspringen
                continue
            if not src:
                continue
            try:
                local = self._download_stream(src, subdir)
                if local:
                    saved.append((local, src))
            except Exception:
                # Medienfehler nicht abbrechen, aber vermerken
                self.append_sources_manifest(f"WARN media download failed: {src}")
        return saved

    def _download_stream(self, url: str, subdir: str) -> Optional[str]:
        r = self.session.get(url, timeout=60, stream=True)
        if r.status_code != 200:
            return None
        ext = self._guess_ext(r.headers.get("Content-Type"))
        fname = safe_filename(f"{int(time.time()*1000)}") + ext
        local = self.path("media", subdir, fname)
        with open(local, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return local

    def download_video_from_post(self, post_id: str) -> Optional[str]:
        try:
            data = self.graph_get(post_id, {"fields": "id,source,title,description"})
            src = data.get("source")
            if not src:
                return None
            local = self._download_stream(src, "videos")
            if local:
                self.append_sources_manifest(f"VIDEO {post_id} {local} <- {src}")
                return local
        except Exception as e:
            self.append_sources_manifest(f"WARN video source {post_id}: {e}")
        return None

    @staticmethod
    def _guess_ext(content_type: Optional[str]) -> str:
        if not content_type:
            return ""
        ct = content_type.lower()
        if "jpeg" in ct or "jpg" in ct:
            return ".jpg"
        if "png" in ct:
            return ".png"
        if "gif" in ct:
            return ".gif"
        if "mp4" in ct:
            return ".mp4"
        if "webm" in ct:
            return ".webm"
        return ""

    def write_readme(self):
        pi = self.page_info
        now = datetime.now(timezone.utc).isoformat() + "Z"
        txt = f"""
Facebook Archiv – erzeugt mit fb_archiver.py
Seite: {pi.name} (ID: {pi.id})
Link: {pi.link}

Erstellt (UTC): {now}
Abfragefenster: since={self.since or '-'} until={self.until or '-'}
Token-Hinweis: Page Access Token (nicht abgelegt)

Inhalte:
- data/posts.jsonl, data/comments.jsonl — maschinelle Rohdaten der Chronik
- data/posts.csv, data/comments.csv — Übersicht (comments.csv inkl. depth/parent_id)
- data/conversations.jsonl, data/messages.jsonl — Inbox-Rohdaten (falls Rechte vorhanden)
- data/conversations.csv, data/messages.csv — Übersicht der Konversationen und Nachrichten
- media/images, media/videos — heruntergeladene Medien (best effort)
- manifests/checksums.sha256 — Prüfsummen aller Dateien
- manifests/sources.txt — API-Endpunkte & Parameter / Warnungen

Hinweise:
- Kommentare und Nachrichten enthalten personenbezogene Daten. Zugriff ggf. beschränken (DSGVO!).
- Medien-Downloads sind best effort; manche Videos/Bilder sind nicht direkt abrufbar.
- Nachrichten-Archivierung erfordert zusätzliche Berechtigungen im Access Token: pages_messaging (und ggf. pages_manage_metadata).
""".strip()
        with open(self.path("README.txt"), "w", encoding="utf-8") as f:
            f.write(txt)

    def append_sources_manifest(self, line: str):
        with open(self.path("manifests", "sources.txt"), "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")

    def write_checksums(self):
        sha_path = self.path("manifests", "checksums.sha256")
        with open(sha_path, "w", encoding="utf-8") as out:
            for root, _, files in os.walk(self.outdir):
                for fn in files:
                    if fn.endswith(".sha256"):
                        continue
                    p = os.path.join(root, fn)
                    h = hashlib.sha256()
                    with open(p, "rb") as f:
                        for chunk in iter(lambda: f.read(8192), b""):
                            h.update(chunk)
                    rel = os.path.relpath(p, self.outdir)
                    out.write(f"{h.hexdigest()}  {rel}\n")

    def run(self):
        # Basis-Infos & Doku
        self.get_page_info()
        self.write_readme()
        self.append_sources_manifest(f"GRAPH_BASE={GRAPH}")
        self.append_sources_manifest(f"PAGE={self.page} -> {self.page_info.id}")
        if self.since:
            self.append_sources_manifest(f"SINCE={self.since}")
        if self.until:
            self.append_sources_manifest(f"UNTIL={self.until}")
    
        # Dateien vorbereiten
        posts_jsonl = open(self.path("data", "posts.jsonl"), "w", encoding="utf-8")
        comments_jsonl = open(
            self.path("data", "comments.jsonl"), "w", encoding="utf-8"
        )
        posts_rows: List[Dict] = []
        comments_rows: List[Dict] = []
    
        # Posts iterieren
        for post in self.iter_posts():
            pid = post.get("id")
            created = iso8601(post.get("created_time"))
            updated = iso8601(post.get("updated_time"))
            msg = (post.get("message") or "").replace("\n", " ").strip()
            perma = post.get("permalink_url")
            reacts = ((post.get("reactions") or {}).get("summary") or {}).get(
                "total_count"
            )
            shares = (post.get("shares") or {}).get("count")
    
            posts_rows.append(
                {
                    "post_id": pid,
                    "created_time": created,
                    "updated_time": updated,
                    "permalink_url": perma,
                    "message": msg,
                    "reactions_total": reacts,
                    "shares_count": shares,
                }
            )
            details = self.get_post_details(pid)
            post.update(details)
            if posts_rows:
                posts_rows[-1]["shares_count"] = (post.get("shares") or {}).get("count")
            posts_jsonl.write(json.dumps(post, ensure_ascii=False) + "\n")
    
            # Kommentare (rekursiv inkl. Replies)
            try:
                for c in self.get_comments_for_post(pid, depth=0, parent=None):
                    comments_rows.append(
                        {
                            "post_id": pid,
                            "comment_id": c.get("id"),
                            "created_time": iso8601(c.get("created_time")),
                            "author_id": ((c.get("from") or {}).get("id")),
                            "author_name": ((c.get("from") or {}).get("name")),
                            "message": (c.get("message") or "")
                            .replace("\n", " ")
                            .strip(),
                            "like_count": c.get("like_count"),
                            "parent_id": c.get("parent_id"),
                            "depth": c.get("depth"),
                            "permalink_url": c.get("permalink_url"),
                        }
                    )
                    comments_jsonl.write(json.dumps(c, ensure_ascii=False) + "\n")
            except Exception as e:
                self.append_sources_manifest(f"WARN comments for {pid}: {e}")
    
            # Medien (best effort)
            if self.media:
                saved = self.download_media_from_post(post)
                for local, src in saved:
                    self.append_sources_manifest(f"MEDIA {pid} {local} <- {src}")
    
        posts_jsonl.close()
        comments_jsonl.close()
    
        # Inbox-Konversationen archivieren
        conv_jsonl = open(
            self.path("data", "conversations.jsonl"), "w", encoding="utf-8"
        )
        msg_jsonl = open(self.path("data", "messages.jsonl"), "w", encoding="utf-8")
        conv_rows: List[Dict] = []
        msg_rows: List[Dict] = []
    
        try:
            for conv in self.get_conversations():
                conv_id = conv.get("id")
                conv_jsonl.write(json.dumps(conv, ensure_ascii=False) + "\n")
                conv_rows.append(
                    {
                        "conversation_id": conv_id,
                        "updated_time": iso8601(conv.get("updated_time")),
                        "link": conv.get("link"),
                        "participants": ", ".join(
                            [
                                p.get("name")
                                for p in (conv.get("participants", {}).get("data", []))
                            ]
                        ),
                    }
                )
                # Nachrichten innerhalb der Konversation
                for msg in self.get_messages(conv_id):
                    msg_jsonl.write(json.dumps(msg, ensure_ascii=False) + "\n")
                    msg_rows.append(
                        {
                            "conversation_id": conv_id,
                            "message_id": msg.get("id"),
                            "created_time": iso8601(msg.get("created_time")),
                            "from": (msg.get("from") or {}).get("name"),
                            "to": (
                                ", ".join(
                                    [
                                        t.get("name")
                                        for t in msg.get("to", {}).get("data", [])
                                    ]
                                )
                                if msg.get("to")
                                else None
                            ),
                            "message": (msg.get("message") or "")
                            .replace("\n", " ")
                            .strip(),
                        }
                    )
        except Exception as e:
            self.append_sources_manifest(f"WARN conversations/messages: {e}")
    
        conv_jsonl.close()
        msg_jsonl.close()
    
        if conv_rows:
            pd.DataFrame(conv_rows).to_csv(
                self.path("data", "conversations.csv"), index=False
            )
        if msg_rows:
            pd.DataFrame(msg_rows).to_csv(
                self.path("data", "messages.csv"), index=False
            )
    
        # CSV schreiben
        if posts_rows:
            pd.DataFrame(posts_rows).to_csv(self.path("data", "posts.csv"), index=False)
        if comments_rows:
            pd.DataFrame(comments_rows).to_csv(
                self.path("data", "comments.csv"), index=False
            )
    
        # Checksums
        self.write_checksums()
    
        print(f"Fertig. Archiv unter: {self.outdir}")
    
    
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Facebook Page Archiv (Graph API)")
    ap.add_argument(
        "--page", required=True, help="Seitenname oder ID (z.B. StadtMannheim)"
    )
    ap.add_argument(
        "--access-token",
        required=True,
        help="Page Access Token mit pages_read_* Rechten",
    )
    ap.add_argument("--out", default="./fb_archive_out", help="Ausgabeverzeichnis")
    ap.add_argument("--since", help="ab Datum (YYYY-MM-DD)")
    ap.add_argument("--until", help="bis Datum (YYYY-MM-DD)")
    ap.add_argument(
        "--no-media", action="store_true", help="keine Medien herunterladen"
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=100,
        help="API-Seitenlimit pro Anfrage (Standard 100)",
    )
    return ap.parse_args()




from pathlib import Path

def run_split_by_years(args):
    import datetime
    base_arch = FacebookArchiver(
        page=args.page,
        access_token=args.access_token,
        outdir=args.out,
        since=args.since,
        until=args.until,
        media=not args.no_media,
        limit=args.limit,
    )
    page_info = base_arch.get_page_info()
    first_date = detect_first_post_date(args.page, args.access_token)
    start_year = int(first_date.split("-")[0])
    end_year = datetime.datetime.now(timezone.utc).year
    print(f"[INFO] Archivierung von {start_year} bis {end_year} für {page_info.name}")

    for year in range(start_year, end_year + 1):
        since = f"{year}-01-01"
        until = f"{year}-12-31"
        year_out = str(Path(args.out) / str(year))
        print(f"[INFO] -> Jahr {year} ({since} bis {until})")
        arch = FacebookArchiver(
            page=args.page,
            access_token=args.access_token,
            outdir=year_out,
            since=since,
            until=until,
            media=not args.no_media,
            limit=args.limit,
        )
        try:
            arch.run()
        except Exception as e:
            print(f"[WARN] Fehler bei Jahr {year}: {e}")


if __name__ == "__main__":
    args = parse_args()
    run_split_by_years(args)
