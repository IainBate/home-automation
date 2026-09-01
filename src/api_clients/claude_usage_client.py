"""Claude Code subscription usage client - read-only, via Anthropic's OAuth usage endpoint.

This is the same endpoint and OAuth token the "Claude Usage" macOS menu bar
app and Claude Code itself use (~/bin/claude_usage_app on this project's
Mac) - NOT an Anthropic API key, which measures a different product
(pay-per-token developer API billing, not a Pro/Max subscription's session/
weekly allowance) and would not work here.

IMPORTANT - shared rate limit: this endpoint is rate-limited per-account and
the budget is shared with every client signed in as the same user, including
real Claude Code sessions. The reference app's own README documents a real
incident: polling every 60 seconds caused sustained HTTP 429s for 45 minutes
across ALL clients on the account (it recovered in ~12 minutes once that
polling stopped). This module's caller (scripts/claude_usage_poller.py) MUST
run on a slow, cron-driven cadence (10+ minutes) - never call this from the
dashboard's own fast per-subsystem poll loop.

Token source, by design one machine only - no cross-machine sync: the access
token expires after ~8 hours and is only refreshed by Claude Code itself
running on the machine that owns the login. This reads that machine's own
local credential store directly (macOS Keychain, or ~/.claude/.credentials.json
on Linux) every poll, so it stays fresh automatically for as long as `claude`
gets used at least occasionally on THAT machine - the same machine
scripts/claude_usage_poller.py's cron job runs on (the Pi, typically).
config.yaml's claude_usage.access_token is only a manual fallback for a
machine that never runs `claude` itself.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

CLAUDE_CODE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CLAUDE_CODE_LINUX_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_TIMEOUT_SECONDS = 20

_KIND_LABELS = {
    "session": "Current session (5-hour)",
    "weekly_all": "This week - all models",
    "weekly_opus": "This week - Opus",
    "weekly_sonnet": "This week - Sonnet",
    "weekly_oauth_apps": "This week - connected apps",
}

# Fallback named blocks, used only if the response has no "limits" list -
# mirrors the reference app's own fallback (see module docstring).
_FALLBACK_BLOCKS = [("five_hour", "session"), ("seven_day", "weekly_all"), ("seven_day_opus", "weekly_opus")]


def _label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind.replace("_", " ").title())


def _read_local_claude_code_access_token() -> str | None:
    """Read this machine's own Claude Code OAuth access token, or None if unavailable.

    macOS: the login Keychain entry (service "Claude Code-credentials") -
    same one the "Claude Usage" menu bar app reads. Linux: Claude Code's own
    ~/.claude/.credentials.json. Neither is written by this function - purely
    a read of whatever Claude Code itself already keeps fresh through normal
    use on this machine.
    """
    try:
        if sys.platform == "darwin":
            result = subprocess.run(  # noqa: S603  # Fixed argv, no shell, no user input
                ["/usr/bin/security", "find-generic-password", "-s", CLAUDE_CODE_KEYCHAIN_SERVICE, "-w"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None
            payload = json.loads(result.stdout)
        elif CLAUDE_CODE_LINUX_CREDENTIALS_PATH.exists():
            payload = json.loads(CLAUDE_CODE_LINUX_CREDENTIALS_PATH.read_text(encoding="utf-8"))
        else:
            return None

        return payload["claudeAiOauth"]["accessToken"]
    except (OSError, json.JSONDecodeError, KeyError) as e:
        logger.debug("Could not read local Claude Code credentials: %s", e)
        return None


def _parse_usage(payload: dict[str, Any]) -> dict[str, Any]:
    buckets = []

    for limit in payload.get("limits") or []:
        kind = limit.get("kind")
        if not kind:
            continue
        buckets.append(
            {
                "kind": kind,
                "label": _label(kind),
                "percent_used": limit.get("percent", 0),
                "resets_at": limit.get("resets_at"),
                "severity": limit.get("severity", "normal"),
            }
        )

    if not buckets:
        for key, kind in _FALLBACK_BLOCKS:
            block = payload.get(key)
            if not isinstance(block, dict) or "utilization" not in block:
                continue
            buckets.append(
                {
                    "kind": kind,
                    "label": _label(kind),
                    "percent_used": block["utilization"],
                    "resets_at": block.get("resets_at"),
                    "severity": "normal",
                }
            )

    extra_usage = payload.get("extra_usage") or {}
    extra_usage_percent = extra_usage.get("utilization") if extra_usage.get("is_enabled") else None

    return {"buckets": buckets, "extra_usage_percent": extra_usage_percent}


def fetch_claude_usage(config: dict[str, Any]) -> dict[str, Any] | None:
    """Read-only Claude Code usage snapshot.

    Args:
        config: Full static config - reads its "claude_usage" section
            (access_token expected merged in from secrets.yaml).

    Returns:
        Dict with "buckets" (list of {kind, label, percent_used, resets_at,
        severity}) and "extra_usage_percent" (or None if not enabled on the
        account), or None if disabled, misconfigured, the token has expired/
        was rejected, or the request was rate-limited - the caller should
        keep showing its last cached snapshot rather than treat any of these
        as reason to blank the display (fail-fast, matches this codebase's
        other cloud clients).

    """
    usage_config = config.get("claude_usage", {})
    if not usage_config.get("enabled", False):
        return None

    # Prefer this machine's own Claude Code login over the static config
    # fallback - see module docstring for why there's no cross-machine sync.
    access_token = _read_local_claude_code_access_token() or usage_config.get("access_token")
    if not access_token:
        logger.error(
            "claude_usage.access_token is not set and no local Claude Code login was found - "
            "see config.yaml's claude_usage comments"
        )
        return None

    timeout_seconds = usage_config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "home_automation dashboard (claude_usage_client.py)",
    }

    try:
        response = requests.get(USAGE_URL, headers=headers, timeout=timeout_seconds)
    except requests.RequestException as e:
        logger.warning("Failed to reach Claude usage endpoint: %s", e)
        return None

    if response.status_code in (401, 403):
        logger.warning(
            "Claude usage token rejected (HTTP %d) - it has likely expired; re-run "
            "scripts/claude_usage_token_extract.py on the machine where `claude` is logged "
            "in, then update secrets.yaml",
            response.status_code,
        )
        return None

    if response.status_code == 429:
        logger.info("Claude usage endpoint rate-limited this request - will retry next cycle")
        return None

    if response.status_code != 200:
        logger.warning("Unexpected Claude usage response: HTTP %d", response.status_code)
        return None

    try:
        payload = response.json()
    except ValueError:
        logger.warning("Claude usage response was not valid JSON")
        return None

    return _parse_usage(payload)
