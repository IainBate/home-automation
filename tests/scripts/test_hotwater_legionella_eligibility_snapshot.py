"""Tests for the daily legionella eligibility snapshot in hotwater_automation_core.py.

A legionella cycle rides on the same force-heat trigger as a normal heat,
but that trigger's own timing (car charging, or the evening/battery/
off-peak check) can land at any hour, including the middle of the night.
_update_legionella_eligibility_snapshot decouples "is a legionella cycle due
today" from that incidental timing: once a day, at legionella_check_hour
(default 18:00), it records whether the tank was below threshold. Only that
snapshot - not a live reading taken whenever the trigger happens to fire -
feeds run_force_heat_check's legionella_due gate.

These tests exercise run_force_heat_check end-to-end (like
test_hotwater_legionella_coupling.py) with a frozen clock (like
test_hotwater_trigger_time_gating.py) to pin down the snapshot's own timing.
"""

from __future__ import annotations

import asyncio
import datetime as datetime_module
import json
from pathlib import Path
from unittest import mock

import hotwater_automation_core as core


class _FrozenDateTime(datetime_module.datetime):
    """A datetime subclass whose now() always returns a fixed UTC instant."""

    _frozen_now: datetime_module.datetime

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003 - matching datetime.now's signature
        return cls._frozen_now


def _freeze_time_of_day(monkeypatch, hour: int, minute: int, *, day: int = 15) -> None:
    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = datetime_module.datetime(
        2026, 1, day, hour, minute, tzinfo=datetime_module.UTC
    )
    monkeypatch.setattr(core, "datetime", frozen)


class FakeMelCloudClient:
    """Cold tank, never already force-heating - so any trigger fires a heat."""

    def __init__(self, *, tank_temp: float = 30.0) -> None:
        self.tank_temp = tank_temp
        self.force_calls: list[bool] = []
        self.target_temp_calls: list[float] = []

    async def connect(self) -> None:
        return None

    async def get_tank_status(self) -> dict:
        return {
            "tank_temperature": self.tank_temp,
            "target_tank_temperature": 45.0,
            "target_tank_temperature_max": 65.0,
            "operation_mode": core.HotWaterOperationMode.AUTO,
        }

    async def set_force_hot_water(self, *, enabled: bool) -> bool:
        self.force_calls.append(enabled)
        return True

    async def set_target_tank_temperature(self, temp: float) -> None:
        self.target_temp_calls.append(temp)

    async def close(self) -> None:
        return None


def _run(tmp_path: Path, monkeypatch, *, hour: int, minute: int, initial_state: dict, tank_temp: float = 30.0):
    _freeze_time_of_day(monkeypatch, hour, minute)
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(json.dumps(initial_state), encoding="utf-8")

    client = FakeMelCloudClient(tank_temp=tank_temp)
    hw_config = {
        "tank_temp_threshold_c": 45.0,
        "trigger_hour": 0.0,  # Always "evening" - isolates the eligibility snapshot itself.
        "battery_soc_min_percent": 0.0,  # Always has "surplus" - same reason.
        "legionella_interval_days": 90,
        "legionella_target_temp_c": 60.0,
        "legionella_check_hour": 18.0,
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


def test_snapshot_not_taken_before_check_hour(tmp_path, monkeypatch):
    exit_code, client, final_state = _run(
        tmp_path, monkeypatch, hour=17, minute=59, initial_state={}
    )
    assert exit_code == 0
    assert "threshold_check_date" not in final_state.get("legionella", {})
    # No snapshot yet this call -> not eligible -> normal heat, not legionella.
    assert client.target_temp_calls == []


def test_snapshot_taken_at_check_hour_tank_cold_marks_eligible(tmp_path, monkeypatch):
    exit_code, client, final_state = _run(
        tmp_path, monkeypatch, hour=18, minute=0, initial_state={}, tank_temp=30.0
    )
    assert exit_code == 0
    legionella_state = final_state["legionella"]
    assert legionella_state["threshold_check_date"] == "2026-01-15"
    assert legionella_state["threshold_met_at_check"] is True
    # Never-run legionella (last_completed_at absent) + eligible today + due
    # trigger -> starts as a legionella cycle in this same call.
    assert client.target_temp_calls == [60.0]


def test_snapshot_taken_at_check_hour_tank_warm_marks_not_eligible(tmp_path, monkeypatch):
    exit_code, client, final_state = _run(
        tmp_path, monkeypatch, hour=18, minute=0, initial_state={}, tank_temp=50.0
    )
    assert exit_code == 0
    legionella_state = final_state["legionella"]
    assert legionella_state["threshold_check_date"] == "2026-01-15"
    assert legionella_state["threshold_met_at_check"] is False
    assert client.force_calls == []  # tank already >= threshold - nothing to heat at all


def test_snapshot_only_taken_once_per_day(tmp_path, monkeypatch):
    """A later same-day tick must not overwrite an earlier reading with a
    now-warm tank (e.g. after the normal heat already ran) - the snapshot is
    meant to reflect the tank's state before any heating that day, not
    whatever it reads later.
    """
    seeded_state = {
        "legionella": {"threshold_check_date": "2026-01-15", "threshold_met_at_check": True}
    }
    exit_code, client, final_state = _run(
        tmp_path, monkeypatch, hour=21, minute=0, initial_state=seeded_state, tank_temp=50.0
    )
    assert exit_code == 0
    legionella_state = final_state["legionella"]
    assert legionella_state["threshold_check_date"] == "2026-01-15"
    assert legionella_state["threshold_met_at_check"] is True  # untouched
    assert client.force_calls == []  # tank now warm - nothing to heat


def test_due_legionella_upgrades_a_later_same_day_trigger_using_the_earlier_snapshot(
    tmp_path, monkeypatch
):
    """The core scenario this exists for: the 6pm snapshot found the tank
    cold, but the actual force-heat trigger doesn't fire until later that
    night (timed by battery/off-peak as usual) - it should still be
    upgraded to a legionella cycle using that earlier reading, not whatever
    the tank happens to read at the later trigger time.
    """
    seeded_state = {
        "legionella": {"threshold_check_date": "2026-01-15", "threshold_met_at_check": True}
    }
    exit_code, client, final_state = _run(
        tmp_path, monkeypatch, hour=23, minute=0, initial_state=seeded_state, tank_temp=30.0
    )
    assert exit_code == 0
    assert client.target_temp_calls == [60.0]
    assert final_state["legionella"]["cycle_in_progress"] is True


def test_stale_snapshot_from_a_previous_day_does_not_count(tmp_path, monkeypatch):
    seeded_state = {
        "legionella": {"threshold_check_date": "2026-01-14", "threshold_met_at_check": True}
    }
    exit_code, client, final_state = _run(
        tmp_path, monkeypatch, hour=19, minute=0, initial_state=seeded_state, tank_temp=30.0
    )
    assert exit_code == 0
    # A fresh snapshot is taken this call (19:00 is >= 18:00) - tank is cold,
    # so it's re-marked eligible today, and (also due by interval) starts a
    # legionella cycle - but on the strength of *today's* fresh reading, not
    # the stale one.
    assert final_state["legionella"]["threshold_check_date"] == "2026-01-15"
    assert client.target_temp_calls == [60.0]
