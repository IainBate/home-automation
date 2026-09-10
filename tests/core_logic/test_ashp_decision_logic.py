"""Tests for src/core_logic/ashp_decision_logic.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.core_logic.ashp_decision_logic import (
    AshpDecisionContext,
    AshpState,
    _is_day_period,
    _would_flap_within_forecast,
    determine_ashp_decision,
)

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _context(**overrides):
    defaults = dict(
        now=NOW,
        state=AshpState(),
        room_temperature_c=17.0,
        house_target_c=20.0,
        playroom_target_c=25.0,
        outdoor_temperature_c=5.0,
        forecast_temps_c=[5.0, 5.0, 6.0],
        hvac_ceiling_c=25.0,
        sustained_deficit_hours=2.0,
        deactivation_margin_c=2.0,
        min_runtime_hours=6.0,
        min_rest_hours=6.0,
        day_start_minute=360,
        night_start_minute=1320,
        day_target_c=18.0,
        night_target_c=14.0,
        night_landing_target_c=18.0,
        night_playroom_target_c=25.0,
    )
    defaults.update(overrides)
    return AshpDecisionContext(**defaults)


# --- _is_day_period ----------------------------------------------------------


def test_is_day_period_boundaries():
    assert _is_day_period(360, 360, 1320) is True  # 06:00 exactly -> day
    assert _is_day_period(359, 360, 1320) is False  # 05:59 -> night
    assert _is_day_period(1319, 360, 1320) is True  # 21:59 -> day
    assert _is_day_period(1320, 360, 1320) is False  # 22:00 exactly -> night
    assert _is_day_period(0, 360, 1320) is False  # 00:00 -> night
    assert _is_day_period(1439, 360, 1320) is False  # 23:59 -> night


# --- _would_flap_within_forecast ---------------------------------------------


def test_flap_check_true_when_forecast_dips_to_baseline():
    assert _would_flap_within_forecast([10.0, 8.0, 5.0], 5.0) is True


def test_flap_check_false_when_forecast_stays_above_baseline():
    assert _would_flap_within_forecast([10.0, 9.0, 8.0], 5.0) is False


# --- Activation (ASHP_OFF -> ASHP_ON) -----------------------------------------


def test_activates_after_sustained_deficit():
    state = AshpState(below_target_since=NOW - timedelta(hours=2, minutes=1))
    decision = determine_ashp_decision(_context(state=state))

    assert decision.ashp_active is True
    assert decision.state.activated_at == NOW
    assert decision.state.activation_baseline_outdoor_c == 5.0
    assert decision.suppress_hvac_automation is True


def test_does_not_activate_before_dwell_completes():
    state = AshpState(below_target_since=NOW - timedelta(hours=1, minutes=59))
    decision = determine_ashp_decision(_context(state=state))

    assert decision.ashp_active is False


def test_timer_starts_fresh_when_all_conditions_hold():
    decision = determine_ashp_decision(_context(state=AshpState()))
    assert decision.state.below_target_since == NOW
    assert decision.ashp_active is False


def test_timer_resets_when_not_day():
    """23:00 is the ASHP-mode night period - trigger must not accumulate then."""
    now_night = NOW.replace(hour=23, minute=0)
    state = AshpState(below_target_since=now_night - timedelta(hours=3))
    decision = determine_ashp_decision(_context(now=now_night, state=state))

    assert decision.state.below_target_since is None
    assert decision.ashp_active is False


def test_timer_resets_when_playroom_not_at_ceiling():
    state = AshpState(below_target_since=NOW - timedelta(hours=3))
    decision = determine_ashp_decision(_context(state=state, playroom_target_c=20.0))

    assert decision.state.below_target_since is None


def test_timer_resets_when_room_temperature_unavailable():
    state = AshpState(below_target_since=NOW - timedelta(hours=3))
    decision = determine_ashp_decision(_context(state=state, room_temperature_c=None))

    assert decision.state.below_target_since is None


def test_timer_resets_when_room_not_actually_below_target():
    state = AshpState(below_target_since=NOW - timedelta(hours=3))
    decision = determine_ashp_decision(_context(state=state, room_temperature_c=21.0))

    assert decision.state.below_target_since is None


def test_activation_blocked_by_min_rest_guard():
    state = AshpState(
        below_target_since=NOW - timedelta(hours=3),
        deactivated_at=NOW - timedelta(hours=1),
    )
    decision = determine_ashp_decision(_context(state=state))

    assert decision.ashp_active is False


def test_activation_allowed_once_min_rest_satisfied():
    state = AshpState(
        below_target_since=NOW - timedelta(hours=3),
        deactivated_at=NOW - timedelta(hours=6, minutes=1),
    )
    decision = determine_ashp_decision(_context(state=state))

    assert decision.ashp_active is True


def test_first_ever_run_has_no_rest_guard_to_serve():
    state = AshpState(below_target_since=NOW - timedelta(hours=3), deactivated_at=None)
    decision = determine_ashp_decision(_context(state=state))

    assert decision.ashp_active is True


def test_activation_sets_day_schedule_immediately():
    state = AshpState(below_target_since=NOW - timedelta(hours=3))
    decision = determine_ashp_decision(_context(state=state))

    assert decision.ashp_target_c == 18.0
    assert decision.hvac_should_power_off is True
    assert decision.hvac_landing_target_c is None
    assert decision.hvac_playroom_target_c is None


# --- While ASHP_ON: day/night schedule ----------------------------------------


def test_day_period_schedule():
    state = AshpState(ashp_active=True, activated_at=NOW - timedelta(hours=1))
    decision = determine_ashp_decision(_context(now=NOW.replace(hour=10), state=state))

    assert decision.ashp_active is True
    assert decision.ashp_target_c == 18.0
    assert decision.hvac_should_power_off is True
    assert decision.hvac_landing_target_c is None
    assert decision.hvac_playroom_target_c is None
    assert decision.suppress_hvac_automation is True


def test_night_period_schedule():
    state = AshpState(ashp_active=True, activated_at=NOW - timedelta(hours=1))
    decision = determine_ashp_decision(_context(now=NOW.replace(hour=23), state=state))

    assert decision.ashp_active is True
    assert decision.ashp_target_c == 14.0
    assert decision.hvac_should_power_off is False
    assert decision.hvac_landing_target_c == 18.0
    assert decision.hvac_playroom_target_c == 25.0


def test_schedule_boundary_0559_is_night():
    state = AshpState(ashp_active=True, activated_at=NOW - timedelta(hours=8))
    decision = determine_ashp_decision(_context(now=NOW.replace(hour=5, minute=59), state=state))
    assert decision.ashp_target_c == 14.0


def test_schedule_boundary_0600_is_day():
    state = AshpState(ashp_active=True, activated_at=NOW - timedelta(hours=8))
    decision = determine_ashp_decision(_context(now=NOW.replace(hour=6, minute=0), state=state))
    assert decision.ashp_target_c == 18.0


def test_schedule_boundary_2159_is_day():
    state = AshpState(ashp_active=True, activated_at=NOW - timedelta(hours=8))
    decision = determine_ashp_decision(_context(now=NOW.replace(hour=21, minute=59), state=state))
    assert decision.ashp_target_c == 18.0


def test_schedule_boundary_2200_is_night():
    state = AshpState(ashp_active=True, activated_at=NOW - timedelta(hours=8))
    decision = determine_ashp_decision(_context(now=NOW.replace(hour=22, minute=0), state=state))
    assert decision.ashp_target_c == 14.0


def test_idempotent_reevaluation_same_period():
    """Re-evaluating with no state change gives the same targets - no unnecessary re-decisions."""
    state = AshpState(ashp_active=True, activated_at=NOW - timedelta(hours=8))
    d1 = determine_ashp_decision(_context(now=NOW.replace(hour=23), state=state))
    d2 = determine_ashp_decision(_context(now=NOW.replace(hour=23), state=d1.state))

    assert d1.ashp_target_c == d2.ashp_target_c
    assert d1.hvac_landing_target_c == d2.hvac_landing_target_c
    assert d1.hvac_playroom_target_c == d2.hvac_playroom_target_c


# --- Deactivation (ASHP_ON -> ASHP_OFF) ---------------------------------------


def _active_state(**overrides):
    defaults = dict(
        ashp_active=True,
        activated_at=NOW - timedelta(hours=7),
        activation_baseline_outdoor_c=5.0,
    )
    defaults.update(overrides)
    return AshpState(**defaults)


def test_deactivates_when_outdoor_rises_and_forecast_stays_warm():
    decision = determine_ashp_decision(
        _context(state=_active_state(), outdoor_temperature_c=7.0, forecast_temps_c=[8.0, 9.0])
    )

    assert decision.ashp_active is False
    assert decision.state.activated_at == (NOW - timedelta(hours=7))
    assert decision.state.deactivated_at == NOW
    assert decision.state.activation_baseline_outdoor_c is None
    assert decision.hvac_should_power_on is True


def test_normal_on_cycle_does_not_request_power_on():
    decision = determine_ashp_decision(
        _context(state=_active_state(), outdoor_temperature_c=5.0, forecast_temps_c=[5.0])
    )
    assert decision.ashp_active is True
    assert decision.hvac_should_power_on is False


def test_does_not_deactivate_below_margin():
    decision = determine_ashp_decision(
        _context(state=_active_state(), outdoor_temperature_c=6.5, forecast_temps_c=[8.0])
    )
    assert decision.ashp_active is True


def test_deactivation_blocked_by_min_runtime_guard():
    state = _active_state(activated_at=NOW - timedelta(hours=1))
    decision = determine_ashp_decision(
        _context(state=state, outdoor_temperature_c=10.0, forecast_temps_c=[10.0])
    )
    assert decision.ashp_active is True


def test_deactivation_allowed_once_min_runtime_satisfied():
    state = _active_state(activated_at=NOW - timedelta(hours=6, minutes=1))
    decision = determine_ashp_decision(
        _context(state=state, outdoor_temperature_c=10.0, forecast_temps_c=[10.0])
    )
    assert decision.ashp_active is False


def test_anti_flapping_holds_ashp_on():
    """A 2C outdoor temp increase WITH a forecasted drop back to baseline - must stay ON."""
    decision = determine_ashp_decision(
        _context(state=_active_state(), outdoor_temperature_c=7.5, forecast_temps_c=[6.0, 5.0, 4.0])
    )

    assert decision.ashp_active is True
    # still reapplies the current period's schedule while held on
    assert decision.ashp_target_c in (18.0, 14.0)


def test_stable_warm_forecast_turns_off():
    """A 2C outdoor temp increase WITH stable warm forecast - must turn OFF."""
    decision = determine_ashp_decision(
        _context(state=_active_state(), outdoor_temperature_c=7.5, forecast_temps_c=[8.0, 9.0, 10.0])
    )
    assert decision.ashp_active is False


def test_missing_outdoor_temperature_blocks_deactivation_fail_safe():
    decision = determine_ashp_decision(
        _context(state=_active_state(), outdoor_temperature_c=None, forecast_temps_c=[10.0])
    )
    assert decision.ashp_active is True


def test_missing_forecast_blocks_deactivation_fail_safe():
    decision = determine_ashp_decision(
        _context(state=_active_state(), outdoor_temperature_c=10.0, forecast_temps_c=None)
    )
    assert decision.ashp_active is True
