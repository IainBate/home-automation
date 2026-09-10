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
