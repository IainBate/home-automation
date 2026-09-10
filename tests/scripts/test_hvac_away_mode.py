"""Tests for scripts/hvac_away_mode.py."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import hvac_away_mode
import hvac_automation_core as core
import pytest


def _write_state(tmp_path: Path, state: dict) -> Path:
    state_path = tmp_path / "hvac_automation_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return state_path


def test_start_away_mode_writes_active_flag(tmp_path):
    state_path = _write_state(tmp_path, {})

    with mock.patch.object(core, "get_hvac_automation_state_path", lambda: str(state_path)):
        before = datetime.now(tz=UTC)
        started_at = hvac_away_mode.start_away_mode()
        after = datetime.now(tz=UTC)

    assert before <= started_at <= after
    final_state = json.loads(state_path.read_text())
    assert final_state["away_mode"]["active"] is True
    assert final_state["away_mode"]["started_at"] == started_at.isoformat()


def test_start_away_mode_preserves_other_state_keys(tmp_path):
    state_path = _write_state(tmp_path, {"hvac": {"hvac_target_c": 19.0}})

    with mock.patch.object(core, "get_hvac_automation_state_path", lambda: str(state_path)):
        hvac_away_mode.start_away_mode()

    final_state = json.loads(state_path.read_text())
    assert final_state["hvac"] == {"hvac_target_c": 19.0}
    assert "away_mode" in final_state


def test_cancel_away_mode_clears_active_flag_and_reports_it_was_active(tmp_path):
    state_path = _write_state(tmp_path, {"away_mode": {"active": True, "started_at": "x"}})

    with mock.patch.object(core, "get_hvac_automation_state_path", lambda: str(state_path)):
        was_active = hvac_away_mode.cancel_away_mode()

    assert was_active is True
    final_state = json.loads(state_path.read_text())
    assert "away_mode" not in final_state


def test_cancel_away_mode_when_nothing_active_reports_false(tmp_path):
    state_path = _write_state(tmp_path, {})

    with mock.patch.object(core, "get_hvac_automation_state_path", lambda: str(state_path)):
        was_active = hvac_away_mode.cancel_away_mode()

    assert was_active is False


def test_print_status_not_active(tmp_path, capsys):
    state_path = _write_state(tmp_path, {})

    with mock.patch.object(core, "get_hvac_automation_state_path", lambda: str(state_path)):
        hvac_away_mode.print_status("Europe/London")

    assert "not active" in capsys.readouterr().out


def test_print_status_active_shows_start_time(tmp_path, capsys):
    started_at = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    state_path = _write_state(tmp_path, {"away_mode": {"active": True, "started_at": started_at}})

    with mock.patch.object(core, "get_hvac_automation_state_path", lambda: str(state_path)):
        hvac_away_mode.print_status("Europe/London")

    assert "ACTIVE" in capsys.readouterr().out


def test_main_start_reports_friendly_error_on_lock_timeout(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["hvac_away_mode.py", "--start"])
    fake_config = {"location": {"default_timezone_str": "Europe/London"}}

    with mock.patch.object(
        hvac_away_mode, "load_static_config", return_value=fake_config
    ), mock.patch.object(
        hvac_away_mode, "start_away_mode", side_effect=TimeoutError("lock busy")
    ), pytest.raises(SystemExit) as exc_info:
        hvac_away_mode.main()

    assert exc_info.value.code == 1
    assert "timed out" in capsys.readouterr().out


def test_main_cancel_reports_friendly_error_on_lock_timeout(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["hvac_away_mode.py", "--cancel"])
    fake_config = {"location": {"default_timezone_str": "Europe/London"}}

    with mock.patch.object(
        hvac_away_mode, "load_static_config", return_value=fake_config
    ), mock.patch.object(
        hvac_away_mode, "cancel_away_mode", side_effect=TimeoutError("lock busy")
    ), pytest.raises(SystemExit) as exc_info:
        hvac_away_mode.main()

    assert exc_info.value.code == 1
    assert "timed out" in capsys.readouterr().out


def test_main_start_warns_when_automation_disabled(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["hvac_away_mode.py", "--start"])
    fake_config = {
        "location": {"default_timezone_str": "Europe/London"},
        "hvac_automation": {"enabled": False},
    }

    with mock.patch.object(
        hvac_away_mode, "load_static_config", return_value=fake_config
    ), mock.patch.object(
        hvac_away_mode, "start_away_mode", return_value=datetime.now(tz=UTC)
    ):
        hvac_away_mode.main()

    assert "hvac_automation.enabled is currently false" in capsys.readouterr().out
