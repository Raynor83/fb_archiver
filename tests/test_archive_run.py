import json
import sys
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fb_archiver import FacebookArchiver, PageInfo


def test_run_writes_complete_archive_structure(tmp_path):
    archiver = FacebookArchiver(
        page="168701373143130",
        access_token="token",
        outdir=str(tmp_path),
        since="2025-01-01",
        until="2025-12-31",
        media=False,
    )

    def get_page_info():
        archiver.page_info = PageInfo(
            id="168701373143130",
            name="MARCHIVUM",
            link="https://www.facebook.com/MARCHIVUMMannheim",
        )
        return archiver.page_info

    archiver.get_page_info = Mock(side_effect=get_page_info)
    archiver.iter_posts = Mock(
        return_value=iter(
            [
                {
                    "id": "page_post1",
                    "created_time": "2025-12-31T12:00:00+0000",
                    "message": "Jahresabschluss",
                    "permalink_url": "https://facebook.example/post1",
                    "reactions": {"summary": {"total_count": 1}},
                }
            ]
        )
    )
    archiver.get_post_details = Mock(return_value={"shares": {"count": 2}})
    archiver.get_comments_for_post = Mock(
        return_value=iter(
            [
                {
                    "id": "comment1",
                    "created_time": "2025-12-31T13:00:00+0000",
                    "message": "Kommentar",
                    "from": {"id": "user1", "name": "Person"},
                    "depth": 0,
                    "parent_id": None,
                }
            ]
        )
    )
    archiver.get_reactions_for_post = Mock(
        return_value=iter([{"id": "user1", "name": "Person", "type": "LIKE"}])
    )
    archiver.get_conversations = Mock(
        return_value=iter(
            [
                {
                    "id": "conversation1",
                    "updated_time": "2025-12-31T14:00:00+0000",
                    "participants": {"data": [{"name": "Person"}]},
                }
            ]
        )
    )
    archiver.get_messages = Mock(
        return_value=iter(
            [
                {
                    "id": "message1",
                    "created_time": "2025-12-31T14:00:00+0000",
                    "from": {"name": "Person"},
                    "message": "Nachricht",
                }
            ]
        )
    )
    archiver.get_albums = Mock(
        return_value=iter(
            [
                {
                    "id": "album1",
                    "name": "Album",
                    "created_time": "2025-12-31T10:00:00+0000",
                }
            ]
        )
    )
    archiver.get_photos_from_album = Mock(
        return_value=iter(
            [
                {
                    "id": "photo1",
                    "created_time": "2025-12-31T10:30:00+0000",
                    "name": "Foto",
                }
            ]
        )
    )
    archiver.get_events = Mock(
        return_value=iter(
            [
                {
                    "id": "event1",
                    "name": "Veranstaltung",
                    "start_time": "2025-12-31T18:00:00+0000",
                }
            ]
        )
    )
    archiver.get_live_videos = Mock(
        return_value=iter(
            [
                {
                    "id": "live1",
                    "title": "Livestream",
                    "created_time": "2025-12-31T19:00:00+0000",
                }
            ]
        )
    )

    archiver.run()

    expected_data_files = {
        "albums.csv",
        "albums.jsonl",
        "comments.csv",
        "comments.jsonl",
        "conversations.csv",
        "conversations.jsonl",
        "events.csv",
        "events.jsonl",
        "live_videos.csv",
        "live_videos.jsonl",
        "messages.csv",
        "messages.jsonl",
        "photos.csv",
        "photos.jsonl",
        "posts.csv",
        "posts.jsonl",
        "reactions.csv",
        "reactions.jsonl",
    }
    assert {path.name for path in (tmp_path / "data").iterdir()} == expected_data_files

    post = json.loads((tmp_path / "data" / "posts.jsonl").read_text(encoding="utf-8"))
    assert post["id"] == "page_post1"
    assert post["shares"]["count"] == 2

    sources = (tmp_path / "manifests" / "sources.txt").read_text(encoding="utf-8")
    assert "GRAPH_BASE=https://graph.facebook.com/v26.0" in sources
    assert "UNTIL=2025-12-31" in sources

    checksums = (tmp_path / "manifests" / "checksums.sha256").read_text(
        encoding="utf-8"
    )
    assert "data\\posts.jsonl" in checksums or "data/posts.jsonl" in checksums
    assert "manifests\\sources.txt" in checksums or "manifests/sources.txt" in checksums
