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

The token expires in about 8 hours and is only refreshed by Claude Code
itself running on THIS machine - copying it elsewhere does not extend that.
For a one-off copy, re-run this manually. For it to stay fresh automatically
on the Pi without manual copying, see scripts/claude_usage_token_sync.py
(same Keychain read as this script, pushed over SSH on a schedule) instead.

Usage:
    python3 scripts/claude_usage_token_extract.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime

KEYCHAIN_SERVICE = "Claude Code-credentials"


class KeychainReadError(Exception):
    """Raised when the Claude Code Keychain entry can't be read or parsed."""


def read_credentials_from_keychain() -> dict[str, object]:
    """Read and parse Claude Code's OAuth credentials from this Mac's login Keychain.

    Returns:
        Dict with "access_token" (str), "plan" (str), and "expires_at"
        (datetime | None).

    Raises:
        KeychainReadError: If not on macOS, the entry is missing, or its
            contents don't match the expected shape.

    """
    if sys.platform != "darwin":
        msg = "This only works on macOS (reads the login Keychain via /usr/bin/security)."
        raise KeychainReadError(msg)

    result = subprocess.run(  # noqa: S603  # Fixed argv, no shell, no user input - not injectable
        ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        msg = f"No '{KEYCHAIN_SERVICE}' entry found in your login Keychain - is Claude Code logged in?"
        raise KeychainReadError(msg)

    try:
        payload = json.loads(result.stdout)
        oauth = payload["claudeAiOauth"]
        access_token = oauth["accessToken"]
    except (json.JSONDecodeError, KeyError) as e:
        msg = f"Keychain entry exists but didn't contain the expected fields: {e}"
        raise KeychainReadError(msg) from e

    expires_at_ms = oauth.get("expiresAt")
    expires_at = datetime.fromtimestamp(expires_at_ms / 1000, tz=UTC) if expires_at_ms else None

    return {
        "access_token": access_token,
        "plan": oauth.get("subscriptionType", "unknown"),
        "expires_at": expires_at,
    }


def main() -> None:
    """Execute main entry point."""
    try:
        credentials = read_credentials_from_keychain()
    except KeychainReadError as e:
        print(str(e))
        sys.exit(1)

    expires_at = credentials["expires_at"]
    if expires_at is not None:
        remaining = expires_at - datetime.now(tz=UTC)
        if remaining.total_seconds() <= 0:
            print("Warning: this token has already expired - run `claude` once in Terminal first, then retry.")
        else:
            print(f"Plan: {credentials['plan']}. Expires in {remaining.total_seconds() / 3600:.1f} hours ({expires_at.isoformat()}).")

    print("\nPaste this into the Pi's secrets.yaml (see secrets.yaml.example):\n")
    print("claude_usage:")
    print(f'  access_token: "{credentials["access_token"]}"')
    print()


if __name__ == "__main__":
    main()
