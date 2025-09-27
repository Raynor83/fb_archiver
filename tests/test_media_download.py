import sys
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
            call("http://example.com/1.jpg", "images"),
            call("http://example.com/2.jpg", "images"),
        ]
    )
