"""One-time interactive Apple account sign-in for FindMy.py.

Run this yourself in Terminal (it prompts for your password and a 2FA code):

    .venv/bin/python findmy_login.py

It saves a reusable session to secrets/findmy_account.json (mode 600). That
file also holds your Apple password, so treat it like a password file.
Location reports are end-to-end encrypted to the tag's key, so in principle any
Apple account can fetch them (a secondary Apple ID would keep your main account
out of it; untested since Apple's 2026-10-12 sign-in change). Re-run this if
the monitor starts warning that it can't fetch locations.
"""

from __future__ import annotations

import getpass
import logging
import os
import sys

from findmy import LoginState
from findmy.reports import SyncSmsSecondFactor

from config import PROJECT_DIR
from native_anisette import native_apple_account

SECRETS_DIR = PROJECT_DIR / "secrets"
ACCOUNT_FILE = SECRETS_DIR / "findmy_account.json"


def main() -> int:
    # Non-secret Apple status codes, to diagnose sign-in problems.
    logging.basicConfig(format="  [%(name)s] %(message)s")
    logging.getLogger("dadtracker.gsa").setLevel(logging.INFO)
    SECRETS_DIR.mkdir(mode=0o700, exist_ok=True)
    account = native_apple_account()

    email = sys.argv[1] if len(sys.argv) > 1 else input("Apple ID email: ").strip()
    password = getpass.getpass("Apple ID password (hidden): ")
    state = account.login(email, password)

    if state == LoginState.REQUIRE_2FA:
        methods = account.get_2fa_methods()
        for i, m in enumerate(methods):
            label = (f"SMS to {m.phone_number}" if isinstance(m, SyncSmsSecondFactor)
                     else "Trusted device (code pops up on your iPhone/Mac)")
            print(f"  [{i}] {label}")
        choice = int(input("2FA method number: ").strip() or "0")
        method = methods[choice]
        method.request()
        state = method.submit(input("2FA code: ").strip())

    if state != LoginState.LOGGED_IN:
        print(f"Login did not complete (state: {state}).", file=sys.stderr)
        return 1

    account.to_json(ACCOUNT_FILE)
    os.chmod(ACCOUNT_FILE, 0o600)
    print(f"Signed in as {account.account_name}. Session saved to {ACCOUNT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
