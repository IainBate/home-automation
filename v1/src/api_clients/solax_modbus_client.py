"""SolaX Modbus TCP API Client Module.

Handles direct communication with SolaX inverters via Modbus TCP protocol.
Provides read access to inverter data and RESTRICTED write access for work mode control.

SAFETY: This module implements strict safety controls:
- READ operations: Full access to all documented registers
- WRITE operations: RESTRICTED to work mode registers with complete combination validation
- MASTER ONLY: All write operations restricted to master inverter only
- FAIL-FAST: All operations use comprehensive error handling
"""

# pylint: disable=too-many-lines
# Rationale: Public API facade for Modbus hardware interface.
# Implementation details already split into internal modules (_modbus_protocol,
# _modbus_reader, _modbus_data_maps, _modbus_mode_controller, _modbus_validator).
# Line count reflects comprehensive hardware interface coverage (15+ operations).
# Splitting the public API would complicate imports and reduce discoverability.

from __future__ import annotations

import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Debug instrumentation for detecting unmarked hardware tests
try:
    # Add dev_scripts to path for debug module import
    PROJECT_ROOT = str(Path(__file__).parent.parent.parent)
    DEV_SCRIPTS_PATH = str(Path(PROJECT_ROOT) / "dev_scripts")  # pragma: no cover
    if DEV_SCRIPTS_PATH not in sys.path:  # pragma: no cover
        sys.path.insert(0, DEV_SCRIPTS_PATH)  # pragma: no cover

    from hardware_debug_access import (  # noqa: F401  # pylint: disable=unused-import  # pragma: no cover
        log_hardware_access,
        log_hardware_write_violation,
        should_log_hardware_access,
    )

    DEBUG_ENABLED = True  # pragma: no cover
except ImportError:  # pragma: no cover
    DEBUG_ENABLED = False  # pragma: no cover

    def log_hardware_access(*_args: object, **_kwargs: object) -> None:  # pragma: no cover
        """No-op stub when hardware debug module unavailable."""  # pragma: no cover
        # pragma: no cover  # pragma: no cover

    def log_hardware_write_violation(*_args: object, **_kwargs: object) -> None:  # pragma: no cover
        """No-op stub when hardware debug module unavailable."""  # pragma: no cover
        # pragma: no cover  # pragma: no cover

    def should_log_hardware_access(*_args: object, **_kwargs: object) -> bool:  # noqa: ARG001  # pragma: no cover
        """No-op stub when hardware debug module unavailable."""  # pragma: no cover
        return False  # pragma: no cover  # pragma: no cover


# Import BatteryMode for type-safe mode handling - HARDWARE BOUNDARY POINT
# This is one of only TWO places where BatteryMode enum is converted:
# 1. HERE: Hardware interface (Modbus numeric ↔ BatteryMode enum)
# 2. battery_simulation.py: User display (BatteryMode enum ↔ display strings)
from src.core_logic.battery_simulation import BatteryMode, battery_mode_to_display_string

from ._modbus_data_maps import _format_rtc_timestamp as _datamaps_format_rtc_timestamp
from ._modbus_data_maps import _format_serial_number as _datamaps_format_serial_number
from ._modbus_data_maps import _interpret_manual_mode as _datamaps_interpret_manual_mode
from ._modbus_data_maps import _interpret_run_mode as _datamaps_interpret_run_mode
from ._modbus_data_maps import _interpret_work_mode as _datamaps_interpret_work_mode
from ._modbus_mode_controller import VALID_WORK_MODE_COMBINATIONS
from ._modbus_mode_controller import (
    _check_mode_change_safety as _controller_check_mode_change_safety,
)
from ._modbus_mode_controller import (
    _execute_mode_change_hardware as _controller_execute_mode_change_hardware,
)
from ._modbus_mode_controller import (
    _get_mode_change_log_path as _controller_get_mode_change_log_path,
)
from ._modbus_mode_controller import _get_project_root as _controller_get_project_root
from ._modbus_mode_controller import _load_mode_change_log as _controller_load_mode_change_log
from ._modbus_mode_controller import _locked_mode_change_log as _controller_locked_mode_change_log

# Mode control functions
from ._modbus_mode_controller import _log_mode_change as _controller_log_mode_change
from ._modbus_mode_controller import _save_mode_change_log as _controller_save_mode_change_log
from ._modbus_mode_controller import _set_master_work_mode as _controller_set_master_work_mode
from ._modbus_mode_controller import _write_single_register as _controller_write_single_register

# Import functions from internal modules and expose them at module level for test compatibility
# This ensures that tests can patch functions like 'src.api_clients.solax_modbus_client._connect_modbus_client'
# Protocol and data mapping functions
from ._modbus_protocol import _connect_modbus_client as _protocol_connect_modbus_client
from ._modbus_protocol import _read_holding_registers as _protocol_read_holding_registers
from ._modbus_protocol import _read_input_registers as _protocol_read_input_registers
from ._modbus_reader import _extract_input_register_data as _reader_extract_input_register_data
from ._modbus_reader import (
    _parallel_bulk_read_both_inverters as _reader_parallel_bulk_read_both_inverters,
)
from ._modbus_reader import (
    _read_bulk_input_data_single_inverter as _reader_read_bulk_input_data_single_inverter,
)
from ._modbus_reader import _read_bulk_work_mode_data as _reader_read_bulk_work_mode_data
from ._modbus_reader import _read_single_ac_power as _reader_read_single_ac_power
from ._modbus_reader import _read_single_battery_capacity as _reader_read_single_battery_capacity
from ._modbus_reader import _read_single_battery_power as _reader_read_single_battery_power
from ._modbus_reader import (
    _read_single_battery_temperature as _reader_read_single_battery_temperature,
)
from ._modbus_reader import _read_single_daily_yield as _reader_read_single_daily_yield
from ._modbus_reader import (
    _read_single_grid_export_total as _reader_read_single_grid_export_total,
)
from ._modbus_reader import (
    _read_single_grid_import_total as _reader_read_single_grid_import_total,
)
from ._modbus_reader import _read_single_grid_power as _reader_read_single_grid_power

# Reading functions
from ._modbus_reader import _read_single_inverter_serial as _reader_read_single_inverter_serial
from ._modbus_reader import _read_single_pv_power as _reader_read_single_pv_power
from ._modbus_reader import _read_single_rtc_timestamp as _reader_read_single_rtc_timestamp
from ._modbus_reader import _read_single_run_mode as _reader_read_single_run_mode
from ._modbus_reader import _read_single_soc as _reader_read_single_soc

# Validation functions
from ._modbus_validator import PowerValidationResult  # noqa: F401  # pylint: disable=unused-import

# Used by test_power_validation.py
from ._modbus_validator import (
    read_individual_registers_comprehensive as _validator_read_individual_registers_comprehensive,
)
from ._modbus_validator import (
    validate_power_data_physical_limits as _validator_validate_power_data_physical_limits,
)

# Re-export functions at module level with original names for test compatibility
# Tests expect to patch 'src.api_clients.solax_modbus_client._connect_modbus_client' etc.

# Protocol functions
_connect_modbus_client = _protocol_connect_modbus_client
_read_holding_registers = _protocol_read_holding_registers
_read_input_registers = _protocol_read_input_registers

# Data mapping functions
_format_serial_number = _datamaps_format_serial_number
_format_rtc_timestamp = _datamaps_format_rtc_timestamp
_interpret_run_mode = _datamaps_interpret_run_mode
_interpret_work_mode = _datamaps_interpret_work_mode
_interpret_manual_mode = _datamaps_interpret_manual_mode

# Reading functions
_read_single_inverter_serial = _reader_read_single_inverter_serial
_read_single_ac_power = _reader_read_single_ac_power
_read_single_rtc_timestamp = _reader_read_single_rtc_timestamp
_read_single_run_mode = _reader_read_single_run_mode
_read_single_battery_power = _reader_read_single_battery_power
_read_single_grid_power = _reader_read_single_grid_power
_read_single_soc = _reader_read_single_soc
_read_single_pv_power = _reader_read_single_pv_power
_read_single_daily_yield = _reader_read_single_daily_yield
_read_single_grid_export_total = _reader_read_single_grid_export_total
_read_single_grid_import_total = _reader_read_single_grid_import_total
_read_single_battery_capacity = _reader_read_single_battery_capacity
_extract_input_register_data = _reader_extract_input_register_data
_read_bulk_input_data_single_inverter = _reader_read_bulk_input_data_single_inverter
_read_bulk_work_mode_data = _reader_read_bulk_work_mode_data
_parallel_bulk_read_both_inverters = _reader_parallel_bulk_read_both_inverters

# Mode control functions
_log_mode_change = _controller_log_mode_change
_check_mode_change_safety = _controller_check_mode_change_safety
_execute_mode_change_hardware = _controller_execute_mode_change_hardware
_locked_mode_change_log = _controller_locked_mode_change_log
_set_master_work_mode = _controller_set_master_work_mode
_get_project_root = _controller_get_project_root
_get_mode_change_log_path = _controller_get_mode_change_log_path
_load_mode_change_log = _controller_load_mode_change_log
_save_mode_change_log = _controller_save_mode_change_log
_write_single_register = _controller_write_single_register

# Validation functions
validate_power_data_physical_limits = _validator_validate_power_data_physical_limits
read_individual_registers_comprehensive = _validator_read_individual_registers_comprehensive

# Setup basic logging
logger = logging.getLogger(__name__)


def solax_modbus_set_work_mode(  # pylint: disable=too-many-return-statements
    config: dict[str, Any],
    mode: BatteryMode,
    changed_by: str = "unknown",
    *,
    test_mode: bool = False,
    force_unsafe: bool = False,
) -> dict[str, Any]:
    """UNIVERSAL mode change function - replaces all previous mode change functions.

    Guard clause pattern: early returns for independent validation checks.
    Alternative nested if/else would reduce readability and violate Clean Code principles.

    This comprehensive function handles ALL mode change scenarios with:
    - File-locked safety (prevents race conditions)
    - Enhanced timezone handling (robust legacy compatibility)
    - Comprehensive error reporting (actionable error types)
    - Config-based interface (consistent across all callers)
    - Test mode support (daemon + testing compatibility)

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
        mode: Work mode to set - must be BatteryMode.SELF_USE, FORCE_CHARGE, or FORCE_DISCHARGE
        changed_by: Human-readable reason for the mode change
        test_mode: If True, skip actual hardware writes (for testing)
        force_unsafe: If True, bypass timing safety checks

    Returns:
        Dictionary with detailed execution results:
        {
            'success': bool,           # Whether mode change succeeded
            'message': str,            # Success or error message
            'error_type': str,         # 'safety_interval', 'mode_read_failed', etc.
            'error_message': str,      # Detailed error description
            'safety_warning': bool,    # True if blocked by safety timing
            'safety_message': str,     # Safety-specific message for UI
            'from_mode': str,         # Previous mode (for confirmation)
            'to_mode': str,           # New mode (for confirmation)
            'current_mode': str,      # Current actual mode
            'actual_mode': str,       # Mode after operation
            'change_id': str          # Unique identifier for this change
        }

    """
    try:
        # Generate unique change ID
        change_id = f"unified_{int(time.time() * 1000)}"

        # Step 1: Validate mode parameter
        if mode not in VALID_WORK_MODE_COMBINATIONS:
            return {
                "success": False,
                "message": f"Invalid work mode '{mode}'. Valid modes: {list(VALID_WORK_MODE_COMBINATIONS.keys())}",
                "error_type": "invalid_mode",
                "error_message": f"Invalid mode: {mode}",
                "safety_warning": False,
                "safety_message": "",
                "from_mode": None,
                "to_mode": None,
                "current_mode": None,
                "actual_mode": None,
                "change_id": change_id,
            }

        # Step 2: Check current mode first - with enhanced error detection
        current_mode = solax_modbus_work_mode(config)
        if current_mode is None:
            return {
                "success": False,
                "message": "Unable to read current inverter mode - cannot proceed safely",
                "error_type": "mode_read_failed",
                "error_message": "Mode change aborted for safety - inverter communication failed",
                "safety_warning": False,
                "safety_message": "",
                "from_mode": None,
                "to_mode": None,
                "current_mode": None,
                "actual_mode": None,
                "change_id": change_id,
            }

        # If already in requested mode, silently succeed
        if current_mode == mode:
            mode_display = battery_mode_to_display_string(mode)
            return {
                "success": True,
                "message": f"System already in {mode_display} mode",
                "error_type": "mode_check",
                "error_message": "Already in target mode",
                "safety_warning": False,
                "safety_message": "",
                "from_mode": mode_display,
                "to_mode": mode_display,
                "current_mode": current_mode,
                "actual_mode": current_mode,
                "change_id": change_id,
            }

        # Step 3: In test mode, simulate successful operation
        if test_mode:
            from_mode_str = battery_mode_to_display_string(current_mode)
            to_mode_str = battery_mode_to_display_string(mode)
            logger.info(
                "TEST MODE: Would change mode from %s to %s - %s",
                from_mode_str,
                to_mode_str,
                changed_by,
            )
            return {
                "success": True,
                "message": f"TEST MODE: Would change from {from_mode_str} to {to_mode_str}",
                "error_type": None,
                "error_message": None,
                "safety_warning": False,
                "safety_message": "",
                "from_mode": from_mode_str,
                "to_mode": to_mode_str,
                "current_mode": current_mode,
                "actual_mode": mode,  # Simulated target mode
                "change_id": change_id,
            }

        # Step 4: Perform mode change with file locking for safety
        try:
            with _locked_mode_change_log() as (log_data, save_log):
                # Check safety with current log data using file-locked context (unless forced)
                if not force_unsafe:
                    safety_passed, safety_message = _check_mode_change_safety(
                        log_data=log_data,
                        forced=False,
                    )

                    if not safety_passed:
                        logger.warning("Safety check failed: %s", safety_message)
                        return {
                            "success": False,
                            "message": "Mode change blocked by safety timing restrictions",
                            "error_type": "safety_interval",
                            "error_message": safety_message,
                            "safety_warning": True,
                            "safety_message": safety_message,
                            "from_mode": battery_mode_to_display_string(current_mode),
                            "to_mode": battery_mode_to_display_string(mode),
                            "current_mode": current_mode,
                            "actual_mode": current_mode,
                            "change_id": change_id,
                        }
                else:
                    # Force override - log warning but proceed
                    logger.warning(
                        "SAFETY BYPASSED: force_unsafe=True - mode change proceeding without timing checks"
                    )

                # Safety passed - perform actual mode change directly
                success = _execute_mode_change_hardware(config, mode, test_mode=test_mode)

                if success:
                    # Get updated mode after change
                    new_mode = solax_modbus_work_mode(config)
                    from_mode_str = (
                        battery_mode_to_display_string(current_mode) if current_mode else "Unknown"
                    )
                    to_mode_str = (
                        battery_mode_to_display_string(new_mode) if new_mode else "Unknown"
                    )
                    logger.info(
                        "Mode change successful: %s -> %s - %s",
                        from_mode_str,
                        to_mode_str,
                        changed_by,
                    )

                    # Log successful change using the locked context
                    log_entry = {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "previous_mode": current_mode.value if current_mode else "unknown",
                        "new_mode": mode.value,
                        "changed_by": changed_by,
                        "forced": force_unsafe,
                    }
                    log_data["last_mode_change"] = log_entry
                    save_log(log_data)

                    return {
                        "success": True,
                        "message": f"Successfully changed mode from {from_mode_str} to {to_mode_str}",
                        "error_type": None,
                        "error_message": None,
                        "safety_warning": False,
                        "safety_message": "",
                        "from_mode": from_mode_str,
                        "to_mode": to_mode_str,
                        "current_mode": current_mode,
                        "actual_mode": new_mode or mode,
                        "change_id": change_id,
                    }
                logger.error("Failed to execute mode change to %s", mode)
                return {
                    "success": False,
                    "message": f"Hardware write failed - unable to change to {battery_mode_to_display_string(mode)} mode",
                    "error_type": "hardware_failure",
                    "error_message": "Failed to execute mode change on hardware",
                    "safety_warning": False,
                    "safety_message": "",
                    "from_mode": battery_mode_to_display_string(current_mode),
                    "to_mode": battery_mode_to_display_string(mode),
                    "current_mode": current_mode,
                    "actual_mode": current_mode,
                    "change_id": change_id,
                }

        except TimeoutError:
            logger.exception("File lock timeout during mode change")
            return {
                "success": False,
                "message": "System busy - unable to acquire safety lock",
                "error_type": "lock_timeout",
                "error_message": "File lock timeout during mode change",
                "safety_warning": False,
                "safety_message": "",
                "from_mode": battery_mode_to_display_string(current_mode),
                "to_mode": battery_mode_to_display_string(mode),
                "current_mode": current_mode,
                "actual_mode": current_mode,
                "change_id": change_id,
            }

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Unexpected error in unified mode change")
        return {
            "success": False,
            "message": "Unexpected error occurred",
            "error_type": "unexpected_error",
            "error_message": "Unexpected error in unified mode change",
            "safety_warning": False,
            "safety_message": "",
            "from_mode": None,
            "to_mode": None,
            "current_mode": None,
            "actual_mode": None,
            "change_id": change_id,
        }


def solax_modbus_serial_numbers(  # pylint: disable=too-many-return-statements
    config: dict[str, Any],
) -> dict[str, str] | None:
    """Read serial numbers from both inverters via Modbus TCP.

    Guard clause pattern: early returns for independent validation checks.
    Alternative nested if/else would reduce readability and violate Clean Code principles.

    This function establishes connections to both master and slave inverters,
    reads their serial numbers from holding registers 0x0000-0x0006, and
    returns them in a dictionary format.

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
                with Modbus TCP settings and IP addresses

    Returns:
        Dictionary with format:
        {
            "master": "H4602AJ8530050",
            "slave": "H4602AL3067054"
        }
        Returns None if any error occurs (fail-fast approach)

    Example:
        config = load_static_config("config.yaml")
        serial_numbers = solax_modbus_serial_numbers(config)
        if serial_numbers:
            print(f"Master: {serial_numbers['master']}")
            print(f"Slave: {serial_numbers['slave']}")

    """
    try:
        # Validate configuration
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            logger.error("Modbus TCP is not enabled in configuration")
            return None

        # Extract configuration parameters
        master_ip = solax_config.get("master_ip")
        slave_ip = solax_config.get("slave_ip")
        port = solax_config.get("modbus_port", 502)
        connection_timeout = solax_config.get("modbus_connection_timeout", 10)
        master_address = solax_config.get("master_modbus_address", 1)
        slave_address = solax_config.get("slave_modbus_address", 2)
        min_interval = solax_config.get("min_command_interval", 1.0)

        # Validate required parameters
        if not master_ip or master_ip == "YOUR_IP_HERE":
            logger.error("Master inverter IP address not configured")
            return None

        if not slave_ip or slave_ip == "YOUR_IP_HERE":
            logger.error("Slave inverter IP address not configured")
            return None

        logger.debug("Master: %s:%s (address %s)", master_ip, port, master_address)
        logger.debug("Slave: %s:%s (address %s)", slave_ip, port, slave_address)

        # Read master inverter serial number
        master_serial = _read_single_inverter_serial(
            master_ip, port, connection_timeout, master_address, min_interval
        )

        if not master_serial:
            logger.error("Failed to read master inverter serial number")
            return None

        logger.debug("Master serial number: %s", master_serial)

        # Wait before connecting to second inverter
        time.sleep(min_interval)

        # Read slave inverter serial number
        slave_serial = _read_single_inverter_serial(
            slave_ip, port, connection_timeout, slave_address, min_interval
        )

        if not slave_serial:
            logger.error("Failed to read slave inverter serial number")
            return None

        logger.debug("Slave serial number: %s", slave_serial)

        # Return successful results
        result = {"master": master_serial, "slave": slave_serial}

        logger.info("Successfully read serial numbers from both inverters")

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Unexpected error in solax_modbus_serial_numbers")
        return None
    return result


def solax_modbus_ac_power(  # pylint: disable=too-many-return-statements
    config: dict[str, Any],
) -> dict[str, int] | None:
    """Read AC power values from both inverters via Modbus TCP.

    Guard clause pattern: early returns for independent validation checks.
    Alternative nested if/else would reduce readability and violate Clean Code principles.

    Reads register 0x0002 (GridPower) from both master and slave inverters.
    This register corresponds to the Cloud API 'acpower' field and represents
    grid power in watts (positive = exporting to grid, negative = importing).

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
                with Modbus TCP settings and IP addresses

    Returns:
        Dictionary with format:
        {
            "master": 2500,  # AC power in watts
            "slave": 2000    # AC power in watts
        }
        Returns None if any error occurs (fail-fast approach)

    Example:
        config = load_static_config("config.yaml")
        ac_power = solax_modbus_ac_power(config)
        if ac_power:
            total_power = ac_power['master'] + ac_power['slave']
            print(f"Total AC Power: {total_power}W")

    """
    try:
        # Validate configuration
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            logger.error("Modbus TCP is not enabled in configuration")
            return None

        # Extract configuration parameters
        master_ip = solax_config.get("master_ip")
        slave_ip = solax_config.get("slave_ip")
        port = solax_config.get("modbus_port", 502)
        connection_timeout = solax_config.get("modbus_connection_timeout", 10)
        master_address = solax_config.get("master_modbus_address", 1)
        slave_address = solax_config.get("slave_modbus_address", 2)
        min_interval = solax_config.get("min_command_interval", 1.0)

        # Validate required parameters
        if not master_ip or master_ip == "YOUR_IP_HERE":
            logger.error("Master inverter IP address not configured")
            return None

        if not slave_ip or slave_ip == "YOUR_IP_HERE":
            logger.error("Slave inverter IP address not configured")
            return None

        logger.debug("Master: %s:%s (address %s)", master_ip, port, master_address)
        logger.debug("Slave: %s:%s (address %s)", slave_ip, port, slave_address)

        # Read master inverter AC power
        master_power = _read_single_ac_power(
            master_ip, port, connection_timeout, master_address, min_interval
        )

        if master_power is None:
            logger.error("Failed to read master inverter AC power")
            return None

        logger.debug("Master AC power: %sW", master_power)

        # Wait before connecting to second inverter
        time.sleep(min_interval)

        # Read slave inverter AC power
        slave_power = _read_single_ac_power(
            slave_ip, port, connection_timeout, slave_address, min_interval
        )

        if slave_power is None:
            logger.error("Failed to read slave inverter AC power")
            return None

        logger.debug("Slave AC power: %sW", slave_power)

        # Return successful results
        result = {"master": master_power, "slave": slave_power}

        logger.info("Successfully read AC power from both inverters")

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Unexpected error in solax_modbus_ac_power")
        return None
    return result


def solax_modbus_battery_temperature(  # pylint: disable=too-many-return-statements
    config: dict[str, Any],
) -> dict[str, int] | None:
    """Read battery temperature from both inverters via Modbus TCP.

    Guard clause pattern: early returns for independent validation checks.
    Alternative nested if/else would reduce readability and violate Clean Code principles.

    Reads register 0x0018 (Battery Temperature) from both master and slave inverters.
    This register provides the current battery pack temperature which is critical for
    optimizing charge rates based on temperature constraints.

    Temperature Range: -20°C to +60°C (LiFePO4 operating limits)
    - Below 0°C: Charge rate severely limited (lithium plating risk)
    - 0-10°C: Charge rate reduced to ~20% (0.2C)
    - 10-20°C: Charge rate reduced to ~50% (0.5C)
    - 20-45°C: Full charge rate available (1.0C) - optimal range
    - 45-50°C: Charge rate reduced (thermal management)
    - Above 50°C: Charge rate severely limited (degradation risk)

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
                with Modbus TCP settings and IP addresses

    Returns:
        Dictionary with format:
        {
            "master": 25,  # Temperature in degrees Celsius
            "slave": 24    # Temperature in degrees Celsius
        }
        Returns None if any error occurs (fail-fast approach)

    Example:
        config = load_static_config("config.yaml")
        temperatures = solax_modbus_battery_temperature(config)
        if temperatures:
            avg_temp = (temperatures['master'] + temperatures['slave']) / 2
            print(f"Average battery temperature: {avg_temp}°C")

    """
    try:
        # Validate configuration
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            logger.error("Modbus TCP is not enabled in configuration")
            return None

        # Extract configuration parameters
        master_ip = solax_config.get("master_ip")
        slave_ip = solax_config.get("slave_ip")
        port = solax_config.get("modbus_port", 502)
        connection_timeout = solax_config.get("modbus_connection_timeout", 10)
        master_address = solax_config.get("master_modbus_address", 1)
        slave_address = solax_config.get("slave_modbus_address", 2)
        min_interval = solax_config.get("min_command_interval", 1.0)

        # Validate required parameters
        if not master_ip or master_ip == "YOUR_IP_HERE":
            logger.error("Master inverter IP address not configured")
            return None

        if not slave_ip or slave_ip == "YOUR_IP_HERE":
            logger.error("Slave inverter IP address not configured")
            return None

        logger.debug("Master: %s:%s (address %s)", master_ip, port, master_address)
        logger.debug("Slave: %s:%s (address %s)", slave_ip, port, slave_address)

        # Read master inverter battery temperature
        master_temperature = _reader_read_single_battery_temperature(
            master_ip, port, connection_timeout, master_address, min_interval
        )

        if master_temperature is None:
            logger.error("Failed to read master inverter battery temperature")
            return None

        logger.debug("Master battery temperature: %s°C", master_temperature)

        # Wait before connecting to second inverter
        time.sleep(min_interval)

        # Read slave inverter battery temperature
        slave_temperature = _reader_read_single_battery_temperature(
            slave_ip, port, connection_timeout, slave_address, min_interval
        )

        if slave_temperature is None:
            logger.error("Failed to read slave inverter battery temperature")
            return None

        logger.debug("Slave battery temperature: %s°C", slave_temperature)

        # Return successful results
        result = {"master": master_temperature, "slave": slave_temperature}

        logger.info("Successfully read battery temperature from both inverters")

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Unexpected error in solax_modbus_battery_temperature")
        return None
    return result


def solax_modbus_rtc_timestamps(  # pylint: disable=too-many-return-statements
    config: dict[str, Any],
) -> dict[str, str] | None:
    """Read RTC timestamps from both inverters via Modbus TCP.

    Guard clause pattern: early returns for independent validation checks.
    Alternative nested if/else would reduce readability and violate Clean Code principles.

    Reads registers 0x0085-0x008A (RTC) from both master and slave inverters.
    Uses the correct Function Code 0x03 (Holding Registers) and proper year
    calculation (register_value + 2000).

    IMPORTANT: Despite being listed under Input Registers in the documentation,
    RTC registers are actually accessible via Holding Registers (Function Code 0x03).

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
                with Modbus TCP settings and IP addresses

    Returns:
        Dictionary with format:
        {
            "master": "2025-06-28 19:46:14",  # ISO format timestamp
            "slave": "2025-06-28 19:46:22"    # ISO format timestamp
        }
        Returns None if any error occurs (fail-fast approach)

    Example:
        config = load_static_config("config.yaml")
        timestamps = solax_modbus_rtc_timestamps(config)
        if timestamps:
            print(f"Master time: {timestamps['master']}")
            print(f"Slave time: {timestamps['slave']}")

    """
    try:
        # Validate configuration
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            logger.error("Modbus TCP is not enabled in configuration")
            return None

        # Extract configuration parameters
        master_ip = solax_config.get("master_ip")
        slave_ip = solax_config.get("slave_ip")
        port = solax_config.get("modbus_port", 502)
        connection_timeout = solax_config.get("modbus_connection_timeout", 10)
        master_address = solax_config.get("master_modbus_address", 1)
        slave_address = solax_config.get("slave_modbus_address", 2)
        min_interval = solax_config.get("min_command_interval", 1.0)

        # Validate required parameters
        if not master_ip or master_ip == "YOUR_IP_HERE":
            logger.error("Master inverter IP address not configured")
            return None

        if not slave_ip or slave_ip == "YOUR_IP_HERE":
            logger.error("Slave inverter IP address not configured")
            return None

        logger.debug("Master: %s:%s (address %s)", master_ip, port, master_address)
        logger.debug("Slave: %s:%s (address %s)", slave_ip, port, slave_address)

        # Read master inverter RTC
        master_timestamp = _read_single_rtc_timestamp(
            master_ip, port, connection_timeout, master_address, min_interval
        )

        if not master_timestamp:
            logger.error("Failed to read master inverter RTC timestamp")
            return None

        logger.debug("Master RTC timestamp: %s", master_timestamp)

        # Wait before connecting to second inverter
        time.sleep(min_interval)

        # Read slave inverter RTC
        slave_timestamp = _read_single_rtc_timestamp(
            slave_ip, port, connection_timeout, slave_address, min_interval
        )

        if not slave_timestamp:
            logger.error("Failed to read slave inverter RTC timestamp")
            return None

        logger.debug("Slave RTC timestamp: %s", slave_timestamp)

        # Return successful results
        result = {"master": master_timestamp, "slave": slave_timestamp}

        logger.info("Successfully read RTC timestamps from both inverters")

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Unexpected error in solax_modbus_rtc_timestamps")
        return None
    return result


def solax_modbus_run_mode(  # pylint: disable=too-many-return-statements
    config: dict[str, Any],
) -> dict[str, str] | None:
    """Read inverter run mode status from both inverters via Modbus TCP.

    Guard clause pattern: early returns for independent validation checks.
    Alternative nested if/else would reduce readability and violate Clean Code principles.

    Reads register 0x0009 (Inverter Run Mode) from both master and slave inverters.
    This register provides the current operational status of each inverter.

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
                with Modbus TCP settings and IP addresses

    Returns:
        Dictionary with format:
        {
            "master": "Normal Mode",     # Descriptive status text
            "slave": "TOU Charging"      # Descriptive status text
        }
        Returns None if any error occurs (fail-fast approach)

    Example:
        config = load_static_config("config.yaml")
        run_modes = solax_modbus_run_mode(config)
        if run_modes:
            print(f"Master Status: {run_modes['master']}")
            print(f"Slave Status: {run_modes['slave']}")

    """
    try:
        # Validate configuration
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            logger.error("Modbus TCP is not enabled in configuration")
            return None

        # Extract configuration parameters
        master_ip = solax_config.get("master_ip")
        slave_ip = solax_config.get("slave_ip")
        port = solax_config.get("modbus_port", 502)
        connection_timeout = solax_config.get("modbus_connection_timeout", 10)
        master_address = solax_config.get("master_modbus_address", 1)
        slave_address = solax_config.get("slave_modbus_address", 2)
        min_interval = solax_config.get("min_command_interval", 1.0)

        # Validate required parameters
        if not master_ip or master_ip == "YOUR_IP_HERE":
            logger.error("Master inverter IP address not configured")
            return None

        if not slave_ip or slave_ip == "YOUR_IP_HERE":
            logger.error("Slave inverter IP address not configured")
            return None

        logger.debug("Master: %s:%s (address %s)", master_ip, port, master_address)
        logger.debug("Slave: %s:%s (address %s)", slave_ip, port, slave_address)

        # Read master inverter run mode
        master_mode = _read_single_run_mode(
            master_ip, port, connection_timeout, master_address, min_interval
        )

        if not master_mode:
            logger.error("Failed to read master inverter run mode")
            return None

        logger.debug("Master run mode: %s", master_mode)

        # Wait before connecting to second inverter
        time.sleep(min_interval)

        # Read slave inverter run mode
        slave_mode = _read_single_run_mode(
            slave_ip, port, connection_timeout, slave_address, min_interval
        )

        if not slave_mode:
            logger.error("Failed to read slave inverter run mode")
            return None

        logger.debug("Slave run mode: %s", slave_mode)

        # Return successful results
        result = {"master": master_mode, "slave": slave_mode}

        logger.info("Successfully read run mode status from both inverters")

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Unexpected error in solax_modbus_run_mode")
        return None
    return result


def solax_modbus_battery_power(  # pylint: disable=too-many-return-statements
    config: dict[str, Any],
) -> dict[str, dict[str, Any]] | None:
    """Read battery power and mode from both inverters via Modbus TCP.

    Guard clause pattern: early returns for independent validation checks.
    Alternative nested if/else would reduce readability and violate Clean Code principles.

    Reads register 0x0016 (Batpower_Charge1) from both master and slave inverters.
    This register provides battery power in watts with sign indicating direction:
    - Positive values = charging (power flowing into battery)
    - Negative values = discharging (power flowing out of battery)

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
                with Modbus TCP settings and IP addresses

    Returns:
        Dictionary with format:
        {
            "master": {
                "power": -1500,  # Battery power in watts (negative = discharging)
                "mode": "Discharging"  # Battery mode text
            },
            "slave": {
                "power": -1400,  # Battery power in watts
                "mode": "Discharging"  # Battery mode text
            }
        }
        Returns None if any error occurs (fail-fast approach)

    Example:
        config = load_static_config("config.yaml")
        battery_data = solax_modbus_battery_power(config)
        if battery_data:
            for inverter, data in battery_data.items():
                print(f"{inverter}: {data['power']}W ({data['mode']})")

    """
    try:
        # Validate configuration
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            logger.error("Modbus TCP is not enabled in configuration")
            return None

        # Extract configuration parameters
        master_ip = solax_config.get("master_ip")
        slave_ip = solax_config.get("slave_ip")
        port = solax_config.get("modbus_port", 502)
        connection_timeout = solax_config.get("modbus_connection_timeout", 10)
        master_address = solax_config.get("master_modbus_address", 1)
        slave_address = solax_config.get("slave_modbus_address", 2)
        min_interval = solax_config.get("min_command_interval", 1.0)

        # Validate required parameters
        if not master_ip or master_ip == "YOUR_IP_HERE":
            logger.error("Master inverter IP address not configured")
            return None

        if not slave_ip or slave_ip == "YOUR_IP_HERE":
            logger.error("Slave inverter IP address not configured")
            return None

        logger.debug("Master: %s:%s (address %s)", master_ip, port, master_address)
        logger.debug("Slave: %s:%s (address %s)", slave_ip, port, slave_address)

        # Read master inverter battery power
        master_data = _read_single_battery_power(
            master_ip, port, connection_timeout, master_address, min_interval
        )

        if not master_data:
            logger.error("Failed to read master inverter battery power")
            return None

        logger.debug("Master battery: %sW (%s)", master_data["power"], master_data["mode"])

        # Wait before connecting to second inverter
        time.sleep(min_interval)

        # Read slave inverter battery power
        slave_data = _read_single_battery_power(
            slave_ip, port, connection_timeout, slave_address, min_interval
        )

        if not slave_data:
            logger.error("Failed to read slave inverter battery power")
            return None

        logger.debug("Slave battery: %sW (%s)", slave_data["power"], slave_data["mode"])

        # Return successful results
        result = {"master": master_data, "slave": slave_data}

        logger.info("Successfully read battery power from both inverters")

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Unexpected error in solax_modbus_battery_power")
        return None
    return result


def solax_modbus_grid_power(config: dict[str, Any]) -> dict[str, int] | None:
    """Read grid power from both inverters via Modbus TCP.

    REGISTER 0x0046 VALIDATED FUNCTIONALITY:
    =======================================
    Reads register 0x0046 (Grid Power) from both master and slave inverters
    using Function Code 0x04 (Input Registers). Through extensive testing
    across different inverter modes, this register provides ACCURATE and
    RELIABLE instantaneous grid import/export power measurements:

    - SELF_USE mode: 0W (legitimate - batteries maintain perfect grid balance)
    - FORCE_DISCHARGE mode: +11,694W (grid export - positive values)
    - FORCE_CHARGE mode: -12,501W (grid import - negative values)

    Sign Convention: Positive = exporting to grid, Negative = importing from grid

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
                with Modbus TCP settings and IP addresses

    Returns:
        Dictionary with format:
        {
            "master": 11771,  # Grid power in watts (positive = exporting)
            "slave": 0        # Grid power in watts
        }
        Returns None if any error occurs (fail-fast approach)

    Example:
        config = load_static_config("config.yaml")
        grid_power = solax_modbus_grid_power(config)
        if grid_power:
            total_export = sum(p for p in grid_power.values() if p > 0)
            print(f"Total export to grid: {total_export}W")

    """
    try:
        # Validate configuration
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            logger.error("Modbus TCP is not enabled in configuration")
            return None

        # Extract configuration parameters
        master_ip = solax_config.get("master_ip")
        slave_ip = solax_config.get("slave_ip")
        port = solax_config.get("modbus_port", 502)
        connection_timeout = solax_config.get("modbus_connection_timeout", 10)
        master_address = solax_config.get("master_modbus_address", 1)
        slave_address = solax_config.get("slave_modbus_address", 2)
        min_interval = solax_config.get("min_command_interval", 1.0)

        # Validate required parameters
        if not master_ip or master_ip == "YOUR_IP_HERE":
            logger.error("Master inverter IP address not configured")
            return None

        if not slave_ip or slave_ip == "YOUR_IP_HERE":
            logger.error("Slave inverter IP address not configured")
            return None

        logger.debug("Master: %s:%s (address %s)", master_ip, port, master_address)
        logger.debug("Slave: %s:%s (address %s)", slave_ip, port, slave_address)

        # Read master inverter grid power
        # Note: Only master inverter provides grid power data (register 0x0046)
        master_power = _read_single_grid_power(
            master_ip, port, connection_timeout, master_address, min_interval
        )

        if master_power is None:
            logger.error("Failed to read master inverter grid power")
            return None

        logger.debug("Master grid power: %sW", master_power)

        # Slave inverter does not provide grid power data (verified 2025-06-28)
        # In dual-inverter configurations, only master shows grid connection
        logger.debug("Slave inverter: Grid power not available (master-only data)")

        # Return successful results
        result = {
            "master": master_power,
            "slave": None,  # Slave does not provide grid power data
        }

        logger.info("Successfully read grid power from both inverters")

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Unexpected error in solax_modbus_grid_power")
        return None
    return result


def solax_modbus_soc(  # pylint: disable=too-many-return-statements
    config: dict[str, Any],
) -> dict[str, int] | None:
    """Read battery State of Charge (SoC) from both inverters via Modbus TCP.

    Guard clause pattern: early returns for independent validation checks.
    Alternative nested if/else would reduce readability and violate Clean Code principles.

    Reads register 0x001C (Battery SoC) from both master and slave inverters
    using Function Code 0x04 (Input Registers). This register provides
    current battery charge percentage.

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
                with Modbus TCP settings and IP addresses

    Returns:
        Dictionary with format:
        {
            "master": 71,  # SoC percentage (0-100)
            "slave": 72    # SoC percentage (0-100)
        }
        Returns None if any error occurs (fail-fast approach)

    Example:
        config = load_static_config("config.yaml")
        soc_data = solax_modbus_soc(config)
        if soc_data:
            avg_soc = (soc_data['master'] + soc_data['slave']) / 2
            print(f"Average SoC: {avg_soc:.1f}%")

    """
    try:
        # Validate configuration
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            logger.error("Modbus TCP is not enabled in configuration")
            return None

        # Extract configuration parameters
        master_ip = solax_config.get("master_ip")
        slave_ip = solax_config.get("slave_ip")
        port = solax_config.get("modbus_port", 502)
        connection_timeout = solax_config.get("modbus_connection_timeout", 10)
        master_address = solax_config.get("master_modbus_address", 1)
        slave_address = solax_config.get("slave_modbus_address", 2)
        min_interval = solax_config.get("min_command_interval", 1.0)

        # Validate required parameters
        if not master_ip or master_ip == "YOUR_IP_HERE":
            logger.error("Master inverter IP address not configured")
            return None

        if not slave_ip or slave_ip == "YOUR_IP_HERE":
            logger.error("Slave inverter IP address not configured")
            return None

        logger.debug("Master: %s:%s (address %s)", master_ip, port, master_address)
        logger.debug("Slave: %s:%s (address %s)", slave_ip, port, slave_address)

        # Read master inverter SoC
        master_soc = _read_single_soc(
            master_ip, port, connection_timeout, master_address, min_interval
        )

        if master_soc is None:
            logger.error("Failed to read master inverter SoC")
            return None

        logger.debug("Master SoC: %s%%", master_soc)

        # Wait before connecting to second inverter
        time.sleep(min_interval)

        # Read slave inverter SoC
        slave_soc = _read_single_soc(
            slave_ip, port, connection_timeout, slave_address, min_interval
        )

        if slave_soc is None:
            logger.error("Failed to read slave inverter SoC")
            return None

        logger.debug("Slave SoC: %s%%", slave_soc)

        # Return successful results
        result = {"master": master_soc, "slave": slave_soc}

        logger.info("Successfully read SoC from both inverters")

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Unexpected error in solax_modbus_soc")
        return None
    return result


def solax_modbus_pv_power(  # pylint: disable=too-many-return-statements
    config: dict[str, Any],
) -> dict[str, dict[str, int]] | None:
    """Read PV power generation from both inverters via Modbus TCP.

    Guard clause pattern: early returns for independent validation checks.
    Alternative nested if/else would reduce readability and violate Clean Code principles.

    Reads registers 0x000A (PV String 1) and 0x000B (PV String 2) from both
    master and slave inverters using Function Code 0x04 (Input Registers).
    This provides current solar generation data for all four PV strings.

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
                with Modbus TCP settings and IP addresses

    Returns:
        Dictionary with format:
        {
            "master": {
                "pv1": 2,  # PV String 1 power in watts
                "pv2": 0   # PV String 2 power in watts
            },
            "slave": {
                "pv1": 0,  # PV String 1 power in watts
                "pv2": 0   # PV String 2 power in watts
            }
        }
        Returns None if any error occurs (fail-fast approach)

    Example:
        config = load_static_config("config.yaml")
        pv_data = solax_modbus_pv_power(config)
        if pv_data:
            total_pv = sum(pv_data['master'].values()) + sum(pv_data['slave'].values())
            print(f"Total PV Generation: {total_pv}W")

    """
    try:
        # Validate configuration
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            logger.error("Modbus TCP is not enabled in configuration")
            return None

        # Extract configuration parameters
        master_ip = solax_config.get("master_ip")
        slave_ip = solax_config.get("slave_ip")
        port = solax_config.get("modbus_port", 502)
        connection_timeout = solax_config.get("modbus_connection_timeout", 10)
        master_address = solax_config.get("master_modbus_address", 1)
        slave_address = solax_config.get("slave_modbus_address", 2)
        min_interval = solax_config.get("min_command_interval", 1.0)

        # Validate required parameters
        if not master_ip or master_ip == "YOUR_IP_HERE":
            logger.error("Master inverter IP address not configured")
            return None

        if not slave_ip or slave_ip == "YOUR_IP_HERE":
            logger.error("Slave inverter IP address not configured")
            return None

        logger.debug("Master: %s:%s (address %s)", master_ip, port, master_address)
        logger.debug("Slave: %s:%s (address %s)", slave_ip, port, slave_address)

        # Read master inverter PV power
        master_pv = _read_single_pv_power(
            master_ip, port, connection_timeout, master_address, min_interval
        )

        if not master_pv:
            logger.error("Failed to read master inverter PV power")
            return None

        logger.debug("Master PV: PV1=%sW, PV2=%sW", master_pv["pv1"], master_pv["pv2"])

        # Wait before connecting to second inverter
        time.sleep(min_interval)

        # Read slave inverter PV power
        slave_pv = _read_single_pv_power(
            slave_ip, port, connection_timeout, slave_address, min_interval
        )

        if not slave_pv:
            logger.error("Failed to read slave inverter PV power")
            return None

        logger.debug("Slave PV: PV1=%sW, PV2=%sW", slave_pv["pv1"], slave_pv["pv2"])

        # Return successful results
        result = {"master": master_pv, "slave": slave_pv}

        logger.info("Successfully read PV power from both inverters")

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Unexpected error in solax_modbus_pv_power")
        return None
    return result


def solax_modbus_daily_yield(  # pylint: disable=too-many-return-statements
    config: dict[str, Any],
) -> dict[str, float] | None:
    """Read daily PV yield from both inverters via Modbus TCP.

    Guard clause pattern: early returns for independent validation checks.
    Alternative nested if/else would reduce readability and violate Clean Code principles.

    Reads register 0x0050 (Daily PV Yield) from both master and slave inverters
    using Function Code 0x04 (Input Registers). This register provides
    accumulated PV generation since midnight in 0.1kWh units.

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
                with Modbus TCP settings and IP addresses

    Returns:
        Dictionary with format:
        {
            "master": 25.5,  # Daily yield in kWh
            "slave": 26.1    # Daily yield in kWh
        }
        Returns None if any error occurs (fail-fast approach)

    Example:
        config = load_static_config("config.yaml")
        yield_data = solax_modbus_daily_yield(config)
        if yield_data:
            total_yield = yield_data['master'] + yield_data['slave']
            print(f"Total Daily Yield: {total_yield:.2f} kWh")

    """
    try:
        # Validate configuration
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            logger.error("Modbus TCP is not enabled in configuration")
            return None

        # Extract configuration parameters
        master_ip = solax_config.get("master_ip")
        slave_ip = solax_config.get("slave_ip")
        port = solax_config.get("modbus_port", 502)
        connection_timeout = solax_config.get("modbus_connection_timeout", 10)
        master_address = solax_config.get("master_modbus_address", 1)
        slave_address = solax_config.get("slave_modbus_address", 2)
        min_interval = solax_config.get("min_command_interval", 1.0)

        # Validate required parameters
        if not master_ip or master_ip == "YOUR_IP_HERE":
            logger.error("Master inverter IP address not configured")
            return None

        if not slave_ip or slave_ip == "YOUR_IP_HERE":
            logger.error("Slave inverter IP address not configured")
            return None

        logger.debug("Master: %s:%s (address %s)", master_ip, port, master_address)
        logger.debug("Slave: %s:%s (address %s)", slave_ip, port, slave_address)

        # Read master inverter daily yield
        master_yield = _read_single_daily_yield(
            master_ip, port, connection_timeout, master_address, min_interval
        )

        if master_yield is None:
            logger.error("Failed to read master inverter daily yield")
            return None

        logger.debug("Master daily yield: %.2f kWh", master_yield)

        # Wait before connecting to second inverter
        time.sleep(min_interval)

        # Read slave inverter daily yield
        slave_yield = _read_single_daily_yield(
            slave_ip, port, connection_timeout, slave_address, min_interval
        )

        if slave_yield is None:
            logger.error("Failed to read slave inverter daily yield")
            return None

        logger.debug("Slave daily yield: %.2f kWh", slave_yield)

        # Return successful results
        result = {"master": master_yield, "slave": slave_yield}

        logger.info("Successfully read daily yield from both inverters")

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Unexpected error in solax_modbus_daily_yield")
        return None
    return result


def solax_modbus_battery_capacity(  # pylint: disable=too-many-return-statements
    config: dict[str, Any],
) -> dict[str, float] | None:
    """Read battery capacity from both inverters via Modbus TCP.

    Guard clause pattern: early returns for independent validation checks.
    Alternative nested if/else would reduce readability and violate Clean Code principles.

    Reads registers 0x0026-0x0027 (Battery Capacity) from both master and slave
    inverters using Function Code 0x04 (Input Registers). These registers provide
    total battery capacity as a uint32 pair (LSB + MSB) in Wh units.

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
                with Modbus TCP settings and IP addresses

    Returns:
        Dictionary with format:
        {
            "master": 11.52,  # Battery capacity in kWh
            "slave": 11.52    # Battery capacity in kWh
        }
        Returns None if any error occurs (fail-fast approach)

    Example:
        config = load_static_config("config.yaml")
        capacity_data = solax_modbus_battery_capacity(config)
        if capacity_data:
            total_capacity = capacity_data['master'] + capacity_data['slave']
            print(f"Total System Capacity: {total_capacity:.2f} kWh")

    """
    try:
        # Validate configuration
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            logger.error("Modbus TCP is not enabled in configuration")
            return None

        # Extract configuration parameters
        master_ip = solax_config.get("master_ip")
        slave_ip = solax_config.get("slave_ip")
        port = solax_config.get("modbus_port", 502)
        connection_timeout = solax_config.get("modbus_connection_timeout", 10)
        master_address = solax_config.get("master_modbus_address", 1)
        slave_address = solax_config.get("slave_modbus_address", 2)
        min_interval = solax_config.get("min_command_interval", 1.0)

        # Validate required parameters
        if not master_ip or master_ip == "YOUR_IP_HERE":
            logger.error("Master inverter IP address not configured")
            return None

        if not slave_ip or slave_ip == "YOUR_IP_HERE":
            logger.error("Slave inverter IP address not configured")
            return None

        logger.debug("Master: %s:%s (address %s)", master_ip, port, master_address)
        logger.debug("Slave: %s:%s (address %s)", slave_ip, port, slave_address)

        # Read master inverter battery capacity
        master_capacity = _read_single_battery_capacity(
            master_ip, port, connection_timeout, master_address, min_interval
        )

        if master_capacity is None:
            logger.error("Failed to read master inverter battery capacity")
            return None

        logger.debug("Master battery capacity: %.2f kWh", master_capacity)

        # Wait before connecting to second inverter
        time.sleep(min_interval)

        # Read slave inverter battery capacity
        slave_capacity = _read_single_battery_capacity(
            slave_ip, port, connection_timeout, slave_address, min_interval
        )

        if slave_capacity is None:
            logger.error("Failed to read slave inverter battery capacity")
            return None

        logger.debug("Slave battery capacity: %.2f kWh", slave_capacity)

        # Return successful results
        result = {"master": master_capacity, "slave": slave_capacity}

        logger.info("Successfully read battery capacity from both inverters")

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Unexpected error in solax_modbus_battery_capacity")
        return None
    return result


def solax_modbus_grid_totals(  # pylint: disable=too-many-return-statements
    config: dict[str, Any],
) -> dict[str, dict[str, float]] | None:
    """Read grid import/export cumulative totals from both inverters via Modbus TCP.

    Guard clause pattern: early returns for independent validation checks.
    Alternative nested if/else would reduce readability and violate Clean Code principles.

    Reads registers 0x0048-0x0049 (feedin_energy_total) and 0x004A-0x004B
    (consum_energy_total) from both master and slave inverters using Function Code 0x04
    (Input Registers). These registers provide cumulative energy totals since inverter
    installation in 0.01 kWh units.

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
                with Modbus TCP settings and IP addresses

    Returns:
        Dictionary with format:
        {
            "master": {
                "import_kwh": 12345.67,  # Grid import total in kWh
                "export_kwh": 23456.78   # Grid export total in kWh
            },
            "slave": {
                "import_kwh": 12456.89,
                "export_kwh": 23567.90
            }
        }
        Returns None if any error occurs (fail-fast approach)

    Example:
        config = load_static_config("config.yaml")
        grid_totals = solax_modbus_grid_totals(config)
        if grid_totals:
            total_import = grid_totals['master']['import_kwh'] + grid_totals['slave']['import_kwh']
            total_export = grid_totals['master']['export_kwh'] + grid_totals['slave']['export_kwh']
            print(f"Total Grid Import: {total_import:.2f} kWh")
            print(f"Total Grid Export: {total_export:.2f} kWh")

    """
    try:
        # Validate configuration
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            logger.error("Modbus TCP is not enabled in configuration")
            return None

        # Extract configuration parameters
        master_ip = solax_config.get("master_ip")
        slave_ip = solax_config.get("slave_ip")
        port = solax_config.get("modbus_port", 502)
        connection_timeout = solax_config.get("modbus_connection_timeout", 10)
        master_address = solax_config.get("master_modbus_address", 1)
        slave_address = solax_config.get("slave_modbus_address", 2)
        min_interval = solax_config.get("min_command_interval", 1.0)

        # Validate required parameters
        if not master_ip or master_ip == "YOUR_IP_HERE":
            logger.error("Master inverter IP address not configured")
            return None

        if not slave_ip or slave_ip == "YOUR_IP_HERE":
            logger.error("Slave inverter IP address not configured")
            return None

        logger.debug("Master: %s:%s (address %s)", master_ip, port, master_address)
        logger.debug("Slave: %s:%s (address %s)", slave_ip, port, slave_address)

        # Read master inverter grid totals
        master_export = _read_single_grid_export_total(
            master_ip, port, connection_timeout, master_address, min_interval
        )

        if master_export is None:
            logger.error("Failed to read master inverter grid export total")
            return None

        logger.debug("Master grid export: %.2f kWh", master_export)

        # Wait before next read
        time.sleep(min_interval)

        master_import = _read_single_grid_import_total(
            master_ip, port, connection_timeout, master_address, min_interval
        )

        if master_import is None:
            logger.error("Failed to read master inverter grid import total")
            return None

        logger.debug("Master grid import: %.2f kWh", master_import)

        # Wait before connecting to second inverter
        time.sleep(min_interval)

        # Read slave inverter grid totals
        slave_export = _read_single_grid_export_total(
            slave_ip, port, connection_timeout, slave_address, min_interval
        )

        if slave_export is None:
            logger.error("Failed to read slave inverter grid export total")
            return None

        logger.debug("Slave grid export: %.2f kWh", slave_export)

        # Wait before next read
        time.sleep(min_interval)

        slave_import = _read_single_grid_import_total(
            slave_ip, port, connection_timeout, slave_address, min_interval
        )

        if slave_import is None:
            logger.error("Failed to read slave inverter grid import total")
            return None

        logger.debug("Slave grid import: %.2f kWh", slave_import)

        # Return successful results
        result = {
            "master": {"import_kwh": master_import, "export_kwh": master_export},
            "slave": {"import_kwh": slave_import, "export_kwh": slave_export},
        }

        logger.info("Successfully read grid totals from both inverters")

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Unexpected error in solax_modbus_grid_totals")
        return None
    return result


def solax_modbus_work_mode(config: dict[str, Any]) -> BatteryMode | None:
    """Read inverter work mode and manual mode from master inverter only via Modbus TCP.

    Reads registers 0x008B (Inverter Work Mode) and 0x008C (Manual Mode) from the
    master inverter only using Function Code 0x03 (Holding Registers). The master
    controls the entire dual-inverter system.

    Work Mode Combinations:
    - Self-Use: Any work mode other than 3 (Manual)
    - Charging: Work Mode 3 (Manual) + Manual Mode 1 (Force Charge)
    - Discharging: Work Mode 3 (Manual) + Manual Mode 2 (Force Discharge)
    - Stop Manual: Work Mode 3 (Manual) + Manual Mode 0 (Stop charge&discharge)

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
                with Modbus TCP settings and IP addresses

    Returns:
        BatteryMode enum representing system mode
        Returns None if any error occurs (fail-fast approach)

    Example:
        config = load_static_config("config.yaml")
        system_mode = solax_modbus_work_mode(config)
        if system_mode:
            logger.info(f"Current System Mode: {system_mode.value}")

    """
    try:
        # Validate configuration
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            logger.error("Modbus TCP is not enabled in configuration")
            return None

        # Extract configuration parameters
        master_ip = solax_config.get("master_ip")
        port = solax_config.get("modbus_port", 502)
        connection_timeout = solax_config.get("modbus_connection_timeout", 10)
        master_address = solax_config.get("master_modbus_address", 1)
        min_interval = solax_config.get("min_command_interval", 1.0)

        # Validate required parameters
        if not master_ip or master_ip == "YOUR_IP_HERE":
            logger.error("Master inverter IP address not configured")
            return None

        logger.debug("Master: %s:%s (Modbus address %s)", master_ip, port, master_address)
        logger.info(
            "NOTE: Reading work mode ONLY from master - slave is not accessed for system work mode"
        )

        # Read master inverter work mode (master controls entire system)
        system_mode = _read_master_work_mode(
            master_ip, port, connection_timeout, master_address, min_interval
        )

        if not system_mode:
            logger.error("Failed to read master inverter work mode")
            return None

        logger.debug("System Mode: %s", system_mode)

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Unexpected error in solax_modbus_work_mode")
        return None
    return system_mode


def _read_master_work_mode(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> BatteryMode | None:
    """Read work mode and manual mode from master inverter to determine system mode.

    ENHANCED RELIABILITY: Uses the retry-enabled _read_bulk_work_mode_data() function
    from _modbus_reader.py for improved reliability and DRY compliance.

    Args:
        ip: IP address of the master inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the master inverter
        min_interval: Minimum interval between commands

    Returns:
        BatteryMode enum or None if error occurs

    """
    try:
        # Use the retry-enabled bulk work mode reader from _modbus_reader.py
        work_mode_data = _reader_read_bulk_work_mode_data(
            ip, port, timeout, slave_address, min_interval
        )
        if not work_mode_data:
            logger.error("Failed to read work mode data using bulk reader")
            return None

        # Extract BatteryMode enum from the bulk data response
        work_mode_enum = work_mode_data.get("work_mode")
        if work_mode_enum is None:
            logger.error("Work mode enum not found in bulk data response")
            return None

        logger.debug("Successfully read work mode via bulk reader: %s", work_mode_enum)

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Error reading work mode from %s", ip)
        return None
    return work_mode_enum


# ================================================================================================
# WORK MODE WRITE OPERATIONS (MASTER INVERTER ONLY)
# ================================================================================================


# =============================================================================
# BULK DATA OPTIMIZATION API
# =============================================================================


def solax_modbus_bulk_data(config: dict[str, Any]) -> dict[str, Any] | None:
    """Read comprehensive SolaX system data using optimized block reads.

    ============================================================================
    PERFORMANCE OPTIMIZATION - DUAL API APPROACH
    ============================================================================

    This function represents a major performance optimization for applications
    that need multiple data points from the SolaX system. Instead of making
    12 separate modbus function calls, this single function retrieves all
    system data using only 2 modbus operations.

    PERFORMANCE COMPARISON:

    Individual Functions Approach (existing):
    - solax_modbus_ac_power(): ~2 seconds (2 operations)
    - solax_modbus_battery_power(): ~2 seconds (2 operations)
    - solax_modbus_pv_power(): ~2 seconds (2 operations)
    - solax_modbus_soc(): ~2 seconds (2 operations)
    - solax_modbus_grid_power(): ~1 second (1 operation)
    - solax_modbus_daily_yield(): ~2 seconds (2 operations)
    - solax_modbus_work_mode(): ~1 second (1 operation)
    - Total: ~12 seconds, 12 modbus operations

    Bulk Data Approach (this function):
    - Parallel bulk input reads: ~1.2 seconds (2 operations total)
    - Work mode read: included in above
    - Serial numbers: included in above
    - Total: ~1.2 seconds, 2 modbus operations
    - Improvement: 90% faster (10x speedup)

    ============================================================================
    WHEN TO USE WHICH API APPROACH
    ============================================================================

    Use solax_modbus_bulk_data() when:
    ✅ You need 2 or more data points
    ✅ Building dashboards or status displays
    ✅ Web interfaces requiring multiple values
    ✅ Performance is important
    ✅ Regular polling/monitoring applications

    Use individual solax_modbus_*() functions when:
    ✅ You need only 1 specific data point
    ✅ Building focused monitoring scripts
    ✅ Integrating with existing code that works well
    ✅ Learning/experimenting with specific values

    ============================================================================
    IMPLEMENTATION DETAILS
    ============================================================================

    This function uses aggressive block read optimization:

    1. Input Register Optimization:
       - Reads 0x0002-0x0050 (79 registers) in 1 operation
       - Extracts only the 10 registers we actually need
       - Applies same processing as individual functions
       - Includes register 0x0046 (grid power) with validated accuracy

    2. Work Mode Optimization:
       - Reads 0x008B-0x008C (2 registers) in 1 operation
       - Instead of 2 separate register reads

    3. Parallel Execution:
       - Master and slave inverters read simultaneously
       - Reduces time from sum to maximum of both reads

    4. Existing Optimizations:
       - Serial numbers already use block read (0x0000-0x0006)
       - RTC timestamps already use block read (0x0085-0x008A)
       - Battery capacity already uses block read (0x0026-0x0027)

    ============================================================================

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
                with Modbus TCP settings and IP addresses

    Returns:
        Dictionary containing all system data with structure matching
        the combined output of individual solax_modbus_* functions:

        {
            "serial_numbers": {
                "master": "H4602AJ8530050",
                "slave": "H4602AL3067054"
            },
            "ac_power": {
                "master": 1150,  # Watts
                "slave": 1050    # Watts
            },
            "battery_power": {
                "master": {"power": -500, "mode": "Discharging"},
                "slave": {"power": -480, "mode": "Discharging"}
            },
            "pv_power": {
                "master": {"pv1": 800, "pv2": 850},
                "slave": {"pv1": 750, "pv2": 800}
            },
            "soc": {
                "master": 85,    # Percentage
                "slave": 83      # Percentage
            },
            "battery_temperature": {
                "master": 25,    # Degrees Celsius
                "slave": 24      # Degrees Celsius
            },
            "grid_power": {
                "master": 250,   # Watts (positive = export)
                "slave": None    # Only master has grid connection
            },
            "grid_totals": {
                "master": {
                    "import_kwh": 12345.67,  # Cumulative grid import (kWh)
                    "export_kwh": 23456.78   # Cumulative grid export (kWh)
                },
                "slave": {
                    "import_kwh": 11234.56,  # Cumulative grid import (kWh)
                    "export_kwh": 22345.67   # Cumulative grid export (kWh)
                }
            },
            "daily_yield": {
                "master": 15.5,  # kWh
                "slave": 14.8    # kWh
            },
            "battery_capacity": {
                "master": 11.52, # kWh
                "slave": 11.52   # kWh
            },
            "run_mode": {
                "master": "Normal Mode",
                "slave": "Normal Mode"
            },
            "rtc_timestamps": {
                "master": "2025-07-19 14:30:15",
                "slave": "2025-07-19 14:30:13"
            },
            "work_mode": "Self-Use"  # System-wide setting
        }

        Returns None if any critical error occurs (fail-fast approach)

    Example Usage:
        # Efficient approach - get all data in one call
        all_data = solax_modbus_bulk_data(config)
        if all_data:
            total_ac = all_data["ac_power"]["master"] + all_data["ac_power"]["slave"]
            avg_soc = (all_data["soc"]["master"] + all_data["soc"]["slave"]) / 2
            system_mode = all_data["work_mode"]

        # Compare with individual approach (much slower):
        # ac_power = solax_modbus_ac_power(config)          # 2 seconds
        # soc = solax_modbus_soc(config)                    # 2 seconds
        # work_mode = solax_modbus_work_mode(config)        # 1 second
        # Total: 5 seconds vs 1.2 seconds with bulk read

    Hardware Safety:
        ✅ Read-only operations - no hardware risk
        ✅ Respects 1-second min_command_interval between operations
        ✅ Uses existing proven modbus client infrastructure
        ✅ Comprehensive error handling and timeouts
        ✅ Fail-fast approach prevents partial/corrupted data

    """
    try:
        # Validate configuration
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            logger.error("Modbus TCP is not enabled in configuration")
            return None

        logger.info("Starting optimized bulk data collection")

        # Execute parallel bulk read from both inverters
        bulk_data = _parallel_bulk_read_both_inverters(config)

        if not bulk_data:
            logger.error("Failed to read bulk data from inverters")
            return None

        # Extract component data
        master_data = bulk_data["master_data"]
        slave_data = bulk_data["slave_data"]
        work_mode_data = bulk_data["work_mode_data"]
        serial_numbers = bulk_data["serial_numbers"]
        timestamps = bulk_data["timestamps"]

        # Construct result in same format as individual functions combined
        result = {
            "serial_numbers": serial_numbers,
            "ac_power": {
                "master": master_data.get("ac_power", 0),
                "slave": slave_data.get("ac_power", 0),
            },
            "battery_power": {
                "master": {
                    "power": master_data.get("battery_power", 0),
                    "mode": master_data.get("battery_mode", "Unknown"),
                },
                "slave": {
                    "power": slave_data.get("battery_power", 0),
                    "mode": slave_data.get("battery_mode", "Unknown"),
                },
            },
            "pv_power": {
                "master": {
                    "pv1": master_data.get("pv1_power", 0),
                    "pv2": master_data.get("pv2_power", 0),
                },
                "slave": {
                    "pv1": slave_data.get("pv1_power", 0),
                    "pv2": slave_data.get("pv2_power", 0),
                },
            },
            "soc": {
                "master": master_data.get("battery_soc", 0),
                "slave": slave_data.get("battery_soc", 0),
            },
            "battery_temperature": {
                "master": master_data.get("battery_temperature_celsius", None),
                "slave": slave_data.get("battery_temperature_celsius", None),
            },
            "grid_power": {
                "master": master_data.get("grid_power_watts", 0),
                "slave": None,  # Only master has grid connection
            },
            "grid_totals": {
                "master": {
                    "import_kwh": master_data.get("grid_import_total_kwh", 0),
                    "export_kwh": master_data.get("grid_export_total_kwh", 0),
                },
                "slave": {
                    "import_kwh": slave_data.get("grid_import_total_kwh", 0),
                    "export_kwh": slave_data.get("grid_export_total_kwh", 0),
                },
            },
            "daily_yield": {
                "master": master_data.get("daily_yield_kwh", 0),
                "slave": slave_data.get("daily_yield_kwh", 0),
            },
            "battery_capacity": {
                "master": master_data.get("battery_capacity_kwh", 0),
                "slave": slave_data.get("battery_capacity_kwh", 0),
            },
            "run_mode": {
                "master": master_data.get("run_mode_str", "Unknown"),
                "slave": slave_data.get("run_mode_str", "Unknown"),
            },
            "rtc_timestamps": timestamps,
            "work_mode": work_mode_data.get(
                "work_mode", None
            ),  # BatteryMode enum or None - consistent type
        }

        logger.info("Successfully completed bulk data collection")
        logger.debug("Bulk data result contains %s top-level data categories", len(result))

    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        logger.exception("Unexpected error in solax_modbus_bulk_data")
        return None
    return result


# ============================================================================
# ENHANCED ERROR HANDLING - POWER SPIKE VALIDATION
# ============================================================================
# NOTE: Validation functions moved to _modbus_validator.py module


def solax_modbus_bulk_data_with_validation(  # noqa: C901 - Inherent complexity of 3-phase error recovery strategy
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Enhanced bulk data read with physical validation and smart fallback.

    This function addresses SolaX Modbus "sudden peaks" issue by:
    1. Reading bulk data (fast path - ~1.2 seconds)
    2. Validating all power values against physical limits (26kW)
    3. Falling back to individual reads if validation fails (~12 seconds)
    4. Comprehensive logging for diagnosis

    Args:
        config: Configuration dictionary with Modbus settings

    Returns:
        Dictionary with validated power data or None if all methods fail

    Performance Note:
        - Normal operation: ~1.2 seconds (bulk read with validation)
        - Error recovery: ~13.2 seconds (bulk attempt + individual fallback)
        - Complete failure: Returns None triggering Cloud API fallback

    Error Recovery Strategy:
        Phase 1: Bulk read with validation (primary path - fastest)
        Phase 2: Individual register fallback (proven reliable)
        Phase 3: Return None - triggers Cloud API fallback in aggregator

    Complexity Justification:
        - C901 (11 branches) is inherent to the 3-phase error recovery strategy
        - Each phase (bulk → individual → Cloud API fallback) requires distinct
          error handling and validation checking
        - Sequential fallback logic must stay together for maintainability:
          extracting phases would obscure the critical recovery flow
        - Physical validation adds necessary branching for the known SolaX
          "sudden peaks" hardware issue (false 26kW+ power readings)
        - Comprehensive error/warning logging for each phase is essential for
          diagnosing hardware communication issues in production
        - Already well-structured with clear phase boundaries and comments

    """
    func_logger = logging.getLogger(__name__)

    # Phase 1: Bulk read with validation (primary path - fastest)
    try:
        func_logger.debug("Attempting bulk data read with validation")
        bulk_data = solax_modbus_bulk_data(config)

        if bulk_data:
            validation_result = validate_power_data_physical_limits(bulk_data)
            if validation_result.physically_possible:
                func_logger.debug("Bulk data passed physical validation")
                return bulk_data
            func_logger.warning(
                "Bulk data FAILED physical validation: %s", validation_result.errors
            )
            for error in validation_result.errors:
                func_logger.error("🚨 POWER SPIKE DETECTED: %s", error)
            # Log warnings too for comprehensive diagnostics
            for warning in validation_result.warnings:
                func_logger.warning("⚠️ HIGH POWER WARNING: %s", warning)
        else:
            func_logger.warning("Bulk data read returned no data")
    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        func_logger.exception("Bulk data read failed")

    # Phase 2: Individual register fallback (proven reliable)
    func_logger.warning(
        "Falling back to individual register reads due to bulk data validation failure"
    )
    try:
        individual_data = read_individual_registers_comprehensive(config)
        if individual_data:
            validation_result = validate_power_data_physical_limits(individual_data)
            if validation_result.physically_possible:
                func_logger.info("Individual register reads passed validation - using as fallback")
                # Log any warnings from individual fallback too
                for warning in validation_result.warnings:
                    func_logger.warning("⚠️ INDIVIDUAL FALLBACK WARNING: %s", warning)
                return individual_data
            func_logger.error(
                "Individual register reads ALSO failed validation: %s", validation_result.errors
            )
            for error in validation_result.errors:
                func_logger.error("🚨 INDIVIDUAL FALLBACK POWER SPIKE: %s", error)
        else:
            func_logger.error("Individual register fallback returned no data")
    except Exception:  # pylint: disable=broad-except
        # Circuit Breaker: Hardware interface must not crash daemon.
        # Specific exceptions handled in internal _modbus_* modules.
        func_logger.exception("Individual register fallback failed")

    # Phase 3: Return None - triggers Cloud API fallback in aggregator
    func_logger.error("All Modbus read methods failed validation - will trigger Cloud API fallback")
    return None
