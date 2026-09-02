#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Holiday Mode - pause ASHP hot water force-heating for N days (one-shot CLI).

"For N days, don't force-heat the hot water tank via the ASHP" - a single
command that stamps a holiday.until timestamp into the same state file
hotwater_automation_core.py's force-heat check already reads
(hotwater_automation_state.json), so it takes effect on that check's very
next run (up to hotwater_automation.poll_interval_seconds later if
hotwater_mode_daemon.py is running, or immediately on the next cron-driven
hotwater_auto_check.py run) - no daemon restart needed.

Overrides every force-heat trigger (car charging, battery surplus, off-peak
grid) for the duration - see src/core_logic/hotwater_decision_logic.py's
HotWaterDecisionContext.holiday_mode_active. An already-in-progress force-heat
window when holiday mode starts is deliberately left to finish/time out
normally via the existing revert check (bounded by
force_heat_max_duration_hours, a few hours by default) rather than being
force-interrupted - simpler and no less safe, since it can only overlap the
first day of a holiday.

Solar-heated hot water (e.g. a separate PV diverter, if the household has
one) is entirely outside this codebase and unaffected either way.

Heating: NOT included yet. The Resideo/Evohome integration is currently
disabled (config.yaml) because this household's actual thermostat is a
Honeywell Lyric device on a different backend to the one this project's
resideo_client.py talks to (evohome-async/TCC v2, Evohome-family only) - so
there is no automated way to lower the heating for a holiday yet. This
command says so explicitly on --start-days rather than silently doing
nothing.

Usage:
    python3 scripts/holiday_mode.py --start-days 7   # pause hot water force-heat for 7 days
    python3 scripts/holiday_mode.py --cancel         # resume normal automation immediately
    python3 scripts/holiday_mode.py --status         # show whether holiday mode is active
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
from datetime import UTC, datetime, timedelta

import pytz
from hotwater_automation_core import (
    get_config_path,
    get_holiday_until,
    is_holiday_active,
    locked_state,
    read_state,
)

from src.config_manager.config_manager import load_static_config

DEFAULT_TIMEZONE = "Europe/London"


def _format_local(when: datetime, tz_name: str) -> str:
    """Render a UTC-aware datetime in the configured local timezone, for display."""
    return when.astimezone(pytz.timezone(tz_name)).strftime("%Y-%m-%d %H:%M %Z")


def start_holiday(days: int) -> datetime:
    """Record a holiday period starting now, for `days` * 24 hours.

    Returns:
        The (UTC) time the holiday ends.

    """
    now = datetime.now(tz=UTC)
    until = now + timedelta(days=days)
    with locked_state() as state:
        state["holiday"] = {
            "started_at": now.isoformat(),
            "until": until.isoformat(),
            "days": days,
        }
    return until


def cancel_holiday() -> bool:
    """Clear any recorded holiday period, effective immediately.

    Returns:
        True if a holiday was actually active (for the CLI's confirmation
        message) - False if there was nothing to cancel.

    """
    with locked_state() as state:
        was_active = is_holiday_active(state)
        state.pop("holiday", None)
    return was_active


def print_status(tz_name: str) -> None:
    """Print whether holiday mode is currently active, and until when."""
    state = read_state()
    until = get_holiday_until(state)
    if until is None:
        print("Holiday mode: not active")
        return

    if is_holiday_active(state):
        print(f"Holiday mode: ACTIVE until {_format_local(until, tz_name)}")
    else:
        print(f"Holiday mode: expired ({_format_local(until, tz_name)} has passed)")


def _create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pause ASHP hot water force-heating for N days (holiday mode)",
        epilog="Examples:\n"
        "  python3 scripts/holiday_mode.py --start-days 7\n"
        "  python3 scripts/holiday_mode.py --cancel\n"
        "  python3 scripts/holiday_mode.py --status",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    action_group = parser.add_mutually_exclusive_group(required=True)
    action_group.add_argument(
        "--start-days", type=int, metavar="N", help="Start holiday mode for N days from now"
    )
    action_group.add_argument(
        "--cancel",
        action="store_true",
        help="Cancel holiday mode and resume normal hot water automation immediately",
    )
    action_group.add_argument(
        "--status", action="store_true", help="Show whether holiday mode is currently active"
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

    if args.start_days is not None:
        if args.start_days <= 0:
            print("--start-days must be a positive number of days")
            sys.exit(1)

        until = start_holiday(args.start_days)
        print(
            f"Holiday mode started for {args.start_days} day(s), until "
            f"{_format_local(until, tz_name)}."
        )
        print(
            "Hot water: ASHP force-heat is paused for the duration (an already-in-progress "
            "heating cycle, if any, will still finish/time out normally, then stay off). "
            "Solar-heated hot water, if you have a separate diverter, is unaffected."
        )
        if not config.get("hotwater_automation", {}).get("enabled", False):
            print(
                "Note: hotwater_automation.enabled is currently false in config.yaml, so hot "
                "water automation isn't actually running yet - this will take effect once you "
                "enable it."
            )
        print(
            "Heating: automatic control isn't available yet (Resideo integration is disabled - "
            "the real thermostat is a Lyric device needing a different API than this project "
            "currently has). Turn the heating down manually if you want it lower while away."
        )
        return

    if args.cancel:
        was_active = cancel_holiday()
        if was_active:
            print("Holiday mode cancelled - hot water automation resumes normally.")
        else:
            print("Holiday mode was not active - nothing to cancel.")
        return

    print_status(tz_name)


if __name__ == "__main__":
    main()
