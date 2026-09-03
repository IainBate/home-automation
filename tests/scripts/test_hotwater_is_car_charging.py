"""Tests for _get_ohme_charging_power_watts() / is_car_charging_confirmed()
in hotwater_automation_core.py.

_get_ohme_charging_power_watts() covers the same error-handling edge cases
as the original is_car_charging() it replaced (see git history) - the
OhmeEVClient(...) construction happening *inside* the try block used to mean
a constructor failure left `client` unbound in the finally clause
(UnboundLocalError), harmless only because the constructor doesn't currently
raise. is_car_charging_confirmed() covers the new behavior layered on top:
gating by trigger_time, power-threshold comparison, and the persisted
2-consecutive-cycle confirmation shared with battery_mode_daemon.py's own
Ohme charging check (src/core_logic/ohme_charging_logic.py).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time
from unittest import mock

import hotwater_automation_core as core


def _now(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 15, hour, minute)


# --- _get_ohme_charging_power_watts ----------------------------------------


def test_disabled_returns_none_without_constructing_a_client():
    result = asyncio.run(core._get_ohme_charging_power_watts({"ohme_ev": {"enabled": False}}))
    assert result is None


def test_constructor_failure_is_handled_gracefully(monkeypatch):
    """Regression test: OhmeEVClient(...) raising must not crash with
    UnboundLocalError inside the finally clause.
    """

    def _raise(*_args, **_kwargs):
        raise ValueError("simulated bad config")

    monkeypatch.setattr(core, "OhmeEVClient", _raise)

    result = asyncio.run(core._get_ohme_charging_power_watts({"ohme_ev": {"enabled": True}}))

    assert result is None


def test_connect_failure_still_closes_the_client():
    close_calls = []

    class _FailingClient:
        def __init__(self, config_path=None):
            pass

        async def connect(self):
            raise ConnectionError("simulated connection failure")

        async def close(self):
            close_calls.append(1)

    with mock.patch.object(core, "OhmeEVClient", _FailingClient):
        result = asyncio.run(core._get_ohme_charging_power_watts({"ohme_ev": {"enabled": True}}))

    assert result is None
    assert close_calls == [1]


def test_returns_power_watts_from_charger_status():
    class _FakeClient:
        def __init__(self, config_path=None):
            pass

        async def connect(self):
            return None

        async def get_charger_status(self, *, use_cache):
            return {"power_watts": 1234}

        async def close(self):
            return None

    with mock.patch.object(core, "OhmeEVClient", _FakeClient):
        result = asyncio.run(core._get_ohme_charging_power_watts({"ohme_ev": {"enabled": True}}))

    assert result == 1234


# --- is_car_charging_confirmed ----------------------------------------------


WINDOW_START = time(15, 0)  # matches DEFAULT_CAR_CHARGING_TRIGGER_START_HOUR (3pm)


def test_at_or_after_trigger_time_returns_false_without_checking_ohme():
    state = {"ohme_charging_confirm_cycles": 5}

    with mock.patch.object(core, "_get_ohme_charging_power_watts") as fake_get_power:
        result = asyncio.run(
            core.is_car_charging_confirmed(
                {}, {}, state, _now(21, 30), trigger_time=time(21, 30),
                window_start_time=WINDOW_START,
            )
        )

    fake_get_power.assert_not_called()
    assert result is False
    assert state["ohme_charging_confirm_cycles"] == 0


def test_before_window_start_returns_false_without_checking_ohme():
    """The scenario this window exists for: car charging in the morning must
    not force-heat the ASHP - solar water heating is still effective then.
    """
    state = {"ohme_charging_confirm_cycles": 5}

    with mock.patch.object(core, "_get_ohme_charging_power_watts") as fake_get_power:
        result = asyncio.run(
            core.is_car_charging_confirmed(
                {}, {}, state, _now(9, 0), trigger_time=time(21, 30),
                window_start_time=WINDOW_START,
            )
        )

    fake_get_power.assert_not_called()
    assert result is False
    assert state["ohme_charging_confirm_cycles"] == 0


def test_first_cycle_above_threshold_is_not_yet_confirmed():
    state: dict = {}

    with mock.patch.object(
        core, "_get_ohme_charging_power_watts", mock.AsyncMock(return_value=600)
    ):
        result = asyncio.run(
            core.is_car_charging_confirmed(
                {}, {"ohme_charging_threshold_watts": 500}, state, _now(19, 0), time(21, 30),
                WINDOW_START,
            )
        )

    assert result is False
    assert state["ohme_charging_confirm_cycles"] == 1


def test_second_consecutive_cycle_above_threshold_confirms():
    state = {"ohme_charging_confirm_cycles": 1}

    with mock.patch.object(
        core, "_get_ohme_charging_power_watts", mock.AsyncMock(return_value=600)
    ):
        result = asyncio.run(
            core.is_car_charging_confirmed(
                {}, {"ohme_charging_threshold_watts": 500}, state, _now(19, 10), time(21, 30),
                WINDOW_START,
            )
        )

    assert result is True
    assert state["ohme_charging_confirm_cycles"] == 2


def test_below_threshold_resets_the_confirmation_count():
    state = {"ohme_charging_confirm_cycles": 1}

    with mock.patch.object(
        core, "_get_ohme_charging_power_watts", mock.AsyncMock(return_value=100)
    ):
        result = asyncio.run(
            core.is_car_charging_confirmed(
                {}, {"ohme_charging_threshold_watts": 500}, state, _now(19, 10), time(21, 30),
                WINDOW_START,
            )
        )

    assert result is False
    assert state["ohme_charging_confirm_cycles"] == 0


def test_unavailable_power_reading_is_treated_as_not_charging():
    state = {"ohme_charging_confirm_cycles": 1}

    with mock.patch.object(
        core, "_get_ohme_charging_power_watts", mock.AsyncMock(return_value=None)
    ):
        result = asyncio.run(
            core.is_car_charging_confirmed(
                {}, {"ohme_charging_threshold_watts": 500}, state, _now(19, 10), time(21, 30),
                WINDOW_START,
            )
        )

    assert result is False
    assert state["ohme_charging_confirm_cycles"] == 0


def test_uses_default_threshold_when_not_configured():
    state: dict = {}

    with mock.patch.object(
        core, "_get_ohme_charging_power_watts", mock.AsyncMock(return_value=501)
    ):
        asyncio.run(
            core.is_car_charging_confirmed({}, {}, state, _now(19, 0), time(21, 30), WINDOW_START)
        )

    assert state["ohme_charging_confirm_cycles"] == 1
    assert core.DEFAULT_OHME_CHARGING_THRESHOLD_WATTS == 500.0
