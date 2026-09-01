"""Tests for BatteryModeDaemon's mode-change log, now backed by the shared
locked-JSON-state primitive (src/utils/state_store.py) instead of its own
unlocked load-then-write.
"""

from __future__ import annotations

from battery_mode_daemon import BatteryModeDaemon
from src.core_logic.battery_simulation import BatteryMode


def _make_daemon(tmp_path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    daemon = BatteryModeDaemon(config_path=str(tmp_path / "daemon_config.json"))
    return daemon


def test_load_mode_change_log_missing_file_returns_defaults(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    log_data = daemon._load_mode_change_log()
    assert log_data["last_change_timestamp"] is None
    assert log_data["change_history"] == []


def test_save_then_load_round_trips(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    daemon._save_mode_change_log(BatteryMode.FORCE_CHARGE, "test reason")

    reloaded = _make_daemon(tmp_path, monkeypatch)
    log_data = reloaded._load_mode_change_log()
    assert log_data["last_change_mode"] == "FORCE_CHARGE"
    assert log_data["last_change_reason"] == "test reason"
    assert len(log_data["change_history"]) == 1
    assert reloaded.last_mode_change_time == log_data["last_change_timestamp"]


def test_change_history_capped_at_100_entries(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    for _ in range(105):
        daemon._save_mode_change_log(BatteryMode.SELF_USE, "cycling")
    log_data = daemon._load_mode_change_log()
    assert len(log_data["change_history"]) == 100


def test_default_log_structure_is_not_shared_mutable_state(tmp_path, monkeypatch):
    """A shared class-level default dict/list would leak mutations across instances/calls."""
    daemon_a = _make_daemon(tmp_path / "a", monkeypatch)
    daemon_b = _make_daemon(tmp_path / "b", monkeypatch)

    log_a = daemon_a._default_mode_change_log()
    log_a["change_history"].append({"fake": "entry"})

    log_b = daemon_b._default_mode_change_log()
    assert log_b["change_history"] == []
