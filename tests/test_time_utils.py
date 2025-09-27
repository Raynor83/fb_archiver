import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fb_archiver import to_utc_epoch


def test_to_utc_epoch_naive_date():
    assert to_utc_epoch("2020-01-01") == 1577836800


def test_to_utc_epoch_timezone_aware():
    # 05:00 at UTC-05 corresponds to 10:00 UTC
    assert to_utc_epoch("2020-01-01T05:00:00-0500") == 1577872800
