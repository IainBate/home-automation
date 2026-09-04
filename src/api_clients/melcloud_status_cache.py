"""Shared read/write access to the cached MELCloud hot water tank status.

Mirrors src/api_clients/ohme_status_cache.py's fix for the same problem:
before this existed, status_collector.py's dashboard poller made its own
direct MELCloud call on every poll_interval_seconds tick (45s by default -
see config.yaml's web_interface comments), on top of the ~9 separate calls
scripts/hotwater_automation_core.py's own force-heat/revert/legionella
checks already make each hour for their own decisions. pymelcloud's own
fetch_device_state() documents it "should not be called more than once a
minute" (quoted in melcloud_client.py's module comment) - the dashboard's
45-second cadence was already violating that, live, before this cache
existed.

Unlike Ohme, this doesn't need a dedicated polling daemon: the force-heat
check in hotwater_automation_core.py already fetches tank status every
hotwater_automation.poll_interval_seconds (10 min default) for its own
purposes, so writing the same fetch here is a free byproduct, not a new
API call. That single write site is enough to keep the cache within a
poll_interval_seconds of fresh at all times whenever hot water automation
is enabled and running - the revert/legionella checks (hourly) also fetch
tank status but don't need to write here too, since the force-heat check's
10-minute cadence already dominates.

Deliberately a plain cache file rather than any coupling between the
processes: a missing or stale cache reads as "no cached answer", and the
dashboard falls back to its own direct MELCloud call - so the hot water
daemon being down or hot water automation being disabled degrades the
dashboard to exactly the behaviour it had before this cache existed, rather
than breaking it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from src.utils.paths import get_melcloud_status_path
from src.utils.state_store import read_json_state, write_json_atomic

logger = logging.getLogger(__name__)

# How old a cached reading may be before readers ignore it and fetch their
# own. Comfortably longer than the force-heat check's 10-minute write
# cadence (so an ordinary check doesn't cause a fallback stampede) but tight
# enough that a stalled/disabled hot water daemon is noticed within about
# one and a half cycles rather than serving very stale tank data as if it
# were current.
DEFAULT_MAX_AGE_SECONDS = 900.0


def serialize_status(status: dict[str, Any]) -> dict[str, Any]:
    """Flatten one MelCloudClient.get_tank_status() result into JSON-safe,
    dashboard-ready fields.

    Field names match what status_collector.py's _collect_hot_water already
    returns (tank_temperature_c, target_tank_temperature_c, power_on, ...)
    rather than get_tank_status()'s own raw names, so the cache-hit branch
    there is a direct field copy - same approach as ohme_status_cache.py's
    serialize_status(). Only the fields _collect_hot_water actually reads
    are kept; automation-state fields (force_heat_active, legionella, ...)
    are read separately from hotwater_automation_state.json regardless of
    cache hit/miss, since they aren't part of this MELCloud fetch at all.
    """
    operation_mode = status.get("operation_mode")
    tank_status = status.get("status")
    return {
        "tank_temperature_c": status.get("tank_temperature"),
        "target_tank_temperature_c": status.get("target_tank_temperature"),
        "operation_mode": operation_mode.value if operation_mode is not None else None,
        "status": tank_status.value if tank_status is not None else None,
        "power_on": status.get("power"),
        "holiday_mode": status.get("holiday_mode"),
    }


def write_status_cache(status: dict[str, Any]) -> None:
    """Write one fetched tank status to the cache file, stamped with the fetch time."""
    record = {"fetched_at": datetime.now(tz=UTC).isoformat(), **serialize_status(status)}
    write_json_atomic(get_melcloud_status_path(), record)


def read_fresh_status(max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> dict[str, Any] | None:
    """Return the cached status if it exists and is recent enough, else None.

    None means "no usable cached answer" for every reason - file missing
    (hot water automation disabled, or the force-heat check hasn't run yet),
    unparseable, no timestamp, or too old (the hot water daemon is stopped
    or wedged). Callers must treat None as "fall back to a direct MELCloud
    call", never as any particular tank state.
    """
    record = read_json_state(get_melcloud_status_path())
    if not record:
        return None

    fetched_at_str = record.get("fetched_at")
    if not fetched_at_str:
        return None

    try:
        fetched_at = datetime.fromisoformat(fetched_at_str)
    except (TypeError, ValueError):
        logger.warning("MELCloud status cache has an unparseable fetched_at, ignoring it")
        return None
    if fetched_at.tzinfo is None:
        return None

    age_seconds = (datetime.now(tz=UTC) - fetched_at).total_seconds()
    if age_seconds > max_age_seconds:
        logger.info(
            "MELCloud status cache is %.0fs old (limit %.0fs) - falling back to a direct read",
            age_seconds,
            max_age_seconds,
        )
        return None

    return record
