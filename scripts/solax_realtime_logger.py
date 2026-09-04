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

Falls back to a local Modbus TCP reading (2026-09-04) whenever the cloud
snapshot is unavailable or is a duplicate of the last stored row - the
SolaX Cloud dongle was found to stop pushing a fresh uploadTime for long
stretches overnight (confirmed: a ~10.5h gap every night even though this
script's cron entry ran every 5 minutes throughout), which starved
src/core_logic/battery_evening_prediction_logic.py's "analog day" matching
of exactly the overnight readings its default trigger_hour (21:30) +
horizon (3h, i.e. past midnight) needs - min_sample_days was never met
because there was never a usable ~00:30 SoC reading to pair with the ~21:30
one. Modbus TCP reads the master inverter directly over the LAN, so it
works regardless of the cloud dongle's own upload cadence.

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
from datetime import datetime
from typing import Any

import pytz

from hotwater_automation_core import get_config_path

from src.api_clients.solax_cloud_client import merge_realtime_snapshot, solax_cloud_get_realtime_snapshot
from src.api_clients.solax_modbus_client import solax_modbus_bulk_data
from src.config_manager.config_manager import load_static_config
from src.utils.logging_setup import configure_cron_safe_logging
from src.utils.paths import get_solax_historical_data_path
from src.utils.state_store import locked_json_update

logger = logging.getLogger(__name__)

_PLACEHOLDER = "NOT_USED_FOR_MODBUS"
DEFAULT_TIMEZONE = "Europe/London"

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


def _build_local_modbus_snapshot(config: dict[str, Any]) -> dict[str, Any] | None:
    """Build a realtime-snapshot-shaped reading from local Modbus TCP.

    Fallback for run() when the cloud snapshot is missing or a duplicate of
    the last stored row (see this module's docstring) - a single
    solax_modbus_bulk_data() call gets everything the cloud snapshot has,
    read directly from the master inverter over the LAN rather than via the
    cloud dongle's own upload cadence. Only the master's readings are used,
    matching what the cloud snapshot has always stored (data/
    solax_historical_data.json has never carried slave-specific data - see
    hotwater_automation_core.py's get_battery_prediction_to_deadline
    docstring). Sign conventions match the cloud snapshot's (verified
    against solax_modbus_client.py's own docstrings): battery_power_kw
    positive = charging, grid_power_kw positive = exporting.

    Returns:
        A dict shaped like solax_cloud_get_realtime_snapshot()'s return
        value, timestamped with the local wall clock (there is no device
        uploadTime to use here) - or None if the Modbus read itself failed.

    """
    bulk = solax_modbus_bulk_data(config)
    if bulk is None:
        return None

    soc = bulk.get("soc", {}).get("master")
    if soc is None:
        return None

    pv = bulk.get("pv_power", {}).get("master") or {}
    pv_power_w = (pv.get("pv1") or 0) + (pv.get("pv2") or 0)
    battery_power_w = (bulk.get("battery_power", {}).get("master") or {}).get("power")
    grid_power_w = bulk.get("grid_power", {}).get("master")
    yield_today_kwh = bulk.get("daily_yield", {}).get("master")

    tz_name = config.get("location", {}).get("default_timezone_str", DEFAULT_TIMEZONE)
    now_local = datetime.now(tz=pytz.timezone(tz_name))

    return {
        "timestamp": now_local.strftime("%Y-%m-%d %H:%M:%S"),
        "pv_power_kw": pv_power_w / 1000.0,
        "battery_power_kw": (battery_power_w / 1000.0) if battery_power_w is not None else None,
        "grid_power_kw": (grid_power_w / 1000.0) if grid_power_w is not None else None,
        "soc_percent": soc,
        "yield_today_kwh": yield_today_kwh,
        "source": "modbus_fallback",
    }


def _store_snapshot(data_path: str, snapshot: dict[str, Any]) -> tuple[bool, int]:
    """Merge one snapshot into data_path under lock. Returns (stored, data_points).

    Locked (not a bare read-then-write): two ticks of this same cron entry
    can overlap when a slow API call runs past the next 5-minute mark, and
    an unlocked read-modify-write would silently drop whichever snapshot
    finished second. locked_json_update also skips the write entirely when
    the merge was a no-op, so a duplicate reading costs no disk I/O on an
    8MB file. Kept network-free (the snapshot is already fetched by the
    time this is called) so the lock is only ever held for the local
    read-merge-write, never across a network call.
    """
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
    return stored, data_points


def run(config: dict[str, Any], *, quiet: bool) -> int:
    """Fetch one realtime snapshot (cloud, falling back to local Modbus) and append it.

    Not configured is a quiet no-op (0, matching solar_forecast_predictor.py's
    own "disabled" handling) rather than an error - there's no
    solaX_cloud_api.enabled flag in config.yaml's schema (unlike
    solar_forecast/mg_saic/etc.), so "still the NOT_USED_FOR_MODBUS
    placeholder", combined with Modbus TCP also not being enabled, is this
    subsystem's actual "disabled" signal.
    """
    cloud_config = config.get("solaX_cloud_api", {})
    token_id = cloud_config.get("token_id")
    wifisn = cloud_config.get("master_wifisn")
    cloud_enabled = bool(token_id) and token_id != _PLACEHOLDER and bool(wifisn) and wifisn != _PLACEHOLDER
    modbus_enabled = bool(cloud_config.get("modbus_enabled", False))

    if not cloud_enabled and not modbus_enabled:
        msg = (
            "SolaX Cloud API not configured (solaX_cloud_api.token_id/master_wifisn) "
            "and Modbus TCP not enabled - skipping"
        )
        logger.info(msg)
        if not quiet:
            print(msg)
        return 0

    data_path = get_solax_historical_data_path()
    snapshot: dict[str, Any] | None = None
    source = None
    stored = False
    data_points = 0

    if cloud_enabled:
        snapshot = solax_cloud_get_realtime_snapshot(config)
        if snapshot is None:
            logger.warning("Failed to fetch SolaX Cloud realtime snapshot (see logs above)")
        else:
            source = "cloud"
            stored, data_points = _store_snapshot(data_path, snapshot)

    if (snapshot is None or not stored) and modbus_enabled:
        fallback_snapshot = _build_local_modbus_snapshot(config)
        if fallback_snapshot is None:
            logger.warning("Local Modbus fallback snapshot also unavailable (see logs above)")
        else:
            fallback_stored, fallback_data_points = _store_snapshot(data_path, fallback_snapshot)
            # Prefer the fallback's own result whenever it actually stored
            # something new, or the cloud attempt never produced a snapshot
            # at all - but a cloud snapshot that merely duplicated the last
            # row is still real, current data, worth reporting as such even
            # if the fallback also turned out to be a duplicate.
            if fallback_stored or snapshot is None:
                snapshot, source, stored, data_points = (
                    fallback_snapshot,
                    "modbus",
                    fallback_stored,
                    fallback_data_points,
                )

    if snapshot is None:
        msg = "Failed to fetch a SolaX realtime snapshot from either the cloud API or local Modbus"
        logger.warning(msg)
        if not quiet:
            print(msg)
        return 1

    detail = f"stored via {source}" if stored else "duplicate reading, not stored"
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
