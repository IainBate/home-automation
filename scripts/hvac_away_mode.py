#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""HVAC Away Mode - both Airstage units to minimum heat while nobody's home (one-shot CLI).

Per the spec: "Away Mode (overrides all other logic) - both units to minimum
heat, 10°C. If units are off, turn them on. On exit, leave units on
regardless of their prior state." A plain --start/--cancel/--status toggle,
not a holiday_mode.py-style --start-days N: the spec gives Away mode no
expiry of its own ("Away mode is off by default", nothing about a duration),
so there's no length to count down - this mirrors service_mode.py's shape
instead (an engineer visit has no predictable duration either).

Stamps hvac_automation_state.json's away_mode.active flag, which
hvac_automation_core.py's hvac_target_update check already reads every cycle
(hvac_automation.poll_intervals.hvac_target_seconds, 30 min by default) - no
daemon restart needed, same as every other flag in this codebase.

Entry immediately powers on any unit that's off (the one sanctioned power-on
in the whole spec); exit immediately restores the schedule's currently-active
target rather than waiting for the next periodic check (plan doc §8.4) - both
happen via the normal hvac_target_update cycle picking up the flag change,
not from this CLI directly, so --start/--cancel's own effect is only visible
once that cycle next runs (up to hvac_target_seconds later).

Usage:
    python3 scripts/hvac_away_mode.py --start     # both units to minimum heat, 10C
    python3 scripts/hvac_away_mode.py --cancel    # resume the normal schedule immediately
    python3 scripts/hvac_away_mode.py --status    # show whether Away mode is currently active
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
from datetime import UTC, datetime

import pytz
from hvac_automation_core import (
    DEFAULT_HVAC_LOCK_TIMEOUT_SECONDS,
    get_config_path,
    is_away_mode_active,
    locked_state,
    read_state,
)

from src.config_manager.config_manager import load_static_config

DEFAULT_TIMEZONE = "Europe/London"

# See holiday_mode.py/service_mode.py's identical constant/rationale -
# waiting the same worst-case-plus-margin here means a routine
# hvac_target_update check in progress doesn't make this CLI fail with a raw
# TimeoutError under normal, expected timing.
AWAY_MODE_STATE_LOCK_TIMEOUT_SECONDS = DEFAULT_HVAC_LOCK_TIMEOUT_SECONDS + 30


def _format_local(when: datetime, tz_name: str) -> str:
    """Render a UTC-aware datetime in the configured local timezone, for display."""
    return when.astimezone(pytz.timezone(tz_name)).strftime("%Y-%m-%d %H:%M %Z")


def start_away_mode() -> datetime:
    """Record Away mode as active, starting now.

    Returns:
        The (UTC) time Away mode was started, for the CLI's confirmation
        message.

    """
    now = datetime.now(tz=UTC)
    with locked_state(timeout=AWAY_MODE_STATE_LOCK_TIMEOUT_SECONDS) as state:
        state["away_mode"] = {"active": True, "started_at": now.isoformat()}
    return now


def cancel_away_mode() -> bool:
    """Clear Away mode, effective immediately.

    Returns:
        True if Away mode was actually active (for the CLI's confirmation
        message) - False if there was nothing to cancel.

    """
    with locked_state(timeout=AWAY_MODE_STATE_LOCK_TIMEOUT_SECONDS) as state:
        was_active = is_away_mode_active(state)
        state.pop("away_mode", None)
    return was_active


def print_status(tz_name: str) -> None:
    """Print whether Away mode is currently active, and since when."""
    state = read_state()
    if not is_away_mode_active(state):
        print("Away mode: not active")
        return

    started_at_str = state.get("away_mode", {}).get("started_at")
    if started_at_str:
        try:
            started_at = datetime.fromisoformat(started_at_str)
        except ValueError:
            started_at = None
    else:
        started_at = None

    if started_at is not None:
        print(f"Away mode: ACTIVE since {_format_local(started_at, tz_name)}")
    else:
        print("Away mode: ACTIVE")


def _create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Force both Airstage units to minimum heat (10C) while away",
        epilog="Examples:\n"
        "  python3 scripts/hvac_away_mode.py --start\n"
        "  python3 scripts/hvac_away_mode.py --cancel\n"
        "  python3 scripts/hvac_away_mode.py --status",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    action_group = parser.add_mutually_exclusive_group(required=True)
    action_group.add_argument(
        "--start", action="store_true", help="Start Away mode - both units to minimum heat"
    )
    action_group.add_argument(
        "--cancel",
        action="store_true",
        help="Cancel Away mode and resume the normal schedule immediately",
    )
    action_group.add_argument(
        "--status", action="store_true", help="Show whether Away mode is currently active"
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
            started_at = start_away_mode()
        except TimeoutError:
            print(
                "Could not start Away mode: timed out waiting for the HVAC state "
                "file lock (a decision check may be stuck). Try again shortly."
            )
            sys.exit(1)

        print(f"Away mode started at {_format_local(started_at, tz_name)}.")
        print(
            "Both Airstage units will be set to minimum heat (10C) and powered on if off, "
            "on the next hvac_target_update cycle (up to "
            "hvac_automation.poll_intervals.hvac_target_seconds later, 30 min by default)."
        )
        if not config.get("hvac_automation", {}).get("enabled", False):
            print(
                "Note: hvac_automation.enabled is currently false in config.yaml, so HVAC "
                "automation isn't actually running yet - this will take effect once you "
                "enable it."
            )
        return

    if args.cancel:
        try:
            was_active = cancel_away_mode()
        except TimeoutError:
            print(
                "Could not cancel Away mode: timed out waiting for the HVAC state "
                "file lock (a decision check may be stuck). Try again shortly."
            )
            sys.exit(1)

        if was_active:
            print(
                "Away mode cancelled - the schedule's current target will be restored "
                "on the next hvac_target_update cycle."
            )
        else:
            print("Away mode was not active - nothing to cancel.")
        return

    print_status(tz_name)


if __name__ == "__main__":
    main()
