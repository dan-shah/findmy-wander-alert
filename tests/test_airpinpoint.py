import pytest

from airpinpoint import AirpinpointError, latest_fix_from_payload, parse_point


def test_guide_shape_picks_newest_by_timestamp_not_order():
    payload = {"locations": [
        {"latitude": 38.05, "longitude": -84.5, "timestamp": "2026-10-21T15:10:00Z"},
        {"latitude": 38.04, "longitude": -84.5, "timestamp": "2026-10-21T15:00:00Z"},
    ]}
    fix = latest_fix_from_payload(payload)
    assert fix.lat == 38.05


def test_inaccurate_points_are_never_used():
    payload = {"locations": [
        {"latitude": 38.04, "longitude": -84.5, "timestamp": 1_800_000_000},
        {"latitude": 39.00, "longitude": -84.5, "timestamp": 1_800_000_900, "isInaccurate": True},
    ]}
    assert latest_fix_from_payload(payload).lat == 38.04
    assert latest_fix_from_payload({"locations": [payload["locations"][1]]}) is None


def test_alternate_shapes():
    nested = [{"location": {"lat": "38.1", "lng": "-84.4", "horizontalAccuracy": 25},
               "reportedAt": 1_800_000_000_000}]                    # epoch millis
    fix = latest_fix_from_payload({"data": nested})
    assert (fix.lat, fix.lon, fix.accuracy_m, fix.ts) == (38.1, -84.4, 25, 1_800_000_000)
    single = {"lat": 38.2, "lon": -84.3, "time": "2026-10-21T10:00:00-04:00"}
    assert latest_fix_from_payload(single).lat == 38.2


def test_empty_response_is_no_fix():
    assert latest_fix_from_payload({"locations": []}) is None
    assert latest_fix_from_payload([]) is None


def test_unparseable_points_raise_so_caregiver_is_warned():
    with pytest.raises(AirpinpointError, match="could not parse"):
        latest_fix_from_payload({"locations": [{"foo": 1, "bar": 2}]})


def test_rejects_null_island_and_bad_values():
    assert parse_point({"latitude": 0, "longitude": 0, "timestamp": 1}) is None
    assert parse_point({"latitude": 95, "longitude": 0, "timestamp": 1}) is None
    assert parse_point({"latitude": 38, "longitude": -84}) is None          # no timestamp
