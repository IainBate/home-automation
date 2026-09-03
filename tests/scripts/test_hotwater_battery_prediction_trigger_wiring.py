"""End-to-end tests for the battery-prediction trigger path's wiring into
run_force_heat_check (hotwater_automation_core.py) - an independent path
alongside car charging and the evening/off-peak window, active across
[battery_prediction_window_start_hour, battery_prediction_deadline_hour)
regardless of trigger_hour, gated on get_battery_prediction_to_deadline
rather than a live/averaged SoC reading. Also covers service_mode_active
dominating this path exactly like holiday_mode_active.

Mocks get_battery_prediction_to_deadline directly (its own internal
predict_evening_soc/historical-data wiring is covered by
test_hotwater_battery_prediction.py) so these tests focus purely on how the
force-heat check uses its result.
"""

from __future__ import annotations

import asyncio
import datetime as datetime_module
import json
from pathlib import Path
from unittest import mock

import hotwater_automation_core as core


class _FrozenDateTime(datetime_module.datetime):
    _frozen_now: datetime_module.datetime

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003
        return cls._frozen_now


def _freeze_time_of_day(monkeypatch, hour: int, minute: int) -> None:
    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = datetime_module.datetime(
        2026, 1, 15, hour, minute, tzinfo=datetime_module.UTC
    )
    monkeypatch.setattr(core, "datetime", frozen)


class FakeMelCloudClient:
    """Cold tank, never already force-heating - so any trigger fires a heat."""

    def __init__(self) -> None:
        self.force_calls: list[bool] = []

    async def connect(self) -> None:
        return None

    async def get_tank_status(self) -> dict:
        return {
            "tank_temperature": 30.0,
            "target_tank_temperature": 45.0,
            "target_tank_temperature_max": 65.0,
            "operation_mode": core.HotWaterOperationMode.AUTO,
        }

    async def set_force_hot_water(self, *, enabled: bool) -> bool:
        self.force_calls.append(enabled)
        return True

    async def set_target_tank_temperature(self, temp: float) -> None:
        return None

    async def close(self) -> None:
        return None


def _run(
    tmp_path: Path,
    monkeypatch,
    *,
    hour: int,
    minute: int,
    predicted_min_percent: float | None,
    state: dict | None = None,
    hw_config_overrides: dict | None = None,
):
    _freeze_time_of_day(monkeypatch, hour, minute)
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(json.dumps(state or {}), encoding="utf-8")

    client = FakeMelCloudClient()
    hw_config = {
        "tank_temp_threshold_c": 45.0,
        "trigger_hour": 21.5,
        "battery_soc_min_percent": 20.0,
        "battery_prediction_window_start_hour": 15.0,
        "battery_prediction_deadline_hour": 23.5,
        "car_charging_trigger_start_hour": 15.0,
        "offpeak_start": "23:30",
        "offpeak_end": "05:30",
        "legionella_interval_days": 90,
    }
    hw_config.update(hw_config_overrides or {})
    config = {"location": {"default_timezone_str": "UTC"}}

    with (
        mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)),
        mock.patch.object(core, "MelCloudClient", lambda config_path=None: client),
        mock.patch.object(
            core, "_get_ohme_charging_power_watts", mock.AsyncMock(return_value=0.0)
        ),
        mock.patch.object(core, "get_battery_soc_percent", lambda cfg: 10.0),
        mock.patch.object(
            core,
            "get_battery_prediction_to_deadline",
            lambda cfg, hw, now_local, deadline: (predicted_min_percent, "stub reason"),
        ),
    ):
        exit_code = asyncio.run(
            core.run_force_heat_check(config, hw_config, dry_run=False, quiet=True)
        )

    return exit_code, client


def test_battery_prediction_trigger_heats_in_the_afternoon_before_trigger_hour(
    tmp_path, monkeypatch
):
    """3:30pm - within the battery-prediction window, well before the 9:30pm
    trigger_hour and outside off-peak - only the prediction path can fire this.
    """
    exit_code, client = _run(tmp_path, monkeypatch, hour=15, minute=30, predicted_min_percent=25.0)

    assert exit_code == 0
    assert client.force_calls == [True]


def test_battery_prediction_below_threshold_does_not_heat(tmp_path, monkeypatch):
    exit_code, client = _run(tmp_path, monkeypatch, hour=15, minute=30, predicted_min_percent=15.0)

    assert exit_code == 0
    assert client.force_calls == []


def test_battery_prediction_unavailable_does_not_heat(tmp_path, monkeypatch):
    exit_code, client = _run(tmp_path, monkeypatch, hour=15, minute=30, predicted_min_percent=None)

    assert exit_code == 0
    assert client.force_calls == []


def test_battery_prediction_is_not_checked_before_the_window_opens(tmp_path, monkeypatch):
    """Before battery_prediction_window_start_hour (default 3pm) - must not
    even call get_battery_prediction_to_deadline, mirroring
    test_hotwater_trigger_time_gating.py's "not even checked" pattern.
    """
    _freeze_time_of_day(monkeypatch, 12, 0)
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(json.dumps({}), encoding="utf-8")
    client = FakeMelCloudClient()
    hw_config = {
        "tank_temp_threshold_c": 45.0,
        "trigger_hour": 21.5,
        "battery_soc_min_percent": 20.0,
        "battery_prediction_window_start_hour": 15.0,
        "battery_prediction_deadline_hour": 23.5,
    }
    config = {"location": {"default_timezone_str": "UTC"}}

    with (
        mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)),
        mock.patch.object(core, "MelCloudClient", lambda config_path=None: client),
        mock.patch.object(
            core, "_get_ohme_charging_power_watts", mock.AsyncMock(return_value=0.0)
        ),
        mock.patch.object(core, "get_battery_soc_percent", lambda cfg: 10.0),
        mock.patch.object(core, "get_battery_prediction_to_deadline") as fake_predict,
    ):
        asyncio.run(core.run_force_heat_check(config, hw_config, dry_run=False, quiet=True))

    fake_predict.assert_not_called()
    assert client.force_calls == []


def test_service_mode_blocks_the_battery_prediction_trigger(tmp_path, monkeypatch):
    exit_code, client = _run(
        tmp_path,
        monkeypatch,
        hour=15,
        minute=30,
        predicted_min_percent=25.0,
        state={"service_mode": {"active": True}},
    )

    assert exit_code == 0
    assert client.force_calls == []


def test_holiday_mode_blocks_the_battery_prediction_trigger(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    until = (datetime.now(tz=UTC) + timedelta(days=1)).isoformat()
    exit_code, client = _run(
        tmp_path,
        monkeypatch,
        hour=15,
        minute=30,
        predicted_min_percent=25.0,
        state={"holiday": {"until": until}},
    )

    assert exit_code == 0
    assert client.force_calls == []
