"""Ohme EV Charger Charging Decision Logic.

Shared "is the car actually charging" logic, so the two automations that ask
that question agree on the answer. Used by:
- scripts/battery_mode_daemon.py, to decide whether to force-charge the
  house battery while the car draws power, and
- scripts/hotwater_automation_core.py, for the same signal in its
  force-heat decision.

Both use is_charging_above_threshold() plus
confirm_charging_over_consecutive_cycles(), so a brief current spike on
plug-in can't trigger either one.

Design Principles:
- Pure functions: No side effects, no API calls, testable
- Clear data contracts: Explicit input/output types using dataclasses
- Price units: GBP/kWh; callers working in pence must convert

Note: OhmeChargingContext and SlotChargingDecision below describe a
price-cap-driven per-slot planner that this repo does not currently have a
consumer for - nothing here builds or reads them. They are kept because
they document the intended shape of that decision, but they are not part of
any live path; the live path is the two threshold functions.
"""

from dataclasses import dataclass

from src.core_logic.battery_simulation.constants_and_models import BatteryMode


@dataclass
class OhmeChargingContext:
    """Context from Ohme charger status needed for charging decisions.

    This dataclass captures the essential state information from the Ohme charger
    that determines whether and when the car should charge.

    Attributes:
        plugged_in: Whether car is plugged into the charger
        smart_sync_enabled: True if charger mode is SMART_CHARGE (SSA-1)
            Maps to OhmeChargerMode.SMART_CHARGE from ohme_ev_client.py
        price_cap_gbp: Maximum electricity price (£/kWh) for charging, or None if no cap
            NOTE: This is in GBP/kWh. Daemon code uses pence/kWh and must convert:
            price_cap_gbp = price_cap_pence / 100.0
        active_charging_mode: Current charger mode (SSA-5 - future)
            Values: "smart_charge", "max_charge", "paused", "unknown"
            Maps to OhmeChargerMode enum values
        max_charge_finish_time_ms: Unix timestamp (ms) for timed max charge, or None (SSA-5 - future)
            When set, indicates user has requested "charge until time X"

    """

    plugged_in: bool
    smart_sync_enabled: bool  # True if mode is SMART_CHARGE
    price_cap_gbp: float | None  # None if no cap set, GBP/kWh if set

    # SSA-5 fields (Max Charge support - future functionality)
    active_charging_mode: str  # "smart_charge", "max_charge", "paused", etc.
    max_charge_finish_time_ms: int | None = None  # Timestamp ms if timed max charge active


@dataclass
class SlotChargingDecision:
    """Result of charging decision for a single optimization slot.

    This represents the outcome of evaluating whether the car should charge
    during a specific 30-minute time slot.

    Attributes:
        should_charge: True if car should charge in this slot
        mode_override: BatteryMode to override to (MANUAL_STOP), or None if no override needed
            When price cap is set and price is below cap, SELF_USE slots are overridden
            to MANUAL_STOP to allow car charging while battery holds charge
        demand_adjustment_kw: Additional household load (kW) from car charging
            = charger_demand_kw if should_charge, 0.0 otherwise
        reason: Human-readable explanation of the decision
            Used for logging (daemon) and optimization_reason (optimizer)

    """

    should_charge: bool
    mode_override: BatteryMode | None  # MANUAL_STOP if daemon would override, None otherwise
    demand_adjustment_kw: float  # Value from config if charging, 0.0 otherwise
    reason: str  # Human-readable explanation


def is_charging_above_threshold(power_watts: float, threshold_watts: float) -> bool:
    """Whether a single power reading counts as "the car is charging".

    A raw, single-reading signal - see confirm_charging_over_consecutive_cycles
    for the debounce that turns this into a stable "confirmed charging" state.

    Examples:
        >>> is_charging_above_threshold(600, 500)
        True
        >>> is_charging_above_threshold(400, 500)
        False
        >>> is_charging_above_threshold(500, 500)
        False

    """
    return power_watts > threshold_watts


def confirm_charging_over_consecutive_cycles(
    previous_confirmed_cycles: int, above_threshold_now: bool, *, required_cycles: int = 2
) -> tuple[int, bool]:
    """Debounce a per-cycle above-threshold reading into a confirmed charging state.

    Shared by battery_mode_daemon.py and hotwater_automation_core.py so both
    treat "the car is charging" identically: requiring required_cycles
    consecutive above-threshold readings before acting, so a brief transient
    (e.g. the moment of plugging in) doesn't trigger a mode change or force-
    heat on its own. Callers persist previous_confirmed_cycles between calls
    themselves - in memory for a long-lived daemon instance, or in a state
    file for anything that might run as separate one-shot invocations.

    Args:
        previous_confirmed_cycles: The consecutive above-threshold count
            going into this cycle (0 if not currently accumulating).
        above_threshold_now: Whether this cycle's reading is above threshold.
        required_cycles: How many consecutive above-threshold cycles are
            needed before charging counts as confirmed.

    Returns:
        (new_confirmed_cycles, confirmed) - new_confirmed_cycles is what the
        caller should persist for the next call; confirmed is True once it
        reaches required_cycles. A below-threshold reading resets the count
        to 0, so a break in charging requires starting the count over.

    Examples:
        >>> confirm_charging_over_consecutive_cycles(0, True)
        (1, False)
        >>> confirm_charging_over_consecutive_cycles(1, True)
        (2, True)
        >>> confirm_charging_over_consecutive_cycles(2, True)
        (3, True)
        >>> confirm_charging_over_consecutive_cycles(1, False)
        (0, False)

    """
    new_count = previous_confirmed_cycles + 1 if above_threshold_now else 0
    return new_count, new_count >= required_cycles


def determine_slot_charging_decision(
    context: OhmeChargingContext,
    battery_mode: BatteryMode,
    slot_price_gbp: float,
    charger_demand_kw: float,
) -> SlotChargingDecision:
    """Determine if car should charge in a slot based on battery mode and price.

    This is the core decision logic that replicates the daemon's behavior:
    - Without price cap: Charge only in FORCE_CHARGE/MANUAL_STOP slots (battery charging)
    - With price cap: Charge when price ≤ cap, override SELF_USE to MANUAL_STOP if needed

    The "cheap electricity" behavior described in the UI emerges naturally:
    - During cheap periods: Optimizer sets FORCE_CHARGE, this function sees FORCE_CHARGE → charge
    - During normal periods: Optimizer leaves SELF_USE, this function overrides → charge
    - During expensive periods: No override, no charging mode → don't charge

    Args:
        context: Ohme charger status and configuration
        battery_mode: Current battery mode for the slot (from optimization plan)
        slot_price_gbp: Import electricity price for the slot (£/kWh)
        charger_demand_kw: Power demand of the charger from config (e.g., 7.3 kW)

    Returns:
        SlotChargingDecision with should_charge, mode_override, demand, and reason

    Examples:
        >>> # No price cap, FORCE_CHARGE slot
        >>> context = OhmeChargingContext(
        ...     plugged_in=True,
        ...     smart_sync_enabled=True,
        ...     price_cap_gbp=None,
        ...     active_charging_mode="smart_charge",
        ... )
        >>> decision = determine_slot_charging_decision(
        ...     context, BatteryMode.FORCE_CHARGE, 0.15, 7.3
        ... )
        >>> decision.should_charge
        True
        >>> print(decision.mode_override)
        None

        >>> # Price cap set, SELF_USE slot, price below cap
        >>> context = OhmeChargingContext(
        ...     plugged_in=True,
        ...     smart_sync_enabled=True,
        ...     price_cap_gbp=0.20,
        ...     active_charging_mode="smart_charge",
        ... )
        >>> decision = determine_slot_charging_decision(
        ...     context, BatteryMode.SELF_USE, 0.15, 7.3
        ... )
        >>> decision.should_charge
        True
        >>> decision.mode_override
        <BatteryMode.MANUAL_STOP: 'MANUAL_STOP'>

    """
    # Guard clauses: Don't charge if car not plugged in or smart sync not enabled
    if not context.plugged_in:
        return SlotChargingDecision(
            should_charge=False,
            mode_override=None,
            demand_adjustment_kw=0.0,
            reason="Car not plugged in",
        )

    if not context.smart_sync_enabled:
        return SlotChargingDecision(
            should_charge=False,
            mode_override=None,
            demand_adjustment_kw=0.0,
            reason="Smart sync not enabled",
        )

    # Case 1: No price cap - only charge during battery charging slots
    if context.price_cap_gbp is None:
        should_charge = battery_mode in [BatteryMode.FORCE_CHARGE, BatteryMode.MANUAL_STOP]
        return SlotChargingDecision(
            should_charge=should_charge,
            mode_override=None,
            demand_adjustment_kw=charger_demand_kw if should_charge else 0.0,
            reason=(
                f"Charging in {battery_mode.value} slot (no price cap)"
                if should_charge
                else f"Not charging in {battery_mode.value} slot (no price cap)"
            ),
        )

    # Case 2: Price cap set - check price and battery mode
    if battery_mode == BatteryMode.SELF_USE and slot_price_gbp <= context.price_cap_gbp:
        # Override SELF_USE to MANUAL_STOP when price below cap
        # This allows car to charge while battery holds charge
        return SlotChargingDecision(
            should_charge=True,
            mode_override=BatteryMode.MANUAL_STOP,
            demand_adjustment_kw=charger_demand_kw,
            reason=(
                f"Price £{slot_price_gbp:.3f} <= cap £{context.price_cap_gbp:.3f}, "
                f"override SELF_USE to MANUAL_STOP for car charging"
            ),
        )

    if battery_mode in [BatteryMode.FORCE_CHARGE, BatteryMode.MANUAL_STOP]:
        # In charging modes, check if price is below cap
        should_charge = slot_price_gbp <= context.price_cap_gbp
        return SlotChargingDecision(
            should_charge=should_charge,
            mode_override=None,
            demand_adjustment_kw=charger_demand_kw if should_charge else 0.0,
            reason=(
                f"Price £{slot_price_gbp:.3f} <= cap £{context.price_cap_gbp:.3f}, "
                f"charging in {battery_mode.value} slot"
                if should_charge
                else f"Price £{slot_price_gbp:.3f} > cap £{context.price_cap_gbp:.3f}, "
                f"not charging in {battery_mode.value} slot"
            ),
        )

    # Case 3: Other modes (e.g., FORCE_DISCHARGE) - don't charge
    return SlotChargingDecision(
        should_charge=False,
        mode_override=None,
        demand_adjustment_kw=0.0,
        reason=f"Not charging in {battery_mode.value} slot (price cap set)",
    )
