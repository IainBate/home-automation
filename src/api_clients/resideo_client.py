"""Resideo (Honeywell Home) T6R thermostat client - read-only, via local HomeKit (aiohomekit).

This household's T6R is a Resideo "Lyric" device, not a genuine Evohome
system, despite "T6R" matching the name of Evohome's own round zone
controller - confirmed 2026-09-02 by testing real credentials against
evohome-async's TCC v2 backend (tccna.resideo.com), which correctly
rejected them. Lyric has no local API of its own, but this WiFi-connected
unit supports Apple HomeKit (HAP-over-IP) locally, with no Honeywell/
Resideo account, cloud, or developer-portal API key involved - the path
this module uses, via `aiohomekit` (the same library Home Assistant's
local "HomeKit Controller" integration uses).

Pairing is a one-time manual step done outside this module (see the
~/heating_automation project, where it was first established) - this
module only ever reads from an already-paired accessory, using the
credentials cached at resideo.pairing_file (default
~/.local/share/aiohomekit/pairing.json on the Pi). It never writes to
Target Temperature or Target Heating Cooling State, even though the
paired accessory technically permits it: controlling the ASHP via the T6R
is deliberately out of scope for now (see home_automation's CLAUDE.md).
The T6R only ever reports "off" or "heat" as its target mode - confirmed
via the accessory's own valid-values metadata, it has no Cool/Auto option
(single-zone, heat-only system).

Read-only: this module only ever calls aiohomekit's read methods (never
set_characteristics), so it carries no risk of changing the thermostat's
settings or affecting the ASHP it's wired to.
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

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_PAIRING_ALIAS = "heating-automation"
DEFAULT_PAIRING_FILE = pathlib.Path.home() / ".local/share/aiohomekit/pairing.json"

# This accessory's Target Heating Cooling State only ever permits 0/1 (see
# module docstring) - 2/3 are included so an unexpected future value is
# still rendered sensibly instead of falling through to a raw int.
_HEATING_COOLING_STATE_NAMES = {0: "off", 1: "heat", 2: "cool", 3: "auto"}


def fetch_resideo_status(config: dict[str, Any]) -> dict[str, Any] | None:
    """Read-only Resideo/T6R snapshot: current temp, target temp, mode, calling-for-heat.

    Args:
        config: Full static config - reads its "resideo" section (enabled,
            optional pairing_file/pairing_alias/timeout_seconds overrides).

    Returns:
        Dict with "device_name", "mode" ("off" or "heat" for this device),
        "calling_for_heat" (bool - whether the thermostat's own heating
        circuit is actively calling for heat right now, distinct from
        "mode"), "current_temperature_c", "target_temperature_c" - or None
        if disabled, not yet paired, or anything failed (fail-fast, matches
        this codebase's other cloud/local clients - a broad except here,
        not just the connection's own errors, since a caller collecting
        several subsystems in one pass (see src/dashboard/status_collector.py)
        must not have one integration's unexpected exception blank the
        whole snapshot).
    """
    try:
        return _fetch_resideo_status_unsafe(config)
    except Exception:
        # Circuit Breaker: see docstring above.
        logger.exception("Unexpected error reading Resideo status")
        return None


def _fetch_resideo_status_unsafe(config: dict[str, Any]) -> dict[str, Any] | None:
    resideo_config = config.get("resideo", {})
    if not resideo_config.get("enabled", False):
        return None

    pairing_file = pathlib.Path(resideo_config.get("pairing_file", DEFAULT_PAIRING_FILE))
    alias = resideo_config.get("pairing_alias", DEFAULT_PAIRING_ALIAS)
    if not pairing_file.exists():
        logger.error(
            "Resideo pairing file %s not found - the T6R must be paired via aiohomekit "
            "first (a one-time manual step - see resideo_client.py's module docstring)",
            pairing_file,
        )
        return None

    timeout_seconds = resideo_config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    return asyncio.run(_fetch_status_async(pairing_file, alias, timeout_seconds))


async def _fetch_status_async(
    pairing_file: pathlib.Path, alias: str, timeout_seconds: float
) -> dict[str, Any] | None:
    zeroconf = AsyncZeroconf()
    controller = Controller(
        async_zeroconf_instance=zeroconf,
        char_cache=CharacteristicCacheFile(pairing_file.parent / "charmap.json"),
    )
    async with zeroconf:
        # aiohomekit refuses to start a Controller at all without a live mDNS
        # browser registered for the HAP service types, even just to read an
        # already-paired accessory via its cached IP/port (raises
        # TransportNotSupportedError otherwise) - not obvious from the public
        # API, found by testing directly against this device.
        listener = ZeroconfServiceListener()
        browser = AsyncServiceBrowser(
            zeroconf.zeroconf,
            ["_hap._tcp.local.", "_hap._udp.local."],
            listener=listener,
        )
        try:
            async with controller:
                controller.load_data(str(pairing_file))
                pairing = controller.aliases.get(alias)
                if pairing is None:
                    logger.error("Resideo pairing alias %r not found in %s", alias, pairing_file)
                    return None

                accessories = await asyncio.wait_for(
                    pairing.list_accessories_and_characteristics(),
                    timeout=timeout_seconds,
                )
        finally:
            await browser.async_cancel()

    return _parse_thermostat_status(accessories)


def _parse_thermostat_status(accessories: list[dict[str, Any]]) -> dict[str, Any] | None:
    for accessory in accessories:
        for service in accessory.get("services", []):
            if service.get("type") != ServicesTypes.THERMOSTAT:
                continue

            chars = {c["type"]: c.get("value") for c in service.get("characteristics", [])}
            current_state = chars.get(CharacteristicsTypes.HEATING_COOLING_CURRENT)
            target_state = chars.get(CharacteristicsTypes.HEATING_COOLING_TARGET)

            return {
                "device_name": chars.get(CharacteristicsTypes.NAME) or "T6R Thermostat",
                "mode": _HEATING_COOLING_STATE_NAMES.get(target_state, str(target_state)),
                "calling_for_heat": current_state == 1,
                "current_temperature_c": chars.get(CharacteristicsTypes.TEMPERATURE_CURRENT),
                "target_temperature_c": chars.get(CharacteristicsTypes.TEMPERATURE_TARGET),
            }

    logger.warning("Paired HomeKit accessory has no Thermostat service")
    return None
