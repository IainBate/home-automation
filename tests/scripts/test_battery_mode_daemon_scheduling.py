"""Tests for BatteryModeDaemon after its migration onto
src/daemon_support/base_daemon.py's TwoTierPollingDaemon, plus the new
--dry-run flag.

Drives _run_one_tick() directly (never run(), which loops with real
time.sleep() and installs signal handlers) with _perform_hardware_cycle
replaced by a recorder, so no real Modbus/Ohme/hardware calls happen. Pins
down the one behaviour the migration had to preserve exactly: the daemon
skips its very first hardware check after startup (a deliberate "wait one
cycle" grace period) and only performs cycles from the second due tick on.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest
from battery_mode_daemon import BatteryModeDaemon, main

SYSTEM_CONFIG_PATH = str(Path(__file__).resolve().parent.parent.parent / "config.yaml")


def _daemon_config() -> dict:
    return {
        "daemon_settings": {
            "hardware_poll_interval_seconds": 60,
            "min_mode_change_interval_seconds": 600,
            "ohme_charging_threshold_watts": 500,
        },
        "ohme_charging": {"enabled": False, "force_charge_mode": "FORCE_CHARGE"},
        "schedule": {"enabled": False, "default_mode": "SELF_USE", "time_ranges": []},
        "logging": {"level": "INFO", "file_path": "logs/battery_mode_daemon.log"},
    }


def _write_daemon_config(tmp_path, overrides: dict | None = None) -> str:
    config = _daemon_config()
    if overrides:
        config.update(overrides)
    path = tmp_path / "daemon_config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return str(path)


def _make_daemon(tmp_path, monkeypatch, config_overrides: dict | None = None) -> BatteryModeDaemon:
    monkeypatch.chdir(tmp_path)
    config_path = _write_daemon_config(tmp_path, config_overrides)
    daemon = BatteryModeDaemon(config_path, SYSTEM_CONFIG_PATH)
    daemon.load_config()
    daemon.register_check(
        "hardware",
        daemon._hardware_check,
        lambda: daemon.daemon_config["daemon_settings"]["hardware_poll_interval_seconds"],
    )
    daemon.hardware_cycle_calls = []
    daemon._perform_hardware_cycle = lambda: daemon.hardware_cycle_calls.append(1)
    return daemon


def test_first_tick_is_a_startup_grace_period_not_a_real_cycle(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)

    daemon._run_one_tick()

    assert daemon.hardware_cycle_calls == []
    assert daemon.startup_complete is True


def test_second_due_tick_performs_a_real_hardware_cycle(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)

    daemon._run_one_tick()  # startup grace - no cycle
    # hardware_poll_interval_seconds' schema minimum is 60s, too long to
    # actually wait out in a unit test - simulate the interval having
    # elapsed by resetting the check's own due-time bookkeeping directly.
    daemon._checks[0]._last_run = 0.0
    daemon._run_one_tick()  # due again - real cycle

    assert daemon.hardware_cycle_calls == [1]


def test_load_config_applies_configured_logging_level(tmp_path, monkeypatch):
    """Regression test: the logger used to be hardcoded to DEBUG regardless of
    this setting, producing a debug line on every fast-poll tick (~500KB/day
    of SD card writes on the Pi for no operational benefit - see
    _apply_logging_level()'s docstring).
    """
    daemon = _make_daemon(tmp_path, monkeypatch, {"logging": {"level": "WARNING", "file_path": "x"}})

    assert daemon.logger.level == logging.WARNING


def test_load_config_defaults_logging_level_to_info_when_unset(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _daemon_config()
    del config["logging"]
    path = tmp_path / "daemon_config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    daemon = BatteryModeDaemon(str(path), SYSTEM_CONFIG_PATH)

    daemon.load_config()

    assert daemon.logger.level == logging.INFO


def test_reload_config_reapplies_logging_level_when_config_changes(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    assert daemon.logger.level == logging.INFO

    daemon.config_path.write_text(
        json.dumps({**_daemon_config(), "logging": {"level": "WARNING", "file_path": "x"}}),
        encoding="utf-8",
    )
    daemon.reload_config()

    assert daemon.logger.level == logging.WARNING


def test_reload_config_runs_every_tick(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    reload_calls = []
    original_reload = daemon.reload_config
    daemon.reload_config = lambda: (reload_calls.append(1), original_reload())[-1]

    daemon._run_one_tick()
    daemon._run_one_tick()

    assert len(reload_calls) == 2


def test_dry_run_with_valid_config_prints_ok_and_never_calls_run(tmp_path, monkeypatch, capsys):
    config_path = _write_daemon_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["battery_mode_daemon.py", config_path, SYSTEM_CONFIG_PATH, "--dry-run"]
    )
    monkeypatch.setattr(
        BatteryModeDaemon, "run", lambda self: pytest.fail("run() must not be called under --dry-run")
    )

    main()

    assert "Configuration OK" in capsys.readouterr().out


def test_dry_run_with_invalid_config_exits_nonzero(tmp_path, monkeypatch, capsys):
    bad_config_path = tmp_path / "bad_daemon_config.json"
    bad_config_path.write_text(json.dumps({"not": "valid"}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["battery_mode_daemon.py", str(bad_config_path), SYSTEM_CONFIG_PATH, "--dry-run"],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    assert "Configuration invalid" in capsys.readouterr().out
