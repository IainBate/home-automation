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
from datetime import UTC, datetime
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
        return {"available": False, "error": "Ohme EV charger disabled in config.yaml"}

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


def _collect_hot_water(config: dict[str, Any], config_path: str) -> dict[str, Any]:
    """Read-only MELCloud tank snapshot, plus this project's own force-heat/legionella state."""
    melcloud_config = config.get("melcloud", {})
    if not melcloud_config.get("enabled", False):
        return {"available": False, "error": "MELCloud disabled in config.yaml"}

    try:
        status = asyncio.run(_fetch_hot_water_status(config_path))
    except (MelCloudAuthenticationError, MelCloudConnectionError) as e:
        logger.warning("Failed to fetch MELCloud tank status: %s", e)
        return {"available": False, "error": str(e)}
    except Exception:
        logger.exception("Unexpected error fetching MELCloud tank status")
        return {"available": False, "error": "Unexpected error reading MELCloud tank"}

    automation_state = read_json_state(get_hotwater_automation_state_path())
    legionella_state = automation_state.get("legionella", {})

    return {
        "available": True,
        "tank_temperature_c": status.get("tank_temperature"),
        "target_tank_temperature_c": status.get("target_tank_temperature"),
        "operation_mode": status["operation_mode"].value if status.get("operation_mode") else None,
        "status": status["status"].value if status.get("status") else None,
        "power_on": status.get("power"),
        "holiday_mode": status.get("holiday_mode"),
        "force_heat_active": bool(automation_state.get("force_heat_activated_at")),
        "force_heat_activated_at": automation_state.get("force_heat_activated_at"),
        "legionella_cycle_in_progress": bool(legionella_state.get("cycle_in_progress")),
        "legionella_last_completed_at": legionella_state.get("last_completed_at"),
    }


async def _fetch_hot_water_status(config_path: str) -> dict[str, Any]:
    client = MelCloudClient(config_path=config_path)
    await client.connect()
    try:
        return await client.get_tank_status(use_cache=False)
    finally:
        await client.close()


def _collect_airstage(config: dict[str, Any]) -> dict[str, Any]:
    """Read-only Airstage snapshot for every configured zone (local network, no cloud account)."""
    if not config.get("airstage", {}).get("enabled", False):
        return {"available": False, "error": "Airstage disabled in config.yaml"}

    zones = fetch_airstage_status(config)
    if zones is None:
        return {"available": False, "error": "Airstage has no zones configured"}

    return {"available": True, "zones": zones}


def _collect_resideo(config: dict[str, Any]) -> dict[str, Any]:
    """Read-only Resideo thermostat snapshot via the official OAuth2 API."""
    if not config.get("resideo", {}).get("enabled", False):
        return {"available": False, "error": "Resideo disabled in config.yaml"}

    status = fetch_resideo_status(config)
    if status is None:
        return {
            "available": False,
            "error": "Could not read from Resideo - token may need refreshing "
            "(scripts/resideo_oauth_setup.py)",
        }

    return {"available": True, **status}


def _collect_solar_forecast(config: dict[str, Any]) -> dict[str, Any]:
    """Read the cached forecast written by scripts/solar_forecast_predictor.py.

    Never runs the model or fetches weather itself - training/inference only
    happen in the periodic cron scripts (see config.yaml's solar_forecast
    comments), keeping this dashboard poll cheap.
    """
    if not config.get("solar_forecast", {}).get("enabled", False):
        return {"available": False, "error": "Solar forecast disabled in config.yaml"}

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
        return {"available": False, "error": "MG SAIC disabled in config.yaml"}

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
        return {"available": False, "error": "Claude usage disabled in config.yaml"}

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
        return {"available": False, "error": "Battery evening prediction disabled in config.yaml"}

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
