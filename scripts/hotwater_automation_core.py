#!/usr/bin/env python3
"""Hot Water Automation - Shared Core Logic.

Shared by scripts/hotwater_auto_check.py (one-shot CLI) and
scripts/hotwater_mode_daemon.py (continuous daemon, mirroring
battery_mode_daemon.py's architecture) - the "evaluate the force-heat
decision and act on it" and "revert to auto if overdue" operations, plus
their supporting state/status helpers, live here once so the two entry
points can't drift apart.

Force-heats the tank via MELCloud if it needs it and either:
- the Ohme EV charger is confirmed charging (same power-threshold +
  2-consecutive-cycle confirmation as battery_mode_daemon.py's own charging
  check - see src/core_logic/ohme_charging_logic.py), it's within the
  car-charging trigger window (hotwater_automation.
  car_charging_trigger_start_hour, default 15:00 / 3pm, up to trigger_hour -
  excludes the morning/midday specifically, since solar water heating is
  still effective then and an ASHP force-heat off the back of an unrelated
  EV session isn't wanted) - Ohme has already decided this is an economical
  time to draw power, so hot water piggybacks on it while that condition is
  still being watched for, and the tank's *live* temperature is below
  tank_temp_threshold_c right now, OR
- it's at/after trigger_hour (hotwater_automation.trigger_hour, default 21.5
  / 9:30pm) AND either the battery has surplus stored solar (SoC >=
  battery_soc_min_percent) or the grid is currently in the tariff's
  off-peak window (Octopus Intelligent Go: 23:30-05:30 by default - see
  hotwater_automation.offpeak_start/offpeak_end), AND the tank was below
  tank_temp_threshold_c at hotwater_automation.daily_check_hour (default
  18:00 / 6pm) - see _update_daily_threshold_snapshot's docstring for why
  this one reading, not a live one, decides "was heating needed today" for
  every non-car-charging path.

Car charging is only monitored within its trigger window, not indefinitely:
once trigger_hour passes without the car having charged, the decision
switches over entirely to the battery/off-peak check above - so a tank
that's still cold at trigger_hour heats from stored solar immediately if
there's enough of it, or otherwise waits for the off-peak window rather than
continuing to wait on the car indefinitely.

Because the car-charging condition can occur at any moment within its
window, the force-heat check needs to run frequently (e.g. every 10-15
minutes), not just once at the trigger hour - each run is cheap and a no-op
unless a condition is actually met.

Turning heating back off happens only in the separate revert check, never in
the force-heat check itself - so a window started because the car was
charging always runs through to completion even if the car stops charging
(or any other trigger condition flips) a few minutes later, rather than
flapping on and off. The revert check reverts once the tank reaches its own
target_tank_temperature (the normal, expected way this ends), or once
hotwater_automation.force_heat_max_duration_hours elapses regardless, as a
safety net in case MELCloud never reports the tank as having reached target.

Legionella high-temperature cycles use this exact same trigger - there is no
separate schedule or condition check for them. The only difference is a
minimum-interval gate: whenever the above conditions fire a force-heat and at
least legionella_interval_days have passed since the last completed cycle,
that force-heat is done as a legionella cycle (raised target temperature)
instead of a normal one. This means a cycle never runs sooner than
legionella_interval_days, but can run later than that if the normal trigger
conditions simply don't occur for a while - it rides on the same "is it worth
heating right now" decision rather than firing on its own clock.

Whether *today* is even a legionella candidate is decided from the exact
same daily_check_hour snapshot as the non-car-charging heat decision above
(_update_daily_threshold_snapshot) - not a separate reading of its own. Only
if that snapshot found the tank cold does a later trigger (whenever it
actually fires - overnight, timed by battery/off-peak as usual) get upgraded
to a legionella cycle. This keeps the trigger's own timing untouched while
pinning the legionella decision itself to a predictable point in the day,
rather than whatever moment the tank happened to be read at (e.g. the middle
of the night). A car-charging-triggered heat never becomes a legionella
cycle, since it's decided from the live reading, not this snapshot - given
legionella cycles are already rare (legionella_interval_days, ~90 days by
default), this is a narrow, low-impact trade rather than a gap worth extra
mechanism to close.

A legionella cycle - or indeed any day's heating, forced or not - is also
considered complete the moment the tank is observed at or above
hotwater_automation.legionella_natural_completion_temp_c (default 55C),
regardless of what put the heat there. run_legionella_progress_check applies
this to an active cycle instead of insisting on the full elevated target;
run_legionella_natural_completion_check applies it independently, on a plain
quiet day with no automation activity at all (e.g. an off-grid solar
diverter this project can't otherwise see). Either way, that resets the
legionella_interval_days clock from the moment of that reading.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any

import pytz

from src.api_clients.melcloud_client import HotWaterOperationMode, MelCloudClient
from src.api_clients.ohme_ev_client import OhmeEVClient
from src.api_clients.ohme_status_cache import read_fresh_status
from src.api_clients.solax_modbus_client import solax_modbus_soc
from src.config_manager.config_manager import get_hotwater_melcloud_config_error
from src.core_logic.battery_evening_prediction_logic import predict_evening_soc
from src.core_logic.hotwater_decision_logic import (
    HotWaterDecisionContext,
    determine_hotwater_decision,
    hour_float_to_time,
    is_in_evening_window,
    is_in_offpeak_window,
)
from src.core_logic.ohme_charging_logic import (
    confirm_charging_over_consecutive_cycles,
    is_charging_above_threshold,
)
from src.utils.emailer import send_email
from src.utils.historical_data import load_historical_records
from src.utils.paths import (
    get_battery_evening_prediction_path,
    get_hotwater_automation_state_path,
    get_project_root,
)
from src.utils.state_store import locked_json_state, read_json_state


# Named as a child of "hotwater_mode_daemon" (not __name__) so its records
# propagate up to that logger's rotating-file handler (see
# src/daemon_support/base_daemon.py's setup_rotating_logger) when running
# under hotwater_mode_daemon.py - otherwise every decision/action log here
# (force-heat activated, reverted, legionella started, etc.) had no handler
# anywhere in its chain and was silently lost rather than reaching
# logs/hotwater_mode_daemon.log (discovered 2026-09-02: the daemon was
# making correct decisions per its own tests, but none of them were
# observable in the log a human would actually check).
logger = logging.getLogger("hotwater_mode_daemon.hotwater_automation_core")

DEFAULT_TANK_TEMP_THRESHOLD_C = 45.0
# Minimum charge BOTH batteries (not their average) must independently clear
# - see get_battery_soc_percent's docstring for why this is a minimum, not an
# average.
DEFAULT_BATTERY_SOC_MIN_PERCENT = 20.0
DEFAULT_OFFPEAK_START = "23:30"
DEFAULT_OFFPEAK_END = "05:30"
DEFAULT_TRIGGER_HOUR = 21.5  # 9:30pm - fractional hours are supported (e.g. 21.5 = 21:30)
# Battery-prediction trigger path (get_battery_prediction_to_deadline) - an
# independent alternative to trigger_hour/car-charging, active across this
# wider afternoon-through-evening span. Deadline defaults to offpeak_start
# (23:30/11:30pm): the moment the grid's off-peak window opens anyway, so a
# prediction that both batteries will still clear battery_soc_min_percent by
# then means it's safe to heat from stored solar any time before that,
# without waiting on trigger_hour first.
DEFAULT_BATTERY_PREDICTION_WINDOW_START_HOUR = 15.0  # 3pm
DEFAULT_BATTERY_PREDICTION_DEADLINE_HOUR = 23.5  # 11:30pm
# Car charging only counts as a force-heat trigger from this hour up to
# trigger_hour - see is_car_charging_confirmed's docstring. Excludes the
# morning/midday specifically (not just "before this hour is fine too") -
# solar water heating is still effective earlier in the day, so an
# ASHP force-heat off the back of an unrelated EV charging session isn't
# wanted then.
DEFAULT_CAR_CHARGING_TRIGGER_START_HOUR = 15.0  # 3pm
DEFAULT_OHME_CHARGING_THRESHOLD_WATTS = 500.0
# Hard safety-net cap on a single heating run, whatever triggered it. Kept
# deliberately short - if the tank isn't reaching target/disinfection
# temperature within this, run_revert_check/run_legionella_progress_check
# stop it and let the next due trigger retry, rather than running long.
DEFAULT_FORCE_HEAT_MAX_DURATION_HOURS = 1.0
DEFAULT_TIMEZONE = "Europe/London"
DEFAULT_LEGIONELLA_INTERVAL_DAYS = 90
# What a legionella cycle asks MELCloud to heat the tank to. Deliberately not
# higher (e.g. 60C) by default - some ASHPs can't reliably reach the top of
# their nominal range (especially in cold weather, when flow-temperature
# output derates), so asking for more than DEFAULT_LEGIONELLA_NATURAL_
# COMPLETION_TEMP_C below just means the cycle runs its full
# legionella_max_cycle_duration_hours every time chasing a target it may
# never reach, without the tank actually being any less disinfected for it.
DEFAULT_LEGIONELLA_TARGET_TEMP_C = 55.0
DEFAULT_LEGIONELLA_MAX_CYCLE_DURATION_HOURS = 6.0
# The tank's below-threshold state is snapshotted once a day at this hour -
# both the normal force-heat decision (outside the car-charging window - see
# DEFAULT_CAR_CHARGING_TRIGGER_START_HOUR) and the legionella-due decision
# are pinned to this one daily reading rather than whatever the tank happens
# to read at whenever their own trigger conditions actually fire. See
# _update_daily_threshold_snapshot's docstring for why.
DEFAULT_DAILY_CHECK_HOUR = 18.0
# A tank reading at/above this, at any time and regardless of what put the
# heat there (the ASHP, an immersion, or an off-grid solar diverter this
# project can't otherwise see), counts as satisfying the current legionella
# interval - see run_legionella_natural_completion_check's docstring.
DEFAULT_LEGIONELLA_NATURAL_COMPLETION_TEMP_C = 55.0
DEFAULT_MAX_PREDICTION_AGE_HOURS = 3.0
# How long run_force_heat_check will wait to acquire the state file lock
# before giving up. It holds the lock across its whole MELCloud
# request-then-verify retry loop (melcloud.mode_change_retry.max_attempts *
# check_delay_seconds, worst case ~60s at the defaults - see
# src/api_clients/melcloud_client.py's DEFAULT_MAX_ATTEMPTS/
# DEFAULT_CHECK_DELAY_SECONDS) to keep it genuinely mutually exclusive with a
# second overlapping invocation (e.g. the cron entry and the daemon both
# firing). A waiting process needs to outlast that worst case with real
# margin - equal timeouts would just race at the boundary - so this is kept
# at roughly 2x the retry loop's own worst case, not raised to match it
# exactly whenever that worst case changes.
DEFAULT_HOTWATER_LOCK_TIMEOUT_SECONDS = 120.0


def get_config_path() -> str:
    """Resolve config.yaml relative to the project root, not the process cwd.

    Cron/systemd jobs typically don't start in the project root, so a bare
    relative "config.yaml" would silently fail to load even though the same
    code works fine run manually from the repo root.
    """
    return str(Path(get_project_root()) / "config.yaml")


def read_state() -> dict[str, Any]:
    """Read the hot water automation state file, or {} if absent/unreadable."""
    return read_json_state(get_hotwater_automation_state_path())


def locked_state(timeout: float = 10.0) -> contextlib.AbstractContextManager[dict[str, Any]]:
    """Exclusive, race-free read-modify-write of the state file.

    hotwater_auto_check.py (cron) and hotwater_mode_daemon.py can both touch
    this file, and each force-heat/revert/legionella check does real
    MELCloud/Ohme I/O (including MELCloud's request-then-verify retry loop,
    which can take many seconds) between when it first reads state and when
    it finally writes an update back. A plain read_state()-then-write_state()
    pair is atomic for the write itself, but not for that whole
    read-decide-write cycle: if two processes' cycles overlap, whichever
    finishes its (possibly much longer) work last overwrites the file with a
    copy of state that was already stale when it started, silently erasing
    the other's update. Holding an exclusive lock for the read-and-final-write
    step closes that window - callers should still do their slow I/O
    *before* entering this context, and only use the block itself for the
    fast "re-read current state, merge in my update" step.

    Thin wrapper around src.utils.state_store.locked_json_state - the same
    primitive src/api_clients/_modbus_mode_controller.py's
    _locked_mode_change_log uses for the SolaX mode-change log, so there is
    one race-safety story rather than a separately-maintained one here.

    Yields:
        The current state dict - mutate it in place; it's written back
        automatically when the block exits normally. Nothing is written if
        the block raises.

    """
    return locked_json_state(get_hotwater_automation_state_path(), timeout)


def get_battery_soc_percent(config: dict[str, Any]) -> float | None:
    """Get average battery SoC across master/slave inverters, or None if unavailable.

    Reads live via the same solax_modbus_soc() function battery_mode_daemon.py
    itself uses - a read-only Modbus call, safe to run independently alongside
    the battery daemon without touching or coordinating with it.
    """
    soc_data = solax_modbus_soc(config)
    if soc_data is None:
        return None
    return (soc_data["master"] + soc_data["slave"]) / 2


def get_hotwater_automation_config_error(config: dict[str, Any]) -> str | None:
    """Return a human-readable error if hotwater_automation can't actually run, else None.

    hotwater_automation.enabled=true with melcloud disabled or missing
    credentials would otherwise only be discovered when MelCloudClient's
    constructor raises ValueError on the first connect() attempt inside
    run_force_heat_check() - in hotwater_mode_daemon.py that's caught by a
    blanket `except Exception` and logged every poll_interval_seconds
    forever, rather than surfaced once as a clear "won't start" condition.
    Callers should check this once at startup (CLI) or once per config
    (re)load (daemon) and refuse to proceed with a clear message instead.

    Delegates the actual condition to config_manager's
    get_hotwater_melcloud_config_error(), which validate_business_rules()
    also uses (as a warning rather than a hard gate) - keeping the rule in
    one place so the two enforcement paths can't drift apart.
    """
    return get_hotwater_melcloud_config_error(config)


def get_holiday_until(state: dict[str, Any]) -> datetime | None:
    """Return the active holiday's end time (state["holiday"]["until"]), or None.

    Written by scripts/holiday_mode.py's --start-days, cleared by --cancel,
    and left in place (but naturally ignored once it's in the past - see
    is_holiday_active) when a holiday simply runs its course. None covers
    "no holiday recorded", a malformed/non-string/unparseable timestamp, and
    a timestamp with no timezone offset (is_holiday_active compares against
    an aware datetime.now(tz=UTC), which raises TypeError against a naive
    one - rejecting it here instead keeps that comparison safe) - all of
    these mean holiday mode has no effect, the safe default for a household
    that isn't currently on holiday.
    """
    until_str = state.get("holiday", {}).get("until")
    if not until_str:
        return None
    try:
        until = datetime.fromisoformat(until_str)
    except (TypeError, ValueError):
        logger.error(
            "holiday.until (%r) is not a valid timestamp, ignoring - holiday mode has no effect",
            until_str,
        )
        return None
    if until.tzinfo is None:
        logger.error(
            "holiday.until (%r) has no timezone offset, ignoring - holiday mode has no effect",
            until_str,
        )
        return None
    return until


def is_holiday_active(state: dict[str, Any]) -> bool:
    """Whether a holiday period (scripts/holiday_mode.py) is currently in effect."""
    until = get_holiday_until(state)
    return until is not None and datetime.now(tz=UTC) < until


def get_effective_battery_soc_percent(
    config: dict[str, Any], hw_config: dict[str, Any], now_local: datetime
) -> tuple[float | None, str]:
    """Get the battery SoC to use for the force-heat decision, and where it came from.

    Prefers a same-evening prediction from scripts/battery_evening_predictor.py
    (written to get_battery_evening_prediction_path()) over a live reading: a
    force-heat run can take up to force_heat_max_duration_hours, so a live SoC
    snapshot at trigger_hour can't vouch for the whole window, but a
    prediction targeting trigger_hour + horizon_hours can. Falls back to a
    live read (get_battery_soc_percent) whenever the predictor is disabled,
    hasn't run yet, its output is stale (older than max_prediction_age_hours
    or from a different calendar day), or its historical data was too thin to
    produce a prediction - the force-heat decision must keep working even
    with zero ML wiring.

    Args:
        config: Full static config (for the live-SoC fallback).
        hw_config: hotwater_automation config section (for max_prediction_age_hours).
        now_local: Current time in the configured local timezone, used to
            reject a prediction left over from a previous calendar day even
            if it's still within max_prediction_age_hours.

    Returns:
        (soc_percent, source) where source is "predicted" or "live" - source
        is included purely for logging, so it's obvious which path was used.

    """
    if config.get("battery_evening_prediction", {}).get("enabled", False):
        predicted = _read_fresh_evening_prediction(hw_config, now_local)
        if predicted is not None:
            return predicted, "predicted"
    return get_battery_soc_percent(config), "live"


def _read_fresh_evening_prediction(
    hw_config: dict[str, Any], now_local: datetime
) -> float | None:
    """Return today's predicted evening SoC if the prediction file is fresh, else None."""
    path = Path(get_battery_evening_prediction_path())
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Failed to read battery evening prediction file, ignoring")
        return None

    predicted_soc = record.get("predicted_soc_percent")
    computed_at_str = record.get("computed_at")
    if predicted_soc is None or not computed_at_str:
        return None

    try:
        computed_at = datetime.fromisoformat(computed_at_str)
    except ValueError:
        return None

    computed_at_local = computed_at.astimezone(now_local.tzinfo)
    if computed_at_local.date() != now_local.date():
        logger.info(
            "Evening SoC prediction is from %s, not today (%s) - ignoring, falling back "
            "to live SoC",
            computed_at_local.date(),
            now_local.date(),
        )
        return None

    max_age_hours = hw_config.get(
        "max_prediction_age_hours", DEFAULT_MAX_PREDICTION_AGE_HOURS
    )
    age_hours = (datetime.now(tz=UTC) - computed_at).total_seconds() / 3600.0
    if age_hours > max_age_hours:
        logger.info(
            "Evening SoC prediction is %.1fh old (> %sh limit), ignoring - falling back "
            "to live SoC",
            age_hours,
            max_age_hours,
        )
        return None

    return float(predicted_soc)


async def _get_ohme_charging_power_watts(config: dict[str, Any]) -> float | None:
    """Read the Ohme EV charger's current power draw in watts, or None if unavailable.

    Prefers scripts/ohme_status_daemon.py's shared cache (see
    src/api_clients/ohme_status_cache.py) over opening a session here, which
    performed a full Firebase login on every force-heat check. Falls back to
    a direct read when that cache is missing or stale, so this behaves
    exactly as it did before the poller existed if it isn't running.

    Best-effort: if Ohme isn't configured/enabled, or the check fails for any
    reason, this degrades to None (treat as "no charging signal") rather than
    blocking the hot water decision - the battery/off-peak conditions can
    still apply on their own.
    """
    if not config.get("ohme_ev", {}).get("enabled", False):
        return None

    cached = read_fresh_status()
    if cached is not None:
        return cached.get("power_watts", 0)

    client = None
    try:
        client = OhmeEVClient(config_path=get_config_path())
        await client.connect()
        status = await client.get_charger_status(use_cache=False)
        return status.get("power_watts", 0)
    except Exception:
        logger.exception("Failed to check Ohme charging status, treating as not charging")
        return None
    finally:
        # client stays None if the constructor itself raised - nothing to close.
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()


async def is_car_charging_confirmed(
    config: dict[str, Any],
    hw_config: dict[str, Any],
    state: dict[str, Any],
    now_local: datetime,
    trigger_time: time,
    window_start_time: time,
) -> bool:
    """Whether the car is confirmed charging, for the force-heat decision.

    Car charging is only monitored within [window_start_time, trigger_time) -
    see this module's docstring for why. Outside that window (including
    before window_start_time - solar water heating is still effective
    earlier in the day, so an unrelated EV session shouldn't force-heat the
    ASHP then), this returns False unconditionally (without even checking
    Ohme) and resets the confirmation counter, so the window's next opening
    starts its own confirmation fresh.

    Uses the same power-threshold + 2-consecutive-cycle confirmation as
    battery_mode_daemon.py's own Ohme charging check
    (src/core_logic/ohme_charging_logic.py), so both automations agree on
    what "the car is charging" means. The consecutive-cycle count is
    persisted in the same locked state dict as the rest of the force-heat
    check (state["ohme_charging_confirm_cycles"]) rather than an in-memory
    instance attribute like the battery daemon uses, since this is called
    from both the continuous daemon and one-shot cron invocations - a plain
    in-memory counter would reset to 0 on every cron run.
    """
    if now_local.time() < window_start_time or now_local.time() >= trigger_time:
        state["ohme_charging_confirm_cycles"] = 0
        return False

    power_watts = await _get_ohme_charging_power_watts(config)
    threshold = hw_config.get(
        "ohme_charging_threshold_watts", DEFAULT_OHME_CHARGING_THRESHOLD_WATTS
    )
    above_threshold = power_watts is not None and is_charging_above_threshold(
        power_watts, threshold
    )

    previous_cycles = state.get("ohme_charging_confirm_cycles", 0)
    new_cycles, confirmed = confirm_charging_over_consecutive_cycles(
        previous_cycles, above_threshold
    )
    state["ohme_charging_confirm_cycles"] = new_cycles
    return confirmed


async def run_force_heat_check(
    config: dict[str, Any], hw_config: dict[str, Any], *, dry_run: bool, quiet: bool
) -> int:
    """Evaluate the force-heat decision and, unless dry_run, act on it.

    A legionella cycle is due to the exact same trigger as a normal
    force-heat (see determine_hotwater_decision) - the only difference is a
    minimum-interval gate: if legionella_interval_days have passed since the
    last completed cycle (_is_legionella_due), this force-heat is done as a
    legionella cycle (raised target temperature, via _start_legionella_cycle)
    instead of a normal one. There is no separate schedule or condition check
    for legionella.

    The legionella-in-progress deferral (below) and the eventual
    force_heat_activated_at/legionella state write both happen inside one
    locked_state() block spanning this whole function - not just a peek at
    the start plus a separately-locked final write - so this can never start
    a cycle while run_legionella_progress_check is still deciding/acting on
    an existing one, and vice versa. A plain read-then-later-write leaves a
    gap wide enough for MELCloud's request-then-verify retry loop (several
    seconds) to run in, letting both read "clear" before either commits.

    Returns:
        0 on success (including "no action needed"), 1 if a requested mode
        change couldn't be confirmed.

    """
    with locked_state(timeout=DEFAULT_HOTWATER_LOCK_TIMEOUT_SECONDS) as state:
        if state.get("legionella", {}).get("cycle_in_progress"):
            # Avoid the two automations fighting over target_tank_temperature/mode
            # at the same time - the legionella cycle already force-heats.
            if not quiet:
                print("Legionella cycle in progress, deferring normal force-heat check")
            return 0

        return await _run_force_heat_check_locked(config, hw_config, state, dry_run=dry_run, quiet=quiet)


async def _run_force_heat_check_locked(
    config: dict[str, Any],
    hw_config: dict[str, Any],
    state: dict[str, Any],
    *,
    dry_run: bool,
    quiet: bool,
) -> int:
    """Body of run_force_heat_check() that runs inside its locked_state() block."""
    client = MelCloudClient(config_path=get_config_path())
    try:
        await client.connect()
        status = await client.get_tank_status()

        tank_temperature = status["tank_temperature"]

        tz_name = config.get("location", {}).get("default_timezone_str", DEFAULT_TIMEZONE)
        now_local = datetime.now(tz=UTC).astimezone(pytz.timezone(tz_name))

        _update_daily_threshold_snapshot(hw_config, state, tank_temperature, now_local)

        # "Evening" spans from trigger_hour through midnight to offpeak_end.
        # Deliberately NOT is_in_offpeak_window() here: that function infers
        # whether a window wraps midnight by comparing start <= end, which is
        # right for a fixed tariff window but wrong for trigger_hour (user-
        # configurable to any hour) - a low trigger_hour (e.g. temporarily
        # lowered to test without waiting for evening) would numerically
        # satisfy start <= end and silently become a same-day-only window
        # instead of the intended always-wrapping one. is_in_evening_window()
        # always treats it as wrapping, regardless of the specific hour.
        trigger_hour = hw_config.get("trigger_hour", DEFAULT_TRIGGER_HOUR)
        trigger_time = hour_float_to_time(trigger_hour)

        # Car charging is only a trigger within its own window - see this
        # module's docstring. Outside it (including at/after trigger_time)
        # the decision switches over entirely to the battery/off-peak check
        # below.
        car_charging_window_start_time = hour_float_to_time(
            hw_config.get(
                "car_charging_trigger_start_hour", DEFAULT_CAR_CHARGING_TRIGGER_START_HOUR
            )
        )
        car_is_charging = await is_car_charging_confirmed(
            config, hw_config, state, now_local, trigger_time, car_charging_window_start_time
        )

        battery_soc, battery_soc_source = get_effective_battery_soc_percent(
            config, hw_config, now_local
        )

        offpeak_start = datetime.strptime(
            hw_config.get("offpeak_start", DEFAULT_OFFPEAK_START), "%H:%M"
        ).time()
        offpeak_end = datetime.strptime(
            hw_config.get("offpeak_end", DEFAULT_OFFPEAK_END), "%H:%M"
        ).time()
        grid_is_cheap = is_in_offpeak_window(now_local.time(), offpeak_start, offpeak_end)

        in_evening_window = is_in_evening_window(now_local.time(), trigger_time, offpeak_end)

        holiday_mode_active = is_holiday_active(state)

        # Car charging is immediate/responsive and stays on a live reading;
        # every other path (evening/battery/off-peak) is pinned to the daily
        # snapshot instead - see _update_daily_threshold_snapshot's
        # docstring. No snapshot yet for today (e.g. it's not daily_check_hour
        # yet) reads as "not below threshold" - the same safe default as an
        # unavailable live reading gets.
        daily_check = state.get("daily_check", {})
        today_str = now_local.date().isoformat()
        if car_is_charging:
            decision_tank_temperature = tank_temperature
        elif daily_check.get("date") == today_str:
            decision_tank_temperature = daily_check.get("tank_temperature_c")
        else:
            decision_tank_temperature = None

        context = HotWaterDecisionContext(
            tank_temperature_c=decision_tank_temperature,
            tank_temp_threshold_c=hw_config.get(
                "tank_temp_threshold_c", DEFAULT_TANK_TEMP_THRESHOLD_C
            ),
            car_is_charging=car_is_charging,
            battery_soc_percent=battery_soc,
            battery_soc_min_percent=hw_config.get(
                "battery_soc_min_percent", DEFAULT_BATTERY_SOC_MIN_PERCENT
            ),
            grid_is_cheap=grid_is_cheap,
            in_evening_window=in_evening_window,
            holiday_mode_active=holiday_mode_active,
        )
        decision = determine_hotwater_decision(context)

        decision_basis = "live" if car_is_charging else "6pm snapshot"
        logger.info(
            "Tank: %sC live (%sC %s) | Car charging: %s | Battery SoC: %s%% (%s) | "
            "Off-peak: %s | Decision: %s (%s)",
            tank_temperature,
            decision_tank_temperature,
            decision_basis,
            car_is_charging,
            battery_soc,
            battery_soc_source,
            grid_is_cheap,
            "FORCE HEAT" if decision.should_force_heat else "no action",
            decision.reason,
        )
        if not quiet:
            print(
                f"Tank: {tank_temperature}C live ({decision_tank_temperature}C {decision_basis}) "
                f"| Car charging: {car_is_charging} | Battery SoC: {battery_soc}% "
                f"({battery_soc_source}) | Off-peak: {grid_is_cheap}"
            )
            print(
                f"Decision: {'FORCE HEAT' if decision.should_force_heat else 'no action'} "
                f"- {decision.reason}"
            )

        if not decision.should_force_heat:
            return 0

        if status["operation_mode"] == HotWaterOperationMode.FORCE_HOT_WATER:
            # Already force-heating from an earlier run - re-requesting the
            # same mode and re-stamping force_heat_activated_at every cycle
            # would both waste MELCloud API calls and reset the
            # force_heat_max_duration_hours safety-net clock indefinitely.
            if not quiet:
                print("Already force-heating, no action needed")
            return 0

        legionella_state = state.get("legionella", {})
        legionella_due = (
            _is_legionella_due(hw_config, legionella_state)
            and daily_check.get("date") == today_str
            and daily_check.get("below_threshold") is True
        )

        if legionella_due:
            legionella_target_temp = hw_config.get(
                "legionella_target_temp_c", DEFAULT_LEGIONELLA_TARGET_TEMP_C
            )
            max_temp = status.get("target_tank_temperature_max")
            if max_temp is not None and max_temp < legionella_target_temp:
                # Can't reach the legionella target - fall through to a normal
                # force-heat below rather than blocking heating altogether;
                # it'll be re-attempted as legionella next time it's due.
                logger.error(
                    "Legionella cycle due but unit's max tank temperature (%sC) is "
                    "below the target (%sC) - doing a normal force-heat instead, "
                    "check hotwater_automation.legionella_target_temp_c",
                    max_temp,
                    legionella_target_temp,
                )
                if not quiet:
                    print(
                        f"Legionella cycle due but unit max temp {max_temp}C < "
                        f"target {legionella_target_temp}C - force-heating normally instead"
                    )
                legionella_due = False

        if legionella_due:
            return await _start_legionella_cycle(
                client, state, legionella_state, legionella_target_temp,
                dry_run=dry_run, quiet=quiet,
            )

        if dry_run:
            if not quiet:
                print("(dry run - not actually requesting mode change)")
            return 0

        success = await client.set_force_hot_water(enabled=True)
    finally:
        await client.close()

    if success:
        state["force_heat_activated_at"] = datetime.now(tz=UTC).isoformat()
        logger.info("Force hot water heating activated and confirmed")
        if not quiet:
            print("Force hot water heating activated and confirmed")
        return 0

    logger.error("Failed to confirm force hot water heating activation")
    if not quiet:
        print("Failed to confirm force hot water heating activation")
    return 1


def _update_daily_threshold_snapshot(
    hw_config: dict[str, Any],
    state: dict[str, Any],
    tank_temperature: float | None,
    now_local: datetime,
) -> None:
    """Once a day, at daily_check_hour, record whether the tank is cold.

    The force-heat trigger's own timing (car charging within its window, or
    the evening/battery/off-peak check) can land at any hour overnight -
    deciding "does the tank need heating today" from whatever it happens to
    read at that moment made the decision depend on incidental timing rather
    than the tank's actual state earlier in the day. This snapshots the
    below-threshold reading once, at a fixed hour (daily_check_hour, default
    18:00), into state["daily_check"]. Two things then key off *that*
    snapshot instead of a live reading:
    - _run_force_heat_check_locked's own decision, for every trigger path
      except car charging (which still uses a live reading - see this
      module's docstring for why that one stays responsive), and
    - the legionella_due check, exactly as before.

    Either way the actual heating (if any) is still timed by the normal
    trigger conditions, unchanged - only *whether* it happens (and, for
    legionella, whether it's upgraded) is decided from this one reading.

    A no-op once already recorded for today (state["daily_check"]["date"]
    matches), or if it's not yet check_hour - so this only ever writes once
    per day, on whichever force-heat tick (poll_interval_seconds, e.g. every
    10 minutes) first lands at or after it.
    """
    check_hour = hw_config.get("daily_check_hour", DEFAULT_DAILY_CHECK_HOUR)
    check_time = hour_float_to_time(check_hour)
    today_str = now_local.date().isoformat()

    if state.get("daily_check", {}).get("date") == today_str:
        return
    if now_local.time() < check_time:
        return

    threshold = hw_config.get("tank_temp_threshold_c", DEFAULT_TANK_TEMP_THRESHOLD_C)
    below_threshold = tank_temperature is not None and tank_temperature < threshold

    state["daily_check"] = {
        "date": today_str,
        "tank_temperature_c": tank_temperature,
        "below_threshold": below_threshold,
    }
    logger.info(
        "Daily threshold check at %s: tank %sC (threshold %sC) -> %s",
        check_time.strftime("%H:%M"),
        tank_temperature,
        threshold,
        "below threshold" if below_threshold else "at/above threshold",
    )


def _is_legionella_due(hw_config: dict[str, Any], legionella_state: dict[str, Any]) -> bool:
    """Return True if legionella_interval_days have passed since the last completed cycle.

    A missing/malformed last_completed_at (never run, or hand-edited state) is
    treated as due, the same "unknown means due" stance run_legionella_check
    took previously - it must get a chance to run at least once rather than
    being permanently blocked by bad state.
    """
    last_completed_str = legionella_state.get("last_completed_at")
    if not last_completed_str:
        return True
    try:
        last_completed = datetime.fromisoformat(last_completed_str)
    except ValueError:
        logger.error(
            "legionella last_completed_at (%r) is not a valid timestamp, treating "
            "the cycle as due",
            last_completed_str,
        )
        return True
    interval_days = hw_config.get("legionella_interval_days", DEFAULT_LEGIONELLA_INTERVAL_DAYS)
    days_since = (datetime.now(tz=UTC) - last_completed).days
    return days_since >= interval_days


async def _start_legionella_cycle(
    client: MelCloudClient,
    state: dict[str, Any],
    legionella_state: dict[str, Any],
    target_temp: float,
    *,
    dry_run: bool,
    quiet: bool,
) -> int:
    """Raise the tank's target temperature and force-heat, as a legionella cycle.

    Called in place of a normal force-heat once the shared trigger conditions
    fire and a cycle is due - see determine_hotwater_decision and
    _is_legionella_due. run_legionella_progress_check later restores the
    original target once the tank reaches it (or a safety timeout is hit).
    """
    status = await client.get_tank_status()
    original_target_temp = status["target_tank_temperature"]

    if not quiet:
        print(
            f"Tank needs heating and legionella cycle is due - raising target from "
            f"{original_target_temp}C to {target_temp}C"
        )

    if dry_run:
        if not quiet:
            print("(dry run - not actually requesting mode/temperature change)")
        return 0

    await client.set_target_tank_temperature(target_temp)
    success = await client.set_force_hot_water(enabled=True)

    if not success:
        logger.error(
            "Legionella cycle: failed to confirm force hot water heating activation - "
            "restoring original target temperature %sC",
            original_target_temp,
        )
        if not quiet:
            print(
                "Failed to start legionella cycle (mode change not confirmed) - "
                "restoring original target temperature"
            )
        # Best-effort: don't leave the tank's target raised to the legionella
        # temperature with no cycle recorded in state to ever bring it back down.
        with contextlib.suppress(Exception):
            await client.set_target_tank_temperature(original_target_temp)
        return 1

    # Merge rather than replace - keeps last_completed_at and any other field
    # a future code version adds, rather than silently discarding them - see
    # run_legionella_progress_check's identical note.
    state["legionella"] = {
        **legionella_state,
        "cycle_in_progress": True,
        "cycle_started_at": datetime.now(tz=UTC).isoformat(),
        "original_target_temp_c": original_target_temp,
        "target_temp_c": target_temp,
    }
    logger.info("Legionella cycle started (target %sC)", target_temp)
    if not quiet:
        print("Legionella cycle started")
    return 0


async def run_revert_check(hw_config: dict[str, Any], *, dry_run: bool, quiet: bool) -> int:
    """Revert to auto mode once the tank reaches temperature, or a safety limit is hit.

    Reverting only ever happens here (never from run_force_heat_check itself),
    so a force-heat window started because the car was charging is never cut
    short just because the car later stops charging (or any other trigger
    condition flips) - it always runs through to one of:
    - the tank reaching its own target_tank_temperature (the normal, expected
      way this ends), or
    - force_heat_max_duration_hours elapsing regardless (a safety net in case
      MELCloud never reports the tank as having reached target, e.g. a stuck
      sensor reading or the unit silently not heating).

    Holds the state-file lock across this whole function - not just a peek at
    the start plus a separately-locked final write - for the same reason
    run_force_heat_check does (see its docstring): a plain unlocked read
    followed by a slow MELCloud call and a much-later write leaves a window
    where this function can act on state that a concurrent
    run_force_heat_check/run_legionella_progress_check invocation has since
    changed - e.g. reverting a force-heat window that a concurrent call just
    turned into (or already finished) a legionella cycle.

    Returns:
        0 on success (including "nothing to revert" / "still heating" /
        "deferred to an in-progress legionella cycle"), 1 if a requested
        revert couldn't be confirmed.

    """
    with locked_state(timeout=DEFAULT_HOTWATER_LOCK_TIMEOUT_SECONDS) as state:
        return await _run_revert_check_locked(hw_config, state, dry_run=dry_run, quiet=quiet)


async def _run_revert_check_locked(
    hw_config: dict[str, Any], state: dict[str, Any], *, dry_run: bool, quiet: bool
) -> int:
    """Body of run_revert_check() that runs inside its locked_state() block."""
    activated_at_str = state.get("force_heat_activated_at")
    if not activated_at_str:
        if not quiet:
            print("No active force-heat window recorded, nothing to revert")
        return 0

    if state.get("legionella", {}).get("cycle_in_progress"):
        # The legionella cycle is already force-heating with its own elevated
        # target and its own revert logic (run_legionella_progress_check) -
        # reverting here on the *original* target_tank_temperature would cut
        # that cycle short, well before it reaches the raised legionella
        # target.
        if not quiet:
            print("Legionella cycle in progress, deferring to its own revert check")
        return 0

    try:
        activated_at = datetime.fromisoformat(activated_at_str)
    except ValueError:
        # Malformed state (hand-edited, partial write, etc.) - clear it rather
        # than raising the same error forever, which would leave the tank
        # force-heating indefinitely with the safety net unable to ever run.
        logger.error(
            "force_heat_activated_at (%r) is not a valid timestamp, clearing it so the "
            "safety net doesn't get stuck - check the tank's mode manually",
            activated_at_str,
        )
        if not quiet:
            print(
                "force_heat_activated_at is malformed - clearing it. "
                "Check the tank's mode manually."
            )
        state.pop("force_heat_activated_at", None)
        return 1

    max_duration_hours = hw_config.get(
        "force_heat_max_duration_hours", DEFAULT_FORCE_HEAT_MAX_DURATION_HOURS
    )
    elapsed_hours = (datetime.now(tz=UTC) - activated_at).total_seconds() / 3600.0
    timed_out = elapsed_hours >= max_duration_hours

    client = MelCloudClient(config_path=get_config_path())
    try:
        await client.connect()
        status = await client.get_tank_status()
        tank_temperature = status["tank_temperature"]
        target_temperature = status["target_tank_temperature"]

        reached_target = (
            tank_temperature is not None
            and target_temperature is not None
            and tank_temperature >= target_temperature
        )

        if not reached_target and not timed_out:
            if not quiet:
                print(
                    f"Still heating: {tank_temperature}C / {target_temperature}C "
                    f"({elapsed_hours:.1f}h elapsed, {max_duration_hours}h limit) - leaving as is"
                )
            return 0

        if reached_target:
            logger.info(
                "Tank reached target %sC (%.1fh elapsed), reverting to auto",
                target_temperature,
                elapsed_hours,
            )
            if not quiet:
                print(f"Tank reached target {target_temperature}C, reverting to auto")
        else:
            logger.warning(
                "Force-heat active for %.1fh >= %sh limit without reaching target "
                "(%sC / %sC) - reverting anyway as a safety net",
                elapsed_hours,
                max_duration_hours,
                tank_temperature,
                target_temperature,
            )
            if not quiet:
                print(
                    f"Force-heat active for {elapsed_hours:.1f}h >= {max_duration_hours}h "
                    f"limit without reaching target ({tank_temperature}C / "
                    f"{target_temperature}C) - reverting anyway"
                )

        if dry_run:
            if not quiet:
                print("(dry run - not actually requesting mode change)")
            return 0

        success = await client.set_force_hot_water(enabled=False)
    finally:
        await client.close()

    if success:
        state.pop("force_heat_activated_at", None)
        logger.info("Reverted to auto mode")
        if not quiet:
            print("Reverted to auto mode")
        return 0

    logger.error("Failed to confirm revert to auto mode")
    if not quiet:
        print("Failed to confirm revert to auto mode")
    return 1


async def run_legionella_progress_check(
    hw_config: dict[str, Any], *, dry_run: bool, quiet: bool
) -> int:
    """Check an in-progress legionella cycle and revert once done or overdue.

    Holds the state-file lock across this whole function, for the same
    reason run_revert_check and run_force_heat_check do (see their
    docstrings) - a plain unlocked read followed by a slow MELCloud call and
    a much-later write leaves a window where a concurrent invocation of one
    of those two could act on (or start) a cycle this function doesn't know
    about yet, and this function's final write would then clobber it.

    Returns:
        0 on success (including "no cycle in progress" / "still in progress"),
        1 if a requested revert couldn't be confirmed.

    """
    with locked_state(timeout=DEFAULT_HOTWATER_LOCK_TIMEOUT_SECONDS) as state:
        return await _run_legionella_progress_check_locked(hw_config, state, dry_run=dry_run, quiet=quiet)


async def _run_legionella_progress_check_locked(
    hw_config: dict[str, Any], state: dict[str, Any], *, dry_run: bool, quiet: bool
) -> int:
    """Body of run_legionella_progress_check() that runs inside its locked_state() block."""
    legionella_state = state.get("legionella", {})

    if not legionella_state.get("cycle_in_progress"):
        if not quiet:
            print("No legionella cycle in progress")
        return 0

    started_at_str = legionella_state.get("cycle_started_at")
    target_temp = legionella_state.get("target_temp_c")
    original_target_temp = legionella_state.get("original_target_temp_c")
    if started_at_str is None or target_temp is None or original_target_temp is None:
        # Malformed state (e.g. hand-edited, or written by a different code
        # version) - clear cycle_in_progress rather than raising the same
        # KeyError forever on every future check, which would leave the tank
        # stuck at the legionella target with no way to ever revert it.
        logger.error(
            "Legionella state is missing required fields (%s), clearing cycle_in_progress "
            "so it doesn't get stuck - check the tank's target temperature manually",
            legionella_state,
        )
        if not quiet:
            print(
                "Legionella state is malformed - clearing cycle_in_progress. "
                "Check the tank's target temperature manually."
            )
        state["legionella"] = {**state.get("legionella", {}), "cycle_in_progress": False}
        return 1

    try:
        started_at = datetime.fromisoformat(started_at_str)
    except ValueError:
        # Malformed timestamp (hand-edited, partial write, etc.) - clear
        # cycle_in_progress rather than raising the same error forever, which
        # would leave the tank stuck at the legionella target (and, since
        # run_force_heat_check defers all hot water automation while a
        # legionella cycle is in progress, disable force-heat/revert too).
        logger.error(
            "Legionella cycle_started_at (%r) is not a valid timestamp, clearing "
            "cycle_in_progress so it doesn't get stuck - check the tank's target "
            "temperature manually",
            started_at_str,
        )
        if not quiet:
            print(
                "Legionella cycle_started_at is malformed - clearing cycle_in_progress. "
                "Check the tank's target temperature manually."
            )
        state["legionella"] = {**state.get("legionella", {}), "cycle_in_progress": False}
        return 1

    max_duration_hours = hw_config.get(
        "legionella_max_cycle_duration_hours", DEFAULT_LEGIONELLA_MAX_CYCLE_DURATION_HOURS
    )
    elapsed_hours = (datetime.now(tz=UTC) - started_at).total_seconds() / 3600.0

    client = MelCloudClient(config_path=get_config_path())
    try:
        await client.connect()
        status = await client.get_tank_status()
        tank_temperature = status["tank_temperature"]

        # A legionella cycle is considered done as soon as the tank is
        # actually hot enough to have been disinfected - not only once it
        # reaches the (higher) target_temp the cycle originally requested
        # from MELCloud. See DEFAULT_LEGIONELLA_NATURAL_COMPLETION_TEMP_C's
        # docstring: the same threshold applies whether that heat came from
        # this cycle's own request or arrived faster than expected.
        completion_temp = hw_config.get(
            "legionella_natural_completion_temp_c", DEFAULT_LEGIONELLA_NATURAL_COMPLETION_TEMP_C
        )
        reached_target = tank_temperature is not None and tank_temperature >= completion_temp
        timed_out = elapsed_hours >= max_duration_hours

        if not reached_target and not timed_out:
            if not quiet:
                print(
                    f"Legionella cycle in progress: {tank_temperature}C / {completion_temp}C "
                    f"disinfection threshold ({target_temp}C requested target, "
                    f"{elapsed_hours:.1f}h elapsed)"
                )
            return 0

        if timed_out and not reached_target:
            logger.warning(
                "Legionella cycle timed out after %.1fh without reaching the %sC "
                "disinfection threshold (currently %sC, %sC requested target) - "
                "reverting without marking complete, will retry next due check",
                elapsed_hours,
                completion_temp,
                tank_temperature,
                target_temp,
            )
            if not quiet:
                print(
                    f"Legionella cycle timed out at {tank_temperature}C "
                    f"(disinfection threshold {completion_temp}C) - reverting, will retry later"
                )
        else:
            logger.info(
                "Legionella cycle: tank at %sC reached the %sC disinfection threshold "
                "(%sC requested target), reverting",
                tank_temperature,
                completion_temp,
                target_temp,
            )
            if not quiet:
                print(f"Legionella cycle reached {tank_temperature}C, reverting")

        if dry_run:
            if not quiet:
                print("(dry run - not actually reverting)")
            return 0

        await client.set_target_tank_temperature(original_target_temp)
        success = await client.set_force_hot_water(enabled=False)
    finally:
        await client.close()

    if not success:
        logger.error("Legionella cycle: failed to confirm revert to auto mode")
        if not quiet:
            print("Failed to confirm revert after legionella cycle")
        return 1

    # Merge rather than replace - keeps any fields a future code version adds
    # to "legionella" that this function doesn't know about, rather than
    # silently discarding them.
    state["legionella"] = {
        **state.get("legionella", {}),
        "cycle_in_progress": False,
        "last_completed_at": (
            datetime.now(tz=UTC).isoformat()
            if reached_target
            else state.get("legionella", {}).get("last_completed_at")
        ),
    }
    logger.info("Legionella cycle reverted (reached_target=%s)", reached_target)
    if not quiet:
        print("Reverted after legionella cycle")
    return 0


async def run_legionella_natural_completion_check(
    hw_config: dict[str, Any], *, dry_run: bool, quiet: bool
) -> int:
    """Mark the legionella interval satisfied if the tank is hot enough on its own.

    The tank's MELCloud sensor reads the same physical water no matter what
    put the heat there - the ASHP, an immersion, or (not otherwise visible to
    this project) an off-grid solar diverter. A reading at or above
    legionella_natural_completion_temp_c on any day satisfies that day's
    disinfection requirement exactly as a completed forced cycle would,
    resetting the legionella_interval_days clock from the moment of that
    reading.

    Unlike run_revert_check and run_legionella_progress_check, this doesn't
    gate on any prior state (an active force-heat window, a cycle already in
    progress) - a quiet day with no automation activity at all is exactly
    the case (solar-heated tank) this exists to catch, so it always takes
    its own live reading.

    Returns:
        0 always - there's nothing here that can fail to "confirm", just a
        temperature reading and, at most, a state write.

    """
    with locked_state(timeout=DEFAULT_HOTWATER_LOCK_TIMEOUT_SECONDS) as state:
        return await _run_legionella_natural_completion_check_locked(
            hw_config, state, dry_run=dry_run, quiet=quiet
        )


async def _run_legionella_natural_completion_check_locked(
    hw_config: dict[str, Any], state: dict[str, Any], *, dry_run: bool, quiet: bool
) -> int:
    """Body of run_legionella_natural_completion_check() inside its locked_state() block."""
    completion_temp = hw_config.get(
        "legionella_natural_completion_temp_c", DEFAULT_LEGIONELLA_NATURAL_COMPLETION_TEMP_C
    )

    client = MelCloudClient(config_path=get_config_path())
    try:
        await client.connect()
        status = await client.get_tank_status()
    finally:
        await client.close()

    tank_temperature = status["tank_temperature"]
    if tank_temperature is None or tank_temperature < completion_temp:
        if not quiet:
            print(f"Tank at {tank_temperature}C, below {completion_temp}C - nothing to record")
        return 0

    legionella_state = state.get("legionella", {})
    last_completed_str = legionella_state.get("last_completed_at")
    today = datetime.now(tz=UTC).date()
    if last_completed_str:
        try:
            if datetime.fromisoformat(last_completed_str).date() == today:
                # Already recorded today - avoid a redundant write/log every
                # time this runs while the tank happens to stay hot.
                return 0
        except ValueError:
            pass  # Malformed - fall through and overwrite with a good value.

    if dry_run:
        if not quiet:
            print(
                f"Tank at {tank_temperature}C >= {completion_temp}C - would mark legionella "
                "satisfied (dry run)"
            )
        return 0

    state["legionella"] = {
        **legionella_state,
        "last_completed_at": datetime.now(tz=UTC).isoformat(),
    }
    logger.info(
        "Tank observed at %sC (>= %sC disinfection threshold) with no legionella cycle "
        "necessarily involved - marking the legionella requirement satisfied, resetting "
        "the interval",
        tank_temperature,
        completion_temp,
    )
    if not quiet:
        print(f"Tank at {tank_temperature}C - legionella satisfied naturally, interval reset")
    return 0
