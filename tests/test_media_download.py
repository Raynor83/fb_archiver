import sys
from base64 import b64decode
from pathlib import Path
from unittest.mock import Mock, call

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fb_archiver import FacebookArchiver


def make_archiver(tmp_path):
    return FacebookArchiver(
        page="page",
        access_token="token",
        outdir=str(tmp_path),
        media=True,
    )


def test_download_media_handles_subattachments(tmp_path):
    archiver = make_archiver(tmp_path)
    archiver._download_stream = Mock(side_effect=["img1", "img2"])
    post = {
        "id": "post1",
        "attachments": {
            "data": [
                {
                    "media_type": "album",
                    "subattachments": {
                        "data": [
                            {
                                "media_type": "photo",
                                "media": {"image": {"src": "http://example.com/1.jpg"}},
                            },
                            {
                                "media_type": "photo",
                                "media": {"image": {"src": "http://example.com/2.jpg"}},
                            },
                        ]
                    },
                }
            ]
        },
    }

    saved = archiver.download_media_from_post(post)

    assert saved == [
        ("img1", "http://example.com/1.jpg"),
        ("img2", "http://example.com/2.jpg"),
    ]
    archiver._download_stream.assert_has_calls(
        [
            call("http://example.com/1.jpg", "images", expected_kind="image"),
            call("http://example.com/2.jpg", "images", expected_kind="image"),
        ]
    )


def test_download_media_handles_inline_video_attachment(tmp_path):
    archiver = make_archiver(tmp_path)
    archiver.download_video_from_post = Mock(return_value="video1")
    post = {
        "id": "post1",
        "attachments": {
            "data": [
                {
                    "id": "attachment1",
                    "media_type": "video_inline",
                    "media": {"source": "http://example.com/video.mp4"},
                    "target": {"id": "video123"},
                }
            ]
        },
    }

    saved = archiver.download_media_from_post(post)

    assert saved == []
    archiver.download_video_from_post.assert_called_once_with(
        "post1", post["attachments"]["data"][0]
    )


def test_download_video_falls_back_to_video_id_lookup(tmp_path):
    archiver = make_archiver(tmp_path)
    archiver.get_post_details = Mock(return_value={"attachments": {"data": []}})
    archiver.graph_get = Mock(return_value={"source": "http://example.com/video.mp4"})
    archiver._download_stream = Mock(return_value="video1")
    archiver.append_sources_manifest = Mock()

    saved = archiver.download_video_from_post(
        "post1",
        {
            "id": "attachment1",
            "media_type": "video",
            "target": {"id": "video123"},
        },
    )

    assert saved == "video1"
    archiver.graph_get.assert_called_once_with("video123", {"fields": "source"})
    archiver._download_stream.assert_called_once_with(
        "http://example.com/video.mp4", "videos", expected_kind="video"
    )


def test_download_video_skips_external_embed(tmp_path):
    archiver = make_archiver(tmp_path)
    archiver.append_sources_manifest = Mock()
    archiver.media_session.get = Mock()

    saved = archiver.download_video_from_post(
        "post1",
        {
            "id": "attachment1",
            "media_type": "video",
            "media": {"source": "https://www.youtube.com/embed/APXVJ1FZ4G8?autoplay=1"},
            "target": {"id": "video123"},
        },
    )

    assert saved is None
    assert list((tmp_path / "media" / "videos").iterdir()) == []
    archiver.media_session.get.assert_not_called()
    archiver.append_sources_manifest.assert_called_once()
    assert "external video embed" in archiver.append_sources_manifest.call_args[0][0]


def test_download_photo_skips_placeholder_png(tmp_path):
    archiver = make_archiver(tmp_path)
    archiver.append_sources_manifest = Mock()
    placeholder_png = b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4////fwAJ+wP9KobjigAAAABJRU5ErkJggg=="
    )
    response = Mock(
        status_code=200,
        headers={"Content-Type": "image/png"},
    )
    response.iter_content = Mock(return_value=iter([placeholder_png]))
    archiver.media_session.get = Mock(return_value=response)

    saved = archiver.download_photo(
        {
            "id": "photo1",
            "picture": "https://example.com/tracker.png",
        }
    )

    assert saved is None
    assert list((tmp_path / "media" / "images").iterdir()) == []
    archiver.append_sources_manifest.assert_called_once()
    assert "placeholder image 1x1" in archiver.append_sources_manifest.call_args[0][0]


def test_media_session_does_not_carry_graph_access_token(tmp_path):
    archiver = make_archiver(tmp_path)

    assert archiver.session.params["access_token"] == "token"
    assert "access_token" not in archiver.media_session.params


def test_manifest_redacts_access_tokens(tmp_path):
    archiver = make_archiver(tmp_path)

    archiver.append_sources_manifest(
        "WARN https://example.invalid/file?access_token=secret-value&next=1"
    )

    manifest = (tmp_path / "manifests" / "sources.txt").read_text(encoding="utf-8")
    assert "secret-value" not in manifest
    assert "access_token=[REDACTED]" in manifest
