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
