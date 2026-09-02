"""Disk-backed MELCloud login token cache.

MelCloudClient.connect() performed a full username/password login on every
construction, and hotwater_automation_core.py constructs a fresh client in
each of its three checks (force-heat every 10-15 min, revert and legionella
progress hourly each) - well over a hundred logins a day against MELCloud's
auth endpoint for what is really one account session.

The obvious fix - keep one connected client alive in the daemon - doesn't
work here: each check runs under its own asyncio.run(), and an aiohttp
ClientSession is bound to the event loop that created it, so the session
cannot outlive the check. The token, however, is just a string. Caching that
lets each check build a fresh session (cheap, local) while skipping the
login round-trip (expensive, remote, rate-limited).

Every failure mode falls back to a normal login: a missing/corrupt/expired
cache, or a cached token the server has since rejected (see
MelCloudClient.connect()'s retry-once-with-a-fresh-login path). The cache is
therefore never load-bearing - at worst it costs one wasted request before
falling back to exactly the previous behaviour.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from src.utils.paths import get_melcloud_token_cache_path
from src.utils.state_store import read_json_state, write_json_atomic

logger = logging.getLogger(__name__)

# MELCloud tokens are long-lived (the official app stays signed in for
# weeks), but this is deliberately conservative: the cost of an expired
# token is one wasted request plus a normal login, while the cost of
# re-logging in too eagerly is the exact problem this exists to fix.
DEFAULT_TOKEN_TTL_SECONDS = 12 * 3600.0


def read_cached_token(
    email: str, ttl_seconds: float = DEFAULT_TOKEN_TTL_SECONDS
) -> str | None:
    """Return a cached login token for this account, or None to log in normally.

    The account email is part of the match so a credentials change can't
    silently keep using the previous account's token.
    """
    record = read_json_state(get_melcloud_token_cache_path())
    if not record:
        return None

    token = record.get("token")
    cached_email = record.get("email")
    obtained_at_str = record.get("obtained_at")
    if not token or cached_email != email or not obtained_at_str:
        return None

    try:
        obtained_at = datetime.fromisoformat(obtained_at_str)
    except (TypeError, ValueError):
        return None
    if obtained_at.tzinfo is None:
        return None

    age_seconds = (datetime.now(tz=UTC) - obtained_at).total_seconds()
    if age_seconds > ttl_seconds:
        logger.debug("Cached MELCloud token is %.0fs old - logging in fresh", age_seconds)
        return None

    return str(token)


def write_cached_token(email: str, token: str) -> None:
    """Persist a freshly-obtained login token for reuse by the next check."""
    record: dict[str, Any] = {
        "email": email,
        "token": token,
        "obtained_at": datetime.now(tz=UTC).isoformat(),
    }
    write_json_atomic(get_melcloud_token_cache_path(), record)


def clear_cached_token() -> None:
    """Drop the cached token after the server rejects it."""
    write_json_atomic(get_melcloud_token_cache_path(), {})
