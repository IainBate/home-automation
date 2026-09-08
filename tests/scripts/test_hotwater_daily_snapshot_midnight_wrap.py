"""Regression tests for daily_check_lookup_date_str and its use in
_run_force_heat_check_locked / _refresh_daily_snapshot_if_warm.

Real gap found 2026-09-07: every read of the once-a-day daily_check snapshot
compared its "date" field against a plain now_local.date().isoformat() -
correct for a decision made the same afternoon/evening the snapshot was
taken, but wrong for the tail of that same overnight session after midnight
(the snapshot's date is now "yesterday" relative to the fresh calendar date).
That silently made the tank's temperature read as unavailable for the whole
00:00-offpeak_end (05:30) stretch, which is exactly the part of the night
where car-charging and battery-prediction have both already closed and only
"grid is now off-peak, heat regardless" is left to cover a still-cold tank.
"""

from __future__ import annotations

import asyncio
import datetime as datetime_module
import json
from pathlib import Path
from unittest import mock

import hotwater_automation_core as core
from _fakes import FakeMelCloudClient


class _FrozenDateTime(datetime_module.datetime):
    _frozen_now: datetime_module.datetime

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003
        return cls._frozen_now


def _freeze(monkeypatch, moment: datetime_module.datetime) -> None:
    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = moment
    monkeypatch.setattr(core, "datetime", frozen)


def _run(tmp_path: Path, monkeypatch, *, moment, initial_state: dict, tank_temp: float = 30.0):
    _freeze(monkeypatch, moment)
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(json.dumps(initial_state), encoding="utf-8")

    client = FakeMelCloudClient(tank_temp=tank_temp)
    hw_config = {
        "tank_temp_threshold_c": 45.0,
        "trigger_hour": 0.0,  # always "evening" - isolates the off-peak branch
        "battery_soc_min_percent": 0.0,
        "legionella_interval_days": 90,
        "legionella_target_temp_c": 60.0,
        "daily_check_hour": 18.0,
        "offpeak_start": "23:30",
        "offpeak_end": "05:30",
    }
    config = {"location": {"default_timezone_str": "UTC"}}

    with (
        mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)),
        mock.patch.object(core, "MelCloudClient", lambda config_path=None: client),
        mock.patch.object(core, "is_car_charging_confirmed", mock.AsyncMock(return_value=False)),
        mock.patch.object(core, "get_battery_soc_percent", lambda cfg: 100.0),
    ):
        exit_code = asyncio.run(
            core.run_force_heat_check(config, hw_config, dry_run=False, quiet=True)
        )

    final_state = json.loads(state_path.read_text())
    return exit_code, client, final_state


# --- daily_check_lookup_date_str (pure) ------------------------------------


def test_lookup_date_before_offpeak_end_is_yesterday():
    hw_config = {"offpeak_end": "05:30"}
    now_local = datetime_module.datetime(2026, 1, 16, 2, 0)
    assert core.daily_check_lookup_date_str(hw_config, now_local) == "2026-01-15"


def test_lookup_date_at_offpeak_end_is_today():
    hw_config = {"offpeak_end": "05:30"}
    now_local = datetime_module.datetime(2026, 1, 16, 5, 30)
    assert core.daily_check_lookup_date_str(hw_config, now_local) == "2026-01-16"


def test_lookup_date_in_the_afternoon_is_today():
    hw_config = {"offpeak_end": "05:30"}
    now_local = datetime_module.datetime(2026, 1, 16, 18, 0)
    assert core.daily_check_lookup_date_str(hw_config, now_local) == "2026-01-16"


# --- end-to-end via run_force_heat_check ------------------------------------


def test_off_peak_heat_after_midnight_uses_yesterdays_snapshot(tmp_path, monkeypatch):
    """The actual regression: a cold tank at 18:00 yesterday, still cold at
    2am the next calendar day, off-peak now open - must heat. Before the
    fix, the date mismatch made decision_tank_temperature None and this
    stayed unheated the entire night.

    No last_completed_at is seeded, so a never-run legionella cycle is
    correctly treated as due and this heat is upgraded to one (a bonus
    confirmation that the legionella-due check - which reads the same
    corrected date - resolved correctly too) rather than a plain force-heat.
    """
    seeded_state = {
        "daily_check": {"date": "2026-01-15", "tank_temperature_c": 30.0, "below_threshold": True}
    }
    exit_code, client, final_state = _run(
        tmp_path,
        monkeypatch,
        moment=datetime_module.datetime(2026, 1, 16, 2, 0, tzinfo=datetime_module.UTC),
        initial_state=seeded_state,
        tank_temp=30.0,
    )
    assert exit_code == 0
    assert client.force_calls == [True]
    assert final_state["legionella"]["cycle_in_progress"] is True


def test_no_heat_before_offpeak_and_before_todays_snapshot_at_2am_still_correct(tmp_path, monkeypatch):
    """Sanity check the fix doesn't overreach: at 2am with NO prior snapshot
    at all (e.g. the daemon only just started), still correctly reports
    "can't decide" rather than inventing one.
    """
    exit_code, client, final_state = _run(
        tmp_path,
        monkeypatch,
        moment=datetime_module.datetime(2026, 1, 16, 2, 0, tzinfo=datetime_module.UTC),
        initial_state={},
        tank_temp=30.0,
    )
    assert exit_code == 0
    assert client.force_calls == []


def test_refresh_daily_snapshot_if_warm_finds_yesterdays_pin_after_midnight(tmp_path, monkeypatch):
    """_refresh_daily_snapshot_if_warm must also resolve the same
    yesterday's-dated pin after midnight, or a heat completed just after
    midnight would leave a stale "below threshold" record that the next
    force-heat tick (still before today's real 18:00 check) could
    re-trigger against.
    """
    state = {
        "daily_check": {"date": "2026-01-15", "tank_temperature_c": 30.0, "below_threshold": True}
    }
    hw_config = {"tank_temp_threshold_c": 45.0, "offpeak_end": "05:30"}
    now_local = datetime_module.datetime(2026, 1, 16, 2, 30, tzinfo=datetime_module.UTC)

    core._refresh_daily_snapshot_if_warm(hw_config, state, 50.0, now_local)

    assert state["daily_check"]["below_threshold"] is False
    assert state["daily_check"]["date"] == "2026-01-15"  # left as the original session's date
