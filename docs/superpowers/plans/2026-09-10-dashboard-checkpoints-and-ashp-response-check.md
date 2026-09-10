# Dashboard Checkpoints & ASHP Response Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) Make the dashboard's battery-SoC checkpoints show the two clock times the hot-water force-heat decision actually cares about (derived from live config/schedule, not hardcoded round numbers), and (2) add a read-only check that cross-references MELCloud's own device status against the ASHP's T6R heat-call, to catch the heat pump silently not responding - tolerant of the heat pump's real response lag.

**Architecture:** Both parts extend existing, already-tested modules rather than adding new subsystems. Part 1 reuses `battery_prediction_eligibility_end_hour`/`derive_forced_discharge_start_hour` (added earlier this session) by promoting one existing private loader to a shared, public function. Part 2 adds one new pure decision module (`ashp_response_check_logic.py`), mirroring the existing `interference_logic.py`'s shape (dataclass state + clock-injected `evaluate()` function, persisted in the same `hvac_automation_state.json` next to `ashp_interference`), and wires it into `ashp_automation_core.py`'s existing decision-check cycle. No new files' worth of infrastructure, no new MELCloud API calls - the compressor-status signal (`HEAT_ZONES`/`HEAT_WATER`/`IDLE`/etc.) is already fetched and cached by the hot-water automation's existing `melcloud_status_cache`.

**Tech Stack:** Python 3.13, pytest (`--doctest-modules` is on - any doctest examples must actually run), dataclasses, no new dependencies.

**Spec:** Developed interactively in this conversation (no separate spec file) - background context in `docs/ASHP.md` (§6 interference detection, Open Question 1's T6R-vs-MELCloud decision) and `docs/ashp_deployment_and_testing_plan.md`. Confirmed with the project owner:
- Dashboard: a checkpoint at the battery-prediction eligibility cutoff (~21:00 today) is the primary/possibly only one needed; if a second is wanted, it's the window-start time (18:00).
- ASHP check: implement it, but the heat pump's response to a T6R call can legitimately lag by several minutes (anti-short-cycle timers, defrost, MELCloud's own polling cadence) - the check must tolerate a window before treating non-response as suspicious, not flag on the first mismatched poll.

## Global Constraints

- Every new function needs a test written and watched to fail first (TDD, per this repo's own conventions and CLAUDE.md).
- No new I/O layer/cache: Part 2 reads the *existing* `melcloud_status_cache.read_fresh_status()` (already populated every ~10 minutes by `hotwater_automation_core.py`'s force-heat check) rather than making a new MELCloud API call.
- This is advisory/diagnostic only, matching `interference_logic.py`'s explicit "efficiency/diagnostic signal, not a safety one" framing - Part 2 only logs a warning; it never changes what gets written to the ASHP.
- `ashp.enabled: false` in the live `config.yaml` today (per `docs/ashp_deployment_and_testing_plan.md`, ASHP deployment hasn't started) - this plan produces code covered entirely by unit/wiring tests; no real-hardware verification is expected or required before merging, consistent with how the rest of the ASHP decision logic was built and tested.

---

## Part 1 - Dashboard checkpoints derived from the hot-water battery-prediction logic

### Task 1: Promote the schedule loader and extract a shared eligibility-resolution helper

**Files:**
- Modify: `scripts/hotwater_automation_core.py` (currently has `_load_battery_daemon_time_ranges` at the line above `get_battery_prediction_to_deadline`, and inline eligibility-resolution logic inside `_run_force_heat_check_locked` just above `battery_prediction_eligibility_end_time = hour_float_to_time(...)`)
- Modify: `tests/scripts/test_hotwater_forced_discharge_window.py` (references `core._load_battery_daemon_time_ranges` three times)
- Test: `tests/scripts/test_hotwater_forced_discharge_window.py` (same file - add one new test, update three existing ones)

**Interfaces:**
- Produces: `hotwater_automation_core.load_battery_daemon_time_ranges() -> list[dict[str, Any]] | None` (renamed from `_load_battery_daemon_time_ranges`, same behavior)
- Produces: `hotwater_automation_core.resolve_battery_prediction_eligibility_end_hour(hw_config: dict[str, Any]) -> float` (new) - Task 2 in Part 2... no, Part 1's Task 2 (dashboard checkpoints) consumes this.

- [ ] **Step 1: Update the three existing tests to use the new (public) name**

In `tests/scripts/test_hotwater_forced_discharge_window.py`, rename every `core._load_battery_daemon_time_ranges` to `core.load_battery_daemon_time_ranges` (3 occurrences, in `test_load_battery_daemon_time_ranges_missing_file_returns_none`, `test_load_battery_daemon_time_ranges_malformed_json_returns_none`, `test_load_battery_daemon_time_ranges_returns_the_schedule_list`).

- [ ] **Step 2: Write the failing test for the new helper**

Add to `tests/scripts/test_hotwater_forced_discharge_window.py`, right after the `_load_battery_daemon_time_ranges` test block:

```python
# --- resolve_battery_prediction_eligibility_end_hour (I/O + pure, combined) -


def test_resolve_eligibility_end_hour_prefers_the_schedule_over_hw_config(tmp_path, monkeypatch):
    config_path = tmp_path / "battery_mode_daemon_config.json"
    config_path.write_text(
        json.dumps(
            {
                "schedule": {
                    "enabled": True,
                    "time_ranges": [
                        {"start_time": "22:00", "end_time": "23:30", "battery_mode": "FORCE_DISCHARGE"}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "get_battery_mode_daemon_config_path", lambda: str(config_path))
    hw_config = {
        "battery_prediction_deadline_hour": 23.5,
        "forced_discharge_start_hour": 22.5,  # stale - the schedule (22:00) must win
        "force_heat_max_duration_hours": 1.0,
        "legionella_max_cycle_duration_hours": 1.0,
    }

    assert core.resolve_battery_prediction_eligibility_end_hour(hw_config) == 21.0


def test_resolve_eligibility_end_hour_falls_back_to_hw_config_without_a_schedule(tmp_path, monkeypatch):
    missing_path = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(core, "get_battery_mode_daemon_config_path", lambda: str(missing_path))
    hw_config = {
        "battery_prediction_deadline_hour": 23.5,
        "forced_discharge_start_hour": 22.5,
        "force_heat_max_duration_hours": 1.0,
        "legionella_max_cycle_duration_hours": 1.0,
    }

    assert core.resolve_battery_prediction_eligibility_end_hour(hw_config) == 21.5
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `source venv/bin/activate && python3 -m pytest tests/scripts/test_hotwater_forced_discharge_window.py -k "resolve_eligibility_end_hour or load_battery_daemon_time_ranges" -v`
Expected: the two new tests FAIL with `AttributeError: ... has no attribute 'resolve_battery_prediction_eligibility_end_hour'`; the three renamed ones FAIL with `AttributeError: ... has no attribute 'load_battery_daemon_time_ranges'` until Step 4 below.

- [ ] **Step 4: Rename the loader and add the new helper in `scripts/hotwater_automation_core.py`**

Rename `_load_battery_daemon_time_ranges` to `load_battery_daemon_time_ranges` (drop the leading underscore; body unchanged - it already exists from this session's earlier work). Then add, directly after it:

```python
def resolve_battery_prediction_eligibility_end_hour(hw_config: dict[str, Any]) -> float:
    """battery_prediction_eligibility_end_hour, preferring the battery daemon's
    real schedule over hw_config's own (possibly stale) forced_discharge_start_hour.

    Single place both this module's own force-heat check and
    scripts/battery_evening_predictor.py's dashboard checkpoints resolve this
    from - previously duplicated inline here alone.
    """
    time_ranges = load_battery_daemon_time_ranges()
    derived_forced_discharge_start_hour = (
        derive_forced_discharge_start_hour(time_ranges) if time_ranges is not None else None
    )
    effective_hw_config = hw_config
    if derived_forced_discharge_start_hour is not None:
        effective_hw_config = {
            **hw_config,
            "forced_discharge_start_hour": derived_forced_discharge_start_hour,
        }
    return battery_prediction_eligibility_end_hour(effective_hw_config)
```

Then simplify the existing call site (currently just above `in_battery_prediction_window = is_in_offpeak_window(...)` in `_run_force_heat_check_locked`) from:

```python
        battery_daemon_time_ranges = _load_battery_daemon_time_ranges()
        derived_forced_discharge_start_hour = (
            derive_forced_discharge_start_hour(battery_daemon_time_ranges)
            if battery_daemon_time_ranges is not None
            else None
        )
        eligibility_hw_config = hw_config
        if derived_forced_discharge_start_hour is not None:
            eligibility_hw_config = {
                **hw_config,
                "forced_discharge_start_hour": derived_forced_discharge_start_hour,
            }
        battery_prediction_eligibility_end_time = hour_float_to_time(
            battery_prediction_eligibility_end_hour(eligibility_hw_config)
        )
```

to:

```python
        battery_prediction_eligibility_end_time = hour_float_to_time(
            resolve_battery_prediction_eligibility_end_hour(hw_config)
        )
```

- [ ] **Step 5: Run the full forced-discharge test file to verify everything passes**

Run: `source venv/bin/activate && python3 -m pytest tests/scripts/test_hotwater_forced_discharge_window.py -v`
Expected: all tests PASS (18 existing + 2 new = 20).

- [ ] **Step 6: Run the full test suite to check for regressions**

Run: `source venv/bin/activate && python3 -m pytest tests/ src/core_logic src/api_clients -q`
Expected: all tests PASS, same count as before plus 2.

- [ ] **Step 7: Commit**

```bash
git add scripts/hotwater_automation_core.py tests/scripts/test_hotwater_forced_discharge_window.py
git commit -m "refactor: promote battery-daemon schedule loader, extract eligibility resolver"
```

---

### Task 2: Derive the dashboard checkpoints from the actual decision logic

**Files:**
- Modify: `scripts/battery_evening_predictor.py` (`DASHBOARD_CHECKPOINT_TIMES` constant, `_compute_dashboard_checkpoints` function, and its one call site inside `run()`)
- Test: `tests/scripts/test_battery_evening_predictor_checkpoints.py` (full rewrite - the existing tests hardcode the old 18:00/20:00/22:00/23:30 checkpoints)

**Interfaces:**
- Consumes: `hotwater_automation_core.resolve_battery_prediction_eligibility_end_hour(hw_config)` and `hotwater_automation_core.DEFAULT_BATTERY_PREDICTION_WINDOW_START_HOUR` (from Task 1)
- Produces: `battery_evening_predictor._compute_dashboard_checkpoints(current_soc_percent, historical_records, now_local, min_sample_days, hw_config) -> list[dict[str, Any]]` (same return shape as before: each item has `time`, `label`, `priority`, `predicted_soc_percent`, `sample_count` - `src/dashboard/status_collector.py` and `src/dashboard/static_page.py` consume this generically and need no changes)

- [ ] **Step 1: Write the failing tests (full rewrite of the test file)**

Replace the entire contents of `tests/scripts/test_battery_evening_predictor_checkpoints.py` with:

```python
"""Tests for battery_evening_predictor.py's dashboard-only checkpoint predictions.

Checkpoints are derived from the same battery-prediction logic the force-heat
decision itself uses (hotwater_automation_core.resolve_battery_prediction_eligibility_end_hour
and DEFAULT_BATTERY_PREDICTION_WINDOW_START_HOUR) rather than hardcoded clock
times, confirmed with the project owner 2026-09-10: the dashboard should show
"will the battery-prediction path still fire today" (the eligibility cutoff),
not arbitrary round numbers.

Regression-safety note: these checkpoints are additive to the existing
predicted_soc_percent/computed_at fields hotwater_automation_core.py reads -
see test_hotwater_battery_soc_source.py for that consumer's own tests, which
this file must not need to touch.
"""

from __future__ import annotations

from datetime import datetime
from unittest import mock

import battery_evening_predictor as predictor
import hotwater_automation_core as core


def _historical_records_with_flat_drift(drift_percent: float) -> list[dict]:
    """Historical days where SoC at 18:00 is 70% and drifts by drift_percent per hour either side."""
    records = []
    for day in range(1, 10):
        for hour in range(17, 24):
            soc = 70.0 + drift_percent * (hour - 18)
            records.append({"timestamp": f"2026-06-{day:02d} {hour:02d}:00:00", "soc_percent": soc})
    return records


def _hw_config(**overrides) -> dict:
    base = {
        "battery_prediction_window_start_hour": 18.0,
        "battery_prediction_deadline_hour": 23.5,
        "forced_discharge_start_hour": 22.5,
        "force_heat_max_duration_hours": 1.0,
        "legionella_max_cycle_duration_hours": 1.0,
    }
    base.update(overrides)
    return base


def _no_schedule_file():
    """No battery_mode_daemon_config.json - resolve_battery_prediction_eligibility_end_hour
    falls back to hw_config's own forced_discharge_start_hour (22.5), giving an
    eligibility cutoff of 21:30 for the default _hw_config() above."""
    return mock.patch.object(core, "get_battery_mode_daemon_config_path", lambda: "/nonexistent/path.json")


def test_checkpoints_are_window_start_and_eligibility_end():
    now_local = datetime(2026, 6, 15, 12, 0)  # noon - both checkpoints still ahead
    records = _historical_records_with_flat_drift(-5.0)

    with _no_schedule_file():
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=_hw_config()
        )

    times = [c["time"] for c in checkpoints]
    assert times == ["18:00", "21:30"]


def test_eligibility_end_checkpoint_is_the_priority_one():
    now_local = datetime(2026, 6, 15, 12, 0)
    records = _historical_records_with_flat_drift(-5.0)

    with _no_schedule_file():
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=_hw_config()
        )

    priority_times = [c["time"] for c in checkpoints if c["priority"]]
    assert priority_times == ["21:30"]
    non_priority_times = [c["time"] for c in checkpoints if not c["priority"]]
    assert non_priority_times == ["18:00"]


def test_window_start_checkpoint_omitted_once_it_has_passed():
    now_local = datetime(2026, 6, 15, 19, 0)  # 18:00 has passed, 21:30 hasn't
    records = _historical_records_with_flat_drift(-5.0)

    with _no_schedule_file():
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=_hw_config()
        )

    assert [c["time"] for c in checkpoints] == ["21:30"]


def test_no_checkpoints_left_once_eligibility_end_has_passed():
    now_local = datetime(2026, 6, 15, 22, 0)  # past both 18:00 and 21:30
    records = _historical_records_with_flat_drift(-5.0)

    with _no_schedule_file():
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=_hw_config()
        )

    assert checkpoints == []


def test_eligibility_end_derived_from_the_real_schedule_when_available(tmp_path):
    """The battery daemon's real FORCE_DISCHARGE start (22:00) - not hw_config's
    stale forced_discharge_start_hour (22.5) - drives the checkpoint, exactly
    like the force-heat decision itself (see resolve_battery_prediction_eligibility_end_hour)."""
    config_path = tmp_path / "battery_mode_daemon_config.json"
    config_path.write_text(
        '{"schedule": {"enabled": true, "time_ranges": '
        '[{"start_time": "22:00", "end_time": "23:30", "battery_mode": "FORCE_DISCHARGE"}]}}',
        encoding="utf-8",
    )
    now_local = datetime(2026, 6, 15, 12, 0)
    records = _historical_records_with_flat_drift(-5.0)

    with mock.patch.object(core, "get_battery_mode_daemon_config_path", lambda: str(config_path)):
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=_hw_config()
        )

    assert [c["time"] for c in checkpoints] == ["18:00", "21:00"]


def test_checkpoint_prediction_applies_historical_drift():
    now_local = datetime(2026, 6, 15, 18, 0)
    records = _historical_records_with_flat_drift(-5.0)

    with _no_schedule_file():
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=_hw_config()
        )

    checkpoint = next(c for c in checkpoints if c["time"] == "21:30")
    assert checkpoint["predicted_soc_percent"] == 52.5  # 70 - 5*3.5h


def test_checkpoint_prediction_uses_fractional_current_time_not_truncated_hour():
    """Regression test (carried over from the fixed-checkpoint version): running
    a few minutes before the hour must not apply a too-large historical drift
    profile (see _compute_dashboard_checkpoints's own docstring).

    Historical data has readings at both 17:00 (soc 75) and 18:00 (soc 70).
    Run at 17:55, 5 minutes from the 18:00 checkpoint: the closest historical
    reading to "now" (17:55) is 18:00 (5 min away - 17:00 is 55 min away,
    outside predict_evening_soc's 15-minute match tolerance), so the correct
    drift is 70->70 = 0, giving 70.0.
    """
    now_local = datetime(2026, 6, 15, 17, 55)
    records = _historical_records_with_flat_drift(-5.0)

    with _no_schedule_file():
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=_hw_config()
        )

    checkpoint_18 = next(c for c in checkpoints if c["time"] == "18:00")
    assert checkpoint_18["predicted_soc_percent"] == 70.0


def test_window_start_checkpoint_omitted_when_it_is_not_before_eligibility_end():
    """A degenerate config where the eligibility cutoff falls at/before the
    window start (e.g. a very early forced_discharge_start_hour) must not show
    the window-start checkpoint out of order - only the (always-present)
    eligibility checkpoint is shown."""
    now_local = datetime(2026, 6, 15, 6, 0)
    records = _historical_records_with_flat_drift(-5.0)
    hw_config = _hw_config(forced_discharge_start_hour=18.5)  # eligibility_end = 17.5, before window_start=18.0

    with _no_schedule_file():
        checkpoints = predictor._compute_dashboard_checkpoints(
            70.0, records, now_local, min_sample_days=5, hw_config=hw_config
        )

    assert [c["time"] for c in checkpoints] == ["17:30"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source venv/bin/activate && python3 -m pytest tests/scripts/test_battery_evening_predictor_checkpoints.py -v`
Expected: FAIL - `_compute_dashboard_checkpoints() got an unexpected keyword argument 'hw_config'` (old signature still in place).

- [ ] **Step 3: Replace `DASHBOARD_CHECKPOINT_TIMES` and `_compute_dashboard_checkpoints` in `scripts/battery_evening_predictor.py`**

Remove the `DASHBOARD_CHECKPOINT_TIMES` constant and its comment block entirely, and add this import (alongside the existing `from hotwater_automation_core import get_config_path`-style cross-script imports already used elsewhere in this codebase, e.g. `solax_realtime_logger.py`):

```python
from hotwater_automation_core import (
    DEFAULT_BATTERY_PREDICTION_WINDOW_START_HOUR,
    resolve_battery_prediction_eligibility_end_hour,
)
```

Replace `_compute_dashboard_checkpoints` with:

```python
def _compute_dashboard_checkpoints(
    current_soc_percent: float,
    historical_records: list[dict[str, Any]],
    now_local: datetime,
    min_sample_days: int,
    hw_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Predict SoC at the two clock times the force-heat decision itself cares about.

    Confirmed with the project owner 2026-09-10: rather than arbitrary round
    numbers, the dashboard should show the battery-prediction window's own
    start (hw_config's battery_prediction_window_start_hour, default 18:00)
    and its eligibility cutoff (resolve_battery_prediction_eligibility_end_hour -
    the same schedule-derived value the force-heat check gates on, e.g. 21:00
    once forced_discharge_start_hour is resolved from the real battery daemon
    schedule) - "will the battery-prediction path still fire today" is the
    actually decision-relevant number, not a fixed clock time. The eligibility
    checkpoint is always shown (when not yet passed); the window-start one only
    when it falls strictly before it - a degenerate config (an unusually early
    forced_discharge_start_hour) must never show them out of order.

    Reuses predict_evening_soc() unmodified, anchored to the exact current
    time (fractional trigger_hour - not truncated to the hour, which would
    apply a too-large historical drift profile whenever this runs off the
    hour) with a horizon computed to land exactly on each checkpoint's clock
    time. A checkpoint already passed today is omitted rather than
    predicting backwards.
    """
    window_start_hour = hw_config.get(
        "battery_prediction_window_start_hour", DEFAULT_BATTERY_PREDICTION_WINDOW_START_HOUR
    )
    eligibility_end_hour = resolve_battery_prediction_eligibility_end_hour(hw_config)

    checkpoint_specs = []
    if window_start_hour < eligibility_end_hour:
        checkpoint_specs.append((window_start_hour, "Battery-prediction window opens", False))
    checkpoint_specs.append((eligibility_end_hour, "Last chance to heat from stored solar", True))

    now_hour_float = now_local.hour + now_local.minute / 60.0
    checkpoints = []
    for target_hour_float, label, is_priority in checkpoint_specs:
        if target_hour_float <= now_hour_float:
            continue

        result = predict_evening_soc(
            current_soc_percent=current_soc_percent,
            historical_records=historical_records,
            trigger_hour=now_hour_float,
            horizon_hours=target_hour_float - now_hour_float,
            reference_day_of_year=now_local.timetuple().tm_yday,
            min_sample_days=min_sample_days,
        )
        checkpoints.append(
            {
                "time": hour_float_to_time_str(target_hour_float),
                "label": label,
                "priority": is_priority,
                "predicted_soc_percent": result.predicted_soc_percent,
                "sample_count": result.sample_count,
            }
        )
    return checkpoints
```

`hour_float_to_time_str` doesn't exist yet - add this small helper right above `_compute_dashboard_checkpoints` (fractional hours like 21.5 need "HH:MM" formatting, and `hotwater_decision_logic.hour_float_to_time` returns a `time` object, not a string - reuse it rather than reimplementing the rounding):

```python
from src.core_logic.hotwater_decision_logic import hour_float_to_time


def hour_float_to_time_str(hour_float: float) -> str:
    """Format a fractional hour (e.g. 21.5) as "HH:MM" (e.g. "21:30")."""
    return hour_float_to_time(hour_float).strftime("%H:%M")
```

- [ ] **Step 4: Update the one call site in `run()`**

Change:

```python
        "dashboard_checkpoints": _compute_dashboard_checkpoints(
            current_soc_percent, historical_records, now_local, min_sample_days
        ),
```

to:

```python
        "dashboard_checkpoints": _compute_dashboard_checkpoints(
            current_soc_percent, historical_records, now_local, min_sample_days, hw_config
        ),
```

- [ ] **Step 5: Run the checkpoint tests to verify they pass**

Run: `source venv/bin/activate && python3 -m pytest tests/scripts/test_battery_evening_predictor_checkpoints.py -v`
Expected: all 9 tests PASS.

- [ ] **Step 6: Run the full test suite to check for regressions**

Run: `source venv/bin/activate && python3 -m pytest tests/ src/core_logic src/api_clients -q`
Expected: all tests PASS (watch particularly for `test_battery_evening_predictor_forecast.py` and anything importing `DASHBOARD_CHECKPOINT_TIMES` directly - grep for it first: `grep -rn DASHBOARD_CHECKPOINT_TIMES tests/` should return nothing once this task is done).

- [ ] **Step 7: Manually verify the dashboard renders correctly**

Run: `source venv/bin/activate && python3 scripts/battery_evening_predictor.py --quiet && cat config/battery_evening_prediction.json` (path from `get_battery_evening_prediction_path()`) and confirm `dashboard_checkpoints` has 1-2 entries with sensible `time`/`label` values reflecting the current config, not `18:00/20:00/22:00/23:30`. Start the dashboard server (`python3 scripts/dashboard_server.py`, per `scripts/home_automation_dashboard.service`) and check the "Battery Forecast" card renders the star (⭐) on the eligibility-cutoff row and no more than two rows.

- [ ] **Step 8: Commit**

```bash
git add scripts/battery_evening_predictor.py tests/scripts/test_battery_evening_predictor_checkpoints.py
git commit -m "feat: derive dashboard battery checkpoints from the force-heat decision logic"
```

---

## Part 2 - ASHP/MELCloud compressor-response corroboration check

### Task 3: `ashp_response_check_logic.py` - pure state machine

**Files:**
- Create: `src/core_logic/ashp_response_check_logic.py`
- Test: `tests/core_logic/test_ashp_response_check_logic.py`

**Interfaces:**
- Produces: `AshpResponseCheckState` (dataclass: `active_since: datetime | None = None`)
- Produces: `AshpResponseVerdict` (dataclass: `status: str`, `reason: str`, `active_for_seconds: float | None = None`)
- Produces: `evaluate_ashp_response(state, *, ashp_active: bool, observed_status: str | None, now: datetime, response_window_minutes: float) -> tuple[AshpResponseCheckState, AshpResponseVerdict]`
- `status` values: `"not_active"`, `"settling"`, `"unknown"`, `"ok"`, `"busy_elsewhere"`, `"no_response_suspected"` (only the last is ever logged as a warning by Task 4)

- [ ] **Step 1: Write the failing tests**

Create `tests/core_logic/test_ashp_response_check_logic.py`:

```python
"""Unit tests for src/core_logic/ashp_response_check_logic.py.

Cross-references the T6R's ashp_active call against MELCloud's own device
status (already cached by hotwater_automation_core.py's force-heat check, via
src/api_clients/melcloud_status_cache.py - see melcloud_client.py's
HotWaterStatus enum, which despite its name already carries HEAT_ZONES: the
Ecodan's status field is device-wide, not tank-specific, confirmed by reading
pymelcloud's own atw_device.py).

The heat pump's response to a T6R call can legitimately lag by several
minutes (anti-short-cycle protection, defrost cycles, MELCloud's own ~10-15
minute cache freshness) - confirmed with the project owner 2026-09-10 this
must never flag on the first mismatched poll. This is advisory/diagnostic
only (mirrors interference_logic.py's "efficiency signal, not a safety one"
framing) - it never changes what gets written to the ASHP.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.core_logic.ashp_response_check_logic import (
    AshpResponseCheckState,
    evaluate_ashp_response,
)

NOW = datetime(2026, 1, 15, 20, 0, 0)


def test_not_active_resets_state_and_returns_not_active():
    state = AshpResponseCheckState(active_since=NOW - timedelta(minutes=30))

    new_state, verdict = evaluate_ashp_response(
        state, ashp_active=False, observed_status="idle", now=NOW, response_window_minutes=20.0
    )

    assert verdict.status == "not_active"
    assert new_state.active_since is None


def test_first_tick_of_new_activation_starts_the_clock_and_settles():
    state = AshpResponseCheckState()

    new_state, verdict = evaluate_ashp_response(
        state, ashp_active=True, observed_status="idle", now=NOW, response_window_minutes=20.0
    )

    assert verdict.status == "settling"
    assert verdict.active_for_seconds == 0.0
    assert new_state.active_since == NOW


def test_within_response_window_settles_regardless_of_status():
    state = AshpResponseCheckState(active_since=NOW - timedelta(minutes=5))

    new_state, verdict = evaluate_ashp_response(
        state, ashp_active=True, observed_status="idle", now=NOW, response_window_minutes=20.0
    )

    assert verdict.status == "settling"
    assert verdict.active_for_seconds == 300.0
    assert new_state.active_since == state.active_since  # clock keeps running, not reset


def test_heat_zones_confirms_ok_even_before_window_elapses():
    state = AshpResponseCheckState(active_since=NOW - timedelta(minutes=2))

    _, verdict = evaluate_ashp_response(
        state, ashp_active=True, observed_status="heat_zones", now=NOW, response_window_minutes=20.0
    )

    assert verdict.status == "ok"


def test_heat_zones_confirms_ok_after_window_elapses_too():
    state = AshpResponseCheckState(active_since=NOW - timedelta(minutes=45))

    _, verdict = evaluate_ashp_response(
        state, ashp_active=True, observed_status="heat_zones", now=NOW, response_window_minutes=20.0
    )

    assert verdict.status == "ok"


def test_missing_observed_status_returns_unknown_without_resetting_clock():
    state = AshpResponseCheckState(active_since=NOW - timedelta(minutes=45))

    new_state, verdict = evaluate_ashp_response(
        state, ashp_active=True, observed_status=None, now=NOW, response_window_minutes=20.0
    )

    assert verdict.status == "unknown"
    assert new_state.active_since == state.active_since


def test_unknown_literal_status_treated_same_as_missing():
    state = AshpResponseCheckState(active_since=NOW - timedelta(minutes=45))

    _, verdict = evaluate_ashp_response(
        state, ashp_active=True, observed_status="unknown", now=NOW, response_window_minutes=20.0
    )

    assert verdict.status == "unknown"


def test_busy_with_hot_water_after_window_is_not_flagged_as_no_response():
    state = AshpResponseCheckState(active_since=NOW - timedelta(minutes=45))

    _, verdict = evaluate_ashp_response(
        state, ashp_active=True, observed_status="heat_water", now=NOW, response_window_minutes=20.0
    )

    assert verdict.status == "busy_elsewhere"


def test_defrost_after_window_is_not_flagged_as_no_response():
    state = AshpResponseCheckState(active_since=NOW - timedelta(minutes=45))

    _, verdict = evaluate_ashp_response(
        state, ashp_active=True, observed_status="defrost", now=NOW, response_window_minutes=20.0
    )

    assert verdict.status == "busy_elsewhere"


def test_idle_after_window_flags_no_response_suspected():
    state = AshpResponseCheckState(active_since=NOW - timedelta(minutes=25))

    _, verdict = evaluate_ashp_response(
        state, ashp_active=True, observed_status="idle", now=NOW, response_window_minutes=20.0
    )

    assert verdict.status == "no_response_suspected"
    assert "25" in verdict.reason


def test_standby_after_window_flags_no_response_suspected():
    state = AshpResponseCheckState(active_since=NOW - timedelta(minutes=25))

    _, verdict = evaluate_ashp_response(
        state, ashp_active=True, observed_status="standby", now=NOW, response_window_minutes=20.0
    )

    assert verdict.status == "no_response_suspected"


def test_deactivating_then_reactivating_resets_the_clock():
    state = AshpResponseCheckState(active_since=NOW - timedelta(hours=2))

    off_state, _ = evaluate_ashp_response(
        state, ashp_active=False, observed_status="idle", now=NOW, response_window_minutes=20.0
    )
    reactivated_state, verdict = evaluate_ashp_response(
        off_state, ashp_active=True, observed_status="idle", now=NOW, response_window_minutes=20.0
    )

    assert verdict.status == "settling"
    assert reactivated_state.active_since == NOW
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source venv/bin/activate && python3 -m pytest tests/core_logic/test_ashp_response_check_logic.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.core_logic.ashp_response_check_logic'`.

- [ ] **Step 3: Write the implementation**

Create `src/core_logic/ashp_response_check_logic.py`:

```python
"""ASHP/MELCloud Response Corroboration - pure functions.

Cross-references the ASHP's T6R heat-call (docs/ASHP.md §1's confirmed
control path) against MELCloud's own device-wide status for the same Ecodan
unit (already fetched and cached by hotwater_automation_core.py's force-heat
check via src/api_clients/melcloud_status_cache.py - see that cache's
"status" field, sourced from melcloud_client.py's HotWaterStatus enum, which
despite its tank-focused name already carries HEAT_ZONES: pymelcloud's
AtwDevice.status property is device-wide ("what is the compressor currently
doing"), not zone-specific).

This answers a different question than src/core_logic/interference_logic.py:
interference_logic asks "is something ELSE overriding what we commanded on
the SAME device/characteristic" (T6R vs T6R); this asks "did our T6R command
actually make the physical unit respond at all" (T6R vs MELCloud, two
independent APIs for the one physical heat pump). Same "advisory only, not a
safety signal" stance either way (confirmed with the project owner
2026-09-09/10) - this never changes what gets written to the ASHP, only logs.

Response lag is expected and must not cause false positives (confirmed with
the project owner 2026-09-10): anti-short-cycle protection and defrost
cycles delay the compressor's own response by several minutes, and
MELCloud's own cache is only refreshed every ~10-15 minutes (see
melcloud_status_cache.DEFAULT_MAX_AGE_SECONDS) - so this never judges a
single poll, only sustained non-response across response_window_minutes.
STATUS_HEAT_WATER/STATUS_DEFROST/STATUS_LEGIONELLA are treated as "busy
elsewhere", not "not responding" - the compressor is genuinely active on a
legitimate competing job (the shared Ecodan unit also serves the hot water
tank), which this module can observe but not distinguish from "the ASHP call
will simply never be served" without also modelling that arbitration -
scoped out of this pass; a human reading the logged reason can tell the
difference.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

# MELCloud device statuses (melcloud_client.py's HotWaterStatus values) that
# mean the compressor is genuinely doing something else, not idling - not
# evidence the ASHP call is going unserved, just not yet served.
_BUSY_ELSEWHERE_STATUSES = frozenset({"heat_water", "defrost", "legionella"})

# Statuses that mean "not responding" once observed_status is neither
# HEAT_ZONES nor one of the above - "idle", "standby", "cool" all qualify by
# simply not being in either set above.


@dataclass
class AshpResponseCheckState:
    """Persisted across polls - one instance per ASHP install.

    Attributes:
        active_since: When the CURRENT continuous ashp_active=True call
            began, or None while ashp_active is False. Reset to None (not
            carried through) the moment ashp_active goes False - a fresh
            activation later starts a clean clock, never a continuation of
            an earlier one.

    """

    active_since: datetime | None = None


@dataclass
class AshpResponseVerdict:
    """The result of one evaluate_ashp_response() call. Advisory only.

    Attributes:
        status: "not_active" (ASHP isn't currently calling for heat) |
            "settling" (active, but either just started or still within
            response_window_minutes) | "unknown" (no fresh MELCloud status
            to judge by) | "ok" (MELCloud confirms HEAT_ZONES) |
            "busy_elsewhere" (compressor active on tank/defrost/legionella
            instead, past the window) | "no_response_suspected" (past the
            window, MELCloud shows neither heat_zones nor a busy-elsewhere
            status - the only status this module's caller should ever log).
        reason: Human-readable explanation, for logging.
        active_for_seconds: How long ashp_active has been continuously True,
            or None while not active.

    """

    status: str
    reason: str
    active_for_seconds: float | None = None


def evaluate_ashp_response(
    state: AshpResponseCheckState,
    *,
    ashp_active: bool,
    observed_status: str | None,
    now: datetime,
    response_window_minutes: float,
) -> tuple[AshpResponseCheckState, AshpResponseVerdict]:
    """Update response tracking against a fresh MELCloud observation.

    Call once per ASHP decision-check cycle (scripts/ashp_automation_core.py's
    run_ashp_decision_check), regardless of whether this cycle wrote anything -
    unlike interference_logic.evaluate, there is no "fresh command" special
    case here, since a genuine response can take longer than the 30s poll
    cadence in either scenario.
    """
    if not ashp_active:
        return AshpResponseCheckState(active_since=None), AshpResponseVerdict(
            "not_active", "ASHP is not currently calling for heat"
        )

    if state.active_since is None:
        new_state = replace(state, active_since=now)
        return new_state, AshpResponseVerdict(
            "settling",
            "Just started calling for heat - too early to judge a response",
            active_for_seconds=0.0,
        )

    active_for_seconds = (now - state.active_since).total_seconds()

    if observed_status is None or observed_status == "unknown":
        return state, AshpResponseVerdict(
            "unknown",
            "No fresh MELCloud status available to corroborate the T6R call",
            active_for_seconds=active_for_seconds,
        )

    if observed_status == "heat_zones":
        return state, AshpResponseVerdict(
            "ok",
            "MELCloud confirms the heat pump is actively heating zones",
            active_for_seconds=active_for_seconds,
        )

    if active_for_seconds < response_window_minutes * 60.0:
        return state, AshpResponseVerdict(
            "settling",
            f"MELCloud reports {observed_status!r} after "
            f"{active_for_seconds:.0f}s - still within the "
            f"{response_window_minutes:.0f}-minute response window",
            active_for_seconds=active_for_seconds,
        )

    if observed_status in _BUSY_ELSEWHERE_STATUSES:
        return state, AshpResponseVerdict(
            "busy_elsewhere",
            f"MELCloud reports {observed_status!r} - the heat pump is busy on a "
            f"legitimate competing job (tank heating/defrost), not necessarily "
            f"unresponsive",
            active_for_seconds=active_for_seconds,
        )

    return state, AshpResponseVerdict(
        "no_response_suspected",
        f"T6R has been calling for heat continuously for "
        f"{active_for_seconds / 60.0:.0f} minutes but MELCloud still reports "
        f"{observed_status!r} - the heat pump may not be responding",
        active_for_seconds=active_for_seconds,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `source venv/bin/activate && python3 -m pytest tests/core_logic/test_ashp_response_check_logic.py -v`
Expected: all 12 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/core_logic/ashp_response_check_logic.py tests/core_logic/test_ashp_response_check_logic.py
git commit -m "feat: add ASHP/MELCloud response corroboration decision logic"
```

---

### Task 4: Wire the response check into `ashp_automation_core.py`

**Files:**
- Modify: `scripts/ashp_automation_core.py` (imports, new constant, two new small `_ashp_response_state_from_dict`/`_ashp_response_state_to_dict` helpers mirroring the existing `_interference_state_*` ones, a new `_check_ashp_response` wrapper, and wiring into `run_ashp_decision_check`)
- Modify: `config.yaml` (`ashp:` section - one new key, next to `interference_dwell_minutes`)
- Modify: `src/config_manager/config_manager.py` (JSON schema - one new property next to `interference_dwell_minutes`)
- Test: `tests/scripts/test_ashp_automation_core.py`

**Interfaces:**
- Consumes: `src.core_logic.ashp_response_check_logic.{AshpResponseCheckState, evaluate_ashp_response}` (Task 3), `src.api_clients.melcloud_status_cache.read_fresh_status`
- Produces: `raw_state["ashp_response_check"]` persisted in `hvac_automation_state.json`, next to the existing `ashp_interference` key

- [ ] **Step 1: Write the failing tests**

Add to `tests/scripts/test_ashp_automation_core.py` (after the existing interference tests - reuses `_config`, `_patch_common`, `_FrozenDateTime`, `_active_ashp_state` already defined in that file):

```python
# --- ASHP/MELCloud response corroboration -----------------------------------


def test_response_check_settles_on_first_activation_tick(tmp_path):
    config = _config()
    now = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    state = {"ashp": _active_ashp_state(now)}  # no ashp_response_check yet - first tick
    patches = _patch_common(tmp_path, state=state)
    state_path = tmp_path / "hvac_automation_state.json"

    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = now

    with (
        mock.patch.object(core, "datetime", frozen),
        mock.patch.object(core, "set_ashp_heat_call", return_value=True),
        mock.patch.object(core, "set_airstage_power", return_value={"Playroom": True, "Landing": True}),
        mock.patch.object(core, "fetch_resideo_status", return_value={"mode": "heat", "target_temperature_c": 18.0}),
        mock.patch.object(core, "read_fresh_status", return_value={"status": "idle"}),
        mock.patch.object(core, "logger") as fake_logger,
    ):
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    fake_logger.warning.assert_not_called()
    saved = json.loads(state_path.read_text())
    assert saved["ashp_response_check"]["active_since"] == now.isoformat()


def test_response_check_flags_sustained_non_response(tmp_path):
    config = _config()
    config["ashp"]["response_window_minutes"] = 20.0
    now = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    state = {
        "ashp": _active_ashp_state(now),
        "ashp_response_check": {"active_since": (now - timedelta(minutes=25)).isoformat()},
    }
    patches = _patch_common(tmp_path, state=state)

    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = now

    with (
        mock.patch.object(core, "datetime", frozen),
        mock.patch.object(core, "set_ashp_heat_call", return_value=True),
        mock.patch.object(core, "set_airstage_power", return_value={"Playroom": True, "Landing": True}),
        mock.patch.object(core, "fetch_resideo_status", return_value={"mode": "heat", "target_temperature_c": 18.0}),
        mock.patch.object(core, "read_fresh_status", return_value={"status": "idle"}),
        mock.patch.object(core, "logger") as fake_logger,
    ):
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    fake_logger.warning.assert_called_once()
    assert "no_response_suspected" not in str(fake_logger.warning.call_args)  # human reason, not the raw status code
    assert "25" in str(fake_logger.warning.call_args)


def test_response_check_does_not_flag_when_busy_heating_the_tank(tmp_path):
    config = _config()
    config["ashp"]["response_window_minutes"] = 20.0
    now = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    state = {
        "ashp": _active_ashp_state(now),
        "ashp_response_check": {"active_since": (now - timedelta(minutes=25)).isoformat()},
    }
    patches = _patch_common(tmp_path, state=state)

    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = now

    with (
        mock.patch.object(core, "datetime", frozen),
        mock.patch.object(core, "set_ashp_heat_call", return_value=True),
        mock.patch.object(core, "set_airstage_power", return_value={"Playroom": True, "Landing": True}),
        mock.patch.object(core, "fetch_resideo_status", return_value={"mode": "heat", "target_temperature_c": 18.0}),
        mock.patch.object(core, "read_fresh_status", return_value={"status": "heat_water"}),
        mock.patch.object(core, "logger") as fake_logger,
    ):
        for p in patches:
            p.start()
        try:
            core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    fake_logger.warning.assert_not_called()


def test_response_check_deactivates_cleanly_when_ashp_turns_off(tmp_path):
    """Same deactivation setup as test_deactivation_powers_hvac_back_on (outdoor
    risen 5.0C -> 8.0C, past deactivation_margin_c=2.0, forecast staying above
    baseline too) - confirms the response-check clock resets to None the
    moment ashp_active goes False, not left dangling from the prior activation."""
    config = _config()
    now = datetime.now(UTC)
    state = {
        "ashp": {
            "ashp_active": True,
            "activated_at": (now - timedelta(hours=7)).isoformat(),
            "activation_baseline_outdoor_c": 5.0,
        },
        "ashp_response_check": {"active_since": (now - timedelta(hours=7)).isoformat()},
    }
    patches = _patch_common(
        tmp_path,
        state=state,
        statuses=[_master_status(outdoor_temperature_c=8.0), _mirror_status()],
        forecast_records=[{"temperature_2m": 9.0}, {"temperature_2m": 10.0}],
    )
    state_path = tmp_path / "hvac_automation_state.json"

    with (
        mock.patch.object(core, "set_ashp_off", return_value=True),
        mock.patch.object(core, "set_airstage_power", return_value={"Playroom": True, "Landing": True}),
        mock.patch.object(core, "_run_hvac_decision_check", return_value=0),
        mock.patch.object(core, "read_fresh_status", return_value={"status": "idle"}),
        mock.patch.object(core, "logger") as fake_logger,
    ):
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    fake_logger.warning.assert_not_called()
    saved = json.loads(state_path.read_text())
    assert saved["ashp_response_check"]["active_since"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source venv/bin/activate && python3 -m pytest tests/scripts/test_ashp_automation_core.py -k "response_check" -v`
Expected: FAIL - `AttributeError: <module 'ashp_automation_core' ...> does not have the attribute 'read_fresh_status'`.

- [ ] **Step 3: Add the config schema entry**

In `src/config_manager/config_manager.py`, change:

```python
                "interference_dwell_minutes": {"type": "number", "minimum": 5, "maximum": 240},
                "interference_min_reasserts": {"type": "integer", "minimum": 0, "maximum": 20},
            },
        },
```

(inside the `"ashp"` schema object) to:

```python
                "interference_dwell_minutes": {"type": "number", "minimum": 5, "maximum": 240},
                "interference_min_reasserts": {"type": "integer", "minimum": 0, "maximum": 20},
                "response_window_minutes": {"type": "number", "minimum": 5, "maximum": 120},
            },
        },
```

- [ ] **Step 4: Add the config.yaml entry**

In `config.yaml`'s `ashp:` section, after the `interference_min_reasserts` block (before the `CLAUDE CODE USAGE` section header), add:

```yaml
  # MELCloud response corroboration (new 2026-09-10) - cross-checks the T6R
  # heat-call against MELCloud's own device status for the same Ecodan unit
  # (already fetched/cached by hot water automation's force-heat check, no
  # extra MELCloud API calls). Diagnostic only, like interference detection
  # above - never changes what gets written, just logs a warning. The heat
  # pump's own anti-short-cycle/defrost timing plus MELCloud's own ~10-15
  # minute cache freshness both mean a genuine response can lag the T6R
  # command by several minutes - this is a starting estimate to refine once
  # observed against real hardware (see docs/ashp_deployment_and_testing_plan.md's
  # verify-empirically-first approach), not a value with any hardware
  # measurement behind it yet.
  response_window_minutes: 20.0
```

- [ ] **Step 5: Wire the check into `scripts/ashp_automation_core.py`**

Add to the imports (near the existing `from src.core_logic.interference_logic import (...)` block):

```python
from src.api_clients.melcloud_status_cache import read_fresh_status
from src.core_logic.ashp_response_check_logic import (
    AshpResponseCheckState,
    evaluate_ashp_response,
)
```

Add near `DEFAULT_INTERFERENCE_MIN_REASSERTS`:

```python
DEFAULT_ASHP_RESPONSE_WINDOW_MINUTES = 20.0
```

Add near `_interference_state_to_dict`:

```python
def _ashp_response_state_from_dict(raw: dict[str, Any]) -> AshpResponseCheckState:
    active_since = raw.get("active_since")
    try:
        active_since = datetime.fromisoformat(active_since) if active_since else None
    except (TypeError, ValueError):
        active_since = None
    return AshpResponseCheckState(active_since=active_since)


def _ashp_response_state_to_dict(state: AshpResponseCheckState) -> dict[str, Any]:
    return {"active_since": state.active_since.isoformat() if state.active_since else None}


def _check_ashp_response(
    ashp_config: dict[str, Any],
    decision: AshpDecision,
    response_state: AshpResponseCheckState,
    now: datetime,
) -> AshpResponseCheckState:
    """Corroborate the T6R heat-call against MELCloud's own device status.

    Read-only cross-check, logging only (docs/ASHP.md §6's "efficiency/
    diagnostic signal, not a safety one" precedent extends here) - see
    ashp_response_check_logic.evaluate_ashp_response's own docstring for why
    a genuine response can lag the T6R command by several minutes and must
    never be flagged on a single poll.
    """
    melcloud_status = read_fresh_status()
    observed_status = melcloud_status.get("status") if melcloud_status else None
    new_state, verdict = evaluate_ashp_response(
        response_state,
        ashp_active=decision.ashp_active,
        observed_status=observed_status,
        now=now,
        response_window_minutes=ashp_config.get(
            "response_window_minutes", DEFAULT_ASHP_RESPONSE_WINDOW_MINUTES
        ),
    )
    if verdict.status == "no_response_suspected":
        logger.warning("ASHP: %s", verdict.reason)
    return new_state
```

In `run_ashp_decision_check`, change:

```python
    with locked_state(timeout=DEFAULT_ASHP_LOCK_TIMEOUT_SECONDS) as raw_state:
        state = _ashp_state_from_dict(raw_state.get("ashp", {}))
        interference_state = _interference_state_from_dict(raw_state.get("ashp_interference", {}))
        context, room_temperature_c = _build_context(config, ashp_config, hvac_config, state)

        decision = determine_ashp_decision(context)
        logger.info("ASHP decision: %s", decision.reason)
        if not quiet:
            print(f"ASHP decision: {decision.reason}")

        raw_state["ashp"] = _ashp_state_to_dict(decision.state)

        if dry_run:
            if not quiet:
                print("(dry run - not applying)")
            return 0

        applied_ok, interference_state = _apply_ashp_decision(
            config, ashp_config, hvac_config, decision, interference_state, context.now, quiet=quiet
        )
        raw_state["ashp_interference"] = _interference_state_to_dict(interference_state)
```

to:

```python
    with locked_state(timeout=DEFAULT_ASHP_LOCK_TIMEOUT_SECONDS) as raw_state:
        state = _ashp_state_from_dict(raw_state.get("ashp", {}))
        interference_state = _interference_state_from_dict(raw_state.get("ashp_interference", {}))
        response_state = _ashp_response_state_from_dict(raw_state.get("ashp_response_check", {}))
        context, room_temperature_c = _build_context(config, ashp_config, hvac_config, state)

        decision = determine_ashp_decision(context)
        logger.info("ASHP decision: %s", decision.reason)
        if not quiet:
            print(f"ASHP decision: {decision.reason}")

        raw_state["ashp"] = _ashp_state_to_dict(decision.state)

        if dry_run:
            if not quiet:
                print("(dry run - not applying)")
            return 0

        applied_ok, interference_state = _apply_ashp_decision(
            config, ashp_config, hvac_config, decision, interference_state, context.now, quiet=quiet
        )
        raw_state["ashp_interference"] = _interference_state_to_dict(interference_state)
        response_state = _check_ashp_response(ashp_config, decision, response_state, context.now)
        raw_state["ashp_response_check"] = _ashp_response_state_to_dict(response_state)
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `source venv/bin/activate && python3 -m pytest tests/scripts/test_ashp_automation_core.py -v`
Expected: all tests PASS, including the 4 new ones.

- [ ] **Step 7: Run the full test suite to check for regressions**

Run: `source venv/bin/activate && python3 -m pytest tests/ src/core_logic src/api_clients -q`
Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add scripts/ashp_automation_core.py config.yaml src/config_manager/config_manager.py tests/scripts/test_ashp_automation_core.py
git commit -m "feat: cross-check ASHP T6R heat-calls against MELCloud's own device status"
```

---

## Notes for whoever executes this plan

- Part 1 and Part 2 are independent - either can be done first, or in parallel by different workers, since they touch entirely disjoint files.
- `response_window_minutes: 20.0` is a considered starting estimate (longer than MELCloud's own ~15-minute cache staleness, shorter than a full ASHP polling cycle's worth of noise), not a hardware-measured value - per `docs/ashp_deployment_and_testing_plan.md`'s own philosophy, expect to tune it once this runs against the real Ecodan unit.
- Neither part changes `src/dashboard/status_collector.py` or `src/dashboard/static_page.py` - both already consume their respective outputs generically (a list of checkpoint dicts; nothing dashboard-facing was added for Part 2, matching the existing interference-detection precedent of "logger.warning only, dashboard surfacing deferred").
