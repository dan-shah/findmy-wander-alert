"""Location source backed by FindMy.py: downloads the card's encrypted Find My
reports from Apple and decrypts them locally with the exported accessory key."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

from incident import Fix


class FindMySourceError(RuntimeError):
    pass


def newest_fix(reports: Iterable) -> Fix | None:
    """Convert FindMy.py LocationReports to the newest Fix (by report time)."""
    best: Fix | None = None
    for r in reports:
        try:
            fix = Fix(lat=float(r.latitude), lon=float(r.longitude),
                      ts=r.timestamp.timestamp(),
                      accuracy_m=float(r.horizontal_accuracy) or None)
        except RuntimeError:  # report not decryptable with this key
            continue
        if fix.lat == 0 and fix.lon == 0:
            continue
        if best is None or fix.ts > best.ts:
            best = fix
    return best


class FindMySource:
    def __init__(self, key_file: Path, account_file: Path, anisette_libs: Path):
        self.key_file, self.account_file, self.anisette_libs = key_file, account_file, anisette_libs
        self._account = None
        self._accessory = None

    def _load(self):
        from findmy import FindMyAccessory

        from native_anisette import native_apple_account

        if not self.key_file.exists():
            raise FindMySourceError(f"key file not found: {self.key_file}")
        if not self.account_file.exists():
            raise FindMySourceError(
                f"no Apple session at {self.account_file}; run .venv/bin/python findmy_login.py")
        if self._account is None:
            self._account = native_apple_account(json.loads(self.account_file.read_text()))
        if self._accessory is None:
            self._accessory = FindMyAccessory.from_json(self.key_file)
        return self._account, self._accessory

    def _save_account(self) -> None:
        # Persist refreshed tokens/anisette state so sessions survive restarts.
        tmp = self.account_file.with_suffix(".tmp")
        self._account.to_json(tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.account_file)

    def fetch_raw(self) -> list[dict]:
        account, accessory = self._load()
        reports = account.fetch_location_history(accessory)
        self._save_account()
        out = []
        for r in sorted(reports, key=lambda r: r.timestamp):
            try:
                out.append({"timestamp": r.timestamp.isoformat(), "latitude": r.latitude,
                            "longitude": r.longitude, "accuracy_m": r.horizontal_accuracy,
                            "confidence": r.confidence})
            except RuntimeError:
                out.append({"timestamp": r.timestamp.isoformat(), "decrypted": False})
        return out

    def latest_fix(self) -> Fix | None:
        account, accessory = self._load()
        try:
            reports = account.fetch_location_history(accessory)
        except Exception:
            # Drop the cached session so the next cycle reloads it from disk
            # (e.g. after re-running findmy_login.py).
            self._account = None
            raise
        self._save_account()
        return newest_fix(reports)
