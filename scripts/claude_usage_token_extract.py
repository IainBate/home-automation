#!/usr/bin/env python3
"""Claude Code Login Token Extractor (macOS only, run on whichever machine runs `claude`).

Reads the same OAuth access token Claude Code itself stores in this Mac's
login Keychain (service "Claude Code-credentials") - the same one the
"Claude Usage" menu bar app (~/bin/claude_usage_app) reads - and prints it
so you can copy it into the Pi's secrets.yaml for the dashboard's Claude
usage panel (see config.yaml's claude_usage comments).

Strictly read-only against the Keychain: shells out to
`/usr/bin/security find-generic-password`, never writes, so it cannot
disturb your Claude Code login.

The printed token expires in about 8 hours and is only refreshed by Claude
Code itself running on THIS machine - copying it to the Pi does not extend
that. Re-run this (and re-paste into the Pi's secrets.yaml) whenever the
dashboard's Claude usage panel shows "token expired".

Usage:
    python3 scripts/claude_usage_token_extract.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime

KEYCHAIN_SERVICE = "Claude Code-credentials"


def main() -> None:
    """Execute main entry point."""
    if sys.platform != "darwin":
        print("This only works on macOS (reads the login Keychain via /usr/bin/security).")
        sys.exit(1)

    result = subprocess.run(  # noqa: S603  # Fixed argv, no shell, no user input - not injectable
        ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        print(f"No '{KEYCHAIN_SERVICE}' entry found in your login Keychain - is Claude Code logged in?")
        sys.exit(1)

    try:
        payload = json.loads(result.stdout)
        oauth = payload["claudeAiOauth"]
        access_token = oauth["accessToken"]
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Keychain entry exists but didn't contain the expected fields: {e}")
        sys.exit(1)

    plan = oauth.get("subscriptionType", "unknown")
    expires_at_ms = oauth.get("expiresAt")
    if expires_at_ms:
        expires_at = datetime.fromtimestamp(expires_at_ms / 1000, tz=UTC)
        remaining = expires_at - datetime.now(tz=UTC)
        if remaining.total_seconds() <= 0:
            print("Warning: this token has already expired - run `claude` once in Terminal first, then retry.")
        else:
            print(f"Plan: {plan}. Expires in {remaining.total_seconds() / 3600:.1f} hours ({expires_at.isoformat()}).")

    print("\nPaste this into the Pi's secrets.yaml (see secrets.yaml.example):\n")
    print("claude_usage:")
    print(f'  access_token: "{access_token}"')
    print()


if __name__ == "__main__":
    main()
