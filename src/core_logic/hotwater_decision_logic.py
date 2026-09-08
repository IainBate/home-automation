"""Hot Water Tank Force-Heat Decision Logic.

This module provides the pure decision function for whether the MELCloud-connected
hot water tank should be force-heated:
- Whenever the EV is charging (car charging dominates all other conditions - Ohme
  has already decided this is an economical time to draw power, either the fixed
  Intelligent Go off-peak window or solar surplus, so hot water piggybacks on
  that decision without re-deriving it).
- Otherwise, at or after a configured evening trigger hour, if the tank has
  cooled below a threshold, and the grid is currently in the tariff's
  off-peak window (a separate, forward-looking battery-prediction path -
  see HotWaterDecisionContext.battery_prediction_trigger_active - is what
  allows heating from stored solar earlier than that, deliberately not a
  live battery-SoC check here).

Design Principles (mirrors ohme_charging_logic.py):
- Pure function: No side effects, no API calls, testable
- Clear data contracts: Explicit input/output types using dataclasses
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# A handful of small default constants duplicated from
# hotwater_automation_core.py (the definitive source - keep these in sync
# with it) rather than imported from there, to avoid a circular import: core
# imports these pure functions FROM this module, so this module can't import
# anything back from core. Each is a stable, rarely-changed fallback default
# for a config key this module's own pure functions read - moved here
# 2026-09-08 alongside the functions themselves (see this module's own
# architectural review) so they're testable without importing MelCloudClient/
# state-file I/O at all.
_DEFAULT_OFFPEAK_END = "05:30"
_DEFAULT_LEGIONELLA_INTERVAL_DAYS = 90
_DEFAULT_BATTERY_PREDICTION_DEADLINE_HOUR = 23.5
_DEFAULT_FORCE_HEAT_MAX_DURATION_HOURS = 1.0
_DEFAULT_LEGIONELLA_MAX_CYCLE_DURATION_HOURS = 1.0


@dataclass
class HotWaterDecisionContext:
    """Inputs needed to decide whether to force-heat the hot water tank.

    Attributes:
        tank_temperature_c: Current tank temperature from MELCloud, or None if
            unavailable.
        tank_temp_threshold_c: Force-heat only if the tank is below this.
        car_is_charging: True if the Ohme EV charger is currently charging.
            Dominates all other conditions - if the tank needs heating, heat it
            now, regardless of trigger_hour/battery/grid.
        battery_soc_percent: Current battery state of charge (%), or None if
            unavailable. Not used by this function's own decision (see
            in_evening_window below) - carried here purely for callers that
            want it alongside the decision, e.g. for logging.
        battery_soc_min_percent: The threshold battery_prediction_trigger_active
            is computed against by the caller (both batteries predicted at/above
            this by the off-peak deadline).
        grid_is_cheap: True if the grid is currently in a cheap/off-peak tariff
            period (e.g. within the Intelligent Go 23:30-05:30 window).
        in_evening_window: True if it's currently evening/overnight - at or
            after the configured trigger hour, through to the start of the
            next off-peak-driven "day" (see is_in_offpeak_window; the caller
            computes this the same way as grid_is_cheap, just with trigger_hour
            as the window start). The whole point is to defer heating until
            evening so the day's solar/battery can cover it, so this is
            checked even on an out-of-schedule (e.g. manual) run. Not checked
            if car_is_charging or battery_prediction_trigger_active. Once
            reached, only grid_is_cheap (not a live battery_soc_percent
            check - see battery_prediction_trigger_active below) may trigger
            heating - see this function's own evening-window branch for why.
        battery_prediction_trigger_active: True if it's currently within
            hotwater_automation.battery_prediction_window_start_hour to
            battery_prediction_deadline_hour (3pm-11:30pm by default) AND
            both batteries are predicted to still be at/above
            battery_soc_min_percent at battery_prediction_deadline_hour (see
            hotwater_automation_core.py's get_battery_prediction_to_deadline).
            An independent trigger path alongside car_is_charging and
            in_evening_window/grid_is_cheap - it exists precisely to
            allow heating earlier than trigger_hour when there's forecast to
            be plenty of stored solar left by the time the grid's off-peak
            window opens anyway, without waiting for trigger_hour to arrive
            first. Computed by the caller (not derived from
            battery_soc_percent/battery_soc_min_percent here), since it needs
            its own forward-looking prediction of SoC AT the off-peak
            deadline, not a live/current reading.
        holiday_mode_active: True if scripts/holiday_mode.py has an active
            holiday period recorded (see hotwater_automation_core.py's
            is_holiday_active). Dominates every other condition, including
            car_is_charging - a holiday means "don't force-heat via the ASHP
            for N days", full stop. This also silently defers a legionella
            high-temperature cycle if one falls due during the holiday - it
            rides on this exact same force-heat trigger (see
            hotwater_automation_core.py's module docstring), so there is no
            separate schedule for it to run on instead. Solar-heated hot
            water (if the household has a separate diverter) is entirely
            outside this codebase and unaffected either way. Not the same as
            MELCloud's own native "Holiday Mode" device setting (see
            melcloud_client.py's holiday_mode field) - that's a setting on
            the physical unit, read-only from this project; this is a
            separate, software-side pause of this project's own automation.
        service_mode_active: True if scripts/service_mode.py has service mode
            active - an engineer/installer visit, during which this project's
            automation must not touch the tank at all so any manual mode/
            temperature changes they make aren't fought or silently reverted.
            Dominates every other condition exactly like holiday_mode_active
            (including car_is_charging and battery_prediction_trigger_active)
            - the two are independent and either alone is enough to suppress
            force-heat. Unlike holiday_mode_active, there's no fixed end date
            here (service_mode.py has no --start-days) - it stays active
            until explicitly cancelled.

    """

    tank_temperature_c: float | None
    tank_temp_threshold_c: float
    car_is_charging: bool
    battery_soc_percent: float | None
    battery_soc_min_percent: float
    grid_is_cheap: bool
    in_evening_window: bool
    holiday_mode_active: bool = False
    service_mode_active: bool = False
    battery_prediction_trigger_active: bool = False


@dataclass
class HotWaterDecision:
    """Result of a hot water force-heat decision.

    Attributes:
        should_force_heat: True if the tank should be force-heated now.
        reason: Human-readable explanation, for logging.

    """

    should_force_heat: bool
    reason: str


def is_in_offpeak_window(current_time: time, window_start: time, window_end: time) -> bool:
    """Check whether current_time falls within a (possibly midnight-crossing) window.

    Used for tariffs with a fixed off-peak window, such as Octopus Intelligent
    Go's 23:30-05:30 whole-home off-peak period (which crosses midnight).

    Args:
        current_time: The time to check.
        window_start: Window start (inclusive).
        window_end: Window end (exclusive).

    Returns:
        True if current_time is within [window_start, window_end).

    Examples:
        >>> from datetime import time
        >>> is_in_offpeak_window(time(0, 0), time(23, 30), time(5, 30))
        True
        >>> is_in_offpeak_window(time(12, 0), time(23, 30), time(5, 30))
        False
        >>> is_in_offpeak_window(time(23, 45), time(23, 30), time(5, 30))
        True

    """
    if window_start <= window_end:
        return window_start <= current_time < window_end
    # Window crosses midnight (e.g. 23:30 -> 05:30)
    return current_time >= window_start or current_time < window_end


def hour_float_to_time(hour_float: float) -> time:
    """Convert a fractional hour (e.g. 21.5) to a time object (e.g. time(21, 30)).

    trigger_hour is configured as a fractional hour (not "HH:MM") so it stays
    a plain number consistent with battery_evening_prediction_logic.py's own
    trigger_hour handling, which already needs fractional-hour arithmetic
    (predicting a horizon some number of hours ahead) - this is just the one
    place that fractional hour needs to become an actual time of day to
    compare against a clock reading.

    Examples:
        >>> hour_float_to_time(21.5)
        datetime.time(21, 30)
        >>> hour_float_to_time(18)
        datetime.time(18, 0)
        >>> # Rounds to the nearest minute rather than truncating.
        >>> hour_float_to_time(21.999)
        datetime.time(22, 0)

    """
    hour = int(hour_float)
    minute = round((hour_float - hour) * 60)
    if minute == 60:
        hour += 1
        minute = 0
    return time(hour % 24, minute)


def is_in_evening_window(current_time: time, trigger_hour_time: time, window_end: time) -> bool:
    """Check whether current_time is at/after trigger_hour_time, or before window_end.

    Unlike is_in_offpeak_window, this always treats the window as spanning
    midnight - it never infers wraparound from comparing the two times.
    is_in_offpeak_window's window_start <= window_end heuristic is right for
    a fixed tariff window (whose start/end are chosen by the tariff, not the
    user), but wrong here: trigger_hour_time is user-configurable to any
    hour, and the "evening" window is *always* meant to span from
    trigger_hour onwards through midnight to window_end, regardless of which
    specific hour trigger_hour_time is. A trigger_hour of 0-5 (with the
    default window_end of 05:30) numerically satisfies window_start <=
    window_end, so is_in_offpeak_window would treat it as a same-day-only
    window (true only between trigger_hour and 05:30) instead of the
    intended "evening onwards, wrapping past midnight" window - silently
    inverting the automation's behaviour (off in the evening/night, on only
    in the early morning) for exactly that plausible trigger_hour range
    (e.g. temporarily lowered to test the automation without waiting for
    evening).

    Args:
        current_time: The time to check.
        trigger_hour_time: Evening window start (inclusive).
        window_end: Evening window end (exclusive) - typically offpeak_end.

    Returns:
        True if current_time is at/after trigger_hour_time, or before window_end.

    Examples:
        >>> from datetime import time
        >>> is_in_evening_window(time(20, 0), time(18, 0), time(5, 30))
        True
        >>> is_in_evening_window(time(2, 0), time(18, 0), time(5, 30))
        True
        >>> is_in_evening_window(time(12, 0), time(18, 0), time(5, 30))
        False
        >>> # A low trigger_hour must still wrap past midnight, not become a
        >>> # same-day-only window:
        >>> is_in_evening_window(time(20, 0), time(2, 0), time(5, 30))
        True
        >>> is_in_evening_window(time(3, 0), time(2, 0), time(5, 30))
        True
        >>> # When trigger_hour_time is itself before window_end, the wrapped
        >>> # window has no gap left to be "outside" of - degenerate, but the
        >>> # right way to err (permanently on, not silently inverted):
        >>> is_in_evening_window(time(12, 0), time(2, 0), time(5, 30))
        True

    """
    return current_time >= trigger_hour_time or current_time < window_end


def determine_hotwater_decision(context: HotWaterDecisionContext) -> HotWaterDecision:
    """Decide whether to force-heat the hot water tank right now.

    Args:
        context: Tank, car-charging, battery, grid and timing state to decide from.

    Returns:
        HotWaterDecision with should_force_heat and a human-readable reason.

    Examples:
        >>> # Daytime, car not charging - never force-heat
        >>> context = HotWaterDecisionContext(
        ...     tank_temperature_c=30.0, tank_temp_threshold_c=45.0, car_is_charging=False,
        ...     battery_soc_percent=90.0, battery_soc_min_percent=50.0, grid_is_cheap=False,
        ...     in_evening_window=False,
        ... )
        >>> determine_hotwater_decision(context).should_force_heat
        False

        >>> # Daytime, but car IS charging - dominates, heat now
        >>> context = HotWaterDecisionContext(
        ...     tank_temperature_c=30.0, tank_temp_threshold_c=45.0, car_is_charging=True,
        ...     battery_soc_percent=None, battery_soc_min_percent=50.0, grid_is_cheap=False,
        ...     in_evening_window=False,
        ... )
        >>> determine_hotwater_decision(context).should_force_heat
        True

        >>> # Evening, tank cold, but a high live battery SoC ALONE is not
        >>> # enough - only battery_prediction_trigger_active or grid_is_cheap
        >>> # may bring heating forward from stored solar/off-peak.
        >>> context = HotWaterDecisionContext(
        ...     tank_temperature_c=30.0, tank_temp_threshold_c=45.0, car_is_charging=False,
        ...     battery_soc_percent=90.0, battery_soc_min_percent=50.0, grid_is_cheap=False,
        ...     in_evening_window=True,
        ... )
        >>> determine_hotwater_decision(context).should_force_heat
        False

        >>> # Evening, tank cold, grid now off-peak -> heat
        >>> context = HotWaterDecisionContext(
        ...     tank_temperature_c=30.0, tank_temp_threshold_c=45.0, car_is_charging=False,
        ...     battery_soc_percent=90.0, battery_soc_min_percent=50.0, grid_is_cheap=True,
        ...     in_evening_window=True,
        ... )
        >>> determine_hotwater_decision(context).should_force_heat
        True

        >>> # Holiday mode dominates even car charging
        >>> context = HotWaterDecisionContext(
        ...     tank_temperature_c=30.0, tank_temp_threshold_c=45.0, car_is_charging=True,
        ...     battery_soc_percent=90.0, battery_soc_min_percent=50.0, grid_is_cheap=True,
        ...     in_evening_window=True, holiday_mode_active=True,
        ... )
        >>> determine_hotwater_decision(context).should_force_heat
        False

        >>> # Service mode dominates even car charging, same as holiday mode
        >>> context = HotWaterDecisionContext(
        ...     tank_temperature_c=30.0, tank_temp_threshold_c=45.0, car_is_charging=True,
        ...     battery_soc_percent=90.0, battery_soc_min_percent=50.0, grid_is_cheap=True,
        ...     in_evening_window=True, service_mode_active=True,
        ... )
        >>> determine_hotwater_decision(context).should_force_heat
        False

        >>> # Afternoon, tank cold, battery-prediction path active - heat now,
        >>> # without waiting for the evening window
        >>> context = HotWaterDecisionContext(
        ...     tank_temperature_c=30.0, tank_temp_threshold_c=45.0, car_is_charging=False,
        ...     battery_soc_percent=None, battery_soc_min_percent=20.0, grid_is_cheap=False,
        ...     in_evening_window=False, battery_prediction_trigger_active=True,
        ... )
        >>> determine_hotwater_decision(context).should_force_heat
        True

    """
    if context.tank_temperature_c is None:
        return HotWaterDecision(
            should_force_heat=False, reason="Tank temperature unavailable, cannot decide"
        )

    if context.tank_temperature_c >= context.tank_temp_threshold_c:
        return HotWaterDecision(
            should_force_heat=False,
            reason=(
                f"Tank at {context.tank_temperature_c:.1f}C >= threshold "
                f"{context.tank_temp_threshold_c:.1f}C, no heating needed"
            ),
        )

    # Holiday and service mode dominate everything below, including car
    # charging - see HotWaterDecisionContext.holiday_mode_active/
    # service_mode_active.
    if context.holiday_mode_active:
        return HotWaterDecision(
            should_force_heat=False,
            reason=(
                f"Tank at {context.tank_temperature_c:.1f}C < threshold "
                f"{context.tank_temp_threshold_c:.1f}C, but holiday mode is active - "
                f"not force-heating"
            ),
        )

    if context.service_mode_active:
        return HotWaterDecision(
            should_force_heat=False,
            reason=(
                f"Tank at {context.tank_temperature_c:.1f}C < threshold "
                f"{context.tank_temp_threshold_c:.1f}C, but service mode is active - "
                f"leaving the tank under manual/engineer control"
            ),
        )

    # Car charging dominates: Ohme has already decided this is an economical
    # time to draw power, so heat the tank too, regardless of trigger_hour,
    # battery SoC or grid tariff period.
    if context.car_is_charging:
        return HotWaterDecision(
            should_force_heat=True,
            reason=(
                f"Tank at {context.tank_temperature_c:.1f}C < threshold "
                f"{context.tank_temp_threshold_c:.1f}C, car is charging - heating now"
            ),
        )

    # Battery-prediction path: an independent trigger alongside car charging
    # and the evening/off-peak window below - see
    # HotWaterDecisionContext.battery_prediction_trigger_active.
    if context.battery_prediction_trigger_active:
        return HotWaterDecision(
            should_force_heat=True,
            reason=(
                f"Tank at {context.tank_temperature_c:.1f}C < threshold "
                f"{context.tank_temp_threshold_c:.1f}C, both batteries predicted "
                f">= {context.battery_soc_min_percent:.0f}% by the off-peak deadline - "
                f"heating now from stored solar"
            ),
        )

    if not context.in_evening_window:
        return HotWaterDecision(
            should_force_heat=False,
            reason="Tank needs heating but it's daytime (before the evening trigger hour)",
        )

    # Deliberately NOT a live battery_soc_percent >= battery_soc_min_percent
    # check here (removed 2026-09-07 - a real incident found the earlier
    # version of this branch triggering an early heat off a live SoC snapshot
    # that said "sufficient right now" without accounting for what heating
    # itself, plus ongoing household load, would draw between now and the
    # off-peak deadline - risking depleting the battery into peak-rate grid
    # import before offpeak_start, which is exactly what heating early from
    # stored solar is supposed to avoid). battery_prediction_trigger_active
    # above is the only path allowed to bring heating forward from stored
    # solar - it's forward-looking (predicts SoC AT the off-peak deadline,
    # not just right now) and is checked every poll tick, so a
    # currently-insufficient prediction doesn't block a later, more
    # confident one as the day's actual usage plays out. Once
    # grid_is_cheap is genuinely true (off-peak has started), nothing more
    # needs predicting - the energy is already cheap, not drawn from the
    # battery.
    if context.grid_is_cheap:
        return HotWaterDecision(
            should_force_heat=True,
            reason=(
                f"Tank at {context.tank_temperature_c:.1f}C < threshold "
                f"{context.tank_temp_threshold_c:.1f}C, grid is in off-peak window "
                f"- heating on cheap import"
            ),
        )

    return HotWaterDecision(
        should_force_heat=False,
        reason=(
            f"Tank at {context.tank_temperature_c:.1f}C < threshold "
            f"{context.tank_temp_threshold_c:.1f}C, no battery-prediction trigger and grid is "
            f"not yet in an off-peak window - waiting"
        ),
    )


# --- Pure helpers moved from hotwater_automation_core.py (2026-09-08) ------
#
# Each of these takes plain data in and returns plain data out - no MELCloud
# calls, no state-file I/O - so they belong here alongside
# determine_hotwater_decision rather than in the module full of async I/O
# orchestration. hotwater_automation_core.py imports them back by name, so
# every existing call site there is unchanged.


def _overnight_deadline_passed(
    activated_at_local: datetime, now_local: datetime, offpeak_end_time: time
) -> bool:
    """Whether now_local is at/after the next offpeak_end_time on/after activated_at_local.

    A hard clock deadline (default 05:30) on top of the max-duration safety
    net - "a cycle scheduled at 6pm must be completed by 5:30am the day
    after" - regardless of how far the tank still is from target.
    Deliberately NOT a plain `now_local.time() >= offpeak_end_time` check:
    that's true for the entire rest of the day once past 05:30 (e.g. 16:30 >=
    05:30), which would wrongly cap an unrelated afternoon car-charging/
    battery-prediction-triggered heat that has nothing to do with an
    overnight deadline. Instead this finds the *next* offpeak_end_time at or
    after activation (same day if activation was already before it, e.g. a
    cycle starting at 04:50; the following day if activation was in the
    evening, e.g. 22:00) and only compares against that.

    Examples:
        >>> from datetime import UTC
        >>> tz = UTC
        >>> # Started 10pm, still running past 6am the next day -> deadline passed
        >>> _overnight_deadline_passed(
        ...     datetime(2026, 1, 1, 22, 0, tzinfo=tz), datetime(2026, 1, 2, 6, 0, tzinfo=tz),
        ...     time(5, 30),
        ... )
        True
        >>> # Started 4:50am, still running at 5:35am the same morning -> deadline passed
        >>> _overnight_deadline_passed(
        ...     datetime(2026, 1, 2, 4, 50, tzinfo=tz), datetime(2026, 1, 2, 5, 35, tzinfo=tz),
        ...     time(5, 30),
        ... )
        True
        >>> # Started 4pm (afternoon path), an hour later -> nowhere near its own deadline
        >>> _overnight_deadline_passed(
        ...     datetime(2026, 1, 1, 16, 0, tzinfo=tz), datetime(2026, 1, 1, 17, 0, tzinfo=tz),
        ...     time(5, 30),
        ... )
        False

    """
    deadline_date = activated_at_local.date()
    if activated_at_local.time() >= offpeak_end_time:
        deadline_date += timedelta(days=1)
    deadline_dt = datetime.combine(
        deadline_date, offpeak_end_time, tzinfo=activated_at_local.tzinfo
    )
    return now_local >= deadline_dt


def _daily_check_lookup_date_str(hw_config: dict[str, Any], now_local: datetime) -> str:
    """The calendar date whose daily_check snapshot governs right now.

    _update_daily_threshold_snapshot always WRITES under the calendar date it
    ran on - safe, since daily_check_hour (18:00 by default) is always in the
    afternoon/evening, never near midnight. But every READ of that snapshot
    (the force-heat decision, the legionella-due check, and
    _refresh_daily_snapshot_if_warm's own correction) needs to keep finding
    that same snapshot for the REST of that evening's session - which runs
    through midnight to offpeak_end (05:30 by default) the following
    calendar day.

    Without this adjustment (a real gap found 2026-09-07, discovered
    alongside the "don't re-trigger" fix elsewhere in this module): any
    decision made between midnight and offpeak_end would compare the
    snapshot's date against TOMORROW's date (relative to when the snapshot
    was actually written), never match, and read the tank's temperature as
    unavailable - silently unable to heat at all during that stretch, no
    matter how cold the tank actually was. That's exactly the part of the
    night (car-charging and battery-prediction have both already closed by
    then) that's supposed to be covered by "the grid is now off-peak, heat
    regardless" - which never got the chance to apply.

    Before offpeak_end, we're still in "last night's" session - look up
    yesterday's date. At/after it, use today's - the same offpeak_end
    boundary _overnight_deadline_passed already uses to mark an overnight
    session as over.
    """
    offpeak_end_time = datetime.strptime(
        hw_config.get("offpeak_end", _DEFAULT_OFFPEAK_END), "%H:%M"
    ).time()
    if now_local.time() < offpeak_end_time:
        return (now_local - timedelta(days=1)).date().isoformat()
    return now_local.date().isoformat()


def _is_legionella_due(hw_config: dict[str, Any], legionella_state: dict[str, Any]) -> bool:
    """Return True if legionella_interval_days have passed since the last completed cycle.

    A missing/malformed last_completed_at (never run, or hand-edited state) is
    treated as due, the same "unknown means due" stance run_legionella_check
    took previously - it must get a chance to run at least once rather than
    being permanently blocked by bad state.
    """
    last_completed_str = legionella_state.get("last_completed_at")
    if not last_completed_str:
        return True
    try:
        last_completed = datetime.fromisoformat(last_completed_str)
    except ValueError:
        logger.error(
            "legionella last_completed_at (%r) is not a valid timestamp, treating "
            "the cycle as due",
            last_completed_str,
        )
        return True
    interval_days = hw_config.get("legionella_interval_days", _DEFAULT_LEGIONELLA_INTERVAL_DAYS)
    days_since = (datetime.now(tz=UTC) - last_completed).days
    return days_since >= interval_days


def _battery_prediction_eligibility_end_hour(hw_config: dict[str, Any]) -> float:
    """The last hour the battery-prediction path may still START a new heat.

    Normally just battery_prediction_deadline_hour itself - the window stays
    open right up to the moment it's forecasting towards. But if
    hotwater_automation.forced_discharge_start_hour is configured (the
    battery system enters a forced-discharge mode at a fixed clock time -
    confirmed 2026-09-07: from that point on, the battery's SoC trajectory is
    no longer driven by normal household usage, so a prediction of what it'll
    be later is meaningless), the window must close earlier than that: a heat
    started too close to forced discharge could still be running - for up to
    whichever of force_heat_max_duration_hours/legionella_max_cycle_duration_hours
    is longer, since a battery-prediction trigger can be upgraded to a
    legionella cycle - when forced discharge begins. Closing the window one
    full heating cycle's worth of time earlier guarantees any heat this path
    starts has definitely finished by then.

    Returns whichever of the two bounds is earlier - the deadline itself is
    still respected if forced discharge starts so late that subtracting the
    duration doesn't bring it any earlier.
    """
    deadline_hour = hw_config.get(
        "battery_prediction_deadline_hour", _DEFAULT_BATTERY_PREDICTION_DEADLINE_HOUR
    )
    forced_discharge_start_hour = hw_config.get("forced_discharge_start_hour")
    if forced_discharge_start_hour is None:
        return deadline_hour

    max_duration_hours = max(
        hw_config.get("force_heat_max_duration_hours", _DEFAULT_FORCE_HEAT_MAX_DURATION_HOURS),
        hw_config.get(
            "legionella_max_cycle_duration_hours", _DEFAULT_LEGIONELLA_MAX_CYCLE_DURATION_HOURS
        ),
    )
    return min(deadline_hour, forced_discharge_start_hour - max_duration_hours)
