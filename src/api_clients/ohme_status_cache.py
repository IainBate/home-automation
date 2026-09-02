"""Shared read/write access to the cached Ohme charger status.

Three separate automations need to know "is the car charging right now":
scripts/battery_mode_daemon.py (every ~60s), scripts/hotwater_automation_core.py
(every 10-15 min) and the dashboard's own poller (every 45s). Each used to
construct its own OhmeEVClient and call connect(), which performs a full
Firebase login every single time - roughly 3,000 logins a day between them,
against a third-party auth endpoint, from three uncoordinated processes.
That is the most likely explanation for the ~9-minute Ohme outage recorded
in status_collector.py's log-health tuning comment.

scripts/ohme_status_daemon.py now does that polling once, from a single
long-lived session (one login per daemon start, not per poll), and writes
the result here. Everything else reads this file.

Deliberately a plain cache file rather than any coupling between those
processes: every reader treats a missing or stale cache as "no cached
answer" and falls back to its own direct Ohme call, so the poller being
down or not yet deployed degrades each consumer to exactly the behaviour it
had before this existed, rather than breaking it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from src.utils.paths import get_ohme_status_path
from src.utils.state_store import read_json_state, write_json_atomic

logger = logging.getLogger(__name__)

# How old a cached reading may be before readers ignore it and fetch their
# own. Comfortably longer than the poller's default 30s cadence (so an
# ordinary slow poll doesn't cause a fallback stampede) but well under the
# battery daemon's own 60s hardware cycle, so a genuinely dead poller is
# noticed within one cycle rather than silently freezing the charging signal.
DEFAULT_MAX_AGE_SECONDS = 150.0


def serialize_status(status: dict[str, Any]) -> dict[str, Any]:
    """Flatten one OhmeEVClient.get_charger_status() result into JSON-safe fields.

    Only the fields consumers actually read are kept - the raw status dict
    contains enum objects (status/mode) that don't survive a JSON round trip,
    plus a lot of detail nothing here uses.
    """
    return {
        "plugged_in": status.get("plugged_in"),
        "status": status["status"].value if status.get("status") else None,
        "mode": status["mode"].value if status.get("mode") else None,
        "power_watts": status.get("power_watts"),
        "battery_percent": status.get("battery_percent"),
        "target_soc": status.get("target_soc"),
        "current_vehicle": status.get("current_vehicle"),
    }


def write_status_cache(status: dict[str, Any]) -> None:
    """Write one polled status to the cache file, stamped with the fetch time."""
    record = {"fetched_at": datetime.now(tz=UTC).isoformat(), **serialize_status(status)}
    write_json_atomic(get_ohme_status_path(), record)


def read_fresh_status(max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> dict[str, Any] | None:
    """Return the cached status if it exists and is recent enough, else None.

    None means "no usable cached answer" for every reason - file missing (the
    poller isn't deployed), unparseable, no timestamp, or too old (the poller
    is stopped or wedged). Callers must treat None as "fall back to a direct
    Ohme call", never as "the car isn't charging": silently reporting "not
    charging" would make the battery daemon drop out of FORCE_CHARGE and the
    hot water automation miss its trigger.
    """
    record = read_json_state(get_ohme_status_path())
    if not record:
        return None

    fetched_at_str = record.get("fetched_at")
    if not fetched_at_str:
        return None

    try:
        fetched_at = datetime.fromisoformat(fetched_at_str)
    except (TypeError, ValueError):
        logger.warning("Ohme status cache has an unparseable fetched_at, ignoring it")
        return None
    if fetched_at.tzinfo is None:
        return None

    age_seconds = (datetime.now(tz=UTC) - fetched_at).total_seconds()
    if age_seconds > max_age_seconds:
        logger.info(
            "Ohme status cache is %.0fs old (limit %.0fs) - falling back to a direct read",
            age_seconds,
            max_age_seconds,
        )
        return None

    return record
