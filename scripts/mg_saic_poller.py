#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""MG SAIC (MG iSmart) EV Poller (one-shot CLI).

Fetches battery SoC/range and caches it for the dashboard. Deliberately a
slow, cron-driven one-shot script rather than part of the dashboard's own
fast poll loop - see src/api_clients/saic_client.py's module docstring for
why: this logs into the same MG account as the household's phones, and
polling infrequently minimizes the (self-recovering) risk of momentarily
kicking one of those phone sessions.

Run via cron no more often than hourly:

    0 * * * * cd /path/to/repo && python3 scripts/mg_saic_poller.py --quiet

Usage:
    python3 scripts/mg_saic_poller.py [--config config.yaml]
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

from src.api_clients.saic_client import fetch_saic_status
from src.config_manager.config_manager import load_static_config
from src.utils.logging_setup import configure_cron_safe_logging
from src.utils.paths import get_mg_saic_status_path
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
    """Fetch and cache MG SAIC status. Returns 0 on success, 1 if disabled/unavailable.

    A fetch failure is not treated as fatal - it leaves the previous cached
    file in place rather than overwriting it with an error, so the
    dashboard keeps showing the last known-good reading.
    """
    if not config.get("mg_saic", {}).get("enabled", False):
        if not quiet:
            print("MG SAIC is disabled (mg_saic.enabled: false)")
        return 1

    status = fetch_saic_status(config)
    if status is None:
        msg = "Failed to fetch MG SAIC status (see logs above) - leaving previous cache in place"
        logger.warning(msg)
        if not quiet:
            print(msg)
        return 1

    record = {"fetched_at": datetime.now(tz=UTC).isoformat(), **status}
    write_json_atomic(get_mg_saic_status_path(), record)

    if not quiet:
        print(f"Battery: {status.get('battery_percent')}%, range: {status.get('range_km')} km")

    return 0


def main() -> None:
    """Execute main entry point."""
    args = _create_argument_parser().parse_args()
    configure_cron_safe_logging(
        level=getattr(logging, args.log_level),
        quiet=args.quiet,
        log_filename="mg_saic_poller.log",
    )

    config_path = args.config or get_config_path()
    config = load_static_config(config_path)
    if config is None:
        print("Failed to load config.yaml (see logs above)")
        sys.exit(1)

    sys.exit(run(config, quiet=args.quiet))


if __name__ == "__main__":
    main()
