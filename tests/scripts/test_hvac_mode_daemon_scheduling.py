"""Tests for HvacModeDaemon's scheduling wiring on top of
src/daemon_support/base_daemon.py's TwoTierPollingDaemon.

Mirrors tests/scripts/test_hotwater_mode_daemon_scheduling.py's shape:
drives _run_one_tick() directly (never run(), which loops with real
time.sleep() and installs signal handlers) with the daemon's three real
cycle methods replaced by recorders.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import hvac_mode_daemon
import yaml
from hvac_mode_daemon import HvacModeDaemon

PROJECT_ROOT_CONFIG = Path(__file__).resolve().parent.parent.parent / "config.yaml"


def _base_config() -> dict:
    with PROJECT_ROOT_CONFIG.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _write_config(config_dir, overrides: dict) -> str:
    config_dir.mkdir(parents=True, exist_ok=True)
    config = _base_config()
    for section, values in overrides.items():
        config.setdefault(section, {})
        config[section] = {**config.get(section, {}), **values}
    path = config_dir / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return str(path)


def _make_daemon(config_path: str, monkeypatch, tmp_path) -> HvacModeDaemon:
    # setup_rotating_logger() creates ./logs relative to cwd - chdir into a
    # scratch dir so tests never touch the real repo's logs/.
    monkeypatch.chdir(tmp_path)
    daemon = HvacModeDaemon(config_path=config_path)
    daemon.load_config()
    daemon._register_checks()
    daemon.thermostat_poll_calls = []
    daemon.hvac_target_update_calls = []
    daemon.time_sync_calls = []
    daemon._run_thermostat_poll_cycle = lambda: daemon.thermostat_poll_calls.append(True)
    daemon._run_hvac_target_update_cycle = lambda hvac_config: daemon.hvac_target_update_calls.append(
        hvac_config
    )
    daemon._run_hvac_time_sync_cycle = lambda: daemon.time_sync_calls.append(True)
    return daemon


def test_disabled_automation_skips_all_checks_but_still_reloads_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_path = _write_config(config_dir, {"hvac_automation": {"enabled": False}})
    daemon = _make_daemon(config_path, monkeypatch, tmp_path)

    daemon._run_one_tick()
    daemon._run_one_tick()

    assert daemon.thermostat_poll_calls == []
    assert daemon.hvac_target_update_calls == []
    assert daemon.time_sync_calls == []


def test_misconfigured_automation_skips_all_checks(tmp_path, monkeypatch):
    """enabled: true but airstage not configured - a real 'won't start until fixed' case."""
    config_dir = tmp_path / "config"
    config_path = _write_config(
        config_dir,
        {
            "hvac_automation": {"enabled": True},
            "airstage": {"enabled": False},
        },
    )
    daemon = _make_daemon(config_path, monkeypatch, tmp_path)

    daemon._run_one_tick()

    assert daemon.thermostat_poll_calls == []
    assert daemon._config_error_logged is not None


def test_enabled_valid_config_runs_all_checks_on_first_tick(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_path = _write_config(
        config_dir,
        {
            "hvac_automation": {
                "enabled": True,
                "poll_intervals": {
                    "thermostat_seconds": 600,
                    "hvac_target_seconds": 1800,
                    "hvac_time_sync_seconds": 3600,
                },
            },
            "airstage": {"enabled": True},
            "resideo": {"enabled": True},
        },
    )
    daemon = _make_daemon(config_path, monkeypatch, tmp_path)

    daemon._run_one_tick()

    assert len(daemon.thermostat_poll_calls) == 1
    assert len(daemon.hvac_target_update_calls) == 1
    assert len(daemon.time_sync_calls) == 1


def test_checks_do_not_rerun_before_their_interval_elapses(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_path = _write_config(
        config_dir,
        {
            "hvac_automation": {
                "enabled": True,
                "poll_intervals": {
                    "thermostat_seconds": 600,
                    "hvac_target_seconds": 1800,
                    "hvac_time_sync_seconds": 3600,
                },
            },
            "airstage": {"enabled": True},
            "resideo": {"enabled": True},
        },
    )
    daemon = _make_daemon(config_path, monkeypatch, tmp_path)

    daemon._run_one_tick()
    daemon._run_one_tick()
    daemon._run_one_tick()

    assert len(daemon.thermostat_poll_calls) == 1
    assert len(daemon.hvac_target_update_calls) == 1
    assert len(daemon.time_sync_calls) == 1


def test_reenabling_after_disabled_does_not_cause_a_catch_up_burst(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    disabled_path = _write_config(config_dir, {"hvac_automation": {"enabled": False}})
    daemon = _make_daemon(disabled_path, monkeypatch, tmp_path)

    daemon._run_one_tick()
    daemon._run_one_tick()
    assert daemon.thermostat_poll_calls == []

    _write_config(
        config_dir,
        {
            "hvac_automation": {"enabled": True},
            "airstage": {"enabled": True},
            "resideo": {"enabled": True},
        },
    )

    daemon._run_one_tick()
    assert len(daemon.thermostat_poll_calls) == 1


def test_reload_config_skips_the_full_reparse_when_config_file_is_unchanged(
    tmp_path, monkeypatch
):
    config_dir = tmp_path / "config"
    config_path = _write_config(config_dir, {"hvac_automation": {"enabled": False}})
    daemon = _make_daemon(config_path, monkeypatch, tmp_path)

    call_count = {"n": 0}
    real_load_static_config = hvac_mode_daemon.load_static_config

    def counting_load_static_config(path):
        call_count["n"] += 1
        return real_load_static_config(path)

    with mock.patch.object(hvac_mode_daemon, "load_static_config", counting_load_static_config):
        daemon.reload_config()
        daemon.reload_config()
        daemon.reload_config()
        assert call_count["n"] == 0  # mtime unchanged since load_config() - never re-parsed

        new_mtime = Path(config_path).stat().st_mtime + 1
        os.utime(config_path, (new_mtime, new_mtime))

        daemon.reload_config()
        assert call_count["n"] == 1  # mtime changed - reparsed once


def test_current_room_temperature_c_stale_reading_treated_as_unavailable(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_path = _write_config(config_dir, {"hvac_automation": {"enabled": True}})
    daemon = _make_daemon(config_path, monkeypatch, tmp_path)

    daemon._room_temperature_c = 19.0
    daemon._room_temperature_read_at = 0.0  # far in the past

    assert daemon._current_room_temperature_c(thermostat_poll_seconds=600) is None


def test_current_room_temperature_c_fresh_reading_is_used(tmp_path, monkeypatch):
    import time as time_module

    config_dir = tmp_path / "config"
    config_path = _write_config(config_dir, {"hvac_automation": {"enabled": True}})
    daemon = _make_daemon(config_path, monkeypatch, tmp_path)

    daemon._room_temperature_c = 19.0
    daemon._room_temperature_read_at = time_module.time()

    assert daemon._current_room_temperature_c(thermostat_poll_seconds=600) == 19.0


def test_current_room_temperature_c_never_read_returns_none(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_path = _write_config(config_dir, {"hvac_automation": {"enabled": True}})
    daemon = _make_daemon(config_path, monkeypatch, tmp_path)

    assert daemon._current_room_temperature_c(thermostat_poll_seconds=600) is None
