"""Tests for status_collector.py's per-subsystem status collection and graceful degradation."""

from __future__ import annotations

import json
from unittest import mock

from src.api_clients.melcloud_client import HotWaterOperationMode, HotWaterStatus
from src.api_clients.ohme_ev_client import OhmeChargerMode, OhmeChargerStatus
from src.core_logic.battery_simulation.constants_and_models import BatteryMode
from src.dashboard import status_collector


class _FakeOhmeClient:
    last_init_kwargs: dict = {}

    def __init__(self, *_args, **kwargs):
        _FakeOhmeClient.last_init_kwargs = kwargs

    async def connect(self):
        return True

    async def get_charger_status(self, *, use_cache=False):  # noqa: ARG002
        return {
            "status": OhmeChargerStatus.CHARGING,
            "mode": OhmeChargerMode.SMART_CHARGE,
            "plugged_in": True,
            "power_watts": 7200,
            "battery_percent": 55,
            "target_soc": 80,
            "current_vehicle": "Test Car",
        }

    async def close(self):
        pass


class _FakeMelCloudClient:
    last_init_kwargs: dict = {}

    def __init__(self, *_args, **kwargs):
        _FakeMelCloudClient.last_init_kwargs = kwargs

    async def connect(self):
        return True

    async def get_tank_status(self, *, use_cache=False):  # noqa: ARG002
        return {
            "tank_temperature": 48.5,
            "target_tank_temperature": 50.0,
            "operation_mode": HotWaterOperationMode.AUTO,
            "status": HotWaterStatus.IDLE,
            "power": True,
            "holiday_mode": False,
        }

    async def close(self):
        pass


def _sample_bulk_data():
    return {
        "work_mode": BatteryMode.SELF_USE,
        "soc": {"master": 60, "slave": 62},
        "pv_power": {"master": {"pv1": 500, "pv2": 300}, "slave": {"pv1": 400, "pv2": 200}},
        "battery_power": {"master": {"power": -100, "mode": "Discharging"}, "slave": {"power": -50, "mode": "Discharging"}},
        "grid_power": {"master": 250, "slave": None},
        "daily_yield": {"master": 5.0, "slave": 4.0},
    }


def test_collect_solar_battery_maps_bulk_data_fields():
    with mock.patch.object(status_collector, "solax_modbus_bulk_data", return_value=_sample_bulk_data()):
        result = status_collector._collect_solar_battery({})

    assert result["available"] is True
    assert result["work_mode"] == "Self-Use"
    assert result["soc_percent_master"] == 60
    assert result["pv_power_w"] == 1400
    assert result["battery_power_w"] == -150
    assert result["grid_power_w"] == 250
    assert result["daily_yield_kwh"] == 9.0


def test_collect_solar_battery_unavailable_on_none():
    with mock.patch.object(status_collector, "solax_modbus_bulk_data", return_value=None):
        result = status_collector._collect_solar_battery({})

    assert result == {"available": False, "error": "Could not read from SolaX inverter(s)"}


def test_collect_ev_charging_disabled_in_config():
    result = status_collector._collect_ev_charging({"ohme_ev": {"enabled": False}}, "config.yaml")

    assert result["available"] is False


def test_collect_ev_charging_maps_fields():
    with mock.patch.object(status_collector, "OhmeEVClient", _FakeOhmeClient):
        result = status_collector._collect_ev_charging({"ohme_ev": {"enabled": True}}, "config.yaml")

    assert result["available"] is True
    assert result["status"] == "charging"
    assert result["mode"] == "smart_charge"
    assert result["power_watts"] == 7200
    assert result["current_vehicle"] == "Test Car"


def test_collect_ev_charging_survives_an_unexpected_field_shape():
    """Regression test: field-extraction (status["status"].value etc.) must be
    inside the same try/except as the network call - an AttributeError here
    must mark only this subsystem unavailable, not propagate out of
    collect_status() and blank every OTHER subsystem's cached data (see
    poller.py's _poll_once(), whose only outer try/except discards the whole
    snapshot on any uncaught exception).
    """

    class _FakeOhmeClientWithBadStatus(_FakeOhmeClient):
        async def get_charger_status(self, *, use_cache=False):  # noqa: ARG002
            return {"status": "charging", "mode": "smart_charge"}  # plain str, no .value

    with mock.patch.object(status_collector, "OhmeEVClient", _FakeOhmeClientWithBadStatus):
        result = status_collector._collect_ev_charging({"ohme_ev": {"enabled": True}}, "config.yaml")

    assert result["available"] is False


def test_collect_ev_charging_passes_explicit_config_path_to_client():
    """Regression test: must not rely on OhmeEVClient's cwd-relative default -
    see status_collector.collect_status()'s config_path docstring.
    """
    with mock.patch.object(status_collector, "OhmeEVClient", _FakeOhmeClient):
        status_collector._collect_ev_charging({"ohme_ev": {"enabled": True}}, "/abs/path/config.yaml")

    assert _FakeOhmeClient.last_init_kwargs.get("config_path") == "/abs/path/config.yaml"


def test_collect_hot_water_includes_force_heat_and_legionella_state(tmp_path):
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(
        json.dumps({"force_heat_activated_at": "2026-08-31T18:00:00+00:00", "legionella": {"cycle_in_progress": True}}),
        encoding="utf-8",
    )

    with (
        mock.patch.object(status_collector, "MelCloudClient", _FakeMelCloudClient),
        mock.patch.object(status_collector, "get_hotwater_automation_state_path", lambda: str(state_path)),
    ):
        result = status_collector._collect_hot_water({"melcloud": {"enabled": True}}, "config.yaml")

    assert result["available"] is True
    assert result["tank_temperature_c"] == 48.5
    assert result["force_heat_active"] is True
    assert result["legionella_cycle_in_progress"] is True


def test_collect_hot_water_passes_explicit_config_path_to_client(tmp_path):
    """Regression test: must not rely on MelCloudClient's cwd-relative default -
    see status_collector.collect_status()'s config_path docstring.
    """
    with (
        mock.patch.object(status_collector, "MelCloudClient", _FakeMelCloudClient),
        mock.patch.object(status_collector, "get_hotwater_automation_state_path", lambda: str(tmp_path / "missing.json")),
    ):
        status_collector._collect_hot_water({"melcloud": {"enabled": True}}, "/abs/path/config.yaml")

    assert _FakeMelCloudClient.last_init_kwargs.get("config_path") == "/abs/path/config.yaml"


def test_collect_status_resolves_a_default_config_path_when_none_given():
    """collect_status() itself must supply a real config_path to the per-subsystem
    collectors even when its own caller doesn't pass one (defaults to the
    project root's config.yaml, matching every other caller in this codebase).
    """
    with (
        mock.patch.object(status_collector, "solax_modbus_bulk_data", return_value=None),
        mock.patch.object(status_collector, "_collect_ev_charging") as fake_collect_ev,
        mock.patch.object(status_collector, "_collect_hot_water") as fake_collect_hot_water,
    ):
        fake_collect_ev.return_value = {"available": False}
        fake_collect_hot_water.return_value = {"available": False}
        status_collector.collect_status({"ohme_ev": {}, "melcloud": {}})

    used_config_path = fake_collect_ev.call_args[0][1]
    assert used_config_path.endswith("config.yaml")
    assert fake_collect_hot_water.call_args[0][1] == used_config_path


def test_collect_airstage_disabled():
    result = status_collector._collect_airstage({"airstage": {"enabled": False}})

    assert result["available"] is False
    assert result["disabled"] is True


def test_collect_airstage_maps_zone_list():
    fake_zones = [
        {"name": "Landing", "available": True, "mode": "HEAT", "current_temperature_c": 21.0, "target_temperature_c": 22.0, "outdoor_temperature_c": 8.0},
        {"name": "Playroom", "available": False, "error": "Could not read from Airstage unit"},
    ]
    with mock.patch.object(status_collector, "fetch_airstage_status", return_value=fake_zones):
        result = status_collector._collect_airstage({"airstage": {"enabled": True}})

    assert result == {"available": True, "zones": fake_zones}


def test_collect_airstage_unavailable_when_client_returns_none():
    with mock.patch.object(status_collector, "fetch_airstage_status", return_value=None):
        result = status_collector._collect_airstage({"airstage": {"enabled": True}})

    assert result["available"] is False


def test_collect_resideo_disabled():
    result = status_collector._collect_resideo({"resideo": {"enabled": False}})

    assert result["available"] is False
    assert result["disabled"] is True


def test_collect_resideo_maps_fields():
    fake_status = {"device_name": "Hall", "mode": "Heat", "current_temperature_c": 20.0}
    with mock.patch.object(status_collector, "fetch_resideo_status", return_value=fake_status):
        result = status_collector._collect_resideo({"resideo": {"enabled": True}})

    assert result == {"available": True, **fake_status}


def test_collect_mg_saic_disabled():
    result = status_collector._collect_mg_saic({"mg_saic": {"enabled": False}})

    assert result["available"] is False


def test_collect_mg_saic_reads_cached_file(tmp_path):
    status_path = tmp_path / "mg_saic_status.json"
    status_path.write_text(
        json.dumps({"vehicle_name": "MG ZS", "battery_percent": 62.5, "range_km": 210.0, "is_charging": True, "is_parked": True, "fetched_at": "2026-09-01T10:00:00+00:00"}),
        encoding="utf-8",
    )

    with mock.patch.object(status_collector, "get_mg_saic_status_path", lambda: str(status_path)):
        result = status_collector._collect_mg_saic({"mg_saic": {"enabled": True}})

    assert result["available"] is True
    assert result["battery_percent"] == 62.5
    assert result["range_km"] == 210.0


def test_collect_mg_saic_no_cache_yet(tmp_path):
    with mock.patch.object(status_collector, "get_mg_saic_status_path", lambda: str(tmp_path / "missing.json")):
        result = status_collector._collect_mg_saic({"mg_saic": {"enabled": True}})

    assert result["available"] is False


def test_collect_claude_usage_disabled():
    result = status_collector._collect_claude_usage({"claude_usage": {"enabled": False}})

    assert result["available"] is False


def test_collect_claude_usage_reads_cached_file(tmp_path):
    usage_path = tmp_path / "claude_usage.json"
    buckets = [{"kind": "session", "label": "Current session (5-hour)", "percent_used": 42, "resets_at": None, "severity": "normal"}]
    usage_path.write_text(
        json.dumps({"fetched_at": "2026-09-01T10:00:00+00:00", "buckets": buckets, "extra_usage_percent": None}),
        encoding="utf-8",
    )

    with mock.patch.object(status_collector, "get_claude_usage_path", lambda: str(usage_path)):
        result = status_collector._collect_claude_usage({"claude_usage": {"enabled": True}})

    assert result["available"] is True
    assert result["buckets"] == buckets


def test_collect_claude_usage_no_cache_yet(tmp_path):
    with mock.patch.object(status_collector, "get_claude_usage_path", lambda: str(tmp_path / "missing.json")):
        result = status_collector._collect_claude_usage({"claude_usage": {"enabled": True}})

    assert result["available"] is False


def test_collect_solar_forecast_disabled():
    result = status_collector._collect_solar_forecast({"solar_forecast": {"enabled": False}})

    assert result["available"] is False


def test_collect_solar_forecast_reads_cached_file(tmp_path):
    forecast_path = tmp_path / "solar_forecast.json"
    forecast_path.write_text(json.dumps({"today_kwh": 12.3, "tomorrow_kwh": 8.1}), encoding="utf-8")

    with mock.patch.object(status_collector, "get_solar_forecast_path", lambda: str(forecast_path)):
        result = status_collector._collect_solar_forecast({"solar_forecast": {"enabled": True}})

    assert result == {
        "available": True,
        "today_kwh": 12.3,
        "tomorrow_kwh": 8.1,
        "current_weather": None,
        "generated_at": None,
        "model_trained_at": None,
    }


def test_collect_battery_forecast_reads_checkpoints(tmp_path):
    prediction_path = tmp_path / "battery_evening_prediction.json"
    checkpoints = [{"time": "23:30", "label": "11:30 PM", "priority": True, "predicted_soc_percent": 45.0}]
    prediction_path.write_text(
        json.dumps({"computed_at": "2026-08-31T18:00:00+00:00", "dashboard_checkpoints": checkpoints}),
        encoding="utf-8",
    )

    with mock.patch.object(status_collector, "get_battery_evening_prediction_path", lambda: str(prediction_path)):
        result = status_collector._collect_battery_forecast({"battery_evening_prediction": {"enabled": True}})

    assert result["available"] is True
    assert result["checkpoints"] == checkpoints


def test_collect_service_health_unavailable_when_systemctl_missing():
    with mock.patch.object(status_collector.shutil, "which", return_value=None):
        result = status_collector._collect_service_health()

    assert result["available"] is False
    assert "systemctl" in result["error"]


def test_collect_service_health_distinguishes_running_stopped_and_not_installed(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "battery_mode_daemon.log").write_text("x", encoding="utf-8")

    def fake_show(unit):
        if unit == "home_automation.service":
            return "loaded", "active"
        if unit == "home_automation_dashboard.service":
            return "loaded", "failed"
        return "not-found", "inactive"

    with (
        mock.patch.object(status_collector.shutil, "which", return_value="/usr/bin/systemctl"),
        mock.patch.object(status_collector, "_systemctl_show", side_effect=fake_show),
        mock.patch.object(status_collector, "get_project_root", return_value=str(tmp_path)),
    ):
        result = status_collector._collect_service_health()

    assert result["available"] is True
    by_key = {s["key"]: s for s in result["services"]}

    assert by_key["battery_daemon"]["installed"] is True
    assert by_key["battery_daemon"]["active"] is True
    assert by_key["battery_daemon"]["log_age_seconds"] < 5

    assert by_key["dashboard"]["installed"] is True
    assert by_key["dashboard"]["active"] is False
    assert by_key["dashboard"]["active_state"] == "failed"

    # Not yet deployed (see docs/PI4_DEPLOYMENT.md) - must read as "not
    # installed", not a false "stopped".
    assert by_key["hot_water_daemon"]["installed"] is False
    assert by_key["hot_water_daemon"]["active"] is None
    assert by_key["hot_water_daemon"]["log_age_seconds"] is None


def test_systemctl_show_returns_none_pair_on_subprocess_failure():
    with mock.patch.object(status_collector.subprocess, "run", side_effect=OSError("systemctl not found")):
        load_state, active_state = status_collector._systemctl_show("home_automation.service")

    assert load_state is None
    assert active_state is None


def test_collect_service_health_survives_an_unexpected_error():
    """Circuit Breaker: an unexpected exception here must not blank the whole snapshot."""
    with (
        mock.patch.object(status_collector.shutil, "which", return_value="/usr/bin/systemctl"),
        mock.patch.object(status_collector, "_check_one_service", side_effect=RuntimeError("boom")),
    ):
        result = status_collector._collect_service_health()

    assert result["available"] is False
