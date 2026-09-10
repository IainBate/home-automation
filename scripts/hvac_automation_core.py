"""HVAC Automation - Shared Core Logic.

Shared by scripts/hvac_mode_daemon.py (continuous daemon, the only real
caller for now) and scripts/hvac_away_mode.py (one-shot CLI for the away-mode
flag) - the I/O and glue that sits between src/core_logic/hvac_schedule_logic.py
/hvac_decision_logic.py's pure functions and the live Airstage/T6R clients,
plus the persisted state file both read/write. Mirrors
scripts/hotwater_automation_core.py's role for the hot water subsystem.

One check, run_hvac_decision_check(), does the whole read-decide-apply-persist
cycle:
1. Load schedule.yaml, resolve the heat/cool comfort targets for right now
   (see hvac_schedule_logic.py's module docstring for why there are two).
2. Read both configured zones' live state via airstage_client.fetch_airstage_status
   (mode, power, live target) - never trusted from a cache or from the
   automation's own last-known state, per plan doc §8.3 (a human's manual
   change is simply the starting point for the next check, with no
   attribution logic needed).
3. Build an HvacDecisionContext and call determine_hvac_decision() - a pure
   function, see hvac_decision_logic.py's own docstring for the full
   priority order and design decisions (§8.1-§8.7).
4. Apply whatever the returned HvacDecision actually asks for (most checks
   ask for nothing at all) via airstage_client's write functions, including
   plan doc §8.7's per-unit mode-change retry/revert.
5. Persist the updated HvacState back to hvac_automation_state.json.

Unlike hotwater_automation_core.py, none of this needs to be async:
airstage_client's fetch/set_* functions are already synchronous wrappers
(each opens its own asyncio.run() internally), so this module stays plain
sync throughout - one fewer layer than the MELCloud/Ohme-backed hot water
checks needed.

The away-mode flag (scripts/hvac_away_mode.py) is a plain boolean, not a
holiday_mode.py-style --start-days N: the spec gives Away mode no expiry of
its own ("Away mode is off by default"), so there is no duration to count
down - it is toggled on/off by a human, like service_mode.py's engineer-pause
flag, not scheduled like holiday_mode.py's automation pause.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytz
import yaml

from src.api_clients.airstage_client import (
    fetch_airstage_status,
    set_airstage_minimum_heat,
    set_airstage_mode,
    set_airstage_power,
    set_airstage_temperature,
)
from src.api_clients.resideo_client import fetch_resideo_status
from src.config_manager.config_manager import (
    get_hvac_automation_config_error as _config_hvac_automation_error,
)
from src.core_logic.hvac_decision_logic import (
    HvacDecision,
    HvacDecisionContext,
    HvacState,
    ModeTempLimits,
    determine_hvac_decision,
)
from src.core_logic.hvac_schedule_logic import (
    SchedulePeriod,
    active_period_for,
    normalise_schedule,
    parse_periods,
    schedule_name_for_weekday,
)
from src.utils.paths import get_hvac_automation_state_path, get_project_root, get_schedule_path
from src.utils.state_store import locked_json_state, read_json_state

logger = logging.getLogger("hvac_mode_daemon.hvac_automation_core")

DEFAULT_TIMEZONE = "Europe/London"
DEFAULT_MASTER_ZONE = "Playroom"
DEFAULT_MIRROR_ZONE = "Landing"
DEFAULT_MIRROR_ZONE_FIXED_TARGET_C = 18
DEFAULT_MAX_DRIFT_C = 3
DEFAULT_AWAY_MODE_TARGET_C = 10
DEFAULT_STARTUP_DEFAULT_MODE = "dry"
DEFAULT_MODE_TEMP_LIMITS: dict[str, dict[str, float]] = {
    "heat": {"min_c": 16, "max_c": 30},
    "dry": {"min_c": 18, "max_c": 30},
    "cool": {"min_c": 18, "max_c": 30},
}
DEFAULT_HVAC_LOCK_TIMEOUT_SECONDS = 60


def get_config_path() -> str:
    """Resolve config.yaml relative to the project root, not the process cwd."""
    return str(Path(get_project_root()) / "config.yaml")


def read_state() -> dict[str, Any]:
    """Read the HVAC automation state file, or {} if absent/unreadable."""
    return read_json_state(get_hvac_automation_state_path())


def locked_state(timeout: float = DEFAULT_HVAC_LOCK_TIMEOUT_SECONDS):
    """Exclusive, race-free read-modify-write of the state file.

    Thin wrapper around src.utils.state_store.locked_json_state, exactly
    like hotwater_automation_core.locked_state - see that function's
    docstring for the full race it closes.
    """
    return locked_json_state(get_hvac_automation_state_path(), timeout)


def is_away_mode_active(state: dict[str, Any]) -> bool:
    """Whether scripts/hvac_away_mode.py's away-mode flag is currently active.

    A plain boolean with no expiry (see this module's docstring) - a
    missing/falsy state["away_mode"]["active"] means Away mode has no
    effect, the safe default.
    """
    return bool(state.get("away_mode", {}).get("active", False))


def get_hvac_automation_config_error(config: dict[str, Any]) -> str | None:
    """Return a human-readable error if hvac_automation can't actually run, else None.

    Delegates to config_manager.get_hvac_automation_config_error(), which
    validate_business_rules() also uses (as a warning rather than a hard
    gate) - keeping the condition in one place, exactly like
    hotwater_automation_core.get_hotwater_automation_config_error does for
    its own melcloud check.
    """
    return _config_hvac_automation_error(config)


def get_schedule_config_path() -> str:
    """Resolve schedule.yaml relative to the project root."""
    return get_schedule_path()


def load_schedule(path: str | None = None) -> tuple[dict[str, list[SchedulePeriod]], dict[str, str]]:
    """Load and normalise every named schedule from schedule.yaml.

    Raises OSError/ValueError on a missing or malformed file - a genuine
    "won't start until fixed" condition, matching load_static_config()'s own
    "raise if invalid" contract for config.yaml.

    Returns:
        (schedules, day_assignments) - schedules maps schedule name ->
        normalised periods (see hvac_schedule_logic.normalise_schedule);
        day_assignments maps lowercase weekday name -> schedule name (see
        hvac_schedule_logic.schedule_name_for_weekday). Days absent from
        day_assignments fall back to schedule_name_for_weekday's own
        DEFAULT_SCHEDULE_NAME ("at_home_all_day").

    """
    if not path:
        path = get_schedule_path()

    with Path(path).open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    schedules_raw = raw.get("schedules")
    if not schedules_raw:
        msg = f"{path}: no 'schedules' defined"
        raise ValueError(msg)

    schedules = {
        name: normalise_schedule(parse_periods(periods))
        for name, periods in schedules_raw.items()
    }
    day_assignments = raw.get("day_assignments", {})
    return (schedules, day_assignments)


def get_house_targets(
    schedules: dict[str, list[SchedulePeriod]],
    day_assignments: dict[str, str],
    now_local: datetime,
) -> tuple[float | None, float | None]:
    """The active schedule's (heat_target_c, cool_target_c) for now_local.

    Both None together covers both "the schedule genuinely has no period for
    this time of day" (see active_period_for's docstring) and "today is
    assigned to a schedule name that doesn't exist in schedule.yaml" (a
    config error, logged here) - callers treat both the same way: make no
    changes rather than inventing a target. See hvac_schedule_logic.py's
    module docstring for why there are two targets, not one.
    """
    schedule_name = schedule_name_for_weekday(day_assignments, now_local.weekday())
    periods = schedules.get(schedule_name)
    if periods is None:
        logger.error(
            "schedule.yaml: day assigned to unknown schedule %r - check day_assignments",
            schedule_name,
        )
        return (None, None)

    period = active_period_for(periods, now_local.time())
    if period is None:
        return (None, None)
    return (period.heat_target_c, period.cool_target_c)


def _mode_temp_limits(hvac_config: dict[str, Any]) -> dict[str, ModeTempLimits]:
    raw = hvac_config.get("mode_temp_limits", DEFAULT_MODE_TEMP_LIMITS)
    return {
        mode: ModeTempLimits(min_c=limits["min_c"], max_c=limits["max_c"])
        for mode, limits in raw.items()
    }


def _hvac_state_from_dict(raw: dict[str, Any]) -> HvacState:
    def _dt(key: str) -> datetime | None:
        value = raw.get(key)
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            logger.error("hvac state field %r (%r) is not a valid timestamp, ignoring", key, value)
            return None

    return HvacState(
        hvac_target_c=raw.get("hvac_target_c"),
        heat_target_c=raw.get("heat_target_c"),
        cool_target_c=raw.get("cool_target_c"),
        below_heat_target_since=_dt("below_heat_target_since"),
        above_heat_target_since=_dt("above_heat_target_since"),
        below_cool_target_since=_dt("below_cool_target_since"),
        above_cool_target_since=_dt("above_cool_target_since"),
        last_mode_change_at=_dt("last_mode_change_at"),
        last_target_change_at=_dt("last_target_change_at"),
        last_observed_mode=raw.get("last_observed_mode"),
        away_active=bool(raw.get("away_active", False)),
    )


def _hvac_state_to_dict(state: HvacState) -> dict[str, Any]:
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "hvac_target_c": state.hvac_target_c,
        "heat_target_c": state.heat_target_c,
        "cool_target_c": state.cool_target_c,
        "below_heat_target_since": _iso(state.below_heat_target_since),
        "above_heat_target_since": _iso(state.above_heat_target_since),
        "below_cool_target_since": _iso(state.below_cool_target_since),
        "above_cool_target_since": _iso(state.above_cool_target_since),
        "last_mode_change_at": _iso(state.last_mode_change_at),
        "last_target_change_at": _iso(state.last_target_change_at),
        "last_observed_mode": state.last_observed_mode,
        "away_active": state.away_active,
    }


def _find_zone(statuses: list[dict[str, Any]] | None, zone_name: str) -> dict[str, Any] | None:
    """Return the live status dict matching zone_name (case-insensitive), or None."""
    if not statuses:
        return None
    for status in statuses:
        if status.get("name", "").lower() == zone_name.lower():
            return status
    return None


def read_room_temperature_c(config: dict[str, Any]) -> float | None:
    """Read the T6R's current room temperature via resideo_client, or None.

    None covers "resideo disabled", "not paired", and any read failure -
    fetch_resideo_status already reduces every failure mode to None (see its
    own docstring's Circuit Breaker note), so this is a thin field-extraction
    wrapper, not a second layer of error handling.
    """
    status = fetch_resideo_status(config)
    if status is None:
        return None
    return status.get("current_temperature_c")


_time_sync_warning_logged = False


def run_hvac_time_sync_check() -> None:
    """Log, once per process, that HVAC time/date sync (spec Phase 4) is unsupported.

    pyairstage and the Airstage local LAN API have no date/time-setting
    capability at all (see this project's CLAUDE.md) - there is nothing to
    poll, retry, or eventually implement here. Registered as a check purely
    for architectural completeness (matching
    docs/hvac_thermostat_automation_plan.md §2's three-check design) rather
    than silently omitted, but logs only the first time it runs, not on
    every poll_intervals.hvac_time_sync_seconds tick - see plan doc §4.
    """
    global _time_sync_warning_logged
    if _time_sync_warning_logged:
        return
    _time_sync_warning_logged = True
    logger.warning(
        "HVAC time/date sync is unsupported by pyairstage/the Airstage local LAN API "
        "(see CLAUDE.md) - this check is a permanent no-op, not a pending TODO"
    )


def run_hvac_decision_check(
    config: dict[str, Any],
    hvac_config: dict[str, Any],
    room_temperature_c: float | None,
    *,
    dry_run: bool = False,
    quiet: bool = False,
) -> int:
    """Read live state, decide, apply, and persist - one full HVAC control cycle.

    Args:
        config: Full static config.
        hvac_config: config["hvac_automation"].
        room_temperature_c: The T6R's most recently read room temperature
            (see read_room_temperature_c), or None if unavailable - passed in
            rather than read here so the daemon can poll it on its own,
            faster cadence (poll_intervals.thermostat_seconds) than this
            decision check runs on.
        dry_run: Log/print the decision but don't call any write function.
        quiet: Suppress the CLI's own print() output (daemon usage).

    Returns:
        0 on success (including "decided to make no changes" and "schedule/
        zones unavailable this check"), 1 if applying the decision failed to
        fully verify.

    """
    tz_name = config.get("location", {}).get("default_timezone_str", DEFAULT_TIMEZONE)
    now_local = datetime.now(tz=pytz.timezone(tz_name))

    try:
        schedules, day_assignments = load_schedule()
        heat_target_c, cool_target_c = get_house_targets(schedules, day_assignments, now_local)
        master_zone = hvac_config.get("master_zone", DEFAULT_MASTER_ZONE)
        mirror_zone = hvac_config.get("mirror_zone", DEFAULT_MIRROR_ZONE)

        statuses = fetch_airstage_status(config)
        master_status = _find_zone(statuses, master_zone)
        mirror_status = _find_zone(statuses, mirror_zone)

        if master_status is None or not master_status.get("available"):
            logger.warning(
                "HVAC check: master zone %r unavailable - skipping this check", master_zone
            )
            if not quiet:
                print(f"Master zone {master_zone!r} unavailable - skipping this check")
            return 0

        if mirror_status is None or not mirror_status.get("available"):
            logger.warning(
                "HVAC check: mirror zone %r unavailable - skipping this check", mirror_zone
            )
            if not quiet:
                print(f"Mirror zone {mirror_zone!r} unavailable - skipping this check")
            return 0

        previous_master_mode = master_status["mode"].lower()

        with locked_state(timeout=DEFAULT_HVAC_LOCK_TIMEOUT_SECONDS) as raw_state:
            state = _hvac_state_from_dict(raw_state.get("hvac", {}))
            away_active = is_away_mode_active(raw_state)

            context = HvacDecisionContext(
                now=now_local,
                room_temperature_c=room_temperature_c,
                heat_target_c=heat_target_c,
                cool_target_c=cool_target_c,
                playroom_mode=previous_master_mode,
                landing_mode=mirror_status["mode"].lower(),
                playroom_powered_on=master_status.get("powered_on", False),
                landing_powered_on=mirror_status.get("powered_on", False),
                playroom_target_c=master_status.get("target_temperature_c"),
                away_mode_active=away_active,
                state=state,
                mode_temp_limits=_mode_temp_limits(hvac_config),
                max_drift_c=hvac_config.get("max_drift_c", DEFAULT_MAX_DRIFT_C),
                away_mode_target_c=hvac_config.get("away_mode_target_c", DEFAULT_AWAY_MODE_TARGET_C),
                mirror_zone_fixed_target_c=hvac_config.get(
                    "mirror_zone_fixed_target_c", DEFAULT_MIRROR_ZONE_FIXED_TARGET_C
                ),
                startup_default_mode=hvac_config.get(
                    "startup_default_mode", DEFAULT_STARTUP_DEFAULT_MODE
                ),
            )

            decision = determine_hvac_decision(context)
            logger.info("HVAC decision: %s", decision.reason)
            if not quiet:
                print(f"HVAC decision: {decision.reason}")

            raw_state["hvac"] = _hvac_state_to_dict(decision.state)

            if dry_run:
                if not quiet:
                    print("(dry run - not applying)")
                return 0

            applied_ok = _apply_decision(
                config, master_zone, mirror_zone, previous_master_mode, decision, quiet=quiet
            )

        if applied_ok:
            return 0
        return 1
    except (OSError, ValueError) as exc:
        logger.error("Could not load schedule.yaml: %s", exc)
        if not quiet:
            print(f"Could not load schedule.yaml: {exc}")
        return 1


def _apply_decision(
    config: dict[str, Any],
    master_zone: str,
    mirror_zone: str,
    previous_master_mode: str,
    decision: HvacDecision,
    *,
    quiet: bool,
) -> bool:
    """Apply an HvacDecision's non-None fields to the two units.

    Order: power-on (Away entry only) -> mode (with §8.7 retry/revert) ->
    temperatures -> minimum_heat. Mode before temperature matches the spec's
    "mode changes take priority over temperature changes" and
    hvac_decision_logic._mode_change_decision's target sometimes depending
    on the mode change itself (heat -> dry/cool forces 18C).

    Returns:
        True if every requested write verified successfully (a decision with
        nothing to apply also returns True).

    """
    ok = True

    if decision.power_on:
        results = set_airstage_power(config, True)
        if not all(results.values()):
            logger.error("HVAC apply: power-on failed for one or more zones: %s", results)
            ok = False

    if decision.target_mode is not None:
        if not _apply_mode_change(config, decision.target_mode, previous_master_mode, quiet=quiet):
            ok = False

    if decision.playroom_target_c is not None:
        results = set_airstage_temperature(config, decision.playroom_target_c, zone_name=master_zone)
        if not results.get(master_zone, False):
            logger.error(
                "HVAC apply: failed to verify %s's target as %sC",
                master_zone,
                decision.playroom_target_c,
            )
            ok = False

    if decision.landing_target_c is not None:
        results = set_airstage_temperature(config, decision.landing_target_c, zone_name=mirror_zone)
        if not results.get(mirror_zone, False):
            logger.error(
                "HVAC apply: failed to verify %s's target as %sC",
                mirror_zone,
                decision.landing_target_c,
            )
            ok = False

    if decision.minimum_heat is not None:
        results = set_airstage_minimum_heat(config, decision.minimum_heat)
        if not all(results.values()):
            logger.error(
                "HVAC apply: minimum_heat=%s failed for one or more zones: %s",
                decision.minimum_heat,
                results,
            )
            ok = False

    return ok


def _apply_mode_change(
    config: dict[str, Any], target_mode: str, previous_mode: str, *, quiet: bool
) -> bool:
    """Set mode on both zones, with plan doc §8.7's retry-then-revert on partial failure.

    set_airstage_mode() always targets every configured zone in one call (a
    structural constraint - see its own docstring), so there is no per-zone
    mode setter to retry individually. A second full-system call retries
    whichever zone(s) failed while being a harmless same-value re-verify for
    the zone(s) that already succeeded. If that retry still leaves any zone
    not at target_mode, revert is likewise one more full-system call back to
    previous_mode - it brings the zone(s) that did succeed back down, and
    re-attempts (without expectation of success) the zone that's actually
    the problem; either way the two zones are never left silently split.

    Returns:
        True only if target_mode was confirmed on both zones (immediately or
        after one retry). False after a revert (successful or not) - the
        caller's requested decision was not achieved, even though the safe
        fallback action was taken.

    """
    results = set_airstage_mode(config, target_mode)
    if all(results.values()):
        return True

    failed = [name for name, succeeded in results.items() if not succeeded]
    logger.warning("HVAC apply: mode change to %s failed on %s - retrying", target_mode, failed)
    if not quiet:
        print(f"Mode change to {target_mode} failed on {failed} - retrying")

    retry_results = set_airstage_mode(config, target_mode)
    if all(retry_results.values()):
        return True

    still_failed = [name for name, succeeded in retry_results.items() if not succeeded]
    logger.error(
        "HVAC apply: mode change to %s still failing on %s after retry - "
        "reverting to the previous mode %s",
        target_mode,
        still_failed,
        previous_mode,
    )
    if not quiet:
        print(
            f"Mode change to {target_mode} still failing on {still_failed} "
            f"after retry - reverting to {previous_mode}"
        )

    revert_results = set_airstage_mode(config, previous_mode)
    if not all(revert_results.values()):
        logger.error(
            "HVAC apply: revert to previous mode %s ALSO failed on one or more zones "
            "(%s) - units may be left in a split mode state, check manually",
            previous_mode,
            revert_results,
        )
    return False
