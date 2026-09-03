#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Hot Water Automation Service Mode - engineer control pause (one-shot CLI).

For an ASHP installer/engineer visit: while active, this project's hot water
automation leaves the tank entirely alone - no force-heat, no revert, no
legionella cycle start/progress check - so nothing here fights or silently
reverts whatever the engineer sets manually. Same "software-side pause"
mechanism as scripts/holiday_mode.py (a flag in the same state file
hotwater_automation_core.py's checks already read, taking effect on the next
check without a daemon restart), but deliberately NOT holiday_mode.py itself:
holiday mode has a fixed --start-days N duration (a holiday has a known
length); a service visit doesn't, so this is a plain --start/--cancel/
--status toggle with no expiry to compute.

Only stands down *new* force-heat decisions (see
src/core_logic/hotwater_decision_logic.py's
HotWaterDecisionContext.service_mode_active) - an already-in-progress
force-heat/legionella window when service mode starts is left to finish/time
out normally via the existing revert/legionella-progress checks, exactly as
holiday mode handles it (see holiday_mode.py's docstring) - simpler, and no
less safe since it can only overlap the moment service mode was turned on.

Usage:
    python3 scripts/service_mode.py --start     # pause all hot water automation
    python3 scripts/service_mode.py --cancel    # resume normal automation immediately
    python3 scripts/service_mode.py --status    # show whether the pause is active
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
from datetime import UTC, datetime

import pytz
from hotwater_automation_core import (
    DEFAULT_HOTWATER_LOCK_TIMEOUT_SECONDS,
    get_config_path,
    is_service_mode_active,
    locked_state,
    read_state,
)

from src.config_manager.config_manager import load_static_config

DEFAULT_TIMEZONE = "Europe/London"

# See holiday_mode.py's identical constant/rationale - waiting the same
# worst-case-plus-margin here means a routine force-heat/revert check in
# progress doesn't make this CLI fail with a raw TimeoutError under normal,
# expected timing.
SERVICE_MODE_STATE_LOCK_TIMEOUT_SECONDS = DEFAULT_HOTWATER_LOCK_TIMEOUT_SECONDS + 30.0


def _format_local(when: datetime, tz_name: str) -> str:
    """Render a UTC-aware datetime in the configured local timezone, for display."""
    return when.astimezone(pytz.timezone(tz_name)).strftime("%Y-%m-%d %H:%M %Z")


def start_service_mode() -> datetime:
    """Record service mode as active, starting now.

    Returns:
        The (UTC) time service mode was started, for the CLI's confirmation
        message.

    """
    now = datetime.now(tz=UTC)
    with locked_state(timeout=SERVICE_MODE_STATE_LOCK_TIMEOUT_SECONDS) as state:
        state["service_mode"] = {"active": True, "started_at": now.isoformat()}
    return now


def cancel_service_mode() -> bool:
    """Clear service mode, effective immediately.

    Returns:
        True if service mode was actually active (for the CLI's confirmation
        message) - False if there was nothing to cancel.

    """
    with locked_state(timeout=SERVICE_MODE_STATE_LOCK_TIMEOUT_SECONDS) as state:
        was_active = is_service_mode_active(state)
        state.pop("service_mode", None)
    return was_active


def print_status(tz_name: str) -> None:
    """Print whether service mode is currently active, and since when."""
    state = read_state()
    if not is_service_mode_active(state):
        print("Service mode: not active")
        return

    started_at_str = state.get("service_mode", {}).get("started_at")
    if started_at_str:
        try:
            started_at = datetime.fromisoformat(started_at_str)
        except ValueError:
            started_at = None
    else:
        started_at = None

    if started_at is not None:
        print(f"Service mode: ACTIVE since {_format_local(started_at, tz_name)}")
    else:
        print("Service mode: ACTIVE")


def _create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Pause ALL hot water automation (force-heat, revert, legionella) for an "
            "engineer/installer visit"
        ),
        epilog="Examples:\n"
        "  python3 scripts/service_mode.py --start\n"
        "  python3 scripts/service_mode.py --cancel\n"
        "  python3 scripts/service_mode.py --status",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    action_group = parser.add_mutually_exclusive_group(required=True)
    action_group.add_argument(
        "--start", action="store_true", help="Start service mode - pause hot water automation"
    )
    action_group.add_argument(
        "--cancel",
        action="store_true",
        help="Cancel service mode and resume normal hot water automation immediately",
    )
    action_group.add_argument(
        "--status", action="store_true", help="Show whether service mode is currently active"
    )
    parser.add_argument("--config", type=str, default=None, help="Path to config.yaml")
    return parser


def main() -> None:
    """Execute main entry point."""
    parser = _create_argument_parser()
    args = parser.parse_args()

    config = load_static_config(args.config or get_config_path())
    if config is None:
        print("Failed to load config.yaml (see logs above)")
        sys.exit(1)
    tz_name = config.get("location", {}).get("default_timezone_str", DEFAULT_TIMEZONE)

    if args.start:
        try:
            started_at = start_service_mode()
        except TimeoutError:
            print(
                "Could not start service mode: timed out waiting for the hot water state "
                "file lock (a force-heat/revert check may be stuck). Try again shortly."
            )
            sys.exit(1)

        print(f"Service mode started at {_format_local(started_at, tz_name)}.")
        print(
            "Hot water: force-heat, revert, and legionella cycle start/progress checks are "
            "all paused until you run --cancel. An already-in-progress heating cycle, if "
            "any, will still finish/time out normally via the existing safety checks."
        )
        if not config.get("hotwater_automation", {}).get("enabled", False):
            print(
                "Note: hotwater_automation.enabled is currently false in config.yaml, so hot "
                "water automation isn't actually running yet."
            )
        return

    if args.cancel:
        try:
            was_active = cancel_service_mode()
        except TimeoutError:
            print(
                "Could not cancel service mode: timed out waiting for the hot water state "
                "file lock (a force-heat/revert check may be stuck). Try again shortly."
            )
            sys.exit(1)

        if was_active:
            print("Service mode cancelled - hot water automation resumes normally.")
        else:
            print("Service mode was not active - nothing to cancel.")
        return

    print_status(tz_name)


if __name__ == "__main__":
    main()
