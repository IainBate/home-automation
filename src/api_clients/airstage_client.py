"""Fujitsu Airstage ductless heat pump client - status and control via local network.

Uses the unofficial `pyairstage` library's local-network mode: talks directly
to each unit over the LAN using its IP address and a device ID derived from
its MAC address (colons/hyphens removed) - see config.yaml's airstage
section for how to find both. No Fujitsu cloud account, password, or token
involved, unlike this project's Ohme/MELCloud clients - so there's no auth
to expire, though the local protocol is still unofficial/reverse-engineered
(same risk category as Ohme's monkey-patch), and could break on a Fujitsu
firmware update.

Supports multiple independent zones (config.yaml's airstage.zones list) -
each is fetched/written independently, so one unreachable zone doesn't hide
or block the others.

Read path (`fetch_airstage_status`) vs. write path: `fetch_airstage_status`
never calls a write function, so the dashboard (`status_collector.py`, which
only ever calls the read function) carries no risk of changing zone settings
and no risk to battery_mode_daemon.py/hotwater_mode_daemon.py - that
safety property now depends on caller discipline (only `hvac_mode_daemon.py`
should call the `set_*` functions below), not on this whole module being
read-only, now that write support has been added (2026-09-04, for the HVAC
automation plan - see docs/hvac_thermostat_automation_plan.md).

Write verification is mandatory, not optional - proven empirically 2026-09-04
(plan doc §4.1): this device's /SetParam endpoint returns `result: OK` and
echoes back the requested value even for writes it silently discards, AND a
write that DID succeed can take a few seconds to propagate, so an immediate
re-read can also show a stale value. Every write below is followed by a
settle/retry re-read loop against the raw parameter, never trusting the HTTP
response or pyairstage's own optimistic in-memory cache (`AirstageAC`'s
`_set_device_parameter` updates its cache from the value it *sent*, not a
verified read-back - so this module talks to `ApiLocal`/`ACParameter`
directly for writes rather than going through `AirstageAC`'s set_* methods).

Also proven empirically 2026-09-04 (plan doc §4.1): the local /SetParam
endpoint only honours ONE parameter key per call, despite accepting a dict
that could carry several - a "mode + temperature in one API call" request
is silently discarded in full. `set_airstage_mode`/`set_airstage_temperature`
are therefore separate, independently-verified calls, not a single fused
"set both" function: composing them (e.g. "set mode on both zones, then each
zone's target temperature, with retry/revert if any zone's write doesn't
verify") is `hvac_decision_logic.py`/`hvac_mode_daemon.py`'s job, matching
this codebase's existing separation between client I/O and decision logic
(see hotwater_decision_logic.py).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from pyairstage.airstageAC import AirstageAC
from pyairstage.airstageApi import ApiLocal
from pyairstage.constants import ACParameter, BooleanProperty, OperationMode

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10

# Write verification: see module docstring. A write can take a few seconds to
# propagate, so the first read-back can show the stale value even for a
# genuinely successful write - hence a settle/retry loop, not one check.
WRITE_VERIFY_SETTLE_SECONDS = 3
WRITE_VERIFY_MAX_ATTEMPTS = 5

MODE_MAP: dict[str, OperationMode] = {
    "auto": OperationMode.AUTO,
    "cool": OperationMode.COOL,
    "dry": OperationMode.DRY,
    "fan": OperationMode.FAN,
    "heat": OperationMode.HEAT,
}


class AirstageWriteError(Exception):
    """A write's HTTP response was OK but the re-verified device state never matched."""


def fetch_airstage_status(config: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Read-only snapshot of every configured Airstage zone's mode and temperatures.

    Args:
        config: Full static config - reads its "airstage" section.

    Returns:
        One dict per configured zone, each with "name" plus either
        ("available": True, "mode", "current_temperature_c",
        "target_temperature_c", "outdoor_temperature_c") or ("available":
        False, "error") - or None if disabled or no zones are configured at
        all (fail-fast, matches this codebase's other hardware clients).
        A single zone being unreachable never hides the others.

    """
    airstage_config = config.get("airstage", {})
    if not airstage_config.get("enabled", False):
        return None

    zones = airstage_config.get("zones", [])
    if not zones:
        logger.error("airstage.zones is empty - see config.yaml's airstage comments")
        return None

    timeout_seconds = airstage_config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    return [_fetch_zone_status(zone, timeout_seconds) for zone in zones]


def _fetch_zone_status(zone: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    name = zone.get("name", "Unknown")
    device_id = zone.get("device_id")
    ip_address = zone.get("ip_address")
    if not device_id or not ip_address:
        logger.error("airstage zone %r is missing device_id/ip_address", name)
        return {"name": name, "available": False, "error": "Zone misconfigured"}

    try:
        status = asyncio.run(_fetch_status_async(device_id, ip_address, timeout_seconds))
    except Exception:
        # Circuit Breaker: a local-network hiccup or library quirk in one
        # zone must not propagate to the dashboard poll loop or hide the
        # other zones' status.
        logger.exception("Unexpected error reading Airstage zone %r", name)
        return {"name": name, "available": False, "error": "Could not read from Airstage unit"}

    return {"name": name, "available": True, **status}


async def _fetch_status_async(
    device_id: str, ip_address: str, timeout_seconds: int
) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        api = ApiLocal(
            session=session,
            device_id=device_id,
            ip_address=ip_address,
            timeout_seconds=timeout_seconds,
        )
        devices = await api.get_devices()

        zone = AirstageAC(dsn=device_id, api=api)
        zone.refresh_parameters(devices[device_id])

        return {
            "mode": zone.get_operating_mode().value,
            "current_temperature_c": zone.get_display_temperature(),
            "target_temperature_c": zone.get_target_temperature(),
            "outdoor_temperature_c": zone.get_outdoor_temperature(),
        }
