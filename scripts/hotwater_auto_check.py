#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Hot Water Tank Automatic Force-Heat Check (one-shot CLI).

Thin command-line wrapper around hotwater_automation_core.py's
run_force_heat_check() / run_revert_check() - see that module's docstring for
the full decision rules. Suitable for a cron entry; for continuous operation
(so the "car is charging" condition is caught promptly) see
scripts/hotwater_mode_daemon.py instead, which shares this same core logic.

Usage:
    python3 scripts/hotwater_auto_check.py                        # evaluate and act
    python3 scripts/hotwater_auto_check.py --dry-run               # evaluate only, don't act
    python3 scripts/hotwater_auto_check.py --revert-if-due         # safety revert check
    python3 scripts/hotwater_auto_check.py --legionella-progress   # check/finish a cycle

A legionella cycle has no separate trigger of its own - it rides on the same
force-heat evaluation above (see hotwater_automation_core.py), so there is no
--legionella-check flag; --legionella-progress still exists to check/finish
a cycle already in progress.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import asyncio
import contextlib
import logging

from hotwater_automation_core import (
    get_config_path,
    get_hotwater_automation_config_error,
    run_force_heat_check,
    run_legionella_progress_check,
    run_revert_check,
)

from src.api_clients.melcloud_client import (
    MelCloudAuthenticationError,
    MelCloudConnectionError,
    MelCloudDeviceNotFoundError,
)
from src.config_manager.config_manager import load_static_config


def _create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(
        description="Automatically force-heat a MELCloud hot water tank under configured conditions",
        epilog="Examples:\n"
        "  python3 scripts/hotwater_auto_check.py\n"
        "  python3 scripts/hotwater_auto_check.py --dry-run\n"
        "  python3 scripts/hotwater_auto_check.py --revert-if-due",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate the decision but don't actually request a mode change",
    )

    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "--revert-if-due",
        action="store_true",
        help="Instead of the force-heat check, revert to auto mode if it's been "
        "force-heating longer than force_heat_max_duration_hours",
    )
    action_group.add_argument(
        "--legionella-progress",
        action="store_true",
        help="Instead of the force-heat check, check an in-progress legionella "
        "cycle and revert once it's reached target or timed out",
    )

    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    parser.add_argument(
        "--log-level",
        type=str,
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set logging level (default: WARNING)",
    )
    return parser


async def main_async() -> None:
    """Execute main command-line interface (async)."""
    parser = _create_argument_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )

    config = load_static_config(get_config_path())
    if config is None:
        print("ERROR" if args.quiet else "Failed to load config.yaml (see logs above)")
        sys.exit(1)

    hw_config = config.get("hotwater_automation", {})
    if not hw_config.get("enabled", False):
        if not args.quiet:
            print("Hot water automation is disabled (hotwater_automation.enabled: false)")
        sys.exit(0)

    config_error = get_hotwater_automation_config_error(config)
    if config_error:
        print("CONFIG_ERROR" if args.quiet else f"Configuration error: {config_error}")
        sys.exit(1)

    try:
        if args.revert_if_due:
            exit_code = await run_revert_check(hw_config, dry_run=args.dry_run, quiet=args.quiet)
        elif args.legionella_progress:
            exit_code = await run_legionella_progress_check(
                hw_config, dry_run=args.dry_run, quiet=args.quiet
            )
        else:
            exit_code = await run_force_heat_check(
                config, hw_config, dry_run=args.dry_run, quiet=args.quiet
            )
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


def main() -> None:
    """Execute main entry point."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main_async())


if __name__ == "__main__":
    main()
