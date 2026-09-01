#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""MELCloud Hot Water Tank Control Script.

Command-line interface for reading and controlling a Mitsubishi Ecodan hot water
tank via MELCloud (the same account used by the official MELCloud iPhone app).

Usage:
    python3 scripts/melcloud_hotwater_control.py --status
    python3 scripts/melcloud_hotwater_control.py --status --verbose
    python3 scripts/melcloud_hotwater_control.py --force-on
    python3 scripts/melcloud_hotwater_control.py --auto
    python3 scripts/melcloud_hotwater_control.py --force-on --no-verify
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import asyncio
import contextlib
import logging
from typing import Any

from hotwater_automation_core import get_config_path

from src.api_clients.melcloud_client import (
    MelCloudAuthenticationError,
    MelCloudClient,
    MelCloudConnectionError,
    MelCloudDeviceNotFoundError,
)


def _create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(
        description="Control a MELCloud-connected hot water tank (Mitsubishi Ecodan)",
        epilog="Examples:\n"
        "  python3 scripts/melcloud_hotwater_control.py --status\n"
        "  python3 scripts/melcloud_hotwater_control.py --force-on\n"
        "  python3 scripts/melcloud_hotwater_control.py --auto",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    action_group = parser.add_mutually_exclusive_group(required=True)
    action_group.add_argument("--status", action="store_true", help="Read current tank status")
    action_group.add_argument(
        "--force-on", action="store_true", help="Force hot water heating on"
    )
    action_group.add_argument(
        "--auto", action="store_true", help="Return to the unit's automatic heating schedule"
    )

    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Send the mode change once and don't wait/retry to confirm it took effect",
    )
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


def _format_status(status: dict[str, Any]) -> str:
    """Format hot water tank status for display."""
    mode_text = status["operation_mode"].value.upper().replace("_", " ")
    activity_text = status["status"].value.upper().replace("_", " ")

    lines = [
        f"Hot Water Tank: {status['device_name']}",
        f"Tank Temperature: {status['tank_temperature']}C "
        f"(target: {status['target_tank_temperature']}C)",
        f"Mode: {mode_text}",
        f"Activity: {activity_text}",
        f"Power: {'ON' if status['power'] else 'OFF'}",
    ]
    if status.get("holiday_mode"):
        lines.append("Holiday Mode: ON")
    lines.append(f"Last Seen: {status['last_seen']}")
    return "\n".join(lines)


async def _handle_status(client: MelCloudClient, args: argparse.Namespace) -> int:
    """Handle status command."""
    if not args.quiet:
        print("Reading hot water tank status...\n")

    status = await client.get_tank_status()

    if args.quiet:
        print(status["operation_mode"].value)
    else:
        print(_format_status(status))

    return 0


async def _handle_set_mode(
    client: MelCloudClient, *, enabled: bool, args: argparse.Namespace
) -> int:
    """Handle force-on / auto commands."""
    label = "force hot water on" if enabled else "auto mode"

    if not args.quiet:
        print(f"Requesting {label}...")

    success = await client.set_force_hot_water(enabled=enabled, verify=not args.no_verify)

    if args.quiet:
        print("SUCCESS" if success else "ERROR")
    elif success:
        print(f"Hot water mode set to {label} successfully")
    else:
        print(f"Failed to confirm mode change to {label} (MELCloud never reported the new mode)")

    return 0 if success else 1


async def _execute_action(client: MelCloudClient, args: argparse.Namespace) -> int:
    """Execute the requested action."""
    if args.status:
        return await _handle_status(client, args)
    if args.force_on:
        return await _handle_set_mode(client, enabled=True, args=args)
    return await _handle_set_mode(client, enabled=False, args=args)


async def main_async() -> None:
    """Execute main command-line interface (async)."""
    parser = _create_argument_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )

    client: MelCloudClient | None = None
    try:
        client = MelCloudClient(config_path=get_config_path())

        if not args.quiet:
            print("Connecting to MELCloud...")

        await client.connect()

        if not args.quiet:
            print("Connected successfully")

        exit_code = await _execute_action(client, args)
        await client.close()
        sys.exit(exit_code)

    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(130)
    except MelCloudAuthenticationError as e:
        print(
            "AUTH_ERROR"
            if args.quiet
            else f"Authentication Error: {e}\n\nPlease check your MELCloud credentials in config.yaml"
        )
        sys.exit(2)
    except MelCloudDeviceNotFoundError as e:
        print("DEVICE_NOT_FOUND" if args.quiet else f"Device Error: {e}")
        sys.exit(4)
    except MelCloudConnectionError as e:
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
