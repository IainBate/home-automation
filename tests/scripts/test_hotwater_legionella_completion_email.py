"""Tests for the legionella-cycle-completed email (_notify_legionella_completed
in hotwater_automation_core.py), fired from both places last_completed_at gets
newly set to "now": run_legionella_progress_check (a forced cycle reaching its
disinfection threshold) and run_legionella_natural_completion_check (the tank
observed hot enough on its own).

send_email is mocked throughout - see the already-identified test-isolation
bug in tests/scenarios/test_hotwater_scenarios.py for why that matters here.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import hotwater_automation_core as core
from _fakes import FakeMelCloudClient


def _write_state(tmp_path: Path, state: dict) -> Path:
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return state_path


def _run(coro_factory, state_path: Path, client: FakeMelCloudClient):
    sent_emails: list[tuple[str, str]] = []
    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)), \
         mock.patch.object(core, "MelCloudClient", lambda config_path=None: client), \
         mock.patch.object(
             core, "send_email",
             lambda cfg, subject, body: sent_emails.append((subject, body)) or True,
         ):
        exit_code = asyncio.run(coro_factory())

    final_state = json.loads(state_path.read_text())
    return exit_code, final_state, sent_emails


def test_forced_cycle_completion_sends_exactly_one_email_with_next_due_date(tmp_path):
    state_path = _write_state(
        tmp_path,
        {
            "legionella": {
                "cycle_in_progress": True,
                "cycle_started_at": datetime.now(tz=UTC).isoformat(),
                "target_temp_c": 55.0,
                "original_target_temp_c": 50.0,
            }
        },
    )
    client = FakeMelCloudClient(tank_temp=55.0, target_temp=55.0)  # reaches disinfection threshold

    exit_code, final_state, emails = _run(
        lambda: core.run_legionella_progress_check(
            {}, {"legionella_interval_days": 90}, dry_run=False, quiet=True
        ),
        state_path,
        client,
    )

    assert exit_code == 0
    assert final_state["legionella"]["last_completed_at"] is not None
    assert len(emails) == 1
    subject, body = emails[0]
    assert "legionella cycle completed" in subject.lower()
    last_completed = datetime.fromisoformat(final_state["legionella"]["last_completed_at"])
    expected_next_due = (last_completed + timedelta(days=90)).strftime("%d %B %Y")
    assert expected_next_due in body


def test_forced_cycle_timeout_without_reaching_target_sends_no_completion_email(tmp_path):
    old_start = (datetime.now(tz=UTC) - timedelta(hours=2)).isoformat()
    state_path = _write_state(
        tmp_path,
        {
            "legionella": {
                "cycle_in_progress": True,
                "cycle_started_at": old_start,
                "target_temp_c": 55.0,
                "original_target_temp_c": 50.0,
            }
        },
    )
    client = FakeMelCloudClient(tank_temp=40.0, target_temp=55.0)  # never got hot enough

    exit_code, final_state, emails = _run(
        lambda: core.run_legionella_progress_check(
            {}, {"legionella_max_cycle_duration_hours": 1.0}, dry_run=False, quiet=True
        ),
        state_path,
        client,
    )

    assert exit_code == 0
    assert final_state["legionella"].get("last_completed_at") is None
    # The existing INSUFFICIENT_DURATION alert still fires - just not the
    # completion email, since nothing actually completed.
    assert not any("legionella cycle completed" in subject.lower() for subject, _ in emails)


def test_natural_completion_sends_exactly_one_email(tmp_path):
    state_path = _write_state(tmp_path, {})
    client = FakeMelCloudClient(tank_temp=56.0, target_temp=45.0)

    exit_code, final_state, emails = _run(
        lambda: core.run_legionella_natural_completion_check(
            {}, {"legionella_interval_days": 90}, dry_run=False, quiet=True
        ),
        state_path,
        client,
    )

    assert exit_code == 0
    assert final_state["legionella"]["last_completed_at"] is not None
    assert len(emails) == 1
    assert "legionella cycle completed" in emails[0][0].lower()


def test_natural_completion_already_recorded_today_sends_no_email(tmp_path):
    already_recorded_at = datetime.now(tz=UTC).isoformat()
    state_path = _write_state(
        tmp_path, {"legionella": {"last_completed_at": already_recorded_at}}
    )
    client = FakeMelCloudClient(tank_temp=56.0, target_temp=45.0)

    exit_code, final_state, emails = _run(
        lambda: core.run_legionella_natural_completion_check({}, {}, dry_run=False, quiet=True),
        state_path,
        client,
    )

    assert exit_code == 0
    assert final_state["legionella"]["last_completed_at"] == already_recorded_at
    assert emails == []


def test_natural_completion_dry_run_sends_no_email(tmp_path):
    state_path = _write_state(tmp_path, {})
    client = FakeMelCloudClient(tank_temp=56.0, target_temp=45.0)

    exit_code, _final_state, emails = _run(
        lambda: core.run_legionella_natural_completion_check({}, {}, dry_run=True, quiet=True),
        state_path,
        client,
    )

    assert exit_code == 0
    assert emails == []
