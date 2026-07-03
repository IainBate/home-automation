#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
# pylint: disable=too-many-lines  # 1055 lines (55 over limit) - comprehensive status reporting with multiple display sections
"""SolaX Modbus Enhanced Status Script.

Complete status script that replicates SolaX Cloud API output using direct
Modbus TCP access. Provides SYSTEM OVERVIEW, BATTERY STATUS, PV GENERATION STATUS,
and INVERTERS STATUS sections using all verified registers.

This script demonstrates the complete functionality of the SolaX Modbus TCP API
client, combining data from all verified registers to create comprehensive
system status reporting that matches the Cloud API format.

SAFETY: READ-ONLY operations only. No write operations to inverters.
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytz
from tabulate import tabulate


def find_project_root() -> Path:
    """Find the project root directory."""
    current_path = Path.cwd().resolve()

    for path in [current_path, *list(current_path.parents)]:
        tests_dir = path / "tests"
        src_dir = path / "src"

        if tests_dir.exists() and src_dir.exists():
            return path

    return current_path


# Add the src directory to Python path
project_root = find_project_root()
sys.path.append(str(project_root))

# Import modules (must be after sys.path setup)
from common_config_utils import find_config_file  # noqa: E402 - after sys.path modification

from src.api_clients import (  # noqa: E402 - after sys.path modification
    solax_modbus_ac_power,
    solax_modbus_battery_capacity,
    solax_modbus_battery_power,
    solax_modbus_bulk_data,
    solax_modbus_daily_yield,
    solax_modbus_grid_power,
    solax_modbus_grid_totals,
    solax_modbus_pv_power,
    solax_modbus_rtc_timestamps,
    solax_modbus_run_mode,
    solax_modbus_serial_numbers,
    solax_modbus_soc,
    solax_modbus_work_mode,
)
from src.api_clients._modbus_reader import (  # noqa: E402 - after sys.path modification
    _read_single_battery_temperature,
)
from src.api_clients.solax_modbus_client import (  # noqa: E402 - after sys.path modification
    _read_single_ac_power,
    _read_single_battery_capacity,
    _read_single_battery_power,
    _read_single_daily_yield,
    _read_single_grid_export_total,
    _read_single_grid_import_total,
    _read_single_grid_power,
    _read_single_inverter_serial,
    _read_single_pv_power,
    _read_single_rtc_timestamp,
    _read_single_run_mode,
    _read_single_soc,
)
from src.config_manager import load_static_config  # noqa: E402 - after sys.path modification

# Configure basic logging (will be updated based on command line args)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="SolaX Modbus Enhanced Status Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run with default settings (clean output)
  python3 solax_modbus_status_report.py

  # Enable debug logging to see detailed Modbus operations
  python3 solax_modbus_status_report.py --log-level DEBUG

  # Show info level logging including work mode details
  python3 solax_modbus_status_report.py --log-level INFO

  # Specify custom config file
  python3 solax_modbus_status_report.py --config /path/to/config.yaml

  # Combine options
  python3 solax_modbus_status_report.py --config ./my_config.yaml --log-level DEBUG
        """,
    )

    parser.add_argument(
        "--config",
        "-c",
        type=str,
        help="Path to configuration file. If not specified, searches current directory, project root, and parent directories for config.yaml",
    )
    parser.add_argument(
        "--log-level",
        "-l",
        type=str,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="ERROR",
        help="Set logging level (default: ERROR for clean output)",
    )
    parser.add_argument(
        "--compare-cloud",
        action="store_true",
        help="Add Cloud API data comparison column to all output tables",
    )
    parser.add_argument(
        "--performance-logging",
        action="store_true",
        help="Enable detailed performance logging for modbus operations",
    )
    parser.add_argument(
        "--performance-output",
        type=str,
        help="Output file for performance logs (default: performance_TIMESTAMP.log)",
    )

    return parser.parse_args()


def load_config(config_path: str | None = None) -> dict[str, Any] | None:
    """Load configuration with automatic config file finding."""
    try:
        # Use provided config path or find automatically
        if config_path:
            logger.info("Using specified config file: %s", config_path)
            if not Path(config_path).exists():
                logger.error("Specified config file not found: %s", config_path)
                return None
            final_config_path = config_path
        else:
            final_config_path = find_config_file(None, logger)

        return load_static_config(final_config_path)
    except (OSError, ValueError, KeyError, TypeError):
        logger.exception("Error loading configuration")
        return None


def gather_cloud_data(config: dict[str, Any]) -> dict[str, Any] | None:
    """Gather data from SolaX Cloud API for comparison."""
    try:
        import requests  # noqa: PLC0415  # pylint: disable=import-outside-toplevel  # lazy loading for optional Cloud API comparison feature

        from src.api_clients import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel  # lazy loading for optional Cloud API comparison feature
            solax_data,
        )

        logger.debug("Gathering Cloud API data for comparison...")

        # Extract required configuration
        solax_config = config["solaX_cloud_api"]
        api_token = solax_config["token_id"]
        base_api_url = solax_config["base_url"]
        master_wifisn = solax_config["master_wifisn"]
        slave_wifisn = solax_config["slave_wifisn"]

        # Create a session for the cloud API requests
        session = requests.Session()

        # Fetch data from both inverters
        master_data = solax_data(api_token, master_wifisn, base_api_url, session)
        slave_data = solax_data(api_token, slave_wifisn, base_api_url, session)

        if not master_data or not slave_data:
            logger.warning("Failed to retrieve Cloud API data from one or both inverters")
            return None

        # Combine the data into a structure that matches the expected format
        cloud_data = {"master_inverter": master_data, "slave_inverter": slave_data}

        logger.info("Successfully retrieved Cloud API data")
        return cloud_data

    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        logger.exception("Error gathering Cloud API data")
        return None


def read_single_inverter_data(config: dict[str, Any], inverter_type: str) -> dict[str, Any] | None:
    """Read all data from a single inverter (master or slave)."""
    try:
        # Get configuration parameters
        solax_config = config["solaX_cloud_api"]
        port = solax_config.get("modbus_port", 502)
        timeout = solax_config.get("modbus_connection_timeout", 10)
        min_interval = solax_config.get("min_command_interval", 1.0)

        if inverter_type == "master":
            ip = solax_config["master_ip"]
            address = solax_config.get("master_modbus_address", 1)
        else:  # slave
            ip = solax_config["slave_ip"]
            address = solax_config.get("slave_modbus_address", 2)

        logger.info("Reading %s inverter data from %s...", inverter_type, ip)

        # Read all data for this inverter
        data = {}

        # Serial number
        data["serial_number"] = _read_single_inverter_serial(
            ip, port, timeout, address, min_interval
        )

        # Power data
        data["ac_power"] = _read_single_ac_power(ip, port, timeout, address, min_interval)
        data["battery_power"] = _read_single_battery_power(ip, port, timeout, address, min_interval)
        data["pv_power"] = _read_single_pv_power(ip, port, timeout, address, min_interval)

        # Grid power (only master has this)
        if inverter_type == "master":
            data["grid_power"] = _read_single_grid_power(ip, port, timeout, address, min_interval)
        else:
            data["grid_power"] = None

        # Battery data
        data["soc"] = _read_single_soc(ip, port, timeout, address, min_interval)
        data["battery_capacity"] = _read_single_battery_capacity(
            ip, port, timeout, address, min_interval
        )
        data["battery_temperature"] = _read_single_battery_temperature(
            ip, port, timeout, address, min_interval
        )

        # Generation data
        data["daily_yield"] = _read_single_daily_yield(ip, port, timeout, address, min_interval)

        # Grid totals (cumulative import/export energy)
        data["grid_import_total"] = _read_single_grid_import_total(
            ip, port, timeout, address, min_interval
        )
        data["grid_export_total"] = _read_single_grid_export_total(
            ip, port, timeout, address, min_interval
        )

        # Status data
        data["run_mode"] = _read_single_run_mode(ip, port, timeout, address, min_interval)

        # Read timestamp with error handling
        try:
            data["timestamp"] = _read_single_rtc_timestamp(ip, port, timeout, address, min_interval)
        except (OSError, ValueError, RuntimeError) as timestamp_error:
            logger.warning(
                "Could not read timestamp from %s inverter: %s", inverter_type, timestamp_error
            )
            data["timestamp"] = "N/A"

        logger.info("Successfully read %s inverter data", inverter_type)
        return data

    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        logger.exception("Error reading %s inverter data", inverter_type)
        return None


def gather_all_modbus_data_parallel(config: dict[str, Any]) -> dict[str, Any] | None:
    """Gather all data from Modbus API functions using parallel reads."""
    try:
        logger.debug("Gathering comprehensive system data via Modbus TCP (parallel mode)...")

        # Read both inverters in parallel
        with ThreadPoolExecutor(max_workers=2) as executor:
            # Submit both inverter reads simultaneously
            master_future = executor.submit(read_single_inverter_data, config, "master")
            slave_future = executor.submit(read_single_inverter_data, config, "slave")

            # Wait for both to complete
            master_data = master_future.result()
            slave_data = slave_future.result()

        if not master_data:
            logger.error("Failed to read data from master inverter")
            return None
        if not slave_data:
            logger.warning("Failed to read data from slave inverter, using partial data")
            # Create empty slave data structure to prevent crashes
            slave_data = {
                "serial_number": "N/A",
                "ac_power": 0,
                "battery_power": {"power": 0, "mode": "Unknown"},
                "pv_power": {"pv1": 0, "pv2": 0},
                "grid_power": None,
                "soc": 0,
                "battery_capacity": 0,
                "battery_temperature": None,
                "daily_yield": 0,
                "run_mode": "Unknown",
                "timestamp": "N/A",
                "grid_import_total": 0,
                "grid_export_total": 0,
            }

        # Read system mode from master inverter only
        try:
            system_mode = solax_modbus_work_mode(config)
        except (OSError, ValueError, RuntimeError) as e:
            logger.warning("Failed to read system mode: %s", e)
            system_mode = "Unknown"

        # Combine data into expected format
        data = {
            "serial_numbers": {
                "master": master_data["serial_number"],
                "slave": slave_data["serial_number"],
            },
            "ac_power": {"master": master_data["ac_power"], "slave": slave_data["ac_power"]},
            "grid_power": {"master": master_data["grid_power"], "slave": slave_data["grid_power"]},
            "grid_totals": {
                "master": {
                    "import_kwh": master_data["grid_import_total"],
                    "export_kwh": master_data["grid_export_total"],
                },
                "slave": {
                    "import_kwh": slave_data["grid_import_total"],
                    "export_kwh": slave_data["grid_export_total"],
                },
            },
            "battery_power": {
                "master": master_data["battery_power"],
                "slave": slave_data["battery_power"],
            },
            "pv_power": {"master": master_data["pv_power"], "slave": slave_data["pv_power"]},
            "soc": {"master": master_data["soc"], "slave": slave_data["soc"]},
            "battery_capacity": {
                "master": master_data["battery_capacity"],
                "slave": slave_data["battery_capacity"],
            },
            "battery_temperature": {
                "master": master_data["battery_temperature"],
                "slave": slave_data["battery_temperature"],
            },
            "daily_yield": {
                "master": master_data["daily_yield"],
                "slave": slave_data["daily_yield"],
            },
            "run_mode": {"master": master_data["run_mode"], "slave": slave_data["run_mode"]},
            "timestamps": {"master": master_data["timestamp"], "slave": slave_data["timestamp"]},
            "system_mode": system_mode,
        }

        logger.info("Parallel data gathering completed successfully!")
        return data

    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        logger.exception("Error gathering Modbus data (parallel)")
        return None


def gather_all_modbus_data_bulk(config: dict[str, Any]) -> dict[str, Any] | None:
    """Ultra-fast data gathering using new bulk data API (89% faster than sequential)."""
    try:
        logger.debug(
            "Gathering comprehensive system data via Modbus TCP (bulk mode - ultra-fast)..."
        )

        # Single function call that gets ALL data in ~3.4 seconds vs ~12 seconds
        bulk_data = solax_modbus_bulk_data(config)

        if not bulk_data:
            logger.warning("Bulk data read failed")
            return None

        # Transform bulk data to match expected format for existing consumers
        data = {
            "serial_numbers": bulk_data["serial_numbers"],
            "ac_power": bulk_data["ac_power"],
            "grid_power": bulk_data["grid_power"],
            "grid_totals": bulk_data["grid_totals"],
            "battery_power": bulk_data["battery_power"],
            "pv_power": bulk_data["pv_power"],
            "soc": bulk_data["soc"],
            "battery_capacity": bulk_data["battery_capacity"],
            "battery_temperature": bulk_data["battery_temperature"],
            "daily_yield": bulk_data["daily_yield"],
            "run_mode": bulk_data["run_mode"],
            "timestamps": bulk_data["rtc_timestamps"],  # Map to expected field name
            "system_mode": bulk_data["work_mode"],  # Map to expected field name
        }

        logger.info("Bulk data gathering completed successfully! (89%% performance improvement)")
        return data

    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as e:
        logger.warning("Bulk data read failed: %s", e)
        return None


def gather_all_modbus_data(config: dict[str, Any]) -> dict[str, Any] | None:
    """Gather all data from Modbus API functions (with performance-optimized fallback chain)."""
    try:
        # Try ultra-fast bulk approach first (89% faster than sequential)
        data = gather_all_modbus_data_bulk(config)
        if data:
            return data

        logger.warning("Bulk read failed, falling back to parallel mode...")

        # Try parallel approach second
        data = gather_all_modbus_data_parallel(config)
        if data:
            return data

        logger.warning("Parallel read failed, falling back to sequential mode...")

        # Fallback to original sequential approach
        logger.debug("Gathering comprehensive system data via Modbus TCP (sequential mode)...")

        # Gather all data with error handling
        data = {}

        # Core system identification
        logger.debug("Reading serial numbers...")
        data["serial_numbers"] = solax_modbus_serial_numbers(config)

        # Power and status data
        logger.debug("Reading AC power...")
        data["ac_power"] = solax_modbus_ac_power(config)

        logger.debug("Reading grid power...")
        data["grid_power"] = solax_modbus_grid_power(config)

        logger.debug("Reading battery power...")
        data["battery_power"] = solax_modbus_battery_power(config)

        logger.debug("Reading PV power...")
        data["pv_power"] = solax_modbus_pv_power(config)

        # Battery status data
        logger.debug("Reading battery SoC...")
        data["soc"] = solax_modbus_soc(config)

        logger.debug("Reading battery capacity...")
        data["battery_capacity"] = solax_modbus_battery_capacity(config)

        # Generation data
        logger.debug("Reading daily yield...")
        data["daily_yield"] = solax_modbus_daily_yield(config)

        # System status
        logger.debug("Reading run modes...")
        data["run_mode"] = solax_modbus_run_mode(config)

        logger.debug("Reading RTC timestamps...")
        data["timestamps"] = solax_modbus_rtc_timestamps(config)

        logger.debug("Reading system work mode...")
        data["system_mode"] = solax_modbus_work_mode(config)

        logger.debug("Reading grid import/export totals...")
        data["grid_totals"] = solax_modbus_grid_totals(config)

        logger.info("Data gathering completed successfully!")
        return data

    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        logger.exception("Error gathering Modbus data")
        return None


def calculate_system_overview(data: dict[str, Any]) -> dict[str, Any]:  # pylint: disable=too-many-locals  # Aggregates multiple inverter metrics
    """Calculate aggregated system overview metrics."""
    overview = {}

    try:
        # Total PV Power
        if data.get("pv_power"):
            master_pv = data["pv_power"].get("master")
            slave_pv = data["pv_power"].get("slave")
            total_pv = 0
            if master_pv and isinstance(master_pv, dict):
                total_pv += master_pv.get("pv1", 0) + master_pv.get("pv2", 0)
            if slave_pv and isinstance(slave_pv, dict):
                total_pv += slave_pv.get("pv1", 0) + slave_pv.get("pv2", 0)
            overview["total_pv_power"] = total_pv

        # Total AC Power
        if data.get("ac_power"):
            master_ac = data["ac_power"].get("master", 0)
            slave_ac = data["ac_power"].get("slave", 0)
            total_ac = (master_ac or 0) + (slave_ac or 0)
            overview["total_ac_power"] = total_ac

        # Grid Power (master only provides this data)
        if data.get("grid_power") and data["grid_power"].get("master") is not None:
            overview["grid_power"] = data["grid_power"]["master"]

        # Total Battery Power
        if data.get("battery_power"):
            master_bat_data = data["battery_power"].get("master", {})
            slave_bat_data = data["battery_power"].get("slave", {})
            master_bat = master_bat_data.get("power", 0) if master_bat_data else 0
            slave_bat = slave_bat_data.get("power", 0) if slave_bat_data else 0
            overview["total_battery_power"] = master_bat + slave_bat

        # Average Battery SoC
        if data.get("soc"):
            master_soc = data["soc"].get("master", 0)
            slave_soc = data["soc"].get("slave", 0)
            if master_soc is not None and slave_soc is not None:
                avg_soc = (master_soc + slave_soc) / 2
                overview["average_soc"] = avg_soc

        # Total Daily Yield
        if data.get("daily_yield"):
            master_yield = data["daily_yield"].get("master", 0)
            slave_yield = data["daily_yield"].get("slave", 0)
            total_yield = (master_yield or 0) + (slave_yield or 0)
            overview["total_daily_yield"] = total_yield

        # Total Battery Capacity
        if data.get("battery_capacity"):
            master_cap = data["battery_capacity"].get("master", 0)
            slave_cap = data["battery_capacity"].get("slave", 0)
            total_capacity = (master_cap or 0) + (slave_cap or 0)
            overview["total_battery_capacity"] = total_capacity

        # Current Energy Stored (SoC × Capacity)  # noqa: RUF003
        if overview.get("average_soc") and overview.get("total_battery_capacity"):
            stored_energy = (overview["average_soc"] / 100) * overview["total_battery_capacity"]
            overview["current_energy_stored"] = stored_energy

        return overview

    except (ValueError, KeyError, TypeError):
        logger.exception("Error calculating system overview")
        return {}


def format_system_overview(  # pylint: disable=too-many-locals  # Comprehensive status display matching Cloud API format
    overview: dict[str, Any], data: dict[str, Any], cloud_data: dict[str, Any] | None = None
) -> None:
    """Format SYSTEM OVERVIEW section to match Cloud API format."""
    # Get system mode from data
    system_mode = data.get("system_mode", "Unknown")

    # Power values with proper formatting (all converted to kW)
    total_pv = overview.get("total_pv_power", 0) / 1000  # Convert W to kW
    total_ac = overview.get("total_ac_power", 0) / 1000  # Convert W to kW
    grid_power = overview.get("grid_power", 0) / 1000  # Convert W to kW
    battery_power = overview.get("total_battery_power", 0) / 1000  # Convert W to kW
    avg_soc = overview.get("average_soc", 0)
    daily_yield = overview.get("total_daily_yield", 0)

    # Determine grid export/import (with scaling fix)
    grid_export = max(0, grid_power)
    grid_import = max(0, -grid_power)

    # Format battery power with direction
    if battery_power < 0:
        battery_status = f"{abs(battery_power):.2f} kW (Discharging)"
    elif battery_power > 0:
        battery_status = f"{battery_power:.2f} kW (Charging)"
    else:
        battery_status = "0.00 kW (Idle)"

    # Build overview data with optional cloud comparison
    if cloud_data:
        # Calculate cloud values for comparison
        # Calculate cloud values from individual inverter data
        cloud_total_ac = (
            cloud_data["master_inverter"]["acpower"] + cloud_data["slave_inverter"]["acpower"]
        ) / 1000
        cloud_total_pv = (
            cloud_data["master_inverter"]["powerdc1"]
            + cloud_data["master_inverter"]["powerdc2"]
            + cloud_data["slave_inverter"]["powerdc1"]
            + cloud_data["slave_inverter"]["powerdc2"]
        ) / 1000
        cloud_avg_soc = (
            cloud_data["master_inverter"]["soc"] + cloud_data["slave_inverter"]["soc"]
        ) / 2
        cloud_daily_yield = (
            cloud_data["master_inverter"]["yieldtoday"] + cloud_data["slave_inverter"]["yieldtoday"]
        )
        cloud_grid_power = cloud_data["master_inverter"]["feedinpower"] / 1000
        cloud_grid_export = max(0, cloud_grid_power)
        cloud_grid_import = max(0, -cloud_grid_power)
        cloud_battery_power = (
            cloud_data["master_inverter"]["batPower"] + cloud_data["slave_inverter"]["batPower"]
        ) / 1000

        if cloud_battery_power < 0:
            cloud_battery_status = f"{abs(cloud_battery_power):.2f} kW (Discharging)"
        elif cloud_battery_power > 0:
            cloud_battery_status = f"{cloud_battery_power:.2f} kW (Charging)"
        else:
            cloud_battery_status = "0.00 kW (Idle)"

        overview_data = [
            ["System Mode", system_mode, "N/A (Modbus only)"],
            ["Total AC Power", f"{total_ac:.2f} kW", f"{cloud_total_ac:.2f} kW"],
            ["Total PV Power", f"{total_pv:.2f} kW", f"{cloud_total_pv:.2f} kW"],
            ["Battery Average SoC", f"{avg_soc:.1f}%", f"{cloud_avg_soc:.1f}%"],
            ["Total Yield Today", f"{daily_yield:.2f} kWh", f"{cloud_daily_yield:.2f} kWh"],
            ["Grid Export Power", f"{grid_export:.2f} kW", f"{cloud_grid_export:.2f} kW"],
            ["Grid Import Power", f"{grid_import:.2f} kW", f"{cloud_grid_import:.2f} kW"],
            ["Battery Net Power", battery_status, cloud_battery_status],
        ]

        headers = ["Parameter", "Modbus TCP", "Cloud API"]
    else:
        overview_data = [
            ["System Mode", system_mode],
            ["Total AC Power", f"{total_ac:.2f} kW"],
            ["Total PV Power", f"{total_pv:.2f} kW"],
            ["Battery Average SoC", f"{avg_soc:.1f}%"],
            ["Total Yield Today", f"{daily_yield:.2f} kWh"],
            ["Grid Export Power", f"{grid_export:.2f} kW"],
            ["Grid Import Power", f"{grid_import:.2f} kW"],
            ["Battery Net Power", battery_status],
        ]

        headers = ["Parameter", "Value"]

    print("\nSYSTEM OVERVIEW:")
    print(tabulate(overview_data, headers=headers, tablefmt="fancy_grid"))


def format_battery_status(data: dict[str, Any], cloud_data: dict[str, Any] | None = None) -> None:  # pylint: disable=too-many-locals  # Detailed battery status display
    """Format BATTERY STATUS section to match Cloud API format."""
    if not data.get("soc") or not data.get("battery_capacity") or not data.get("battery_power"):
        print("\nBATTERY STATUS:")
        print("❌ Battery data not available")
        return

    # Individual battery information
    master_soc = data["soc"]["master"]
    slave_soc = data["soc"]["slave"]
    master_capacity = data["battery_capacity"]["master"]
    slave_capacity = data["battery_capacity"]["slave"]
    master_power = data["battery_power"]["master"]["power"]
    slave_power = data["battery_power"]["slave"]["power"]
    master_temp = data.get("battery_temperature", {}).get("master")
    slave_temp = data.get("battery_temperature", {}).get("slave")

    # Calculate totals and stored energy
    total_capacity = master_capacity + slave_capacity
    total_stored = ((master_soc + slave_soc) / 2 / 100) * total_capacity

    # Separate charging and discharging power
    charging_power = 0
    discharging_power = 0

    if master_power > 0:
        charging_power += master_power
    else:
        discharging_power += abs(master_power)

    if slave_power > 0:
        charging_power += slave_power
    else:
        discharging_power += abs(slave_power)

    # Build battery data with optional cloud comparison
    if cloud_data:
        # Cloud battery data
        cloud_master_soc = cloud_data["master_inverter"]["soc"]
        cloud_slave_soc = cloud_data["slave_inverter"]["soc"]
        cloud_master_power = cloud_data["master_inverter"]["batPower"] / 1000
        cloud_slave_power = cloud_data["slave_inverter"]["batPower"] / 1000

        cloud_charging_power = 0
        cloud_discharging_power = 0

        if cloud_master_power > 0:
            cloud_charging_power += cloud_master_power
        else:
            cloud_discharging_power += abs(cloud_master_power)

        if cloud_slave_power > 0:
            cloud_charging_power += cloud_slave_power
        else:
            cloud_discharging_power += abs(cloud_slave_power)

        # Assume same total capacity for cloud (not provided by Cloud API)
        cloud_total_stored = ((cloud_master_soc + cloud_slave_soc) / 2 / 100) * total_capacity

        battery_data = [
            ["Master SoC", f"{master_soc:.1f}%", f"{cloud_master_soc:.1f}%"],
            ["Slave SoC", f"{slave_soc:.1f}%", f"{cloud_slave_soc:.1f}%"],
            [
                "Master Temperature",
                f"{master_temp}°C" if master_temp is not None else "N/A",
                "N/A (Cloud API)",
            ],
            [
                "Slave Temperature",
                f"{slave_temp}°C" if slave_temp is not None else "N/A",
                "N/A (Cloud API)",
            ],
            [
                "Charging Power",
                f"{charging_power / 1000:.2f} kW" if charging_power > 0 else "0.00 kW",
                f"{cloud_charging_power:.2f} kW" if cloud_charging_power > 0 else "0.00 kW",
            ],
            [
                "Discharging Power",
                f"{discharging_power / 1000:.2f} kW" if discharging_power > 0 else "0.00 kW",
                f"{cloud_discharging_power:.2f} kW" if cloud_discharging_power > 0 else "0.00 kW",
            ],
            ["Total Capacity", f"{total_capacity:.2f} kWh", "N/A (Modbus only)"],
            ["Current Energy Stored", f"{total_stored:.2f} kWh", f"{cloud_total_stored:.2f} kWh"],
        ]

        headers = ["Parameter", "Modbus TCP", "Cloud API"]
    else:
        battery_data = [
            ["Master SoC", f"{master_soc:.1f}%"],
            ["Slave SoC", f"{slave_soc:.1f}%"],
            ["Master Temperature", f"{master_temp}°C" if master_temp is not None else "N/A"],
            ["Slave Temperature", f"{slave_temp}°C" if slave_temp is not None else "N/A"],
            [
                "Charging Power",
                f"{charging_power / 1000:.2f} kW" if charging_power > 0 else "0.00 kW",
            ],
            [
                "Discharging Power",
                f"{discharging_power / 1000:.2f} kW" if discharging_power > 0 else "0.00 kW",
            ],
            ["Total Capacity", f"{total_capacity:.2f} kWh"],
            ["Current Energy Stored", f"{total_stored:.2f} kWh"],
        ]

        headers = ["Parameter", "Value"]

    print("\nBATTERY STATUS:")
    print(tabulate(battery_data, headers=headers, tablefmt="fancy_grid"))


def format_pv_generation_status(
    data: dict[str, Any], cloud_data: dict[str, Any] | None = None
) -> None:
    """Format PV GENERATION STATUS section to match Cloud API format."""
    if not data.get("pv_power") or not data.get("daily_yield"):
        print("\nPV GENERATION STATUS:")
        print("❌ PV data not available")
        return

    # Individual PV string information with safe access
    master_pv = data["pv_power"].get("master", {})
    slave_pv = data["pv_power"].get("slave", {})
    master_yield = data["daily_yield"].get("master", 0)
    slave_yield = data["daily_yield"].get("slave", 0)

    # Handle None values gracefully
    master_pv1 = master_pv.get("pv1", 0) if master_pv else 0
    master_pv2 = master_pv.get("pv2", 0) if master_pv else 0
    slave_pv1 = slave_pv.get("pv1", 0) if slave_pv else 0
    slave_pv2 = slave_pv.get("pv2", 0) if slave_pv else 0

    # Build PV data with optional cloud comparison
    if cloud_data:
        # Cloud PV data
        cloud_master_pv1 = cloud_data["master_inverter"]["powerdc1"] / 1000
        cloud_master_pv2 = cloud_data["master_inverter"]["powerdc2"] / 1000
        cloud_slave_pv1 = cloud_data["slave_inverter"]["powerdc1"] / 1000
        cloud_slave_pv2 = cloud_data["slave_inverter"]["powerdc2"] / 1000
        cloud_master_yield = cloud_data["master_inverter"]["yieldtoday"]
        cloud_slave_yield = cloud_data["slave_inverter"]["yieldtoday"]

        pv_data = [
            ["Master PV1", f"{master_pv1 / 1000:.2f} kW", f"{cloud_master_pv1:.2f} kW"],
            ["Master PV2", f"{master_pv2 / 1000:.2f} kW", f"{cloud_master_pv2:.2f} kW"],
            ["Slave PV1", f"{slave_pv1 / 1000:.2f} kW", f"{cloud_slave_pv1:.2f} kW"],
            ["Slave PV2", f"{slave_pv2 / 1000:.2f} kW", f"{cloud_slave_pv2:.2f} kW"],
            ["Master Yield Today", f"{master_yield:.2f} kWh", f"{cloud_master_yield:.2f} kWh"],
            ["Slave Yield Today", f"{slave_yield:.2f} kWh", f"{cloud_slave_yield:.2f} kWh"],
        ]

        headers = ["Parameter", "Modbus TCP", "Cloud API"]
    else:
        pv_data = [
            ["Master PV1", f"{master_pv1 / 1000:.2f} kW"],
            ["Master PV2", f"{master_pv2 / 1000:.2f} kW"],
            ["Slave PV1", f"{slave_pv1 / 1000:.2f} kW"],
            ["Slave PV2", f"{slave_pv2 / 1000:.2f} kW"],
            ["Master Yield Today", f"{master_yield:.2f} kWh"],
            ["Slave Yield Today", f"{slave_yield:.2f} kWh"],
        ]

        headers = ["Parameter", "Value"]

    print("\nPV GENERATION STATUS:")
    print(tabulate(pv_data, headers=headers, tablefmt="fancy_grid"))


def format_grid_totals(data: dict[str, Any]) -> None:
    """Format GRID IMPORT/EXPORT TOTALS section.

    Note: Cloud API does not provide cumulative grid import/export totals,
    so this function only displays Modbus data without cloud comparison.
    """
    if not data.get("grid_totals"):
        print("\nGRID IMPORT/EXPORT TOTALS:")
        print("❌ Grid totals data not available")
        return

    # Individual grid total information
    master_import = data["grid_totals"].get("master", {}).get("import_kwh", 0)
    master_export = data["grid_totals"].get("master", {}).get("export_kwh", 0)
    slave_import = data["grid_totals"].get("slave", {}).get("import_kwh", 0)
    slave_export = data["grid_totals"].get("slave", {}).get("export_kwh", 0)

    # Calculate totals
    total_import = master_import + slave_import
    total_export = master_export + slave_export

    # Build grid totals data (no cloud API comparison available)
    grid_totals_data = [
        ["Master Grid Import", f"{master_import:.2f} kWh"],
        ["Master Grid Export", f"{master_export:.2f} kWh"],
        ["Slave Grid Import", f"{slave_import:.2f} kWh"],
        ["Slave Grid Export", f"{slave_export:.2f} kWh"],
        ["Total Grid Import", f"{total_import:.2f} kWh"],
        ["Total Grid Export", f"{total_export:.2f} kWh"],
    ]

    headers = ["Parameter", "Value"]

    print("\nGRID IMPORT/EXPORT TOTALS (Cumulative since installation):")
    print(tabulate(grid_totals_data, headers=headers, tablefmt="fancy_grid"))


def format_inverters_status(data: dict[str, Any], cloud_data: dict[str, Any] | None = None) -> None:  # pylint: disable=too-many-locals  # Per-inverter status display
    """Format INVERTERS STATUS section to match Cloud API format."""
    if not data.get("serial_numbers") or not data.get("run_mode") or not data.get("timestamps"):
        print("\nINVERTERS STATUS:")
        print("❌ Inverter status data not available")
        return

    # Individual inverter information
    master_serial = data["serial_numbers"]["master"]
    slave_serial = data["serial_numbers"]["slave"]
    master_mode = data["run_mode"]["master"]
    slave_mode = data["run_mode"]["slave"]
    master_time = data["timestamps"]["master"]
    slave_time = data["timestamps"]["slave"]
    master_ac = data.get("ac_power", {}).get("master", 0)
    slave_ac = data.get("ac_power", {}).get("slave", 0)
    master_battery = data.get("battery_power", {}).get("master", {}).get("power", 0)
    slave_battery = data.get("battery_power", {}).get("slave", {}).get("power", 0)
    master_battery_mode = data.get("battery_power", {}).get("master", {}).get("mode", "Unknown")
    slave_battery_mode = data.get("battery_power", {}).get("slave", {}).get("mode", "Unknown")
    grid_power = data.get("grid_power", {}).get("master", 0)

    # Create the inverter status table matching Cloud API format
    # Note: Cloud API shows positive values for discharging, negative for charging
    # Modbus gives negative for discharging, so we need to flip the sign
    master_battery_display = -master_battery if master_battery != 0 else 0.0
    slave_battery_display = -slave_battery if slave_battery != 0 else 0.0

    # Get system mode for display
    system_mode = data.get("system_mode", "Unknown")

    # Build inverter data with optional cloud comparison
    if cloud_data:
        # Cloud inverter data
        cloud_master_ac = cloud_data["master_inverter"]["acpower"] / 1000
        cloud_slave_ac = cloud_data["slave_inverter"]["acpower"] / 1000
        cloud_master_battery = (
            -cloud_data["master_inverter"]["batPower"] / 1000
        )  # Cloud API sign convention
        cloud_slave_battery = -cloud_data["slave_inverter"]["batPower"] / 1000
        cloud_grid_power = cloud_data["master_inverter"]["feedinpower"] / 1000
        cloud_master_time = cloud_data["master_inverter"]["uploadTime"]
        cloud_slave_time = cloud_data["slave_inverter"]["uploadTime"]

        # Determine cloud battery modes
        cloud_master_battery_mode = (
            "Discharging"
            if cloud_data["master_inverter"]["batPower"] < 0
            else "Charging"
            if cloud_data["master_inverter"]["batPower"] > 0
            else "Idle"
        )
        cloud_slave_battery_mode = (
            "Discharging"
            if cloud_data["slave_inverter"]["batPower"] < 0
            else "Charging"
            if cloud_data["slave_inverter"]["batPower"] > 0
            else "Idle"
        )

        inverter_data = [
            [
                "Serial Number",
                f"{master_serial} | {cloud_data['master_inverter']['inverterSN']}",
                f"{slave_serial} | {cloud_data['slave_inverter']['inverterSN']}",
            ],
            [
                "Status",
                f"{master_mode} | {cloud_data['master_inverter']['inverterStatus']}",
                f"{slave_mode} | {cloud_data['slave_inverter']['inverterStatus']}",
            ],
            ["System Mode", f"{system_mode} | N/A (Modbus only)", ""],
            [
                "AC Power",
                f"{master_ac / 1000:.2f} kW | {cloud_master_ac:.2f} kW",
                f"{slave_ac / 1000:.2f} kW | {cloud_slave_ac:.2f} kW",
            ],
            [
                "Battery Power (+ discharging / - charging)",
                f"{master_battery_display / 1000:.2f} kW | {cloud_master_battery:.2f} kW",
                f"{slave_battery_display / 1000:.2f} kW | {cloud_slave_battery:.2f} kW",
            ],
            [
                "Battery Mode",
                f"{master_battery_mode} | {cloud_master_battery_mode}",
                f"{slave_battery_mode} | {cloud_slave_battery_mode}",
            ],
            [
                "Grid Power (+ exporting / - importing)",
                f"{grid_power / 1000:.2f} kW | {cloud_grid_power:.2f} kW",
                "N/A",
            ],
            [
                "Last Update",
                f"{master_time} | {cloud_master_time}",
                f"{slave_time} | {cloud_slave_time}",
            ],
        ]

        headers = ["Parameter", "Master (Modbus | Cloud)", "Slave (Modbus | Cloud)"]
    else:
        inverter_data = [
            ["Serial Number", master_serial, slave_serial],
            ["Status", master_mode, slave_mode],
            ["System Mode", system_mode, ""],
            ["AC Power", f"{master_ac / 1000:.2f} kW", f"{slave_ac / 1000:.2f} kW"],
            [
                "Battery Power (+ discharging / - charging)",
                f"{master_battery_display / 1000:.2f} kW",
                f"{slave_battery_display / 1000:.2f} kW",
            ],
            ["Battery Mode", master_battery_mode, slave_battery_mode],
            ["Grid Power (+ exporting / - importing)", f"{grid_power / 1000:.2f} kW", "N/A"],
            ["Last Update", master_time, slave_time],
        ]

        headers = ["Parameter", "Master Inverter", "Slave Inverter"]

    print("\nINVERTERS STATUS:")
    print(tabulate(inverter_data, headers=headers, tablefmt="fancy_grid"))


def main() -> None:
    """Execute enhanced status script."""
    # Parse command line arguments
    args = parse_arguments()

    # Set logging level based on command line argument
    log_level = getattr(logging, args.log_level.upper())
    logging.getLogger().setLevel(log_level)
    logger.info("Logging level set to: %s", args.log_level)

    # Setup performance logging if requested
    if args.performance_logging:
        # Generate output filename if not provided
        if args.performance_output:
            perf_output_file = args.performance_output
        else:
            timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
            perf_output_file = f"performance_{timestamp}.log"

        # Setup performance logger with file handler
        perf_logger = logging.getLogger("src.api_clients.solax_modbus_client.performance")
        perf_logger.setLevel(logging.INFO)

        # Create file handler for performance logs
        perf_handler = logging.FileHandler(perf_output_file)
        perf_formatter = logging.Formatter("%(message)s")  # Just the JSON data
        perf_handler.setFormatter(perf_formatter)
        perf_logger.addHandler(perf_handler)

        print(f"📊 Performance logging enabled - output: {perf_output_file}")

    # Load configuration
    config = load_config(args.config)
    if not config:
        sys.exit(1)

    if not config.get("solaX_cloud_api", {}).get("modbus_enabled", False):
        print("❌ Modbus is not enabled in configuration")
        sys.exit(1)

    # Gather all system data
    data = gather_all_modbus_data(config)

    if not data:
        print("❌ Failed to gather system data")
        sys.exit(1)

    # Gather cloud data if comparison mode is enabled
    cloud_data = None
    if args.compare_cloud:
        logger.info("Cloud comparison mode enabled - gathering Cloud API data...")
        cloud_data = gather_cloud_data(config)
        if not cloud_data:
            logger.warning(
                "Could not retrieve Cloud API data - proceeding with Modbus-only display"
            )

    # Calculate overview metrics
    overview = calculate_system_overview(data)

    # Get current timestamp - UK local time for display with timezone indicator
    uk_tz = pytz.timezone("Europe/London")
    utc_time = datetime.now(tz=UTC)
    uk_time = utc_time.astimezone(uk_tz)
    tz_name = uk_time.strftime("%Z")  # BST or GMT
    local_time = uk_time.strftime("%Y-%m-%d %H:%M:%S")
    utc_time_str = utc_time.strftime("%Y-%m-%d %H:%M:%S UTC")

    # Print header matching Cloud API format
    print("=" * 80)
    print(f"SolaX System Status Report - {local_time} {tz_name} ({utc_time_str})")
    print("=" * 80)

    # Format all sections with optional cloud comparison
    format_system_overview(overview, data, cloud_data)
    format_battery_status(data, cloud_data)
    format_pv_generation_status(data, cloud_data)
    format_grid_totals(data)
    format_inverters_status(data, cloud_data)


if __name__ == "__main__":
    main()
