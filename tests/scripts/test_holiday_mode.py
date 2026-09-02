"""Tests for scripts/holiday_mode.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import holiday_mode
import hotwater_automation_core as core


def _write_state(tmp_path: Path, state: dict) -> Path:
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return state_path


def test_start_holiday_writes_until_n_days_from_now(tmp_path):
    state_path = _write_state(tmp_path, {})

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)):
        before = datetime.now(tz=UTC)
        until = holiday_mode.start_holiday(7)
        after = datetime.now(tz=UTC)

    assert timedelta(days=7) - timedelta(seconds=5) <= until - before <= timedelta(days=7) + timedelta(
        seconds=5
    )
    assert until <= after + timedelta(days=7)

    final_state = json.loads(state_path.read_text())
    assert final_state["holiday"]["until"] == until.isoformat()
    assert final_state["holiday"]["days"] == 7


def test_start_holiday_preserves_other_state_keys(tmp_path):
    state_path = _write_state(tmp_path, {"force_heat_activated_at": "2026-01-01T00:00:00+00:00"})

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)):
        holiday_mode.start_holiday(3)

    final_state = json.loads(state_path.read_text())
    assert final_state["force_heat_activated_at"] == "2026-01-01T00:00:00+00:00"
    assert "holiday" in final_state


def test_cancel_holiday_clears_active_holiday_and_reports_it_was_active(tmp_path):
    until = (datetime.now(tz=UTC) + timedelta(days=2)).isoformat()
    state_path = _write_state(tmp_path, {"holiday": {"until": until, "days": 2}})

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)):
        was_active = holiday_mode.cancel_holiday()

    assert was_active is True
    final_state = json.loads(state_path.read_text())
    assert "holiday" not in final_state


def test_cancel_holiday_when_nothing_active_reports_false(tmp_path):
    state_path = _write_state(tmp_path, {})

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)):
        was_active = holiday_mode.cancel_holiday()

    assert was_active is False


def test_cancel_holiday_on_already_expired_holiday_reports_false_but_still_clears(tmp_path):
    expired_until = (datetime.now(tz=UTC) - timedelta(days=1)).isoformat()
    state_path = _write_state(tmp_path, {"holiday": {"until": expired_until, "days": 2}})

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)):
        was_active = holiday_mode.cancel_holiday()
        final_state = json.loads(state_path.read_text())

    assert was_active is False
    assert "holiday" not in final_state


def test_print_status_not_active(tmp_path, capsys):
    state_path = _write_state(tmp_path, {})

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)):
        holiday_mode.print_status("Europe/London")

    assert "not active" in capsys.readouterr().out


def test_print_status_active(tmp_path, capsys):
    until = (datetime.now(tz=UTC) + timedelta(days=2)).isoformat()
    state_path = _write_state(tmp_path, {"holiday": {"until": until, "days": 2}})

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)):
        holiday_mode.print_status("Europe/London")

    assert "ACTIVE" in capsys.readouterr().out


def test_print_status_expired(tmp_path, capsys):
    until = (datetime.now(tz=UTC) - timedelta(days=1)).isoformat()
    state_path = _write_state(tmp_path, {"holiday": {"until": until, "days": 2}})

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)):
        holiday_mode.print_status("Europe/London")

    assert "expired" in capsys.readouterr().out


def test_main_start_days_reports_friendly_error_on_lock_timeout(monkeypatch, capsys):
    import sys

    monkeypatch.setattr(sys, "argv", ["holiday_mode.py", "--start-days", "7"])
    fake_config = {"location": {"default_timezone_str": "Europe/London"}}

    with mock.patch.object(
        holiday_mode, "load_static_config", return_value=fake_config
    ), mock.patch.object(
        holiday_mode, "start_holiday", side_effect=TimeoutError("lock busy")
    ), mock.patch.object(sys, "exit") as exit_mock:
        holiday_mode.main()

    exit_mock.assert_called_once_with(1)
    assert "timed out" in capsys.readouterr().out


def test_main_cancel_reports_friendly_error_on_lock_timeout(monkeypatch, capsys):
    import sys

    monkeypatch.setattr(sys, "argv", ["holiday_mode.py", "--cancel"])
    fake_config = {"location": {"default_timezone_str": "Europe/London"}}

    with mock.patch.object(
        holiday_mode, "load_static_config", return_value=fake_config
    ), mock.patch.object(
        holiday_mode, "cancel_holiday", side_effect=TimeoutError("lock busy")
    ), mock.patch.object(sys, "exit") as exit_mock:
        holiday_mode.main()

    exit_mock.assert_called_once_with(1)
    assert "timed out" in capsys.readouterr().out
