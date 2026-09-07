"""Unit tests for src/core_logic/hvac_schedule_logic.py.

Covers the spec's four schedule-normalisation rules and the day-to-schedule
mapping directly (the module's own docstring examples cover a few of these
too, via --doctest-modules, but those are easy to read past without noticing
if they stop being exercised - these tests are the ones that actually fail a
build).
"""

from __future__ import annotations

from datetime import time

import pytest

from src.core_logic.hvac_schedule_logic import (
    MINUTES_PER_DAY,
    SchedulePeriod,
    active_period_for,
    format_minutes,
    normalise_schedule,
    parse_hhmm,
    parse_periods,
    schedule_name_for_weekday,
    time_to_minutes,
)


def _bounds(periods):
    return [(p.start_minute, p.end_minute) for p in periods]


# ---------------------------------------------------------------------------
# Time parsing / formatting
# ---------------------------------------------------------------------------


def test_parse_hhmm_accepts_end_of_day_24_00():
    """24:00 is in the spec's own initial schedule and has no datetime.time
    equivalent - it must survive as 1440, not be clamped to 23:59."""
    assert parse_hhmm("24:00") == MINUTES_PER_DAY


@pytest.mark.parametrize("text", ["25:00", "24:01", "-1:00"])
def test_parse_hhmm_rejects_times_outside_the_day(text):
    with pytest.raises(ValueError, match="outside"):
        parse_hhmm(text)


def test_format_minutes_round_trips_parse_hhmm():
    for text in ("00:00", "06:30", "15:00", "24:00"):
        assert format_minutes(parse_hhmm(text)) == text


def test_time_to_minutes_ignores_sub_minute_precision():
    assert time_to_minutes(time(7, 30, 45)) == 450


def test_parse_periods_builds_from_schedule_yaml_shape():
    periods = parse_periods(
        [
            {"start": "00:00", "end": "06:00", "house_target_c": 18.0},
            {"start": "06:00", "end": "24:00", "house_target_c": 21.5},
        ]
    )

    assert _bounds(periods) == [(0, 360), (360, 1440)]
    assert periods[1].house_target_c == 21.5


# ---------------------------------------------------------------------------
# Normalisation - the spec's four rules
# ---------------------------------------------------------------------------


def test_rule_1_first_period_is_forced_to_start_at_midnight():
    result = normalise_schedule([SchedulePeriod(120, 360, 18.0)])

    assert _bounds(result) == [(0, 360)]


def test_rule_2_gap_is_closed_by_bringing_the_later_period_forward():
    result = normalise_schedule(
        [SchedulePeriod(0, 360, 18.0), SchedulePeriod(480, 600, 19.0)]
    )

    assert _bounds(result) == [(0, 360), (360, 600)]


def test_rule_3_overlap_moves_the_later_start_to_the_earlier_end():
    result = normalise_schedule(
        [SchedulePeriod(0, 600, 18.0), SchedulePeriod(480, 720, 19.0)]
    )

    assert _bounds(result) == [(0, 600), (600, 720)]


def test_rule_4_period_fully_swallowed_by_an_earlier_one_is_deleted():
    result = normalise_schedule(
        [
            SchedulePeriod(0, 600, 18.0),
            SchedulePeriod(480, 540, 19.0),  # entirely inside the first
            SchedulePeriod(540, 720, 20.0),
        ]
    )

    assert _bounds(result) == [(0, 600), (600, 720)]
    assert [p.house_target_c for p in result] == [18.0, 20.0]


def test_rule_4_deletion_relinks_against_the_last_surviving_period():
    """A deleted period must not leave the next one anchored to the period that
    was removed - it re-links to the last one that actually survived."""
    result = normalise_schedule(
        [
            SchedulePeriod(0, 600, 18.0),
            SchedulePeriod(60, 120, 19.0),  # deleted
            SchedulePeriod(90, 180, 20.0),  # also deleted (still inside the first)
            SchedulePeriod(200, 900, 21.0),
        ]
    )

    assert _bounds(result) == [(0, 600), (600, 900)]
    assert [p.house_target_c for p in result] == [18.0, 21.0]


def test_periods_supplied_out_of_order_are_sorted_before_normalising():
    result = normalise_schedule(
        [SchedulePeriod(360, 600, 19.0), SchedulePeriod(0, 360, 18.0)]
    )

    assert _bounds(result) == [(0, 360), (360, 600)]
    assert [p.house_target_c for p in result] == [18.0, 19.0]


def test_already_contiguous_schedule_is_left_unchanged():
    spec_initial = [
        SchedulePeriod(0, 360, 18.0),
        SchedulePeriod(360, 480, 18.0),
        SchedulePeriod(480, 900, 18.0),
        SchedulePeriod(900, 1320, 18.0),
        SchedulePeriod(1320, 1440, 18.0),
    ]

    assert normalise_schedule(spec_initial) == spec_initial


def test_empty_schedule_normalises_to_empty():
    assert normalise_schedule([]) == []


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_active_period_boundaries_are_start_inclusive_end_exclusive():
    periods = normalise_schedule(
        [SchedulePeriod(0, 360, 18.0), SchedulePeriod(360, 1440, 21.0)]
    )

    assert active_period_for(periods, time(5, 59)).house_target_c == 18.0
    assert active_period_for(periods, time(6, 0)).house_target_c == 21.0


def test_active_period_covers_the_final_minute_of_a_full_day():
    """A period ending at 24:00 must cover 23:59 - the reason end-of-day is
    stored as 1440 rather than a datetime.time."""
    periods = normalise_schedule([SchedulePeriod(0, 1440, 18.0)])

    assert active_period_for(periods, time(23, 59)).house_target_c == 18.0


def test_active_period_is_none_when_the_schedule_does_not_reach_that_time():
    """The spec has no "last period must end at 24:00" rule, so a short
    schedule genuinely leaves part of the day unscheduled. Callers must see
    None rather than an invented default target."""
    periods = normalise_schedule([SchedulePeriod(0, 360, 18.0)])

    assert active_period_for(periods, time(9, 0)) is None


def test_active_period_is_none_for_an_empty_schedule():
    assert active_period_for([], time(12, 0)) is None


# ---------------------------------------------------------------------------
# Day -> schedule mapping
# ---------------------------------------------------------------------------


def test_spec_default_weekday_assignment():
    """Spec: at_home_all_day Mon-Thu, at_home_part_of_day Fri-Sun."""
    assignments = {
        "friday": "at_home_part_of_day",
        "saturday": "at_home_part_of_day",
        "sunday": "at_home_part_of_day",
    }

    monday_to_thursday = [schedule_name_for_weekday(assignments, d) for d in range(4)]
    friday_to_sunday = [schedule_name_for_weekday(assignments, d) for d in range(4, 7)]

    assert monday_to_thursday == ["at_home_all_day"] * 4
    assert friday_to_sunday == ["at_home_part_of_day"] * 3


def test_unassigned_day_falls_back_to_the_default_schedule():
    assert schedule_name_for_weekday({}, 2) == "at_home_all_day"


def test_default_schedule_name_is_overridable():
    assert schedule_name_for_weekday({}, 2, default_name="something_else") == "something_else"
