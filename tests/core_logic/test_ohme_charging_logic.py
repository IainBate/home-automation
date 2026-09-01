"""Unit tests for src/core_logic/ohme_charging_logic.py."""

from __future__ import annotations

from src.core_logic.battery_simulation.constants_and_models import BatteryMode
from src.core_logic.ohme_charging_logic import (
    OhmeChargingContext,
    confirm_charging_over_consecutive_cycles,
    determine_slot_charging_decision,
    is_charging_above_threshold,
)


def _context(**overrides):
    defaults = {
        "plugged_in": True,
        "smart_sync_enabled": True,
        "price_cap_gbp": None,
        "active_charging_mode": "smart_charge",
    }
    defaults.update(overrides)
    return OhmeChargingContext(**defaults)


def test_not_plugged_in_never_charges():
    decision = determine_slot_charging_decision(
        _context(plugged_in=False), BatteryMode.FORCE_CHARGE, 0.15, 7.3
    )
    assert decision.should_charge is False
    assert decision.mode_override is None


def test_smart_sync_disabled_never_charges():
    decision = determine_slot_charging_decision(
        _context(smart_sync_enabled=False), BatteryMode.FORCE_CHARGE, 0.15, 7.3
    )
    assert decision.should_charge is False


def test_no_price_cap_charges_in_force_charge_slot():
    decision = determine_slot_charging_decision(
        _context(price_cap_gbp=None), BatteryMode.FORCE_CHARGE, 0.15, 7.3
    )
    assert decision.should_charge is True
    assert decision.mode_override is None
    assert decision.demand_adjustment_kw == 7.3


def test_no_price_cap_does_not_charge_in_self_use_slot():
    decision = determine_slot_charging_decision(
        _context(price_cap_gbp=None), BatteryMode.SELF_USE, 0.15, 7.3
    )
    assert decision.should_charge is False
    assert decision.demand_adjustment_kw == 0.0


def test_price_cap_overrides_self_use_when_price_below_cap():
    decision = determine_slot_charging_decision(
        _context(price_cap_gbp=0.20), BatteryMode.SELF_USE, 0.15, 7.3
    )
    assert decision.should_charge is True
    assert decision.mode_override == BatteryMode.MANUAL_STOP


def test_price_cap_does_not_override_self_use_when_price_above_cap():
    decision = determine_slot_charging_decision(
        _context(price_cap_gbp=0.10), BatteryMode.SELF_USE, 0.15, 7.3
    )
    assert decision.should_charge is False
    assert decision.mode_override is None


def test_price_cap_blocks_force_charge_when_price_above_cap():
    decision = determine_slot_charging_decision(
        _context(price_cap_gbp=0.10), BatteryMode.FORCE_CHARGE, 0.15, 7.3
    )
    assert decision.should_charge is False
    assert decision.demand_adjustment_kw == 0.0


def test_other_battery_modes_never_charge():
    decision = determine_slot_charging_decision(
        _context(price_cap_gbp=0.20), BatteryMode.FORCE_DISCHARGE, 0.05, 7.3
    )
    assert decision.should_charge is False
    assert decision.mode_override is None


# --- is_charging_above_threshold / confirm_charging_over_consecutive_cycles ---


def test_is_charging_above_threshold():
    assert is_charging_above_threshold(600, 500) is True
    assert is_charging_above_threshold(400, 500) is False
    assert is_charging_above_threshold(500, 500) is False  # exactly at threshold, not above


def test_confirm_charging_requires_two_consecutive_cycles():
    count, confirmed = confirm_charging_over_consecutive_cycles(0, True)
    assert (count, confirmed) == (1, False)

    count, confirmed = confirm_charging_over_consecutive_cycles(count, True)
    assert (count, confirmed) == (2, True)

    # Stays confirmed on further consecutive cycles.
    count, confirmed = confirm_charging_over_consecutive_cycles(count, True)
    assert (count, confirmed) == (3, True)


def test_confirm_charging_resets_on_a_below_threshold_cycle():
    count, confirmed = confirm_charging_over_consecutive_cycles(1, False)
    assert (count, confirmed) == (0, False)


def test_confirm_charging_required_cycles_is_configurable():
    count, confirmed = confirm_charging_over_consecutive_cycles(0, True, required_cycles=1)
    assert (count, confirmed) == (1, True)
