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

IMPORTANT - token lifetime: the access token expires after ~8 hours and is
normally refreshed automatically by Claude Code itself running on the
machine that owns the login. There is no refresh token available to a
third-party reader of the keychain entry, so a token copied to a second
machine (e.g. a Raspberry Pi that never runs `claude`) WILL go stale after
about 8 hours and needs periodically re-extracting - see
scripts/claude_usage_token_extract.py (macOS-only, run wherever `claude` is
actually logged in).
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from src.utils.paths import get_claude_usage_token_state_path
from src.utils.state_store import read_json_state

logger = logging.getLogger(__name__)

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

    # The token state file holds whichever access token scripts/
    # claude_usage_token_sync.py last pushed, since it's kept fresher than
    # the bootstrap value in secrets.yaml - same pattern as resideo_client.py's
    # token rotation handling.
    token_state = read_json_state(get_claude_usage_token_state_path())
    access_token = token_state.get("access_token") or usage_config.get("access_token")
    if not access_token:
        logger.error(
            "claude_usage.access_token is not set - see config.yaml's claude_usage "
            "comments and scripts/claude_usage_token_extract.py"
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
