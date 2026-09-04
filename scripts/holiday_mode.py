#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Hot Water Automation Holiday Pause - N days, no ASHP force-heat (one-shot CLI).

NOT the same as MELCloud's own native "Holiday Mode" device setting (see
src/api_clients/melcloud_client.py's holiday_mode field, surfaced by
scripts/melcloud_hotwater_control.py --status as "Holiday Mode: ON/OFF") -
that's a setting on the physical unit itself, which this project only ever
reads, never sets. This is a separate, software-side pause of *this
project's own* force-heat automation, deliberately labelled "automation
holiday" everywhere it's printed to avoid the two being confused.

"For N days, don't force-heat the hot water tank via the ASHP" - a single
command that stamps a holiday.until timestamp into the same state file
hotwater_automation_core.py's force-heat check already reads
(hotwater_automation_state.json), so it takes effect on that check's very
next run (up to hotwater_automation.poll_interval_seconds later if
hotwater_mode_daemon.py is running, or immediately on the next cron-driven
hotwater_auto_check.py run) - no daemon restart needed.

Overrides every force-heat trigger (car charging, battery surplus, off-peak
grid) for the duration - see src/core_logic/hotwater_decision_logic.py's
HotWaterDecisionContext.holiday_mode_active. This also means a legionella
high-temperature cycle (see hotwater_automation_core.py's module docstring)
is silently deferred for the whole holiday if it falls due during it, since
that cycle rides on the exact same force-heat trigger - there is no separate
schedule for it, so pausing force-heat pauses it too. It will simply run at
the next opportunity after the holiday ends. An already-in-progress
force-heat window when holiday mode starts is deliberately left to
finish/time out normally via the existing revert check (bounded by
force_heat_max_duration_hours, a few hours by default) rather than being
force-interrupted - simpler and no less safe, since it can only overlap the
first day of a holiday.

Solar-heated hot water (e.g. a separate PV diverter, if the household has
one) is entirely outside this codebase and unaffected either way.

Heating: NOT included yet. resideo_client.py can now read the T6R (a
Honeywell Lyric device) via local HomeKit, but there is deliberately no
automated heating *control* yet - the household hasn't finalised a spec or
verification harness for it. This command says so explicitly on
--start-days rather than silently doing nothing.

Usage:
    python3 scripts/holiday_mode.py --start-days 7   # pause hot water force-heat for 7 days
    python3 scripts/holiday_mode.py --cancel         # resume normal automation immediately
    python3 scripts/holiday_mode.py --status         # show whether the pause is active
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
from datetime import UTC, datetime, timedelta

import pytz
from hotwater_automation_core import (
    DEFAULT_HOTWATER_LOCK_TIMEOUT_SECONDS,
    get_config_path,
    get_holiday_until,
    is_holiday_active,
    locked_state,
    read_state,
)

from src.config_manager.config_manager import load_static_config

DEFAULT_TIMEZONE = "Europe/London"

# start_holiday()/cancel_holiday() can run concurrently with
# hotwater_mode_daemon.py or a cron-triggered hotwater_auto_check.py, which
# hold this same state-file lock across their whole MELCloud
# request-then-verify retry loop - up to DEFAULT_HOTWATER_LOCK_TIMEOUT_SECONDS
# (~120s worst case, see hotwater_automation_core.py). Waiting the same
# worst-case-plus-margin here (rather than locked_state()'s own 10s default)
# means a routine force-heat/revert check in progress doesn't make this CLI
# fail with a raw TimeoutError under normal, expected timing.
HOLIDAY_STATE_LOCK_TIMEOUT_SECONDS = DEFAULT_HOTWATER_LOCK_TIMEOUT_SECONDS + 30.0


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
    with locked_state(timeout=HOLIDAY_STATE_LOCK_TIMEOUT_SECONDS) as state:
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
    with locked_state(timeout=HOLIDAY_STATE_LOCK_TIMEOUT_SECONDS) as state:
        was_active = is_holiday_active(state)
        state.pop("holiday", None)
    return was_active


def print_status(tz_name: str) -> None:
    """Print whether the automation holiday pause is currently active, and until when."""
    state = read_state()
    until = get_holiday_until(state)
    if until is None:
        print("Automation holiday: not active")
        return

    if is_holiday_active(state):
        print(f"Automation holiday: ACTIVE until {_format_local(until, tz_name)}")
    else:
        print(f"Automation holiday: expired ({_format_local(until, tz_name)} has passed)")


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

        try:
            until = start_holiday(args.start_days)
        except TimeoutError:
            print(
                "Could not start automation holiday: timed out waiting for the hot water "
                "state file lock (a force-heat/revert check may be stuck). Try again shortly."
            )
            sys.exit(1)

        print(
            f"Automation holiday started for {args.start_days} day(s), until "
            f"{_format_local(until, tz_name)}."
        )
        print(
            "Hot water: ASHP force-heat (including any overdue legionella cycle - it rides "
            "on the same trigger, so it's deferred too) is paused for the duration. An "
            "already-in-progress heating cycle, if any, will still finish/time out normally, "
            "then stay off. Solar-heated hot water, if you have a separate diverter, is "
            "unaffected."
        )
        if not config.get("hotwater_automation", {}).get("enabled", False):
            print(
                "Note: hotwater_automation.enabled is currently false in config.yaml, so hot "
                "water automation isn't actually running yet - this will take effect once you "
                "enable it."
            )
        print(
            "Heating: automatic control isn't available yet (the dashboard can read the T6R, "
            "but automated heating control hasn't been built/agreed yet). Turn the heating down "
            "manually if you want it lower while away."
        )
        return

    if args.cancel:
        try:
            was_active = cancel_holiday()
        except TimeoutError:
            print(
                "Could not cancel automation holiday: timed out waiting for the hot water "
                "state file lock (a force-heat/revert check may be stuck). Try again shortly."
            )
            sys.exit(1)

        if was_active:
            print("Automation holiday cancelled - hot water automation resumes normally.")
        else:
            print("Automation holiday was not active - nothing to cancel.")
        return

    print_status(tz_name)


if __name__ == "__main__":
    main()
