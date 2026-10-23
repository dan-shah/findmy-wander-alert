"""Poll Find My (via FindMy.py) for a Find My tag and alert when it leaves the residence.

Usage:
  python monitor.py                 run forever (what launchd runs)
  python monitor.py --once          run a single cycle
  python monitor.py --dry-run       log alerts instead of sending them
  python monitor.py --probe         print the raw location reports
  python monitor.py --status        show latest fix, distance and state
  python monitor.py --test-alert    send a test caregiver alert (push or SMS)
  python monitor.py --test-call     call each caregiver with the front-desk script
  python monitor.py --pause 3h      silence away alerts (e.g. a family outing)
  python monitor.py --resume        end a pause early
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator

import requests

from airpinpoint import AirpinpointClient
from findmy_source import FindMySource
from config import PROJECT_DIR, Config, ConfigError, load_config
from incident import Action, Fix, State, call_text, classify, distance_m, is_paused, step
from notifier import DryRunNotifier, Notifier

STATE_PATH = PROJECT_DIR / "state.json"
LOCK_PATH = PROJECT_DIR / "state.lock"
LOG_DIR = PROJECT_DIR / "logs"

log = logging.getLogger("dad_tracker")


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(LOG_DIR / "monitor.log", maxBytes=2_000_000, backupCount=5)
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    log.handlers[:] = [file_handler, stream_handler]
    log.setLevel(logging.INFO)


class StateStore:
    """state.json guarded by a file lock so CLI commands (--pause) and the
    running daemon never clobber each other."""

    def __init__(self, path: Path = STATE_PATH, lock_path: Path = LOCK_PATH):
        self.path, self.lock_path = path, lock_path

    def load(self) -> State:
        try:
            return State.from_dict(json.loads(self.path.read_text()))
        except FileNotFoundError:
            return State()
        except (ValueError, TypeError) as exc:
            log.error("state.json unreadable (%s); starting fresh", exc)
            return State()

    def _save(self, state: State) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.to_dict(), indent=2))
        os.replace(tmp, self.path)

    @contextlib.contextmanager
    def locked(self) -> Iterator[State]:
        with open(self.lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.load()
            yield state
            self._save(state)


def make_notifier(cfg: Config, dry_run: bool):
    return DryRunNotifier(cfg) if dry_run else Notifier(cfg)


def make_source(cfg: Config):
    if cfg.location_source == "airpinpoint":
        return AirpinpointClient(cfg.airpinpoint_key, cfg.trackable_id, cfg.lookback_hours)
    return FindMySource(cfg.findmy_key_file, cfg.findmy_account_file, cfg.findmy_anisette_libs)


def run_cycle(cfg: Config, client, notifier, store: StateStore,
              now: float | None = None) -> bool:
    """One check. Returns True if Find My answered (with or without a fix)."""
    fix: Fix | None = None
    error: str | None = None
    try:
        fix = client.latest_fix()
    except Exception as exc:  # any fetch/parse failure counts toward the health alert
        error = str(exc) or exc.__class__.__name__
        log.warning("Fetch failed: %s", error)

    now = time.time() if now is None else now
    with store.locked() as state:
        if fix:
            log.info("Fix %.6f,%.6f ±%s m, %.0f m from residence (%s), age %.0f min",
                     fix.lat, fix.lon, fix.accuracy_m or "?", distance_m(cfg.rules, fix),
                     classify(cfg.rules, fix), (now - fix.ts) / 60)
        elif error is None:
            log.info("No valid location in the last %s h", cfg.lookback_hours)
        actions = step(state, cfg.rules, now, fix=fix, error=error)
        for action in actions:
            try:
                notifier.send(action)
            except Exception as exc:
                log.error("FAILED to send %s (%s): %s", action.kind, action.channel, exc)
                state.on_send_failed(action)
        if is_paused(state, now) and state.away_streak:
            log.info("Paused until %s; away alerts suppressed",
                     datetime.fromtimestamp(state.paused_until).strftime("%c"))
    return error is None


def ping_watchdog(url: str) -> None:
    """Tell the external dead-man's switch we're alive. It alerts the family
    when pings stop: Mac off/asleep, network down, crash loop, or Apple
    fetches failing. Never let a ping problem disturb monitoring."""
    if not url:
        return
    try:
        requests.get(url, timeout=10).raise_for_status()
    except Exception as exc:
        log.warning("Watchdog ping failed: %s", exc)


def parse_duration(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mhd])\s*", text.lower())
    if not m:
        raise argparse.ArgumentTypeError("use e.g. 45m, 3h, 1d")
    return float(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]


def cmd_status(cfg: Config, client, store: StateStore) -> None:
    state = store.load()
    now = time.time()
    print(json.dumps(state.to_dict(), indent=2))
    if is_paused(state, now):
        print(f"\nPAUSED until {datetime.fromtimestamp(state.paused_until):%c}")
    fix = client.latest_fix()
    if fix is None:
        print("\nNo valid location returned.")
        return
    print(f"\nLatest fix: {fix.lat:.6f},{fix.lon:.6f} ±{fix.accuracy_m or '?'} m, "
          f"reported {datetime.fromtimestamp(fix.ts):%c} ({(now - fix.ts) / 60:.0f} min ago)")
    print(f"Distance from residence: {distance_m(cfg.rules, fix):.0f} m "
          f"(geofence {cfg.rules.radius_m:.0f} m) -> {classify(cfg.rules, fix).upper()}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true")
    g.add_argument("--probe", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--test-alert", "--test-sms", dest="test_alert", action="store_true")
    g.add_argument("--test-call", action="store_true")
    g.add_argument("--pause", type=parse_duration, metavar="DURATION")
    g.add_argument("--resume", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    setup_logging()
    needs_twilio = not (args.dry_run or args.probe or args.status or args.pause or args.resume)
    needs_location = not (args.test_alert or args.test_call or args.pause or args.resume)
    try:
        cfg = load_config(require_twilio=needs_twilio, require_location=needs_location)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    store = StateStore()
    client = make_source(cfg)

    if args.probe:
        print(json.dumps(client.fetch_raw(), indent=2)[:20000])
        print("\nParsed latest fix:", client.latest_fix())
        return 0
    if args.status:
        cmd_status(cfg, client, store)
        return 0
    if args.pause or args.resume:
        with store.locked() as state:
            state.paused_until = time.time() + args.pause if args.pause else None
        log.info("Away alerts %s", f"paused until {datetime.fromtimestamp(state.paused_until):%c}"
                 if args.pause else "resumed")
        return 0

    notifier = make_notifier(cfg, args.dry_run)
    if args.test_alert:
        notifier.send(_test_action("sms", f"Test from {cfg.rules.parent_name}'s tracker monitor. "
                                          "If you got this, caregiver alerts work."))
        return 0
    if args.test_call:
        sample = Fix(cfg.rules.residence_lat + 0.005, cfg.rules.residence_lon, time.time() - 600)
        notifier.send(_test_action("call", "This is a test call from the tracker monitor. "
                                           "Here is what the front desk will hear. "
                                           + call_text(cfg.rules, sample, time.time())))
        return 0

    log.info("Monitoring via %s every %ds (geofence %.0f m around %s)%s",
             cfg.location_source, cfg.check_interval_s, cfg.rules.radius_m,
             cfg.rules.residence_name, " [DRY RUN]" if args.dry_run else "")
    while True:
        started = time.monotonic()
        try:
            if run_cycle(cfg, client, notifier, store) and not args.dry_run:
                ping_watchdog(cfg.watchdog_url)
        except Exception:
            log.exception("Unexpected error in cycle")
        if args.once:
            return 0
        time.sleep(max(5.0, cfg.check_interval_s - (time.monotonic() - started)))


def _test_action(channel: str, text: str) -> Action:
    return Action("test", channel, "caregiver", text)


if __name__ == "__main__":
    sys.exit(main())
