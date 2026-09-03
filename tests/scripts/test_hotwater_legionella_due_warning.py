"""Tests for check_legionella_due_warning in hotwater_automation_core.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import hotwater_automation_core as core


def _write_state(tmp_path: Path, state: dict) -> Path:
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return state_path


def _check(tmp_path, state_path, config, hw_config=None, *, dry_run=False, sent=True):
    hw_config = hw_config or {}
    sent_calls = []

    def fake_send_email(cfg, subject, body):
        sent_calls.append((subject, body))
        return sent

    with mock.patch.object(
        core, "get_hotwater_automation_state_path", lambda: str(state_path)
    ), mock.patch.object(core, "send_email", fake_send_email):
        exit_code = core.check_legionella_due_warning(
            config, hw_config, dry_run=dry_run, quiet=True
        )

    final_state = json.loads(state_path.read_text())
    return exit_code, final_state, sent_calls


def test_no_email_sent_when_email_disabled(tmp_path):
    state_path = _write_state(
        tmp_path,
        {"legionella": {"last_completed_at": (datetime.now(tz=UTC) - timedelta(days=85)).isoformat()}},
    )
    exit_code, final_state, sent_calls = _check(
        tmp_path, state_path, {"email": {"enabled": False}}
    )

    assert exit_code == 0
    assert sent_calls == []
    assert "due_warning_sent_for" not in final_state.get("legionella", {})


def test_no_cycle_ever_completed_is_a_noop(tmp_path):
    state_path = _write_state(tmp_path, {})
    exit_code, final_state, sent_calls = _check(tmp_path, state_path, {"email": {"enabled": True}})

    assert exit_code == 0
    assert sent_calls == []


def test_sends_warning_when_within_the_warning_window(tmp_path):
    # 90-day interval, 7-day warning: due in 5 days (85 days since completion).
    last_completed = (datetime.now(tz=UTC) - timedelta(days=85)).isoformat()
    state_path = _write_state(tmp_path, {"legionella": {"last_completed_at": last_completed}})

    exit_code, final_state, sent_calls = _check(
        tmp_path,
        state_path,
        {"email": {"enabled": True}},
        {"legionella_interval_days": 90, "legionella_due_warning_days": 7},
    )

    assert exit_code == 0
    assert len(sent_calls) == 1
    assert "legionella" in sent_calls[0][0].lower()
    assert final_state["legionella"]["due_warning_sent_for"] == last_completed


def test_too_early_does_not_send(tmp_path):
    # Only 10 days since completion, 90-day interval, 7-day warning - due in 80 days.
    last_completed = (datetime.now(tz=UTC) - timedelta(days=10)).isoformat()
    state_path = _write_state(tmp_path, {"legionella": {"last_completed_at": last_completed}})

    exit_code, final_state, sent_calls = _check(
        tmp_path,
        state_path,
        {"email": {"enabled": True}},
        {"legionella_interval_days": 90, "legionella_due_warning_days": 7},
    )

    assert exit_code == 0
    assert sent_calls == []


def test_already_due_does_not_send_a_new_warning(tmp_path):
    # Already past the interval entirely - the cycle itself is due, not "coming due".
    last_completed = (datetime.now(tz=UTC) - timedelta(days=95)).isoformat()
    state_path = _write_state(tmp_path, {"legionella": {"last_completed_at": last_completed}})

    exit_code, final_state, sent_calls = _check(
        tmp_path,
        state_path,
        {"email": {"enabled": True}},
        {"legionella_interval_days": 90, "legionella_due_warning_days": 7},
    )

    assert exit_code == 0
    assert sent_calls == []


def test_does_not_resend_within_the_same_interval(tmp_path):
    last_completed = (datetime.now(tz=UTC) - timedelta(days=85)).isoformat()
    state_path = _write_state(
        tmp_path,
        {"legionella": {"last_completed_at": last_completed, "due_warning_sent_for": last_completed}},
    )

    exit_code, final_state, sent_calls = _check(
        tmp_path,
        state_path,
        {"email": {"enabled": True}},
        {"legionella_interval_days": 90, "legionella_due_warning_days": 7},
    )

    assert exit_code == 0
    assert sent_calls == []


def test_dry_run_does_not_send_or_write(tmp_path):
    last_completed = (datetime.now(tz=UTC) - timedelta(days=85)).isoformat()
    state_path = _write_state(tmp_path, {"legionella": {"last_completed_at": last_completed}})

    exit_code, final_state, sent_calls = _check(
        tmp_path,
        state_path,
        {"email": {"enabled": True}},
        {"legionella_interval_days": 90, "legionella_due_warning_days": 7},
        dry_run=True,
    )

    assert exit_code == 0
    assert sent_calls == []
    assert "due_warning_sent_for" not in final_state["legionella"]


def test_failed_send_does_not_stamp_state(tmp_path):
    last_completed = (datetime.now(tz=UTC) - timedelta(days=85)).isoformat()
    state_path = _write_state(tmp_path, {"legionella": {"last_completed_at": last_completed}})

    exit_code, final_state, sent_calls = _check(
        tmp_path,
        state_path,
        {"email": {"enabled": True}},
        {"legionella_interval_days": 90, "legionella_due_warning_days": 7},
        sent=False,
    )

    assert exit_code == 0
    assert len(sent_calls) == 1
    assert "due_warning_sent_for" not in final_state["legionella"]
