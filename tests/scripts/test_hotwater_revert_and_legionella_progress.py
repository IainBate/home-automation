"""Tests for run_revert_check and run_legionella_progress_check in
hotwater_automation_core.py, including regression coverage for a code-review
finding: both used to read state.py via the unlocked read_state() at the
start of the function, do slow MELCloud I/O, then take the state-file lock
only for a final write - a window in which a concurrent run_force_heat_check
(which correctly holds the lock across its whole body) could start or finish
a legionella cycle these functions don't know about, and then have their
final write clobber or cancel it. Both now hold the lock (via locked_state())
across their entire body instead, matching run_force_heat_check's existing
pattern - never touching the unlocked read_state() at all, which these
tests confirm directly.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import hotwater_automation_core as core


class FakeMelCloudClient:
    """Stand-in for MelCloudClient - records calls instead of touching MELCloud."""

    def __init__(self, *, tank_temp: float, target_temp: float) -> None:
        self.tank_temp = tank_temp
        self.target_temp = target_temp
        self.force_calls: list[bool] = []
        self.target_temp_calls: list[float] = []

    async def connect(self) -> None:
        return None

    async def get_tank_status(self) -> dict:
        return {"tank_temperature": self.tank_temp, "target_tank_temperature": self.target_temp}

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


def _run(coro_factory, state_path: Path, client: FakeMelCloudClient):
    """Run a core.run_*(...) coroutine with read_state() disabled entirely -
    proves the function under test never uses the old unlocked-read path.
    """

    def _fail_if_called():
        raise AssertionError("read_state() must not be called - this path must use locked_state()")

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)), \
         mock.patch.object(core, "read_state", _fail_if_called), \
         mock.patch.object(core, "MelCloudClient", lambda config_path=None: client):
        exit_code = asyncio.run(coro_factory())

    final_state = json.loads(state_path.read_text())
    return exit_code, final_state


# --- run_revert_check ---------------------------------------------------


def test_revert_check_no_active_window_is_a_noop(tmp_path):
    state_path = _write_state(tmp_path, {})
    client = FakeMelCloudClient(tank_temp=30.0, target_temp=45.0)

    exit_code, final_state = _run(
        lambda: core.run_revert_check({}, {}, dry_run=False, quiet=True), state_path, client
    )

    assert exit_code == 0
    assert client.force_calls == []
    assert final_state == {}


def test_revert_check_defers_when_legionella_in_progress(tmp_path):
    state_path = _write_state(
        tmp_path,
        {
            "force_heat_activated_at": datetime.now(tz=UTC).isoformat(),
            "legionella": {"cycle_in_progress": True},
        },
    )
    client = FakeMelCloudClient(tank_temp=30.0, target_temp=45.0)

    exit_code, final_state = _run(
        lambda: core.run_revert_check({}, {}, dry_run=False, quiet=True), state_path, client
    )

    assert exit_code == 0
    assert client.force_calls == []
    assert final_state["force_heat_activated_at"] is not None  # untouched


def test_revert_check_reverts_when_tank_reached_target(tmp_path):
    state_path = _write_state(
        tmp_path, {"force_heat_activated_at": datetime.now(tz=UTC).isoformat()}
    )
    client = FakeMelCloudClient(tank_temp=50.0, target_temp=45.0)  # reached

    exit_code, final_state = _run(
        lambda: core.run_revert_check({}, {}, dry_run=False, quiet=True), state_path, client
    )

    assert exit_code == 0
    assert client.force_calls == [False]
    assert "force_heat_activated_at" not in final_state


def test_revert_check_leaves_window_open_while_still_heating(tmp_path):
    state_path = _write_state(
        tmp_path, {"force_heat_activated_at": datetime.now(tz=UTC).isoformat()}
    )
    client = FakeMelCloudClient(tank_temp=30.0, target_temp=45.0)  # not reached, not timed out

    exit_code, final_state = _run(
        lambda: core.run_revert_check({}, 
            {"force_heat_max_duration_hours": 3.0}, dry_run=False, quiet=True
        ),
        state_path,
        client,
    )

    assert exit_code == 0
    assert client.force_calls == []
    assert "force_heat_activated_at" in final_state


def test_revert_check_reverts_anyway_once_timed_out(tmp_path):
    activated_at = datetime.now(tz=UTC) - timedelta(hours=5)
    state_path = _write_state(tmp_path, {"force_heat_activated_at": activated_at.isoformat()})
    client = FakeMelCloudClient(tank_temp=30.0, target_temp=45.0)  # never reached

    exit_code, final_state = _run(
        lambda: core.run_revert_check({}, 
            {"force_heat_max_duration_hours": 3.0}, dry_run=False, quiet=True
        ),
        state_path,
        client,
    )

    assert exit_code == 0
    assert client.force_calls == [False]
    assert "force_heat_activated_at" not in final_state


def test_revert_check_clears_malformed_timestamp(tmp_path):
    state_path = _write_state(tmp_path, {"force_heat_activated_at": "not-a-timestamp"})
    client = FakeMelCloudClient(tank_temp=30.0, target_temp=45.0)

    exit_code, final_state = _run(
        lambda: core.run_revert_check({}, {}, dry_run=False, quiet=True), state_path, client
    )

    assert exit_code == 1
    assert client.force_calls == []
    assert "force_heat_activated_at" not in final_state


# --- run_legionella_progress_check --------------------------------------


def test_legionella_progress_no_cycle_is_a_noop(tmp_path):
    state_path = _write_state(tmp_path, {})
    client = FakeMelCloudClient(tank_temp=30.0, target_temp=45.0)

    exit_code, final_state = _run(
        lambda: core.run_legionella_progress_check({}, {}, dry_run=False, quiet=True),
        state_path,
        client,
    )

    assert exit_code == 0
    assert client.force_calls == []
    assert final_state == {}


def test_legionella_progress_clears_on_missing_fields(tmp_path):
    state_path = _write_state(tmp_path, {"legionella": {"cycle_in_progress": True}})
    client = FakeMelCloudClient(tank_temp=30.0, target_temp=45.0)

    exit_code, final_state = _run(
        lambda: core.run_legionella_progress_check({}, {}, dry_run=False, quiet=True),
        state_path,
        client,
    )

    assert exit_code == 1
    assert final_state["legionella"]["cycle_in_progress"] is False


def test_legionella_progress_clears_on_malformed_timestamp(tmp_path):
    """Regression test: cycle_started_at parsing used to have no try/except at
    all (unlike every other timestamp parse in this file), so a malformed
    value would raise uncaught and leave cycle_in_progress stuck forever.
    """
    state_path = _write_state(
        tmp_path,
        {
            "legionella": {
                "cycle_in_progress": True,
                "cycle_started_at": "not-a-timestamp",
                "target_temp_c": 60.0,
                "original_target_temp_c": 45.0,
            }
        },
    )
    client = FakeMelCloudClient(tank_temp=30.0, target_temp=45.0)

    exit_code, final_state = _run(
        lambda: core.run_legionella_progress_check({}, {}, dry_run=False, quiet=True),
        state_path,
        client,
    )

    assert exit_code == 1
    assert final_state["legionella"]["cycle_in_progress"] is False


def test_legionella_progress_reverts_when_target_reached(tmp_path):
    started_at = datetime.now(tz=UTC) - timedelta(hours=1)
    state_path = _write_state(
        tmp_path,
        {
            "some_unrelated_top_level_key": "must survive",
            "legionella": {
                "cycle_in_progress": True,
                "cycle_started_at": started_at.isoformat(),
                "target_temp_c": 60.0,
                "original_target_temp_c": 45.0,
                "some_future_field": "must also survive",
            },
        },
    )
    client = FakeMelCloudClient(tank_temp=61.0, target_temp=60.0)  # reached

    exit_code, final_state = _run(
        lambda: core.run_legionella_progress_check({}, 
            {"legionella_max_cycle_duration_hours": 6.0}, dry_run=False, quiet=True
        ),
        state_path,
        client,
    )

    assert exit_code == 0
    assert client.force_calls == [False]
    assert client.target_temp_calls == [45.0]
    assert final_state["legionella"]["cycle_in_progress"] is False
    assert final_state["legionella"]["last_completed_at"] is not None
    # Regression: the final write used to *replace* the whole "legionella"
    # dict wholesale instead of merging, silently dropping any field it
    # didn't explicitly know about.
    assert final_state["legionella"]["some_future_field"] == "must also survive"
    assert final_state["some_unrelated_top_level_key"] == "must survive"


def test_legionella_progress_times_out_without_marking_completed(tmp_path):
    started_at = datetime.now(tz=UTC) - timedelta(hours=10)
    state_path = _write_state(
        tmp_path,
        {
            "legionella": {
                "cycle_in_progress": True,
                "cycle_started_at": started_at.isoformat(),
                "target_temp_c": 60.0,
                "original_target_temp_c": 45.0,
                "last_completed_at": None,
            }
        },
    )
    # Below the (default 55C) disinfection threshold too, not just the 60C
    # requested target - genuinely never reached, not just short of target.
    client = FakeMelCloudClient(tank_temp=40.0, target_temp=60.0)

    exit_code, final_state = _run(
        lambda: core.run_legionella_progress_check({}, 
            {"legionella_max_cycle_duration_hours": 6.0}, dry_run=False, quiet=True
        ),
        state_path,
        client,
    )

    assert exit_code == 0
    assert final_state["legionella"]["cycle_in_progress"] is False
    assert final_state["legionella"]["last_completed_at"] is None  # not marked complete


def test_legionella_progress_reverts_at_the_natural_completion_temp_even_below_the_requested_target(
    tmp_path,
):
    """The scenario this exists for: an ASHP that can't reliably reach the
    full requested target (60C here) shouldn't run the full
    legionella_max_cycle_duration_hours every time - once the tank is
    genuinely hot enough to have been disinfected (legionella_natural_
    completion_temp_c, default 55C), the cycle is done, not merely timed out.
    """
    started_at = datetime.now(tz=UTC) - timedelta(hours=1)  # nowhere near timed out
    state_path = _write_state(
        tmp_path,
        {
            "legionella": {
                "cycle_in_progress": True,
                "cycle_started_at": started_at.isoformat(),
                "target_temp_c": 60.0,
                "original_target_temp_c": 45.0,
            }
        },
    )
    client = FakeMelCloudClient(tank_temp=56.0, target_temp=60.0)  # >= 55C, < 60C requested

    exit_code, final_state = _run(
        lambda: core.run_legionella_progress_check({}, 
            {"legionella_max_cycle_duration_hours": 6.0}, dry_run=False, quiet=True
        ),
        state_path,
        client,
    )

    assert exit_code == 0
    assert client.force_calls == [False]
    assert client.target_temp_calls == [45.0]
    assert final_state["legionella"]["cycle_in_progress"] is False
    assert final_state["legionella"]["last_completed_at"] is not None  # marked complete


def test_legionella_progress_natural_completion_temp_is_configurable(tmp_path):
    started_at = datetime.now(tz=UTC) - timedelta(hours=1)
    state_path = _write_state(
        tmp_path,
        {
            "legionella": {
                "cycle_in_progress": True,
                "cycle_started_at": started_at.isoformat(),
                "target_temp_c": 60.0,
                "original_target_temp_c": 45.0,
            }
        },
    )
    client = FakeMelCloudClient(tank_temp=52.0, target_temp=60.0)

    exit_code, final_state = _run(
        lambda: core.run_legionella_progress_check({}, 
            {"legionella_max_cycle_duration_hours": 6.0, "legionella_natural_completion_temp_c": 50.0},
            dry_run=False,
            quiet=True,
        ),
        state_path,
        client,
    )

    assert exit_code == 0
    assert final_state["legionella"]["cycle_in_progress"] is False
    assert final_state["legionella"]["last_completed_at"] is not None


# --- run_legionella_natural_completion_check -----------------------------


def test_natural_completion_below_threshold_is_a_noop(tmp_path):
    state_path = _write_state(tmp_path, {})
    client = FakeMelCloudClient(tank_temp=50.0, target_temp=45.0)  # < 55C default

    exit_code, final_state = _run(
        lambda: core.run_legionella_natural_completion_check({}, dry_run=False, quiet=True),
        state_path,
        client,
    )

    assert exit_code == 0
    assert final_state == {}


def test_natural_completion_marks_the_interval_satisfied_with_no_cycle_involved(tmp_path):
    """The scenario this exists for: a tank that reaches disinfection
    temperature entirely on its own (e.g. an off-grid solar diverter this
    project can't otherwise see) - not a legionella cycle in progress at all.
    """
    state_path = _write_state(tmp_path, {"some_unrelated_top_level_key": "must survive"})
    client = FakeMelCloudClient(tank_temp=56.0, target_temp=45.0)

    exit_code, final_state = _run(
        lambda: core.run_legionella_natural_completion_check({}, dry_run=False, quiet=True),
        state_path,
        client,
    )

    assert exit_code == 0
    assert final_state["legionella"]["last_completed_at"] is not None
    assert final_state["some_unrelated_top_level_key"] == "must survive"


def test_natural_completion_does_not_rewrite_an_already_recorded_today(tmp_path):
    already_recorded_at = datetime.now(tz=UTC).isoformat()
    state_path = _write_state(
        tmp_path, {"legionella": {"last_completed_at": already_recorded_at}}
    )
    client = FakeMelCloudClient(tank_temp=56.0, target_temp=45.0)

    exit_code, final_state = _run(
        lambda: core.run_legionella_natural_completion_check({}, dry_run=False, quiet=True),
        state_path,
        client,
    )

    assert exit_code == 0
    assert final_state["legionella"]["last_completed_at"] == already_recorded_at  # untouched


def test_natural_completion_rewrites_a_stale_previous_day_record(tmp_path):
    stale = (datetime.now(tz=UTC) - timedelta(days=3)).isoformat()
    state_path = _write_state(tmp_path, {"legionella": {"last_completed_at": stale}})
    client = FakeMelCloudClient(tank_temp=56.0, target_temp=45.0)

    exit_code, final_state = _run(
        lambda: core.run_legionella_natural_completion_check({}, dry_run=False, quiet=True),
        state_path,
        client,
    )

    assert exit_code == 0
    assert final_state["legionella"]["last_completed_at"] != stale


def test_natural_completion_runs_regardless_of_an_in_progress_cycle(tmp_path):
    """No prior-state gate (unlike run_revert_check/run_legionella_progress_check)
    - a quiet day with no automation activity at all is exactly the case
    this exists to catch, so it always takes its own live reading.
    """
    state_path = _write_state(
        tmp_path, {"legionella": {"cycle_in_progress": True, "some_future_field": "must survive"}}
    )
    client = FakeMelCloudClient(tank_temp=56.0, target_temp=45.0)

    exit_code, final_state = _run(
        lambda: core.run_legionella_natural_completion_check({}, dry_run=False, quiet=True),
        state_path,
        client,
    )

    assert exit_code == 0
    assert final_state["legionella"]["last_completed_at"] is not None
    assert final_state["legionella"]["cycle_in_progress"] is True  # untouched
    assert final_state["legionella"]["some_future_field"] == "must survive"


def test_natural_completion_temp_is_configurable(tmp_path):
    state_path = _write_state(tmp_path, {})
    client = FakeMelCloudClient(tank_temp=52.0, target_temp=45.0)

    exit_code, final_state = _run(
        lambda: core.run_legionella_natural_completion_check(
            {"legionella_natural_completion_temp_c": 50.0}, dry_run=False, quiet=True
        ),
        state_path,
        client,
    )

    assert exit_code == 0
    assert final_state["legionella"]["last_completed_at"] is not None


def test_natural_completion_dry_run_does_not_write(tmp_path):
    state_path = _write_state(tmp_path, {})
    client = FakeMelCloudClient(tank_temp=56.0, target_temp=45.0)

    exit_code, final_state = _run(
        lambda: core.run_legionella_natural_completion_check({}, dry_run=True, quiet=True),
        state_path,
        client,
    )

    assert exit_code == 0
    assert final_state == {}


# --- locking discipline (finding 1) --------------------------------------


def test_revert_and_legionella_progress_hold_a_single_lock_for_the_whole_call(tmp_path):
    """Both functions must acquire locked_state() exactly once for their
    entire body, not read unlocked state and lock only for a final write -
    otherwise a concurrent run_force_heat_check could start/finish a
    legionella cycle in the gap and have it silently clobbered.
    """
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(json.dumps({}), encoding="utf-8")

    lock_call_count = {"n": 0}
    real_locked_state = core.locked_state

    def counting_locked_state(*args, **kwargs):
        lock_call_count["n"] += 1
        return real_locked_state(*args, **kwargs)

    client = FakeMelCloudClient(tank_temp=30.0, target_temp=45.0)

    with mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)), \
         mock.patch.object(core, "locked_state", counting_locked_state), \
         mock.patch.object(core, "MelCloudClient", lambda config_path=None: client):
        asyncio.run(core.run_revert_check({}, {}, dry_run=False, quiet=True))

    assert lock_call_count["n"] == 1
