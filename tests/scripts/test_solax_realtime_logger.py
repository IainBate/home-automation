"""Tests for solax_realtime_logger.py's snapshot-fetch-and-append behavior."""

from __future__ import annotations

import json
from unittest import mock

import solax_realtime_logger as logger_script


def test_run_returns_0_when_not_configured(capsys):
    config = {"solaX_cloud_api": {"token_id": "NOT_USED_FOR_MODBUS", "master_wifisn": "NOT_USED_FOR_MODBUS"}}

    exit_code = logger_script.run(config, quiet=False)

    assert exit_code == 0
    assert "skipping" in capsys.readouterr().out


def test_run_returns_1_when_fetch_fails():
    config = {"solaX_cloud_api": {"token_id": "real-token", "master_wifisn": "SR2NZD2S3B"}}

    with mock.patch.object(logger_script, "solax_cloud_get_realtime_snapshot", return_value=None):
        exit_code = logger_script.run(config, quiet=True)

    assert exit_code == 1


def test_run_appends_snapshot_on_success(tmp_path):
    data_path = tmp_path / "solax_historical_data.json"
    data_path.write_text(
        json.dumps({"meta": {"data_points": 1}, "data": [{"timestamp": "2026-09-01 12:00:00", "pv_power_kw": 2.0}]}),
        encoding="utf-8",
    )
    config = {"solaX_cloud_api": {"token_id": "real-token", "master_wifisn": "SR2NZD2S3B"}}
    snapshot = {"timestamp": "2026-09-02 08:00:00", "pv_power_kw": 1.5, "battery_power_kw": 0.0, "grid_power_kw": 0.0, "soc_percent": 90}

    with (
        mock.patch.object(logger_script, "get_solax_historical_data_path", lambda: str(data_path)),
        mock.patch.object(logger_script, "solax_cloud_get_realtime_snapshot", return_value=snapshot),
    ):
        exit_code = logger_script.run(config, quiet=True)

    assert exit_code == 0
    saved = json.loads(data_path.read_text(encoding="utf-8"))
    assert saved["data"][-1] == snapshot
    assert saved["meta"]["data_points"] == 2


def test_run_falls_back_to_modbus_when_cloud_fetch_fails(tmp_path):
    data_path = tmp_path / "solax_historical_data.json"
    data_path.write_text(json.dumps({"meta": {"data_points": 0}, "data": []}), encoding="utf-8")
    config = {
        "solaX_cloud_api": {"token_id": "real-token", "master_wifisn": "SR2NZD2S3B", "modbus_enabled": True}
    }
    bulk = {
        "soc": {"master": 42},
        "pv_power": {"master": {"pv1": 100, "pv2": 200}},
        "battery_power": {"master": {"power": -500}},
        "grid_power": {"master": 0},
        "daily_yield": {"master": 5.0},
    }

    with (
        mock.patch.object(logger_script, "get_solax_historical_data_path", lambda: str(data_path)),
        mock.patch.object(logger_script, "solax_cloud_get_realtime_snapshot", return_value=None),
        mock.patch.object(logger_script, "solax_modbus_bulk_data", return_value=bulk),
    ):
        exit_code = logger_script.run(config, quiet=True)

    assert exit_code == 0
    saved = json.loads(data_path.read_text(encoding="utf-8"))
    assert saved["data"][-1]["soc_percent"] == 42
    assert saved["data"][-1]["pv_power_kw"] == 0.3
    assert saved["data"][-1]["battery_power_kw"] == -0.5
    assert saved["meta"]["data_points"] == 1


def test_run_falls_back_to_modbus_when_cloud_snapshot_is_duplicate(tmp_path):
    data_path = tmp_path / "solax_historical_data.json"
    duplicate_snapshot = {
        "timestamp": "2026-09-02 08:00:00",
        "timestamp_utc": "2026-09-02T00:00:00Z",
        "pv_power_kw": 1.0,
        "battery_power_kw": 0.0,
        "grid_power_kw": 0.0,
        "soc_percent": 90,
    }
    data_path.write_text(
        json.dumps({"meta": {"data_points": 1}, "data": [duplicate_snapshot]}), encoding="utf-8"
    )
    config = {
        "solaX_cloud_api": {"token_id": "real-token", "master_wifisn": "SR2NZD2S3B", "modbus_enabled": True}
    }
    bulk = {
        "soc": {"master": 91},
        "pv_power": {"master": {"pv1": 0, "pv2": 0}},
        "battery_power": {"master": {"power": 0}},
        "grid_power": {"master": 0},
        "daily_yield": {"master": 1.0},
    }

    with (
        mock.patch.object(logger_script, "get_solax_historical_data_path", lambda: str(data_path)),
        mock.patch.object(logger_script, "solax_cloud_get_realtime_snapshot", return_value=duplicate_snapshot),
        mock.patch.object(logger_script, "solax_modbus_bulk_data", return_value=bulk),
    ):
        exit_code = logger_script.run(config, quiet=True)

    assert exit_code == 0
    saved = json.loads(data_path.read_text(encoding="utf-8"))
    assert saved["data"][-1]["soc_percent"] == 91
    assert saved["data"][-1]["source"] == "modbus_fallback"
    assert saved["meta"]["data_points"] == 2


def test_run_returns_1_when_cloud_and_modbus_both_fail():
    config = {
        "solaX_cloud_api": {"token_id": "real-token", "master_wifisn": "SR2NZD2S3B", "modbus_enabled": True}
    }

    with (
        mock.patch.object(logger_script, "solax_cloud_get_realtime_snapshot", return_value=None),
        mock.patch.object(logger_script, "solax_modbus_bulk_data", return_value=None),
    ):
        exit_code = logger_script.run(config, quiet=True)

    assert exit_code == 1
