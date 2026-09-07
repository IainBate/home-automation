"""HVAC Automated Control Decision Logic - spec Phase 4.

Decides what the two Airstage units should be set to, given the room
temperature the T6R reports, the schedule's current comfort target(s), and
the automation's own persisted state. Playroom is the master unit whose
target temperature the control loop actually tunes; Landing mirrors its
*mode* (a hardware constraint - one shared outdoor unit) but holds its own
fixed target.

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

**Two comfort targets, not one (deviation decided 2026-09-07)**: the
original spec gave every schedule period a single ``house_target_c``. In
practice, running the units manually, ~20C feels right when cooling in
summer and ~18C feels right when heating in winter - one flat number doesn't
match comfort. ``heat_target_c``/``cool_target_c`` (see
hvac_schedule_logic.py, ``dry`` shares ``cool_target_c`` with ``cool``)
replace it. Season itself is never modelled - it falls out for free, since
which target is "active" simply follows whichever mode is actually running.

This means every place the old code compared room temperature against one
number now has to pick the *right* one of two, and mode changes crossing
between the heat family and the cool/dry family have to decide whether to
carry the old setpoint across or reset it. Four independent dwell timers
(rather than one below/above pair) track how long the room has continuously
been on each side of *each* target:

- ``below_heat_target_since`` / ``above_heat_target_since`` - vs
  ``heat_target_c``.
- ``below_cool_target_since`` / ``above_cool_target_since`` - vs
  ``cool_target_c``.

Two independent uses read these four timers, deliberately not the same one:

1. **Intra-mode adjustment** (the 30-minute +/-0.5C nudge) always compares
   against the *current* mode's own family target - heat mode nudges toward
   heat_target_c, dry/cool nudge toward cool_target_c. This is what makes a
   mode "happy" or not by its own standard.
2. **Mode escalation** (the 60-minute mode-change check) compares against
   the *destination* mode's own family target, not the current mode's. Dry
   only escalates into heat once the room is genuinely below heat_target_c
   (not merely below cool_target_c); heat only de-escalates back to dry once
   the room is genuinely above cool_target_c (not merely above
   heat_target_c). Both directions reuse the identical helper
   (``_below_dwell_since``/``_above_dwell_since``), just with the mode
   argument being the destination instead of the current mode - the same
   mechanism serves both uses, only which mode's timer it reads for differs.

Because heat_target_c is always strictly less than cool_target_c (enforced
by hvac_schedule_logic.parse_periods), rule 2 creates a genuine deadband: a
room between the two targets is "fine" by whichever mode is currently
running and never triggers escalation in either direction purely from being
in that band - only a *sustained* excursion past the *other* family's own
target does. See docs/hvac_thermostat_automation_plan.md's note on this
decision for the full reasoning against continual mode switching (the
existing 60-minute strict-debounce dwell and the drift-cap gate below both
still apply on top of this and were not changed).

On a mode change, the setpoint is *retained* when staying within the same
family (cool<->dry - the old, possibly drift-adjusted, value still means the
same thing) but *reset* to the new family's own target when crossing
families (dry<->heat - the spec's literal "moving from heat to dry/cool, set
target to 18C" rule, generalised from a hardcoded 18C to whichever
cool_target_c the active period actually configures).

Deliberate design decisions recorded in the plan doc's §8, implemented here
(unaffected by the heat/cool split above):
- §8.1: mode escalation is gated symmetrically - the warming direction
  requires hvac_target to be maxed out, mirroring the spec's explicit
  requirement in the cooling direction.
- §8.2: hvac_target is bounded to the current mode's family target +/-
  max_drift_c as well as by the mode's own hardware limits, and "at the
  drift cap" is what gates mode escalation.
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

from dataclasses import dataclass, replace
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

OFF_MODE = "off"


@dataclass
class HvacState:
    """The automation's persisted state, round-tripped via hvac_automation_state.json.

    Every daemon in this repo treats its state file as authoritative across
    restarts; this one follows suit (§8.6), so a Pi reboot mid-winter does not
    yank a correctly-running heat mode back to the startup default.

    Attributes:
        hvac_target_c: The Playroom setpoint the loop is currently driving.
            None on a first-ever run, seeded from the current mode's family
            target.
        heat_target_c: The schedule's heat_target_c seen at the last check,
            used only to notice the schedule has moved into a new period.
        cool_target_c: Same, for cool_target_c.
        below_heat_target_since: When the room first went below
            heat_target_c and has stayed below since, or None. Reset by any
            contradicting sample (§8.5).
        above_heat_target_since: The same, for above heat_target_c.
        below_cool_target_since: The same, for below cool_target_c.
        above_cool_target_since: The same, for above cool_target_c.
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
    heat_target_c: float | None = None
    cool_target_c: float | None = None
    below_heat_target_since: datetime | None = None
    above_heat_target_since: datetime | None = None
    below_cool_target_since: datetime | None = None
    above_cool_target_since: datetime | None = None
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
        heat_target_c: The schedule's target for right now while in heat mode,
            or None if the schedule does not cover this time of day (see
            hvac_schedule_logic.active_period_for) - in which case the
            automation makes no changes rather than inventing a target.
            Always None exactly when cool_target_c is None (same period).
        cool_target_c: The schedule's target for right now while in cool/dry
            mode. See hvac_schedule_logic.py's module docstring for why these
            are separate numbers, always with heat_target_c < cool_target_c.
        playroom_mode/landing_mode: Live operating mode of each unit,
            lowercased ("off", "cool", "dry", "heat", ...).
        playroom_powered_on/landing_powered_on: Live power state. Only a human
            may power units on or off, except during Away entry.
        playroom_target_c: Playroom's live setpoint, or None if unreadable.
        away_mode_active: Whether Away mode is currently on.
        state: The automation's persisted state from the previous check.
        mode_temp_limits: Per-mode target bounds, keyed by mode name.
        max_drift_c: How far hvac_target may drift from the active family
            target (§8.2).
        away_mode_target_c: Target forced on both units during Away.
        mirror_zone_fixed_target_c: Landing's fixed target outside Away.
        startup_default_mode: Seed mode for a first-ever run only (§8.6).

    """

    now: datetime
    room_temperature_c: float | None
    heat_target_c: float | None
    cool_target_c: float | None
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


def family_target_c(mode: str, heat_target_c: float, cool_target_c: float) -> float:
    """The comfort target belonging to `mode`'s own family.

    heat has its own target; cool and dry share one (they already share the
    same hardware temperature range - see ModeTempLimits/mode_temp_limits).
    A mode outside the normal cycle (e.g. "off", "auto") falls back to
    cool_target_c - only ever reached from Away-exit's live-mode read, where
    a sane default matters more than raising on an edge case.

    Examples:
        >>> family_target_c("heat", 18.0, 20.0)
        18.0
        >>> family_target_c("dry", 18.0, 20.0)
        20.0

    """
    return heat_target_c if mode == "heat" else cool_target_c


def allowed_target_range(
    mode: str,
    family_target_c: float,
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
    low = max(limits.min_c, family_target_c - max_drift_c)
    high = min(limits.max_c, family_target_c + max_drift_c)
    return low, high


def _dwell_satisfied(since: datetime | None, now: datetime, minutes: int) -> bool:
    """Whether a dwell condition has held continuously for at least `minutes`."""
    if since is None:
        return False
    return now - since >= timedelta(minutes=minutes)


def _update_dwell_timers(
    state: HvacState, room_c: float, heat_target_c: float, cool_target_c: float, now: datetime
) -> None:
    """Advance or reset all four below/above dwell timers, in place.

    Both target comparisons are tracked unconditionally, regardless of the
    current mode - see the module docstring for why intra-mode adjustment and
    mode escalation deliberately read different pairs of these four timers.

    Strict debounce (§8.5) applies to each pair independently: a single
    sample that contradicts a given comparison resets that comparison's
    clock, rather than being tolerated within a hysteresis band.
    """
    if room_c < heat_target_c:
        state.below_heat_target_since = state.below_heat_target_since or now
        state.above_heat_target_since = None
    elif room_c > heat_target_c:
        state.above_heat_target_since = state.above_heat_target_since or now
        state.below_heat_target_since = None
    else:
        state.below_heat_target_since = None
        state.above_heat_target_since = None

    if room_c < cool_target_c:
        state.below_cool_target_since = state.below_cool_target_since or now
        state.above_cool_target_since = None
    elif room_c > cool_target_c:
        state.above_cool_target_since = state.above_cool_target_since or now
        state.below_cool_target_since = None
    else:
        state.below_cool_target_since = None
        state.above_cool_target_since = None


def _below_dwell_since(state: HvacState, mode: str) -> datetime | None:
    """How long the room has continuously been below `mode`'s own family target."""
    return state.below_heat_target_since if mode == "heat" else state.below_cool_target_since


def _above_dwell_since(state: HvacState, mode: str) -> datetime | None:
    """How long the room has continuously been above `mode`'s own family target."""
    return state.above_heat_target_since if mode == "heat" else state.above_cool_target_since


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
        state.below_heat_target_since = None
        state.above_heat_target_since = None
        state.below_cool_target_since = None
        state.above_cool_target_since = None
        if context.heat_target_c is None:
            return HvacDecision(
                reason="Away exited, but the schedule does not cover this time - "
                "turning minimum heat off and leaving temperatures alone",
                state=state,
                minimum_heat=False,
            )
        resume_target = family_target_c(context.playroom_mode, context.heat_target_c, context.cool_target_c)
        state.hvac_target_c = resume_target
        state.heat_target_c = context.heat_target_c
        state.cool_target_c = context.cool_target_c
        state.last_target_change_at = now
        return HvacDecision(
            reason=(
                f"Away exited - restoring the schedule's {context.playroom_mode} target of "
                f"{resume_target}C immediately"
            ),
            state=state,
            minimum_heat=False,
            playroom_target_c=resume_target,
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

    if context.heat_target_c is None:
        return HvacDecision(
            reason="Schedule does not cover this time of day - making no changes",
            state=state,
        )

    # --- Schedule propagation -----------------------------------------------
    # A new schedule period resets the setpoint to the current mode's family
    # target, which is also what seeds hvac_target on a first-ever run.
    if (
        state.heat_target_c != context.heat_target_c
        or state.cool_target_c != context.cool_target_c
        or state.hvac_target_c is None
    ):
        state.heat_target_c = context.heat_target_c
        state.cool_target_c = context.cool_target_c
        new_target = family_target_c(context.playroom_mode, context.heat_target_c, context.cool_target_c)
        state.hvac_target_c = new_target
        state.last_target_change_at = now
        state.below_heat_target_since = None
        state.above_heat_target_since = None
        state.below_cool_target_since = None
        state.above_cool_target_since = None
        return HvacDecision(
            reason=(
                f"Schedule moved to a {new_target}C {context.playroom_mode} target - "
                f"propagating it to the units"
            ),
            state=state,
            playroom_target_c=new_target,
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

    _update_dwell_timers(
        state, context.room_temperature_c, context.heat_target_c, context.cool_target_c, now
    )

    current_family_target = family_target_c(context.playroom_mode, context.heat_target_c, context.cool_target_c)
    low, high = allowed_target_range(
        context.playroom_mode, current_family_target, context.mode_temp_limits, context.max_drift_c
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
    state.below_heat_target_since = None
    state.above_heat_target_since = None
    state.below_cool_target_since = None
    state.above_cool_target_since = None

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
    """Spec's 60-minute mode-change check, with §8.1's symmetric gate, §8.2's drift
    cap, and the directional destination-target trigger that creates the deadband
    against continual switching (see module docstring).

    Returns None if no mode change is due.
    """
    now = context.now
    mode = context.playroom_mode

    warmer = next_warmer_mode(mode)
    if warmer is not None:
        # Escalating INTO warmer uses warmer's OWN target as the trigger
        # (e.g. dry -> heat only once genuinely below heat_target_c, not
        # merely below cool_target_c) - the directional rule that creates
        # the deadband. §8.1's addition (absent from the spec's literal
        # cooling-direction-only wording): the setpoint must also already be
        # maxed out in its effort.
        warming_since = _mode_dwell_start(_below_dwell_since(state, warmer), state.last_target_change_at)
        if _dwell_satisfied(warming_since, now, MODE_DWELL_MINUTES) and current_target >= high:
            return _mode_change_decision(context, state, warmer, from_mode=mode, warming=True)

    colder = next_colder_mode(mode)
    if colder is not None:
        # Symmetric: de-escalating into colder uses colder's OWN target.
        cooling_since = _mode_dwell_start(_above_dwell_since(state, colder), state.last_target_change_at)
        if _dwell_satisfied(cooling_since, now, MODE_DWELL_MINUTES) and current_target <= low:
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
    from_family_target = family_target_c(from_mode, context.heat_target_c, context.cool_target_c)
    new_family_target = family_target_c(new_mode, context.heat_target_c, context.cool_target_c)

    if from_family_target == new_family_target:
        # Same family (cool <-> dry) - retain the existing, possibly
        # drift-adjusted setpoint. It still means the same thing in the new
        # mode, and losing a hot day's built-up adjustment on a mode tick
        # that doesn't even change what "comfortable" means would be wrong.
        target = state.hvac_target_c
    else:
        # Crossing families (dry <-> heat): spec's "moving from heat to
        # dry/cool, set target to 18C" - generalised from that literal 18C
        # to whichever cool_target_c the active period actually configures,
        # now that it need not be 18. The old setpoint belongs to the other
        # family's frame of reference and is meaningless here.
        target = new_family_target

    # Spec Phase 1: "On a mode change, if the current target is below the new
    # mode's minimum, raise it to that minimum immediately."
    new_limits = context.mode_temp_limits[new_mode]
    if target < new_limits.min_c:
        target = new_limits.min_c

    state.hvac_target_c = target
    state.last_mode_change_at = now
    state.last_target_change_at = now
    state.below_heat_target_since = None
    state.above_heat_target_since = None
    state.below_cool_target_since = None
    state.above_cool_target_since = None

    direction = "warmer" if warming else "colder"
    return HvacDecision(
        reason=(
            f"Room has been {'below' if warming else 'above'} the "
            f"{new_family_target}C {new_mode} target for {MODE_DWELL_MINUTES} min with the "
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
    """Spec's 30-minute +/-0.5C nudge, bounded by the mode limits and drift cap.

    Compares against the *current* mode's own family target (not the
    destination-target rule mode escalation uses above) - see module
    docstring.
    """
    now = context.now
    mode = context.playroom_mode

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

    if _dwell_satisfied(_below_dwell_since(state, mode), now, TEMPERATURE_DWELL_MINUTES):
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

    if _dwell_satisfied(_above_dwell_since(state, mode), now, TEMPERATURE_DWELL_MINUTES):
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
    own_family_target = family_target_c(context.playroom_mode, context.heat_target_c, context.cool_target_c)
    state.hvac_target_c = new_target
    # Spec: "A target temperature change resets the 60-minute mode-change timer."
    state.last_target_change_at = context.now
    return HvacDecision(
        reason=(
            f"Room has been {direction} the {own_family_target}C {context.playroom_mode} "
            f"target for {TEMPERATURE_DWELL_MINUTES} min - {verb.lower()} the setpoint to "
            f"{new_target}C"
        ),
        state=state,
        playroom_target_c=new_target,
        landing_target_c=context.mirror_zone_fixed_target_c,
    )
