from pathlib import Path

import pytest

from airpinpoint import AirpinpointError
from config import Config
from monitor import StateStore, run_cycle, parse_duration
from tests.test_incident import RULES, T0, away_fix

CFG = Config(rules=RULES, check_interval_s=300, lookback_hours=12,
             caregiver_phones=("+15551234567", "+15557654321"), front_desk_phone="+15559876543",
             front_desk_send_digits="", twilio_sid="", twilio_token="",
             twilio_from="+15550001111", caregiver_methods=("push",),
             ntfy_server="https://ntfy.sh", ntfy_topic="test-topic-0123456789abcdef",
             location_source="findmy", findmy_key_file=Path("k.json"),
             findmy_account_file=Path("a.json"), findmy_anisette_libs=Path("ani.bin"),
             airpinpoint_key="k", trackable_id="t")


class FakeClient:
    def __init__(self, result):
        self.result = result

    def latest_fix(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeNotifier:
    def __init__(self, fail_kinds=()):
        self.sent, self.fail_kinds = [], set(fail_kinds)

    def send(self, action):
        if action.kind in self.fail_kinds:
            raise RuntimeError("twilio down")
        self.sent.append(action)


def store(tmp_path):
    return StateStore(tmp_path / "state.json", tmp_path / "state.lock")


def test_cycle_persists_and_survives_restart(tmp_path):
    st, n = store(tmp_path), FakeNotifier()
    run_cycle(CFG, FakeClient(away_fix(T0)), n, st, now=T0)
    assert [a.kind for a in n.sent] == ["away_sms", "call"]
    n2 = FakeNotifier()                                     # "restart": new objects, same file
    run_cycle(CFG, FakeClient(away_fix(T0)), n2, st, now=T0 + 300)
    assert [a.kind for a in n2.sent] == ["away_sms"]


def test_sms_failure_does_not_block_call_and_is_retried(tmp_path):
    st, n = store(tmp_path), FakeNotifier(fail_kinds={"away_sms"})
    run_cycle(CFG, FakeClient(away_fix(T0)), n, st, now=T0)
    assert [a.kind for a in n.sent] == ["call"]
    assert st.load().last_sms is None
    n2 = FakeNotifier()
    run_cycle(CFG, FakeClient(away_fix(T0)), n2, st, now=T0 + 60)
    assert [a.kind for a in n2.sent] == ["away_sms"]


def test_fetch_errors_counted(tmp_path):
    st, n = store(tmp_path), FakeNotifier()
    for i in range(7):
        run_cycle(CFG, FakeClient(AirpinpointError("HTTP 401")), n, st, now=T0 + i * 300)
    assert [a.kind for a in n.sent] == ["failure_alert"]


def test_parse_duration():
    assert parse_duration("3h") == 10800
    assert parse_duration("45m") == 2700


def test_push_sends_urgent_ntfy_with_map_link(monkeypatch):
    from notifier import Notifier
    from tests.test_incident import away_fix as af
    from incident import State, step

    sent = {}

    class Resp:
        def raise_for_status(self):
            pass

    def fake_post(url, data, headers, timeout):
        sent.update(url=url, data=data, headers=headers)
        return Resp()

    monkeypatch.setattr("notifier.requests.post", fake_post)
    n = Notifier.__new__(Notifier)
    n.cfg = CFG
    action = step(State(), RULES, T0, fix=af(T0))[0]
    n.send(action)
    assert sent["url"] == "https://ntfy.sh/test-topic-0123456789abcdef"
    assert sent["headers"]["Priority"] == "urgent"
    assert sent["headers"]["Click"].startswith("https://maps.google.com/?q=")
    assert sent["headers"]["Title"] == "Dad left Sunrise"


def _notifier(cfg, fail=()):
    from notifier import Notifier
    n = Notifier.__new__(Notifier)
    n.cfg = cfg
    n.log = []

    def rec(kind):
        def f(*args, **kw):
            label = f"{kind} {args[0]}" if kind != "push" else "push"
            n.log.append(label)
            if label in fail:
                raise RuntimeError("boom")
        return f
    n.call, n.sms, n.push = rec("call"), rec("sms"), rec("push")
    return n


def test_fans_out_to_every_caregiver_and_channel():
    import dataclasses
    from incident import Action
    cfg = dataclasses.replace(CFG, caregiver_methods=("push", "sms"))
    n = _notifier(cfg)
    n.send(Action("away_sms", "sms", "caregiver", "x"))
    assert n.log == ["push", "sms +15551234567", "sms +15557654321"]
    n.log.clear()
    n.send(Action("caregiver_call", "call", "caregiver", "x"))
    assert n.log == ["call +15551234567", "call +15557654321"]


def test_partial_failure_counts_as_delivered_total_failure_raises():
    import dataclasses
    from incident import Action
    cfg = dataclasses.replace(CFG, caregiver_methods=("sms",))
    n = _notifier(cfg, fail={"sms +15551234567"})
    n.send(Action("away_sms", "sms", "caregiver", "x"))          # one got through: no raise
    n = _notifier(cfg, fail={"sms +15551234567", "sms +15557654321"})
    with pytest.raises(RuntimeError):
        n.send(Action("away_sms", "sms", "caregiver", "x"))


def test_run_cycle_reports_fetch_health(tmp_path):
    from notifier import DryRunNotifier
    st = StateStore(tmp_path / "s.json", tmp_path / "s.lock")
    assert run_cycle(CFG, FakeClient(away_fix(T0)), DryRunNotifier(CFG), st, now=T0) is True
    assert run_cycle(CFG, FakeClient(AirpinpointError("HTTP 401")), DryRunNotifier(CFG), st,
                     now=T0 + 300) is False


def test_watchdog_ping_never_raises(monkeypatch):
    import monitor

    def boom(*a, **k):
        raise monitor.requests.ConnectionError("offline")
    monkeypatch.setattr(monitor.requests, "get", boom)
    monitor.ping_watchdog("https://hc-ping.com/x")   # logs, doesn't raise
    monitor.ping_watchdog("")                         # disabled: no request
