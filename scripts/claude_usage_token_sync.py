#!/usr/bin/env python3
"""Claude Code Token Sync (macOS only, run on whichever machine runs `claude`).

Automatically keeps the dashboard's Claude usage panel authenticated,
without the manual "extract, copy, paste into secrets.yaml" cycle
scripts/claude_usage_token_extract.py describes: reads the current access
token from this Mac's login Keychain (exactly as that script does) and
pushes it over SSH into the remote repo's config/claude_usage_token_state.json,
which claude_usage_client.py reads in preference to secrets.yaml's static
bootstrap value.

Why this can't run ON the dashboard machine (e.g. the Pi) instead: the
access token is only ever refreshed by Claude Code itself running on the
machine that's actually logged in - see claude_usage_client.py's module
docstring. This script must run wherever THAT is (this Mac), on a schedule,
for the Pi's copy to stay fresh automatically. The Claude Usage menu bar app
doesn't need this at all: it reads the Keychain directly, every poll, on the
same machine `claude` keeps logged in - there's nothing to sync there.

Schedule via cron (every 2 hours - the token lasts ~8, so this leaves
plenty of margin even if a run is missed):

    0 */2 * * * /usr/bin/python3 /path/to/repo/scripts/claude_usage_token_sync.py --quiet

Requires:
    - Passwordless SSH to the remote host (Tailscale SSH - see README's
      Status Dashboard section - or an SSH key already set up).
    - `claude` to have been run at least once on this Mac so Keychain has a
      valid entry (see claude_usage_token_extract.py).

Usage:
    python3 scripts/claude_usage_token_sync.py --host homepi4
    python3 scripts/claude_usage_token_sync.py --host homepi4 --user pi --remote-repo-path /home/pi/home_automation
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from claude_usage_token_extract import KeychainReadError, read_credentials_from_keychain

DEFAULT_REMOTE_REPO_PATH = "/home/pi/home_automation"
DEFAULT_SSH_USER = "pi"
DEFAULT_SSH_TIMEOUT_SECONDS = 15


def _create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="Remote hostname/IP (e.g. a Tailscale hostname)")
    parser.add_argument("--user", default=DEFAULT_SSH_USER, help=f"SSH user (default: {DEFAULT_SSH_USER})")
    parser.add_argument(
        "--remote-repo-path",
        default=DEFAULT_REMOTE_REPO_PATH,
        help=f"Path to the repo on the remote host (default: {DEFAULT_REMOTE_REPO_PATH})",
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    return parser


def push_token(access_token: str, *, host: str, user: str, remote_repo_path: str) -> bool:
    """Write {"access_token": ...} to the remote repo's token state file over SSH.

    Returns:
        True on success, False if the SSH command failed (logged to stderr
        by ssh itself - nothing here needs to duplicate that).

    """
    remote_path = f"{remote_repo_path}/config/claude_usage_token_state.json"
    payload = json.dumps({"access_token": access_token})

    result = subprocess.run(  # noqa: S603  # Fixed argv aside from the JSON payload on stdin - no shell involved
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={DEFAULT_SSH_TIMEOUT_SECONDS}",
            f"{user}@{host}",
            f"mkdir -p $(dirname {remote_path}) && cat > {remote_path}",
        ],
        input=payload,
        capture_output=True,
        text=True,
        timeout=DEFAULT_SSH_TIMEOUT_SECONDS + 5,
        check=False,
    )
    if result.returncode != 0:
        print(f"SSH push failed: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def main() -> None:
    """Execute main entry point."""
    args = _create_argument_parser().parse_args()

    try:
        credentials = read_credentials_from_keychain()
    except KeychainReadError as e:
        print(str(e))
        sys.exit(1)

    success = push_token(
        credentials["access_token"],
        host=args.host,
        user=args.user,
        remote_repo_path=args.remote_repo_path,
    )
    if not success:
        sys.exit(1)

    if not args.quiet:
        print(f"Synced Claude Code access token to {args.user}@{args.host}")


if __name__ == "__main__":
    main()
