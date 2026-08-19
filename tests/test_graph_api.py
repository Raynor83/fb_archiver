import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fb_archiver as archiver_module
from fb_archiver import (
    FacebookArchiver,
    GraphAPIError,
    PageInfo,
    detect_first_post_date,
    redact_access_tokens,
)


def make_archiver(tmp_path):
    return FacebookArchiver(
        page="page",
        access_token="token",
        outdir=str(tmp_path),
        media=False,
    )


def response(status_code, payload, headers=None):
    result = Mock(status_code=status_code, headers=headers or {})
    result.json.return_value = payload
    result.text = str(payload)
    return result


def test_graph_get_retries_meta_rate_limit(tmp_path, monkeypatch):
    archiver = make_archiver(tmp_path)
    archiver.session.get = Mock(
        side_effect=[
            response(
                400,
                {"error": {"code": 4, "message": "Application request limit reached"}},
                {"Retry-After": "0"},
            ),
            response(200, {"data": [{"id": "1"}]}),
        ]
    )
    sleep = Mock()
    monkeypatch.setattr(archiver_module.time, "sleep", sleep)

    result = archiver.graph_get("page/posts", {"fields": "id"})

    assert result == {"data": [{"id": "1"}]}
    assert archiver.session.get.call_count == 2
    sleep.assert_called_once_with(0)


def test_graph_get_explains_expired_token_without_leaking_it(tmp_path):
    archiver = make_archiver(tmp_path)
    secret = "EAATESTSECRET"
    archiver.session.get = Mock(
        return_value=response(
            400,
            {
                "error": {
                    "code": 190,
                    "message": f"Invalid OAuth access_token={secret}&debug=1",
                }
            },
        )
    )

    with pytest.raises(RuntimeError) as exc_info:
        archiver.graph_get("page", {"fields": "id"})

    message = str(exc_info.value)
    assert isinstance(exc_info.value, GraphAPIError)
    assert exc_info.value.code == 190
    assert secret not in message
    assert "[REDACTED]" in message
    assert "ungültig oder abgelaufen" in message


def test_redact_access_tokens_handles_encoded_separator():
    value = "https://example.invalid/?access_token%3Dsecret-value&next=1"

    assert "secret-value" not in redact_access_tokens(value)


def test_detect_first_post_date_uses_current_year_for_empty_page(monkeypatch):
    monkeypatch.setattr(
        archiver_module.requests.Session,
        "get",
        Mock(return_value=response(200, {"data": []})),
    )

    result = detect_first_post_date("page", "token", "v26.0")

    assert result.startswith(str(archiver_module.datetime.now().year))


def test_detect_first_post_date_does_not_fall_back_after_api_error(monkeypatch):
    monkeypatch.setattr(
        archiver_module.requests.Session,
        "get",
        Mock(return_value=response(403, {"error": {"message": "Forbidden"}})),
    )

    with pytest.raises(RuntimeError, match="konnte nicht ermittelt werden"):
        detect_first_post_date("page", "token", "v26.0")


def test_year_split_stops_after_authentication_error(tmp_path, monkeypatch):
    created_archivers = []

    class FakeArchiver:
        def __init__(self, **kwargs):
            created_archivers.append(kwargs)

        def get_page_info(self):
            return PageInfo(id="page", name="Page", link=None)

        def run(self):
            raise GraphAPIError(
                "invalid token",
                status_code=400,
                code=190,
                subcode=467,
            )

    monkeypatch.setattr(archiver_module, "FacebookArchiver", FakeArchiver)
    args = SimpleNamespace(
        page="page",
        access_token="token",
        out=str(tmp_path),
        since="2013-01-01",
        until="2015-12-31",
        no_media=True,
        limit=100,
        graph_api_version="v26.0",
        overwrite=False,
    )

    assert archiver_module.run_split_by_years(args) is False
    assert len(created_archivers) == 2  # Vorprüfung plus erstes fehlgeschlagenes Jahr
