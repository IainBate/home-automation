#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""ASHP Control-Path Probe (standalone diagnostic - NOT part of the automation).

Answers ASHP.md Open Question 1, the hard blocker on planning any ASHP
automation: **what can actually turn the Ecodan's space-heating (Air Source
Heat Pump) on/off and change its target temperature?** Two candidate paths
exist in this codebase and neither is currently wired up for space heating:

- **MELCloud zone control** (`src/api_clients/melcloud_client.py` is
  tank-only today, but the underlying `pymelcloud` library already fully
  supports Ecodan zone/space-heating control - gated behind a
  `HasThermostatZone1` capability flag only a live API call can read).
- **The Resideo/Honeywell T6R thermostat** (`src/api_clients/resideo_client.py`
  is deliberately read-only today, even though its own docstring notes the
  paired HomeKit accessory technically permits writing Target Temperature /
  Target Heating Cooling State).

It's also possible neither actually works: some Ecodan installations run
space heating purely off a wired room thermostat's call-for-heat signal, in
which case a MELCloud zone write might change what MELCloud *reports* without
the compressor ever actually running.

This script is deliberately kept OUTSIDE the real automation - it is never
imported by any daemon or by hvac_automation_core.py, and it never touches
config/hvac_automation_state.json or any other shared state file. It exists
purely so the control path can be validated against the real household's
hardware before any decision logic is built against an untested assumption
(see ASHP.md's Open Question 1 resolution and this repo's CLAUDE.md-level
"verify against real hardware first" convention, e.g.
hvac_thermostat_automation_plan.md §4.1 for the equivalent Airstage
verification). Once you're happy with what it reports, the actual control
path gets wired into hvac_automation_core.py/ashp_decision_logic.py as a
separate, deliberate integration step - this script's job ends here.

Three read-only reports, each safe to run any time:
    python3 scripts/ashp_control_probe.py --melcloud-zones
    python3 scripts/ashp_control_probe.py --t6r-characteristics
    python3 scripts/ashp_control_probe.py --status   # both of the above

One opt-in write test, guarded by a confirmation prompt (or --yes) and a
required --target choice - nudges ONE setpoint by --nudge-c (default 0.5C),
watches whether the T6R's own calling-for-heat signal actually responds,
then restores the original value:
    python3 scripts/ashp_control_probe.py --write-test --target melcloud
    python3 scripts/ashp_control_probe.py --write-test --target resideo --yes

The T6R write test is the more invasive of the two - it changes what a human
looking at the physical thermostat or the Resideo/Lyric app would see, for
the duration of the test, whereas resideo_client.py has otherwise never
written to this accessory. Read --t6r-characteristics first and confirm
Target Temperature / Target Heating Cooling State actually report the
paired-write ("pw") permission before attempting it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import asyncio
import logging
import pathlib
import time
from typing import Any

from aiohomekit import Controller
from aiohomekit.characteristic_cache import CharacteristicCacheFile
from aiohomekit.model.characteristics import CharacteristicsTypes
from aiohomekit.model.services import ServicesTypes
from aiohomekit.zeroconf import ZeroconfServiceListener
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

from src.api_clients.melcloud_client import (
    MelCloudAuthenticationError,
    MelCloudClient,
    MelCloudConnectionError,
    MelCloudDeviceNotFoundError,
)
from src.api_clients.resideo_client import (
    DEFAULT_PAIRING_ALIAS,
    DEFAULT_PAIRING_FILE,
    DEFAULT_TIMEOUT_SECONDS,
)
from src.config_manager.config_manager import load_static_config
from src.utils.paths import get_project_root

logger = logging.getLogger("ashp_control_probe")

# aiohomekit's CharacteristicsTypes only offers name->UUID lookup; build the
# reverse map once for readable output below.
_CHARACTERISTIC_NAME_BY_UUID = {
    v: k for k, v in vars(CharacteristicsTypes).items() if isinstance(v, str) and not k.startswith("_")
}

# MELCloud's own documented rate limit is "no more than once a minute" for a
# state fetch - see melcloud_client.py's module docstring. The write test
# waits this long before re-reading to give a real chance of seeing the
# change land, not just an immediately-stale cached value.
MELCLOUD_VERIFY_WAIT_SECONDS = 65.0
DEFAULT_NUDGE_C = 0.5


def get_config_path() -> str:
    """Resolve config.yaml relative to the project root, not the process cwd."""
    return str(Path(get_project_root()) / "config.yaml")


def _create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Probe whether MELCloud zone control or T6R HomeKit writes can actually "
            "control the ASHP - read-only unless --write-test is given"
        ),
        epilog="Examples:\n"
        "  python3 scripts/ashp_control_probe.py --status\n"
        "  python3 scripts/ashp_control_probe.py --melcloud-zones\n"
        "  python3 scripts/ashp_control_probe.py --t6r-characteristics\n"
        "  python3 scripts/ashp_control_probe.py --write-test --target melcloud",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    action_group = parser.add_mutually_exclusive_group(required=True)
    action_group.add_argument(
        "--status", action="store_true", help="Run both read-only reports below"
    )
    action_group.add_argument(
        "--melcloud-zones",
        action="store_true",
        help="Read-only: dump MELCloud's Ecodan zone capability flags and live zone state",
    )
    action_group.add_argument(
        "--t6r-characteristics",
        action="store_true",
        help="Read-only: dump the T6R's HomeKit Thermostat characteristics and their permissions",
    )
    action_group.add_argument(
        "--write-test",
        action="store_true",
        help="Opt-in: nudge one real setpoint, verify, then restore it - requires --target",
    )

    parser.add_argument(
        "--target",
        choices=["melcloud", "resideo", "resideo-heat-call"],
        default=None,
        help=(
            "Which control path --write-test exercises (required with --write-test). "
            "resideo-heat-call additionally sets HEATING_COOLING_TARGET=heat with a "
            "target above the current room temperature, to actually try to call for "
            "heat rather than just nudging the temperature target - a materially "
            "bigger, more visible intervention than plain 'resideo'."
        ),
    )
    parser.add_argument(
        "--nudge-c",
        type=float,
        default=DEFAULT_NUDGE_C,
        help=f"Degrees C to nudge the setpoint by during --write-test (default: {DEFAULT_NUDGE_C})",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the --write-test confirmation prompt (for non-interactive use)",
    )
    parser.add_argument(
        "--config", type=str, default=None, help="Path to config.yaml (default: project root)"
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set logging level (default: WARNING)",
    )
    return parser


# --- MELCloud: read-only zone report ---------------------------------------


async def melcloud_zones_report(config_path: str) -> None:
    """Print MELCloud's Ecodan zone capability flags and live zone state. Never writes."""
    print("Connecting to MELCloud...")
    client = MelCloudClient(config_path=config_path)
    try:
        await client.connect()
        device = client.device
        print(f"Connected: {device.name}\n")

        has_zone1 = device.get_device_prop("HasThermostatZone1")
        has_zone2_flag = device.get_device_prop("HasZone2")
        has_thermostat_zone2 = device.get_device_prop("HasThermostatZone2")
        print("Capability flags (from MELCloud's own device config):")
        print(f"  HasThermostatZone1: {has_zone1!r}")
        print(f"  HasZone2: {has_zone2_flag!r}")
        print(f"  HasThermostatZone2: {has_thermostat_zone2!r}")

        print(f"\nDevice-level state:")
        print(f"  status: {device.status!r}")
        print(f"  operation_mode: {device.operation_mode!r}")
        try:
            print(f"  outside_temperature: {device.outside_temperature!r}")
        except Exception as e:  # noqa: BLE001
            print(f"  outside_temperature: <error reading: {e}>")

        zones = device.zones or []
        print(f"\nzones (Zone objects with a thermostat): {len(zones)}")
        if not zones:
            print(
                "  None reported - this Ecodan's space-heating zone(s) don't expose a "
                "MELCloud thermostat, so MELCloud zone writes are NOT a viable ASHP "
                "control path on this installation. See resideo/T6R instead."
            )
        for zone in zones:
            print(f"\n  Zone {zone.zone_index}: {zone.name!r}")
            print(f"    room_temperature: {zone.room_temperature!r}")
            print(f"    target_temperature: {zone.target_temperature!r}")
            print(f"    status: {zone.status!r}")
            print(f"    operation_mode: {zone.operation_mode!r}")
            print(f"    operation_modes: {zone.operation_modes!r}")
            try:
                print(f"    flow_temperature: {zone.flow_temperature!r}")
            except Exception as e:  # noqa: BLE001
                # Documented as sometimes absent from the standard poll response.
                print(f"    flow_temperature: <not in this poll: {e}>")
    finally:
        await client.close()


# --- T6R: read-only characteristics-and-permissions report ------------------


async def _t6r_list_accessories(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Read-only HomeKit connection, mirroring resideo_client.py's own dance exactly."""
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
                    raise ValueError(msg)
                return await asyncio.wait_for(
                    pairing.list_accessories_and_characteristics(), timeout=timeout_seconds
                )
        finally:
            await browser.async_cancel()


def _t6r_thermostat_service(accessories: list[dict[str, Any]]) -> dict[str, Any] | None:
    for accessory in accessories:
        for service in accessory.get("services", []):
            if service.get("type") == ServicesTypes.THERMOSTAT:
                return {"aid": accessory["aid"], "service": service}
    return None


async def _t6r_status_async(config: dict[str, Any]) -> dict[str, Any] | None:
    """Native-async equivalent of resideo_client.fetch_resideo_status().

    That function is sync and wraps its own asyncio.run() internally, which
    cannot be called from inside a running event loop - this whole script
    already runs under one outer asyncio.run(main_async()), so every
    write-test call site below needs this instead, not the sync wrapper.
    """
    try:
        accessories = await _t6r_list_accessories(config)
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error reading Resideo status")
        return None
    found = _t6r_thermostat_service(accessories)
    if found is None:
        return None
    chars = {c["type"]: c.get("value") for c in found["service"].get("characteristics", [])}
    return {
        "calling_for_heat": chars.get(CharacteristicsTypes.HEATING_COOLING_CURRENT) == 1,
        "current_temperature_c": chars.get(CharacteristicsTypes.TEMPERATURE_CURRENT),
        "target_temperature_c": chars.get(CharacteristicsTypes.TEMPERATURE_TARGET),
    }


async def t6r_characteristics_report(config: dict[str, Any]) -> None:
    """Print every T6R Thermostat characteristic with its HomeKit permissions. Never writes."""
    print("Connecting to the T6R via local HomeKit...")
    accessories = await _t6r_list_accessories(config)
    found = _t6r_thermostat_service(accessories)
    if found is None:
        print("No Thermostat service found on the paired accessory.")
        return

    print(f"Connected (accessory aid={found['aid']})\n")
    print("Thermostat characteristics:")
    for c in found["service"].get("characteristics", []):
        name = _CHARACTERISTIC_NAME_BY_UUID.get(c.get("type", ""), c.get("type"))
        writable = "pw" in c.get("perms", [])
        print(f"  {name} (iid={c.get('iid')}): value={c.get('value')!r}")
        print(f"    perms={c.get('perms')!r}  {'<-- WRITABLE' if writable else ''}")

    target_temp = next(
        (
            c
            for c in found["service"]["characteristics"]
            if c.get("type") == CharacteristicsTypes.TEMPERATURE_TARGET
        ),
        None,
    )
    target_mode = next(
        (
            c
            for c in found["service"]["characteristics"]
            if c.get("type") == CharacteristicsTypes.HEATING_COOLING_TARGET
        ),
        None,
    )
    print()
    if target_temp and "pw" in target_temp.get("perms", []):
        print("Target Temperature is paired-writable on this accessory.")
    else:
        print("Target Temperature does NOT report paired-write permission.")
    if target_mode and "pw" in target_mode.get("perms", []):
        print("Target Heating Cooling State is paired-writable on this accessory.")
    else:
        print("Target Heating Cooling State does NOT report paired-write permission.")


# --- Write test: MELCloud zone ----------------------------------------------


async def _melcloud_write_test(config: dict[str, Any], config_path: str, nudge_c: float) -> None:
    print("Connecting to MELCloud...")
    client = MelCloudClient(config_path=config_path)
    try:
        await client.connect()
        zones = client.device.zones or []
        if not zones:
            print("No thermostat zones available - nothing to write-test. See --melcloud-zones.")
            return
        zone = zones[0]
        baseline = zone.target_temperature
        if baseline is None:
            print(f"Zone {zone.name!r} reports no current target_temperature - aborting.")
            return

        direction = -1 if baseline + nudge_c > 30 else 1
        new_target = round(baseline + direction * nudge_c, 1)

        status_before = await _t6r_status_async(config)
        calling_before = status_before.get("calling_for_heat") if status_before else None
        print(f"Baseline: {zone.name!r} target={baseline}C, T6R calling_for_heat={calling_before}")
        print(f"Setting {zone.name!r} target to {new_target}C...")
        await zone.set_target_temperature(new_target)

        print(f"Waiting {MELCLOUD_VERIFY_WAIT_SECONDS:.0f}s before re-checking (MELCloud rate limit)...")
        await asyncio.sleep(MELCLOUD_VERIFY_WAIT_SECONDS)

        await client.device.update()
        confirmed = zone.target_temperature
        status_after = await _t6r_status_async(config)
        calling_after = status_after.get("calling_for_heat") if status_after else None
        print(f"After write: target={confirmed}C, T6R calling_for_heat={calling_after}")

        if confirmed == new_target:
            print("MELCloud CONFIRMS the new target - the write reached the device.")
        else:
            print(
                f"MELCloud still reports {confirmed}C, not {new_target}C - the write did "
                "NOT visibly take effect within the wait window."
            )
        if calling_before != calling_after:
            print(
                "T6R's calling_for_heat CHANGED - meaningful evidence the ASHP itself "
                "responded, not just MELCloud's own reported state."
            )
        else:
            print(
                "T6R's calling_for_heat did not change in this window - inconclusive either "
                "way (a heat call can take longer than this test waited, or was already in "
                "the state it needed to be)."
            )

        print(f"\nRestoring {zone.name!r} to {baseline}C...")
        await zone.set_target_temperature(baseline)
        print("Restored (not re-verified against the 1/min rate limit - check --melcloud-zones later).")
    finally:
        await client.close()


# --- Write test: T6R via HomeKit --------------------------------------------


async def _resideo_write_test(config: dict[str, Any], nudge_c: float) -> None:
    accessories = await _t6r_list_accessories(config)
    found = _t6r_thermostat_service(accessories)
    if found is None:
        print("No Thermostat service found - nothing to write-test.")
        return
    aid = found["aid"]
    chars = found["service"]["characteristics"]
    target_temp_char = next(
        (c for c in chars if c.get("type") == CharacteristicsTypes.TEMPERATURE_TARGET), None
    )
    if target_temp_char is None or "pw" not in target_temp_char.get("perms", []):
        print(
            "Target Temperature is not present or not paired-writable on this accessory - "
            "aborting. See --t6r-characteristics."
        )
        return

    iid = target_temp_char["iid"]
    baseline = target_temp_char["value"]
    direction = -1 if baseline + nudge_c > 30 else 1
    new_target = round(baseline + direction * nudge_c, 1)

    resideo_config = config.get("resideo", {})
    pairing_file = pathlib.Path(resideo_config.get("pairing_file", DEFAULT_PAIRING_FILE))
    alias = resideo_config.get("pairing_alias", DEFAULT_PAIRING_ALIAS)

    zeroconf = AsyncZeroconf()
    controller = Controller(
        async_zeroconf_instance=zeroconf,
        char_cache=CharacteristicCacheFile(pairing_file.parent / "charmap.json"),
    )
    async with zeroconf:
        listener = ZeroconfServiceListener()
        browser = AsyncServiceBrowser(
            zeroconf.zeroconf, ["_hap._tcp.local.", "_hap._udp.local."], listener=listener
        )
        try:
            async with controller:
                controller.load_data(str(pairing_file))
                pairing = controller.aliases[alias]

                async def _read_current() -> tuple[float | None, bool | None]:
                    fresh = await pairing.list_accessories_and_characteristics()
                    fresh_found = _t6r_thermostat_service(fresh)
                    if fresh_found is None:
                        return None, None
                    fresh_chars = {
                        c["type"]: c.get("value")
                        for c in fresh_found["service"].get("characteristics", [])
                    }
                    return (
                        fresh_chars.get(CharacteristicsTypes.TEMPERATURE_TARGET),
                        fresh_chars.get(CharacteristicsTypes.HEATING_COOLING_CURRENT) == 1,
                    )

                calling_before = (await _read_current())[1]
                print(f"Baseline: target={baseline}C, T6R calling_for_heat={calling_before}")
                print(f"Writing target={new_target}C via HomeKit (aid={aid}, iid={iid})...")
                result = await pairing.put_characteristics([(aid, iid, new_target)])
                print(f"put_characteristics result: {result!r}")

                print("Waiting 15s before re-checking...")
                await asyncio.sleep(15)
                confirmed, calling_after = await _read_current()
                print(f"After write: target={confirmed}C, T6R calling_for_heat={calling_after}")

                if confirmed == new_target:
                    print("The T6R CONFIRMS the new target - the write reached the device.")
                else:
                    print(f"The T6R still reports {confirmed}C, not {new_target}C.")
                if calling_before != calling_after:
                    print(
                        "T6R's calling_for_heat CHANGED - meaningful evidence the ASHP "
                        "itself responded."
                    )
                else:
                    print("T6R's calling_for_heat did not change in this window - inconclusive.")

                print(f"\nRestoring target to {baseline}C...")
                restore_result = await pairing.put_characteristics([(aid, iid, baseline)])
                print(f"Restore result: {restore_result!r}")
        finally:
            await browser.async_cancel()


# --- Write test: T6R, mode + target together (actually tries to call for heat) ---


async def _resideo_heat_call_write_test(config: dict[str, Any]) -> None:
    """Set HEATING_COOLING_TARGET=heat with a target above current room temp.

    Unlike _resideo_write_test (a plain TEMPERATURE_TARGET nudge, which was
    found not to stick while mode=off), this is the test that can actually
    show whether the compressor responds - it tries to make the T6R genuinely
    call for heat. Always restores both original values, even on error.
    """
    accessories = await _t6r_list_accessories(config)
    found = _t6r_thermostat_service(accessories)
    if found is None:
        print("No Thermostat service found - nothing to write-test.")
        return
    aid = found["aid"]
    chars = {c["type"]: c for c in found["service"]["characteristics"]}
    target_temp_char = chars.get(CharacteristicsTypes.TEMPERATURE_TARGET)
    target_mode_char = chars.get(CharacteristicsTypes.HEATING_COOLING_TARGET)
    current_temp_char = chars.get(CharacteristicsTypes.TEMPERATURE_CURRENT)
    if target_temp_char is None or "pw" not in target_temp_char.get("perms", []):
        print("Target Temperature not paired-writable - aborting.")
        return
    if target_mode_char is None or "pw" not in target_mode_char.get("perms", []):
        print("Target Heating Cooling State not paired-writable - aborting.")
        return
    if current_temp_char is None or current_temp_char.get("value") is None:
        print("Current room temperature unavailable - aborting (need it to pick a real target).")
        return

    temp_iid = target_temp_char["iid"]
    mode_iid = target_mode_char["iid"]
    baseline_temp = target_temp_char["value"]
    baseline_mode = target_mode_char["value"]
    current_room_temp = current_temp_char["value"]
    call_target = min(round(current_room_temp + 2.0, 1), 30.0)

    resideo_config = config.get("resideo", {})
    pairing_file = pathlib.Path(resideo_config.get("pairing_file", DEFAULT_PAIRING_FILE))
    alias = resideo_config.get("pairing_alias", DEFAULT_PAIRING_ALIAS)

    zeroconf = AsyncZeroconf()
    controller = Controller(
        async_zeroconf_instance=zeroconf,
        char_cache=CharacteristicCacheFile(pairing_file.parent / "charmap.json"),
    )
    async with zeroconf:
        listener = ZeroconfServiceListener()
        browser = AsyncServiceBrowser(
            zeroconf.zeroconf, ["_hap._tcp.local.", "_hap._udp.local."], listener=listener
        )
        try:
            async with controller:
                controller.load_data(str(pairing_file))
                pairing = controller.aliases[alias]

                async def _read_current() -> dict[str, Any]:
                    fresh = await pairing.list_accessories_and_characteristics()
                    fresh_found = _t6r_thermostat_service(fresh)
                    if fresh_found is None:
                        return {}
                    return {
                        c["type"]: c.get("value")
                        for c in fresh_found["service"].get("characteristics", [])
                    }

                print(
                    f"Baseline: mode={baseline_mode}, target={baseline_temp}C, "
                    f"room={current_room_temp}C"
                )
                print(
                    f"Writing mode=1 (heat) + target={call_target}C via HomeKit "
                    f"(aid={aid}, mode_iid={mode_iid}, temp_iid={temp_iid})..."
                )
                try:
                    result = await pairing.put_characteristics(
                        [(aid, mode_iid, 1), (aid, temp_iid, call_target)]
                    )
                    print(f"put_characteristics result: {result!r}")

                    saw_calling = False
                    for i in range(6):
                        await asyncio.sleep(15)
                        elapsed = (i + 1) * 15
                        state = await _read_current()
                        calling = state.get(CharacteristicsTypes.HEATING_COOLING_CURRENT) == 1
                        mode_now = state.get(CharacteristicsTypes.HEATING_COOLING_TARGET)
                        temp_now = state.get(CharacteristicsTypes.TEMPERATURE_TARGET)
                        print(
                            f"  t+{elapsed}s: mode={mode_now}, target={temp_now}C, "
                            f"calling_for_heat={calling}"
                        )
                        if calling:
                            saw_calling = True

                    if saw_calling:
                        print(
                            "\ncalling_for_heat WAS observed True at some point - the T6R "
                            "genuinely tried to call for heat via this write."
                        )
                    else:
                        print(
                            "\ncalling_for_heat was never observed True in this window - "
                            "either the write didn't stick, the ASHP has a startup delay "
                            "longer than tested here, or something else is blocking it."
                        )
                finally:
                    print(f"\nRestoring mode={baseline_mode}, target={baseline_temp}C...")
                    restore_result = await pairing.put_characteristics(
                        [(aid, mode_iid, baseline_mode), (aid, temp_iid, baseline_temp)]
                    )
                    print(f"Restore result: {restore_result!r}")
        finally:
            await browser.async_cancel()


# --- CLI ---------------------------------------------------------------------


async def _run(args: argparse.Namespace, config: dict[str, Any], config_path: str) -> int:
    if args.status or args.melcloud_zones:
        try:
            await melcloud_zones_report(config_path)
        except (MelCloudAuthenticationError, MelCloudConnectionError, MelCloudDeviceNotFoundError) as e:
            print(f"MELCloud error: {e}")
            if not args.status:
                return 1
        print()

    if args.status or args.t6r_characteristics:
        try:
            await t6r_characteristics_report(config)
        except (FileNotFoundError, ValueError, TimeoutError) as e:
            print(f"T6R error: {e}")
            if not args.status:
                return 1

    if args.write_test:
        if args.target is None:
            print("--write-test requires --target melcloud|resideo|resideo-heat-call")
            return 2
        duration = {"melcloud": "65s", "resideo": "15s", "resideo-heat-call": "up to 90s"}[
            args.target
        ]
        extra = (
            " This one also switches the T6R into heat mode with a target above the "
            "current room temperature, to actually try to call for heat - more "
            "consequential than a plain setpoint nudge."
            if args.target == "resideo-heat-call"
            else ""
        )
        print(
            f"\nThis will change a REAL setpoint on your {args.target} device for about "
            f"{duration}, then restore it. It will be visible on the physical "
            f"thermostat/app while the test runs.{extra}"
        )
        if not args.yes:
            reply = input("Type 'yes' to proceed: ")
            if reply.strip().lower() != "yes":
                print("Aborted.")
                return 1
        if args.target == "melcloud":
            await _melcloud_write_test(config, config_path, args.nudge_c)
        elif args.target == "resideo":
            await _resideo_write_test(config, args.nudge_c)
        else:
            await _resideo_heat_call_write_test(config)

    return 0


async def main_async() -> None:
    parser = _create_argument_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )

    config_path = args.config or get_config_path()
    config = load_static_config(config_path)
    if config is None:
        print(f"Failed to load config from {config_path} (see logs above)")
        sys.exit(1)

    try:
        exit_code = await _run(args, config, config_path)
    except KeyboardInterrupt:
        print("\nAborted by user")
        sys.exit(130)
    sys.exit(exit_code)


def main() -> None:
    """Execute main entry point."""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
