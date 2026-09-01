"""End-to-end tests for the trigger_hour car-charging cutoff in
hotwater_automation_core.py's force-heat check.

Unlike test_hotwater_legionella_coupling.py (which mocks is_car_charging_confirmed
away entirely), these tests exercise the real is_car_charging_confirmed +
hour_float_to_time + is_in_evening_window wiring end-to-end via
run_force_heat_check, only mocking the raw Ohme power reading and the live
battery SoC - the actual behavior being pinned down:
- car confirmed charging before trigger_hour -> force-heat, regardless of
  battery/off-peak
- at/after trigger_hour, car charging is no longer checked at all (even if
  it's actually happening) - only battery surplus / off-peak matters
- before trigger_hour with no car charging and not in the evening window ->
  no action
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
    ohme_power_watts: float | None,
    battery_soc_percent: float | None,
    grid_is_cheap: bool = False,
) -> tuple[int, FakeMelCloudClient]:
    _freeze_time_of_day(monkeypatch, hour, minute)
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(json.dumps({}), encoding="utf-8")

    client = FakeMelCloudClient()
    hw_config = {
        "tank_temp_threshold_c": 45.0,
        "trigger_hour": 21.5,
        "battery_soc_min_percent": 50.0,
        "offpeak_start": "23:30",
        "offpeak_end": "05:30",
        "legionella_interval_days": 90,
    }
    config = {"location": {"default_timezone_str": "UTC"}}

    offpeak_start = "23:30" if not grid_is_cheap else "00:00"
    offpeak_end = "05:30" if not grid_is_cheap else "23:59"
    hw_config["offpeak_start"] = offpeak_start
    hw_config["offpeak_end"] = offpeak_end

    with (
        mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)),
        mock.patch.object(core, "MelCloudClient", lambda config_path=None: client),
        mock.patch.object(
            core, "_get_ohme_charging_power_watts", mock.AsyncMock(return_value=ohme_power_watts)
        ),
        mock.patch.object(core, "get_battery_soc_percent", lambda cfg: battery_soc_percent),
    ):
        exit_code = asyncio.run(
            core.run_force_heat_check(config, hw_config, dry_run=False, quiet=True)
        )

    return exit_code, client


def test_car_charging_before_trigger_hour_heats_even_with_no_battery_surplus(
    tmp_path, monkeypatch
):
    # Two consecutive above-threshold cycles are needed to confirm - a single
    # call only sees cycle 1, so seed state at cycle 1 first, then run for real.
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(
        json.dumps({"ohme_charging_confirm_cycles": 1}), encoding="utf-8"
    )
    _freeze_time_of_day(monkeypatch, 19, 0)
    client = FakeMelCloudClient()
    hw_config = {"tank_temp_threshold_c": 45.0, "trigger_hour": 21.5, "battery_soc_min_percent": 50.0}
    config = {"location": {"default_timezone_str": "UTC"}}

    with (
        mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)),
        mock.patch.object(core, "MelCloudClient", lambda config_path=None: client),
        mock.patch.object(
            core, "_get_ohme_charging_power_watts", mock.AsyncMock(return_value=600.0)
        ),
        mock.patch.object(core, "get_battery_soc_percent", lambda cfg: 10.0),
    ):
        exit_code = asyncio.run(
            core.run_force_heat_check(config, hw_config, dry_run=False, quiet=True)
        )

    assert exit_code == 0
    assert client.force_calls == [True]


def test_before_trigger_hour_no_car_charging_and_daytime_does_nothing(tmp_path, monkeypatch):
    exit_code, client = _run(
        tmp_path,
        monkeypatch,
        hour=12,
        minute=0,
        ohme_power_watts=0.0,
        battery_soc_percent=90.0,
    )
    assert exit_code == 0
    assert client.force_calls == []


def test_at_trigger_hour_with_battery_surplus_heats_regardless_of_car(tmp_path, monkeypatch):
    exit_code, client = _run(
        tmp_path,
        monkeypatch,
        hour=21,
        minute=30,
        ohme_power_watts=9999.0,  # would confirm-charge if it were still being checked
        battery_soc_percent=90.0,
    )
    assert exit_code == 0
    assert client.force_calls == [True]


def test_at_trigger_hour_without_battery_surplus_or_offpeak_waits(tmp_path, monkeypatch):
    exit_code, client = _run(
        tmp_path,
        monkeypatch,
        hour=21,
        minute=30,
        ohme_power_watts=9999.0,
        battery_soc_percent=10.0,
        grid_is_cheap=False,
    )
    assert exit_code == 0
    assert client.force_calls == []


def test_after_trigger_hour_in_offpeak_window_heats(tmp_path, monkeypatch):
    exit_code, client = _run(
        tmp_path,
        monkeypatch,
        hour=23,
        minute=45,
        ohme_power_watts=None,
        battery_soc_percent=10.0,
        grid_is_cheap=True,
    )
    assert exit_code == 0
    assert client.force_calls == [True]


def test_ohme_check_is_skipped_entirely_at_or_after_trigger_hour(tmp_path, monkeypatch):
    """Confirms the "no longer even checked" part, not just "ignored"."""
    _freeze_time_of_day(monkeypatch, 21, 30)
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(json.dumps({}), encoding="utf-8")
    client = FakeMelCloudClient()
    hw_config = {"tank_temp_threshold_c": 45.0, "trigger_hour": 21.5, "battery_soc_min_percent": 50.0}
    config = {"location": {"default_timezone_str": "UTC"}}

    with (
        mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)),
        mock.patch.object(core, "MelCloudClient", lambda config_path=None: client),
        mock.patch.object(core, "_get_ohme_charging_power_watts") as fake_get_power,
        mock.patch.object(core, "get_battery_soc_percent", lambda cfg: 10.0),
    ):
        asyncio.run(core.run_force_heat_check(config, hw_config, dry_run=False, quiet=True))

    fake_get_power.assert_not_called()
