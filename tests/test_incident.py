import json

from incident import Fix, Rules, State, step

HOME = (38.0406, -84.5037)
RULES = Rules(parent_name="Dad", residence_name="Sunrise", residence_lat=HOME[0],
              residence_lon=HOME[1], radius_m=150, sms_repeat_s=300,
              call_cooldown_s=1800, failure_alert_s=1800, stale_fix_alert_s=14400)
T0 = 1_800_000_000.0


def home_fix(ts, acc=None):
    return Fix(HOME[0] + 0.0002, HOME[1], ts, acc)          # ~22 m away


def away_fix(ts, acc=None):
    return Fix(HOME[0] + 0.01, HOME[1], ts, acc)            # ~1.1 km away


def kinds(actions):
    return [a.kind for a in actions]


def test_home_fix_sends_nothing():
    s = State()
    assert step(s, RULES, T0, fix=home_fix(T0)) == []
    assert s.incident_started is None


def test_leaving_sends_sms_and_calls_front_desk():
    s = State()
    step(s, RULES, T0, fix=home_fix(T0))
    acts = step(s, RULES, T0 + 300, fix=away_fix(T0 + 290))
    assert kinds(acts) == ["away_sms", "call"]
    assert acts[0].target == "caregiver" and acts[1].target == "front_desk"
    assert acts[0].text.startswith("ALERT") and "Calling front desk now" in acts[0].text
    assert "maps.google.com" in acts[0].text
    assert "north" in acts[1].text


def test_repeat_sms_every_interval_and_call_cooldown():
    s = State()
    step(s, RULES, T0, fix=away_fix(T0))
    sent = []
    for i in range(1, 7):                                   # 6 more cycles, 5 min apart (+ jitter)
        now = T0 + i * 300 - 3
        sent.append(kinds(step(s, RULES, now, fix=away_fix(now - 60))))
    assert all("away_sms" in k for k in sent)               # SMS each cycle despite jitter
    assert [k for k in sent if "call" in k] == [["away_sms", "call"]]   # second call at 30 min
    assert sent[5] == ["away_sms", "call"]
    assert s.incident_call_count == 2


def test_repeat_sms_continues_without_new_fix():
    s = State()
    step(s, RULES, T0, fix=away_fix(T0))
    acts = step(s, RULES, T0 + 300, fix=away_fix(T0))       # same fix again
    assert kinds(acts) == ["away_sms"]
    assert "UPDATE" in acts[0].text and "Front desk called 1x" in acts[0].text


def test_return_home_clears_incident():
    s = State()
    step(s, RULES, T0, fix=away_fix(T0))
    acts = step(s, RULES, T0 + 300, fix=home_fix(T0 + 280))
    assert kinds(acts) == ["returned_sms"]
    assert s.incident_started is None
    # leaving again starts a fresh incident with a fresh call
    acts = step(s, RULES, T0 + 600, fix=away_fix(T0 + 590))
    assert kinds(acts) == ["away_sms", "call"]


def test_old_or_duplicate_fix_does_not_change_zone():
    s = State()
    step(s, RULES, T0, fix=home_fix(T0))
    assert step(s, RULES, T0 + 300, fix=away_fix(T0 - 100)) == []   # older than last fix
    assert s.away_streak == 0


def test_accuracy_circle_overlapping_geofence_is_not_away():
    s = State()
    # ~1.1 km away but 1.2 km uncertainty: could still be inside
    assert step(s, RULES, T0, fix=away_fix(T0, acc=1200)) == []
    assert s.incident_started is None


def test_confirmation_requires_multiple_fixes():
    rules = Rules(**{**RULES.__dict__, "away_confirm_fixes": 2})
    s = State()
    assert step(s, rules, T0, fix=away_fix(T0)) == []
    assert kinds(step(s, rules, T0 + 300, fix=away_fix(T0 + 200))) == ["away_sms", "call"]


def test_call_disabled():
    rules = Rules(**{**RULES.__dict__, "call_enabled": False})
    s = State()
    acts = step(s, rules, T0, fix=away_fix(T0))
    assert kinds(acts) == ["away_sms"]
    assert "Calling" not in acts[0].text


def test_failure_alert_once_then_recovery():
    s = State()
    step(s, RULES, T0, fix=home_fix(T0))
    out = []
    for i in range(1, 9):
        out += kinds(step(s, RULES, T0 + i * 300, error="HTTP 500"))
    assert out == ["failure_alert"]                         # fires at 30 min, only once
    assert kinds(step(s, RULES, T0 + 3000, fix=home_fix(T0 + 2900))) == ["failure_recovered"]


def test_stale_alert_once_then_recovery():
    s = State()
    step(s, RULES, T0, fix=home_fix(T0))
    out = []
    for i in range(1, 60):
        out += kinds(step(s, RULES, T0 + i * 300, fix=home_fix(T0)))
    assert out == ["stale_alert"]
    assert kinds(step(s, RULES, T0 + 18000, fix=home_fix(T0 + 17900))) == ["stale_recovered"]


def test_stale_alert_when_never_received_a_fix():
    s = State()
    step(s, RULES, T0)
    assert kinds(step(s, RULES, T0 + 14400)) == ["stale_alert"]


def test_pause_suppresses_then_alerts_after_expiry():
    s = State(paused_until=T0 + 3600)
    assert step(s, RULES, T0, fix=away_fix(T0)) == []
    assert step(s, RULES, T0 + 300, fix=away_fix(T0 + 200)) == []
    acts = step(s, RULES, T0 + 3600, fix=away_fix(T0 + 200))
    assert kinds(acts) == ["away_sms", "call"]


def test_failed_send_is_retried_next_cycle():
    s = State()
    acts = step(s, RULES, T0, fix=away_fix(T0))
    for a in acts:
        s.on_send_failed(a)
    acts = step(s, RULES, T0 + 300, fix=away_fix(T0))
    assert kinds(acts) == ["away_sms", "call"]
    assert acts[0].text.startswith("ALERT")                # still the first alert


def test_state_roundtrip_preserves_incident():
    s = State()
    step(s, RULES, T0, fix=away_fix(T0))
    restored = State.from_dict(json.loads(json.dumps(s.to_dict())))
    assert restored == s
    # a restart 5 min later must not re-call the front desk
    assert kinds(step(restored, RULES, T0 + 300, fix=away_fix(T0))) == ["away_sms"]


def test_caregiver_called_once_per_incident():
    rules = Rules(**{**RULES.__dict__, "caregiver_call_on_alert": True})
    s = State()
    acts = step(s, rules, T0, fix=away_fix(T0))
    assert kinds(acts) == ["away_sms", "caregiver_call", "call"]
    assert [a.target for a in acts if a.channel == "call"] == ["caregiver", "front_desk"]
    for i in range(1, 7):
        assert "caregiver_call" not in kinds(step(s, rules, T0 + i * 300, fix=away_fix(T0)))
    step(s, rules, T0 + 2400, fix=home_fix(T0 + 2300))       # back home
    assert "caregiver_call" in kinds(step(s, rules, T0 + 2700, fix=away_fix(T0 + 2600)))


def test_maps_link_has_no_bare_comma():
    # ntfy's Actions header is comma-separated; a bare comma breaks the push.
    from incident import _maps_link
    link = _maps_link(Fix(lat=38.041234, lon=-84.503712, ts=0, accuracy_m=None))
    assert "," not in link and "38.041234%2C-84.503712" in link
