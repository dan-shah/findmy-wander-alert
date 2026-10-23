"""Airpinpoint REST client.

Airpinpoint's public docs only show `GET /v1/trackables/{id}/locations` with a
Bearer token; the response schema is not published. Parsing is therefore
tolerant of common field names and container shapes. Run
`python monitor.py --probe` once against the real API and tighten the key lists
below if the actual names differ.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests

from incident import Fix

BASE_URL = "https://api.airpinpoint.com/v1"

LAT_KEYS = ("latitude", "lat")
LON_KEYS = ("longitude", "lon", "lng", "long")
TS_KEYS = ("timestamp", "time", "reportedAt", "reported_at", "locationTimestamp",
           "location_timestamp", "datePublished", "date", "createdAt", "created_at")
ACC_KEYS = ("horizontalAccuracy", "horizontal_accuracy", "accuracy", "accuracyMeters",
            "accuracy_m")
INACCURATE_KEYS = ("isInaccurate", "is_inaccurate", "inaccurate")
NESTED_KEYS = ("location", "coordinates", "position", "coords")
LIST_KEYS = ("locations", "data", "results", "items", "points")


class AirpinpointError(RuntimeError):
    pass


def _first(d: dict, keys: Iterable[str]) -> Any:
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def _parse_ts(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e12 else float(value)
    if isinstance(value, str):
        s = value.strip()
        if s.replace(".", "", 1).isdigit():
            return _parse_ts(float(s))
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_points(payload: Any) -> list[dict]:
    """Find the list of location dicts in an API response."""
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for k in LIST_KEYS:
            v = payload.get(k)
            if isinstance(v, list):
                return [p for p in v if isinstance(p, dict)]
            if isinstance(v, dict):
                inner = extract_points(v)
                if inner:
                    return inner
        if _first(payload, LAT_KEYS) is not None or any(
                isinstance(payload.get(k), dict) for k in NESTED_KEYS):
            return [payload]
    return []


def parse_point(p: dict) -> Fix | None:
    """Normalize one location dict; None if unusable or flagged inaccurate."""
    if any(p.get(k) is True for k in INACCURATE_KEYS):
        return None
    src = p
    if _first(p, LAT_KEYS) is None:
        for k in NESTED_KEYS:
            if isinstance(p.get(k), dict):
                src = p[k]
                break
    lat, lon = _float(_first(src, LAT_KEYS)), _float(_first(src, LON_KEYS))
    ts = _parse_ts(_first(p, TS_KEYS) if _first(p, TS_KEYS) is not None else _first(src, TS_KEYS))
    if lat is None or lon is None or ts is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    acc = _float(_first(src, ACC_KEYS))
    if acc is None and src is not p:
        acc = _float(_first(p, ACC_KEYS))
    return Fix(lat=lat, lon=lon, ts=ts, accuracy_m=acc if acc and acc > 0 else None)


def latest_fix_from_payload(payload: Any) -> Fix | None:
    """Newest valid fix by timestamp (never trusts API ordering, never falls
    back to points flagged inaccurate)."""
    points = extract_points(payload)
    fixes = [f for f in (parse_point(p) for p in points) if f]
    if fixes:
        return max(fixes, key=lambda f: f.ts)
    flagged = [p for p in points if any(p.get(k) is True for k in INACCURATE_KEYS)]
    if points and len(flagged) < len(points):
        # Data came back but nothing was readable: treat as a failure so the
        # caregiver is warned, instead of silently looking like "no updates".
        raise AirpinpointError(
            f"could not parse any of {len(points)} location(s); "
            f"first keys: {sorted(points[0].keys())[:12]} - run --probe")
    return None


class AirpinpointClient:
    def __init__(self, api_key: str, trackable_id: str, lookback_hours: float = 12,
                 timeout_s: float = 20, session: requests.Session | None = None):
        self.api_key = api_key
        self.trackable_id = trackable_id
        self.lookback_hours = lookback_hours
        self.timeout_s = timeout_s
        self.session = session or requests.Session()

    def fetch_raw(self) -> Any:
        now = datetime.now(timezone.utc)
        params = {
            "start_time": (now - timedelta(hours=self.lookback_hours)).isoformat(),
            "end_time": now.isoformat(),
        }
        url = f"{BASE_URL}/trackables/{self.trackable_id}/locations"
        try:
            resp = self.session.get(
                url, params=params, timeout=self.timeout_s,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Accept": "application/json"})
        except requests.RequestException as exc:
            raise AirpinpointError(f"network error: {exc}") from exc
        if resp.status_code != 200:
            raise AirpinpointError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise AirpinpointError(f"non-JSON response: {resp.text[:200]}") from exc

    def latest_fix(self) -> Fix | None:
        return latest_fix_from_payload(self.fetch_raw())
