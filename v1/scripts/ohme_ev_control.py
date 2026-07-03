#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Ohme EV Charger Control Script.

Command-line interface for controlling Ohme Home Pro EV charger.

Usage:
    python3 scripts/ohme_ev_control.py --status
    python3 scripts/ohme_ev_control.py --status --verbose
    python3 scripts/ohme_ev_control.py --pause
    python3 scripts/ohme_ev_control.py --resume
    python3 scripts/ohme_ev_control.py --max-charge
    python3 scripts/ohme_ev_control.py --smart-charge
    python3 scripts/ohme_ev_control.py --set-target 80
    python3 scripts/ohme_ev_control.py --set-target-time 07:30
    python3 scripts/ohme_ev_control.py --set-price-cap 0.15
    python3 scripts/ohme_ev_control.py --list-vehicles
    python3 scripts/ohme_ev_control.py --select-vehicle "BMW iX3"

Testing Commands:
    python3 scripts/ohme_ev_control.py --test-library-pause     # Library method (NO AppCheck header)
    python3 scripts/ohme_ev_control.py --test-library-resume    # Library method (NO AppCheck header)
    python3 scripts/ohme_ev_control.py --test-appcheck-pause    # WITH expired AppCheck token
    python3 scripts/ohme_ev_control.py --test-appcheck-resume   # WITH expired AppCheck token
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

import argparse
import asyncio
import contextlib
import logging
import traceback
from datetime import datetime
from typing import Any

from src.api_clients.ohme_ev_client import (
    OhmeAuthenticationError,
    OhmeChargerStatus,
    OhmeConnectionError,
    OhmeEVClient,
    OhmeNotPluggedInError,
)

# Validation constants
SOC_MAX_PERCENT = 100
HOUR_MAX = 23
MINUTE_MAX = 59


def _get_status_emoji(status: OhmeChargerStatus) -> str:
    """Get emoji for OhmeChargerStatus enum."""
    emoji_map = {
        OhmeChargerStatus.CHARGING: "[CHG]",
        OhmeChargerStatus.PLUGGED_IN: "[PLUG]",
        OhmeChargerStatus.UNPLUGGED: "[OFF]",
        OhmeChargerStatus.PAUSED: "[PAUSE]",
        OhmeChargerStatus.FINISHED: "[DONE]",
        OhmeChargerStatus.PENDING_APPROVAL: "[WAIT]",
        OhmeChargerStatus.UNKNOWN: "[?]",
    }
    return emoji_map.get(status, "[?]")


def _format_power_line(status: dict[str, Any]) -> str:
    """Format power and CT clamp information."""
    output = f"Power: {status['power_watts']:.0f}W ({status['power_amps']:.1f}A"
    if status["power_volts"]:
        output += f" @ {status['power_volts']}V"
    output += ")"

    if status["ct_connected"]:
        output += f" | CT Clamp: {status['ct_amps']:.1f}A"

    return output


def _format_battery_line(status: dict[str, Any]) -> str:
    """Format battery and energy information."""
    output = f"Battery: {status['battery_percent']}%"
    if status["energy_wh"] > 0:
        output += f" | Energy: {status['energy_wh']:.1f}Wh ({status['energy_wh'] / 1000:.2f}kWh)"
    return output


def _format_target_line(status: dict[str, Any]) -> str:
    """Format target and schedule information."""
    output = f"Target: {status['target_soc']}%"
    if status["target_time"]:
        hours, mins = status["target_time"]
        output += f" by {hours:02d}:{mins:02d}"
    return output


def _format_next_slot_line(status: dict[str, Any]) -> str:
    """Format next charging slot information."""
    if not status["next_slot_start"]:
        return ""

    slot_start = datetime.fromisoformat(status["next_slot_start"])
    output = f"Next Slot: {slot_start.strftime('%H:%M on %d %b')}"
    if status["next_slot_end"]:
        slot_end = datetime.fromisoformat(status["next_slot_end"])
        output += f" - {slot_end.strftime('%H:%M')}"
    return output


def _format_connection_status(status: dict[str, Any]) -> str:
    """Format charger online and plugged in status."""
    # Online status (from dedicated device status endpoint)
    if status.get("online"):
        output = "Charger Online"
    elif status.get("online") is False:
        output = "Charger Offline"
    else:
        output = "Charger Status Unknown"

    # Plugged in status
    if status.get("plugged_in") is True:
        output += " | Cable Plugged In"
    elif status.get("plugged_in") is False:
        output += " | Cable Unplugged"

    return output


def _format_ct_clamp_line(status: dict[str, Any]) -> str:
    """Format CT clamp information."""
    if not status.get("ct_connected"):
        return ""

    ct_amps = status.get("ct_clamp_amps") or status.get("ct_amps", 0)
    return f"CT Clamp: {ct_amps:.1f}A (connected)"


def _format_firmware_line(status: dict[str, Any]) -> str:
    """Format firmware and load balancing information."""
    parts = []

    firmware = status.get("firmware_version")
    if firmware:
        parts.append(f"Firmware: {firmware}")

    if status.get("load_balancing_enabled"):
        parts.append("Load Balancing: Enabled")

    return " | ".join(parts) if parts else ""


def _format_verbose_section(status: dict[str, Any]) -> str:
    """Format verbose details section."""
    output = "\n\nDetailed Status:"
    output += f"\n   Timestamp: {status['timestamp']}"

    if status["preconditioning_mins"] > 0:
        output += f"\n   Preconditioning: {status['preconditioning_mins']} minutes"

    output += f"\n   CT Clamp Connected: {'Yes' if status['ct_connected'] else 'No'}"

    # Device info
    device_info = status.get("device_info", {})
    if device_info:
        output += "\n\nDevice Information:"
        output += f"\n   Model: {device_info.get('name', 'Unknown')}"
        output += f"\n   Firmware: {status.get('firmware_version') or device_info.get('sw_version', 'Unknown')}"

    if status.get("load_balancing_enabled") is not None:
        output += (
            f"\n   Load Balancing: {'Enabled' if status['load_balancing_enabled'] else 'Disabled'}"
        )

    # Raw data
    output += "\n\nRaw Status Data:"
    for key, value in status.items():
        if key not in ["device_info", "timestamp"]:
            output += f"\n   {key}: {value}"

    return output


def format_status_output(status: dict[str, Any], *, verbose: bool = False) -> str:
    """Format Ohme charger status for display."""
    # OhmeChargerStatus is always populated (UNKNOWN if unavailable)
    charger_status = status["status"]
    status_text = charger_status.value.upper().replace("_", " ")
    status_emoji = _get_status_emoji(charger_status)

    output = f"Ohme Charger Status: {status_emoji} {status_text}"

    # OhmeChargerMode is always populated (UNKNOWN if unavailable)
    charger_mode = status["mode"]
    mode_text = charger_mode.value.upper().replace("_", " ")
    output += f"\nMode: {mode_text}"

    output += f"\n{_format_power_line(status)}"
    output += f"\n{_format_battery_line(status)}"
    output += f"\n{_format_target_line(status)}"

    next_slot = _format_next_slot_line(status)
    if next_slot:
        output += f"\n{next_slot}"

    # Vehicle
    if status["current_vehicle"]:
        output += f"\nVehicle: {status['current_vehicle']}"

    # Price cap
    if status.get("price_cap_enabled"):
        cap_value = status.get("price_cap_gbp_per_kwh")
        if cap_value is not None:
            output += f"\nPrice Cap: £{cap_value:.3f}/kWh (enabled)"
        else:
            output += "\nPrice Cap: Enabled (value unknown)"
    else:
        output += "\nPrice Cap: Disabled"

    # Connection status (online + plugged in)
    output += f"\n{_format_connection_status(status)}"

    # CT Clamp (if connected)
    ct_line = _format_ct_clamp_line(status)
    if ct_line:
        output += f"\n{ct_line}"

    # Firmware and load balancing
    firmware_line = _format_firmware_line(status)
    if firmware_line:
        output += f"\n{firmware_line}"

    if verbose:
        output += _format_verbose_section(status)

    return output


def _create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(
        description="Control Ohme Home Pro EV charger",
        epilog="Examples:\n"
        "  python3 scripts/ohme_ev_control.py --status\n"
        "  python3 scripts/ohme_ev_control.py --pause\n"
        "  python3 scripts/ohme_ev_control.py --resume\n"
        "  python3 scripts/ohme_ev_control.py --max-charge\n"
        "  python3 scripts/ohme_ev_control.py --set-target 80\n"
        "  python3 scripts/ohme_ev_control.py --set-target-time 07:30\n"
        "  python3 scripts/ohme_ev_control.py --set-price-cap 0.15",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Action arguments (mutually exclusive)
    action_group = parser.add_mutually_exclusive_group(required=True)
    action_group.add_argument("--status", action="store_true", help="Read current charger status")
    action_group.add_argument("--pause", action="store_true", help="Pause charging")
    action_group.add_argument("--resume", action="store_true", help="Resume charging")
    action_group.add_argument("--approve", action="store_true", help="Approve pending charge")
    action_group.add_argument("--max-charge", action="store_true", help="Enable max charge mode")
    action_group.add_argument(
        "--smart-charge", action="store_true", help="Enable smart charge mode"
    )
    action_group.add_argument(
        "--set-target", type=int, metavar="PERCENT", help="Set charge target percentage (0-100)"
    )
    action_group.add_argument(
        "--set-target-time", type=str, metavar="HH:MM", help="Set target time (e.g., 07:30)"
    )
    action_group.add_argument(
        "--set-price-cap", type=float, metavar="VALUE", help="Set price cap (GBP/kWh)"
    )
    action_group.add_argument(
        "--list-vehicles", action="store_true", help="List available vehicles"
    )
    action_group.add_argument(
        "--select-vehicle", type=str, metavar="NAME", help="Select vehicle to charge"
    )
    action_group.add_argument(
        "--test-library-pause",
        action="store_true",
        help="TEST: Call library's native async_pause_charge() (no AppCheck header)",
    )
    action_group.add_argument(
        "--test-library-resume",
        action="store_true",
        help="TEST: Call library's native async_resume_charge() (no AppCheck header)",
    )
    action_group.add_argument(
        "--test-appcheck-pause",
        action="store_true",
        help="TEST: Call /stop endpoint WITH expired AppCheck token",
    )
    action_group.add_argument(
        "--test-appcheck-resume",
        action="store_true",
        help="TEST: Call /resume endpoint WITH expired AppCheck token",
    )

    # Options
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show detailed status information"
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true", help="Minimal output (success/failure only)"
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set logging level (default: WARNING)",
    )

    return parser


async def _handle_status(client: OhmeEVClient, args: argparse.Namespace) -> int:
    """Handle status command."""
    if not args.quiet:
        print("Reading charger status...\n")

    status = await client.get_charger_status(use_cache=False)

    if args.quiet:
        # OhmeChargerStatus always has a value (UNKNOWN if unavailable)
        print(status["status"].value)
    else:
        print(format_status_output(status, verbose=args.verbose))

    return 0


async def _handle_simple_action(
    action_name: str,
    action_coro: object,
    args: argparse.Namespace,
) -> int:
    """Handle simple success/failure actions (pause, resume, approve, max_charge)."""
    if not args.quiet:
        print(f"Executing {action_name}...")

    success = await action_coro

    if args.quiet:
        print("SUCCESS" if success else "ERROR")
    elif success:
        print(f"{action_name} completed successfully")
    else:
        print(f"Failed to {action_name.lower()}")

    return 0 if success else 1


async def _handle_smart_charge(client: OhmeEVClient, args: argparse.Namespace) -> int:
    """Handle smart charge mode command."""
    if not args.quiet:
        print("Enabling smart charge mode...")

    await client.set_mode("smart_charge")

    if args.quiet:
        print("SUCCESS")
    else:
        print("Smart charge mode enabled successfully")

    return 0


async def _handle_set_target(
    client: OhmeEVClient, target_percent: int, args: argparse.Namespace
) -> int:
    """Handle set-target command."""
    if target_percent < 0 or target_percent > SOC_MAX_PERCENT:
        print(f"Error: Target percentage must be 0-100, got {target_percent}")
        return 1

    if not args.quiet:
        print(f"Setting charge target to {target_percent}%...")

    success = await client.set_target(target_percent=target_percent)

    if args.quiet:
        print("SUCCESS" if success else "ERROR")
    elif success:
        print(f"Charge target set to {target_percent}% successfully")
    else:
        print("Failed to set charge target")

    return 0 if success else 1


async def _handle_set_target_time(
    client: OhmeEVClient, time_str: str, args: argparse.Namespace
) -> int:
    """Handle set-target-time command."""
    # Parse time string (HH:MM)
    try:
        hours, minutes = map(int, time_str.split(":"))
        if hours < 0 or hours > HOUR_MAX or minutes < 0 or minutes > MINUTE_MAX:
            msg = "Invalid time range"
            raise ValueError(msg)  # noqa: TRY301 - Validation in try for exception handling
    except (ValueError, AttributeError):
        print("Error: Invalid time format. Use HH:MM (e.g., 07:30)")
        return 1

    if not args.quiet:
        print(f"Setting target time to {hours:02d}:{minutes:02d}...")

    success = await client.set_target(target_time=(hours, minutes))

    if args.quiet:
        print("SUCCESS" if success else "ERROR")
    elif success:
        print(f"Target time set to {hours:02d}:{minutes:02d} successfully")
    else:
        print("Failed to set target time")

    return 0 if success else 1


async def _handle_set_price_cap(
    client: OhmeEVClient, price_cap: float, args: argparse.Namespace
) -> int:
    """Handle set-price-cap command."""
    if price_cap < 0:
        print(f"Error: Price cap must be positive, got {price_cap}")
        return 1

    if not args.quiet:
        print(f"Setting price cap to GBP{price_cap:.2f}/kWh...")

    success = await client.set_price_cap(cap=price_cap)

    if args.quiet:
        print("SUCCESS" if success else "ERROR")
    elif success:
        print(f"Price cap set to GBP{price_cap:.2f}/kWh successfully")
    else:
        print("Failed to set price cap")

    return 0 if success else 1


async def _handle_list_vehicles(client: OhmeEVClient, args: argparse.Namespace) -> int:
    """Handle list-vehicles command."""
    if not args.quiet:
        print("Retrieving vehicle list...\n")

    vehicles = await client.get_vehicles()

    if args.quiet:
        for vehicle in vehicles:
            print(vehicle)
    elif vehicles:
        print(f"Available vehicles ({len(vehicles)}):")
        for i, vehicle in enumerate(vehicles, 1):
            print(f"  {i}. {vehicle}")
    else:
        print("No vehicles found")

    return 0


async def _handle_select_vehicle(
    client: OhmeEVClient, vehicle_name: str, args: argparse.Namespace
) -> int:
    """Handle select-vehicle command."""
    if not args.quiet:
        print(f"Selecting vehicle: {vehicle_name}...")

    success = await client.select_vehicle(vehicle_name)

    if args.quiet:
        print("SUCCESS" if success else "ERROR")
    elif success:
        print(f"Vehicle '{vehicle_name}' selected successfully")
    else:
        print(f"Vehicle '{vehicle_name}' not found")

    return 0 if success else 1


async def _handle_test_library_pause(client: OhmeEVClient, args: argparse.Namespace) -> int:
    """Handle test-library-pause command - calls library's native async_pause_charge()."""
    if not args.quiet:
        print("⚠️  WARNING: Testing library's native pause method")
        print("    This calls /stop endpoint which requires AppCheck")
        print("    May fail with 401 if AppCheck token expired\n")
        print("Calling library's async_pause_charge()...")

    try:
        # Access the internal client object to call library method directly
        success = await client.client.async_pause_charge()

        if args.quiet:
            print("SUCCESS" if success else "ERROR")
        elif success:
            print("✅ Library pause succeeded")
            print("    API endpoint: /v1/chargeSessions/{serial}/stop")
            print("    → AppCheck not required OR token still valid")
        else:
            print("❌ Library pause failed")

        return 0 if success else 1

    except Exception as e:  # noqa: BLE001  # pylint: disable=broad-exception-caught  # CLI top-level exception handler
        if args.quiet:
            print(f"ERROR: {e}")
        else:
            print(f"❌ Library pause raised exception: {e}")
            print(f"    Type: {type(e).__name__}")
            if "401" in str(e) or "Unauthorized" in str(e):
                print("    → This confirms AppCheck token is required and has expired")
            elif "404" in str(e):
                print("    → No active charge session (car unplugged or not charging)")
            else:
                print("    Full error details:")
                traceback.print_exc()
        return 1


async def _handle_test_library_resume(client: OhmeEVClient, args: argparse.Namespace) -> int:
    """Handle test-library-resume command - calls library's native async_resume_charge()."""
    if not args.quiet:
        print("⚠️  WARNING: Testing library's native resume method")
        print("    This calls /resume endpoint which requires AppCheck")
        print("    May fail with 401 if AppCheck token expired\n")
        print("Calling library's async_resume_charge()...")

    try:
        # Access the internal client object to call library method directly
        success = await client.client.async_resume_charge()

        if args.quiet:
            print("SUCCESS" if success else "ERROR")
        elif success:
            print("✅ Library resume succeeded")
            print("    API endpoint: /v1/chargeSessions/{serial}/resume")
            print("    → AppCheck not required OR token still valid")
        else:
            print("❌ Library resume failed")

        return 0 if success else 1

    except Exception as e:  # noqa: BLE001  # pylint: disable=broad-exception-caught  # CLI top-level exception handler
        if args.quiet:
            print(f"ERROR: {e}")
        else:
            print(f"❌ Library resume raised exception: {e}")
            print(f"    Type: {type(e).__name__}")
            if "401" in str(e) or "Unauthorized" in str(e):
                print("    → This confirms AppCheck token is required and has expired")
            elif "404" in str(e):
                print("    → No active charge session (car unplugged or not charging)")
            else:
                print("    Full error details:")
                traceback.print_exc()
        return 1


async def _handle_test_appcheck_pause(
    client: OhmeEVClient, args: argparse.Namespace, appcheck_token: str
) -> int:
    """Handle test-appcheck-pause command - calls /stop with expired AppCheck token."""
    if not args.quiet:
        print("⚠️  WARNING: Testing /stop endpoint WITH expired AppCheck token")
        print("    This sends X-Firebase-AppCheck header with old captured token")
        print(f"    Token: {appcheck_token[:50]}...\n")
        print("Calling /stop endpoint with AppCheck header...")

    try:
        # pylint: disable=import-outside-toplevel
        import aiohttp  # noqa: PLC0415, I001

        url = f"https://api-beta.ohme.io/v1/chargeSessions/{client.client.serial}/stop"
        headers = {
            "Authorization": f"Firebase {client.client._token}",  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            "Content-Type": "application/json",
            "User-Agent": "ohmepy/1.5.2",
            "X-Firebase-AppCheck": appcheck_token,
        }

        async with aiohttp.ClientSession() as session:
            async with asyncio.timeout(10):
                async with session.post(url, headers=headers) as resp:
                    status = resp.status
                    body = await resp.text()

                    if args.quiet:
                        print("SUCCESS" if status == 200 else f"ERROR: {status}")  # noqa: PLR2004
                    else:
                        print(f"HTTP Status: {status}")
                        print(f"Response: {body[:200]}")

                        if status == 200:  # noqa: PLR2004
                            print("✅ /stop endpoint WORKS with expired AppCheck token!")
                            print("    → This means Ohme may not validate AppCheck expiration")
                        elif status == 401:  # noqa: PLR2004
                            print("❌ 401 Unauthorized - AppCheck token expired/invalid")
                            print("    → Confirms AppCheck validation is enforced")
                        elif status == 404:  # noqa: PLR2004
                            print("⚠️  404 Not Found - No active charge session")
                            print("    → Need car plugged in and charging to test")
                        else:
                            print(f"⚠️  Unexpected status: {status}")

                    return 0 if status == 200 else 1  # noqa: PLR2004

    except Exception as e:  # noqa: BLE001  # pylint: disable=broad-exception-caught  # CLI top-level exception handler
        if args.quiet:
            print(f"ERROR: {e}")
        else:
            print(f"❌ Exception: {e}")
            traceback.print_exc()
        return 1


async def _handle_test_appcheck_resume(
    client: OhmeEVClient, args: argparse.Namespace, appcheck_token: str
) -> int:
    """Handle test-appcheck-resume command - calls /resume with expired AppCheck token."""
    if not args.quiet:
        print("⚠️  WARNING: Testing /resume endpoint WITH expired AppCheck token")
        print("    This sends X-Firebase-AppCheck header with old captured token")
        print(f"    Token: {appcheck_token[:50]}...\n")
        print("Calling /resume endpoint with AppCheck header...")

    try:
        # pylint: disable=import-outside-toplevel
        import aiohttp  # noqa: PLC0415, I001

        url = f"https://api-beta.ohme.io/v1/chargeSessions/{client.client.serial}/resume"
        headers = {
            "Authorization": f"Firebase {client.client._token}",  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            "Content-Type": "application/json",
            "User-Agent": "ohmepy/1.5.2",
            "X-Firebase-AppCheck": appcheck_token,
        }

        async with aiohttp.ClientSession() as session:
            async with asyncio.timeout(10):
                async with session.post(url, headers=headers) as resp:
                    status = resp.status
                    body = await resp.text()

                    if args.quiet:
                        print("SUCCESS" if status == 200 else f"ERROR: {status}")  # noqa: PLR2004
                    else:
                        print(f"HTTP Status: {status}")
                        print(f"Response: {body[:200]}")

                        if status == 200:  # noqa: PLR2004
                            print("✅ /resume endpoint WORKS with expired AppCheck token!")
                            print("    → This means Ohme may not validate AppCheck expiration")
                        elif status == 401:  # noqa: PLR2004
                            print("❌ 401 Unauthorized - AppCheck token expired/invalid")
                            print("    → Confirms AppCheck validation is enforced")
                        elif status == 404:  # noqa: PLR2004
                            print("⚠️  404 Not Found - No active charge session")
                            print("    → Need car plugged in and charging to test")
                        else:
                            print(f"⚠️  Unexpected status: {status}")

                    return 0 if status == 200 else 1  # noqa: PLR2004

    except Exception as e:  # noqa: BLE001  # pylint: disable=broad-exception-caught  # CLI top-level exception handler
        if args.quiet:
            print(f"ERROR: {e}")
        else:
            print(f"❌ Exception: {e}")
            traceback.print_exc()
        return 1


async def _execute_simple_actions(client: OhmeEVClient, args: argparse.Namespace) -> int | None:
    """Execute simple success/failure actions. Returns None if no match."""
    if args.pause:
        return await _handle_simple_action("Pause charge", client.pause_charge(), args)
    if args.resume:
        # resume_charge() was removed - use set_max_charge() directly
        return await _handle_simple_action(
            "Resume charge", client.set_max_charge(enabled=True), args
        )
    if args.approve:
        return await _handle_simple_action("Approve charge", client.approve_charge(), args)
    if args.max_charge:
        return await _handle_simple_action(
            "Enable max charge", client.set_max_charge(enabled=True), args
        )
    return None


async def _execute_parameterized_actions(
    client: OhmeEVClient, args: argparse.Namespace
) -> int | None:
    """Execute actions that require parameters. Returns None if no match."""
    if args.set_target:
        return await _handle_set_target(client, args.set_target, args)
    if args.set_target_time:
        return await _handle_set_target_time(client, args.set_target_time, args)
    if args.set_price_cap:
        return await _handle_set_price_cap(client, args.set_price_cap, args)
    if args.select_vehicle:
        return await _handle_select_vehicle(client, args.select_vehicle, args)
    return None


async def _execute_action(client: OhmeEVClient, args: argparse.Namespace) -> int:  # noqa: C901  # pylint: disable=too-many-return-statements  # CLI argument dispatcher with early returns
    """Execute the requested action."""
    if args.status:
        return await _handle_status(client, args)
    if args.smart_charge:
        return await _handle_smart_charge(client, args)
    if args.list_vehicles:
        return await _handle_list_vehicles(client, args)

    # Test commands for library's native methods (no AppCheck)
    if args.test_library_pause:
        return await _handle_test_library_pause(client, args)
    if args.test_library_resume:
        return await _handle_test_library_resume(client, args)

    # Test commands with expired AppCheck token
    if args.test_appcheck_pause or args.test_appcheck_resume:
        appcheck_token = client.ohme_config.get("appcheck_token")
        if not appcheck_token:
            print("❌ ERROR: No appcheck_token configured in config.yaml")
            print("    Add under ohme_ev: section:")
            print('    appcheck_token: "eyJ..."')
            print("    See specs/ohme_capture_script.md for token capture instructions")
            return 1

        if args.test_appcheck_pause:
            return await _handle_test_appcheck_pause(client, args, appcheck_token)
        if args.test_appcheck_resume:
            return await _handle_test_appcheck_resume(client, args, appcheck_token)

    # Try simple actions (pause, resume, approve, max_charge)
    result = await _execute_simple_actions(client, args)
    if result is not None:
        return result

    # Try parameterized actions (set_target, set_target_time, set_price_cap, select_vehicle)
    result = await _execute_parameterized_actions(client, args)
    if result is not None:
        return result

    return 0


async def main_async() -> None:
    """Execute main command-line interface (async)."""
    parser = _create_argument_parser()
    args = parser.parse_args()

    # Configure logging with user-specified level
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )

    client: OhmeEVClient | None = None
    try:
        # Create client
        client = OhmeEVClient()

        if not args.quiet:
            print("Connecting to Ohme charger...")

        # Connect to Ohme API
        await client.connect()

        if not args.quiet:
            print("Connected successfully")

        # Execute requested action
        exit_code = await _execute_action(client, args)
        await client.close()
        sys.exit(exit_code)

    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(130)
    except OhmeAuthenticationError as e:
        print(
            "AUTH_ERROR"
            if args.quiet
            else f"Authentication Error: {e}\n\nPlease check your Ohme credentials in config.yaml"
        )
        sys.exit(2)
    except OhmeNotPluggedInError:
        print(
            "NOT_PLUGGED_IN"
            if args.quiet
            else "Car is not plugged in.\n\nPlease plug in the charging cable and try again."
        )
        sys.exit(4)
    except OhmeConnectionError as e:
        print("CONNECTION_ERROR" if args.quiet else f"Connection Error: {e}")
        sys.exit(3)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as e:
        print("ERROR" if args.quiet else f"Error: {e}")
        sys.exit(1)
    finally:
        if client:
            with contextlib.suppress(OSError, ValueError, RuntimeError):
                await client.close()


def main() -> None:
    """Execute main entry point."""
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
