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
  (requests, python-dateutil, tqdm)
- Facebook-App + Page-Access-Token mit den nötigen Scopes

BEISPIEL
    python fb_archiver.py \
        --page "168701373143130" \
        --since "2025-01-01" \
        --until "2025-12-31" \
        --out ./archive_MARCHIVUM_2025

    Der Page Access Token wird dabei aus FB_PAGE_TOKEN gelesen.

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
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests
from tqdm import tqdm

TOOL_VERSION = "2026.08.2"

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


def normalize_graph_api_version(version: Optional[str]) -> str:
    raw = (version or "26.0").strip()
    if not raw:
        raise ValueError("Graph API version darf nicht leer sein.")
    if not re.fullmatch(r"v?\d+\.\d+", raw):
        raise ValueError(
            f"Ungültige Graph API version '{version}'. Erwartet z. B. 'v26.0' oder '26.0'."
        )
    return raw if raw.startswith("v") else f"v{raw}"


DEFAULT_GRAPH_API_VERSION = normalize_graph_api_version(
    os.getenv("FB_GRAPH_API_VERSION", "v26.0")
)


def graph_base(version: Optional[str] = None) -> str:
    selected_version = normalize_graph_api_version(version or DEFAULT_GRAPH_API_VERSION)
    return f"https://graph.facebook.com/{selected_version}"


GRAPH = graph_base(DEFAULT_GRAPH_API_VERSION)


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


def parse_to_utc(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        dt = dtparse.parse(dt_str)
    except Exception:
        return None
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def prefer_latest_record(
    existing: Optional[Dict],
    candidate: Optional[Dict],
    time_keys: Tuple[str, ...] = ("updated_time", "created_time", "timestamp"),
) -> Optional[Dict]:
    if not existing:
        return candidate
    if not candidate:
        return existing

    def stamp(item: Dict) -> Optional[datetime]:
        for key in time_keys:
            if key in item:
                ts = parse_to_utc(item.get(key))
                if ts:
                    return ts
        return None

    existing_ts = stamp(existing)
    candidate_ts = stamp(candidate)

    if existing_ts and candidate_ts:
        if candidate_ts > existing_ts:
            return candidate
        if candidate_ts < existing_ts:
            return existing
    elif candidate_ts and not existing_ts:
        return candidate
    elif existing_ts and not candidate_ts:
        return existing

    existing_len = len(existing) if isinstance(existing, dict) else 0
    candidate_len = len(candidate) if isinstance(candidate, dict) else 0
    return candidate if candidate_len > existing_len else existing


def safe_filename(name: str) -> str:
    name = name.strip().replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name)[:200]


def redact_access_tokens(value: str) -> str:
    return re.sub(
        r"(?i)(access_token(?:%3D|=))[^&\s\"']+",
        r"\1[REDACTED]",
        str(value),
    )


def detect_first_post_date(
    page: str, token: str, api_version: Optional[str] = None
) -> str:
    """Ermittelt das älteste Post-Datum einer Seite (Paging, ohne until, ohne ID)."""
    url = f"{graph_base(api_version)}/{page}/posts"
    params = {
        "fields": "created_time",
        "limit": 100,
    }
    session = requests.Session()
    session.params = {"access_token": token}
    session.headers.update({"User-Agent": f"fb_archiver/{TOOL_VERSION}"})

    oldest = None
    page_count = 0
    while True:
        response = None
        for attempt in range(6):
            try:
                response = session.get(url, params=params, timeout=60)
            except requests.RequestException as exc:
                if attempt >= 5:
                    raise RuntimeError(
                        "Ältestes Post-Datum konnte wegen eines Netzwerkfehlers "
                        f"nicht ermittelt werden: {redact_access_tokens(exc)}"
                    ) from exc
                time.sleep(min(2**attempt, 30))
                continue

            if response.status_code == 200:
                break
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 5:
                time.sleep(min(2**attempt, 30))
                continue
            raise RuntimeError(
                "Ältestes Post-Datum konnte nicht ermittelt werden: "
                f"Graph API {response.status_code}: "
                f"{redact_access_tokens(response.text)}"
            )

        if response is None or response.status_code != 200:
            raise RuntimeError("Ältestes Post-Datum konnte nicht ermittelt werden.")

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                "Ältestes Post-Datum: Graph API lieferte ungültiges JSON."
            ) from exc
        posts = data.get("data", [])
        if not posts:
            break

        page_count += 1
        oldest = posts[-1]["created_time"].split("T")[0]
        print(f"[DEBUG] Seite {page_count}: ältestes bisher {oldest}")

        paging = data.get("paging", {})
        if "next" in paging:
            url = paging["next"]
            params = {}  # "next" enthält alles Nötige
        else:
            break

    if oldest is None:
        oldest = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        print("[INFO] Keine Posts gefunden; starte mit dem aktuellen Jahr.")
    else:
        print(f"[INFO] Ältestes gefundenes Datum: {oldest}")
    return oldest


def to_utc_epoch(dt_str: str) -> int:
    """Konvertiert einen Datum/Zeit-String in Sekunden seit Epoche (UTC)."""
    dt = dtparse.parse(dt_str)
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp())


def to_api_until_epoch(dt_str: str) -> int:
    """Meta behandelt ``until`` exklusiv; ein reines Datum umfasst daher den Folgetag."""
    dt = dtparse.parse(dt_str)
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", dt_str.strip()):
        dt += timedelta(days=1)
    return int(dt.timestamp())


def parse_cli_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Ungültiges Datum '{value}'. Erwartet wird YYYY-MM-DD."
        ) from exc
    return value


def prepare_output_directory(path: Path, overwrite: bool = False) -> None:
    """Verhindert, dass neue Exporte unbemerkt mit alten Dateien vermischt werden."""
    if not path.exists() or not any(path.iterdir()):
        return
    if not overwrite:
        raise FileExistsError(
            f"Ausgabeverzeichnis ist nicht leer: {path}. "
            "Nutze ein neues --out oder bestätige das Ersetzen mit --overwrite."
        )
    shutil.rmtree(path)


def write_csv_rows(path: str, rows: List[Dict]) -> None:
    """Schreibt Zeilen als CSV und bildet die Spaltenmenge stabil aus allen Dict-Keys."""
    if not rows:
        return

    fieldnames: List[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)

    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


class GraphAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int,
        code: Optional[int] = None,
        subcode: Optional[int] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.subcode = subcode


def is_authentication_error(exc: BaseException) -> bool:
    return isinstance(exc, GraphAPIError) and exc.code == 190


class MediaDownloadRejected(RuntimeError):
    """Das geladene Payload ist kein archivierungsfaehiges Medium."""


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
        graph_api_version: Optional[str] = None,
        prepare_output_dirs: bool = True,
    ):
        self.page = page
        self.token = access_token
        self.outdir = outdir
        self.since = since
        self.until = until
        self.media = media
        self.limit = limit
        self.graph_api_version = normalize_graph_api_version(
            graph_api_version or DEFAULT_GRAPH_API_VERSION
        )
        self.graph_base = graph_base(self.graph_api_version)

        self._since_dt = parse_to_utc(self.since)
        self._until_dt = parse_to_utc(self.until)
        self._until_cutoff = None
        if self._until_dt:
            if self._until_dt.time() == datetime.min.time():
                self._until_cutoff = self._until_dt + timedelta(days=1)
            else:
                self._until_cutoff = self._until_dt

        if prepare_output_dirs:
            os.makedirs(self.outdir, exist_ok=True)
            os.makedirs(self.path("data"), exist_ok=True)
            os.makedirs(self.path("media/images"), exist_ok=True)
            os.makedirs(self.path("media/videos"), exist_ok=True)
            os.makedirs(self.path("manifests"), exist_ok=True)

        self.session = requests.Session()
        self.session.params = {"access_token": self.token}
        self.session.headers.update({"User-Agent": f"fb_archiver/{TOOL_VERSION}"})
        # Media URLs can point to arbitrary third-party hosts. They must never use
        # the Graph session, which carries the Page token as a default parameter.
        self.media_session = requests.Session()
        self.media_session.headers.update(
            {"User-Agent": f"fb_archiver/{TOOL_VERSION}"}
        )
        self.page_info: Optional[PageInfo] = None

    def path(self, *parts) -> str:
        return os.path.join(self.outdir, *parts)

    def graph_get(self, endpoint: str, params: Dict) -> Dict:
        url = f"{self.graph_base}/{endpoint.lstrip('/')}"
        return self._request_json(url, params=params, context="Graph API")

    def _request_json(
        self,
        url: str,
        params: Optional[Dict] = None,
        context: str = "Graph API paging",
        max_retries: int = 6,
    ) -> Dict:
        transient_codes = {1, 2, 4, 17, 32, 613}
        for attempt in range(max_retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=60)
            except requests.RequestException as exc:
                if attempt >= max_retries:
                    raise RuntimeError(
                        f"{context} network error after {max_retries} retries: "
                        f"{redact_access_tokens(exc)}"
                    ) from exc
                wait_time = min(2**attempt, 30)
                print(
                    f"[WARN] {context} network error, retry "
                    f"{attempt + 1}/{max_retries} in {wait_time}s ..."
                )
                time.sleep(wait_time)
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise RuntimeError(f"{context} returned invalid JSON") from exc

            error_code = None
            error_subcode = None
            message = response.text
            try:
                error = (response.json() or {}).get("error") or {}
                error_code = error.get("code")
                error_subcode = error.get("error_subcode")
                message = error.get("message") or message
            except ValueError:
                pass

            temporary = response.status_code in {429, 500, 502, 503, 504}
            temporary = temporary or error_code in transient_codes
            if temporary and attempt < max_retries:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_time = min(max(float(retry_after), 0), 30)
                except (TypeError, ValueError):
                    wait_time = min(2**attempt, 30)
                print(
                    f"[WARN] {context} {response.status_code}, retry "
                    f"{attempt + 1}/{max_retries} in {wait_time:g}s ..."
                )
                time.sleep(wait_time)
                continue

            details = redact_access_tokens(message)
            code_info = f"code={error_code}"
            if error_subcode is not None:
                code_info += f", subcode={error_subcode}"
            if error_code == 190:
                details += " (Access Token ist ungültig oder abgelaufen.)"
            elif error_code in {10, 200}:
                details += " (Berechtigung oder App-Freigabe fehlt.)"
            raise GraphAPIError(
                f"{context} error {response.status_code} ({code_info}): {details}",
                status_code=response.status_code,
                code=error_code,
                subcode=error_subcode,
            )

        raise RuntimeError(f"{context} failed unexpectedly")

    def _is_within_requested_range(self, dt_str: Optional[str]) -> bool:
        if not dt_str:
            return True
        dt = parse_to_utc(dt_str)
        if not dt:
            return True
        if self._since_dt and dt < self._since_dt:
            return False
        if self._until_cutoff and dt >= self._until_cutoff:
            return False
        return True

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
            params["since"] = to_utc_epoch(self.since)
        if self.until:
            params["until"] = to_api_until_epoch(self.until)

        endpoint = f"{self.page_info.id}/posts"
        next_url = None
        with tqdm(desc="Posts", unit="post") as bar:
            while True:
                if next_url:
                    data = self._request_json(next_url, context="Posts paging")
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
            if is_authentication_error(e):
                raise
            self.append_sources_manifest(f"WARN details for {post_id}: {e}")
            return {}
    def get_reactions_for_post(self, post_id: str) -> Iterable[Dict]:
        """Liefert detaillierte Reaktionen für einen Post (WER hat WIE reagiert)."""
        fields = ["id", "name", "type"]
        endpoint = f"{post_id}/reactions"
        params = {"fields": ",".join(fields), "limit": 100, "summary": "true"}
        next_url = None
        while True:
            if next_url:
                data = self._request_json(next_url, context="Reactions paging")
            else:
                data = self.graph_get(endpoint, params)
            for reaction in data.get("data", []):
                yield reaction
            paging = data.get("paging", {})
            next_url = paging.get("next")
            if not next_url:
                break

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
                data = self._request_json(next_url, context="Comments paging")
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
                data = self._request_json(next_url, context="Conversations paging")
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
                data = self._request_json(next_url, context="Messages paging")
            else:
                data = self.graph_get(endpoint, params)
            for msg in data.get("data", []):
                yield msg
            paging = data.get("paging", {})
            next_url = paging.get("next")
            if not next_url:
                break

    def get_albums(self) -> Iterable[Dict]:
        """Liefert alle Alben der Seite zurück."""
        fields = [
            "id",
            "name",
            "description",
            "created_time",
            "updated_time",
            "link",
            "count",
            "type",
            "cover_photo{id,picture,images}"
        ]
        endpoint = f"{self.page_info.id}/albums"
        params = {"fields": ",".join(fields), "limit": 100}
        next_url = None
        with tqdm(desc="Albums", unit="album") as bar:
            while True:
                if next_url:
                    data = self._request_json(next_url, context="Albums paging")
                else:
                    data = self.graph_get(endpoint, params)
                for album in data.get("data", []):
                    bar.update(1)
                    yield album
                paging = data.get("paging", {})
                next_url = paging.get("next")
                if not next_url:
                    break

    def get_photos_from_album(self, album_id: str) -> Iterable[Dict]:
        """Liefert alle Fotos eines Albums."""
        fields = [
            "id",
            "created_time",
            "updated_time",
            "name",
            "picture",
            "images",
            "link",
            "alt_text",
            "width",
            "height"
        ]
        endpoint = f"{album_id}/photos"
        params = {"fields": ",".join(fields), "limit": 100}
        next_url = None
        while True:
            if next_url:
                data = self._request_json(next_url, context="Photos paging")
            else:
                data = self.graph_get(endpoint, params)
            for photo in data.get("data", []):
                yield photo
            paging = data.get("paging", {})
            next_url = paging.get("next")
            if not next_url:
                break

    def get_events(self) -> Iterable[Dict]:
        """Liefert alle Events der Seite zurück (Vergangenheit & Zukunft)."""
        fields = [
            "id",
            "name",
            "description",
            "start_time",
            "end_time",
            "updated_time",
            "event_times",
            "place",
            "cover",
            "attending_count",
            "declined_count",
            "interested_count",
            "maybe_count",
            "noreply_count",
            "ticket_uri",
            "category",
            "is_canceled",
            "is_online",
            "is_page_owned",
        ]
        endpoint = f"{self.page_info.id}/events"
        base_params = {
            "fields": ",".join(fields),
            "limit": self.limit,
            "include_canceled": True,
        }
        if self.since:
            base_params["since"] = to_utc_epoch(self.since)
        if self.until:
            base_params["until"] = to_api_until_epoch(self.until)

        seen_ids: set[str] = set()
        filters = ["upcoming", "past"]
        with tqdm(desc="Events", unit="event") as bar:
            for time_filter in filters:
                params = dict(base_params)
                params["time_filter"] = time_filter
                next_url = None
                while True:
                    if next_url:
                        data = self._request_json(next_url, context="Events paging")
                    else:
                        try:
                            data = self.graph_get(endpoint, params)
                        except RuntimeError as exc:
                            if is_authentication_error(exc):
                                raise
                            # Wenn der Filter nicht unterstützt wird, loggen und abbrechen
                            self.append_sources_manifest(f"WARN events ({time_filter}): {exc}")
                            break
                    for event in data.get("data", []):
                        eid = event.get("id")
                        if not eid or eid in seen_ids:
                            continue
                        seen_ids.add(eid)
                        bar.update(1)
                        yield event
                    paging = data.get("paging", {})
                    next_url = paging.get("next")
                    if not next_url:
                        break

    def get_live_videos(self) -> Iterable[Dict]:
        """Liefert alle Live Videos der Seite zurück."""
        fields = [
            "id",
            "title",
            "description",
            "created_time",
            "updated_time",
            "permalink_url",
            "video",
            "status",
            "live_views",
            "total_views",
            "embed_html",
            "is_reference_only",
            "broadcast_start_time"
        ]
        endpoint = f"{self.page_info.id}/live_videos"
        base_params = {"fields": ",".join(fields), "limit": 100}
        seen_ids: set[str] = set()
        statuses = ["LIVE", "LIVE_STOPPED", "VOD"]
        with tqdm(desc="Live Videos", unit="video") as bar:
            for status in statuses:
                params = dict(base_params)
                params["broadcast_status"] = json.dumps([status])
                next_url = None
                while True:
                    if next_url:
                        data = self._request_json(next_url, context="Live videos paging")
                    else:
                        try:
                            data = self.graph_get(endpoint, params)
                        except Exception as e:
                            if is_authentication_error(e):
                                raise
                            self.append_sources_manifest(f"WARN live_videos ({status}): {e}")
                            break
                    for video in data.get("data", []):
                        vid = video.get("id")
                        if not vid or vid in seen_ids:
                            continue
                        seen_ids.add(vid)
                        bar.update(1)
                        yield video
                    paging = data.get("paging", {})
                    next_url = paging.get("next")
                    if not next_url:
                        break

    def download_media_from_post(self, post: Dict) -> List[Tuple[str, str]]:
        """Gibt Liste (local_path, source_url) zurück für gespeicherte Dateien."""
        saved: List[Tuple[str, str]] = []
        atts = (post.get("attachments") or {}).get("data") or []
        for attachment in atts:
            self._process_attachment_media(post.get("id"), attachment, saved)
        return saved

    def _process_attachment_media(
        self, post_id: Optional[str], attachment: Dict, saved: List[Tuple[str, str]]
    ) -> None:
        subattachments = (attachment.get("subattachments") or {}).get("data") or []
        for sub in subattachments:
            self._process_attachment_media(post_id, sub, saved)

        mtype = (attachment.get("media_type") or "").lower()
        media = attachment.get("media") or {}

        if mtype in ("video", "native_video", "video_inline", "video_direct_response"):
            # Versuche, Video-Quellen aus dem Attachment zu nutzen (Fallback holt Details nach)
            self.download_video_from_post(post_id, attachment)
            return

        src = (media.get("image") or {}).get("src")

        if not src:
            return

        try:
            local = self._download_stream(src, "images", expected_kind="image")
            if local:
                saved.append((local, src))
        except Exception as e:
            # Medienfehler nicht abbrechen, aber vermerken
            self.append_sources_manifest(f"WARN media download failed: {src} - {e}")

    @staticmethod
    def _looks_like_html(payload: bytes) -> bool:
        head = payload.lstrip()[:512].lower()
        return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<html" in head

    @staticmethod
    def _looks_like_image_payload(payload: bytes) -> bool:
        return (
            payload.startswith(b"\x89PNG\r\n\x1a\n")
            or payload.startswith(b"\xff\xd8\xff")
            or payload.startswith((b"GIF87a", b"GIF89a"))
            or payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"
        )

    @staticmethod
    def _looks_like_video_payload(payload: bytes) -> bool:
        return payload[4:8] == b"ftyp" or payload.startswith(b"\x1a\x45\xdf\xa3")

    @staticmethod
    def _png_dimensions(payload: bytes) -> Optional[Tuple[int, int]]:
        if len(payload) < 24 or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return None
        width = int.from_bytes(payload[16:20], "big")
        height = int.from_bytes(payload[20:24], "big")
        return width, height

    @staticmethod
    def _gif_dimensions(payload: bytes) -> Optional[Tuple[int, int]]:
        if len(payload) < 10 or not payload.startswith((b"GIF87a", b"GIF89a")):
            return None
        width = int.from_bytes(payload[6:8], "little")
        height = int.from_bytes(payload[8:10], "little")
        return width, height

    @staticmethod
    def _is_external_video_embed(url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return host in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "youtube-nocookie.com",
            "www.youtube-nocookie.com",
            "youtu.be",
        }

    def _placeholder_image_reason(self, payload: bytes) -> Optional[str]:
        dims = self._png_dimensions(payload) or self._gif_dimensions(payload)
        if dims and dims[0] <= 1 and dims[1] <= 1:
            return f"placeholder image {dims[0]}x{dims[1]}"
        return None

    def _download_stream(
        self, url: str, subdir: str, expected_kind: Optional[str] = None
    ) -> Optional[str]:
        if expected_kind == "video" and self._is_external_video_embed(url):
            raise MediaDownloadRejected("external video embed instead of downloadable video")

        r = self.media_session.get(url, timeout=(10, 30), stream=True)
        if r.status_code != 200:
            raise MediaDownloadRejected(f"HTTP {r.status_code}")

        content_type = (r.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        chunks = r.iter_content(chunk_size=8192)
        first_chunk = b""
        for chunk in chunks:
            if chunk:
                first_chunk = chunk
                break

        if not first_chunk:
            raise MediaDownloadRejected("empty response")

        if self._looks_like_html(first_chunk):
            raise MediaDownloadRejected(
                f"expected {expected_kind or 'media'}, got HTML content"
            )

        if expected_kind == "image":
            if content_type and not (
                content_type.startswith("image/")
                or content_type == "application/octet-stream"
                and self._looks_like_image_payload(first_chunk)
            ):
                raise MediaDownloadRejected(f"expected image, got {content_type}")
            if not content_type and not self._looks_like_image_payload(first_chunk):
                raise MediaDownloadRejected("expected image payload")
            placeholder_reason = self._placeholder_image_reason(first_chunk)
            if placeholder_reason:
                raise MediaDownloadRejected(placeholder_reason)

        if expected_kind == "video":
            if content_type and not (
                content_type.startswith("video/")
                or content_type == "application/octet-stream"
                and self._looks_like_video_payload(first_chunk)
            ):
                raise MediaDownloadRejected(f"expected video, got {content_type}")
            if not content_type and not self._looks_like_video_payload(first_chunk):
                raise MediaDownloadRejected("expected video payload")

        ext = self._guess_ext(r.headers.get("Content-Type"))
        fname = safe_filename(f"{int(time.time()*1000)}") + ext
        local = self.path("media", subdir, fname)
        with open(local, "wb") as f:
            f.write(first_chunk)
            for chunk in chunks:
                if chunk:
                    f.write(chunk)
        return local

    def download_video_from_post(
        self, post_id: Optional[str], attachment: Optional[Dict] = None
    ) -> Optional[str]:
        video_id: Optional[str] = None
        src: Optional[str] = None

        if attachment:
            media = attachment.get("media") or {}
            src = media.get("source") or attachment.get("source")
            video_id = (attachment.get("target") or {}).get("id") or attachment.get("id")

        # Falls noch keine Quelle vorhanden ist, Details zum Post nachladen
        if not src and post_id:
            try:
                details = self.get_post_details(post_id)
            except Exception as exc:
                if is_authentication_error(exc):
                    raise
                details = {}
            attachments = (details.get("attachments") or {}).get("data") or []
            for att in attachments:
                att_type = (att.get("media_type") or "").lower()
                if att_type not in ("video", "native_video", "video_inline", "video_direct_response"):
                    continue
                media = att.get("media") or {}
                if not src:
                    src = media.get("source") or att.get("source")
                if not video_id:
                    video_id = (att.get("target") or {}).get("id") or att.get("id")
                if src:
                    break

        # Wenn eine Video-ID existiert, aber noch keine Quelle, gezielt nach der Quelle fragen
        lookup_id = video_id
        if lookup_id and not src:
            try:
                data = self.graph_get(lookup_id, {"fields": "source"})
                src = data.get("source")
            except Exception as e:
                if is_authentication_error(e):
                    raise
                self.append_sources_manifest(f"WARN video source {lookup_id}: {e}")
                return None

        if not src:
            ref = lookup_id or post_id or "unknown"
            self.append_sources_manifest(f"WARN video source {ref}: missing download source")
            return None

        try:
            local = self._download_stream(src, "videos", expected_kind="video")
        except Exception as e:
            ref = lookup_id or post_id or "unknown"
            self.append_sources_manifest(f"WARN video source {ref}: {e}")
            return None

        if local:
            local_rel = self._manifest_relpath(local)
            ref = video_id or post_id or "unknown"
            self.append_sources_manifest(f"VIDEO {ref} {local_rel} <- {src}")
            return local
        return None

    def download_photo(self, photo: Dict, album_id: str = "") -> Optional[Tuple[str, str]]:
        """Lädt das Foto in höchster Qualität herunter. Gibt (local_path, source_url) zurück."""
        # Versuche die beste Auflösung aus images-Array zu finden
        images = photo.get("images", [])
        if images:
            # Sortiere nach Breite (höchste zuerst)
            sorted_images = sorted(images, key=lambda x: x.get("width", 0), reverse=True)
            src = sorted_images[0].get("source")
        else:
            # Fallback auf picture
            src = photo.get("picture")

        if not src:
            return None

        try:
            local = self._download_stream(src, "images", expected_kind="image")
            if local:
                photo_id = photo.get("id", "unknown")
                local_rel = self._manifest_relpath(local)
                self.append_sources_manifest(f"PHOTO {album_id}/{photo_id} {local_rel} <- {src}")
                return (local, src)
        except Exception as e:
            self.append_sources_manifest(f"WARN photo download failed: {src} - {e}")
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
        if "webp" in ct:
            return ".webp"
        if "mp4" in ct:
            return ".mp4"
        if "webm" in ct:
            return ".webm"
        return ""

    def write_readme(self):
        pi = self.page_info
        now = datetime.now(timezone.utc).isoformat() + "Z"
        txt = f"""
Facebook Archiv – erzeugt mit fb_archiver {TOOL_VERSION}
Seite: {pi.name} (ID: {pi.id})
Link: {pi.link}

Erstellt (UTC): {now}
Abfragefenster: since={self.since or '-'} until={self.until or '-'}
Token-Hinweis: Page Access Token (nicht abgelegt)

Inhalte:
- data/posts.jsonl, data/comments.jsonl — maschinelle Rohdaten der Chronik
- data/posts.csv, data/comments.csv — Übersicht (comments.csv inkl. depth/parent_id)
- data/reactions.jsonl, data/reactions.csv — Detaillierte Reaktionen (WER hat WIE reagiert)
- data/conversations.jsonl, data/messages.jsonl — Inbox-Rohdaten (falls Rechte vorhanden)
- data/conversations.csv, data/messages.csv — Übersicht der Konversationen und Nachrichten
- data/albums.jsonl, data/albums.csv — Alben der Seite
- data/photos.jsonl, data/photos.csv — Fotos aus Alben (inkl. Metadaten)
- data/events.jsonl, data/events.csv — Events/Veranstaltungen der Seite
- data/live_videos.jsonl, data/live_videos.csv — Live Videos der Seite
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
            f.write(redact_access_tokens(line).rstrip("\n") + "\n")

    def _manifest_relpath(self, file_path: Optional[str]) -> Optional[str]:
        if not file_path:
            return file_path
        try:
            rel = os.path.relpath(file_path, self.outdir)
        except Exception:
            rel = file_path
        return rel.replace(os.sep, "/")

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
        with open(self.path("manifests", "sources.txt"), "w", encoding="utf-8"):
            pass
        self.append_sources_manifest(f"TOOL_VERSION={TOOL_VERSION}")
        self.append_sources_manifest(f"GRAPH_BASE={self.graph_base}")
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
        reactions_jsonl = open(self.path("data", "reactions.jsonl"), "w", encoding="utf-8")
        post_records: "OrderedDict[str, Dict]" = OrderedDict()
        comment_records: "OrderedDict[str, Dict]" = OrderedDict()
        reaction_records: "OrderedDict[str, Dict]" = OrderedDict()
        posts_rows_map: "OrderedDict[str, Dict]" = OrderedDict()
        comments_rows_map: "OrderedDict[str, Dict]" = OrderedDict()
        reactions_rows_map: "OrderedDict[str, Dict]" = OrderedDict()

        def normalize_str(value: Optional[str]) -> Optional[str]:
            if value is None:
                return None
            s = str(value).strip()
            return s or None

        def sanitize_thread_id(value: Optional[str]) -> Optional[str]:
            return normalize_str(value[2:]) if (isinstance(value, str) and value.startswith('t_')) else normalize_str(value)

        def store_record(
            store: "OrderedDict[str, Dict]",
            key: Optional[str],
            candidate: Optional[Dict],
            prefer=prefer_latest_record,
            fallback_prefix: str = "__missing__",
        ) -> None:
            if not candidate:
                return
            key_norm = normalize_str(key)
            if key_norm is None:
                key_norm = f"{fallback_prefix}:{len(store)}"
            existing = store.get(key_norm)
            if existing is None:
                store[key_norm] = candidate
            else:
                chosen = prefer(existing, candidate)
                store[key_norm] = chosen if chosen is not None else existing

        def has_meaningful_value(value) -> bool:
            if value is None:
                return False
            if isinstance(value, str):
                return bool(value.strip())
            if isinstance(value, (list, tuple, set, dict)):
                return len(value) > 0
            return True

        def message_dedupe_key(conv_identifier: str, msg: Dict) -> Optional[str]:
            primary = sanitize_thread_id(
                msg.get("id")
                or msg.get("message_id")
                or msg.get("mid")
                or msg.get("messageId")
            )
            if primary:
                return f"id:{conv_identifier}:{primary}"
            created = normalize_str(iso8601(msg.get("created_time")))
            from_name = normalize_str(((msg.get("from") or {}).get("name")))
            snippet = normalize_str((msg.get("message") or "")[:160])
            if not any((created, from_name, snippet)):
                return None
            return f"fallback:{conv_identifier}:{created}:{from_name}:{snippet}"

        def merge_message_payload(existing: Dict, incoming: Dict) -> Dict:
            if not existing:
                return incoming
            if not incoming:
                return existing
            incoming_message = incoming.get("message")
            if isinstance(incoming_message, str):
                existing_message = existing.get("message")
                if not isinstance(existing_message, str) or len(incoming_message) > len(existing_message):
                    existing["message"] = incoming_message
            for key in ("conversation_link", "mailbox_id", "thread_type", "selected_item_id", "platform"):
                if not has_meaningful_value(existing.get(key)) and has_meaningful_value(incoming.get(key)):
                    existing[key] = incoming[key]
            if not has_meaningful_value(existing.get("from")) and has_meaningful_value(incoming.get("from")):
                existing["from"] = incoming["from"]
            if not has_meaningful_value(existing.get("to")) and has_meaningful_value(incoming.get("to")):
                existing["to"] = incoming["to"]
            if has_meaningful_value(incoming.get("attachments")):
                if has_meaningful_value(existing.get("attachments")):
                    if isinstance(existing["attachments"], list) and isinstance(incoming["attachments"], list):
                        existing["attachments"].extend(
                            att for att in incoming["attachments"] if att not in existing["attachments"]
                        )
                    else:
                        existing["attachments"] = incoming["attachments"]
                else:
                    existing["attachments"] = incoming["attachments"]
            return existing

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

            post_row = {
                "post_id": pid,
                "created_time": created,
                "updated_time": updated,
                "permalink_url": perma,
                "message": msg,
                "reactions_total": reacts,
                "shares_count": shares,
            }
            details = self.get_post_details(pid)
            post.update(details)
            post_row["shares_count"] = (post.get("shares") or {}).get("count")
            store_record(post_records, pid, post)
            store_record(posts_rows_map, pid, post_row)

            # Kommentare (rekursiv inkl. Replies)
            try:
                for c in self.get_comments_for_post(pid, depth=0, parent=None):
                    c["post_id"] = pid
                    if "root_post_id" not in c:
                        c["root_post_id"] = pid
                    if c.get("depth", 0) > 0 and c.get("parent_id"):
                        c.setdefault("parent_comment_id", c.get("parent_id"))
                    comment_row = {
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
                    store_record(comment_records, c.get("id"), c, fallback_prefix="comment")
                    store_record(comments_rows_map, c.get("id"), comment_row, fallback_prefix="comment_row")
            except Exception as e:
                if is_authentication_error(e):
                    raise
                self.append_sources_manifest(f"WARN comments for {pid}: {e}")

            # Reaktions-Details holen (WER hat WIE reagiert)
            try:
                for reaction in self.get_reactions_for_post(pid):
                    reaction["post_id"] = pid
                    reaction_key = f"{pid}:{reaction.get('id') or ''}:{reaction.get('type') or ''}"
                    reaction_row = {
                        "post_id": pid,
                        "user_id": reaction.get("id"),
                        "user_name": reaction.get("name"),
                        "type": reaction.get("type"),  # LIKE, LOVE, WOW, HAHA, SAD, ANGRY, THANKFUL
                    }
                    store_record(reaction_records, reaction_key, reaction, fallback_prefix="reaction")
                    store_record(reactions_rows_map, reaction_key, reaction_row, fallback_prefix="reaction_row")
            except Exception as e:
                if is_authentication_error(e):
                    raise
                self.append_sources_manifest(f"WARN reactions for {pid}: {e}")

            # Medien (best effort)
            if self.media:
                saved = self.download_media_from_post(post)
                for local, src in saved:
                    local_rel = self._manifest_relpath(local)
                    self.append_sources_manifest(f"MEDIA {pid} {local_rel} <- {src}")

        for record in post_records.values():
            posts_jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        posts_jsonl.close()

        for record in comment_records.values():
            comments_jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        comments_jsonl.close()

        for record in reaction_records.values():
            reactions_jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        reactions_jsonl.close()

        posts_rows = list(posts_rows_map.values())
        comments_rows = list(comments_rows_map.values())
        reactions_rows = list(reactions_rows_map.values())
        if reactions_rows:
            write_csv_rows(self.path("data", "reactions.csv"), reactions_rows)
    
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
                conv_link = conv.get("link")
                mailbox_id = normalize_str(conv.get("mailbox_id")) or self.page_info.id
                thread_type = normalize_str(conv.get("thread_type")) or "FB_MESSAGE"
                selected_item_id = sanitize_thread_id(conv.get("selected_item_id"))

                def update_meta_from_link(link_value: Optional[str]) -> None:
                    nonlocal mailbox_id, thread_type, selected_item_id
                    if not link_value:
                        return
                    try:
                        parsed = urlparse(link_value)
                        query = parse_qs(parsed.query)
                        mailbox_candidate = normalize_str((query.get("mailbox_id") or [None])[0])
                        thread_candidate = normalize_str((query.get("thread_type") or [None])[0])
                        selected_candidate = sanitize_thread_id((query.get("selected_item_id") or [None])[0])
                        if mailbox_candidate:
                            mailbox_id = mailbox_candidate
                        if thread_candidate:
                            thread_type = thread_candidate
                        if selected_candidate:
                            selected_item_id = selected_candidate
                    except Exception:
                        return

                update_meta_from_link(conv_link)

                # Nachrichten innerhalb der Konversation verarbeiten um Metadaten zu sammeln
                conv_identifier = sanitize_thread_id(conv_id) or normalize_str(conv_id) or str(conv_id or "")
                messages_map: Dict[str, Dict] = {}
                auto_counter = 0
                for msg in self.get_messages(conv_id):
                    msg.setdefault("conversation_id", conv_id)
                    if conv_link:
                        msg.setdefault("conversation_link", conv_link)

                    update_meta_from_link(msg.get("conversation_link"))

                    if not mailbox_id:
                        mailbox_id = normalize_str(msg.get("mailbox_id"))
                    if not thread_type:
                        thread_type = normalize_str(msg.get("thread_type"))
                    if not selected_item_id:
                        selected_item_id = sanitize_thread_id(msg.get("selected_item_id"))

                    if mailbox_id:
                        msg.setdefault("mailbox_id", mailbox_id)
                    if thread_type:
                        msg.setdefault("thread_type", thread_type)
                    if selected_item_id:
                        msg.setdefault("selected_item_id", selected_item_id)

                    if not self._is_within_requested_range(msg.get("created_time")):
                        continue

                    key = message_dedupe_key(conv_identifier, msg)
                    if not key:
                        key = f"auto:{conv_identifier}:{auto_counter}"
                        auto_counter += 1
                    existing_msg = messages_map.get(key)
                    if existing_msg:
                        messages_map[key] = merge_message_payload(existing_msg, msg)
                    else:
                        messages_map[key] = msg

                messages_to_write = list(messages_map.values())
                messages_to_write.sort(
                    key=lambda m: (parse_to_utc(m.get("created_time")) or datetime.min.replace(tzinfo=timezone.utc))
                )

                conversation_in_range = self._is_within_requested_range(conv.get("updated_time"))
                if not conversation_in_range and not messages_to_write:
                    continue

                # Extrahierte Metadaten zurück in Conversation-Objekt schreiben
                if mailbox_id:
                    conv.setdefault("mailbox_id", mailbox_id)
                if thread_type:
                    conv.setdefault("thread_type", thread_type)
                if selected_item_id:
                    conv.setdefault("selected_item_id", selected_item_id)

                # JETZT erst das Conversation-Objekt mit aktualisierten Metadaten schreiben
                conv_jsonl.write(json.dumps(conv, ensure_ascii=False) + "\n")

                conv_row = {
                    "conversation_id": conv_id,
                    "updated_time": iso8601(conv.get("updated_time")),
                    "link": conv_link,
                    "mailbox_id": mailbox_id,
                    "thread_type": thread_type,
                    "selected_item_id": selected_item_id,
                    "participants": ", ".join(
                        [
                            p.get("name")
                            for p in (conv.get("participants", {}).get("data", []))
                        ]
                    ),
                }
                conv_rows.append(conv_row)

                # Messages mit aktualisierten Metadaten schreiben
                for msg in messages_to_write:
                    created_iso = iso8601(msg.get("created_time"))
                    if created_iso:
                        msg["created_time"] = created_iso
                    msg_jsonl.write(json.dumps(msg, ensure_ascii=False) + "\n")
                    msg_rows.append(
                        {
                            "conversation_id": conv_id,
                            "message_id": msg.get("id"),
                            "created_time": created_iso,
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
                            "conversation_link": msg.get("conversation_link"),
                            "mailbox_id": msg.get("mailbox_id") or mailbox_id,
                            "thread_type": msg.get("thread_type") or thread_type,
                            "selected_item_id": msg.get("selected_item_id") or selected_item_id,
                            "platform": msg.get("platform"),
                            "message": (msg.get("message") or "")
                            .replace("\n", " ")
                            .strip(),
                        }
                    )
        except Exception as e:
            if is_authentication_error(e):
                raise
            self.append_sources_manifest(f"WARN conversations/messages: {e}")

        conv_jsonl.close()
        msg_jsonl.close()

        if conv_rows:
            write_csv_rows(self.path("data", "conversations.csv"), conv_rows)
        if msg_rows:
            write_csv_rows(self.path("data", "messages.csv"), msg_rows)

        # Alben und Fotos archivieren
        albums_jsonl = open(self.path("data", "albums.jsonl"), "w", encoding="utf-8")
        photos_jsonl = open(self.path("data", "photos.jsonl"), "w", encoding="utf-8")
        album_records: "OrderedDict[str, Dict]" = OrderedDict()
        photo_records: "OrderedDict[str, Dict]" = OrderedDict()
        albums_rows_map: "OrderedDict[str, Dict]" = OrderedDict()
        photos_rows_map: "OrderedDict[str, Dict]" = OrderedDict()

        try:
            for album in self.get_albums():
                album_id = album.get("id")
                album_name = album.get("name", "")

                album_in_range = (
                    self._is_within_requested_range(album.get("created_time"))
                    or self._is_within_requested_range(album.get("updated_time"))
                )
                if not album_in_range:
                    continue

                album_row = {
                    "album_id": album_id,
                    "name": album_name,
                    "description": (album.get("description") or "").replace("\n", " ").strip(),
                    "created_time": iso8601(album.get("created_time")),
                    "updated_time": iso8601(album.get("updated_time")),
                    "link": album.get("link"),
                    "photo_count": album.get("count", 0),
                    "type": album.get("type", "")
                }
                store_record(album_records, album_id, album, fallback_prefix="album")
                store_record(albums_rows_map, album_id, album_row, fallback_prefix="album_row")

                # Fotos des Albums holen
                try:
                    for photo in self.get_photos_from_album(album_id):
                        photo["album_id"] = album_id
                        photo["album_name"] = album_name

                        photo_in_range = (
                            self._is_within_requested_range(photo.get("created_time"))
                            or self._is_within_requested_range(photo.get("updated_time"))
                        )
                        if not photo_in_range:
                            continue

                        photo_row = {
                            "album_id": album_id,
                            "album_name": album_name,
                            "photo_id": photo.get("id"),
                            "created_time": iso8601(photo.get("created_time")),
                            "name": (photo.get("name") or "").replace("\n", " ").strip(),
                            "alt_text": (photo.get("alt_text") or "").replace("\n", " ").strip(),
                            "link": photo.get("link"),
                            "width": photo.get("width"),
                            "height": photo.get("height")
                        }

                        # Foto herunterladen
                        if self.media:
                            result = self.download_photo(photo, album_id)
                            if result:
                                local, src = result
                                photo_row["local_path"] = local

                        store_record(photo_records, photo.get("id"), photo, fallback_prefix="photo")
                        store_record(photos_rows_map, photo.get("id"), photo_row, fallback_prefix="photo_row")

                except Exception as e:
                    if is_authentication_error(e):
                        raise
                    self.append_sources_manifest(f"WARN photos for album {album_id}: {e}")

        except Exception as e:
            if is_authentication_error(e):
                raise
            self.append_sources_manifest(f"WARN albums: {e}")

        for record in album_records.values():
            albums_jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        albums_jsonl.close()

        for record in photo_records.values():
            photos_jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        photos_jsonl.close()

        albums_rows = list(albums_rows_map.values())
        if albums_rows:
            write_csv_rows(self.path("data", "albums.csv"), albums_rows)
        photos_rows = list(photos_rows_map.values())
        if photos_rows:
            write_csv_rows(self.path("data", "photos.csv"), photos_rows)

        # Events archivieren
        events_jsonl = open(self.path("data", "events.jsonl"), "w", encoding="utf-8")
        event_records: "OrderedDict[str, Dict]" = OrderedDict()
        events_rows_map: "OrderedDict[str, Dict]" = OrderedDict()

        try:
            for event in self.get_events():
                event_id = event.get("id")

                event_in_range = (
                    self._is_within_requested_range(event.get("start_time"))
                    or self._is_within_requested_range(event.get("end_time"))
                    or self._is_within_requested_range(event.get("updated_time"))
                )
                if not event_in_range:
                    continue

                # Location extrahieren
                place = event.get("place", {})
                location_name = place.get("name", "") if isinstance(place, dict) else ""

                event_row = {
                    "event_id": event_id,
                    "name": event.get("name", ""),
                    "description": (event.get("description") or "").replace("\n", " ").strip(),
                    "start_time": iso8601(event.get("start_time")),
                    "end_time": iso8601(event.get("end_time")),
                    "updated_time": iso8601(event.get("updated_time")),
                    "location": location_name,
                    "attending_count": event.get("attending_count", 0),
                    "interested_count": event.get("interested_count", 0),
                    "declined_count": event.get("declined_count", 0),
                    "category": event.get("category", ""),
                    "is_canceled": event.get("is_canceled", False),
                    "is_online": event.get("is_online", False),
                    "ticket_uri": event.get("ticket_uri", "")
                }
                store_record(event_records, event_id, event, fallback_prefix="event")
                store_record(events_rows_map, event_id, event_row, fallback_prefix="event_row")

        except Exception as e:
            if is_authentication_error(e):
                raise
            self.append_sources_manifest(f"WARN events: {e}")

        for record in event_records.values():
            events_jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        events_jsonl.close()

        events_rows = list(events_rows_map.values())
        if events_rows:
            write_csv_rows(self.path("data", "events.csv"), events_rows)

        # Live Videos archivieren
        live_videos_jsonl = open(self.path("data", "live_videos.jsonl"), "w", encoding="utf-8")
        live_video_records: "OrderedDict[str, Dict]" = OrderedDict()
        live_videos_rows_map: "OrderedDict[str, Dict]" = OrderedDict()

        try:
            for video in self.get_live_videos():
                video_id = video.get("id")

                video_in_range = (
                    self._is_within_requested_range(video.get("created_time"))
                    or self._is_within_requested_range(video.get("broadcast_start_time"))
                    or self._is_within_requested_range(video.get("updated_time"))
                )
                if not video_in_range:
                    continue

                # Video-Objekt extrahieren
                video_data = video.get("video", {}) if isinstance(video.get("video"), dict) else {}

                live_video_row = {
                    "video_id": video_id,
                    "title": video.get("title", ""),
                    "description": (video.get("description") or "").replace("\n", " ").strip(),
                    "created_time": iso8601(video.get("created_time")),
                    "broadcast_start_time": iso8601(video.get("broadcast_start_time")),
                    "status": video.get("status", ""),
                    "live_views": video.get("live_views", 0),
                    "total_views": video.get("total_views", 0),
                    "permalink_url": video.get("permalink_url", ""),
                    "video_source": video_data.get("source", "") if video_data else ""
                }
                store_record(live_video_records, video_id, video, fallback_prefix="live_video")
                store_record(live_videos_rows_map, video_id, live_video_row, fallback_prefix="live_video_row")

        except Exception as e:
            if is_authentication_error(e):
                raise
            self.append_sources_manifest(f"WARN live_videos: {e}")

        for record in live_video_records.values():
            live_videos_jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        live_videos_jsonl.close()

        live_videos_rows = list(live_videos_rows_map.values())
        if live_videos_rows:
            write_csv_rows(self.path("data", "live_videos.csv"), live_videos_rows)

        # CSV schreiben
        if posts_rows:
            write_csv_rows(self.path("data", "posts.csv"), posts_rows)
        if comments_rows:
            write_csv_rows(self.path("data", "comments.csv"), comments_rows)

        # Checksums
        self.write_checksums()

        print(f"Fertig. Archiv unter: {self.outdir}")
    
    
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Facebook Page Archiv (Graph API)")
    ap.add_argument("--version", action="version", version=f"fb_archiver {TOOL_VERSION}")
    ap.add_argument(
        "--page", required=True, help="Seitenname, ID oder URL (z.B. https://www.facebook.com/StadtMannheim)"
    )
    ap.add_argument(
        "--access-token",
        default=os.getenv("FB_PAGE_TOKEN"),
        help="Page Access Token; alternativ aus FB_PAGE_TOKEN",
    )
    ap.add_argument("--out", default="./fb_archive_out", help="Ausgabeverzeichnis")
    ap.add_argument("--since", type=parse_cli_date, help="ab Datum (YYYY-MM-DD)")
    ap.add_argument("--until", type=parse_cli_date, help="bis einschließlich Datum (YYYY-MM-DD)")
    ap.add_argument(
        "--graph-api-version",
        default=DEFAULT_GRAPH_API_VERSION,
        help=(
            "Graph API-Version "
            f"(Standard: {DEFAULT_GRAPH_API_VERSION}; alternativ per FB_GRAPH_API_VERSION)"
        ),
    )
    ap.add_argument(
        "--no-media", action="store_true", help="keine Medien herunterladen"
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="bestehende, nicht leere Jahresordner vor dem Export ersetzen",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=100,
        help="API-Seitenlimit pro Anfrage (Standard 100)",
    )
    args = ap.parse_args(argv)

    if not args.access_token:
        ap.error("Access Token fehlt: --access-token angeben oder FB_PAGE_TOKEN setzen.")
    if args.since and args.until and args.since > args.until:
        ap.error("--since darf nicht nach --until liegen.")

    # Falls eine vollständige URL übergeben wurde -> Seitennamen extrahieren
    if args.page.startswith("http://") or args.page.startswith("https://"):
        m = re.search(r"facebook\.com/([^/?#]+)", args.page)
        if m:
            print(f"[INFO] Extrahiere Seitennamen aus URL: {m.group(1)}")
            args.page = m.group(1)

    return args

def run_split_by_years(args) -> bool:
    base_arch = FacebookArchiver(
        page=args.page,
        access_token=args.access_token,
        outdir=args.out,
        since=args.since,
        until=args.until,
        media=not args.no_media,
        limit=args.limit,
        graph_api_version=args.graph_api_version,
        prepare_output_dirs=False,
    )
    page_info = base_arch.get_page_info()

    # Bestimme Start- und Endjahr unter Berücksichtigung der User-Parameter
    if args.since:
        # User hat --since angegeben: verwende dieses Jahr als Start
        start_year = int(args.since.split("-")[0])
    else:
        # Kein --since: Ermittle ältesten Post
        first_date = detect_first_post_date(
            args.page, args.access_token, api_version=args.graph_api_version
        )
        start_year = int(first_date.split("-")[0])

    if args.until:
        # User hat --until angegeben: verwende dieses Jahr als Ende
        end_year = int(args.until.split("-")[0])
    else:
        # Kein --until: verwende aktuelles Jahr
        end_year = datetime.now(timezone.utc).year

    print(f"[INFO] Archivierung von {start_year} bis {end_year} für {page_info.name}")

    failures = 0
    for year in range(start_year, end_year + 1):
        # Bestimme Jahresgrenzen, aber respektiere User-Parameter
        year_since = f"{year}-01-01"
        year_until = f"{year}-12-31"

        # Falls User spezifische Grenzen gesetzt hat, diese innerhalb des Jahres anwenden
        if args.since and year == start_year:
            year_since = args.since
        if args.until and year == end_year:
            year_until = args.until

        year_path = Path(args.out) / str(year)
        year_out = str(year_path)
        print(f"[INFO] -> Jahr {year} ({year_since} bis {year_until})")
        try:
            prepare_output_directory(year_path, overwrite=args.overwrite)
            arch = FacebookArchiver(
                page=args.page,
                access_token=args.access_token,
                outdir=year_out,
                since=year_since,
                until=year_until,
                media=not args.no_media,
                limit=args.limit,
                graph_api_version=args.graph_api_version,
            )
            arch.run()
        except Exception as e:
            failures += 1
            print(f"[ERROR] Fehler bei Jahr {year}: {e}", file=sys.stderr)
            if is_authentication_error(e):
                print(
                    "[ERROR] Authentifizierung ist ungültig; weitere Jahre werden nicht versucht.",
                    file=sys.stderr,
                )
                break

    return failures == 0


if __name__ == "__main__":
    cli_args = parse_args()
    try:
        success = run_split_by_years(cli_args)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(0 if success else 1)
