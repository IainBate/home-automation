"""ASHP control via the T6R (Resideo/Honeywell Lyric) thermostat over local HomeKit.

Internal backend for `ashp_client.py` - see that module's docstring for why
this lives behind a swappable-backend seam rather than being called
directly. Confirmed working against the real household T6R 2026-09-09 via
`scripts/ashp_control_probe.py`: writing HEATING_COOLING_TARGET=heat
*together with* a TEMPERATURE_TARGET above the current room temperature
reliably made the thermostat's own `calling_for_heat` signal go true and
hold for the full 90s test window. A target-only write (mode left at "off")
was tried first and found NOT to stick at all - the device appears to
silently ignore a Target Temperature write while its own mode is "off",
which is why every write here always sets both characteristics together,
never just one.

Deliberately narrow: only the two writes `ashp_client.py`'s public API
needs (call for heat at a target, or turn off), each write-then-verify
against the household's real hardware - mirroring
`airstage_client.py`'s own `_write_and_verify` shape (settle, re-read,
retry a bounded number of times, raise if it never confirms) rather than
inventing a new pattern. Everything else about the T6R - reading room
temperature, dashboard status - stays in `resideo_client.py`, unchanged
and still strictly read-only; this is the one deliberate exception, scoped
to exactly the two characteristics ASHP control needs to write.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import Any

from aiohomekit import Controller
from aiohomekit.characteristic_cache import CharacteristicCacheFile
from aiohomekit.model.characteristics import CharacteristicsTypes
from aiohomekit.model.services import ServicesTypes
from aiohomekit.zeroconf import ZeroconfServiceListener
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

from .resideo_client import DEFAULT_PAIRING_ALIAS, DEFAULT_PAIRING_FILE, DEFAULT_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# This accessory's Target Heating Cooling State only ever permits 0 (off) /
# 1 (heat) - see resideo_client.py's own module docstring (single-zone,
# heat-only system, no Cool/Auto option).
_HEATING_COOLING_TARGET_OFF = 0
_HEATING_COOLING_TARGET_HEAT = 1

# Settle-then-verify, same shape and same order of magnitude as
# airstage_client.py's WRITE_VERIFY_SETTLE_SECONDS/WRITE_VERIFY_MAX_ATTEMPTS
# (3s x 5 = 15s worst case there); the probe script's manual test confirmed
# the T6R's write lands well within a 15s window, so this matches that
# proven timing rather than guessing at a tighter or looser one.
WRITE_VERIFY_SETTLE_SECONDS = 3.0
WRITE_VERIFY_MAX_ATTEMPTS = 5


class AshpT6rWriteError(Exception):
    """A write to the T6R did not verify as applied within the retry window."""


def set_ashp_heat_call(config: dict[str, Any], target_temp_c: float) -> bool:
    """Set the T6R calling for heat at target_temp_c. Write-then-verify.

    Returns:
        True if both HEATING_COOLING_TARGET=heat and TEMPERATURE_TARGET
        confirmed as target_temp_c within the retry window, False on any
        failure (connection, pairing, or verify timeout) - never raises,
        matching this codebase's fail-fast-to-None/False convention for
        hardware calls (see resideo_client.py's own Circuit Breaker note).

    """
    try:
        return asyncio.run(
            _set_state_async(config, mode=_HEATING_COOLING_TARGET_HEAT, target_temp_c=target_temp_c)
        )
    except Exception:
        logger.exception("Unexpected error setting ASHP heat call via T6R")
        return False


def set_ashp_off(config: dict[str, Any]) -> bool:
    """Set the T6R to off. Write-then-verify.

    Leaves TEMPERATURE_TARGET at whatever it currently reads (read fresh,
    then rewritten unchanged alongside mode=off) rather than picking a new
    value - mode=off is what actually stops the heat call; the target
    value is irrelevant once off, so there is no need to invent one, and
    always writing both characteristics together matches the one write
    shape confirmed to reliably stick (see module docstring).

    Returns:
        True if HEATING_COOLING_TARGET confirmed as off within the retry
        window, False on any failure - never raises.

    """
    try:
        return asyncio.run(_set_state_async(config, mode=_HEATING_COOLING_TARGET_OFF, target_temp_c=None))
    except Exception:
        logger.exception("Unexpected error setting ASHP off via T6R")
        return False


async def _connect(config: dict[str, Any]):  # noqa: ANN201 - returns an aiohomekit Pairing
    """Shared connection dance - same as resideo_client.py's _fetch_status_async, but a
    caller-held connection (not a single read) since callers here also need to write."""
    resideo_config = config.get("resideo", {})
    pairing_file = pathlib.Path(resideo_config.get("pairing_file", DEFAULT_PAIRING_FILE))
    alias = resideo_config.get("pairing_alias", DEFAULT_PAIRING_ALIAS)
    if not pairing_file.exists():
        msg = f"Resideo pairing file {pairing_file} not found - the T6R must be paired first"
        raise FileNotFoundError(msg)
    timeout_seconds = resideo_config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)

    zeroconf = AsyncZeroconf()
    controller = Controller(
        async_zeroconf_instance=zeroconf,
        char_cache=CharacteristicCacheFile(pairing_file.parent / "charmap.json"),
    )
    return zeroconf, controller, pairing_file, alias, timeout_seconds


def _thermostat_service(accessories: list[dict[str, Any]]) -> dict[str, Any] | None:
    for accessory in accessories:
        for service in accessory.get("services", []):
            if service.get("type") == ServicesTypes.THERMOSTAT:
                return {"aid": accessory["aid"], "service": service}
    return None


async def _set_state_async(
    config: dict[str, Any], *, mode: int, target_temp_c: float | None
) -> bool:
    zeroconf, controller, pairing_file, alias, timeout_seconds = await _connect(config)
    async with zeroconf:
        listener = ZeroconfServiceListener()
        browser = AsyncServiceBrowser(
            zeroconf.zeroconf, ["_hap._tcp.local.", "_hap._udp.local."], listener=listener
        )
        try:
            async with controller:
                controller.load_data(str(pairing_file))
                pairing = controller.aliases.get(alias)
                if pairing is None:
                    msg = f"Resideo pairing alias {alias!r} not found in {pairing_file}"
                    raise ValueError(msg)  # noqa: TRY301

                accessories = await asyncio.wait_for(
                    pairing.list_accessories_and_characteristics(), timeout=timeout_seconds
                )
                found = _thermostat_service(accessories)
                if found is None:
                    logger.error("No Thermostat service found on the paired T6R accessory")
                    return False

                chars = {c["type"]: c for c in found["service"]["characteristics"]}
                mode_char = chars.get(CharacteristicsTypes.HEATING_COOLING_TARGET)
                temp_char = chars.get(CharacteristicsTypes.TEMPERATURE_TARGET)
                if mode_char is None or temp_char is None:
                    logger.error("T6R accessory is missing an expected Thermostat characteristic")
                    return False
                if "pw" not in mode_char.get("perms", []) or "pw" not in temp_char.get("perms", []):
                    logger.error("T6R Thermostat characteristics are not paired-writable")
                    return False

                aid = found["aid"]
                mode_iid = mode_char["iid"]
                temp_iid = temp_char["iid"]
                # Always write both together - a target-only write was
                # found not to stick while mode was "off" (see module
                # docstring); "off" keeps whatever target is already set.
                target = target_temp_c if target_temp_c is not None else temp_char["value"]

                await pairing.put_characteristics([(aid, mode_iid, mode), (aid, temp_iid, target)])

                for attempt in range(1, WRITE_VERIFY_MAX_ATTEMPTS + 1):
                    await asyncio.sleep(WRITE_VERIFY_SETTLE_SECONDS)
                    fresh = await pairing.list_accessories_and_characteristics()
                    fresh_found = _thermostat_service(fresh)
                    fresh_chars = (
                        {c["type"]: c.get("value") for c in fresh_found["service"]["characteristics"]}
                        if fresh_found
                        else {}
                    )
                    if (
                        fresh_chars.get(CharacteristicsTypes.HEATING_COOLING_TARGET) == mode
                        and fresh_chars.get(CharacteristicsTypes.TEMPERATURE_TARGET) == target
                    ):
                        return True
                    logger.debug(
                        "ASHP T6R verify attempt %d/%d: mode=%r target=%r, wanted mode=%r target=%r",
                        attempt,
                        WRITE_VERIFY_MAX_ATTEMPTS,
                        fresh_chars.get(CharacteristicsTypes.HEATING_COOLING_TARGET),
                        fresh_chars.get(CharacteristicsTypes.TEMPERATURE_TARGET),
                        mode,
                        target,
                    )

                logger.error(
                    "ASHP T6R write (mode=%r target=%r) did not verify after %d attempts",
                    mode,
                    target,
                    WRITE_VERIFY_MAX_ATTEMPTS,
                )
                return False
        finally:
            await browser.async_cancel()
