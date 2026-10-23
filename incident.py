"""Pure decision logic: given the persisted state, the latest observation and the
current time, decide which alerts to send. No network or file I/O happens here,
so every rule can be unit-tested with synthetic clocks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from geo import compass_direction, describe_distance, haversine_meters

# Loop timing drifts by a few seconds; don't skip a repeat because of it.
TIMING_SLACK_S = 10.0


@dataclass(frozen=True)
class Fix:
    lat: float
    lon: float
    ts: float                      # epoch seconds (UTC) the location was reported
    accuracy_m: float | None = None


@dataclass(frozen=True)
class Rules:
    parent_name: str
    residence_name: str
    residence_lat: float
    residence_lon: float
    radius_m: float
    sms_repeat_s: float = 300
    call_cooldown_s: float = 1800
    call_enabled: bool = True
    away_confirm_fixes: int = 1
    failure_alert_s: float = 1800
    stale_fix_alert_s: float = 14400
    caregiver_call_on_alert: bool = False


@dataclass(frozen=True)
class Action:
    kind: str       # away_sms | call | caregiver_call | returned_sms | failure_alert | failure_recovered | stale_alert | stale_recovered
    channel: str    # sms (a caregiver message: push or SMS) | call
    target: str     # caregiver | front_desk
    text: str
    url: str | None = None


@dataclass
class State:
    started_at: float | None = None
    last_fix: dict | None = None          # asdict(Fix)
    away_streak: int = 0
    incident_started: float | None = None
    incident_sms_count: int = 0
    incident_call_count: int = 0
    last_sms: float | None = None
    last_call: float | None = None
    first_failure: float | None = None
    failure_alerted: bool = False
    stale_alerted: bool = False
    paused_until: float | None = None
    caregiver_called: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @property
    def fix(self) -> Fix | None:
        return Fix(**self.last_fix) if self.last_fix else None

    def on_send_failed(self, action: Action) -> None:
        """Undo the bookkeeping for an alert that could not be delivered so the
        next cycle retries it instead of waiting out a full interval."""
        if action.kind == "away_sms":
            self.last_sms = None
            self.incident_sms_count = max(0, self.incident_sms_count - 1)
        elif action.kind == "call":
            self.last_call = None
            self.incident_call_count = max(0, self.incident_call_count - 1)
        elif action.kind == "caregiver_call":
            self.caregiver_called = False
        elif action.kind == "failure_alert":
            self.failure_alerted = False
        elif action.kind == "stale_alert":
            self.stale_alerted = False


def distance_m(rules: Rules, fix: Fix) -> float:
    return haversine_meters(rules.residence_lat, rules.residence_lon, fix.lat, fix.lon)


def classify(rules: Rules, fix: Fix) -> str:
    """'away' only when the whole accuracy circle is outside the geofence,
    'home' when the reported point is inside, otherwise 'uncertain'."""
    dist = distance_m(rules, fix)
    acc = fix.accuracy_m or 0.0
    if dist - acc > rules.radius_m:
        return "away"
    if dist <= rules.radius_m:
        return "home"
    return "uncertain"


def is_paused(state: State, now: float) -> bool:
    return state.paused_until is not None and now < state.paused_until


def _due(last: float | None, interval: float, now: float) -> bool:
    return last is None or now - last >= interval - TIMING_SLACK_S


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%-I:%M %p")


def _age(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes < 1:
        return "just now"
    if minutes < 120:
        return f"{minutes} min ago"
    return f"{minutes // 60}h {minutes % 60}m ago"


def _maps_link(fix: Fix) -> str:
    # %2C, not ",": ntfy's Actions header is comma-separated and rejects the
    # whole push (HTTP 400) if the URL contains a bare comma.
    return f"https://maps.google.com/?q={fix.lat:.6f}%2C{fix.lon:.6f}"


def _where(rules: Rules, fix: Fix) -> tuple[str, str]:
    dist = describe_distance(distance_m(rules, fix))
    direction = compass_direction(rules.residence_lat, rules.residence_lon, fix.lat, fix.lon)
    return dist, direction


def away_sms_text(rules: Rules, state: State, fix: Fix, now: float,
                  calling_now: bool = False) -> str:
    dist, direction = _where(rules, fix)
    location = (f"{dist} {direction} of {rules.residence_name} "
                f"(location from {_age(now - fix.ts)}). Map: {_maps_link(fix)}")
    if state.incident_sms_count == 0:
        call_note = " Calling front desk now." if calling_now else ""
        return (f"ALERT {_clock(now)}: {rules.parent_name}'s wallet has left the geofence. "
                f"{location}{call_note}")
    calls = (f" Front desk called {state.incident_call_count}x."
             if state.incident_call_count else "")
    return (f"UPDATE {_clock(now)}: {rules.parent_name}'s wallet still away "
            f"(since {_clock(state.incident_started)}). {location}{calls}")


def caregiver_call_text(rules: Rules, fix: Fix, now: float) -> str:
    dist, direction = _where(rules, fix)
    msg = (f"Alert. {rules.parent_name}'s wallet has left {rules.residence_name}. "
           f"It was last detected about {dist} {direction} of the building, "
           f"{_age(now - fix.ts).replace('min', 'minutes')}. "
           "Check your phone notifications for a map link. ")
    return msg + "Repeating. " + msg


def call_text(rules: Rules, fix: Fix, now: float) -> str:
    dist, direction = _where(rules, fix)
    msg = (f"Hello, this is an automated family safety alert about {rules.parent_name}, "
           f"a resident at {rules.residence_name}. The tracker in {rules.parent_name}'s "
           f"wallet was last detected about {dist} {direction} of the building, "
           f"{_age(now - fix.ts).replace('min', 'minutes')}. "
           f"Please check whether {rules.parent_name} has left the residence, "
           "and contact the family. ")
    return msg + "Repeating. " + msg


def step(state: State, rules: Rules, now: float,
         fix: Fix | None = None, error: str | None = None) -> list[Action]:
    """Advance `state` in place for one poll cycle and return alerts to send.

    `fix` is the newest valid location the API returned (None if it returned
    none); `error` is set when the fetch itself failed.
    """
    actions: list[Action] = []
    if state.started_at is None:
        state.started_at = now

    def sms(kind: str, text: str, url: str | None = None) -> None:
        actions.append(Action(kind, "sms", "caregiver", text, url))

    # --- Fetch health -------------------------------------------------------
    if error is not None:
        if state.first_failure is None:
            state.first_failure = now
        if not state.failure_alerted and now - state.first_failure >= rules.failure_alert_s:
            state.failure_alerted = True
            sms("failure_alert",
                f"WARNING: {rules.parent_name}'s tracker monitor can't fetch locations "
                f"(failing since {_clock(state.first_failure)}). Alerts are NOT working. "
                f"Last error: {error[:120]}")
    else:
        if state.failure_alerted:
            sms("failure_recovered",
                f"OK: {rules.parent_name}'s tracker monitor is fetching locations again.")
        state.first_failure = None
        state.failure_alerted = False

    # --- New location -------------------------------------------------------
    prev = state.fix
    if fix is not None and (prev is None or fix.ts > prev.ts):
        state.last_fix = asdict(fix)
        zone = classify(rules, fix)
        if zone == "away":
            state.away_streak += 1
        elif zone == "home":
            state.away_streak = 0
            if state.incident_started is not None:
                minutes = int((now - state.incident_started) // 60)
                sms("returned_sms",
                    f"OK {_clock(now)}: {rules.parent_name}'s wallet is back at "
                    f"{rules.residence_name} (location from {_age(now - fix.ts)}). "
                    f"Alert lasted {minutes} min.")
                state.incident_started = None
                state.incident_sms_count = state.incident_call_count = 0
                state.last_sms = state.last_call = None
                state.caregiver_called = False
        # 'uncertain' leaves the streak unchanged.

    # --- Staleness ----------------------------------------------------------
    latest = state.fix
    reference = latest.ts if latest else state.started_at
    if now - reference >= rules.stale_fix_alert_s:
        if not state.stale_alerted:
            state.stale_alerted = True
            last = f"from {_age(now - latest.ts)}" if latest else "never received"
            sms("stale_alert",
                f"WARNING: no new location for {rules.parent_name}'s wallet "
                f"(last {last}). Card battery may be dead or no iPhones nearby.")
    elif state.stale_alerted:
        state.stale_alerted = False
        sms("stale_recovered",
            f"OK: receiving locations for {rules.parent_name}'s wallet again.")

    # --- Incident -----------------------------------------------------------
    paused = is_paused(state, now)
    if (state.incident_started is None and not paused
            and state.away_streak >= rules.away_confirm_fixes):
        state.incident_started = now

    if state.incident_started is not None and not paused and latest is not None:
        call_due = rules.call_enabled and _due(state.last_call, rules.call_cooldown_s, now)
        if _due(state.last_sms, rules.sms_repeat_s, now):
            sms("away_sms", away_sms_text(rules, state, latest, now, calling_now=call_due),
                _maps_link(latest))
            state.last_sms = now
            state.incident_sms_count += 1
        if rules.caregiver_call_on_alert and not state.caregiver_called:
            actions.append(Action("caregiver_call", "call", "caregiver",
                                  caregiver_call_text(rules, latest, now)))
            state.caregiver_called = True
        if call_due:
            actions.append(Action("call", "call", "front_desk", call_text(rules, latest, now)))
            state.last_call = now
            state.incident_call_count += 1

    return actions
