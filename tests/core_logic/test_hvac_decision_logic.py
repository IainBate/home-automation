"""Unit tests for src/core_logic/hvac_decision_logic.py.

Covers the spec's Phase 4 rules plus every decision recorded in the plan
doc's §8, including the scenarios §7 calls out as required: windup, debounce,
mode ceilings, restart mid-cycle, Away entry/exit, and the two mode-consistency
verification conditions (§8.7).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.core_logic.hvac_decision_logic import (
    HvacDecisionContext,
    HvacState,
    ModeTempLimits,
    allowed_target_range,
    determine_hvac_decision,
    next_colder_mode,
    next_warmer_mode,
)

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

LIMITS = {
    "heat": ModeTempLimits(16.0, 30.0),
    "dry": ModeTempLimits(18.0, 30.0),
    "cool": ModeTempLimits(18.0, 30.0),
}


def _context(**overrides) -> HvacDecisionContext:
    """A steady, controllable baseline: both units on in heat, schedule at 21C."""
    defaults = {
        "now": NOW,
        "room_temperature_c": 21.0,
        "house_target_c": 21.0,
        "playroom_mode": "heat",
        "landing_mode": "heat",
        "playroom_powered_on": True,
        "landing_powered_on": True,
        "playroom_target_c": 21.0,
        "away_mode_active": False,
        "state": HvacState(
            hvac_target_c=21.0,
            house_target_c=21.0,
            last_observed_mode="heat",
        ),
        "mode_temp_limits": LIMITS,
    }
    return HvacDecisionContext(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# Mode cycle helpers
# ---------------------------------------------------------------------------


def test_mode_cycle_has_no_wraparound_at_either_end():
    assert next_warmer_mode("heat") is None
    assert next_colder_mode("cool") is None
    assert next_warmer_mode("cool") == "dry"
    assert next_colder_mode("heat") == "dry"


def test_minimum_heat_is_outside_the_normal_cycle():
    """It's Away-only and bypasses temperature validation, so it must never be
    reachable by ordinary mode escalation."""
    assert next_warmer_mode("minimum_heat") is None
    assert next_colder_mode("minimum_heat") is None


def test_drift_cap_tightens_the_mode_limits_but_never_widens_them():
    assert allowed_target_range("heat", 21.0, LIMITS, max_drift_c=3.0) == (18.0, 24.0)
    # Near the mode's own floor, the mode limit is tighter than the drift cap.
    assert allowed_target_range("heat", 17.0, LIMITS, max_drift_c=3.0) == (16.0, 20.0)


# ---------------------------------------------------------------------------
# §8.7 Mode consistency between units
# ---------------------------------------------------------------------------


def test_mode_divergence_is_corrected_immediately_bypassing_the_cadence():
    decision = determine_hvac_decision(
        _context(playroom_mode="heat", landing_mode="cool")
    )

    assert decision.target_mode == "heat"  # master unit wins
    assert "disagree on mode" in decision.reason


def test_mode_divergence_correction_does_not_wait_for_a_dwell_timer():
    """No dwell condition has been met at all here - divergence must still act."""
    decision = determine_hvac_decision(
        _context(
            playroom_mode="dry",
            landing_mode="heat",
            room_temperature_c=21.0,
            state=HvacState(hvac_target_c=21.0, house_target_c=21.0, last_observed_mode="dry"),
        )
    )

    assert decision.target_mode == "dry"


def test_a_powered_off_unit_is_not_treated_as_mode_divergence():
    """An off unit reads as mode "off" but isn't fighting anything, and
    correcting it would mean powering it on - which only a human may do."""
    decision = determine_hvac_decision(
        _context(playroom_mode="heat", landing_mode="off", landing_powered_on=False)
    )

    assert decision.target_mode is None
    assert decision.power_on is False


# ---------------------------------------------------------------------------
# Away mode
# ---------------------------------------------------------------------------


def test_away_entry_powers_units_on_and_forces_minimum_heat():
    decision = determine_hvac_decision(
        _context(
            away_mode_active=True,
            playroom_powered_on=False,
            landing_powered_on=False,
            playroom_mode="off",
            landing_mode="off",
        )
    )

    assert decision.power_on is True
    assert decision.minimum_heat is True
    assert decision.playroom_target_c == 10.0
    assert decision.landing_target_c == 10.0
    assert decision.state.away_active is True


def test_away_steady_state_does_not_rewrite_an_already_correct_state():
    decision = determine_hvac_decision(
        _context(
            away_mode_active=True,
            playroom_target_c=10.0,
            state=HvacState(hvac_target_c=10.0, away_active=True, last_observed_mode="heat"),
        )
    )

    assert decision.minimum_heat is None
    assert decision.playroom_target_c is None
    assert decision.power_on is False


def test_away_re_asserts_itself_if_the_units_have_drifted():
    decision = determine_hvac_decision(
        _context(
            away_mode_active=True,
            playroom_target_c=21.0,  # someone moved it
            state=HvacState(hvac_target_c=10.0, away_active=True, last_observed_mode="heat"),
        )
    )

    assert decision.minimum_heat is True
    assert decision.playroom_target_c == 10.0


def test_away_exit_restores_the_schedule_target_immediately_not_at_the_next_tick():
    """§8.4: leaving the house at 10C for up to an hour after someone gets home
    is clearly not the intent."""
    decision = determine_hvac_decision(
        _context(
            away_mode_active=False,
            house_target_c=21.0,
            state=HvacState(hvac_target_c=10.0, away_active=True, last_observed_mode="heat"),
        )
    )

    assert decision.minimum_heat is False
    assert decision.playroom_target_c == 21.0
    assert decision.landing_target_c == 18.0
    assert decision.state.away_active is False
    assert decision.state.hvac_target_c == 21.0


def test_away_exit_never_powers_units_off():
    """Spec: "On exit, leave units on regardless of their prior state"."""
    decision = determine_hvac_decision(
        _context(
            away_mode_active=False,
            state=HvacState(hvac_target_c=10.0, away_active=True, last_observed_mode="heat"),
        )
    )

    assert decision.power_on is False


# ---------------------------------------------------------------------------
# Preconditions for control
# ---------------------------------------------------------------------------


def test_powered_off_master_unit_is_left_alone():
    decision = determine_hvac_decision(
        _context(playroom_powered_on=False, playroom_mode="off", landing_powered_on=False, landing_mode="off")
    )

    assert decision.target_mode is None
    assert decision.playroom_target_c is None
    assert decision.power_on is False


def test_unscheduled_time_of_day_makes_no_changes():
    decision = determine_hvac_decision(_context(house_target_c=None))

    assert decision.playroom_target_c is None
    assert "does not cover" in decision.reason


def test_unreadable_room_temperature_suspends_the_dwell_logic_only():
    decision = determine_hvac_decision(_context(room_temperature_c=None))

    assert decision.playroom_target_c is None
    assert "unavailable" in decision.reason


def test_a_mode_outside_the_cycle_is_left_to_a_human():
    decision = determine_hvac_decision(
        _context(
            playroom_mode="fan",
            landing_mode="fan",
            state=HvacState(hvac_target_c=21.0, house_target_c=21.0, last_observed_mode="fan"),
        )
    )

    assert decision.target_mode is None
    assert "outside the cool/dry/heat cycle" in decision.reason


# ---------------------------------------------------------------------------
# Schedule propagation
# ---------------------------------------------------------------------------


def test_new_schedule_period_propagates_its_target_to_the_units():
    decision = determine_hvac_decision(
        _context(
            house_target_c=19.0,
            state=HvacState(hvac_target_c=21.0, house_target_c=21.0, last_observed_mode="heat"),
        )
    )

    assert decision.playroom_target_c == 19.0
    assert decision.landing_target_c == 18.0
    assert decision.state.hvac_target_c == 19.0


def test_first_ever_run_seeds_the_setpoint_from_the_schedule():
    decision = determine_hvac_decision(
        _context(state=HvacState(last_observed_mode="heat"))
    )

    assert decision.state.hvac_target_c == 21.0
    assert decision.playroom_target_c == 21.0


def test_landing_holds_its_fixed_target_regardless_of_playrooms():
    decision = determine_hvac_decision(
        _context(
            house_target_c=24.0,
            mirror_zone_fixed_target_c=18.0,
            state=HvacState(hvac_target_c=21.0, house_target_c=21.0, last_observed_mode="heat"),
        )
    )

    assert decision.playroom_target_c == 24.0
    assert decision.landing_target_c == 18.0


# ---------------------------------------------------------------------------
# Temperature adjustment (30 min) and §8.5 debounce
# ---------------------------------------------------------------------------


def test_setpoint_rises_after_thirty_minutes_below_target():
    decision = determine_hvac_decision(
        _context(
            room_temperature_c=20.0,
            state=HvacState(
                hvac_target_c=21.0,
                house_target_c=21.0,
                below_target_since=NOW - timedelta(minutes=30),
                last_observed_mode="heat",
            ),
        )
    )

    assert decision.playroom_target_c == 21.5
    assert decision.state.hvac_target_c == 21.5


def test_setpoint_falls_after_thirty_minutes_above_target():
    decision = determine_hvac_decision(
        _context(
            room_temperature_c=22.0,
            state=HvacState(
                hvac_target_c=21.0,
                house_target_c=21.0,
                above_target_since=NOW - timedelta(minutes=30),
                last_observed_mode="heat",
            ),
        )
    )

    assert decision.playroom_target_c == 20.5


def test_setpoint_does_not_move_before_the_dwell_completes():
    decision = determine_hvac_decision(
        _context(
            room_temperature_c=20.0,
            state=HvacState(
                hvac_target_c=21.0,
                house_target_c=21.0,
                below_target_since=NOW - timedelta(minutes=29),
                last_observed_mode="heat",
            ),
        )
    )

    assert decision.playroom_target_c is None


def test_strict_debounce_a_single_contrary_sample_resets_the_dwell_clock():
    """§8.5, decided deliberately: any wobble restarts the clock rather than
    being tolerated within a hysteresis band."""
    decision = determine_hvac_decision(
        _context(
            room_temperature_c=21.5,  # crossed back above target
            state=HvacState(
                hvac_target_c=21.0,
                house_target_c=21.0,
                below_target_since=NOW - timedelta(minutes=55),
                last_observed_mode="heat",
            ),
        )
    )

    assert decision.state.below_target_since is None
    assert decision.playroom_target_c is None


def test_room_exactly_at_target_clears_both_dwell_timers():
    decision = determine_hvac_decision(
        _context(
            room_temperature_c=21.0,
            state=HvacState(
                hvac_target_c=21.0,
                house_target_c=21.0,
                below_target_since=NOW - timedelta(minutes=55),
                above_target_since=None,
                last_observed_mode="heat",
            ),
        )
    )

    assert decision.state.below_target_since is None
    assert decision.state.above_target_since is None


# ---------------------------------------------------------------------------
# §8.2 Windup / drift cap
# ---------------------------------------------------------------------------


def test_setpoint_stops_at_the_drift_cap_on_a_weather_limited_day():
    """The room never catches up, so the nudge fires forever - the cap is what
    stops the setpoint running away to the mode ceiling and overshooting badly
    once the weather improves."""
    decision = determine_hvac_decision(
        _context(
            room_temperature_c=15.0,
            house_target_c=21.0,
            max_drift_c=3.0,
            state=HvacState(
                hvac_target_c=24.0,  # already at house_target + max_drift
                house_target_c=21.0,
                below_target_since=NOW - timedelta(hours=6),
                last_observed_mode="heat",
            ),
        )
    )

    assert decision.playroom_target_c is None
    assert decision.state.hvac_target_c == 24.0
    assert "ceiling" in decision.reason


def test_setpoint_is_clamped_to_the_cap_rather_than_overshooting_it():
    decision = determine_hvac_decision(
        _context(
            room_temperature_c=15.0,
            house_target_c=21.0,
            max_drift_c=3.0,
            state=HvacState(
                hvac_target_c=23.8,
                house_target_c=21.0,
                below_target_since=NOW - timedelta(minutes=30),
                last_observed_mode="heat",
            ),
        )
    )

    assert decision.playroom_target_c == 24.0


def test_cooling_direction_is_capped_symmetrically():
    decision = determine_hvac_decision(
        _context(
            playroom_mode="cool",
            landing_mode="cool",
            room_temperature_c=30.0,
            house_target_c=21.0,
            max_drift_c=3.0,
            state=HvacState(
                hvac_target_c=18.0,  # house_target - drift, and also cool's own floor
                house_target_c=21.0,
                above_target_since=NOW - timedelta(hours=6),
                last_observed_mode="cool",
            ),
        )
    )

    assert decision.playroom_target_c is None
    assert "floor" in decision.reason


# ---------------------------------------------------------------------------
# Mode escalation - §8.1's symmetric gate, and the ceilings/floors
# ---------------------------------------------------------------------------


def test_warming_escalation_requires_the_setpoint_to_be_maxed_first():
    """§8.1: read literally the spec would escalate dry -> heat after 60 min of a
    cold room even with the setpoint nowhere near tried. The explicit gate
    prevents that."""
    decision = determine_hvac_decision(
        _context(
            playroom_mode="dry",
            landing_mode="dry",
            room_temperature_c=18.0,
            house_target_c=21.0,
            state=HvacState(
                hvac_target_c=21.0,  # nowhere near the 24.0 cap
                house_target_c=21.0,
                below_target_since=NOW - timedelta(minutes=90),
                last_observed_mode="dry",
            ),
        )
    )

    assert decision.target_mode is None


def test_warming_escalation_fires_once_the_setpoint_is_capped():
    decision = determine_hvac_decision(
        _context(
            playroom_mode="dry",
            landing_mode="dry",
            room_temperature_c=18.0,
            house_target_c=21.0,
            state=HvacState(
                hvac_target_c=24.0,
                house_target_c=21.0,
                below_target_since=NOW - timedelta(minutes=90),
                last_observed_mode="dry",
            ),
        )
    )

    assert decision.target_mode == "heat"


def test_cooling_escalation_fires_when_the_setpoint_is_at_its_floor():
    decision = determine_hvac_decision(
        _context(
            playroom_mode="heat",
            landing_mode="heat",
            room_temperature_c=25.0,
            house_target_c=21.0,
            state=HvacState(
                hvac_target_c=18.0,
                house_target_c=21.0,
                above_target_since=NOW - timedelta(minutes=90),
                last_observed_mode="heat",
            ),
        )
    )

    assert decision.target_mode == "dry"


def test_never_escalates_past_heat_at_the_warm_ceiling():
    """Already in the warmest mode with the setpoint capped - must settle, not error."""
    decision = determine_hvac_decision(
        _context(
            playroom_mode="heat",
            landing_mode="heat",
            room_temperature_c=15.0,
            house_target_c=21.0,
            state=HvacState(
                hvac_target_c=24.0,
                house_target_c=21.0,
                below_target_since=NOW - timedelta(hours=6),
                last_observed_mode="heat",
            ),
        )
    )

    assert decision.target_mode is None
    assert decision.state.hvac_target_c == 24.0


def test_never_escalates_past_cool_at_the_cold_floor():
    decision = determine_hvac_decision(
        _context(
            playroom_mode="cool",
            landing_mode="cool",
            room_temperature_c=30.0,
            house_target_c=21.0,
            state=HvacState(
                hvac_target_c=18.0,
                house_target_c=21.0,
                above_target_since=NOW - timedelta(hours=6),
                last_observed_mode="cool",
            ),
        )
    )

    assert decision.target_mode is None


def test_heat_to_cooler_mode_resets_the_target_to_eighteen():
    """Spec: "except when moving from heat to dry/cool - set target to 18C"."""
    decision = determine_hvac_decision(
        _context(
            playroom_mode="heat",
            landing_mode="heat",
            room_temperature_c=26.0,
            house_target_c=21.0,
            state=HvacState(
                hvac_target_c=18.0,
                house_target_c=21.0,
                above_target_since=NOW - timedelta(minutes=90),
                last_observed_mode="heat",
            ),
        )
    )

    assert decision.target_mode == "dry"
    assert decision.playroom_target_c == 18.0


def test_mode_change_raises_a_target_below_the_new_modes_minimum():
    """Spec Phase 1: "On a mode change, if the current target is below the new
    mode's minimum, raise it to that minimum immediately."

    With the spec's default limits this rule is unreachable (heat -> dry/cool is
    already forced to 18C by its own rule, and cool -> dry share a floor), so
    this uses custom limits - mode_temp_limits is configurable per plan doc §5,
    and the rule has to hold for whatever the user configures.
    """
    limits = {
        "heat": ModeTempLimits(16.0, 30.0),
        "dry": ModeTempLimits(22.0, 30.0),  # deliberately above cool's floor
        "cool": ModeTempLimits(18.0, 30.0),
    }
    decision = determine_hvac_decision(
        _context(
            playroom_mode="cool",
            landing_mode="cool",
            room_temperature_c=15.0,
            house_target_c=19.0,
            max_drift_c=3.0,
            mode_temp_limits=limits,
            state=HvacState(
                hvac_target_c=22.0,  # at cool's ceiling (19 + 3), so escalation fires
                house_target_c=19.0,
                below_target_since=NOW - timedelta(minutes=90),
                last_observed_mode="cool",
            ),
        )
    )

    assert decision.target_mode == "dry"
    # Retained target would have been 22.0, which happens to equal dry's floor,
    # so push the starting point lower to prove the lift actually happens.
    assert decision.playroom_target_c == 22.0


def test_mode_change_lifts_a_retained_target_up_to_the_new_modes_floor():
    limits = {
        "heat": ModeTempLimits(16.0, 30.0),
        "dry": ModeTempLimits(24.0, 30.0),  # floor above anything cool would hold
        "cool": ModeTempLimits(18.0, 30.0),
    }
    decision = determine_hvac_decision(
        _context(
            playroom_mode="cool",
            landing_mode="cool",
            room_temperature_c=15.0,
            house_target_c=19.0,
            max_drift_c=3.0,
            mode_temp_limits=limits,
            state=HvacState(
                hvac_target_c=22.0,
                house_target_c=19.0,
                below_target_since=NOW - timedelta(minutes=90),
                last_observed_mode="cool",
            ),
        )
    )

    assert decision.target_mode == "dry"
    assert decision.playroom_target_c == 24.0  # lifted from the retained 22.0
    assert decision.state.hvac_target_c == 24.0


# ---------------------------------------------------------------------------
# Timers: suppression, mode-timer reset, and §8.3 human overrides
# ---------------------------------------------------------------------------


def test_temperature_adjustment_is_suppressed_for_thirty_minutes_after_a_mode_change():
    decision = determine_hvac_decision(
        _context(
            room_temperature_c=20.0,
            state=HvacState(
                hvac_target_c=21.0,
                house_target_c=21.0,
                below_target_since=NOW - timedelta(minutes=45),
                last_mode_change_at=NOW - timedelta(minutes=10),
                last_observed_mode="heat",
            ),
        )
    )

    assert decision.playroom_target_c is None
    assert "suppressing" in decision.reason


def test_suppression_expires_after_the_full_thirty_minutes():
    decision = determine_hvac_decision(
        _context(
            room_temperature_c=20.0,
            state=HvacState(
                hvac_target_c=21.0,
                house_target_c=21.0,
                below_target_since=NOW - timedelta(minutes=45),
                last_mode_change_at=NOW - timedelta(minutes=30),
                last_observed_mode="heat",
            ),
        )
    )

    assert decision.playroom_target_c == 21.5


def test_a_target_change_resets_the_sixty_minute_mode_timer():
    """The room has been below target for 90 min, which alone would escalate the
    mode - but the setpoint moved 10 minutes ago, so the mode clock restarted."""
    decision = determine_hvac_decision(
        _context(
            playroom_mode="dry",
            landing_mode="dry",
            room_temperature_c=18.0,
            house_target_c=21.0,
            state=HvacState(
                hvac_target_c=24.0,
                house_target_c=21.0,
                below_target_since=NOW - timedelta(minutes=90),
                last_target_change_at=NOW - timedelta(minutes=10),
                last_observed_mode="dry",
            ),
        )
    )

    assert decision.target_mode is None


def test_a_human_mode_change_starts_the_suppression_window_without_attribution():
    """§8.3: the daemon never asks "was this me or a person" - it just notices
    that the observed mode differs from last time and suppresses accordingly."""
    decision = determine_hvac_decision(
        _context(
            playroom_mode="dry",
            landing_mode="dry",
            room_temperature_c=20.0,
            state=HvacState(
                hvac_target_c=21.0,
                house_target_c=21.0,
                below_target_since=NOW - timedelta(minutes=45),
                last_observed_mode="heat",  # a human moved it to dry since
            ),
        )
    )

    assert decision.state.last_mode_change_at == NOW
    assert decision.playroom_target_c is None
    assert "suppressing" in decision.reason


def test_observed_mode_is_recorded_even_when_nothing_else_happens():
    decision = determine_hvac_decision(_context())

    assert decision.state.last_observed_mode == "heat"


# ---------------------------------------------------------------------------
# §8.6 Restart behaviour
# ---------------------------------------------------------------------------


def test_restart_mid_cycle_preserves_state_rather_than_resetting_to_defaults():
    """Dwell timers and the setpoint must survive a daemon restart - the state
    file is authoritative, and startup_default_mode is a first-run seed only."""
    mid_cycle = HvacState(
        hvac_target_c=23.5,
        house_target_c=21.0,
        below_target_since=NOW - timedelta(minutes=20),
        last_mode_change_at=NOW - timedelta(minutes=50),
        last_target_change_at=NOW - timedelta(minutes=20),
        last_observed_mode="heat",
    )

    decision = determine_hvac_decision(
        _context(room_temperature_c=20.0, state=mid_cycle, startup_default_mode="dry")
    )

    assert decision.state.hvac_target_c == 23.5
    assert decision.state.below_target_since == NOW - timedelta(minutes=20)
    assert decision.target_mode is None  # not yanked back to the "dry" seed
