"""ASHP Automation - Shared Core Logic.

Mirrors hvac_automation_core.py's role, for the ASHP whole-house state
machine (src/core_logic/ashp_decision_logic.py) instead. One check,
run_ashp_decision_check(), does the whole read-decide-apply-persist cycle,
and is also the ONE place that decides whether hvac_automation's own
decision logic runs at all this cycle - see ashp_decision_logic.py's
module docstring's "No Double Control" note. hvac_automation_core.py and
hvac_decision_logic.py are both used here entirely unmodified (schedule
loading, room temperature reading, live zone status) - this module only
adds the ASHP layer on top and the one branch point.

Deliberately does not import src.api_clients.ashp_client directly into any
decision-making code - only into the apply step, mirroring
hvac_automation_core.py's own separation between determine_hvac_decision
(pure) and _apply_decision (I/O). The T6R-vs-MELCloud backend question is
entirely ashp_client.py's concern (see its own docstring) - this module
never needs to know which one is actually in use.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytz

from hvac_automation_core import (
    DEFAULT_MASTER_ZONE,
    DEFAULT_MIRROR_ZONE,
    DEFAULT_TIMEZONE,
    _find_zone,
    get_house_targets,
    load_schedule,
    locked_state,
    read_room_temperature_c,
)
from hvac_automation_core import run_hvac_decision_check as _run_hvac_decision_check

from src.api_clients.airstage_client import (
    fetch_airstage_status,
    set_airstage_power,
    set_airstage_temperature,
)
from src.api_clients.ashp_client import set_ashp_heat_call, set_ashp_off
from src.api_clients.weather_client import fetch_forecast_weather_hourly
from src.core_logic.ashp_decision_logic import (
    AshpDecision,
    AshpDecisionContext,
    AshpState,
    determine_ashp_decision,
)
from src.core_logic.hvac_schedule_logic import parse_hhmm
from src.utils.paths import get_project_root

logger = logging.getLogger("hvac_mode_daemon.ashp_automation_core")

DEFAULT_HVAC_CEILING_C = 25.0
DEFAULT_SUSTAINED_DEFICIT_HOURS = 2.0
DEFAULT_DEACTIVATION_MARGIN_C = 2.0
DEFAULT_FORECAST_LOOKAHEAD_DAYS = 2
DEFAULT_MIN_RUNTIME_HOURS = 6.0
DEFAULT_MIN_REST_HOURS = 6.0
DEFAULT_DAY_START_TIME = "06:00"
DEFAULT_NIGHT_START_TIME = "22:00"
DEFAULT_DAY_TARGET_C = 18.0
DEFAULT_NIGHT_TARGET_C = 14.0
DEFAULT_NIGHT_LANDING_TARGET_C = 18.0
DEFAULT_NIGHT_PLAYROOM_TARGET_C = 25.0
DEFAULT_ASHP_LOCK_TIMEOUT_SECONDS = 60


def get_config_path() -> str:
    """Resolve config.yaml relative to the project root, not the process cwd."""
    return str(Path(get_project_root()) / "config.yaml")


def _ashp_state_from_dict(raw: dict[str, Any]) -> AshpState:
    def _dt(key: str) -> datetime | None:
        value = raw.get(key)
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            logger.error("ashp state field %r (%r) is not a valid timestamp, ignoring", key, value)
            return None

    return AshpState(
        ashp_active=bool(raw.get("ashp_active", False)),
        activated_at=_dt("activated_at"),
        deactivated_at=_dt("deactivated_at"),
        activation_baseline_outdoor_c=raw.get("activation_baseline_outdoor_c"),
        below_target_since=_dt("below_target_since"),
    )


def _ashp_state_to_dict(state: AshpState) -> dict[str, Any]:
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "ashp_active": state.ashp_active,
        "activated_at": _iso(state.activated_at),
        "deactivated_at": _iso(state.deactivated_at),
        "activation_baseline_outdoor_c": state.activation_baseline_outdoor_c,
        "below_target_since": _iso(state.below_target_since),
    }


def _build_context(
    config: dict[str, Any], ashp_config: dict[str, Any], hvac_config: dict[str, Any], state: AshpState
) -> tuple[AshpDecisionContext, float | None]:
    """Gather all live inputs and build the context. Returns (context, room_temperature_c) -
    the latter also needed by the caller if it ends up delegating to hvac_automation_core.
    """
    tz_name = config.get("location", {}).get("default_timezone_str", DEFAULT_TIMEZONE)
    now_local = datetime.now(tz=pytz.timezone(tz_name))

    room_temperature_c = read_room_temperature_c(config)

    house_target_c = None
    try:
        schedules, day_assignments = load_schedule()
        house_target_c, _cool_target_c = get_house_targets(schedules, day_assignments, now_local)
    except (OSError, ValueError):
        logger.exception("Could not load schedule.yaml for the ASHP activation trigger")

    master_zone = hvac_config.get("master_zone", DEFAULT_MASTER_ZONE)
    statuses = fetch_airstage_status(config)
    master_status = _find_zone(statuses, master_zone)
    playroom_target_c = master_status.get("target_temperature_c") if master_status else None
    outdoor_temperature_c = master_status.get("outdoor_temperature_c") if master_status else None

    forecast_temps_c = None
    latitude = config.get("location", {}).get("latitude")
    longitude = config.get("location", {}).get("longitude")
    if latitude is not None and longitude is not None:
        records = fetch_forecast_weather_hourly(
            latitude, longitude, tz_name, forecast_days=DEFAULT_FORECAST_LOOKAHEAD_DAYS
        )
        if records:
            forecast_temps_c = [
                r["temperature_2m"] for r in records if r.get("temperature_2m") is not None
            ]
        else:
            logger.warning("ASHP: weather forecast unavailable - deactivation will hold ASHP on")

    context = AshpDecisionContext(
        now=now_local,
        state=state,
        room_temperature_c=room_temperature_c,
        house_target_c=house_target_c,
        playroom_target_c=playroom_target_c,
        outdoor_temperature_c=outdoor_temperature_c,
        forecast_temps_c=forecast_temps_c,
        hvac_ceiling_c=ashp_config.get("hvac_ceiling_c", DEFAULT_HVAC_CEILING_C),
        sustained_deficit_hours=ashp_config.get(
            "sustained_deficit_hours", DEFAULT_SUSTAINED_DEFICIT_HOURS
        ),
        deactivation_margin_c=ashp_config.get("deactivation_margin_c", DEFAULT_DEACTIVATION_MARGIN_C),
        min_runtime_hours=ashp_config.get("min_runtime_hours", DEFAULT_MIN_RUNTIME_HOURS),
        min_rest_hours=ashp_config.get("min_rest_hours", DEFAULT_MIN_REST_HOURS),
        day_start_minute=parse_hhmm(ashp_config.get("day_start_time", DEFAULT_DAY_START_TIME)),
        night_start_minute=parse_hhmm(ashp_config.get("night_start_time", DEFAULT_NIGHT_START_TIME)),
        day_target_c=ashp_config.get("day_target_c", DEFAULT_DAY_TARGET_C),
        night_target_c=ashp_config.get("night_target_c", DEFAULT_NIGHT_TARGET_C),
        night_landing_target_c=ashp_config.get(
            "night_landing_target_c", DEFAULT_NIGHT_LANDING_TARGET_C
        ),
        night_playroom_target_c=ashp_config.get(
            "night_playroom_target_c", DEFAULT_NIGHT_PLAYROOM_TARGET_C
        ),
    )
    return context, room_temperature_c


def _apply_ashp_decision(
    config: dict[str, Any], hvac_config: dict[str, Any], decision: AshpDecision, *, quiet: bool
) -> bool:
    """Apply an AshpDecision's fields. Order: ASHP target/off first, then whatever
    the day/night schedule says about the HVAC units (only when suppress_hvac_automation).
    """
    ok = True

    if decision.ashp_active:
        if decision.ashp_target_c is not None and not set_ashp_heat_call(
            config, decision.ashp_target_c
        ):
            logger.error("ASHP apply: failed to verify heat call at %sC", decision.ashp_target_c)
            ok = False
    elif not set_ashp_off(config):
        logger.error("ASHP apply: failed to verify ASHP off")
        ok = False

    if decision.hvac_should_power_on:
        results = set_airstage_power(config, True)
        if not all(results.values()):
            logger.error(
                "ASHP apply: failed to power HVAC units back on after deactivation: %s", results
            )
            ok = False

    if not decision.suppress_hvac_automation:
        return ok

    master_zone = hvac_config.get("master_zone", DEFAULT_MASTER_ZONE)
    mirror_zone = hvac_config.get("mirror_zone", DEFAULT_MIRROR_ZONE)

    if decision.hvac_should_power_off:
        results = set_airstage_power(config, False)
        if not all(results.values()):
            logger.error("ASHP apply: failed to power HVAC units off for the day period: %s", results)
            ok = False
        return ok

    if decision.hvac_landing_target_c is not None:
        results = set_airstage_temperature(config, decision.hvac_landing_target_c, zone_name=mirror_zone)
        if not results.get(mirror_zone, False):
            logger.error(
                "ASHP apply: failed to verify %s's ASHP-night target as %sC",
                mirror_zone,
                decision.hvac_landing_target_c,
            )
            ok = False
    if decision.hvac_playroom_target_c is not None:
        results = set_airstage_temperature(config, decision.hvac_playroom_target_c, zone_name=master_zone)
        if not results.get(master_zone, False):
            logger.error(
                "ASHP apply: failed to verify %s's ASHP-night target as %sC",
                master_zone,
                decision.hvac_playroom_target_c,
            )
            ok = False

    if not quiet:
        print(
            f"ASHP night schedule: {mirror_zone}={decision.hvac_landing_target_c}C, "
            f"{master_zone}={decision.hvac_playroom_target_c}C"
        )

    return ok


def run_ashp_decision_check(
    config: dict[str, Any],
    ashp_config: dict[str, Any],
    hvac_config: dict[str, Any],
    *,
    dry_run: bool = False,
    quiet: bool = False,
) -> int:
    """Read live state, decide, apply, and persist - one full ASHP control cycle.

    THE single branch point for docs/ASHP.md's "No Double Control" V&V
    constraint: delegates to hvac_automation_core.run_hvac_decision_check
    (unmodified) only when this cycle's decision does NOT require the ASHP
    layer to own the HVAC units - never both in the same cycle.

    Args:
        config: Full static config.
        ashp_config: config["ashp"].
        hvac_config: config["hvac_automation"] - reused for master_zone/
            mirror_zone naming and passed through to
            hvac_automation_core.run_hvac_decision_check on delegation.
        dry_run: Log/print the decision but don't call any write function,
            and don't delegate to hvac_automation either.
        quiet: Suppress this check's own print() output.

    Returns:
        0 on success, 1 if applying the decision (or the delegated hvac
        check) failed to fully verify.

    """
    with locked_state(timeout=DEFAULT_ASHP_LOCK_TIMEOUT_SECONDS) as raw_state:
        state = _ashp_state_from_dict(raw_state.get("ashp", {}))
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

        applied_ok = _apply_ashp_decision(config, hvac_config, decision, quiet=quiet)

    if decision.suppress_hvac_automation:
        return 0 if applied_ok else 1

    hvac_ok = _run_hvac_decision_check(
        config, hvac_config, room_temperature_c, dry_run=dry_run, quiet=quiet
    )
    return 0 if applied_ok and hvac_ok == 0 else 1
