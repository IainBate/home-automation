"""Tests for HotWaterModeDaemon's scheduling wiring after its migration onto
src/daemon_support/base_daemon.py's TwoTierPollingDaemon.

Drives _run_one_tick() directly (never run(), which loops with real
time.sleep() and installs signal handlers) with the daemon's three real
force_heat/revert/legionella_progress cycle methods replaced by recorders,
to confirm the migration preserved the original daemon's behaviour:
- disabled/misconfigured hotwater_automation skips all checks but still
  reloads config
- enabled + valid config runs all three checks on the first tick
- each check then waits for its own configured interval before rerunning
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import yaml
import hotwater_mode_daemon
from hotwater_mode_daemon import HotWaterModeDaemon

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


def _make_daemon(config_path: str, monkeypatch, tmp_path) -> HotWaterModeDaemon:
    # setup_rotating_logger() creates ./logs relative to cwd - chdir into a
    # scratch dir so tests never touch the real repo's logs/.
    monkeypatch.chdir(tmp_path)
    daemon = HotWaterModeDaemon(config_path=config_path)
    daemon.load_config()
    daemon._register_checks()
    daemon.force_heat_calls = []
    daemon.revert_calls = []
    daemon.legionella_progress_calls = []
    daemon.legionella_natural_completion_calls = []
    daemon._run_force_heat_cycle = lambda hw_config: daemon.force_heat_calls.append(hw_config)
    daemon._run_revert_cycle = lambda hw_config: daemon.revert_calls.append(hw_config)
    daemon._run_legionella_progress_cycle = lambda hw_config: daemon.legionella_progress_calls.append(
        hw_config
    )
    daemon._run_legionella_natural_completion_cycle = (
        lambda hw_config: daemon.legionella_natural_completion_calls.append(hw_config)
    )
    return daemon


def test_disabled_automation_skips_all_checks_but_still_reloads_config(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_path = _write_config(config_dir, {"hotwater_automation": {"enabled": False}})
    daemon = _make_daemon(config_path, monkeypatch, tmp_path)

    daemon._run_one_tick()
    daemon._run_one_tick()

    assert daemon.force_heat_calls == []
    assert daemon.revert_calls == []
    assert daemon.legionella_progress_calls == []


def test_misconfigured_automation_skips_all_checks(tmp_path, monkeypatch):
    """enabled: true but melcloud not configured - a real 'won't start until fixed' case."""
    config_dir = tmp_path / "config"
    config_path = _write_config(
        config_dir,
        {
            "hotwater_automation": {"enabled": True},
            "melcloud": {"enabled": False},
        },
    )
    daemon = _make_daemon(config_path, monkeypatch, tmp_path)

    daemon._run_one_tick()

    assert daemon.force_heat_calls == []
    assert daemon._config_error_logged is not None


def test_enabled_valid_config_runs_all_checks_on_first_tick(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_path = _write_config(
        config_dir,
        {
            "hotwater_automation": {
                "enabled": True,
                "poll_interval_seconds": 600,
                "revert_check_interval_seconds": 3600,
            },
            "melcloud": {"enabled": True, "email": "test@example.com", "password": "dummy"},
        },
    )
    daemon = _make_daemon(config_path, monkeypatch, tmp_path)

    daemon._run_one_tick()

    assert len(daemon.force_heat_calls) == 1
    assert len(daemon.revert_calls) == 1
    assert len(daemon.legionella_progress_calls) == 1
    assert len(daemon.legionella_natural_completion_calls) == 1


def test_checks_do_not_rerun_before_their_interval_elapses(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_path = _write_config(
        config_dir,
        {
            "hotwater_automation": {
                "enabled": True,
                "poll_interval_seconds": 600,
                "revert_check_interval_seconds": 3600,
            },
            "melcloud": {"enabled": True, "email": "test@example.com", "password": "dummy"},
        },
    )
    daemon = _make_daemon(config_path, monkeypatch, tmp_path)

    daemon._run_one_tick()
    daemon._run_one_tick()
    daemon._run_one_tick()

    assert len(daemon.force_heat_calls) == 1
    assert len(daemon.revert_calls) == 1
    assert len(daemon.legionella_progress_calls) == 1
    assert len(daemon.legionella_natural_completion_calls) == 1


def test_reenabling_after_disabled_does_not_cause_a_catch_up_burst(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    disabled_path = _write_config(config_dir, {"hotwater_automation": {"enabled": False}})
    daemon = _make_daemon(disabled_path, monkeypatch, tmp_path)

    daemon._run_one_tick()
    daemon._run_one_tick()
    assert daemon.force_heat_calls == []

    # Rewrite the same config file on disk (as a real hot-reload would see)
    # rather than poking daemon.config directly, since the next tick's
    # reload_config() would otherwise just read the still-disabled file
    # back in and clobber a directly-injected value.
    _write_config(
        config_dir,
        {
            "hotwater_automation": {"enabled": True},
            "melcloud": {"enabled": True, "email": "test@example.com", "password": "dummy"},
        },
    )

    daemon._run_one_tick()
    assert len(daemon.force_heat_calls) == 1


def test_reload_config_skips_the_full_reparse_when_config_file_is_unchanged(
    tmp_path, monkeypatch
):
    """Regression test: reload_config() used to re-parse + re-validate the
    whole config.yaml unconditionally on every 30s tick forever, even though
    the file is rarely touched - now it should skip that work when the
    file's mtime hasn't changed since the last successful load.
    """
    config_dir = tmp_path / "config"
    config_path = _write_config(config_dir, {"hotwater_automation": {"enabled": False}})
    daemon = _make_daemon(config_path, monkeypatch, tmp_path)

    call_count = {"n": 0}
    real_load_static_config = hotwater_mode_daemon.load_static_config

    def counting_load_static_config(path):
        call_count["n"] += 1
        return real_load_static_config(path)

    with mock.patch.object(
        hotwater_mode_daemon, "load_static_config", counting_load_static_config
    ):
        daemon.reload_config()
        daemon.reload_config()
        daemon.reload_config()
        assert call_count["n"] == 0  # mtime unchanged since load_config() - never re-parsed

        # Touch the file (content-identical) so its mtime actually changes -
        # some filesystems have 1s mtime resolution, so nudge it forward.
        new_mtime = Path(config_path).stat().st_mtime + 1
        os.utime(config_path, (new_mtime, new_mtime))

        daemon.reload_config()
        assert call_count["n"] == 1  # mtime changed - reparsed once
