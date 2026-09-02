#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""SolaX Cloud Realtime Snapshot Logger (one-shot CLI).

Appends one live SolaX Cloud reading to data/solax_historical_data.json per
run, meant to run every few minutes via cron - see
src/api_clients/solax_cloud_client.py's module docstring for the full story,
but in short: this file's historical-data endpoints (scripts/
solax_cloud_data_logger.py) turned out to need a mobile-app session token
nobody has, and the token an end-user actually gets from their account
(2026-09-02) only works against a real-time-only endpoint with no
historical data of any kind. Polling it going forward is the only way
data/solax_historical_data.json can grow at all right now - it can never
backfill a day that's already over.

Run via cron every few minutes (the API's own documented limit is 10
calls/min and 10,000/day, so this has huge headroom):

    */5 * * * * cd /path/to/repo && python3 scripts/solax_realtime_logger.py --quiet

Usage:
    python3 scripts/solax_realtime_logger.py [--config config.yaml]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import logging
from typing import Any

from hotwater_automation_core import get_config_path

from src.api_clients.solax_cloud_client import merge_realtime_snapshot, solax_cloud_get_realtime_snapshot
from src.config_manager.config_manager import load_static_config
from src.utils.logging_setup import configure_cron_safe_logging
from src.utils.paths import get_solax_historical_data_path
from src.utils.state_store import locked_json_update

logger = logging.getLogger(__name__)

_PLACEHOLDER = "NOT_USED_FOR_MODBUS"

# Generous relative to this script's own work (a lock is only held for the
# local read-merge-write, never across the network call above), but a
# stalled sibling tick holding the lock should be waited out rather than
# racing it - this cron entry runs every 5 minutes, so a wait of even a
# minute still finishes long before the next tick.
LOCK_TIMEOUT_SECONDS = 60.0


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
    """Fetch one realtime snapshot and append it. 0/1 exit code.

    Not configured is a quiet no-op (0, matching solar_forecast_predictor.py's
    own "disabled" handling) rather than an error - there's no
    solaX_cloud_api.enabled flag in config.yaml's schema (unlike
    solar_forecast/mg_saic/etc.), so "still the NOT_USED_FOR_MODBUS
    placeholder" is this subsystem's actual "disabled" signal.
    """
    cloud_config = config.get("solaX_cloud_api", {})
    token_id = cloud_config.get("token_id")
    wifisn = cloud_config.get("master_wifisn")
    if not token_id or token_id == _PLACEHOLDER or not wifisn or wifisn == _PLACEHOLDER:
        msg = "SolaX Cloud API not configured (solaX_cloud_api.token_id/master_wifisn) - skipping"
        logger.info(msg)
        if not quiet:
            print(msg)
        return 0

    snapshot = solax_cloud_get_realtime_snapshot(config)
    if snapshot is None:
        msg = "Failed to fetch SolaX Cloud realtime snapshot (see logs above)"
        logger.warning(msg)
        if not quiet:
            print(msg)
        return 1

    # Locked (not a bare read-then-write): two ticks of this same cron entry
    # can overlap when a slow API call runs past the next 5-minute mark, and
    # an unlocked read-modify-write would silently drop whichever snapshot
    # finished second. locked_json_update also skips the write entirely when
    # the merge was a no-op, so a duplicate reading costs no disk I/O on an
    # 8MB file.
    data_path = get_solax_historical_data_path()
    stored = False
    data_points = 0
    with locked_json_update(data_path, timeout=LOCK_TIMEOUT_SECONDS) as record:
        updated_record = merge_realtime_snapshot(record, snapshot)
        stored = updated_record is not record
        if stored:
            record.clear()
            record.update(updated_record)
        # .get() rather than record["meta"]: merge_realtime_snapshot returns
        # the existing record untouched on a duplicate reading, and that
        # record isn't guaranteed to carry a "meta" key.
        data_points = record.get("meta", {}).get("data_points", len(record.get("data", [])))

    detail = "stored" if stored else "duplicate reading, not stored"
    summary = (
        f"SolaX realtime snapshot at {snapshot['timestamp']}: "
        f"PV {snapshot['pv_power_kw']:.2f}kW, SoC {snapshot['soc_percent']}% "
        f"({detail}; {data_points} total data points)"
    )
    logger.info(summary)
    if not quiet:
        print(summary)

    return 0


def main() -> None:
    """Execute main entry point."""
    args = _create_argument_parser().parse_args()
    configure_cron_safe_logging(
        level=getattr(logging, args.log_level),
        quiet=args.quiet,
        log_filename="solax_realtime_logger.log",
    )

    config_path = args.config or get_config_path()
    config = load_static_config(config_path)
    if config is None:
        print("Failed to load config.yaml (see logs above)")
        sys.exit(1)

    sys.exit(run(config, quiet=args.quiet))


if __name__ == "__main__":
    main()
