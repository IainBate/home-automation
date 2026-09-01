"""Fujitsu Airstage ductless heat pump client - read-only status via local network control.

Uses the unofficial `pyairstage` library's local-network mode: talks directly
to each unit over the LAN using its IP address and a device ID derived from
its MAC address (colons/hyphens removed) - see config.yaml's airstage
section for how to find both. No Fujitsu cloud account, password, or token
involved, unlike this project's Ohme/MELCloud clients - so there's no auth
to expire, though the local protocol is still unofficial/reverse-engineered
(same risk category as Ohme's monkey-patch), and could break on a Fujitsu
firmware update.

Supports multiple independent zones (config.yaml's airstage.zones list) -
each is fetched and reported independently, so one unreachable zone doesn't
hide the others.

Read-only: this module never calls pyairstage's turn_on/turn_off/
set_operation_mode/set_target_temperature/etc, so it carries no risk of
changing zone settings, and no risk to battery_mode_daemon.py or
hotwater_mode_daemon.py either (entirely separate hardware/network path).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from pyairstage.airstageAC import AirstageAC
from pyairstage.airstageApi import ApiLocal

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10


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
