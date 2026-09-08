"""Tests for solax_realtime_logger.py's snapshot-fetch-and-append behavior."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
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


def test_run_appends_to_wal_without_touching_main_file_when_compaction_not_due(tmp_path):
    """A fresh main file (just written, so not yet a compaction interval old) - the new
    snapshot should land only in the write-ahead log, leaving the main file untouched.
    This is the whole point of the WAL design: most ticks must not pay to rewrite it.
    """
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
    # Main file unchanged - the new reading has not been compacted in yet.
    saved = json.loads(data_path.read_text(encoding="utf-8"))
    assert saved["meta"]["data_points"] == 1
    assert saved["data"][-1]["timestamp"] == "2026-09-01 12:00:00"
    # ...but it is durably recorded in the write-ahead log.
    wal_path = logger_script._wal_path(str(data_path))
    wal_lines = wal_path.read_text(encoding="utf-8").splitlines()
    assert len(wal_lines) == 1
    assert json.loads(wal_lines[0]) == snapshot


def test_run_compacts_wal_into_main_file_once_compaction_is_due(tmp_path):
    """An old (stale-mtime) main file plus a pending WAL entry triggers a compaction -
    the new reading should be folded into the main file and the WAL cleared."""
    data_path = tmp_path / "solax_historical_data.json"
    data_path.write_text(
        json.dumps({"meta": {"data_points": 1}, "data": [{"timestamp": "2026-09-01 12:00:00", "pv_power_kw": 2.0}]}),
        encoding="utf-8",
    )
    old_time = time.time() - logger_script.COMPACTION_INTERVAL_SECONDS - 60
    os.utime(data_path, (old_time, old_time))

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
    wal_path = logger_script._wal_path(str(data_path))
    assert wal_path.read_text(encoding="utf-8") == ""


def test_compact_now_folds_wal_in_regardless_of_age(tmp_path, capsys):
    """--compact-now (via compact_now()) must fold in pending entries even when the
    main file is fresh (i.e. an ordinary tick would have deferred compaction)."""
    data_path = tmp_path / "solax_historical_data.json"
    data_path.write_text(
        json.dumps({"meta": {"data_points": 1}, "data": [{"timestamp": "2026-09-01 12:00:00"}]}), encoding="utf-8"
    )
    wal_path = logger_script._wal_path(str(data_path))
    wal_path.write_text(json.dumps({"timestamp": "2026-09-02 08:00:00", "soc_percent": 90}) + "\n", encoding="utf-8")

    with mock.patch.object(logger_script, "get_solax_historical_data_path", lambda: str(data_path)):
        exit_code = logger_script.compact_now(quiet=False)

    assert exit_code == 0
    saved = json.loads(data_path.read_text(encoding="utf-8"))
    assert saved["meta"]["data_points"] == 2
    assert wal_path.read_text(encoding="utf-8") == ""
    assert "Compacted 1 pending reading" in capsys.readouterr().out


def test_compact_is_idempotent_if_wal_clear_did_not_complete(tmp_path):
    """Simulates a crash between the main-file write succeeding and the WAL being
    cleared: the WAL still holds the already-applied entry. Compacting again must not
    duplicate it - only finish clearing the WAL."""
    data_path = tmp_path / "solax_historical_data.json"
    already_applied = {"timestamp": "2026-09-02 08:00:00", "pv_power_kw": 1.5, "soc_percent": 90}
    data_path.write_text(
        json.dumps({"meta": {"data_points": 2}, "data": [{"timestamp": "2026-09-01 12:00:00"}, already_applied]}),
        encoding="utf-8",
    )
    wal_path = logger_script._wal_path(str(data_path))
    wal_path.write_text(json.dumps(already_applied) + "\n", encoding="utf-8")

    merged = logger_script._compact(Path(data_path), wal_path)

    assert len(merged["data"]) == 2  # not duplicated
    assert wal_path.read_text(encoding="utf-8") == ""


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
    wal_path = logger_script._wal_path(str(data_path))
    wal_lines = [json.loads(line) for line in wal_path.read_text(encoding="utf-8").splitlines()]
    assert wal_lines[-1]["soc_percent"] == 42
    assert wal_lines[-1]["pv_power_kw"] == 0.3
    assert wal_lines[-1]["battery_power_kw"] == -0.5


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
    wal_path = logger_script._wal_path(str(data_path))
    wal_lines = [json.loads(line) for line in wal_path.read_text(encoding="utf-8").splitlines()]
    assert wal_lines[-1]["soc_percent"] == 91
    assert wal_lines[-1]["source"] == "modbus_fallback"


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
