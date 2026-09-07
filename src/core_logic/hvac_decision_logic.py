"""HVAC Automated Control Decision Logic - spec Phase 4.

Decides what the two Airstage units should be set to, given the room
temperature the T6R reports, the schedule's current house target, and the
automation's own persisted state. Playroom is the master unit whose target
temperature the control loop actually tunes; Landing mirrors its *mode* (a
hardware constraint - one shared outdoor unit) but holds its own fixed target.

Design Principles (mirrors hotwater_decision_logic.py):
- Pure function: No side effects, no API calls, testable
- Clear data contracts: Explicit input/output types using dataclasses

The decision is *declarative*: it describes the end state that should hold
(mode, targets, minimum-heat), with None meaning "no opinion, leave this
alone". Sequencing, write verification, retry and revert all live in the
daemon and airstage_client, not here - the device only accepts one parameter
per write and lies about whether writes applied (see
docs/hvac_thermostat_automation_plan.md §4.1), so "how to make it so" is
messy I/O that has no place in a pure function.

Two temperatures are in play throughout, and conflating them is the easiest
mistake to make here:
- ``house_target_c`` is what the *room* should reach, from the schedule. It
  is never written to any device.
- ``hvac_target_c`` is the Playroom unit's setpoint, which the loop nudges up
  and down in 0.5C steps trying to make the room actually reach
  house_target_c. On a cold day it may sit well above house_target_c; §8.2's
  drift cap bounds how far.

Deliberate design decisions recorded in the plan doc's §8, implemented here:
- §8.1: mode escalation is gated symmetrically - the warming direction
  requires hvac_target to be maxed out, mirroring the spec's explicit
  requirement in the cooling direction.
- §8.2: hvac_target is bounded to house_target +/- max_drift_c as well as by
  the mode's own limits, and "at the drift cap" is what gates mode escalation
  (rather than the mode's hardware limit, which in practice is never reached).
- §8.3: no human-override attribution. Live device state is re-read every
  check, so a human's change is simply the starting point for that check.
  *Any* observed mode change - the automation's own or a person's - starts
  the 30-minute post-mode-change suppression window.
- §8.5: strict debounce. A single contradicting sample resets a dwell timer.
- §8.6: startup_default_mode is a seed for a first-ever run, never a reset
  applied on restart.
- §8.7: a mode mismatch between the two units is corrected immediately,
  bypassing the normal 30/60-minute cadence.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

#: Normal cycle, coldest to warmest. "Minimum heat" is deliberately absent -
#: the spec uses it exclusively for Away mode and it bypasses all temperature
#: validation, so it is a separate override rather than a fourth cycle member
#: (plan doc §4, "Mode-cycle boundaries").
MODES_COLDEST_TO_WARMEST = ("cool", "dry", "heat")

ADJUSTMENT_STEP_C = 0.5
TEMPERATURE_DWELL_MINUTES = 30
MODE_DWELL_MINUTES = 60
POST_MODE_CHANGE_SUPPRESSION_MINUTES = 30

#: Spec Phase 4: "except when moving from heat to dry/cool - set target to 18C".
HEAT_TO_COOLER_TARGET_C = 18.0

OFF_MODE = "off"


@dataclass
class HvacState:
    """The automation's persisted state, round-tripped via hvac_automation_state.json.

    Every daemon in this repo treats its state file as authoritative across
    restarts; this one follows suit (§8.6), so a Pi reboot mid-winter does not
    yank a correctly-running heat mode back to the startup default.

    Attributes:
        hvac_target_c: The Playroom setpoint the loop is currently driving.
            None on a first-ever run, seeded from the schedule.
        house_target_c: The house target seen at the last check, used only to
            notice that the schedule has moved into a new period.
        below_target_since: When the room first went below house_target and
            has stayed below since, or None. Reset by any contradicting
            sample (§8.5).
        above_target_since: The same, for the room being above house_target.
        last_mode_change_at: When a mode change was last *observed*, from any
            source (§8.3). Starts the temperature-adjustment suppression window.
        last_target_change_at: When hvac_target was last changed. Resets the
            60-minute mode-change timer, per the spec.
        last_observed_mode: The mode read from the master unit at the previous
            check, so a change by any actor can be noticed without attributing it.
        away_active: Whether Away mode was active at the last check, so entry
            and exit can be detected as edges.

    """

    hvac_target_c: float | None = None
    house_target_c: float | None = None
    below_target_since: datetime | None = None
    above_target_since: datetime | None = None
    last_mode_change_at: datetime | None = None
    last_target_change_at: datetime | None = None
    last_observed_mode: str | None = None
    away_active: bool = False


@dataclass
class ModeTempLimits:
    """A mode's configurable target-temperature bounds (spec Phase 1's table)."""

    min_c: float
    max_c: float


@dataclass
class HvacDecisionContext:
    """Everything needed to decide what the units should be set to right now.

    Device state must be *live-read* rather than remembered - see §8.3. That
    is what makes human overrides work without any attribution logic: a
    person's change is simply where this check starts from.

    Attributes:
        now: Current time (timezone-aware).
        room_temperature_c: Room temperature from the T6R, or None if it could
            not be read this poll. None suspends the dwell-driven logic (there
            is nothing to compare against) but does not suspend schedule
            propagation, which needs no room reading.
        house_target_c: The schedule's target for right now, or None if the
            schedule does not cover this time of day (see
            hvac_schedule_logic.active_period_for) - in which case the
            automation makes no changes rather than inventing a target.
        playroom_mode/landing_mode: Live operating mode of each unit,
            lowercased ("off", "cool", "dry", "heat", ...).
        playroom_powered_on/landing_powered_on: Live power state. Only a human
            may power units on or off, except during Away entry.
        playroom_target_c: Playroom's live setpoint, or None if unreadable.
        away_mode_active: Whether Away mode is currently on.
        state: The automation's persisted state from the previous check.
        mode_temp_limits: Per-mode target bounds, keyed by mode name.
        max_drift_c: How far hvac_target may drift from house_target (§8.2).
        away_mode_target_c: Target forced on both units during Away.
        mirror_zone_fixed_target_c: Landing's fixed target outside Away.
        startup_default_mode: Seed mode for a first-ever run only (§8.6).

    """

    now: datetime
    room_temperature_c: float | None
    house_target_c: float | None
    playroom_mode: str
    landing_mode: str
    playroom_powered_on: bool
    landing_powered_on: bool
    playroom_target_c: float | None
    away_mode_active: bool
    state: HvacState
    mode_temp_limits: dict[str, ModeTempLimits]
    max_drift_c: float = 3.0
    away_mode_target_c: float = 10.0
    mirror_zone_fixed_target_c: float = 18.0
    startup_default_mode: str = "dry"


@dataclass
class HvacDecision:
    """The end state that should hold after this check.

    Every field defaults to "no opinion" - the daemon writes only what is set,
    and writes nothing at all for a decision that is entirely None/False.

    Attributes:
        target_mode: Mode both units should be in, or None to leave mode alone.
            Always applies to both units - mode is a whole-system property.
        playroom_target_c: Playroom's setpoint, or None to leave it alone.
        landing_target_c: Landing's setpoint, or None to leave it alone.
        minimum_heat: Minimum-heat flag both units should have, or None to
            leave it alone. Only ever set for Away entry/exit.
        power_on: True only for Away entry, the single case in the whole spec
            where the automation may power a unit on. Never True otherwise,
            and never used to power anything *off* - Away exit explicitly
            leaves units on regardless of their prior state.
        reason: Human-readable explanation, for logging.
        state: Updated state to persist.

    """

    reason: str
    state: HvacState
    target_mode: str | None = None
    playroom_target_c: float | None = None
    landing_target_c: float | None = None
    minimum_heat: bool | None = None
    power_on: bool = False


def next_warmer_mode(mode: str) -> str | None:
    """The next mode up the cool -> dry -> heat cycle, or None at the ceiling.

    Examples:
        >>> next_warmer_mode("cool")
        'dry'
        >>> next_warmer_mode("heat") is None
        True

    """
    if mode not in MODES_COLDEST_TO_WARMEST:
        return None
    index = MODES_COLDEST_TO_WARMEST.index(mode)
    if index == len(MODES_COLDEST_TO_WARMEST) - 1:
        return None
    return MODES_COLDEST_TO_WARMEST[index + 1]


def next_colder_mode(mode: str) -> str | None:
    """The next mode down the cool -> dry -> heat cycle, or None at the floor.

    Examples:
        >>> next_colder_mode("heat")
        'dry'
        >>> next_colder_mode("cool") is None
        True

    """
    if mode not in MODES_COLDEST_TO_WARMEST:
        return None
    index = MODES_COLDEST_TO_WARMEST.index(mode)
    if index == 0:
        return None
    return MODES_COLDEST_TO_WARMEST[index - 1]


def allowed_target_range(
    mode: str,
    house_target_c: float,
    mode_temp_limits: dict[str, ModeTempLimits],
    max_drift_c: float,
) -> tuple[float, float]:
    """The bounds hvac_target may occupy: the mode's own limits, tightened by the drift cap.

    The drift cap (§8.2) is what stops setpoint windup on a weather-limited
    day - without it, a cold snap ratchets the setpoint to the mode's ceiling
    and the house then overshoots badly once the weather improves.

    Examples:
        >>> limits = {"heat": ModeTempLimits(16.0, 30.0)}
        >>> allowed_target_range("heat", 21.0, limits, max_drift_c=3.0)
        (18.0, 24.0)
        >>> # The mode's own limits still win where they are tighter.
        >>> allowed_target_range("heat", 17.0, limits, max_drift_c=3.0)
        (16.0, 20.0)

    """
    limits = mode_temp_limits[mode]
    low = max(limits.min_c, house_target_c - max_drift_c)
    high = min(limits.max_c, house_target_c + max_drift_c)
    return low, high


def _dwell_satisfied(since: datetime | None, now: datetime, minutes: int) -> bool:
    """Whether a dwell condition has held continuously for at least `minutes`."""
    if since is None:
        return False
    return now - since >= timedelta(minutes=minutes)


def _update_dwell_timers(state: HvacState, room_c: float, house_target_c: float, now: datetime) -> None:
    """Advance or reset the below/above dwell timers, in place.

    Strict debounce (§8.5): a single sample that contradicts the condition
    resets its clock, rather than being tolerated within a hysteresis band.
    Chosen deliberately as the simpler reading of the spec; 0.5C adjustment
    steps are coarse enough that flapping exactly on the boundary is unlikely
    to be a real problem.
    """
    if room_c < house_target_c:
        state.below_target_since = state.below_target_since or now
        state.above_target_since = None
    elif room_c > house_target_c:
        state.above_target_since = state.above_target_since or now
        state.below_target_since = None
    else:
        state.below_target_since = None
        state.above_target_since = None


def _mode_dwell_start(dwell_since: datetime | None, last_target_change_at: datetime | None) -> datetime | None:
    """When the 60-minute mode-change clock effectively started.

    The spec says a target temperature change resets the mode-change timer, so
    the clock runs from whichever happened later: the room crossing the target,
    or the last setpoint change.
    """
    if dwell_since is None:
        return None
    if last_target_change_at is None:
        return dwell_since
    return max(dwell_since, last_target_change_at)


def determine_hvac_decision(context: HvacDecisionContext) -> HvacDecision:  # noqa: PLR0911, PLR0912
    """Decide what the two units should be set to right now.

    Evaluated in strict priority order: mode divergence between the units,
    then Away mode, then "are we even allowed to control these units", then
    schedule propagation, then mode changes, then temperature adjustment
    (the spec's "mode changes take priority over temperature changes").

    Args:
        context: Live device state, room/schedule temperatures, and persisted state.

    Returns:
        HvacDecision describing the end state that should hold, plus the state
        to persist. An all-None decision means "make no changes".

    """
    state = replace(context.state)
    now = context.now

    # Notice a mode change from any source (§8.3) before anything else reads
    # last_mode_change_at. A human touching a unit's remote and the automation's
    # own last write are deliberately indistinguishable here.
    observed_mode = context.playroom_mode
    if state.last_observed_mode is not None and observed_mode != state.last_observed_mode:
        state.last_mode_change_at = now
    state.last_observed_mode = observed_mode

    # --- Away mode (overrides all other logic) ------------------------------
    if context.away_mode_active:
        return _decide_away(context, state)

    if state.away_active:
        # Away exit: restore the schedule's target immediately rather than
        # leaving the house at 10C until the next periodic check (§8.4).
        state.away_active = False
        state.below_target_since = None
        state.above_target_since = None
        if context.house_target_c is None:
            return HvacDecision(
                reason="Away exited, but the schedule does not cover this time - "
                "turning minimum heat off and leaving temperatures alone",
                state=state,
                minimum_heat=False,
            )
        state.hvac_target_c = context.house_target_c
        state.house_target_c = context.house_target_c
        state.last_target_change_at = now
        return HvacDecision(
            reason=(
                f"Away exited - restoring the schedule's target of "
                f"{context.house_target_c}C immediately"
            ),
            state=state,
            minimum_heat=False,
            playroom_target_c=context.house_target_c,
            landing_target_c=context.mirror_zone_fixed_target_c,
        )

    # --- Mode consistency between the units (§8.7) --------------------------
    # Only meaningful for units that are both powered on: an off unit isn't
    # fighting anything, and correcting it would mean powering it on, which
    # only a human (or Away entry) may do.
    if (
        context.playroom_powered_on
        and context.landing_powered_on
        and context.playroom_mode != context.landing_mode
    ):
        return HvacDecision(
            reason=(
                f"Units disagree on mode (Playroom {context.playroom_mode!r}, "
                f"Landing {context.landing_mode!r}) - correcting immediately to the "
                f"master unit's mode, bypassing the normal cadence"
            ),
            state=state,
            target_mode=context.playroom_mode,
        )

    # --- Only controlled when powered on ------------------------------------
    if not context.playroom_powered_on:
        return HvacDecision(
            reason="Playroom is powered off - only a human may power units on",
            state=state,
        )

    if context.house_target_c is None:
        return HvacDecision(
            reason="Schedule does not cover this time of day - making no changes",
            state=state,
        )

    # --- Schedule propagation -----------------------------------------------
    # A new schedule period resets the setpoint to its target, which is also
    # what seeds hvac_target on a first-ever run.
    if state.house_target_c != context.house_target_c or state.hvac_target_c is None:
        state.house_target_c = context.house_target_c
        state.hvac_target_c = context.house_target_c
        state.last_target_change_at = now
        state.below_target_since = None
        state.above_target_since = None
        return HvacDecision(
            reason=(
                f"Schedule moved to a {context.house_target_c}C period - "
                f"propagating it to the units"
            ),
            state=state,
            playroom_target_c=context.house_target_c,
            landing_target_c=context.mirror_zone_fixed_target_c,
        )

    if context.room_temperature_c is None:
        return HvacDecision(
            reason="Room temperature unavailable this poll - making no changes",
            state=state,
        )

    if context.playroom_mode not in MODES_COLDEST_TO_WARMEST:
        return HvacDecision(
            reason=(
                f"Playroom is in {context.playroom_mode!r}, which is outside the "
                f"cool/dry/heat cycle - leaving it to a human"
            ),
            state=state,
        )

    _update_dwell_timers(state, context.room_temperature_c, context.house_target_c, now)

    low, high = allowed_target_range(
        context.playroom_mode, context.house_target_c, context.mode_temp_limits, context.max_drift_c
    )
    current_target = state.hvac_target_c

    # --- Mode change (checked every 60 min; takes priority) -----------------
    mode_decision = _decide_mode_change(context, state, low, high, current_target)
    if mode_decision is not None:
        return mode_decision

    # --- Temperature adjustment (every 30 min) ------------------------------
    return _decide_temperature_adjustment(context, state, low, high, current_target)


def _decide_away(context: HvacDecisionContext, state: HvacState) -> HvacDecision:
    """Away mode: both units to minimum heat at the away target, powered on.

    Asserted on entry and re-asserted only if the invariant has drifted, so a
    steady Away period isn't rewriting the same values every 30 minutes.
    """
    entering = not state.away_active
    state.away_active = True
    state.below_target_since = None
    state.above_target_since = None

    units_off = not (context.playroom_powered_on and context.landing_powered_on)
    target_wrong = context.playroom_target_c != context.away_mode_target_c
    modes_disagree = context.playroom_mode != context.landing_mode

    if not entering and not units_off and not target_wrong and not modes_disagree:
        return HvacDecision(reason="Away mode active and already applied", state=state)

    state.hvac_target_c = context.away_mode_target_c
    state.last_target_change_at = context.now
    reason = (
        "Away mode entered"
        if entering
        else "Away mode active but drifted - re-asserting minimum heat"
    )
    return HvacDecision(
        reason=f"{reason} - minimum heat at {context.away_mode_target_c}C on both units",
        state=state,
        minimum_heat=True,
        playroom_target_c=context.away_mode_target_c,
        landing_target_c=context.away_mode_target_c,
        # The one sanctioned power-on in the whole spec. Never powers off:
        # "On exit, leave units on regardless of their prior state."
        power_on=True,
    )


def _decide_mode_change(
    context: HvacDecisionContext,
    state: HvacState,
    low: float,
    high: float,
    current_target: float,
) -> HvacDecision | None:
    """Spec's 60-minute mode-change check, with §8.1's symmetric gate and §8.2's drift cap.

    Returns None if no mode change is due.
    """
    now = context.now
    mode = context.playroom_mode

    warming_since = _mode_dwell_start(state.below_target_since, state.last_target_change_at)
    cooling_since = _mode_dwell_start(state.above_target_since, state.last_target_change_at)

    # Warming: room below target, not already at the warmest mode, and the
    # setpoint has nothing left to give. The last clause is §8.1's addition -
    # the spec states it only for the cooling direction, leaving the warming
    # direction able to escalate while the setpoint was still nowhere near tried.
    if (
        _dwell_satisfied(warming_since, now, MODE_DWELL_MINUTES)
        and mode != "heat"
        and current_target >= high
    ):
        warmer = next_warmer_mode(mode)
        if warmer is not None:
            return _mode_change_decision(context, state, warmer, from_mode=mode, warming=True)

    # Cooling: the spec's own version already required the setpoint to be
    # maxed out in its effort ("HVAC target is already at its minimum").
    if (
        _dwell_satisfied(cooling_since, now, MODE_DWELL_MINUTES)
        and mode != "cool"
        and current_target <= low
    ):
        colder = next_colder_mode(mode)
        if colder is not None:
            return _mode_change_decision(context, state, colder, from_mode=mode, warming=False)

    return None


def _mode_change_decision(
    context: HvacDecisionContext,
    state: HvacState,
    new_mode: str,
    *,
    from_mode: str,
    warming: bool,
) -> HvacDecision:
    """Build the decision for a mode change, applying the spec's target rules."""
    now = context.now
    target = state.hvac_target_c

    # Spec: "retain the existing target temperature in the new mode, except
    # when moving from heat to dry/cool - set target to 18C".
    if from_mode == "heat" and new_mode in ("dry", "cool"):
        target = HEAT_TO_COOLER_TARGET_C

    # Spec Phase 1: "On a mode change, if the current target is below the new
    # mode's minimum, raise it to that minimum immediately."
    new_limits = context.mode_temp_limits[new_mode]
    if target < new_limits.min_c:
        target = new_limits.min_c

    state.hvac_target_c = target
    state.last_mode_change_at = now
    state.last_target_change_at = now
    state.below_target_since = None
    state.above_target_since = None

    direction = "warmer" if warming else "colder"
    return HvacDecision(
        reason=(
            f"Room has been {'below' if warming else 'above'} the "
            f"{context.house_target_c}C house target for {MODE_DWELL_MINUTES} min with the "
            f"setpoint already at its limit - switching to the next {direction} mode "
            f"({from_mode} -> {new_mode}) at {target}C"
        ),
        state=state,
        target_mode=new_mode,
        playroom_target_c=target,
        landing_target_c=context.mirror_zone_fixed_target_c,
    )


def _decide_temperature_adjustment(
    context: HvacDecisionContext,
    state: HvacState,
    low: float,
    high: float,
    current_target: float,
) -> HvacDecision:
    """Spec's 30-minute +/-0.5C nudge, bounded by the mode limits and drift cap."""
    now = context.now

    # Spec: "After a mode change, suppress temperature adjustments for 30
    # minutes (unless the target is below the new mode's minimum)." The
    # exception is already handled at the moment of the mode change, which
    # raises the target to the new mode's minimum immediately.
    if state.last_mode_change_at is not None and now - state.last_mode_change_at < timedelta(
        minutes=POST_MODE_CHANGE_SUPPRESSION_MINUTES
    ):
        return HvacDecision(
            reason=(
                f"Within {POST_MODE_CHANGE_SUPPRESSION_MINUTES} min of a mode change - "
                f"suppressing temperature adjustment"
            ),
            state=state,
        )

    if _dwell_satisfied(state.below_target_since, now, TEMPERATURE_DWELL_MINUTES):
        new_target = min(current_target + ADJUSTMENT_STEP_C, high)
        if new_target == current_target:
            return HvacDecision(
                reason=(
                    f"Room below target but the setpoint is already at its ceiling "
                    f"({high}C) - holding"
                ),
                state=state,
            )
        return _target_change_decision(context, state, new_target, "below", "Raising")

    if _dwell_satisfied(state.above_target_since, now, TEMPERATURE_DWELL_MINUTES):
        new_target = max(current_target - ADJUSTMENT_STEP_C, low)
        if new_target == current_target:
            return HvacDecision(
                reason=(
                    f"Room above target but the setpoint is already at its floor "
                    f"({low}C) - holding"
                ),
                state=state,
            )
        return _target_change_decision(context, state, new_target, "above", "Lowering")

    return HvacDecision(reason="No dwell condition met - making no changes", state=state)


def _target_change_decision(
    context: HvacDecisionContext,
    state: HvacState,
    new_target: float,
    direction: str,
    verb: str,
) -> HvacDecision:
    """Build the decision for a setpoint nudge, resetting the mode-change timer."""
    state.hvac_target_c = new_target
    # Spec: "A target temperature change resets the 60-minute mode-change timer."
    state.last_target_change_at = context.now
    return HvacDecision(
        reason=(
            f"Room has been {direction} the {context.house_target_c}C house target for "
            f"{TEMPERATURE_DWELL_MINUTES} min - {verb.lower()} the setpoint to {new_target}C"
        ),
        state=state,
        playroom_target_c=new_target,
        landing_target_c=context.mirror_zone_fixed_target_c,
    )
