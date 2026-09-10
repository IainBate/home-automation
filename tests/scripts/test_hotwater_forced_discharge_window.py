"""Tests for battery_prediction_eligibility_end_hour and its wiring into
_run_force_heat_check_locked's battery-prediction window.

Confirmed 2026-09-07: the battery system enters a forced-discharge mode at a
fixed clock time (forced_discharge_start_hour), after which the battery-
prediction forecast is meaningless. The battery-prediction path must stop
being eligible to START a new heat early enough that even a full heating
cycle (whichever of force_heat_max_duration_hours/
legionella_max_cycle_duration_hours is longer) finishes before forced
discharge begins - without changing battery_prediction_deadline_hour itself,
which is still a sensible forecast target.
"""

from __future__ import annotations

import asyncio
import datetime as datetime_module
import json
from pathlib import Path
from unittest import mock

import hotwater_automation_core as core
from _fakes import FakeMelCloudClient


# --- derive_forced_discharge_start_hour (pure) ------------------------------
#
# config.yaml's forced_discharge_start_hour used to be a hand-maintained
# number that could (and did - confirmed 2026-09-10) drift out of sync with
# battery_mode_daemon_config.json's actual schedule. These tests cover
# reading it straight from the schedule instead.


def test_no_force_discharge_time_range_returns_none():
    time_ranges = [
        {"start_time": "00:00", "end_time": "05:30", "battery_mode": "FORCE_CHARGE"},
        {"start_time": "05:30", "end_time": "22:00", "battery_mode": "SELF_USE"},
    ]
    assert core.derive_forced_discharge_start_hour(time_ranges) is None


def test_single_force_discharge_time_range_returns_its_start_hour():
    time_ranges = [
        {"start_time": "05:30", "end_time": "22:00", "battery_mode": "SELF_USE"},
        {"start_time": "22:00", "end_time": "23:30", "battery_mode": "FORCE_DISCHARGE"},
        {"start_time": "23:30", "end_time": "00:00", "battery_mode": "FORCE_CHARGE"},
    ]
    assert core.derive_forced_discharge_start_hour(time_ranges) == 22.0


def test_multiple_force_discharge_time_ranges_returns_the_earliest_start_hour():
    time_ranges = [
        {"start_time": "16:00", "end_time": "16:30", "battery_mode": "FORCE_DISCHARGE"},
        {"start_time": "22:00", "end_time": "23:30", "battery_mode": "FORCE_DISCHARGE"},
    ]
    assert core.derive_forced_discharge_start_hour(time_ranges) == 16.0


def test_malformed_start_time_is_skipped_not_raised():
    time_ranges = [
        {"start_time": "not-a-time", "end_time": "23:30", "battery_mode": "FORCE_DISCHARGE"},
        {"start_time": "22:15", "end_time": "23:30", "battery_mode": "FORCE_DISCHARGE"},
    ]
    assert core.derive_forced_discharge_start_hour(time_ranges) == 22.25


def test_empty_time_ranges_returns_none():
    assert core.derive_forced_discharge_start_hour([]) is None


# --- load_battery_daemon_time_ranges (I/O) ----------------------------------


def test_load_battery_daemon_time_ranges_missing_file_returns_none(tmp_path, monkeypatch):
    missing_path = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(core, "get_battery_mode_daemon_config_path", lambda: str(missing_path))
    assert core.load_battery_daemon_time_ranges() is None


def test_load_battery_daemon_time_ranges_malformed_json_returns_none(tmp_path, monkeypatch):
    bad_path = tmp_path / "battery_mode_daemon_config.json"
    bad_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(core, "get_battery_mode_daemon_config_path", lambda: str(bad_path))
    assert core.load_battery_daemon_time_ranges() is None


def test_load_battery_daemon_time_ranges_returns_the_schedule_list(tmp_path, monkeypatch):
    config_path = tmp_path / "battery_mode_daemon_config.json"
    config_path.write_text(
        json.dumps(
            {
                "schedule": {
                    "enabled": True,
                    "time_ranges": [
                        {"start_time": "22:00", "end_time": "23:30", "battery_mode": "FORCE_DISCHARGE"}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "get_battery_mode_daemon_config_path", lambda: str(config_path))
    assert core.load_battery_daemon_time_ranges() == [
        {"start_time": "22:00", "end_time": "23:30", "battery_mode": "FORCE_DISCHARGE"}
    ]


# --- resolve_battery_prediction_eligibility_end_hour (I/O + pure, combined) -


def test_resolve_eligibility_end_hour_prefers_the_schedule_over_hw_config(tmp_path, monkeypatch):
    config_path = tmp_path / "battery_mode_daemon_config.json"
    config_path.write_text(
        json.dumps(
            {
                "schedule": {
                    "enabled": True,
                    "time_ranges": [
                        {"start_time": "22:00", "end_time": "23:30", "battery_mode": "FORCE_DISCHARGE"}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "get_battery_mode_daemon_config_path", lambda: str(config_path))
    hw_config = {
        "battery_prediction_deadline_hour": 23.5,
        "forced_discharge_start_hour": 22.5,  # stale - the schedule (22:00) must win
        "force_heat_max_duration_hours": 1.0,
        "legionella_max_cycle_duration_hours": 1.0,
    }

    assert core.resolve_battery_prediction_eligibility_end_hour(hw_config) == 21.0


def test_resolve_eligibility_end_hour_falls_back_to_hw_config_without_a_schedule(tmp_path, monkeypatch):
    missing_path = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(core, "get_battery_mode_daemon_config_path", lambda: str(missing_path))
    hw_config = {
        "battery_prediction_deadline_hour": 23.5,
        "forced_discharge_start_hour": 22.5,
        "force_heat_max_duration_hours": 1.0,
        "legionella_max_cycle_duration_hours": 1.0,
    }

    assert core.resolve_battery_prediction_eligibility_end_hour(hw_config) == 21.5


# --- battery_prediction_eligibility_end_hour (pure) ------------------------


def test_no_forced_discharge_configured_uses_deadline_hour_unchanged():
    hw_config = {"battery_prediction_deadline_hour": 23.5}
    assert core.battery_prediction_eligibility_end_hour(hw_config) == 23.5


def test_forced_discharge_closes_window_one_full_heating_cycle_earlier():
    hw_config = {
        "battery_prediction_deadline_hour": 23.5,
        "forced_discharge_start_hour": 22.5,
        "force_heat_max_duration_hours": 1.0,
        "legionella_max_cycle_duration_hours": 1.0,
    }
    assert core.battery_prediction_eligibility_end_hour(hw_config) == 21.5


def test_forced_discharge_uses_the_longer_of_the_two_duration_limits():
    hw_config = {
        "battery_prediction_deadline_hour": 23.5,
        "forced_discharge_start_hour": 22.5,
        "force_heat_max_duration_hours": 1.0,
        "legionella_max_cycle_duration_hours": 3.0,  # longer - a legionella
        # upgrade must also definitely finish before forced discharge
    }
    assert core.battery_prediction_eligibility_end_hour(hw_config) == 19.5


def test_forced_discharge_starting_late_falls_back_to_the_deadline():
    """If forced discharge starts so late that subtracting the duration
    would land AFTER the deadline, the deadline itself still governs -
    the window was never going to be open past its own forecast target
    anyway.
    """
    hw_config = {
        "battery_prediction_deadline_hour": 23.5,
        "forced_discharge_start_hour": 23.9,
        "force_heat_max_duration_hours": 0.1,
        "legionella_max_cycle_duration_hours": 0.1,
    }
    assert core.battery_prediction_eligibility_end_hour(hw_config) == 23.5


# --- end-to-end via run_force_heat_check ------------------------------------


class _FrozenDateTime(datetime_module.datetime):
    _frozen_now: datetime_module.datetime

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003
        return cls._frozen_now


def _freeze(monkeypatch, hour: int, minute: int) -> None:
    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = datetime_module.datetime(2026, 1, 15, hour, minute, tzinfo=datetime_module.UTC)
    monkeypatch.setattr(core, "datetime", frozen)


def _run(
    tmp_path: Path,
    monkeypatch,
    *,
    hour: int,
    minute: int,
    battery_daemon_time_ranges: list[dict] | None = None,
):
    _freeze(monkeypatch, hour, minute)
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(json.dumps({}), encoding="utf-8")

    # No battery_daemon_config.json by default - load_battery_daemon_time_ranges
    # returns None, so hw_config's own forced_discharge_start_hour is used
    # unchanged, exactly as before that function existed.
    daemon_config_path = tmp_path / "battery_mode_daemon_config.json"
    if battery_daemon_time_ranges is not None:
        daemon_config_path.write_text(
            json.dumps({"schedule": {"enabled": True, "time_ranges": battery_daemon_time_ranges}}),
            encoding="utf-8",
        )

    client = FakeMelCloudClient(tank_temp=30.0)
    hw_config = {
        "tank_temp_threshold_c": 45.0,
        "trigger_hour": 21.5,
        "battery_prediction_window_start_hour": 18.0,
        "battery_prediction_deadline_hour": 23.5,
        "forced_discharge_start_hour": 22.5,
        "force_heat_max_duration_hours": 1.0,
        "legionella_max_cycle_duration_hours": 1.0,
        "legionella_interval_days": 90,
        "daily_check_hour": 18.0,
        "offpeak_start": "23:30",
        "offpeak_end": "05:30",
    }
    config = {"location": {"default_timezone_str": "UTC"}}

    with (
        mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)),
        mock.patch.object(core, "get_battery_mode_daemon_config_path", lambda: str(daemon_config_path)),
        mock.patch.object(core, "MelCloudClient", lambda config_path=None: client),
        mock.patch.object(core, "is_car_charging_confirmed", mock.AsyncMock(return_value=False)),
        mock.patch.object(
            core, "get_battery_prediction_to_deadline", lambda *a, **k: (99.0, "mocked: confident")
        ),
    ):
        exit_code = asyncio.run(
            core.run_force_heat_check(config, hw_config, dry_run=False, quiet=True)
        )

    return exit_code, client


def test_confident_prediction_still_heats_just_before_the_forced_discharge_cutoff(
    tmp_path, monkeypatch
):
    """21:29 is still inside [18:00, 21:30) - the window closes AT 21:30, not
    before it.
    """
    exit_code, client = _run(tmp_path, monkeypatch, hour=21, minute=29)
    assert exit_code == 0
    assert client.force_calls == [True]


def test_confident_prediction_no_longer_heats_after_the_forced_discharge_cutoff(
    tmp_path, monkeypatch
):
    """21:30 itself, and later, must NOT trigger via the prediction path -
    even though the mocked prediction is confidently positive, a heat
    started here could still be running when forced discharge begins at
    22:30."""
    exit_code, client = _run(tmp_path, monkeypatch, hour=21, minute=30)
    assert exit_code == 0
    assert client.force_calls == []


def test_still_closed_well_before_the_old_2330_deadline(tmp_path, monkeypatch):
    """Before this change, 22:00 would have been comfortably inside the
    battery-prediction window (18:00-23:30) - confirms the window has
    genuinely narrowed, not just shifted its rounding.
    """
    exit_code, client = _run(tmp_path, monkeypatch, hour=22, minute=0)
    assert exit_code == 0
    assert client.force_calls == []


# --- schedule-derived forced_discharge_start_hour overrides hw_config's ----
#
# hw_config's forced_discharge_start_hour is 22.5 (22:30) in every test above
# via _run's fixed hw_config - but confirmed 2026-09-10, the real battery
# daemon schedule actually starts FORCE_DISCHARGE at 22:00. These tests
# confirm the schedule wins once battery_mode_daemon_config.json is present,
# narrowing the eligibility cutoff to 21:00 (22:00 - the 1h heating cycle)
# rather than the stale config.yaml-derived 21:30.


def test_schedule_derived_discharge_start_narrows_the_cutoff_to_2100(tmp_path, monkeypatch):
    """20:59 is still inside [18:00, 21:00) once the schedule (not hw_config's
    stale 22.5) governs the cutoff."""
    exit_code, client = _run(
        tmp_path,
        monkeypatch,
        hour=20,
        minute=59,
        battery_daemon_time_ranges=[
            {"start_time": "22:00", "end_time": "23:30", "battery_mode": "FORCE_DISCHARGE"}
        ],
    )
    assert exit_code == 0
    assert client.force_calls == [True]


def test_schedule_derived_discharge_start_closes_the_stale_2130_gap(tmp_path, monkeypatch):
    """21:00-21:29 used to be inside the (stale) [18:00, 21:30) window and
    would heat - but a heat starting there would still be running when the
    real 22:00 discharge begins. With the schedule wired in, this must no
    longer heat."""
    exit_code, client = _run(
        tmp_path,
        monkeypatch,
        hour=21,
        minute=0,
        battery_daemon_time_ranges=[
            {"start_time": "22:00", "end_time": "23:30", "battery_mode": "FORCE_DISCHARGE"}
        ],
    )
    assert exit_code == 0
    assert client.force_calls == []


def test_missing_battery_daemon_config_falls_back_to_hw_config_value(tmp_path, monkeypatch):
    """No battery_mode_daemon_config.json at all (the default in every test
    above) - falls back to hw_config's own forced_discharge_start_hour (22.5),
    unchanged from before this feature existed."""
    exit_code, client = _run(tmp_path, monkeypatch, hour=21, minute=29)
    assert exit_code == 0
    assert client.force_calls == [True]
