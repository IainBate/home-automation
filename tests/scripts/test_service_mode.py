"""Tests for scripts/service_mode.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import hotwater_automation_core as core
import service_mode


def _write_state(tmp_path: Path, state: dict) -> Path:
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return state_path


def test_start_service_mode_writes_active_flag(tmp_path):
    state_path = _write_state(tmp_path, {})

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)):
        before = datetime.now(tz=UTC)
        started_at = service_mode.start_service_mode()
        after = datetime.now(tz=UTC)

    assert before <= started_at <= after
    final_state = json.loads(state_path.read_text())
    assert final_state["service_mode"]["active"] is True
    assert final_state["service_mode"]["started_at"] == started_at.isoformat()


def test_start_service_mode_preserves_other_state_keys(tmp_path):
    state_path = _write_state(tmp_path, {"force_heat_activated_at": "2026-01-01T00:00:00+00:00"})

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)):
        service_mode.start_service_mode()

    final_state = json.loads(state_path.read_text())
    assert final_state["force_heat_activated_at"] == "2026-01-01T00:00:00+00:00"
    assert "service_mode" in final_state


def test_cancel_service_mode_clears_active_and_reports_it_was_active(tmp_path):
    state_path = _write_state(tmp_path, {"service_mode": {"active": True}})

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)):
        was_active = service_mode.cancel_service_mode()

    assert was_active is True
    final_state = json.loads(state_path.read_text())
    assert "service_mode" not in final_state


def test_cancel_service_mode_when_nothing_active_reports_false(tmp_path):
    state_path = _write_state(tmp_path, {})

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)):
        was_active = service_mode.cancel_service_mode()

    assert was_active is False


def test_is_service_mode_active_true_when_flag_set(tmp_path):
    assert core.is_service_mode_active({"service_mode": {"active": True}}) is True
    assert core.is_service_mode_active({"service_mode": {"active": False}}) is False
    assert core.is_service_mode_active({}) is False


def test_print_status_not_active(tmp_path, capsys):
    state_path = _write_state(tmp_path, {})

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)):
        service_mode.print_status("Europe/London")

    assert "not active" in capsys.readouterr().out


def test_print_status_active(tmp_path, capsys):
    started_at = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    state_path = _write_state(
        tmp_path, {"service_mode": {"active": True, "started_at": started_at}}
    )

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)):
        service_mode.print_status("Europe/London")

    assert "ACTIVE" in capsys.readouterr().out


def test_main_start_reports_friendly_error_on_lock_timeout(monkeypatch, capsys):
    import sys

    import pytest

    monkeypatch.setattr(sys, "argv", ["service_mode.py", "--start"])
    fake_config = {"location": {"default_timezone_str": "Europe/London"}}

    with mock.patch.object(
        service_mode, "load_static_config", return_value=fake_config
    ), mock.patch.object(
        service_mode, "start_service_mode", side_effect=TimeoutError("lock busy")
    ), pytest.raises(SystemExit) as exc_info:
        service_mode.main()

    assert exc_info.value.code == 1
    assert "timed out" in capsys.readouterr().out


def test_main_cancel_reports_friendly_error_on_lock_timeout(monkeypatch, capsys):
    import sys

    import pytest

    monkeypatch.setattr(sys, "argv", ["service_mode.py", "--cancel"])
    fake_config = {"location": {"default_timezone_str": "Europe/London"}}

    with mock.patch.object(
        service_mode, "load_static_config", return_value=fake_config
    ), mock.patch.object(
        service_mode, "cancel_service_mode", side_effect=TimeoutError("lock busy")
    ), pytest.raises(SystemExit) as exc_info:
        service_mode.main()

    assert exc_info.value.code == 1
    assert "timed out" in capsys.readouterr().out
