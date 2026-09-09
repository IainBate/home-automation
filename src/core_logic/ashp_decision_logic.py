"""ASHP (Air Source Heat Pump) whole-house decision logic - pure functions.

Companion to hvac_decision_logic.py, same shape and conventions (clock
injected via context.now, never datetime.now() internally; a persisted
State round-tripped by the caller; a declarative Decision with "no opinion"
defaults so the caller writes only what changed) - see that module's own
docstring for the rationale, not repeated here.

State machine: just two whole-house states, ASHP_OFF and ASHP_ON -
simplified 2026-09-09 from an earlier four-state design (see docs/ASHP.md
§"State Machine Integrity") now that day/night lives *inside* ASHP_ON's own
fixed clock rather than needing a separate top-level state per time of day.

- **ASHP_OFF -> ASHP_ON** (see docs/ASHP.md requirement 2, unchanged since
  the original proposal): triggers when, continuously for
  `sustained_deficit_hours`, ALL of (a) it's the ASHP-mode "day" period
  (reusing that same clock's day/night boundary - see below), (b) the
  Playroom HVAC's live target has reached `hvac_ceiling_c`, and (c) the
  T6R's room reading stays below the active hvac_automation schedule's
  `house_target_c`. Gated by `min_rest_hours` since the last deactivation.

- **ASHP_ON -> ASHP_OFF** (docs/ASHP.md requirement 3, unchanged):
  triggers when outdoor temperature has risen `deactivation_margin_c`
  above the baseline recorded at activation, UNLESS the 48h forecast dips
  back to or below that same baseline first (anti-flapping - see
  _would_flap_within_forecast). Gated by `min_runtime_hours`.

- **While ASHP_ON** (docs/ASHP.md requirement 1, clarified 2026-09-09 -
  see that section for the owner's exact wording): a fixed daily clock,
  no longer the deficit/forecast logic above (that only decides *whether*
  ASHP_ON at all) -
    day_start..night_start   -> ASHP calls for heat at day_target_c;
                                 HVAC (both Airstage units) OFF.
    night_start..day_start   -> ASHP calls for heat at night_target_c;
                                 HVAC ON at fixed night_landing_target_c /
                                 night_playroom_target_c - NOT via the
                                 normal hvac_automation schedule/decision
                                 logic, which must not run at all this
                                 cycle (docs/ASHP.md's "No Double Control"
                                 V&V constraint - see
                                 scripts/ashp_automation_core.py, the only
                                 caller, for how that's actually enforced).

This module has no opinion on *how* a decision gets applied (HomeKit vs
MELCloud, which Airstage functions) - see src/api_clients/ashp_client.py
for the swappable control-path backend, and
scripts/ashp_automation_core.py for the read-decide-apply-persist glue,
mirroring hvac_automation_core.py's own role for hvac_decision_logic.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class AshpState:
    """Persisted state, round-tripped via hvac_automation_state.json's "ashp" key.

    Attributes:
        ashp_active: Whether the whole-house mode is currently ASHP_ON.
        activated_at: When ASHP_ON was last entered - starts the
            min_runtime_hours guard. None if never activated.
        deactivated_at: When ASHP_ON was last exited - starts the
            min_rest_hours guard. None if never activated (a fresh
            install has no rest guard to serve).
        activation_baseline_outdoor_c: Outdoor temperature recorded at the
            moment of the most recent activation - the deactivation
            threshold is this plus deactivation_margin_c. None while
            ASHP_OFF and never yet activated.
        below_target_since: When the room first went below house_target_c
            and has stayed below since, while day/ceiling conditions also
            held - see _update_deficit_timer. Only meaningful while
            ASHP_OFF; irrelevant (left as-is) while ASHP_ON.

    """

    ashp_active: bool = False
    activated_at: datetime | None = None
    deactivated_at: datetime | None = None
    activation_baseline_outdoor_c: float | None = None
    below_target_since: datetime | None = None


@dataclass
class AshpDecisionContext:
    """Everything needed to decide the ASHP's state and targets right now.

    Attributes:
        now: Current time (timezone-aware).
        state: The automation's persisted state from the previous check.
        room_temperature_c: T6R's current room reading, or None if
            unreadable - suspends the activation deficit timer (see
            _update_deficit_timer) rather than inventing a reading.
        house_target_c: The active hvac_automation schedule period's
            heat_target_c right now (hvac_automation_core.get_house_targets),
            or None if the schedule doesn't cover this time - suspends the
            activation trigger the same way.
        playroom_target_c: Live Playroom HVAC target (fetch_airstage_status),
            or None if unreadable - suspends the activation trigger.
        outdoor_temperature_c: Current outdoor reading (Airstage zones
            report this per-zone), or None if unreadable - suspends
            deactivation (fail-safe: can't confirm it's actually warmer,
            so stays ON rather than guessing).
        forecast_temps_c: Hourly forecast temperatures for the next
            forecast_lookahead_hours (weather_client.fetch_forecast_weather_hourly),
            or None if the fetch failed - suspends deactivation the same
            way as outdoor_temperature_c (can't rule out flapping without it).
        hvac_ceiling_c: Playroom target considered "at max" for the
            activation trigger's condition 2.
        sustained_deficit_hours: How long the deficit must hold continuously
            before activation (docs/ASHP.md requirement 2.3).
        deactivation_margin_c: Degrees above the activation baseline before
            deactivation is considered (requirement 3).
        min_runtime_hours: Minimum time ASHP_ON must hold before
            deactivation is even considered, once activated.
        min_rest_hours: Minimum time ASHP_OFF must hold before activation
            is even considered, once deactivated.
        day_start_minute: Start of the ASHP-mode "day" period, minutes
            since midnight (see hvac_schedule_logic.parse_hhmm) - shared
            boundary for both the activation trigger's "is it day" check
            and the day/night schedule below.
        night_start_minute: Start of the ASHP-mode "night" period, minutes
            since midnight.
        day_target_c: ASHP target while ASHP_ON during the day period.
        night_target_c: ASHP target while ASHP_ON during the night period.
        night_landing_target_c: Landing HVAC's fixed target during the
            ASHP_ON night period.
        night_playroom_target_c: Playroom HVAC's fixed target during the
            ASHP_ON night period.

    """

    now: datetime
    state: AshpState
    room_temperature_c: float | None
    house_target_c: float | None
    playroom_target_c: float | None
    outdoor_temperature_c: float | None
    forecast_temps_c: list[float] | None
    hvac_ceiling_c: float = 25.0
    sustained_deficit_hours: float = 2.0
    deactivation_margin_c: float = 2.0
    min_runtime_hours: float = 6.0
    min_rest_hours: float = 6.0
    day_start_minute: int = 360  # 06:00
    night_start_minute: int = 1320  # 22:00
    day_target_c: float = 18.0
    night_target_c: float = 14.0
    night_landing_target_c: float = 18.0
    night_playroom_target_c: float = 25.0


@dataclass
class AshpDecision:
    """The end state that should hold after this check.

    Every field defaults to "no opinion" - the caller applies only what is
    set, mirroring HvacDecision's own contract.

    Attributes:
        reason: Human-readable explanation, for logging.
        state: Updated state to persist.
        ashp_active: The whole-house mode after this decision.
        ashp_target_c: What to set the ASHP calling for heat at, or None
            to mean "turn it off" (only meaningful when ashp_active is
            False after a deactivation this cycle, or was already False).
        suppress_hvac_automation: True whenever hvac_automation's own
            decision logic must NOT run this cycle (i.e. ashp_active is
            True) - see module docstring's "No Double Control" note. The
            single field scripts/ashp_automation_core.py actually branches
            on; hvac_landing_target_c/hvac_playroom_target_c below say
            what to set instead.
        hvac_landing_target_c: Landing's fixed target while ASHP_ON at
            night, else None (leave alone / let hvac_automation decide).
        hvac_playroom_target_c: Same, for Playroom.
        hvac_should_power_off: True while ASHP_ON during the day period
            (HVAC fully off then) - distinct from "no opinion" (None target
            fields), which is what ASHP_OFF leaves for hvac_automation to
            decide instead.
        hvac_should_power_on: True only on the ASHP_ON -> ASHP_OFF
            transition itself (mirrors HvacDecision.power_on's role for
            Away-mode exit). hvac_decision_logic.determine_hvac_decision
            never powers a unit on by itself ("only a human (or Away
            entry) may power units on or off") - if the day period just
            powered the units off, deactivation must explicitly power them
            back on, or normal hvac_automation would simply no-op forever
            against powered-off units once control resumes, silently
            leaving the house unheated.

    """

    reason: str
    state: AshpState
    ashp_active: bool
    ashp_target_c: float | None = None
    suppress_hvac_automation: bool = False
    hvac_landing_target_c: float | None = None
    hvac_playroom_target_c: float | None = None
    hvac_should_power_off: bool = False
    hvac_should_power_on: bool = False


def _dwell_satisfied(since: datetime | None, now: datetime, hours: float) -> bool:
    """Whether a dwell condition has held continuously for at least `hours`."""
    if since is None:
        return False
    return now - since >= timedelta(hours=hours)


def _is_day_period(now_minute: int, day_start_minute: int, night_start_minute: int) -> bool:
    """True if now_minute falls in [day_start_minute, night_start_minute).

    Handles the general case where the day period itself wraps past
    midnight (day_start_minute > night_start_minute), though the household's
    actual configured values (06:00/22:00) never need that branch.
    """
    if day_start_minute <= night_start_minute:
        return day_start_minute <= now_minute < night_start_minute
    return now_minute >= day_start_minute or now_minute < night_start_minute


def _update_deficit_timer(state: AshpState, context: AshpDecisionContext, is_day: bool) -> None:
    """Advance or reset the activation deficit timer, in place.

    Strict debounce, matching hvac_decision_logic._update_dwell_timers: any
    sample that contradicts the full condition (wrong time of day, Playroom
    not at ceiling, room reading unavailable, or room reading not actually
    below target) resets the clock, rather than tolerating a blip.
    """
    playroom_at_ceiling = (
        context.playroom_target_c is not None and context.playroom_target_c >= context.hvac_ceiling_c
    )
    room_below_target = (
        context.room_temperature_c is not None
        and context.house_target_c is not None
        and context.room_temperature_c < context.house_target_c
    )
    if is_day and playroom_at_ceiling and room_below_target:
        state.below_target_since = state.below_target_since or context.now
    else:
        state.below_target_since = None


def _would_flap_within_forecast(
    forecast_temps_c: list[float], activation_baseline_outdoor_c: float
) -> bool:
    """True if the forecast dips back to or below the baseline that triggered activation.

    docs/ASHP.md requirement 3's anti-flapping rule: the *activation*
    baseline, not baseline + deactivation_margin_c - the point is to avoid
    turning off only to immediately need to turn back on again once the
    same cold snap that originally triggered it returns.
    """
    return any(temp <= activation_baseline_outdoor_c for temp in forecast_temps_c)


def _schedule_for(context: AshpDecisionContext, is_day: bool) -> tuple[float, float | None, float | None, bool]:
    """(ashp_target_c, landing_target_c, playroom_target_c, hvac_should_power_off) for the current period."""
    if is_day:
        return context.day_target_c, None, None, True
    return context.night_target_c, context.night_landing_target_c, context.night_playroom_target_c, False


def determine_ashp_decision(context: AshpDecisionContext) -> AshpDecision:
    """Decide the ASHP's state and targets for this check. Pure function."""
    state = context.state
    is_day = _is_day_period(
        context.now.hour * 60 + context.now.minute, context.day_start_minute, context.night_start_minute
    )

    if state.ashp_active:
        return _decide_while_active(context, is_day)
    return _decide_while_inactive(context, is_day)


def _decide_while_active(context: AshpDecisionContext, is_day: bool) -> AshpDecision:
    state = context.state

    if _dwell_satisfied(state.activated_at, context.now, context.min_runtime_hours):
        if (
            context.outdoor_temperature_c is not None
            and context.forecast_temps_c is not None
            and state.activation_baseline_outdoor_c is not None
            and context.outdoor_temperature_c
            >= state.activation_baseline_outdoor_c + context.deactivation_margin_c
        ):
            if _would_flap_within_forecast(
                context.forecast_temps_c, state.activation_baseline_outdoor_c
            ):
                pass  # held ON by anti-flapping - fall through to reapplying the schedule below
            else:
                new_state = AshpState(
                    ashp_active=False,
                    activated_at=state.activated_at,
                    deactivated_at=context.now,
                    activation_baseline_outdoor_c=None,
                    below_target_since=None,
                )
                return AshpDecision(
                    reason=(
                        f"Deactivating ASHP: outdoor {context.outdoor_temperature_c}C >= "
                        f"baseline {state.activation_baseline_outdoor_c}C + "
                        f"{context.deactivation_margin_c}C, forecast doesn't dip back down"
                    ),
                    state=new_state,
                    ashp_active=False,
                    ashp_target_c=None,
                    hvac_should_power_on=True,
                )

    ashp_target_c, landing_c, playroom_c, power_off = _schedule_for(context, is_day)
    return AshpDecision(
        reason=f"ASHP_ON, {'day' if is_day else 'night'} period: target {ashp_target_c}C",
        state=state,
        ashp_active=True,
        ashp_target_c=ashp_target_c,
        suppress_hvac_automation=True,
        hvac_landing_target_c=landing_c,
        hvac_playroom_target_c=playroom_c,
        hvac_should_power_off=power_off,
    )


def _decide_while_inactive(context: AshpDecisionContext, is_day: bool) -> AshpDecision:
    state = context.state
    _update_deficit_timer(state, context, is_day)

    if _dwell_satisfied(
        state.deactivated_at, context.now, context.min_rest_hours
    ) or state.deactivated_at is None:
        if _dwell_satisfied(state.below_target_since, context.now, context.sustained_deficit_hours):
            new_state = AshpState(
                ashp_active=True,
                activated_at=context.now,
                deactivated_at=state.deactivated_at,
                activation_baseline_outdoor_c=context.outdoor_temperature_c,
                below_target_since=None,
            )
            ashp_target_c, landing_c, playroom_c, power_off = _schedule_for(context, is_day)
            return AshpDecision(
                reason=(
                    f"Activating ASHP: room {context.room_temperature_c}C below target "
                    f"{context.house_target_c}C for {context.sustained_deficit_hours}h+ while "
                    f"Playroom at ceiling {context.hvac_ceiling_c}C"
                ),
                state=new_state,
                ashp_active=True,
                ashp_target_c=ashp_target_c,
                suppress_hvac_automation=True,
                hvac_landing_target_c=landing_c,
                hvac_playroom_target_c=playroom_c,
                hvac_should_power_off=power_off,
            )

    return AshpDecision(
        reason="ASHP_OFF: no change",
        state=state,
        ashp_active=False,
    )
