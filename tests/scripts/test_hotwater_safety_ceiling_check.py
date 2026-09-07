"""Tests for run_safety_ceiling_check in hotwater_automation_core.py - the
independent, one-way last-resort backstop (see its own docstring).

Unlike run_revert_check/run_legionella_progress_check's tests, these do NOT
forbid read_state() - this function deliberately uses the plain unlocked
read (it never writes to the state file at all), which is exactly the
property under test in test_no_violation_does_not_write_state and
test_temperature_violation_does_not_write_state below.
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

    async def connect(self) -> None:
        return None

    async def get_tank_status(self) -> dict:
        return {"tank_temperature": self.tank_temp, "operation_mode": self.operation_mode}

    async def set_force_hot_water(self, *, enabled: bool) -> bool:
        self.force_calls.append(enabled)
        return True

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
    client = FakeMelCloudClient(tank_temp=55.0, operation_mode=HotWaterOperationMode.FORCE_HOT_WATER)

    exit_code, _state_bytes, emails = _run({}, state_path, client)

    assert exit_code == 0
    assert client.force_calls == []
    assert emails == []
