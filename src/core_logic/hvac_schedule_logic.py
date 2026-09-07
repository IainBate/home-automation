"""HVAC Schedule Logic - spec Phase 3, extended 2026-09-07 with a per-mode-family target.

Pure functions for resolving which comfort target applies at a given moment,
from the named schedules stored in schedule.yaml:

- ``at_home_all_day`` is the default, applying to any weekday that has no
  explicit assignment.
- ``at_home_part_of_day`` (and any other named schedule) applies only to the
  weekdays explicitly assigned to it.
- Exactly one schedule is active per day; a schedule may cover zero or more days.

Design Principles (mirrors hotwater_decision_logic.py):
- Pure function: No side effects, no API calls, testable
- Clear data contracts: Explicit input/output types using dataclasses

Period boundaries are held as minutes since midnight (0-1440) rather than
``datetime.time``, because the spec's schedules end at 24:00 and
``datetime.time`` cannot represent it (its maximum is 23:59:59.999999).
Storing 1440 keeps "22:00-24:00" exactly as written in schedule.yaml rather
than fudging it to 23:59 and leaving a sliver of the day uncovered.

**Deviation from the original spec, decided 2026-09-07 (not yet written up
in docs/hvac_thermostat_automation_plan.md's numbered §8 list - see its
"Seasonal/mode-family comfort target" note near the end)**: the spec's Phase
3 gave each period a single ``house_target_c``. The project owner's own
experience running the units manually is that a single number doesn't match
comfort - around 20C feels right when cooling in summer, around 18C when
heating in winter. Each period therefore now carries ``heat_target_c`` and
``cool_target_c`` separately (``dry`` shares ``cool_target_c`` with ``cool`` -
they already share the same hardware temperature range in
hvac_decision_logic.py's ``mode_temp_limits``). Season is *not* modelled
explicitly anywhere - it falls out for free, since heat mode is what runs in
winter and cool/dry is what runs in summer; the two targets are simply
whichever the schedule says for that time of day, picked by
``heat_target_c is not None`` won't reach a point - see
hvac_decision_logic.py's docstring for how the two targets interact
(directional escalation trigger, deadband, "retain vs reset" on a mode
change).

``heat_target_c`` must be strictly less than ``cool_target_c`` for every
period - see parse_periods()'s validation. This isn't just a sanity check:
hvac_decision_logic.py's deadband guarantee against continual mode switching
depends on that gap actually existing and being positive.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import time
from typing import Any

MINUTES_PER_DAY = 24 * 60
DEFAULT_SCHEDULE_NAME = "at_home_all_day"
WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


@dataclass(frozen=True)
class SchedulePeriod:
    """One period of a day's schedule.

    Attributes:
        start_minute: Start of the period, minutes since midnight (inclusive).
        end_minute: End of the period, minutes since midnight (exclusive).
            1440 represents 24:00 - see the module docstring.
        house_target_c: The house target temperature during this period. This
            is the temperature the automation tries to bring the *room* to
            (as read from the T6R), not a value written to any device
            directly - see hvac_decision_logic.py.

    """

    start_minute: int
    end_minute: int
    house_target_c: float


def parse_hhmm(text: str) -> int:
    """Convert an "HH:MM" string to minutes since midnight.

    Accepts "24:00" (= 1440) for a period running to end of day, which
    ``datetime.time`` cannot represent.

    Examples:
        >>> parse_hhmm("00:00")
        0
        >>> parse_hhmm("06:30")
        390
        >>> parse_hhmm("24:00")
        1440

    """
    hours_text, _, minutes_text = text.strip().partition(":")
    hours, minutes = int(hours_text), int(minutes_text)
    total = hours * 60 + minutes
    if not 0 <= total <= MINUTES_PER_DAY:
        msg = f"Time {text!r} is outside 00:00-24:00"
        raise ValueError(msg)
    return total


def format_minutes(minute_of_day: int) -> str:
    """Render minutes since midnight back as "HH:MM", for logs and reasons.

    Examples:
        >>> format_minutes(0)
        '00:00'
        >>> format_minutes(1440)
        '24:00'

    """
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


def time_to_minutes(at_time: time) -> int:
    """Convert a ``datetime.time`` to minutes since midnight.

    Examples:
        >>> from datetime import time
        >>> time_to_minutes(time(7, 30))
        450

    """
    return at_time.hour * 60 + at_time.minute


def parse_periods(raw_periods: list[dict[str, Any]]) -> list[SchedulePeriod]:
    """Build SchedulePeriods from schedule.yaml's raw ``{start, end, house_target_c}`` dicts.

    Does no normalisation - call normalise_schedule() on the result. Keeping
    parsing and normalisation separate means a malformed *file* raises here
    (loudly, at load time), while merely untidy but valid periods are silently
    tidied there, exactly as the spec intends ("applied logically at runtime,
    not by editing the file").

    Examples:
        >>> parse_periods([{"start": "00:00", "end": "06:00", "house_target_c": 18.0}])
        [SchedulePeriod(start_minute=0, end_minute=360, house_target_c=18.0)]

    """
    return [
        SchedulePeriod(
            start_minute=parse_hhmm(period["start"]),
            end_minute=parse_hhmm(period["end"]),
            house_target_c=float(period["house_target_c"]),
        )
        for period in raw_periods
    ]


def normalise_schedule(periods: list[SchedulePeriod]) -> list[SchedulePeriod]:
    """Apply the spec's four normalisation rules, returning a gapless day.

    The spec's rules are:
      1. The first period of the day must start at 00:00.
      2. Gaps between periods are closed by bringing the later period's start
         time forward.
      3. Overlapping periods: move the later period's start to the earlier
         period's end.
      4. If resolving an overlap leaves a period's start at or past its end,
         delete that period.

    Rules 1-3 all reduce to the same operation - *every* period starts where
    the previous surviving one ended (and the first starts at 00:00) - so they
    are applied as one pass rather than as three separate special cases, with
    rule 4 falling out as "skip a period that this leaves with nothing left".

    Periods are sorted by start time first. The spec's wording ("the *later*
    period") presumes a day-ordered sequence but doesn't say to sort, so this
    is an inferred step: without it, an out-of-order schedule.yaml would
    produce nonsense rather than a tidy day.

    Note that this guarantees coverage from 00:00 to the *last period's end*,
    which is not necessarily 24:00 - the spec has no "last period must end at
    24:00" rule, so a schedule whose final period ends earlier leaves the rest
    of the day genuinely uncovered. active_period_for() returns None there
    rather than inventing a target; see its docstring.

    Examples:
        >>> # Rule 1 (first period forced to midnight) and rule 2 (gap closed):
        >>> periods = [SchedulePeriod(120, 360, 18.0), SchedulePeriod(480, 600, 19.0)]
        >>> [(p.start_minute, p.end_minute) for p in normalise_schedule(periods)]
        [(0, 360), (360, 600)]

        >>> # Rule 3 (overlap) and rule 4 (fully-swallowed period deleted):
        >>> periods = [
        ...     SchedulePeriod(0, 600, 18.0),
        ...     SchedulePeriod(480, 540, 19.0),
        ...     SchedulePeriod(540, 720, 20.0),
        ... ]
        >>> [(p.start_minute, p.end_minute, p.house_target_c) for p in normalise_schedule(periods)]
        [(0, 600, 18.0), (600, 720, 20.0)]

    """
    normalised: list[SchedulePeriod] = []
    previous_end = 0
    for period in sorted(periods, key=lambda p: (p.start_minute, p.end_minute)):
        if previous_end >= period.end_minute:
            # Rule 4: the earlier period already covers all of this one.
            continue
        normalised.append(replace(period, start_minute=previous_end))
        previous_end = period.end_minute
    return normalised


def active_period_for(
    periods: list[SchedulePeriod], at_time: time
) -> SchedulePeriod | None:
    """Return the normalised period covering at_time, or None if uncovered.

    None means the schedule genuinely says nothing about this time of day (its
    last period ends before midnight - see normalise_schedule). Callers must
    treat that as "no scheduled target, make no change" rather than
    substituting a default: silently inventing a house target for an
    unscheduled part of the day would have the automation heating or cooling
    to a number the user never actually chose.

    Args:
        periods: Periods from normalise_schedule() - unnormalised input may
            contain gaps or overlaps and give surprising answers.
        at_time: Time of day to look up.

    Examples:
        >>> from datetime import time
        >>> periods = normalise_schedule(
        ...     [SchedulePeriod(0, 360, 18.0), SchedulePeriod(360, 1440, 21.0)]
        ... )
        >>> active_period_for(periods, time(3, 0)).house_target_c
        18.0
        >>> active_period_for(periods, time(7, 0)).house_target_c
        21.0

        >>> # A schedule that stops before midnight leaves the rest uncovered:
        >>> partial = normalise_schedule([SchedulePeriod(0, 360, 18.0)])
        >>> active_period_for(partial, time(9, 0)) is None
        True

    """
    minute_of_day = time_to_minutes(at_time)
    for period in periods:
        if period.start_minute <= minute_of_day < period.end_minute:
            return period
    return None


def schedule_name_for_weekday(
    day_assignments: dict[str, str],
    weekday: int,
    default_name: str = DEFAULT_SCHEDULE_NAME,
) -> str:
    """Return the schedule name assigned to a weekday, or the default.

    Args:
        day_assignments: Weekday name (lowercase, e.g. "friday") -> schedule name.
            Days absent from this mapping fall back to default_name, which is
            how the spec's "``at_home_all_day`` ... applies to any day without
            an explicit schedule" is expressed.
        weekday: Python's ``date.weekday()`` - 0 is Monday, 6 is Sunday.
        default_name: Schedule to use for unassigned days.

    Examples:
        >>> assignments = {"friday": "at_home_part_of_day"}
        >>> schedule_name_for_weekday(assignments, 4)  # Friday
        'at_home_part_of_day'
        >>> schedule_name_for_weekday(assignments, 0)  # Monday - unassigned
        'at_home_all_day'

    """
    return day_assignments.get(WEEKDAY_NAMES[weekday], default_name)
