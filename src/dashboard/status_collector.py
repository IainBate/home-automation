"""Collects a single point-in-time snapshot of home automation status for the dashboard.

Each subsystem (SolaX inverters, Ohme EV charger, MELCloud hot water tank) is
collected independently and never raises - a failure in one (an unreachable
inverter, an expired cloud token, a network blip) is reported inline as
{"available": False, "error": ...} rather than blanking the whole snapshot or
crashing the poll loop, mirroring the fail-fast/circuit-breaker convention the
rest of this codebase uses for hardware/API access (see CLAUDE.md).

This module only ever reads - it never changes battery mode, charger mode, or
hot water settings. That keeps it safe to poll on its own schedule alongside
battery_mode_daemon.py and hotwater_mode_daemon.py without risking a write
collision with either.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.api_clients.airstage_client import fetch_airstage_status
from src.api_clients.melcloud_client import (
    MelCloudAuthenticationError,
    MelCloudClient,
    MelCloudConnectionError,
)
from src.api_clients.ohme_ev_client import (
    OhmeAuthenticationError,
    OhmeConnectionError,
    OhmeEVClient,
)
from src.api_clients.resideo_client import fetch_resideo_status
from src.api_clients.solax_modbus_client import solax_modbus_bulk_data
from src.core_logic.battery_simulation.constants_and_models import (
    BatteryMode,
    battery_mode_to_display_string,
)
from src.utils.paths import (
    get_battery_evening_prediction_path,
    get_claude_usage_path,
    get_hotwater_automation_state_path,
    get_mg_saic_status_path,
    get_mode_change_log_path,
    get_project_root,
    get_solar_forecast_path,
)
from src.utils.state_store import read_json_state

logger = logging.getLogger(__name__)

# Bounds how long a single subsystem's async fetch may block the poller
# thread - without this, a stalled cloud call (MelCloudClient's session has
# no explicit timeout of its own, so it inherits aiohttp's 300s default)
# could delay refreshing every OTHER subsystem's cached data for minutes.
DASHBOARD_FETCH_TIMEOUT_SECONDS = 20

# Bounds how long a single `systemctl show` call may block the poller thread.
SYSTEMCTL_TIMEOUT_SECONDS = 5

# The daemons this dashboard reports on. Read-only: this only ever calls
# `systemctl show` (never start/stop/restart), so it observes the battery and
# hot water automation without altering either - see this module's docstring
# and CLAUDE.md's "must not alter" constraint. `unit` names match the actual
# unit files under scripts/ (home_automation.service,
# home_automation_dashboard.service) and docs/PI4_DEPLOYMENT.md's planned
# name for the not-yet-deployed hot water daemon
# (home_automation_hotwater.service) - if that unit isn't installed yet,
# _check_one_service() reports "installed": False rather than a false
# "stopped".
SERVICE_HEALTH_CHECKS = [
    {"key": "battery_daemon", "label": "Battery Daemon", "unit": "home_automation.service", "log_filename": "battery_mode_daemon.log"},
    {"key": "hot_water_daemon", "label": "Hot Water Daemon", "unit": "home_automation_hotwater.service", "log_filename": "hotwater_mode_daemon.log"},
    {"key": "dashboard", "label": "Dashboard", "unit": "home_automation_dashboard.service", "log_filename": "dashboard_server.log"},
]

# How far back _check_log_health() looks for ERROR/CRITICAL lines, and how
# many it requires before calling a service "unhealthy" rather than
# "healthy". Tuned against ~15 days of real battery_mode_daemon.log history
# (2026-06-27 to 07-04 and 2026-08-26 onward, pulled from the Pi): a
# threshold of 1 flags the daemon's own min_command_interval safety check
# (_modbus_mode_controller.py's "please wait N more seconds" message, logged
# at ERROR by battery_mode_daemon.py) as unhealthy even though it's expected,
# self-correcting behavior - 3 of the only 5 ERROR incidents in that history
# were exactly this. Requiring 2+ within the window drops those isolated,
# non-repeating incidents while still catching the one genuine sustained
# problem in that history (a ~9-minute Ohme API outage that logged 7
# consecutive "Failed to check Ohme status" errors).
LOG_HEALTH_WINDOW_MINUTES = 60
LOG_HEALTH_ERROR_THRESHOLD = 2
_UNHEALTHY_LOG_LEVELS = {"ERROR", "CRITICAL"}
_LOG_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ - \S+ - (\w+) - ")
# Bounds how much of a (potentially still-growing, up to a day's worth of)
# log file _check_log_health() reads per poll - only the tail is relevant to
# a 60-minute-old question, so there's no need to read the whole file.
_LOG_HEALTH_TAIL_BYTES = 128_000


def collect_status(config: dict[str, Any], config_path: str | None = None) -> dict[str, Any]:
    """Gather a snapshot of current solar/battery, EV charging and hot water status.

    Args:
        config: Loaded config.yaml dictionary (see config_manager.load_static_config)
        config_path: Absolute path config was loaded from - passed to
            OhmeEVClient/MelCloudClient so they don't fall back to their
            cwd-relative "config.yaml" default (which would silently break if
            the dashboard is ever started from outside the project root).
            Defaults to the project root's config.yaml, matching every other
            caller in this codebase (hotwater_automation_core.py,
            battery_mode_daemon.py, ...).

    Returns:
        Dictionary with "generated_at" plus one entry per subsystem, each of
        which is at least {"available": bool} and, when unavailable, includes
        an "error" string.

    """
    config_path = config_path or str(Path(get_project_root()) / "config.yaml")
    return {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "solar_battery": _collect_solar_battery(config),
        "ev_charging": _collect_ev_charging(config, config_path),
        "hot_water": _collect_hot_water(config, config_path),
        "airstage": _collect_airstage(config),
        "resideo": _collect_resideo(config),
        "solar_forecast": _collect_solar_forecast(config),
        "battery_forecast": _collect_battery_forecast(config),
        "claude_usage": _collect_claude_usage(config),
        "mg_saic": _collect_mg_saic(config),
        "service_health": _collect_service_health(),
    }


def _collect_solar_battery(config: dict[str, Any]) -> dict[str, Any]:
    """Read-only SolaX snapshot via the existing bulk Modbus read."""
    bulk = solax_modbus_bulk_data(config)
    if bulk is None:
        return {"available": False, "error": "Could not read from SolaX inverter(s)"}

    work_mode = bulk.get("work_mode")
    mode_log = read_json_state(get_mode_change_log_path())

    soc = bulk.get("soc", {})
    pv_power = bulk.get("pv_power", {})
    battery_power = bulk.get("battery_power", {})
    daily_yield = bulk.get("daily_yield", {})

    return {
        "available": True,
        "work_mode": (
            battery_mode_to_display_string(work_mode) if isinstance(work_mode, BatteryMode) else None
        ),
        "soc_percent_master": soc.get("master"),
        "soc_percent_slave": soc.get("slave"),
        "pv_power_w": _sum_pv_inverter(pv_power.get("master")) + _sum_pv_inverter(pv_power.get("slave")),
        "battery_power_w": _sum_optional(
            (battery_power.get("master") or {}).get("power"),
            (battery_power.get("slave") or {}).get("power"),
        ),
        "grid_power_w": bulk.get("grid_power", {}).get("master"),
        "daily_yield_kwh": _sum_optional(daily_yield.get("master"), daily_yield.get("slave")),
        "last_mode_change_reason": mode_log.get("last_change_reason"),
        "last_mode_change_at": mode_log.get("last_change_timestamp"),
    }


def _sum_pv_inverter(pv: dict[str, Any] | None) -> int:
    """Sum an inverter's two PV string readings, treating a missing inverter as 0W."""
    if not pv:
        return 0
    return (pv.get("pv1") or 0) + (pv.get("pv2") or 0)


def _sum_optional(*values: float | int | None) -> float | int | None:
    """Sum values that are present, or None if none of them are (vs. a misleading 0)."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present)


def _collect_ev_charging(config: dict[str, Any], config_path: str) -> dict[str, Any]:
    """Read-only Ohme charger snapshot, following battery_mode_daemon.py's own async pattern."""
    ohme_config = config.get("ohme_ev", {})
    if not ohme_config.get("enabled", False):
        return {"available": False, "disabled": True, "error": "Ohme EV charger disabled in config.yaml"}

    try:
        status = asyncio.run(_fetch_ohme_status(config_path))
        return {
            "available": True,
            "plugged_in": status.get("plugged_in"),
            "status": status["status"].value if status.get("status") else None,
            "mode": status["mode"].value if status.get("mode") else None,
            "power_watts": status.get("power_watts"),
            "battery_percent": status.get("battery_percent"),
            "target_soc": status.get("target_soc"),
            "current_vehicle": status.get("current_vehicle"),
        }
    except (OhmeAuthenticationError, OhmeConnectionError) as e:
        logger.warning("Failed to fetch Ohme charger status: %s", e)
        return {"available": False, "error": str(e)}
    except TimeoutError:
        logger.warning("Ohme charger status fetch timed out after %ss", DASHBOARD_FETCH_TIMEOUT_SECONDS)
        return {"available": False, "error": "Timed out reading Ohme charger"}
    except Exception:
        # Circuit Breaker: the field-extraction above must be covered too, not
        # just the network call - an unexpected field shape here must not
        # abort collect_status() for every OTHER subsystem (see
        # poller.py's _poll_once(), whose only outer try/except discards the
        # whole snapshot on any uncaught exception).
        logger.exception("Unexpected error fetching Ohme charger status")
        return {"available": False, "error": "Unexpected error reading Ohme charger"}


async def _fetch_ohme_status(config_path: str) -> dict[str, Any]:
    client = OhmeEVClient(config_path=config_path)
    await client.connect()
    try:
        return await asyncio.wait_for(client.get_charger_status(use_cache=False), timeout=DASHBOARD_FETCH_TIMEOUT_SECONDS)
    finally:
        await client.close()


def _parse_holiday_until(automation_state: dict[str, Any]) -> datetime | None:
    """Return hotwater_automation_state.json's holiday.until as an aware datetime, or None.

    Deliberately reimplements scripts/hotwater_automation_core.py's
    get_holiday_until() rather than importing it - this module (src/) doesn't
    otherwise depend on scripts/, and this is a display-only read, so it's
    fine to duplicate the same tolerant "missing/malformed/naive means no
    holiday" parsing rather than invert that dependency direction for it.
    """
    until_str = automation_state.get("holiday", {}).get("until")
    if not until_str:
        return None
    try:
        until = datetime.fromisoformat(until_str)
    except (TypeError, ValueError):
        return None
    return until if until.tzinfo is not None else None


def _collect_hot_water(config: dict[str, Any], config_path: str) -> dict[str, Any]:
    """Read-only MELCloud tank snapshot, plus this project's own force-heat/legionella state."""
    melcloud_config = config.get("melcloud", {})
    if not melcloud_config.get("enabled", False):
        return {"available": False, "disabled": True, "error": "MELCloud disabled in config.yaml"}

    try:
        status = asyncio.run(_fetch_hot_water_status(config_path))

        automation_state = read_json_state(get_hotwater_automation_state_path())
        legionella_state = automation_state.get("legionella", {})
        automation_holiday_until = _parse_holiday_until(automation_state)

        return {
            "available": True,
            "tank_temperature_c": status.get("tank_temperature"),
            "target_tank_temperature_c": status.get("target_tank_temperature"),
            "operation_mode": status["operation_mode"].value if status.get("operation_mode") else None,
            "status": status["status"].value if status.get("status") else None,
            "power_on": status.get("power"),
            # MELCloud's own native device-level setting - NOT the same as
            # automation_holiday_active below (this project's own force-heat
            # pause, via scripts/holiday_mode.py). See that script's module
            # docstring for why the two are kept clearly distinct.
            "holiday_mode": status.get("holiday_mode"),
            "force_heat_active": bool(automation_state.get("force_heat_activated_at")),
            "force_heat_activated_at": automation_state.get("force_heat_activated_at"),
            "legionella_cycle_in_progress": bool(legionella_state.get("cycle_in_progress")),
            "legionella_last_completed_at": legionella_state.get("last_completed_at"),
            "automation_holiday_active": (
                automation_holiday_until is not None
                and datetime.now(tz=UTC) < automation_holiday_until
            ),
            "automation_holiday_until": (
                automation_holiday_until.isoformat() if automation_holiday_until else None
            ),
        }
    except (MelCloudAuthenticationError, MelCloudConnectionError) as e:
        logger.warning("Failed to fetch MELCloud tank status: %s", e)
        return {"available": False, "error": str(e)}
    except TimeoutError:
        logger.warning("MELCloud tank status fetch timed out after %ss", DASHBOARD_FETCH_TIMEOUT_SECONDS)
        return {"available": False, "error": "Timed out reading MELCloud tank"}
    except Exception:
        # Circuit Breaker: see the matching comment in _collect_ev_charging -
        # the field-extraction/state-file-read above must be covered too, not
        # just the network call.
        logger.exception("Unexpected error fetching MELCloud tank status")
        return {"available": False, "error": "Unexpected error reading MELCloud tank"}


async def _fetch_hot_water_status(config_path: str) -> dict[str, Any]:
    client = MelCloudClient(config_path=config_path)
    await client.connect()
    try:
        return await asyncio.wait_for(client.get_tank_status(use_cache=False), timeout=DASHBOARD_FETCH_TIMEOUT_SECONDS)
    finally:
        await client.close()


def _collect_airstage(config: dict[str, Any]) -> dict[str, Any]:
    """Read-only Airstage snapshot for every configured zone (local network, no cloud account)."""
    if not config.get("airstage", {}).get("enabled", False):
        return {"available": False, "disabled": True, "error": "Airstage disabled in config.yaml"}

    zones = fetch_airstage_status(config)
    if zones is None:
        return {"available": False, "error": "Airstage has no zones configured"}

    return {"available": True, "zones": zones}


def _collect_resideo(config: dict[str, Any]) -> dict[str, Any]:
    """Read-only Resideo thermostat snapshot via evohome-async (see resideo_client.py)."""
    if not config.get("resideo", {}).get("enabled", False):
        return {"available": False, "disabled": True, "error": "Resideo disabled in config.yaml"}

    status = fetch_resideo_status(config)
    if status is None:
        return {
            "available": False,
            "error": "Could not read from Resideo - check resideo.username/password in secrets.yaml",
        }

    return {"available": True, **status}


def _collect_solar_forecast(config: dict[str, Any]) -> dict[str, Any]:
    """Read the cached forecast written by scripts/solar_forecast_predictor.py.

    Never runs the model or fetches weather itself - training/inference only
    happen in the periodic cron scripts (see config.yaml's solar_forecast
    comments), keeping this dashboard poll cheap.
    """
    if not config.get("solar_forecast", {}).get("enabled", False):
        return {"available": False, "disabled": True, "error": "Solar forecast disabled in config.yaml"}

    record = read_json_state(get_solar_forecast_path())
    if not record:
        return {
            "available": False,
            "error": "No forecast yet - run scripts/solar_forecast_trainer.py then "
            "scripts/solar_forecast_predictor.py",
        }

    return {
        "available": True,
        "today_kwh": record.get("today_kwh"),
        "tomorrow_kwh": record.get("tomorrow_kwh"),
        "yesterday_forecast_kwh": record.get("yesterday_forecast_kwh"),
        "yesterday_actual_kwh": record.get("yesterday_actual_kwh"),
        "yesterday_error_kwh": record.get("yesterday_error_kwh"),
        "current_weather": record.get("current_weather"),
        "generated_at": record.get("generated_at"),
        "model_trained_at": record.get("model_trained_at"),
    }


def _collect_mg_saic(config: dict[str, Any]) -> dict[str, Any]:
    """Read the cached snapshot written by scripts/mg_saic_poller.py.

    Never fetches the SAIC API itself - see that script's and
    saic_client.py's docstrings for why (shared login session with the
    household's phones).
    """
    if not config.get("mg_saic", {}).get("enabled", False):
        return {"available": False, "disabled": True, "error": "MG SAIC disabled in config.yaml"}

    record = read_json_state(get_mg_saic_status_path())
    if not record:
        return {
            "available": False,
            "error": "No status yet - run scripts/mg_saic_poller.py",
        }

    return {
        "available": True,
        "vehicle_name": record.get("vehicle_name"),
        "battery_percent": record.get("battery_percent"),
        "range_km": record.get("range_km"),
        "is_charging": record.get("is_charging"),
        "is_parked": record.get("is_parked"),
        "fetched_at": record.get("fetched_at"),
    }


def _collect_claude_usage(config: dict[str, Any]) -> dict[str, Any]:
    """Read the cached snapshot written by scripts/claude_usage_poller.py.

    Never fetches the usage endpoint itself - see that script's and
    claude_usage_client.py's docstrings for why (shared, easily-exhausted
    rate limit with real Claude Code sessions).
    """
    if not config.get("claude_usage", {}).get("enabled", False):
        return {"available": False, "disabled": True, "error": "Claude usage disabled in config.yaml"}

    record = read_json_state(get_claude_usage_path())
    if not record:
        return {
            "available": False,
            "error": "No usage data yet - run scripts/claude_usage_poller.py",
        }

    return {
        "available": True,
        "buckets": record.get("buckets", []),
        "extra_usage_percent": record.get("extra_usage_percent"),
        "fetched_at": record.get("fetched_at"),
    }


def _collect_battery_forecast(config: dict[str, Any]) -> dict[str, Any]:
    """Read the dashboard SoC checkpoints written by scripts/battery_evening_predictor.py."""
    if not config.get("battery_evening_prediction", {}).get("enabled", False):
        return {"available": False, "disabled": True, "error": "Battery evening prediction disabled in config.yaml"}

    record = read_json_state(get_battery_evening_prediction_path())
    if not record:
        return {
            "available": False,
            "error": "No prediction yet - run scripts/battery_evening_predictor.py",
        }

    return {
        "available": True,
        "computed_at": record.get("computed_at"),
        "checkpoints": record.get("dashboard_checkpoints", []),
    }


def _collect_service_health() -> dict[str, Any]:
    """Report whether the battery/hot-water/dashboard daemons are actually running.

    Distinct from every other _collect_* function here: those report whether
    the *data* looks fresh, this reports whether the *process* is up at all -
    a stalled daemon can leave yesterday's cached data looking perfectly
    plausible. Read-only: only ever runs `systemctl show`, never
    start/stop/restart, so this never alters the battery or hot water
    automation it's reporting on (see module docstring / CLAUDE.md).

    Not config-gated (unlike the other _collect_* functions) - there's no
    "service_health.enabled" toggle, since this is always meaningful
    wherever systemd is present.
    """
    try:
        if shutil.which("systemctl") is None:
            return {
                "available": False,
                "error": "systemctl not found - expected off the Pi (e.g. Mac development)",
            }
        units = [s["unit"] for s in SERVICE_HEALTH_CHECKS]
        states = _systemctl_show_batch(units)
        return {
            "available": True,
            "services": [_check_one_service(s, states[s["unit"]]) for s in SERVICE_HEALTH_CHECKS],
        }
    except Exception:
        # Circuit Breaker: see the matching comment in _collect_ev_charging.
        logger.exception("Unexpected error checking service health")
        return {"available": False, "error": "Unexpected error checking service health"}


def _check_one_service(service: dict[str, str], state: tuple[str | None, str | None]) -> dict[str, Any]:
    load_state, active_state = state
    installed = load_state == "loaded"
    active = active_state == "active" if installed else False
    # "disabled" covers both "not installed" and "installed but stopped/
    # failed" - callers only need the three-state health_status, not a
    # separate not-deployed-vs-stopped distinction (active_state carries
    # that detail already for anyone who wants it).
    health_status = "disabled" if not active else _check_log_health(service["log_filename"])
    return {
        "key": service["key"],
        "label": service["label"],
        "installed": installed,
        "active": active_state == "active" if installed else None,
        "active_state": active_state if installed else None,
        "log_age_seconds": _log_file_age_seconds(service["log_filename"]) if installed else None,
        "health_status": health_status,
    }


def _systemctl_show_batch(units: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """Return {unit: (LoadState, ActiveState)} for every unit, in a single systemctl call.

    One batched call rather than one per unit: on a slow/contended systemd
    (heavy load, a dpkg lock), each separate call could hit
    SYSTEMCTL_TIMEOUT_SECONDS independently, serially blocking the poller
    thread for up to len(units) * SYSTEMCTL_TIMEOUT_SECONDS; batched, the
    worst case is a single timeout.

    Parses `Property=Value` lines (rather than `--value`'s bare values) so a
    unit's properties are matched by name, not by trusting the properties
    come back in the same order they were requested - LoadState is "loaded"
    only when a unit file was actually found, which is what lets
    _check_one_service() tell "not deployed" (the hot water daemon,
    currently) apart from "deployed but down".
    """
    try:
        result = subprocess.run(
            ["systemctl", "show", *units, "--no-page", "-p", "LoadState", "-p", "ActiveState"],
            capture_output=True,
            text=True,
            timeout=SYSTEMCTL_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        logger.warning("systemctl show failed or timed out for units: %s", units)
        return dict.fromkeys(units, (None, None))

    # systemctl separates each unit's property block with a blank line, in
    # the same order the units were passed on the command line.
    blocks = result.stdout.strip("\n").split("\n\n")
    states: dict[str, tuple[str | None, str | None]] = {}
    for unit, block in zip(units, blocks, strict=False):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        states[unit] = (props.get("LoadState"), props.get("ActiveState"))
    for unit in units:
        states.setdefault(unit, (None, None))
    return states


def _log_file_age_seconds(log_filename: str) -> float | None:
    """Seconds since a daemon's log file was last written, or None if it doesn't exist yet.

    A secondary signal alongside ActiveState: a wedged-but-still-"active"
    process (see battery_mode_daemon.py's own Circuit Breaker docs for how a
    hardware cycle can fail without crashing the process) can otherwise look
    healthy by ActiveState alone.
    """
    log_path = Path(get_project_root()) / "logs" / log_filename
    try:
        mtime = log_path.stat().st_mtime
    except OSError:
        return None
    return datetime.now(tz=UTC).timestamp() - mtime


def _check_log_health(log_filename: str) -> str:
    """"unhealthy" if a daemon's log shows repeated recent errors, else "healthy".

    See LOG_HEALTH_ERROR_THRESHOLD's comment for why this requires 2+
    ERROR/CRITICAL lines in the window rather than just 1 - a single one is
    frequently the daemon's own safety check self-correcting, not a real
    problem. A missing/unreadable log, or one with no matching recent lines,
    reads as "healthy" (absence of evidence of a problem), matching how
    _log_file_age_seconds() already treats a missing log.
    """
    log_path = Path(get_project_root()) / "logs" / log_filename
    try:
        with log_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - _LOG_HEALTH_TAIL_BYTES)
            f.seek(start)
            tail = f.read()
    except OSError:
        return "healthy"

    lines = tail.decode("utf-8", errors="replace").splitlines()
    if start > 0:
        # The seek landed mid-file, possibly mid-line - drop the (possibly
        # truncated) first line rather than risk misparsing it. When start
        # is 0 the whole file was read, so there's nothing to drop.
        lines = lines[1:]

    cutoff = datetime.now() - timedelta(minutes=LOG_HEALTH_WINDOW_MINUTES)  # noqa: DTZ005 - asctime is local time, must compare naive-to-naive
    recent_issue_count = 0
    for line in lines:
        match = _LOG_LINE_RE.match(line)
        if not match:
            continue
        level = match.group(2)
        if level not in _UNHEALTHY_LOG_LEVELS:
            continue
        try:
            timestamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007 - see cutoff above
        except ValueError:
            continue
        if timestamp >= cutoff:
            recent_issue_count += 1

    return "unhealthy" if recent_issue_count >= LOG_HEALTH_ERROR_THRESHOLD else "healthy"
