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


# ---------------------------------------------------------------------------
# Write path - see module docstring for the verification and
# single-parameter-per-call caveats every function below is built around.
# ---------------------------------------------------------------------------


def _resolve_zones(config: dict[str, Any], zone_name: str | None) -> list[dict[str, Any]] | None:
    """Return the configured zone dict(s) matching zone_name, or all zones if None.

    None (logged) if airstage is disabled, has no zones configured, or
    zone_name doesn't match any configured zone - callers treat None as
    "could not resolve a target," same fail-fast convention as
    fetch_airstage_status.
    """
    airstage_config = config.get("airstage", {})
    if not airstage_config.get("enabled", False):
        logger.error("airstage.enabled is false in config.yaml")
        return None

    zones = airstage_config.get("zones", [])
    if not zones:
        logger.error("airstage.zones is empty - see config.yaml's airstage comments")
        return None

    if zone_name is None:
        return zones

    matches = [z for z in zones if z.get("name", "").lower() == zone_name.lower()]
    if not matches:
        logger.error("No airstage zone named %r in config.yaml", zone_name)
        return None
    return matches


async def _write_and_verify(
    api: ApiLocal, device_id: str, parameter: ACParameter, wire_value: str
) -> None:
    """Write one parameter and confirm the device actually applied it.

    Raises AirstageWriteError if the value never matches after the
    settle/retry window - see module docstring for why neither the HTTP
    response nor a single immediate re-read can be trusted alone.
    """
    await api.set_parameter(device_id, parameter, wire_value)
    for attempt in range(1, WRITE_VERIFY_MAX_ATTEMPTS + 1):
        await asyncio.sleep(WRITE_VERIFY_SETTLE_SECONDS)
        current = await api.get_parameters([parameter])
        if str(current.get(parameter)) == str(wire_value):
            return
        logger.debug(
            "Verify attempt %d/%d: %s on %s still %r, wanted %r",
            attempt,
            WRITE_VERIFY_MAX_ATTEMPTS,
            parameter,
            device_id,
            current.get(parameter),
            wire_value,
        )
    raise AirstageWriteError(
        f"{parameter} on {device_id} did not verify as {wire_value!r} after "
        f"{WRITE_VERIFY_MAX_ATTEMPTS} attempts"
    )


async def _write_zone_parameter(
    zone: dict[str, Any], timeout_seconds: int, parameter: ACParameter, wire_value: str
) -> bool:
    """Open a session, write+verify one parameter on one zone. True on verified success."""
    name = zone.get("name", "Unknown")
    device_id = zone.get("device_id")
    ip_address = zone.get("ip_address")
    if not device_id or not ip_address:
        logger.error("airstage zone %r is missing device_id/ip_address", name)
        return False

    try:
        async with aiohttp.ClientSession() as session:
            api = ApiLocal(
                session=session,
                device_id=device_id,
                ip_address=ip_address,
                timeout_seconds=timeout_seconds,
            )
            await _write_and_verify(api, device_id, parameter, wire_value)
        return True
    except Exception:
        # Circuit Breaker: one zone's write failure must not raise out of a
        # caller writing several zones at once (e.g. set_airstage_mode's
        # "all zones" loop) - it's reported per-zone so the caller can
        # implement the spec's retry/revert without one exception aborting
        # every other zone's write.
        logger.exception("Failed to write %s=%s to Airstage zone %r", parameter, wire_value, name)
        return False


def set_airstage_power(config: dict[str, Any], on: bool, zone_name: str | None = None) -> dict[str, bool]:
    """Turn zone(s) on or off. All zones if zone_name is None.

    Returns {zone_name: True/False} - True only for a zone whose new power
    state was independently re-read and verified, never just a trusted
    HTTP-OK (see module docstring).
    """
    zones = _resolve_zones(config, zone_name)
    if zones is None:
        return {}

    timeout_seconds = config.get("airstage", {}).get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    wire_value = str(BooleanProperty.ON if on else BooleanProperty.OFF)

    async def _run() -> dict[str, bool]:
        results = await asyncio.gather(
            *[_write_zone_parameter(z, timeout_seconds, ACParameter.ONOFF_MODE, wire_value) for z in zones]
        )
        return dict(zip((z["name"] for z in zones), results))

    return asyncio.run(_run())


def set_airstage_mode(config: dict[str, Any], mode: str) -> dict[str, bool]:
    """Set operating mode on ALL configured zones - never a single zone.

    No zone_name parameter, unlike the other set_* functions here: the two
    Airstage units share one outdoor heat exchanger and can only run one
    refrigerant direction at a time, so mode is physically a whole-system
    property, not a per-zone one. This is the one hard constraint this
    module enforces structurally (by omitting the parameter) rather than
    leaving it to the caller.

    Returns {zone_name: True/False}, one entry per configured zone, so a
    caller can implement the spec's retry/revert logic (see plan doc §8.7:
    after any retry/revert, both zones must end up reporting the same mode,
    never left split).
    """
    mode_lower = mode.lower()
    if mode_lower not in MODE_MAP:
        raise ValueError(f"Invalid mode {mode!r}. Valid modes: {', '.join(MODE_MAP)}")

    zones = _resolve_zones(config, None)
    if zones is None:
        return {}

    timeout_seconds = config.get("airstage", {}).get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    wire_value = str(int(MODE_MAP[mode_lower]))

    async def _run() -> dict[str, bool]:
        results = await asyncio.gather(
            *[_write_zone_parameter(z, timeout_seconds, ACParameter.OPERATION_MODE, wire_value) for z in zones]
        )
        return dict(zip((z["name"] for z in zones), results))

    return asyncio.run(_run())


def set_airstage_temperature(
    config: dict[str, Any], temp_c: float, zone_name: str | None = None
) -> dict[str, bool]:
    """Set target temperature (°C, rounded to the nearest 0.5°C). All zones if zone_name is None.

    No mode-range validation here (e.g. heat's 16-30°C) - that's
    hvac_decision_logic.py's job (config.yaml's
    hvac_automation.mode_temp_limits is caller-configurable; this client
    just writes and verifies whatever value it's given, matching this
    module's read functions doing no business-rule interpretation either).

    Returns {zone_name: True/False}, per the same verification convention as
    set_airstage_power/set_airstage_mode.
    """
    zones = _resolve_zones(config, zone_name)
    if zones is None:
        return {}

    timeout_seconds = config.get("airstage", {}).get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    rounded_temp = round(temp_c * 2) / 2
    wire_value = str(int(rounded_temp * 10))

    async def _run() -> dict[str, bool]:
        results = await asyncio.gather(
            *[_write_zone_parameter(z, timeout_seconds, ACParameter.TARGET_TEMPERATURE, wire_value) for z in zones]
        )
        return dict(zip((z["name"] for z in zones), results))

    return asyncio.run(_run())


def set_airstage_minimum_heat(
    config: dict[str, Any], enabled: bool, zone_name: str | None = None
) -> dict[str, bool]:
    """Enable or disable Minimum Heat mode. All zones if zone_name is None.

    Per the spec, minimum heat is exclusively used in Away mode and bypasses
    all normal temperature validation - enforcing that policy is
    hvac_decision_logic.py's job, not this client's.

    Returns {zone_name: True/False}, per the same verification convention as
    the other set_* functions here.
    """
    zones = _resolve_zones(config, zone_name)
    if zones is None:
        return {}

    timeout_seconds = config.get("airstage", {}).get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    wire_value = str(BooleanProperty.ON if enabled else BooleanProperty.OFF)

    async def _run() -> dict[str, bool]:
        results = await asyncio.gather(
            *[_write_zone_parameter(z, timeout_seconds, ACParameter.MINIMUM_HEAT, wire_value) for z in zones]
        )
        return dict(zip((z["name"] for z in zones), results))

    return asyncio.run(_run())
