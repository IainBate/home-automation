"""Tests for hotwater_automation_core.py's holiday-mode state helpers.

Covers get_holiday_until()/is_holiday_active() edge cases found in review:
a naive (no tzinfo) timestamp must not reach is_holiday_active's
datetime.now(tz=UTC) comparison (TypeError: can't compare offset-naive and
offset-aware datetimes), and a non-string "until" must not blow up
datetime.fromisoformat() with an uncaught TypeError.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import hotwater_automation_core as core


def test_get_holiday_until_missing_returns_none():
    assert core.get_holiday_until({}) is None
    assert core.get_holiday_until({"holiday": {}}) is None


def test_get_holiday_until_parses_aware_timestamp():
    until_str = "2026-09-10T12:00:00+00:00"
    assert core.get_holiday_until({"holiday": {"until": until_str}}) == datetime.fromisoformat(
        until_str
    )


def test_get_holiday_until_malformed_string_returns_none():
    assert core.get_holiday_until({"holiday": {"until": "not-a-timestamp"}}) is None


def test_get_holiday_until_non_string_returns_none_not_typeerror():
    assert core.get_holiday_until({"holiday": {"until": 12345}}) is None


def test_get_holiday_until_naive_timestamp_returns_none_not_typeerror():
    # No timezone offset - would otherwise reach is_holiday_active's aware
    # datetime.now(tz=UTC) comparison and raise TypeError.
    assert core.get_holiday_until({"holiday": {"until": "2026-09-10T12:00:00"}}) is None


def test_is_holiday_active_true_for_future_aware_timestamp():
    until = (datetime.now(tz=UTC) + timedelta(days=1)).isoformat()
    assert core.is_holiday_active({"holiday": {"until": until}}) is True


def test_is_holiday_active_false_for_past_aware_timestamp():
    until = (datetime.now(tz=UTC) - timedelta(days=1)).isoformat()
    assert core.is_holiday_active({"holiday": {"until": until}}) is False


def test_is_holiday_active_false_for_naive_timestamp_no_crash():
    assert core.is_holiday_active({"holiday": {"until": "2026-09-10T12:00:00"}}) is False


def test_is_holiday_active_false_for_non_string_no_crash():
    assert core.is_holiday_active({"holiday": {"until": 12345}}) is False
