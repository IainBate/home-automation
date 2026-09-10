"""Tests for src/core_logic/interference_logic.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.core_logic.interference_logic import (
    ControlledAttributeState,
    evaluate,
    note_reasserted,
    record_verified_write,
)

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def test_nothing_commanded_yet_is_ok():
    state = ControlledAttributeState()
    new_state, verdict = evaluate(state, "heat", NOW, dwell_minutes=10, min_reasserts=1)

    assert verdict.status == "ok"
    assert new_state == state


def test_matching_observation_is_ok():
    state = record_verified_write(ControlledAttributeState(), 18.0, NOW)
    new_state, verdict = evaluate(state, 18.0, NOW, dwell_minutes=10, min_reasserts=1)

    assert verdict.status == "ok"
    assert new_state.diverged_since is None


def test_single_blip_is_settling_not_flagged():
    """A one-off mismatch must never be flagged - only sustained divergence."""
    state = record_verified_write(ControlledAttributeState(), 18.0, NOW)
    new_state, verdict = evaluate(state, 16.0, NOW, dwell_minutes=10, min_reasserts=1)

    assert verdict.status == "settling"
    assert new_state.diverged_since == NOW
    assert new_state.diverged_to == 16.0


def test_returning_to_commanded_value_clears_divergence():
    state = record_verified_write(ControlledAttributeState(), 18.0, NOW)
    state, _ = evaluate(state, 16.0, NOW, dwell_minutes=10, min_reasserts=1)
    state, verdict = evaluate(state, 18.0, NOW + timedelta(minutes=1), dwell_minutes=10, min_reasserts=1)

    assert verdict.status == "ok"
    assert state.diverged_since is None
    assert state.reassert_count == 0


def test_divergence_to_a_different_foreign_value_resets_clock():
    """Drifting between different values each poll is noise, not the same-actor signature."""
    state = record_verified_write(ControlledAttributeState(), 18.0, NOW)
    state, _ = evaluate(state, 16.0, NOW, dwell_minutes=10, min_reasserts=0)
    later = NOW + timedelta(minutes=20)
    state, verdict = evaluate(state, 20.0, later, dwell_minutes=10, min_reasserts=0)

    assert verdict.status == "settling"
    assert state.diverged_since == later
    assert state.diverged_to == 20.0


def test_sustained_divergence_without_reassert_stays_settling():
    state = record_verified_write(ControlledAttributeState(), 18.0, NOW)
    state, _ = evaluate(state, 16.0, NOW, dwell_minutes=10, min_reasserts=1)
    later = NOW + timedelta(minutes=15)
    state, verdict = evaluate(state, 16.0, later, dwell_minutes=10, min_reasserts=1)

    # dwell satisfied, but never reasserted -> not yet flagged
    assert verdict.status == "settling"


def test_sustained_divergence_with_reassert_is_flagged():
    state = record_verified_write(ControlledAttributeState(), 18.0, NOW)
    state, _ = evaluate(state, 16.0, NOW, dwell_minutes=10, min_reasserts=1)
    state = note_reasserted(state)
    later = NOW + timedelta(minutes=15)
    state, verdict = evaluate(state, 16.0, later, dwell_minutes=10, min_reasserts=1)

    assert verdict.status == "external_override_suspected"
    assert verdict.foreign_value == 16.0
    assert verdict.diverged_for_seconds == timedelta(minutes=15).total_seconds()


def test_dwell_not_yet_satisfied_stays_settling_even_with_reasserts():
    state = record_verified_write(ControlledAttributeState(), 18.0, NOW)
    state, _ = evaluate(state, 16.0, NOW, dwell_minutes=10, min_reasserts=1)
    state = note_reasserted(state)
    soon = NOW + timedelta(minutes=5)
    state, verdict = evaluate(state, 16.0, soon, dwell_minutes=10, min_reasserts=1)

    assert verdict.status == "settling"


def test_record_verified_write_resets_everything():
    state = record_verified_write(ControlledAttributeState(), 18.0, NOW)
    state, _ = evaluate(state, 16.0, NOW, dwell_minutes=10, min_reasserts=1)
    state = note_reasserted(state)

    fresh = record_verified_write(state, 20.0, NOW + timedelta(hours=1))

    assert fresh.commanded_value == 20.0
    assert fresh.diverged_since is None
    assert fresh.diverged_to is None
    assert fresh.reassert_count == 0


def test_note_reasserted_increments_counter():
    state = ControlledAttributeState(reassert_count=2)
    state = note_reasserted(state)
    assert state.reassert_count == 3
