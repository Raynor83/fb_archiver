import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fb_archiver import graph_base, normalize_graph_api_version, to_utc_epoch


def test_to_utc_epoch_naive_date():
    assert to_utc_epoch("2020-01-01") == 1577836800


def test_to_utc_epoch_timezone_aware():
    # 05:00 at UTC-05 corresponds to 10:00 UTC
    assert to_utc_epoch("2020-01-01T05:00:00-0500") == 1577872800


def test_normalize_graph_api_version_accepts_short_version():
    assert normalize_graph_api_version("25.0") == "v25.0"


def test_graph_base_uses_normalized_version():
    assert graph_base("25.0") == "https://graph.facebook.com/v25.0"
