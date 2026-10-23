"""Load and validate settings from .env. Fails fast with every problem listed."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from incident import Rules

PROJECT_DIR = Path(__file__).resolve().parent
E164 = re.compile(r"^\+[1-9]\d{7,14}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    rules: Rules
    check_interval_s: float
    lookback_hours: float
    caregiver_phones: tuple[str, ...]
    front_desk_phone: str
    front_desk_send_digits: str
    twilio_sid: str
    twilio_token: str
    twilio_from: str
    caregiver_methods: tuple[str, ...]   # any of: push, sms
    ntfy_server: str
    ntfy_topic: str
    location_source: str    # findmy | airpinpoint
    findmy_key_file: Path
    findmy_account_file: Path
    findmy_anisette_libs: Path
    airpinpoint_key: str
    trackable_id: str
    watchdog_url: str = ""   # dead-man's switch pinged after each good fetch


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def load_config(require_twilio: bool = True, require_location: bool = True,
                env_file: Path | None = None) -> Config:
    load_dotenv(env_file or PROJECT_DIR / ".env")
    errors: list[str] = []

    def get(name: str, default: str | None = None, required: bool = True) -> str:
        v = os.getenv(name, default)
        v = v.strip() if v is not None else ""
        if required and not v:
            errors.append(f"{name} is not set")
        return v

    def num(name: str, default: str, lo: float | None = None, hi: float | None = None) -> float:
        raw = get(name, default)
        if not raw:
            return 0.0  # already reported as unset
        try:
            v = float(raw)
        except ValueError:
            errors.append(f"{name}={raw!r} is not a number")
            return 0.0
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            errors.append(f"{name}={raw} is out of range [{lo}, {hi}]")
        return v

    def phone(name: str, required: bool = True) -> str:
        v = get(name, required=required)
        if v and not E164.match(v):
            errors.append(f"{name}={v!r} must be E.164, e.g. +15551234567")
        return v

    lat = num("RESIDENCE_LAT", "", -90, 90)
    lon = num("RESIDENCE_LON", "", -180, 180)
    if lat == 0 and lon == 0 and os.getenv("RESIDENCE_LAT") and os.getenv("RESIDENCE_LON"):
        errors.append("RESIDENCE_LAT/RESIDENCE_LON look unset (0,0)")

    call_enabled = _bool(get("FRONT_DESK_CALL_ENABLED", "true"))
    rules = Rules(
        parent_name=get("PARENT_NAME", "Dad"),
        residence_name=get("RESIDENCE_NAME", "the residence"),
        residence_lat=lat,
        residence_lon=lon,
        radius_m=num("GEOFENCE_RADIUS_METERS", "150", 20),
        sms_repeat_s=num("SMS_REPEAT_SECONDS", "300", 60),
        call_cooldown_s=num("FRONT_DESK_CALL_COOLDOWN_SECONDS", "1800", 120),
        call_enabled=call_enabled,
        away_confirm_fixes=int(num("AWAY_CONFIRM_FIXES", "1", 1, 10)),
        failure_alert_s=num("MONITOR_FAILURE_ALERT_SECONDS", "1800", 300),
        stale_fix_alert_s=num("STALE_FIX_ALERT_SECONDS", "14400", 1800),
        caregiver_call_on_alert=_bool(get("CAREGIVER_CALL_ON_ALERT", "true")),
    )

    methods = tuple(m.strip().lower() for m in
                    get("CAREGIVER_ALERT_METHODS", os.getenv("CAREGIVER_ALERT_METHOD", "push")).split(",")
                    if m.strip())
    if not methods or any(m not in ("push", "sms") for m in methods):
        errors.append(f"CAREGIVER_ALERT_METHODS={','.join(methods)!r} must list push and/or sms")
    ntfy_topic = get("NTFY_TOPIC", required="push" in methods)

    phones_raw = get("CAREGIVER_PHONES", os.getenv("CAREGIVER_PHONE", ""), required=require_twilio)
    caregiver_phones = tuple(p.strip() for p in phones_raw.split(",") if p.strip())
    for p in caregiver_phones:
        if not E164.match(p):
            errors.append(f"CAREGIVER_PHONES entry {p!r} must be E.164, e.g. +15551234567")
    if ntfy_topic and (len(ntfy_topic) < 20 or not re.fullmatch(r"[A-Za-z0-9_-]+", ntfy_topic)):
        errors.append("NTFY_TOPIC must be a long random name (20+ letters/digits/-/_); "
                      "anyone who guesses it can read the alerts")

    source = get("LOCATION_SOURCE", "findmy").lower()
    if source not in ("findmy", "airpinpoint"):
        errors.append(f"LOCATION_SOURCE={source!r} must be 'findmy' or 'airpinpoint'")
    use_findmy = require_location and source == "findmy"
    use_airpinpoint = require_location and source == "airpinpoint"

    def path(name: str, default: str, required: bool) -> Path:
        p = Path(get(name, default, required=required)).expanduser()
        return p if p.is_absolute() else PROJECT_DIR / p

    watchdog_url = get("WATCHDOG_PING_URL", "", required=False)
    if watchdog_url and not watchdog_url.startswith("https://"):
        errors.append("WATCHDOG_PING_URL must be an https:// URL")

    digits = get("FRONT_DESK_SEND_DIGITS", "", required=False)
    if digits and not re.fullmatch(r"[0-9w#*]+", digits):
        errors.append("FRONT_DESK_SEND_DIGITS may only contain 0-9, w, #, *")

    cfg = Config(
        rules=rules,
        check_interval_s=num("CHECK_INTERVAL_SECONDS", "300", 60),
        lookback_hours=num("LOOKBACK_HOURS", "12", 1, 168),
        caregiver_phones=caregiver_phones,
        front_desk_phone=phone("FRONT_DESK_PHONE", required=require_twilio and call_enabled),
        front_desk_send_digits=digits,
        twilio_sid=get("TWILIO_ACCOUNT_SID", required=require_twilio),
        twilio_token=get("TWILIO_AUTH_TOKEN", required=require_twilio),
        twilio_from=phone("TWILIO_FROM_NUMBER", required=require_twilio),
        caregiver_methods=methods,
        ntfy_server=get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
        ntfy_topic=ntfy_topic,
        location_source=source,
        findmy_key_file=path("FINDMY_KEY_FILE", "", use_findmy),
        findmy_account_file=path("FINDMY_ACCOUNT_FILE", "secrets/findmy_account.json", use_findmy),
        findmy_anisette_libs=path("FINDMY_ANISETTE_LIBS", "secrets/ani_libs.bin", use_findmy),
        airpinpoint_key=get("AIRPINPOINT_API_KEY", required=use_airpinpoint),
        trackable_id=get("AIRPINPOINT_TRACKABLE_ID", required=use_airpinpoint),
        watchdog_url=watchdog_url,
    )
    if errors:
        raise ConfigError("Fix these in .env:\n  - " + "\n  - ".join(errors))
    return cfg
