import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fb_archiver import (
    FacebookArchiver,
    graph_base,
    normalize_graph_api_version,
    parse_args,
    prepare_output_directory,
    to_api_until_epoch,
    to_utc_epoch,
    write_csv_rows,
)


def test_to_utc_epoch_naive_date():
    assert to_utc_epoch("2020-01-01") == 1577836800


def test_to_utc_epoch_timezone_aware():
    # 05:00 at UTC-05 corresponds to 10:00 UTC
    assert to_utc_epoch("2020-01-01T05:00:00-0500") == 1577872800


def test_normalize_graph_api_version_accepts_short_version():
    assert normalize_graph_api_version("26.0") == "v26.0"


def test_graph_base_uses_normalized_version():
    assert graph_base("26.0") == "https://graph.facebook.com/v26.0"


def test_date_only_until_includes_the_complete_day():
    assert to_api_until_epoch("2020-01-01") == 1577923200


def test_timestamp_until_is_not_extended():
    assert to_api_until_epoch("2020-01-01T12:00:00+00:00") == 1577880000


def test_archiver_can_skip_preparing_output_dirs(tmp_path):
    outdir = tmp_path / "archive_root"

    FacebookArchiver(
        page="page",
        access_token="token",
        outdir=str(outdir),
        prepare_output_dirs=False,
    )

    assert not outdir.exists()


def test_prepare_output_directory_rejects_existing_archive(tmp_path):
    year_dir = tmp_path / "2025"
    year_dir.mkdir()
    marker = year_dir / "existing.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        prepare_output_directory(year_dir)

    assert marker.read_text(encoding="utf-8") == "keep"


def test_prepare_output_directory_overwrites_only_explicitly(tmp_path):
    year_dir = tmp_path / "2025"
    year_dir.mkdir()
    (year_dir / "existing.txt").write_text("old", encoding="utf-8")

    prepare_output_directory(year_dir, overwrite=True)

    assert not year_dir.exists()


def test_parse_args_uses_token_from_environment(monkeypatch):
    monkeypatch.setenv("FB_PAGE_TOKEN", "secret-token")

    args = parse_args(
        [
            "--page",
            "https://www.facebook.com/MARCHIVUMMannheim",
            "--since",
            "2025-01-01",
            "--until",
            "2025-12-31",
        ]
    )

    assert args.page == "MARCHIVUMMannheim"
    assert args.access_token == "secret-token"


def test_parse_args_rejects_reversed_date_range(monkeypatch):
    monkeypatch.setenv("FB_PAGE_TOKEN", "secret-token")

    with pytest.raises(SystemExit):
        parse_args(
            [
                "--page",
                "168701373143130",
                "--since",
                "2025-12-31",
                "--until",
                "2025-01-01",
            ]
        )


def test_write_csv_rows_unions_columns_in_stable_order(tmp_path):
    target = tmp_path / "rows.csv"

    write_csv_rows(
        str(target),
        [
            {"a": 1, "b": 2},
            {"b": 3, "c": 4},
        ],
    )

    assert target.read_text(encoding="utf-8").splitlines() == [
        "a,b,c",
        "1,2,",
        ",3,4",
    ]
