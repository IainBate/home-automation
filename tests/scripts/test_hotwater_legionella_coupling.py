"""Tests for the legionella/force-heat coupling in hotwater_automation_core.py.

A legionella cycle has no schedule of its own - it rides on the exact same
force-heat trigger as a normal heat (see determine_hotwater_decision), gated
only by a minimum days-since-last-cycle check (_is_legionella_due). These
tests exercise run_force_heat_check end-to-end with a fake MELCloud client
(no real hardware/network) to pin down that coupling:
- never run before / overdue -> starts a legionella cycle instead of a
  normal one
- recently completed -> normal force-heat, untouched
- due, but the unit can't reach the legionella target -> falls back to a
  normal force-heat rather than blocking heating altogether
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import hotwater_automation_core as core


class FakeMelCloudClient:
    """Stand-in for MelCloudClient - records calls instead of touching MELCloud."""

    def __init__(self, *, target_temp: float, tank_temp: float, max_temp: float | None) -> None:
        self.target_temp = target_temp
        self.tank_temp = tank_temp
        self.max_temp = max_temp
        self.force_calls: list[bool] = []
        self.target_temp_calls: list[float] = []

    async def connect(self) -> None:
        return None

    async def get_tank_status(self) -> dict:
        return {
            "tank_temperature": self.tank_temp,
            "target_tank_temperature": self.target_temp,
            "target_tank_temperature_max": self.max_temp,
            "operation_mode": core.HotWaterOperationMode.AUTO,
        }

    async def set_force_hot_water(self, *, enabled: bool) -> bool:
        self.force_calls.append(enabled)
        return True

    async def set_target_tank_temperature(self, temp: float) -> None:
        self.target_temp_calls.append(temp)
        self.target_temp = temp

    async def close(self) -> None:
        return None


def _run_force_heat_check(tmp_path: Path, *, legionella_last_completed_days_ago: float | None, max_temp: float = 65.0):
    state_path = tmp_path / "hotwater_automation_state.json"
    if legionella_last_completed_days_ago is None:
        initial_state = {}
    else:
        completed_at = datetime.now(tz=UTC) - timedelta(days=legionella_last_completed_days_ago)
        initial_state = {
            "legionella": {"cycle_in_progress": False, "last_completed_at": completed_at.isoformat()}
        }
    state_path.write_text(json.dumps(initial_state), encoding="utf-8")

    client = FakeMelCloudClient(target_temp=45.0, tank_temp=30.0, max_temp=max_temp)
    hw_config = {
        "tank_temp_threshold_c": 45.0,
        "legionella_interval_days": 90,
        "legionella_target_temp_c": 60.0,
    }
    config = {"location": {"default_timezone_str": "Europe/London"}}

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)), \
         mock.patch.object(core, "MelCloudClient", lambda config_path=None: client), \
         mock.patch.object(core, "is_car_charging_confirmed", mock.AsyncMock(return_value=True)):
        exit_code = asyncio.run(
            core.run_force_heat_check(config, hw_config, dry_run=False, quiet=True)
        )

    final_state = json.loads(state_path.read_text())
    return exit_code, client, final_state


def test_legionella_never_run_starts_a_legionella_cycle(tmp_path):
    exit_code, client, final_state = _run_force_heat_check(
        tmp_path, legionella_last_completed_days_ago=None
    )
    assert exit_code == 0
    assert client.force_calls == [True]
    assert client.target_temp_calls == [60.0]
    assert final_state["legionella"]["cycle_in_progress"] is True
    assert "force_heat_activated_at" not in final_state


def test_legionella_recently_completed_does_a_normal_force_heat(tmp_path):
    exit_code, client, final_state = _run_force_heat_check(
        tmp_path, legionella_last_completed_days_ago=10
    )
    assert exit_code == 0
    assert client.force_calls == [True]
    assert client.target_temp_calls == []
    assert final_state["legionella"]["cycle_in_progress"] is False
    assert "force_heat_activated_at" in final_state


def test_legionella_overdue_starts_a_legionella_cycle(tmp_path):
    exit_code, client, final_state = _run_force_heat_check(
        tmp_path, legionella_last_completed_days_ago=95
    )
    assert exit_code == 0
    assert client.target_temp_calls == [60.0]
    assert final_state["legionella"]["cycle_in_progress"] is True


def test_legionella_never_runs_sooner_than_the_interval():
    """The interval boundary itself: 89 days is not due, 90 days is."""
    # Covered at the unit level, independent of MELCloud, since it's a pure
    # date calculation - avoids depending on tmp_path plumbing for a boundary check.
    not_due_state = {
        "last_completed_at": (datetime.now(tz=UTC) - timedelta(days=89)).isoformat()
    }
    due_state = {
        "last_completed_at": (datetime.now(tz=UTC) - timedelta(days=90)).isoformat()
    }
    hw_config = {"legionella_interval_days": 90}
    assert core._is_legionella_due(hw_config, not_due_state) is False
    assert core._is_legionella_due(hw_config, due_state) is True


def test_legionella_due_but_unit_cannot_reach_target_falls_back_to_normal_heat(tmp_path):
    exit_code, client, final_state = _run_force_heat_check(
        tmp_path, legionella_last_completed_days_ago=None, max_temp=50.0
    )
    assert exit_code == 0
    assert client.force_calls == [True]
    assert client.target_temp_calls == []
    assert final_state.get("legionella", {}).get("cycle_in_progress") is not True
    assert "force_heat_activated_at" in final_state


def test_legionella_in_progress_defers_the_normal_force_heat_check(tmp_path):
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(
        json.dumps({"legionella": {"cycle_in_progress": True}}), encoding="utf-8"
    )
    client = FakeMelCloudClient(target_temp=45.0, tank_temp=30.0, max_temp=65.0)
    hw_config = {"tank_temp_threshold_c": 45.0}
    config = {"location": {"default_timezone_str": "Europe/London"}}

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)), \
         mock.patch.object(core, "MelCloudClient", lambda config_path=None: client), \
         mock.patch.object(core, "is_car_charging_confirmed", mock.AsyncMock(return_value=True)):
        exit_code = asyncio.run(
            core.run_force_heat_check(config, hw_config, dry_run=False, quiet=True)
        )

    assert exit_code == 0
    assert client.force_calls == []
