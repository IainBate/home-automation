"""Hot Water Tank Force-Heat Decision Logic.

This module provides the pure decision function for whether the MELCloud-connected
hot water tank should be force-heated:
- Whenever the EV is charging (car charging dominates all other conditions - Ohme
  has already decided this is an economical time to draw power, either the fixed
  Intelligent Go off-peak window or solar surplus, so hot water piggybacks on
  that decision without re-deriving it).
- Otherwise, at or after a configured evening trigger hour, if the tank has
  cooled below a threshold, and cheap energy is available (battery has surplus
  stored solar, or the grid is currently in the tariff's off-peak window).

Design Principles (mirrors ohme_charging_logic.py):
- Pure function: No side effects, no API calls, testable
- Clear data contracts: Explicit input/output types using dataclasses
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time


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
            unavailable.
        battery_soc_min_percent: Battery is considered to have spare stored
            solar if its SoC is at or above this.
        grid_is_cheap: True if the grid is currently in a cheap/off-peak tariff
            period (e.g. within the Intelligent Go 23:30-05:30 window).
        in_evening_window: True if it's currently evening/overnight - at or
            after the configured trigger hour, through to the start of the
            next off-peak-driven "day" (see is_in_offpeak_window; the caller
            computes this the same way as grid_is_cheap, just with trigger_hour
            as the window start). The whole point is to defer heating until
            evening so the day's solar/battery can cover it, so this is
            checked even on an out-of-schedule (e.g. manual) run. Not checked
            if car_is_charging.
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

    """

    tank_temperature_c: float | None
    tank_temp_threshold_c: float
    car_is_charging: bool
    battery_soc_percent: float | None
    battery_soc_min_percent: float
    grid_is_cheap: bool
    in_evening_window: bool
    holiday_mode_active: bool = False


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

        >>> # Evening, tank cold, battery has surplus -> heat
        >>> context = HotWaterDecisionContext(
        ...     tank_temperature_c=30.0, tank_temp_threshold_c=45.0, car_is_charging=False,
        ...     battery_soc_percent=90.0, battery_soc_min_percent=50.0, grid_is_cheap=False,
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

    # Holiday mode dominates everything below, including car charging - see
    # HotWaterDecisionContext.holiday_mode_active.
    if context.holiday_mode_active:
        return HotWaterDecision(
            should_force_heat=False,
            reason=(
                f"Tank at {context.tank_temperature_c:.1f}C < threshold "
                f"{context.tank_temp_threshold_c:.1f}C, but holiday mode is active - "
                f"not force-heating"
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

    if not context.in_evening_window:
        return HotWaterDecision(
            should_force_heat=False,
            reason="Tank needs heating but it's daytime (before the evening trigger hour)",
        )

    battery_has_surplus = (
        context.battery_soc_percent is not None
        and context.battery_soc_percent >= context.battery_soc_min_percent
    )

    if battery_has_surplus:
        return HotWaterDecision(
            should_force_heat=True,
            reason=(
                f"Tank at {context.tank_temperature_c:.1f}C < threshold "
                f"{context.tank_temp_threshold_c:.1f}C, battery SoC "
                f"{context.battery_soc_percent:.0f}% >= {context.battery_soc_min_percent:.0f}% "
                f"- heating from stored solar"
            ),
        )

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
            f"{context.tank_temp_threshold_c:.1f}C, but battery SoC "
            f"({context.battery_soc_percent}) is below minimum and grid is not in an "
            f"off-peak window - waiting"
        ),
    )
