#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Claude Code Usage Poller (one-shot CLI).

Fetches Claude Code's session (5-hour) and weekly usage percentages and
caches them for the dashboard. Deliberately a slow, cron-driven one-shot
script rather than part of the dashboard's own fast poll loop - see
src/api_clients/claude_usage_client.py's module docstring for why: the
usage endpoint's rate limit is shared with real Claude Code sessions using
the same login token, and polling faster than a few minutes has caused
sustained failures for every client on the account before.

Run via cron no more often than every 10 minutes:

    */10 * * * * cd /path/to/repo && python3 scripts/claude_usage_poller.py --quiet

Requires `claude` to be logged in on THIS machine (the one this cron job
runs on) - claude_usage_client.py reads that login directly, no separate
token setup needed. See config.yaml's claude_usage comments for the
config.yaml-based fallback if this machine never runs `claude` itself.

Usage:
    python3 scripts/claude_usage_poller.py [--config config.yaml]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import logging
from datetime import UTC, datetime
from typing import Any

from hotwater_automation_core import get_config_path

from src.api_clients.claude_usage_client import RateLimited, fetch_claude_usage
from src.config_manager.config_manager import load_static_config
from src.utils.paths import get_claude_usage_path
from src.utils.state_store import write_json_atomic

logger = logging.getLogger(__name__)


def _create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser


def run(config: dict[str, Any], *, quiet: bool) -> int:
    """Fetch and cache Claude usage. Returns 0 on success, 1 if disabled/unavailable.

    A fetch failure (expired token, rate limited, network error) is not
    treated as fatal - it leaves the previous cached file in place rather
    than overwriting it with an error, so the dashboard keeps showing the
    last known-good reading rather than going blank on a transient hiccup.

    A rate limit (HTTP 429) is logged at INFO, not WARNING - it's the
    expected, self-healing outcome of this shared-budget endpoint under
    load (see claude_usage_client.py's module docstring), not something
    that should page anyone. This cron job's own --log-level default
    (WARNING) means an INFO line here produces no stderr output, so cron's
    MAILTO doesn't fire for it. Every other failure still logs at WARNING.
    """
    if not config.get("claude_usage", {}).get("enabled", False):
        if not quiet:
            print("Claude usage is disabled (claude_usage.enabled: false)")
        return 1

    usage = fetch_claude_usage(config)
    if isinstance(usage, RateLimited):
        retry_after = usage.retry_after_seconds if usage.retry_after_seconds is not None else "?"
        msg = f"Claude usage endpoint rate-limited (retry after {retry_after}s) - leaving previous cache in place"
        logger.info(msg)
        if not quiet:
            print(msg)
        return 1
    if usage is None:
        msg = "Failed to fetch Claude usage (see logs above) - leaving previous cache in place"
        logger.warning(msg)
        if not quiet:
            print(msg)
        return 1

    record = {"fetched_at": datetime.now(tz=UTC).isoformat(), **usage}
    write_json_atomic(get_claude_usage_path(), record)

    if not quiet:
        for bucket in usage["buckets"]:
            print(f"{bucket['label']}: {bucket['percent_used']:.0f}% used")

    return 0


def main() -> None:
    """Execute main entry point."""
    args = _create_argument_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )

    config_path = args.config or get_config_path()
    config = load_static_config(config_path)
    if config is None:
        print("Failed to load config.yaml (see logs above)")
        sys.exit(1)

    sys.exit(run(config, quiet=args.quiet))


if __name__ == "__main__":
    main()
