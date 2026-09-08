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
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, TypedDict

import pytz

from src.api_clients.melcloud_client import HotWaterOperationMode, MelCloudClient
from src.api_clients.melcloud_status_cache import write_status_cache as write_melcloud_status_cache
from src.api_clients.ohme_ev_client import OhmeEVClient
from src.api_clients.ohme_status_cache import read_fresh_status
from src.api_clients.solax_modbus_client import solax_modbus_soc
from src.config_manager.config_manager import get_hotwater_melcloud_config_error
from src.core_logic.battery_evening_prediction_logic import predict_evening_soc
from src.core_logic.hotwater_decision_logic import (
    HotWaterDecisionContext,
    _battery_prediction_eligibility_end_hour,
    _daily_check_lookup_date_str,
    _is_legionella_due,
    _overnight_deadline_passed,
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


# --- hotwater_automation_state.json shape (pure static typing - the file is
# still plain JSON at runtime, read/written as ordinary dicts; these exist so
# an editor/type-checker can catch a typo'd key or wrong value type at the
# many call sites that read this state, without requiring every field to be
# present - a fresh state file starts as {} and each section is added lazily
# by whichever check first needs it). -------------------------------------


class DailyCheckState(TypedDict, total=False):
    """state["daily_check"] - the once-a-day 6pm tank-temperature snapshot."""

    date: str
    tank_temperature_c: float
    below_threshold: bool


class LegionellaState(TypedDict, total=False):
    """state["legionella"] - in-progress/last-completed legionella cycle tracking."""

    cycle_in_progress: bool
    cycle_started_at: str
    target_temp_c: float
    original_target_temp_c: float
    last_completed_at: str | None
    due_warning_sent_for: str


class HolidayState(TypedDict, total=False):
    """state["holiday"] - written by scripts/holiday_mode.py."""

    until: str


class ServiceModeState(TypedDict, total=False):
    """state["service_mode"] - written by scripts/service_mode.py."""

    active: bool


class NormalTargetMismatchAlert(TypedDict):
    """state["normal_target_mismatch_alerted_for"] - see _alert_normal_target_mismatch.

    Both fields, not just "actual" alone (confirmed 2026-09-08) - deduping
    on actual alone would permanently swallow a genuinely new mismatch that
    happens to reuse a previously-alerted actual value.
    """

    actual: float
    expected: float


class HotWaterAutomationState(TypedDict, total=False):
    """The full shape of hotwater_automation_state.json, as read_state()/locked_state() return it."""

    daily_check: DailyCheckState
    legionella: LegionellaState
    holiday: HolidayState
    service_mode: ServiceModeState
    force_heat_activated_at: str | None
    ohme_charging_confirm_cycles: int
    normal_target_mismatch_alerted_for: NormalTargetMismatchAlert


DEFAULT_TANK_TEMP_THRESHOLD_C = 45.0
# Minimum charge BOTH batteries (not their average) must independently clear
# - see get_battery_soc_percent's docstring for why this is a minimum, not an
# average.
DEFAULT_BATTERY_SOC_MIN_PERCENT = 20.0
DEFAULT_OFFPEAK_START = "23:30"
# DEFAULT_OFFPEAK_END lives in hotwater_decision_logic.py (single source of
# truth, 2026-09-08) - imported below alongside the other names from there.
DEFAULT_TRIGGER_HOUR = 21.5  # 9:30pm - fractional hours are supported (e.g. 21.5 = 21:30)
# Battery-prediction trigger path (get_battery_prediction_to_deadline) - an
# independent alternative to trigger_hour/car-charging, active across this
# wider evening span. Deadline defaults to offpeak_start (23:30/11:30pm): the
# moment the grid's off-peak window opens anyway, so a prediction that both
# batteries will still clear battery_soc_min_percent by then means it's safe
# to heat from stored solar any time before that, without waiting on
# trigger_hour first. Deliberately starts at 6pm, not car_charging_trigger_
# start_hour's 3pm - between 3pm and this hour, only car charging may trigger
# a heat; the battery's state is irrelevant in that narrower window, by
# design (confirmed 2026-09-07 - see config.yaml's own comment on this key).
DEFAULT_BATTERY_PREDICTION_WINDOW_START_HOUR = 18.0  # 6pm
# DEFAULT_BATTERY_PREDICTION_DEADLINE_HOUR (11:30pm) lives in
# hotwater_decision_logic.py - imported below.
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
# What THIS PROJECT expects a normal (non-legionella) heat's target to be -
# confirmed 2026-09-08. Deliberately a value this code owns and compares
# against directly, not just trusted from MELCloud's own reported
# target_tank_temperature: the unit's own configured target lives entirely
# outside this codebase (set via the app), so nothing previously noticed if
# it ever drifted from what's actually intended - see run_revert_check's own
# docstring for how a mismatch is now surfaced rather than either silently
# trusted or silently overridden.
DEFAULT_NORMAL_TARGET_TEMP_C = 50.0
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
# Same hard safety-net cap as DEFAULT_FORCE_HEAT_MAX_DURATION_HOURS, applied
# to a legionella cycle instead of a normal force-heat - see that constant's
# docstring.
DEFAULT_LEGIONELLA_MAX_CYCLE_DURATION_HOURS = 1.0
# How many days before a legionella cycle becomes due (legionella_interval_days
# since the last completed one) to send a warning email - see
# check_legionella_due_warning.
DEFAULT_LEGIONELLA_DUE_WARNING_DAYS = 7
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
# run_safety_ceiling_check's own last-resort limits - deliberately separate
# constants from DEFAULT_FORCE_HEAT_MAX_DURATION_HOURS/
# DEFAULT_LEGIONELLA_MAX_CYCLE_DURATION_HOURS above, not a reuse of them.
# Those are the primary decision logic's own tunable duration limits; this
# is an independent backstop that assumes the primary logic's bookkeeping
# could itself be wrong (that's the whole point of it), so its duration
# limit deliberately never reads or is defined in terms of those values -
# set well above every normal operating duration, so it should essentially
# never fire on duration alone in normal operation.
#
# The temperature ceiling is different: confirmed 2026-09-07, "the water
# shouldn't be heated above 55 degrees" IS the household's actual absolute
# limit (a "proper"/officially-supported legionella cycle can never be
# triggered on this hardware, so there's no legitimate reason to ever need
# more) - this deliberately DOES equal DEFAULT_LEGIONELLA_TARGET_TEMP_C/
# DEFAULT_LEGIONELLA_NATURAL_COMPLETION_TEMP_C (both 55.0) rather than
# sitting safely above them. See run_safety_ceiling_check's own docstring
# for how it still avoids alarming on every routine legionella completion
# despite that.
DEFAULT_SAFETY_CEILING_TEMP_C = 55.0
DEFAULT_SAFETY_MAX_DURATION_HOURS = 3.0
# How long a freshly-set force_heat_activated_at may legitimately still read
# as "not yet FORCE_HOT_WATER" before _run_force_heat_check_locked's dangling-
# marker cleanup is allowed to treat that as genuine staleness rather than
# ordinary MELCloud propagation delay. Comfortably above
# melcloud.mode_change_retry's own documented worst case (max_attempts *
# check_delay_seconds, ~60s at its defaults) - not tied to that config value
# directly, since this must stay safe even if that retry budget is tuned up
# later.
MODE_CHANGE_GRACE_SECONDS = 120.0
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


def read_state() -> HotWaterAutomationState:
    """Read the hot water automation state file, or {} if absent/unreadable."""
    return read_json_state(get_hotwater_automation_state_path())


def locked_state(timeout: float = 10.0) -> contextlib.AbstractContextManager[HotWaterAutomationState]:
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
    """Get the lower of the master/slave inverters' live SoC, or None if unavailable.

    A minimum, not an average: hotwater_automation.battery_soc_min_percent is
    meant as "both batteries have this much spare charge", not "the pair does
    on average" - an average can clear the threshold while one battery is
    already low, which this is meant to catch. Reads live via the same
    solax_modbus_soc() function battery_mode_daemon.py itself uses - a
    read-only Modbus call, safe to run independently alongside the battery
    daemon without touching or coordinating with it.
    """
    soc_data = solax_modbus_soc(config)
    if soc_data is None:
        return None
    return min(soc_data["master"], soc_data["slave"])


# _battery_prediction_eligibility_end_hour moved to
# src/core_logic/hotwater_decision_logic.py (2026-09-08, see this module's
# own architectural review) - it's pure (plain data in, plain data out, no
# I/O), so it belongs alongside determine_hotwater_decision. Imported back in
# below; every call site here is unchanged.


def get_battery_prediction_to_deadline(
    config: dict[str, Any], hw_config: dict[str, Any], now_local: datetime, deadline_hour: float
) -> tuple[float | None, str]:
    """Predict the lower of master/slave SoC at deadline_hour, from right now.

    Powers the battery-prediction trigger path (see
    HotWaterDecisionContext.battery_prediction_trigger_active) - unlike
    get_battery_soc_percent's live minimum (used by the evening/off-peak
    path), this looks forward to whether stored solar will *still* be there
    by deadline_hour (by default hotwater_automation.offpeak_start, 11:30pm -
    the moment the grid's off-peak window opens anyway), so heating can start
    earlier than trigger_hour whenever that's forecast to hold.

    Runs src.core_logic.battery_evening_prediction_logic.predict_evening_soc
    once per battery, each anchored to its own *current* live reading with a
    horizon shrinking as the day goes on (deadline_hour - now) - not a
    genuinely independent per-battery model (data/solax_historical_data.json
    only ever logs the master inverter's SoC via the SolaX cloud API, so
    there's no slave-specific historical drift to train against), but this
    still captures "the pair drains together, so apply the same
    historically-typical drift to each battery's own current level" rather
    than shifting a single averaged prediction, which is what a plain
    average-based check would give.

    Returns:
        (predicted_min_percent, reason) - predicted_min_percent is None if
        the live SoC or historical data couldn't be read, or there wasn't
        enough historical data for either battery to predict (the caller
        should then simply not treat this path as active, the same as any
        other "prediction unavailable" case elsewhere in this module).

    """
    deadline_str = hour_float_to_time(deadline_hour).strftime("%H:%M")
    now_hour_float = now_local.hour + now_local.minute / 60.0
    horizon_hours = deadline_hour - now_hour_float
    if horizon_hours <= 0:
        return None, f"Already at/past the {deadline_str} deadline, nothing to predict"

    soc_data = solax_modbus_soc(config)
    if soc_data is None:
        return None, "Could not read live battery SoC, cannot predict"

    historical_records = load_historical_records()
    if not historical_records:
        return None, "Could not load historical SoC data, cannot predict"

    min_sample_days = hw_config.get(
        "battery_prediction_min_sample_days",
        config.get("battery_evening_prediction", {}).get("min_sample_days", 5),
    )
    reference_day_of_year = now_local.timetuple().tm_yday

    predicted_values = []
    for label, current_soc in (("master", soc_data["master"]), ("slave", soc_data["slave"])):
        result = predict_evening_soc(
            current_soc_percent=current_soc,
            historical_records=historical_records,
            trigger_hour=now_hour_float,
            horizon_hours=horizon_hours,
            reference_day_of_year=reference_day_of_year,
            min_sample_days=min_sample_days,
        )
        if result.predicted_soc_percent is None:
            return None, f"{label} battery: {result.reason}"
        predicted_values.append((label, result.predicted_soc_percent))

    predicted_min_label, predicted_min_percent = min(predicted_values, key=lambda pair: pair[1])
    return (
        predicted_min_percent,
        (
            f"Predicted {predicted_min_label} battery at {predicted_min_percent:.1f}% "
            f"by {deadline_str} (lower of master/slave predictions)"
        ),
    )


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


def is_service_mode_active(state: dict[str, Any]) -> bool:
    """Whether scripts/service_mode.py's engineer-control pause is currently active.

    Unlike holiday mode, service mode has no expiry timestamp to check - it's
    a plain boolean written by --start and cleared by --cancel, since there's
    no equivalent of holiday_mode.py's --start-days N (an engineer visit
    doesn't have a predictable duration to count down). A missing/falsy
    state["service_mode"]["active"] means service mode has no effect, the
    safe default.
    """
    return bool(state.get("service_mode", {}).get("active", False))


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

        # Free byproduct of this check's own fetch, not a new API call - see
        # melcloud_status_cache.py's module docstring for why this is the
        # one write site that keeps the dashboard's cache fresh, rather than
        # the dashboard making its own separate MELCloud call every poll.
        with contextlib.suppress(Exception):
            write_melcloud_status_cache(status)

        tank_temperature = status["tank_temperature"]

        tz_name = config.get("location", {}).get("default_timezone_str", DEFAULT_TIMEZONE)
        now_local = datetime.now(tz=UTC).astimezone(pytz.timezone(tz_name))

        _update_daily_threshold_snapshot(hw_config, state, tank_temperature, now_local)

        # A dangling force_heat_activated_at with the tank not actually
        # force-heating (per this live read) and already proven warm enough
        # is what's left after run_safety_ceiling_check's independent,
        # one-way cutoff - it never touches this state file, so nothing else
        # clears this marker or corrects the (now stale) daily snapshot until
        # this tick or the next run_revert_check does. Clean both up here,
        # before the decision below, so this same tick can't immediately
        # re-trigger off state that's about to be shown stale.
        #
        # Deliberately narrow: gated on a dangling force_heat_activated_at,
        # NOT on "the live tank happens to be warm" alone - a plain warm
        # reading with no dangling activation must NOT correct the pinned
        # snapshot (see test_hotwater_legionella_eligibility_snapshot.py's
        # test_snapshot_is_only_taken_once_per_day_and_the_pinned_reading_
        # still_drives_the_decision - the daily pin is deliberately immune to
        # incidental live readings from an unrelated source, e.g. solar).
        #
        # Also gated on the marker being older than MODE_CHANGE_GRACE_SECONDS
        # (regression found 2026-09-07: a freshly-activated force-heat - this
        # tick's own live status read landing inside MELCloud's normal
        # request-then-verify propagation delay, up to mode_change_retry's
        # own worst case of ~60s - still legitimately reads as "not yet
        # FORCE_HOT_WATER" for a few seconds after activation. Without this
        # grace period, that ordinary delay looked identical to a genuinely
        # dangling marker and got cleaned up here, which then made
        # run_safety_ceiling_check's own "can't tell how long this has been
        # running" fail-safe treat the resulting missing timestamp as a
        # duration violation and revert a heating window that had only just
        # legitimately started - the exact "two safety mechanisms fighting
        # each other" failure mode this whole design is supposed to avoid).
        # A malformed (unparseable) timestamp is left alone here too - that's
        # run_revert_check's own error-recovery path to handle, not this one.
        force_heat_activated_at_str = state.get("force_heat_activated_at")
        activated_at_age_seconds: float | None = None
        if force_heat_activated_at_str:
            with contextlib.suppress(ValueError):
                activated_at_age_seconds = (
                    datetime.now(tz=UTC) - datetime.fromisoformat(force_heat_activated_at_str)
                ).total_seconds()

        if (
            status["operation_mode"] != HotWaterOperationMode.FORCE_HOT_WATER
            and activated_at_age_seconds is not None
            and activated_at_age_seconds >= MODE_CHANGE_GRACE_SECONDS
        ):
            state.pop("force_heat_activated_at", None)
            _refresh_daily_snapshot_if_warm(hw_config, state, tank_temperature, now_local)

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

        # Battery-prediction path (get_battery_prediction_to_deadline) - an
        # independent, wider (afternoon-through-evening) alternative to
        # trigger_hour/car-charging. Not is_in_evening_window(): this window
        # doesn't wrap midnight, so the plain start<=end<=current check is
        # correct and simpler here.
        battery_prediction_window_start_time = hour_float_to_time(
            hw_config.get(
                "battery_prediction_window_start_hour",
                DEFAULT_BATTERY_PREDICTION_WINDOW_START_HOUR,
            )
        )
        battery_prediction_deadline_hour = hw_config.get(
            "battery_prediction_deadline_hour", DEFAULT_BATTERY_PREDICTION_DEADLINE_HOUR
        )
        # The window this path may still START a new heat in can close
        # earlier than the deadline it predicts TOWARDS - see
        # _battery_prediction_eligibility_end_hour's own docstring
        # (forced_discharge_start_hour).
        battery_prediction_eligibility_end_time = hour_float_to_time(
            _battery_prediction_eligibility_end_hour(hw_config)
        )
        in_battery_prediction_window = is_in_offpeak_window(
            now_local.time(), battery_prediction_window_start_time, battery_prediction_eligibility_end_time
        )

        battery_soc_min_percent = hw_config.get(
            "battery_soc_min_percent", DEFAULT_BATTERY_SOC_MIN_PERCENT
        )
        battery_prediction_trigger_active = False
        battery_prediction_reason = "Outside the battery-prediction window"
        if in_battery_prediction_window:
            predicted_min, battery_prediction_reason = get_battery_prediction_to_deadline(
                config, hw_config, now_local, battery_prediction_deadline_hour
            )
            battery_prediction_trigger_active = (
                predicted_min is not None and predicted_min >= battery_soc_min_percent
            )

        holiday_mode_active = is_holiday_active(state)
        service_mode_active = is_service_mode_active(state)

        # Car charging and the battery-prediction path are both immediate/
        # responsive and stay on a live reading; every other path (evening/
        # battery/off-peak) is pinned to the daily snapshot instead - see
        # _update_daily_threshold_snapshot's docstring. No snapshot yet for
        # today (e.g. it's not daily_check_hour yet - true for the whole
        # start of the battery-prediction window, which opens before the
        # default daily_check_hour) reads as "not below threshold" - the same
        # safe default as an unavailable live reading gets.
        daily_check = state.get("daily_check", {})
        # _daily_check_lookup_date_str, not a plain now_local.date() - a
        # decision made between midnight and offpeak_end is still part of
        # LAST evening's session and must still find that snapshot (see its
        # own docstring for the real gap this closes).
        today_str = _daily_check_lookup_date_str(hw_config, now_local)
        if car_is_charging or battery_prediction_trigger_active:
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
            battery_soc_min_percent=battery_soc_min_percent,
            grid_is_cheap=grid_is_cheap,
            in_evening_window=in_evening_window,
            holiday_mode_active=holiday_mode_active,
            service_mode_active=service_mode_active,
            battery_prediction_trigger_active=battery_prediction_trigger_active,
        )
        decision = determine_hotwater_decision(context)

        decision_basis = (
            "live" if (car_is_charging or battery_prediction_trigger_active) else "6pm snapshot"
        )
        logger.info(
            "Tank: %sC live (%sC %s) | Car charging: %s | Battery SoC: %s%% (%s) | "
            "Battery prediction: %s | Off-peak: %s | Holiday: %s | Service mode: %s | "
            "Decision: %s (%s)",
            tank_temperature,
            decision_tank_temperature,
            decision_basis,
            car_is_charging,
            battery_soc,
            battery_soc_source,
            battery_prediction_reason,
            grid_is_cheap,
            holiday_mode_active,
            service_mode_active,
            "FORCE HEAT" if decision.should_force_heat else "no action",
            decision.reason,
        )
        if not quiet:
            print(
                f"Tank: {tank_temperature}C live ({decision_tank_temperature}C {decision_basis}) "
                f"| Car charging: {car_is_charging} | Battery SoC: {battery_soc}% "
                f"({battery_soc_source}) | Battery prediction: {battery_prediction_reason} "
                f"| Off-peak: {grid_is_cheap}"
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


# _daily_check_lookup_date_str moved to
# src/core_logic/hotwater_decision_logic.py (2026-09-08, see this module's
# own architectural review) - pure, no I/O. Imported back in below; every
# call site here is unchanged.


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


def _refresh_daily_snapshot_if_warm(
    hw_config: dict[str, Any],
    state: dict[str, Any],
    tank_temperature: float | None,
    now_local: datetime,
) -> None:
    """Correct today's daily-check snapshot the moment a live reading proves it stale.

    _update_daily_threshold_snapshot deliberately writes state["daily_check"]
    only once per day, at daily_check_hour - see its own docstring for why. The
    problem: that snapshot is what every non-live force-heat trigger path
    decides from for the rest of the day, so it never reflects reality again
    once the tank is actually heated - a completed force-heat/legionella cycle
    (or this module's own safety-ceiling backstop in run_safety_ceiling_check,
    which deliberately never writes state at all) would otherwise leave the
    very next check still reading "below threshold" from hours earlier and
    re-triggering another cycle immediately. That is the Friday-night
    reheat/revert loop this function exists to prevent.

    Deliberately narrow and one-directional to avoid reintroducing the
    incidental-timing problem _update_daily_threshold_snapshot was written to
    solve:
    - Only ever corrects an EXISTING today's snapshot, never creates one early
      - if daily_check_hour hasn't run yet today, this is a no-op, so a warm
        reading at (say) 9am can't freeze "at/above threshold" for the whole
        day if the tank happens to be cold again by the real daily_check_hour
        reading.
      - Only ever moves the snapshot from below-threshold to at/above-threshold
        on live proof of warmth, never the reverse - a live reading that's
        merely cold doesn't get to override an existing at/above-threshold
        snapshot the same day.
    """
    if tank_temperature is None:
        return
    threshold = hw_config.get("tank_temp_threshold_c", DEFAULT_TANK_TEMP_THRESHOLD_C)
    if tank_temperature < threshold:
        return
    daily_check = state.get("daily_check", {})
    today_str = _daily_check_lookup_date_str(hw_config, now_local)
    if daily_check.get("date") != today_str or daily_check.get("below_threshold") is not True:
        return
    state["daily_check"] = {**daily_check, "tank_temperature_c": tank_temperature, "below_threshold": False}
    logger.info(
        "Daily threshold snapshot corrected: live reading %sC >= threshold %sC after an earlier "
        "below-threshold snapshot - marking today at/above threshold to avoid re-triggering",
        tank_temperature,
        threshold,
    )


# _is_legionella_due moved to src/core_logic/hotwater_decision_logic.py
# (2026-09-08, see this module's own architectural review) - pure, no I/O
# (its one logger.error call is a validation warning, not a side effect that
# needed this module's own logger setup). Imported back in below; every call
# site here is unchanged.


def check_legionella_due_warning(
    config: dict[str, Any], hw_config: dict[str, Any], *, dry_run: bool = False, quiet: bool = False
) -> int:
    """Send a warning email once a legionella cycle is within legionella_due_warning_days of due.

    Purely a heads-up, sent well before _is_legionella_due would actually
    start upgrading a force-heat to a legionella cycle - no MELCloud call, no
    effect on the automation itself. Sends at most once per interval: stamps
    state["legionella"]["due_warning_sent_for"] with the last_completed_at
    value the warning was raised against, so a re-run later in the same
    interval (e.g. every poll_interval_seconds) doesn't spam the same email
    repeatedly. That stamp is naturally invalidated the next time a cycle
    actually completes (last_completed_at changes), so the next interval's
    approaching-due warning fires normally.

    A never-yet-completed cycle (legionella_state has no last_completed_at)
    is skipped rather than warned about - _is_legionella_due already treats
    that as immediately due, so there's no "coming due in N days" state to
    warn about; it'll simply run at the next opportunity.

    Returns:
        0 always - a failed/skipped/disabled email must never be treated as
        an error by a caller (e.g. the daemon's own "never raises" checks).

    """
    email_config = config.get("email", {})
    if not email_config.get("enabled", False):
        if not quiet:
            print("Email is disabled (email.enabled: false), skipping legionella-due warning")
        return 0

    with locked_state(timeout=DEFAULT_HOTWATER_LOCK_TIMEOUT_SECONDS) as state:
        legionella_state = state.get("legionella", {})
        last_completed_str = legionella_state.get("last_completed_at")
        if not last_completed_str:
            if not quiet:
                print("No legionella cycle ever completed yet - nothing to warn about")
            return 0

        try:
            last_completed = datetime.fromisoformat(last_completed_str)
        except ValueError:
            if not quiet:
                print("legionella.last_completed_at is malformed - skipping due warning")
            return 0

        interval_days = hw_config.get(
            "legionella_interval_days", DEFAULT_LEGIONELLA_INTERVAL_DAYS
        )
        warning_days = hw_config.get(
            "legionella_due_warning_days", DEFAULT_LEGIONELLA_DUE_WARNING_DAYS
        )
        days_since = (datetime.now(tz=UTC) - last_completed).days
        days_until_due = interval_days - days_since

        if not (0 < days_until_due <= warning_days):
            if not quiet:
                print(f"Legionella cycle due in {days_until_due} day(s) - not yet within warning window")
            return 0

        if legionella_state.get("due_warning_sent_for") == last_completed_str:
            if not quiet:
                print("Legionella due-soon warning already sent for this interval")
            return 0

        subject = "Hot water: legionella cycle due soon"
        body = (
            f"A legionella disinfection cycle will become due in approximately "
            f"{days_until_due} day(s) (last completed {last_completed:%d %B %Y}, "
            f"{interval_days}-day interval).\n\n"
            "It will run automatically the next time the usual force-heat "
            "conditions are met, unless holiday mode or service mode is active."
        )

        if dry_run:
            if not quiet:
                print(f"(dry run) would send: {subject}")
            return 0

        if send_email(config, subject, body):
            state["legionella"] = {**legionella_state, "due_warning_sent_for": last_completed_str}
            logger.info("Sent legionella-due-soon warning email (%s day(s) until due)", days_until_due)
            if not quiet:
                print(f"Sent legionella-due-soon warning email ({days_until_due} day(s) until due)")
        elif not quiet:
            print("Failed to send legionella-due-soon warning email (see logs above)")

    return 0


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


# _overnight_deadline_passed moved to
# src/core_logic/hotwater_decision_logic.py (2026-09-08, see this module's
# own architectural review) - pure, no I/O, already had its own doctests
# there. Imported back in below; every call site here is unchanged.


def _alert_insufficient_duration(
    config: dict[str, Any],
    *,
    kind: str,
    tank_temperature: float | None,
    target_temperature: float | None,
    elapsed_hours: float,
    max_duration_hours: float,
    config_key: str,
    dry_run: bool,
    quiet: bool,
) -> None:
    """Log and email that a heating window ended without reaching target.

    Called from run_revert_check/run_legionella_progress_check's own
    "timed out without reaching target" branches, right where that's
    discovered - both already log a warning of their own with the specific
    deadline-vs-duration detail; this adds one more, deliberately identical
    each time (grep-able as "INSUFFICIENT_DURATION"), plus an email.

    Sent on EVERY occurrence, deliberately with no dedupe (unlike
    check_legionella_due_warning's once-per-interval stamp): the whole point
    is to let hotwater_automation.<config_key> be tuned from real frequency
    data - if it's raised only after seeing how often this actually happens,
    every occurrence needs to be visible, not just the first.
    """
    logger.warning(
        "INSUFFICIENT_DURATION: %s heating window ended without reaching target "
        "(%sC / %sC) after %.1fh (limit %sh, hotwater_automation.%s) - consider raising "
        "it if this keeps happening",
        kind,
        tank_temperature,
        target_temperature,
        elapsed_hours,
        max_duration_hours,
        config_key,
    )

    if dry_run:
        if not quiet:
            print(f"(dry run) would send 'heating window insufficient' alert email ({kind})")
        return

    subject = f"Hot water: {kind} did not reach target in time"
    body = (
        f"The {kind} heating window ended after {elapsed_hours:.1f}h (limit "
        f"{max_duration_hours}h) without reaching target: tank at {tank_temperature}C, "
        f"target {target_temperature}C.\n\n"
        f"If this keeps happening, consider raising hotwater_automation.{config_key} in "
        "config.yaml.\n\n"
        "This is only a heads-up - the automation has already reverted safely and will "
        "retry at the next opportunity."
    )
    if send_email(config, subject, body):
        if not quiet:
            print(f"Sent 'heating window insufficient' alert email ({kind})")
    elif not quiet:
        print(f"Failed to send 'heating window insufficient' alert email ({kind}) - see logs above")


def _notify_legionella_completed(
    config: dict[str, Any],
    hw_config: dict[str, Any],
    *,
    tank_temperature: float | None,
    completed_at: datetime,
    source: str,
    dry_run: bool,
    quiet: bool,
) -> None:
    """Log and email that a legionella cycle has just completed.

    Called from both places last_completed_at gets newly set to "now" -
    _run_legionella_progress_check_locked (a forced cycle reaching its
    disinfection threshold) and _run_legionella_natural_completion_check_locked
    (the tank observed hot enough on its own, no cycle involved) - each only
    calls this on an actual fresh completion, so no extra dedupe is needed
    here (unlike check_legionella_due_warning's once-per-interval stamp).

    States the next-due date directly in this email rather than firing a
    second, separate "next due" notification - check_legionella_due_warning
    already re-derives "due in <=legionella_due_warning_days" independently
    each interval, so a second immediate email here would just be a ~90-day-
    early duplicate of no practical use.
    """
    interval_days = hw_config.get("legionella_interval_days", DEFAULT_LEGIONELLA_INTERVAL_DAYS)
    next_due = completed_at + timedelta(days=interval_days)
    logger.info(
        "Legionella cycle completed (%s) at %sC - next due around %s",
        source,
        tank_temperature,
        next_due.date().isoformat(),
    )

    if dry_run:
        if not quiet:
            print(f"(dry run) would send 'legionella cycle completed' email ({source})")
        return

    subject = "Hot water: legionella cycle completed"
    body = (
        f"A legionella disinfection cycle has completed ({source}), tank observed at "
        f"{tank_temperature}C on {completed_at:%d %B %Y}.\n\n"
        f"The next cycle will become due around {next_due:%d %B %Y} "
        f"({interval_days}-day interval) - you'll get a separate heads-up "
        f"{hw_config.get('legionella_due_warning_days', DEFAULT_LEGIONELLA_DUE_WARNING_DAYS)} "
        "day(s) before then."
    )
    if send_email(config, subject, body):
        if not quiet:
            print(f"Sent 'legionella cycle completed' email ({source})")
    elif not quiet:
        print(f"Failed to send 'legionella cycle completed' email ({source}) - see logs above")


def _elapsed_hours_and_deadline_passed(
    hw_config: dict[str, Any], started_at: datetime, now: datetime, tz: pytz.BaseTzInfo
) -> tuple[float, bool]:
    """Shared by run_revert_check/run_legionella_progress_check: how long a
    heating window has been running, and whether the overnight completion
    deadline (offpeak_end) has passed since it started - see
    _overnight_deadline_passed. Pulled out specifically because this was
    previously implemented twice, identically - see this module's own
    architectural review (2026-09-08) for why that duplication mattered.
    """
    elapsed_hours = (now - started_at).total_seconds() / 3600.0
    offpeak_end_time = datetime.strptime(
        hw_config.get("offpeak_end", DEFAULT_OFFPEAK_END), "%H:%M"
    ).time()
    deadline_passed = _overnight_deadline_passed(started_at.astimezone(tz), now.astimezone(tz), offpeak_end_time)
    return elapsed_hours, deadline_passed


def _decide_heating_window_outcome(
    config: dict[str, Any],
    *,
    kind: str,
    tank_temperature: float | None,
    completion_temp: float | None,
    elapsed_hours: float,
    deadline_passed: bool,
    max_duration_hours: float,
    duration_config_key: str,
    dry_run: bool,
    quiet: bool,
) -> bool | None:
    """Shared by run_revert_check/run_legionella_progress_check: decide
    whether a heating window is done (reached its completion temperature, or
    timed out) and log/alert accordingly - the ~60% of both functions that
    was previously implemented twice, identically apart from wording (see
    this module's own architectural review, 2026-09-08). What differs
    between the two callers - which temperature counts as "completion" (the
    unit's own reported target for a plain force-heat vs. an independent
    disinfection threshold for legionella), and what to do once a window IS
    done (restore a raised target, credit a legionella completion, pop
    force_heat_activated_at) - stays in each caller, not here.

    completion_temp is intentionally just a number, not a distinction
    between "the unit's own target" and "our own known-correct value" - the
    caller resolves that (see run_revert_check's own mismatch-alert logic)
    before calling this; by the time this runs, whichever value is the
    right one to complete against has already been decided.

    Returns:
        None if the window isn't done yet (caller should leave it alone and
        return 0). Otherwise True if it reached completion_temp, False if it
        only got there via a timeout/deadline - the caller uses this to
        decide whether to credit completion (legionella) or just note which
        branch fired (force-heat, where there's nothing to credit).

    """
    timed_out = elapsed_hours >= max_duration_hours or deadline_passed
    reached_target = tank_temperature is not None and completion_temp is not None and tank_temperature >= completion_temp

    if not reached_target and not timed_out:
        if not quiet:
            print(
                f"{kind.capitalize()} in progress: {tank_temperature}C / {completion_temp}C "
                f"({elapsed_hours:.1f}h elapsed, {max_duration_hours}h limit) - leaving as is"
            )
        return None

    if reached_target:
        logger.info(
            "%s reached %sC (%.1fh elapsed), reverting to auto",
            kind.capitalize(),
            completion_temp,
            elapsed_hours,
        )
        if not quiet:
            print(f"{kind.capitalize()} reached {completion_temp}C, reverting to auto")
        return True

    if deadline_passed:
        logger.warning(
            "%s active past the overnight deadline (%.1fh elapsed) without reaching %sC "
            "(currently %sC) - reverting anyway as a safety net",
            kind.capitalize(),
            elapsed_hours,
            completion_temp,
            tank_temperature,
        )
        if not quiet:
            print(
                f"{kind.capitalize()} active past the overnight deadline ({elapsed_hours:.1f}h "
                f"elapsed) without reaching {completion_temp}C (currently {tank_temperature}C) - "
                "reverting anyway"
            )
    else:
        logger.warning(
            "%s active for %.1fh >= %sh limit without reaching %sC (currently %sC) - "
            "reverting anyway as a safety net",
            kind.capitalize(),
            elapsed_hours,
            max_duration_hours,
            completion_temp,
            tank_temperature,
        )
        if not quiet:
            print(
                f"{kind.capitalize()} active for {elapsed_hours:.1f}h >= {max_duration_hours}h "
                f"limit without reaching {completion_temp}C (currently {tank_temperature}C) - "
                "reverting anyway"
            )

    _alert_insufficient_duration(
        config,
        kind=kind,
        tank_temperature=tank_temperature,
        target_temperature=completion_temp,
        elapsed_hours=elapsed_hours,
        max_duration_hours=max_duration_hours,
        config_key=duration_config_key,
        dry_run=dry_run,
        quiet=quiet,
    )
    return False


def _alert_normal_target_mismatch(
    config: dict[str, Any],
    state: dict[str, Any],
    *,
    expected: float,
    actual: float,
    dry_run: bool,
    quiet: bool,
) -> None:
    """One-off (per distinct mismatch) heads-up that the unit's own configured
    target doesn't match hotwater_automation.normal_target_temp_c - see
    run_revert_check's own docstring for why this alerts rather than
    silently overriding. Deduped via state["normal_target_mismatch_alerted_for"]
    (an {"actual": ..., "expected": ...} pair, not just the actual value
    alone - confirmed 2026-09-08: deduping on actual alone would permanently
    swallow a genuinely new mismatch that happens to reuse a previously-
    alerted actual value, e.g. if normal_target_temp_c itself is changed, or
    the unit's target cycles back to an earlier value while a real,
    persistent mismatch is ongoing) so it doesn't re-fire every poll tick for
    the same ongoing difference - only when the mismatch first appears, or
    changes to a different (actual, expected) pair.

    The dedupe stamp is only written once send_email actually succeeds
    (confirmed 2026-09-08, matching check_legionella_due_warning's existing
    pattern) - stamping unconditionally would permanently suppress the alert
    for that mismatch after a single transient email failure, since nothing
    else would ever ask for it again while the mismatch stays unchanged.
    """
    already_alerted = state.get("normal_target_mismatch_alerted_for")
    current_mismatch = {"actual": actual, "expected": expected}
    if already_alerted == current_mismatch:
        return
    logger.warning(
        "NORMAL_TARGET_MISMATCH: tank's configured target (%sC) does not match "
        "hotwater_automation.normal_target_temp_c (%sC) - heating to the tank's own "
        "target regardless (see run_revert_check's docstring), but flagging in case this "
        "wasn't intentional",
        actual,
        expected,
    )
    if not quiet:
        print(f"NORMAL_TARGET_MISMATCH: tank target {actual}C != expected {expected}C")

    if dry_run:
        if not quiet:
            print("(dry run) would send 'tank target mismatch' alert email")
        return

    sent = send_email(
        config,
        "Hot water: tank target doesn't match the expected normal target",
        (
            f"The tank's own configured target is {actual}C, but "
            f"hotwater_automation.normal_target_temp_c expects {expected}C.\n\n"
            "The automation heats to whichever target the tank itself reports - it does "
            "not override your own configuration - so this is only a heads-up, not an "
            "action taken.\n\n"
            "If you changed this deliberately (e.g. via the MELCloud app), no action "
            f"needed - update normal_target_temp_c to {actual} in config.yaml if you'd "
            "like this to stop being flagged. If you didn't, it's worth checking why "
            "the tank's target changed."
        ),
    )
    if sent:
        state["normal_target_mismatch_alerted_for"] = current_mismatch
    elif not quiet:
        print("Failed to send 'tank target mismatch' alert email - will retry next tick")


async def run_revert_check(
    config: dict[str, Any], hw_config: dict[str, Any], *, dry_run: bool, quiet: bool
) -> int:
    """Revert to auto mode once the tank reaches temperature, or a safety limit is hit.

    Reverting only ever happens here (never from run_force_heat_check itself),
    so a force-heat window started because the car was charging is never cut
    short just because the car later stops charging (or any other trigger
    condition flips) - it always runs through to one of:
    - the tank reaching hotwater_automation.normal_target_temp_c (the normal,
      expected way this ends) - confirmed 2026-09-08, this compares against
      OUR OWN configured expectation, not blindly against whatever MELCloud's
      target_tank_temperature happens to report. The unit's own target lives
      entirely outside this codebase (set via the app), so a plain
      "reached the unit's target" comparison was blind to a target that had
      drifted from what's actually intended. If the two disagree, this still
      reverts once the tank reaches the UNIT's own (possibly higher) target -
      your own choice always wins - but sends a one-off alert first (see
      _alert_normal_target_mismatch) rather than silently either trusting or
      overriding it. See "how does the legionella cycle differ" - legionella
      already worked this way (an independent completion threshold, not the
      unit's own reported target), this brings the normal path to parity,
    - force_heat_max_duration_hours elapsing regardless (a safety net in case
      MELCloud never reports the tank as having reached target, e.g. a stuck
      sensor reading or the unit silently not heating), or
    - the offpeak_end (05:30 by default) clock deadline passing since
      activation, whichever of the two comes first - see
      _overnight_deadline_passed.

    Not gated on holiday_mode_active/service_mode_active: an already-active
    force-heat window when either starts is deliberately left to finish/
    time out normally here rather than being force-interrupted (see
    scripts/holiday_mode.py's docstring) - simpler and no less safe, since it
    can only overlap the moment the mode was turned on.

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
        return await _run_revert_check_locked(config, hw_config, state, dry_run=dry_run, quiet=quiet)


async def _run_revert_check_locked(
    config: dict[str, Any], hw_config: dict[str, Any], state: dict[str, Any], *, dry_run: bool, quiet: bool
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

    tz_name = config.get("location", {}).get("default_timezone_str", DEFAULT_TIMEZONE)
    tz = pytz.timezone(tz_name)
    now = datetime.now(tz=UTC)
    now_local = now.astimezone(tz)

    max_duration_hours = hw_config.get(
        "force_heat_max_duration_hours", DEFAULT_FORCE_HEAT_MAX_DURATION_HOURS
    )
    elapsed_hours, deadline_passed = _elapsed_hours_and_deadline_passed(
        hw_config, activated_at, now, tz
    )

    client = MelCloudClient(config_path=get_config_path())
    try:
        await client.connect()
        status = await client.get_tank_status()
        tank_temperature = status["tank_temperature"]
        unit_target = status["target_tank_temperature"]
        _refresh_daily_snapshot_if_warm(hw_config, state, tank_temperature, now_local)

        normal_target = hw_config.get("normal_target_temp_c", DEFAULT_NORMAL_TARGET_TEMP_C)
        if unit_target is not None and unit_target != normal_target:
            _alert_normal_target_mismatch(
                config, state, expected=normal_target, actual=unit_target, dry_run=dry_run, quiet=quiet
            )
        # Revert once the tank reaches the UNIT's own target (never lower than
        # our own expectation would require anyway when they match, and never
        # overriding your own choice when they don't - see this function's
        # own docstring and _alert_normal_target_mismatch above).
        completion_temp = unit_target if unit_target is not None else normal_target

        reached_target = _decide_heating_window_outcome(
            config,
            kind="force-heat",
            tank_temperature=tank_temperature,
            completion_temp=completion_temp,
            elapsed_hours=elapsed_hours,
            deadline_passed=deadline_passed,
            max_duration_hours=max_duration_hours,
            duration_config_key="force_heat_max_duration_hours",
            dry_run=dry_run,
            quiet=quiet,
        )
        if reached_target is None:
            return 0

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


async def run_safety_ceiling_check(
    config: dict[str, Any], hw_config: dict[str, Any], *, dry_run: bool, quiet: bool
) -> int:
    """Last-resort, independent safety backstop for the normal decision path.

    Deliberately does NOT call determine_hotwater_decision, does not reuse
    run_revert_check's/run_legionella_progress_check's already-computed
    elapsed_hours/timed_out conclusions, and takes its own fresh MELCloud tank
    reading. The whole point is that a bug in that normal path (e.g. the
    daily-snapshot staleness that caused a real reheat/revert loop) must not
    also be able to fool this check - it has to arrive at "is this actually
    too hot / has this actually been heating too long" on its own.

    safety_ceiling_temp_c (confirmed 2026-09-07) is the household's actual
    absolute limit - "the water shouldn't be heated above 55 degrees",
    full stop, since a "proper" (officially MELCloud-supported) legionella
    cycle can never actually be triggered on this hardware, so there is no
    higher temperature a legitimate cycle could ever need. That means this
    ceiling now exactly equals legionella_natural_completion_temp_c/
    legionella_target_temp_c (both 55 by default) - every normal, successful
    legionella cycle finishes AT this ceiling, not below it. See the
    "one-way, but not silent about legitimate completions" note below for
    how that's handled without alarming on routine operation.

    ONE-WAY BY CONSTRUCTION FOR NORMAL FORCE-HEAT: on a plain force-heat
    (no legionella cycle involved), the only action this can ever take is
    client.set_force_hot_water(enabled=False), and it never writes to
    hotwater_automation_state.json - it reads state fresh but doesn't hold a
    lock unless a legionella cycle needs cleanup (see below), so it cannot
    clear/reset force_heat_activated_at or anything else the normal logic
    uses to decide when to start again. The normal logic's own next
    run_revert_check tick finds the tank already off and finishes that
    bookkeeping normally, exactly as if it had reverted it itself.

    LEGIONELLA CYCLES ARE THE ONE EXCEPTION, AND DELIBERATELY SO: because
    this ceiling now equals the legionella completion temperature, this
    check and run_legionella_progress_check both now run on
    poll_interval_seconds (~10 min, since 2026-09-08) - see
    hotwater_mode_daemon.py's _register_checks for why this one is
    registered FIRST of the two: whichever runs first within a tick is the
    one that actually completes a cycle that just reached temperature, and
    only run_safety_ceiling_check knows to treat that as a quiet completion
    rather than an alarm (see below) - if run_legionella_progress_check ran
    first instead, it would clear cycle_in_progress before this check ever
    saw it, and this check would then misread the still-hot tank as a
    genuine, unexplained violation with no legionella cycle to credit it to.
    Leaving a genuine completion half-finished (heat cut, but the raised
    target never restored and the cycle never marked complete) would be
    worse than not having this check catch it at all - the tank's target
    would stay wrong until run_legionella_progress_check's next tick, and
    if THAT also has a bug, could stay wrong indefinitely. So: when a
    legionella cycle is in progress at the moment a violation is found, this
    check DOES acquire locked_state() and perform the same completion/
    timeout bookkeeping run_legionella_progress_check's own
    reached_target/timed_out branches would - restoring
    original_target_temp_c always, and crediting last_completed_at only when
    it was genuinely the temperature ceiling (not just a duration timeout)
    that triggered. Two independent checks converging on the identical,
    already-tested completion logic isn't "fighting" - run_legionella_
    progress_check's own next read (later this same tick, or next tick)
    will simply find cycle_in_progress already False and no-op, the same
    guard it already has for a concurrent run_force_heat_check start.

    A genuine temperature violation with NO legionella cycle in progress, or
    a duration violation on a plain force-heat, still gets the loud
    CRITICAL log + alert email exactly as before - those really are
    unexpected. A legionella cycle reaching its own completion temperature
    is not; it's logged at INFO and gets the same calm completion email
    check_legionella_due_warning's sibling functions already send, not a
    "SAFETY CEILING" alarm - so this doesn't turn every routine ~90-day
    legionella cycle into a false alarm.

    safety_max_duration_hours is unaffected by any of the above - still
    configured well above the normal operating limits (see
    DEFAULT_SAFETY_MAX_DURATION_HOURS's docstring), so a duration-only
    violation should still be rare in normal operation.

    Duration source: whichever of state["legionella"]["cycle_started_at"] (if
    a legionella cycle is in progress) or state["force_heat_activated_at"] is
    set - the same timestamps the normal logic already writes, read fresh
    here rather than trusting anyone else's already-computed elapsed time. A
    missing/unparseable timestamp while the tank is actively force-heating
    (per this function's own live MELCloud read) is treated as a duration
    violation, not skipped - "can't tell how long this has been running" must
    never be read as permission to leave it alone.

    Returns:
        0 on success (including "no violation found"), 1 if a violation was
        found but the revert request couldn't be confirmed.

    """
    ceiling_temp = hw_config.get("safety_ceiling_temp_c", DEFAULT_SAFETY_CEILING_TEMP_C)
    max_duration_hours = hw_config.get(
        "safety_max_duration_hours", DEFAULT_SAFETY_MAX_DURATION_HOURS
    )
    state = read_state()
    legionella_state_snapshot = state.get("legionella", {})
    legionella_in_progress = bool(legionella_state_snapshot.get("cycle_in_progress"))

    client = MelCloudClient(config_path=get_config_path())
    try:
        await client.connect()
        status = await client.get_tank_status()
        tank_temperature = status["tank_temperature"]

        temp_violation = tank_temperature is not None and tank_temperature >= ceiling_temp

        duration_violation = False
        elapsed_hours: float | None = None
        if status["operation_mode"] == HotWaterOperationMode.FORCE_HOT_WATER:
            if legionella_in_progress:
                started_at_str = legionella_state_snapshot.get("cycle_started_at")
            else:
                started_at_str = state.get("force_heat_activated_at")

            started_at = None
            if started_at_str:
                try:
                    started_at = datetime.fromisoformat(started_at_str)
                except ValueError:
                    started_at = None

            if started_at is None:
                # Actively heating with no (or an unreadable) start time to
                # measure against - can't confirm this is within limits, so
                # fail toward reverting rather than toward permitting it.
                duration_violation = True
            else:
                elapsed_hours = (datetime.now(tz=UTC) - started_at).total_seconds() / 3600.0
                duration_violation = elapsed_hours >= max_duration_hours

        if not temp_violation and not duration_violation:
            if not quiet:
                print(
                    f"Safety ceiling check: {tank_temperature}C (ceiling {ceiling_temp}C) - no violation"
                )
            return 0

        # A temperature violation while a legionella cycle is in progress is
        # exactly what a successful cycle looks like now the ceiling equals
        # its completion temperature - not an alarm. A duration-only
        # violation during one is a genuine timeout (like
        # run_legionella_progress_check's own timed_out branch) - still
        # cleaned up below, but not credited as complete, and still alarmed.
        quiet_legionella_completion = temp_violation and legionella_in_progress

        if quiet_legionella_completion:
            logger.info(
                "Legionella cycle reached its %sC disinfection temperature (caught by the "
                "independent safety check, ahead of run_legionella_progress_check's own next "
                "tick) - completing normally",
                ceiling_temp,
            )
        else:
            if temp_violation:
                logger.critical(
                    "SAFETY_CEILING_TEMP: tank at %sC >= safety ceiling %sC - cutting force-heat "
                    "regardless of mode/cause (hotwater_automation.safety_ceiling_temp_c)",
                    tank_temperature,
                    ceiling_temp,
                )
            if duration_violation:
                logger.critical(
                    "SAFETY_CEILING_DURATION: force-heat/legionella cycle has been active for "
                    "%s >= safety limit %sh - cutting force-heat regardless of the normal revert "
                    "logic's own conclusion (hotwater_automation.safety_max_duration_hours)",
                    f"{elapsed_hours:.1f}h" if elapsed_hours is not None else "an unknown duration",
                    max_duration_hours,
                )
        if not quiet:
            print(
                f"SAFETY CEILING VIOLATION: temp={temp_violation} ({tank_temperature}C), "
                f"duration={duration_violation} - cutting force-heat"
            )

        if dry_run:
            if not quiet:
                print("(dry run - not actually cutting force-heat)")
            return 0

        original_target_temp = legionella_state_snapshot.get("original_target_temp_c")
        if legionella_in_progress and original_target_temp is not None:
            await client.set_target_tank_temperature(original_target_temp)
        success = await client.set_force_hot_water(enabled=False)
    finally:
        await client.close()

    completed_at = datetime.now(tz=UTC)
    if legionella_in_progress and success:
        # Mirrors run_legionella_progress_check's own reached_target/
        # timed_out state update exactly - see this function's own
        # docstring for why converging on it here is safe, not a fight.
        with locked_state(timeout=DEFAULT_HOTWATER_LOCK_TIMEOUT_SECONDS) as state:
            current_legionella = state.get("legionella", {})
            if current_legionella.get("cycle_in_progress"):
                state["legionella"] = {
                    **current_legionella,
                    "cycle_in_progress": False,
                    "last_completed_at": (
                        completed_at.isoformat()
                        if quiet_legionella_completion
                        else current_legionella.get("last_completed_at")
                    ),
                }

    if quiet_legionella_completion:
        if success:
            _notify_legionella_completed(
                config,
                hw_config,
                tank_temperature=tank_temperature,
                completed_at=completed_at,
                source="forced cycle (caught by the independent safety check)",
                dry_run=dry_run,
                quiet=quiet,
            )
            if not quiet:
                print("Safety ceiling: legionella cycle completed, target restored")
            return 0
        logger.error("SAFETY_CEILING: failed to confirm force-heat was cut")
        if not quiet:
            print("Safety ceiling: failed to confirm force-heat was cut")
        return 1

    subject = "Hot water: SAFETY CEILING triggered - force-heat cut"
    reasons = []
    if temp_violation:
        reasons.append(f"tank temperature {tank_temperature}C reached/exceeded the {ceiling_temp}C safety ceiling")
    if duration_violation:
        elapsed_str = f"{elapsed_hours:.1f}h" if elapsed_hours is not None else "an unrecorded duration"
        reasons.append(
            f"force-heat/legionella has been active for {elapsed_str}, past the "
            f"{max_duration_hours}h safety limit"
        )
    body = (
        "The independent hot water safety backstop has cut force-heat because "
        + " and ".join(reasons)
        + ".\n\n"
        "This is separate from, and independent of, the normal force-heat/revert/legionella "
        "logic and its own (shorter) limits - it exists specifically to catch a case where "
        "that normal logic itself failed to stop heating in time. It never re-enables "
        "heating, so the usual automation will simply pick up from here (already off) on its "
        "own next check."
        + (
            "\n\nA legionella cycle was in progress and timed out without reaching its "
            "disinfection temperature - its target has been restored to normal, but it has "
            "NOT been credited as complete and will be retried at the next due opportunity."
            if legionella_in_progress
            else ""
        )
        + "\n\nWorth investigating why the normal logic didn't stop this itself."
    )
    send_email(config, subject, body)

    if success:
        if not quiet:
            print("Safety ceiling: force-heat cut and confirmed")
        return 0

    logger.error("SAFETY_CEILING: failed to confirm force-heat was cut")
    if not quiet:
        print("Safety ceiling: failed to confirm force-heat was cut")
    return 1


async def run_legionella_progress_check(
    config: dict[str, Any], hw_config: dict[str, Any], *, dry_run: bool, quiet: bool
) -> int:
    """Check an in-progress legionella cycle and revert once done or overdue.

    Holds the state-file lock across this whole function, for the same
    reason run_revert_check and run_force_heat_check do (see their
    docstrings) - a plain unlocked read followed by a slow MELCloud call and
    a much-later write leaves a window where a concurrent invocation of one
    of those two could act on (or start) a cycle this function doesn't know
    about yet, and this function's final write would then clobber it.

    Not gated on holiday_mode_active/service_mode_active - same rationale as
    run_revert_check.

    Returns:
        0 on success (including "no cycle in progress" / "still in progress"),
        1 if a requested revert couldn't be confirmed.

    """
    with locked_state(timeout=DEFAULT_HOTWATER_LOCK_TIMEOUT_SECONDS) as state:
        return await _run_legionella_progress_check_locked(
            config, hw_config, state, dry_run=dry_run, quiet=quiet
        )


async def _run_legionella_progress_check_locked(
    config: dict[str, Any], hw_config: dict[str, Any], state: dict[str, Any], *, dry_run: bool, quiet: bool
) -> int:
    """Body of run_legionella_progress_check() that runs inside its locked_state() block."""
    legionella_state = state.get("legionella", {})

    if not legionella_state.get("cycle_in_progress"):
        if not quiet:
            print("No legionella cycle in progress")
        return 0

    started_at_str = legionella_state.get("cycle_started_at")
    original_target_temp = legionella_state.get("original_target_temp_c")
    # target_temp_c (the raised target this cycle originally requested) is
    # checked for presence here as part of validating the state is well-formed,
    # but not used below - completion is decided against
    # legionella_natural_completion_temp_c instead (see below), independent
    # of whatever was actually requested.
    if started_at_str is None or legionella_state.get("target_temp_c") is None or original_target_temp is None:
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
    now = datetime.now(tz=UTC)
    tz_name = config.get("location", {}).get("default_timezone_str", DEFAULT_TIMEZONE)
    tz = pytz.timezone(tz_name)
    elapsed_hours, deadline_passed = _elapsed_hours_and_deadline_passed(hw_config, started_at, now, tz)

    client = MelCloudClient(config_path=get_config_path())
    try:
        await client.connect()
        status = await client.get_tank_status()
        tank_temperature = status["tank_temperature"]
        _refresh_daily_snapshot_if_warm(hw_config, state, tank_temperature, now.astimezone(tz))

        # A legionella cycle is considered done as soon as the tank is
        # actually hot enough to have been disinfected - not only once it
        # reaches the (higher) target_temp the cycle originally requested
        # from MELCloud. See DEFAULT_LEGIONELLA_NATURAL_COMPLETION_TEMP_C's
        # docstring: the same threshold applies whether that heat came from
        # this cycle's own request or arrived faster than expected. This is
        # exactly the "independent completion threshold, not the unit's own
        # reported target" pattern run_revert_check now also uses for a
        # plain force-heat - legionella just always worked this way.
        completion_temp = hw_config.get(
            "legionella_natural_completion_temp_c", DEFAULT_LEGIONELLA_NATURAL_COMPLETION_TEMP_C
        )

        reached_target = _decide_heating_window_outcome(
            config,
            kind="legionella cycle",
            tank_temperature=tank_temperature,
            completion_temp=completion_temp,
            elapsed_hours=elapsed_hours,
            deadline_passed=deadline_passed,
            max_duration_hours=max_duration_hours,
            duration_config_key="legionella_max_cycle_duration_hours",
            dry_run=dry_run,
            quiet=quiet,
        )
        if reached_target is None:
            return 0

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

    completed_at = datetime.now(tz=UTC)

    # Merge rather than replace - keeps any fields a future code version adds
    # to "legionella" that this function doesn't know about, rather than
    # silently discarding them.
    state["legionella"] = {
        **state.get("legionella", {}),
        "cycle_in_progress": False,
        "last_completed_at": (
            completed_at.isoformat()
            if reached_target
            else state.get("legionella", {}).get("last_completed_at")
        ),
    }
    logger.info("Legionella cycle reverted (reached_target=%s)", reached_target)
    if not quiet:
        print("Reverted after legionella cycle")

    if reached_target:
        _notify_legionella_completed(
            config,
            hw_config,
            tank_temperature=tank_temperature,
            completed_at=completed_at,
            source="forced cycle",
            dry_run=dry_run,
            quiet=quiet,
        )

    return 0


async def run_legionella_natural_completion_check(
    config: dict[str, Any], hw_config: dict[str, Any], *, dry_run: bool, quiet: bool
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
            config, hw_config, state, dry_run=dry_run, quiet=quiet
        )


async def _run_legionella_natural_completion_check_locked(
    config: dict[str, Any], hw_config: dict[str, Any], state: dict[str, Any], *, dry_run: bool, quiet: bool
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

    completed_at = datetime.now(tz=UTC)
    state["legionella"] = {
        **legionella_state,
        "last_completed_at": completed_at.isoformat(),
    }
    logger.info(
        "Tank observed at %sC (>= %sC disinfection threshold) with no legionella cycle "
        "necessarily involved - marking the legionella requirement satisfied, resetting "
        "the interval",
        tank_temperature,
        completion_temp,
    )
    _notify_legionella_completed(
        config,
        hw_config,
        tank_temperature=tank_temperature,
        completed_at=completed_at,
        source="natural completion (tank observed hot without a cycle)",
        dry_run=dry_run,
        quiet=quiet,
    )
    if not quiet:
        print(f"Tank at {tank_temperature}C - legionella satisfied naturally, interval reset")
    return 0
