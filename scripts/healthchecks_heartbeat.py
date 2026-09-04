#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Healthchecks.io Dead-Man's-Switch Heartbeat (one-shot CLI).

Pings an external Healthchecks.io check on every run. This is deliberately
NOT another "did something go wrong" check like weekly_health_check.py /
daily_digest_check.py - those email you FROM the Pi, which means a Pi that's
powered off, off the network, or has a dead cron simply sends no email at
all, indistinguishable from "nothing to report". A ping that stops arriving
is the one failure mode those two can't detect, and it's exactly what
Healthchecks.io's own missed-ping alert is for.

Success and failure both ping (a bare GET for success, "/fail" appended for
failure) - see ~/bin/home_backup on the Pi for the same convention already
in use for the backup job. This heartbeat has no real "check logic" of its
own to fail beyond reaching the internet, so in practice the plain ping
covers almost every run; /fail exists mainly so a network-level failure
shows up in Healthchecks.io's own history instead of just going quiet for
one tick.

Usage:
    python3 scripts/healthchecks_heartbeat.py [--config config.yaml]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import logging
from typing import Any

import requests

from hotwater_automation_core import get_config_path

from src.config_manager.config_manager import load_static_config
from src.utils.logging_setup import configure_cron_safe_logging

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10


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
    """Ping the configured Healthchecks.io check. Returns 0 on success, 1 otherwise."""
    hc_config = config.get("healthchecks_io", {})
    if not hc_config.get("enabled", False):
        if not quiet:
            print("Healthchecks.io heartbeat is disabled (healthchecks_io.enabled: false)")
        return 1

    ping_url = hc_config.get("ping_url")
    timeout_seconds = hc_config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)

    try:
        requests.get(ping_url, timeout=timeout_seconds)
    except requests.RequestException as e:
        logger.warning("Healthchecks.io ping failed: %s", e)
        if not quiet:
            print(f"Ping failed: {e}")
        # Best-effort "/fail" ping so the miss is visible in Healthchecks.io's
        # own history rather than just a silent gap - swallow any failure
        # here too, since there's nothing further to fall back to.
        try:
            requests.get(f"{ping_url}/fail", timeout=timeout_seconds)
        except requests.RequestException:
            pass
        return 1

    if not quiet:
        print("Healthchecks.io ping sent")
    return 0


def main() -> None:
    """Execute main entry point."""
    args = _create_argument_parser().parse_args()
    configure_cron_safe_logging(
        level=getattr(logging, args.log_level),
        quiet=args.quiet,
        log_filename="healthchecks_heartbeat.log",
    )

    config_path = args.config or get_config_path()
    config = load_static_config(config_path)
    if config is None:
        print("Failed to load config.yaml (see logs above)")
        sys.exit(1)

    sys.exit(run(config, quiet=args.quiet))


if __name__ == "__main__":
    main()
