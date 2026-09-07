"""Tests for run_safety_ceiling_check in hotwater_automation_core.py - the
independent last-resort backstop (see its own docstring).

Unlike run_revert_check/run_legionella_progress_check's tests, these do NOT
forbid read_state() - this function deliberately uses the plain unlocked
read for its own violation-detection pass (it never writes to the state
file on a PLAIN force-heat violation - see test_no_violation_does_not_write_state
and test_temperature_violation_does_not_write_state below). The one
exception (confirmed 2026-09-07, see run_safety_ceiling_check's own
docstring) is a legionella cycle in progress at the moment a violation is
found - it DOES then acquire locked_state() to complete/timeout that cycle's
bookkeeping exactly as run_legionella_progress_check's own reached_target/
timed_out branches would, tested separately below.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import hotwater_automation_core as core
from src.api_clients.melcloud_client import HotWaterOperationMode


class FakeMelCloudClient:
    """Stand-in for MelCloudClient - records calls instead of touching MELCloud."""

    def __init__(self, *, tank_temp: float | None, operation_mode: HotWaterOperationMode) -> None:
        self.tank_temp = tank_temp
        self.operation_mode = operation_mode
        self.force_calls: list[bool] = []
        self.target_temp_calls: list[float] = []

    async def connect(self) -> None:
        return None

    async def get_tank_status(self) -> dict:
        return {"tank_temperature": self.tank_temp, "operation_mode": self.operation_mode}

    async def set_force_hot_water(self, *, enabled: bool) -> bool:
        self.force_calls.append(enabled)
        return True

    async def set_target_tank_temperature(self, temp: float) -> None:
        self.target_temp_calls.append(temp)

    async def close(self) -> None:
        return None


def _write_state(tmp_path: Path, state: dict) -> Path:
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return state_path


def _run(hw_config: dict, state_path: Path, client: FakeMelCloudClient, *, dry_run: bool = False):
    sent_emails: list[tuple[str, str]] = []
    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)), \
         mock.patch.object(core, "MelCloudClient", lambda config_path=None: client), \
         mock.patch.object(
             core, "send_email",
             lambda cfg, subject, body: sent_emails.append((subject, body)) or True,
         ):
        exit_code = asyncio.run(
            core.run_safety_ceiling_check({}, hw_config, dry_run=dry_run, quiet=True)
        )

    state_bytes_after = state_path.read_bytes()
    return exit_code, state_bytes_after, sent_emails


def test_no_violation_is_a_noop(tmp_path):
    state_path = _write_state(tmp_path, {})
    state_bytes_before = state_path.read_bytes()
    client = FakeMelCloudClient(tank_temp=45.0, operation_mode=HotWaterOperationMode.AUTO)

    exit_code, state_bytes_after, emails = _run({}, state_path, client)

    assert exit_code == 0
    assert client.force_calls == []
    assert emails == []
    assert state_bytes_after == state_bytes_before  # never writes


def test_temperature_violation_cuts_force_heat_and_emails(tmp_path):
    state_path = _write_state(tmp_path, {})
    state_bytes_before = state_path.read_bytes()
    client = FakeMelCloudClient(tank_temp=61.0, operation_mode=HotWaterOperationMode.AUTO)

    exit_code, state_bytes_after, emails = _run(
        {"safety_ceiling_temp_c": 60.0}, state_path, client
    )

    assert exit_code == 0
    assert client.force_calls == [False]
    assert len(emails) == 1
    assert "SAFETY CEILING" in emails[0][0]
    assert state_bytes_after == state_bytes_before  # still never writes state


def test_temperature_exactly_at_ceiling_is_a_violation(tmp_path):
    state_path = _write_state(tmp_path, {})
    client = FakeMelCloudClient(tank_temp=60.0, operation_mode=HotWaterOperationMode.AUTO)

    exit_code, _state_bytes, emails = _run({"safety_ceiling_temp_c": 60.0}, state_path, client)

    assert exit_code == 0
    assert client.force_calls == [False]
    assert len(emails) == 1


def test_duration_violation_via_old_force_heat_activated_at(tmp_path):
    old_activation = (datetime.now(tz=UTC) - timedelta(hours=4)).isoformat()
    state_path = _write_state(tmp_path, {"force_heat_activated_at": old_activation})
    client = FakeMelCloudClient(tank_temp=48.0, operation_mode=HotWaterOperationMode.FORCE_HOT_WATER)

    exit_code, _state_bytes, emails = _run(
        {"safety_max_duration_hours": 3.0}, state_path, client
    )

    assert exit_code == 0
    assert client.force_calls == [False]
    assert len(emails) == 1
    assert "SAFETY CEILING" in emails[0][0]


def test_duration_within_limit_is_not_a_violation(tmp_path):
    recent_activation = (datetime.now(tz=UTC) - timedelta(minutes=30)).isoformat()
    state_path = _write_state(tmp_path, {"force_heat_activated_at": recent_activation})
    client = FakeMelCloudClient(tank_temp=48.0, operation_mode=HotWaterOperationMode.FORCE_HOT_WATER)

    exit_code, _state_bytes, emails = _run(
        {"safety_max_duration_hours": 3.0}, state_path, client
    )

    assert exit_code == 0
    assert client.force_calls == []
    assert emails == []


def test_duration_check_uses_legionella_cycle_started_at_when_in_progress(tmp_path):
    """force_heat_activated_at is recent (would pass on its own), but a
    legionella cycle has actually been running for hours - the duration
    check must key off cycle_started_at, not force_heat_activated_at.
    """
    old_legionella_start = (datetime.now(tz=UTC) - timedelta(hours=4)).isoformat()
    recent_force_heat = datetime.now(tz=UTC).isoformat()
    state_path = _write_state(
        tmp_path,
        {
            "force_heat_activated_at": recent_force_heat,
            "legionella": {"cycle_in_progress": True, "cycle_started_at": old_legionella_start},
        },
    )
    client = FakeMelCloudClient(tank_temp=53.0, operation_mode=HotWaterOperationMode.FORCE_HOT_WATER)

    exit_code, _state_bytes, emails = _run(
        {"safety_max_duration_hours": 3.0}, state_path, client
    )

    assert exit_code == 0
    assert client.force_calls == [False]
    assert len(emails) == 1


def test_actively_heating_with_no_start_timestamp_is_treated_as_violated(tmp_path):
    """Fail-safe direction: 'can't tell how long this has been running' while
    actively force-heating must never be read as permission to leave it
    alone.
    """
    state_path = _write_state(tmp_path, {})  # no force_heat_activated_at at all
    client = FakeMelCloudClient(tank_temp=48.0, operation_mode=HotWaterOperationMode.FORCE_HOT_WATER)

    exit_code, _state_bytes, emails = _run(
        {"safety_max_duration_hours": 3.0}, state_path, client
    )

    assert exit_code == 0
    assert client.force_calls == [False]
    assert len(emails) == 1


def test_actively_heating_with_malformed_start_timestamp_is_treated_as_violated(tmp_path):
    state_path = _write_state(tmp_path, {"force_heat_activated_at": "not-a-timestamp"})
    client = FakeMelCloudClient(tank_temp=48.0, operation_mode=HotWaterOperationMode.FORCE_HOT_WATER)

    exit_code, _state_bytes, emails = _run(
        {"safety_max_duration_hours": 3.0}, state_path, client
    )

    assert exit_code == 0
    assert client.force_calls == [False]
    assert len(emails) == 1


def test_dry_run_makes_no_call_and_sends_no_email(tmp_path):
    state_path = _write_state(tmp_path, {})
    client = FakeMelCloudClient(tank_temp=65.0, operation_mode=HotWaterOperationMode.AUTO)

    exit_code, _state_bytes, emails = _run(
        {"safety_ceiling_temp_c": 60.0}, state_path, client, dry_run=True
    )

    assert exit_code == 0
    assert client.force_calls == []
    assert emails == []


def test_defaults_are_well_above_normal_operating_targets(tmp_path):
    """Not exercising the actual defaults' numeric values by name (that's
    what the config schema bounds test covers) - just confirming a normal,
    healthy reading (well within every other real limit) produces no
    violation under the module's own DEFAULT_* constants with an empty
    hw_config.
    """
    state_path = _write_state(tmp_path, {"force_heat_activated_at": datetime.now(tz=UTC).isoformat()})
    # 50C, not 55C - the ceiling default now EQUALS 55 (the household's
    # actual absolute limit, confirmed 2026-09-07), not a margin above the
    # normal 50C target, so 50C is the right "healthy" reading here.
    client = FakeMelCloudClient(tank_temp=50.0, operation_mode=HotWaterOperationMode.FORCE_HOT_WATER)

    exit_code, _state_bytes, emails = _run({}, state_path, client)

    assert exit_code == 0
    assert client.force_calls == []
    assert emails == []


# --- Legionella cycle in progress: completion/timeout crediting ------------


def test_legionella_temp_violation_completes_the_cycle_quietly(tmp_path):
    """The ceiling now EQUALS the legionella completion temperature, so
    reaching it while a cycle is in progress is a normal completion, not an
    alarm - restore the target, credit last_completed_at, send the calm
    completion email (not the SAFETY CEILING one), log at INFO not CRITICAL.
    """
    state_path = _write_state(
        tmp_path,
        {
            "legionella": {
                "cycle_in_progress": True,
                "cycle_started_at": datetime.now(tz=UTC).isoformat(),
                "original_target_temp_c": 50.0,
                "target_temp_c": 55.0,
            }
        },
    )
    client = FakeMelCloudClient(tank_temp=55.0, operation_mode=HotWaterOperationMode.FORCE_HOT_WATER)

    exit_code, state_bytes, emails = _run({}, state_path, client)

    assert exit_code == 0
    assert client.force_calls == [False]
    assert client.target_temp_calls == [50.0]  # restored to the original target
    assert len(emails) == 1
    assert "legionella cycle completed" in emails[0][0].lower()

    final_state = json.loads(state_bytes)
    assert final_state["legionella"]["cycle_in_progress"] is False
    assert final_state["legionella"]["last_completed_at"] is not None


def test_legionella_duration_timeout_cleans_up_without_crediting(tmp_path):
    """Below the completion temperature, but timed out on duration - a
    genuine timeout (like run_legionella_progress_check's own timed_out
    branch): target still restored so it doesn't stay wrong, but NOT
    credited as complete, and still the loud SAFETY CEILING alarm.
    """
    old_start = (datetime.now(tz=UTC) - timedelta(hours=4)).isoformat()
    state_path = _write_state(
        tmp_path,
        {
            "legionella": {
                "cycle_in_progress": True,
                "cycle_started_at": old_start,
                "original_target_temp_c": 50.0,
                "target_temp_c": 55.0,
            }
        },
    )
    client = FakeMelCloudClient(tank_temp=48.0, operation_mode=HotWaterOperationMode.FORCE_HOT_WATER)

    exit_code, state_bytes, emails = _run(
        {"safety_max_duration_hours": 3.0}, state_path, client
    )

    assert exit_code == 0
    assert client.force_calls == [False]
    assert client.target_temp_calls == [50.0]
    assert len(emails) == 1
    assert "safety ceiling" in emails[0][0].lower()
    assert "not been credited as complete" in emails[0][1].lower()

    final_state = json.loads(state_bytes)
    assert final_state["legionella"]["cycle_in_progress"] is False
    # Matches run_legionella_progress_check's own timed_out branch: the key
    # is always present, just left falsy rather than credited.
    assert not final_state["legionella"].get("last_completed_at")


def test_temperature_violation_without_a_legionella_cycle_is_unaffected(tmp_path):
    """No legionella state at all - the original, unchanged behavior: loud
    alarm, no state write, no set_target_tank_temperature call.
    """
    state_path = _write_state(tmp_path, {})
    client = FakeMelCloudClient(tank_temp=56.0, operation_mode=HotWaterOperationMode.FORCE_HOT_WATER)

    exit_code, state_bytes, emails = _run({}, state_path, client)

    assert exit_code == 0
    assert client.force_calls == [False]
    assert client.target_temp_calls == []
    assert len(emails) == 1
    assert "safety ceiling" in emails[0][0].lower()
    assert json.loads(state_bytes) == {}


def test_legionella_completion_is_a_noop_if_something_else_already_cleared_it(tmp_path):
    """A race guard: if cycle_in_progress is already False by the time the
    lock is acquired (e.g. run_legionella_progress_check's own concurrent
    tick got there first), don't overwrite whatever it already wrote.
    """
    state_path = _write_state(
        tmp_path,
        {
            "legionella": {
                "cycle_in_progress": False,
                "last_completed_at": "2026-01-01T00:00:00+00:00",
            }
        },
    )
    client = FakeMelCloudClient(tank_temp=55.0, operation_mode=HotWaterOperationMode.FORCE_HOT_WATER)

    exit_code, state_bytes, _emails = _run({}, state_path, client)

    assert exit_code == 0
    final_state = json.loads(state_bytes)
    assert final_state["legionella"]["last_completed_at"] == "2026-01-01T00:00:00+00:00"
