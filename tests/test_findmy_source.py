from datetime import datetime, timezone

import pytest

from findmy_source import FindMySource, FindMySourceError, newest_fix


class Report:
    def __init__(self, ts, lat=38.04, lon=-84.50, acc=12, encrypted=False):
        self.timestamp = datetime.fromtimestamp(ts, tz=timezone.utc)
        self._lat, self._lon, self._acc, self._enc = lat, lon, acc, encrypted

    def _get(self, v):
        if self._enc:
            raise RuntimeError("encrypted")
        return v

    latitude = property(lambda s: s._get(s._lat))
    longitude = property(lambda s: s._get(s._lon))
    horizontal_accuracy = property(lambda s: s._get(s._acc))


def test_newest_by_timestamp_skipping_undecryptable():
    fix = newest_fix([Report(100, lat=41.0), Report(300, encrypted=True), Report(200, lat=42.0)])
    assert (fix.lat, fix.ts, fix.accuracy_m) == (42.0, 200, 12)


def test_no_reports_is_no_fix():
    assert newest_fix([]) is None
    assert newest_fix([Report(1, encrypted=True)]) is None


def test_missing_files_raise_each_cycle(tmp_path):
    src = FindMySource(tmp_path / "key.json", tmp_path / "acct.json", tmp_path / "ani.bin")
    with pytest.raises(FindMySourceError, match="key file not found"):
        src.latest_fix()
    (tmp_path / "key.json").write_text("{}")
    with pytest.raises(FindMySourceError, match="findmy_login.py"):
        src.latest_fix()
