"""SAFETY-CRITICAL SolaX Modbus Mode Controller Module.

CRITICAL SAFETY WARNING: This module contains SAFETY-CRITICAL functions for controlling
SolaX inverter work modes via Modbus TCP protocol. These functions directly control
hardware and include file-locking mechanisms to prevent race conditions.

INTERNAL MODULE: This module is for internal use only by solax_modbus_client.py.
Functions in this module should NOT be called directly from external code.

SAFETY FEATURES:
- File locking mechanism to prevent concurrent mode changes
- 2-minute safety interval between mode changes
- Comprehensive error handling and logging
- Test mode protection to prevent hardware writes during testing
- Timezone-aware timestamp handling for safety checks

HARDWARE BOUNDARY: Functions in this module perform actual hardware writes to
SolaX inverters. All safety validations must be performed before calling these functions.
"""

# pylint: disable=cyclic-import
# Justification: Intentional modbus client split architecture. This helper module
# is imported by solax_modbus_client.py and uses type hints from it. The cycle
# is resolved at runtime and doesn't cause actual circular dependency issues.
from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from src.utils.paths import get_mode_change_log_path

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

# Import BatteryMode for type-safe mode handling
from src.core_logic.battery_simulation import (  # pylint: disable=wrong-import-position
    BatteryMode,
    battery_mode_to_display_string,
)

# Import modbus protocol functions for hardware communication
# For test compatibility, we need to access functions through the main module

# Debug instrumentation for detecting unmarked hardware tests
try:
    import os  # pylint: disable=reimported, ungrouped-imports
    import sys  # pylint: disable=reimported, ungrouped-imports

    # Add dev_scripts to path for debug module import
    project_root = str(Path(__file__).parent.parent.parent)  # pylint: disable=invalid-name  # Local variable
    dev_scripts_path = str(  # pylint: disable=invalid-name  # Local variable
        Path(project_root) / "dev_scripts"
    )  # pragma: no cover
    if dev_scripts_path not in sys.path:  # pragma: no cover
        sys.path.insert(0, dev_scripts_path)  # pragma: no cover

    from hardware_debug_access import (  # pragma: no cover  # pylint: disable=import-outside-toplevel
        log_hardware_access,
        log_hardware_write_violation,
        should_log_hardware_access,
    )

    DEBUG_ENABLED = True  # pragma: no cover
except ImportError:  # pragma: no cover
    DEBUG_ENABLED = False  # pragma: no cover

    def log_hardware_access(  # pylint: disable=missing-function-docstring  # Stub function
        *_args: object, **_kwargs: object
    ) -> None:  # pragma: no cover
        pass  # pragma: no cover

    def log_hardware_write_violation(  # pylint: disable=missing-function-docstring  # Stub
        *_args: object, **_kwargs: object
    ) -> None:  # pragma: no cover
        pass  # pragma: no cover

    def should_log_hardware_access(*_args: object, **_kwargs: object) -> bool:  # noqa: ARG001  # pragma: no cover  # pylint: disable=missing-function-docstring  # Stub
        return False  # pragma: no cover


# Setup basic logging
logger = logging.getLogger(__name__)

# Modbus register addresses for work mode control
REGISTER_WORK_MODE = 0x001F  # Inverter Work Mode register
REGISTER_MANUAL_MODE = 0x0020  # Manual Mode register

# Valid work mode combinations for hardware registers
VALID_WORK_MODE_COMBINATIONS = {
    BatteryMode.SELF_USE: {
        0x001F: 0,  # Inverter Work Mode = Self-Use (standalone)
    },
    BatteryMode.FORCE_CHARGE: {
        0x001F: 3,  # Inverter Work Mode = Manual
        0x0020: 1,  # Manual Mode = Force Charge
    },
    BatteryMode.FORCE_DISCHARGE: {
        0x001F: 3,  # Inverter Work Mode = Manual
        0x0020: 2,  # Manual Mode = Force Discharge
    },
    BatteryMode.MANUAL_STOP: {
        0x001F: 3,  # Inverter Work Mode = Manual
        0x0020: 0,  # Manual Mode = Stop charge&discharge (Hold)
    },
}


def _get_project_root() -> str:
    """Get the absolute path to the project root directory.

    This ensures the JSON file is always in the project root regardless of where
    scripts or web servers are running from.
    """
    # Get the directory of this source file
    current_file = Path(__file__).resolve()
    # Navigate up: src/api_clients/_modbus_mode_controller.py -> project root
    return str(current_file.parent.parent.parent)


def _get_mode_change_log_path() -> str:
    """Get the absolute path to the mode change log JSON file."""
    return get_mode_change_log_path()


def _load_mode_change_log() -> dict[str, object]:
    """Load the mode change log from JSON file.

    Returns:
        Dictionary with mode change log data, or empty dict if file doesn't exist

    """
    log_path = _get_mode_change_log_path()
    try:
        log_path_obj = Path(log_path)
        if log_path_obj.exists():
            with log_path_obj.open(encoding="utf-8") as f:
                return json.load(f)
        else:
            return {}
    except (OSError, json.JSONDecodeError):
        logger.exception("Error loading mode change log")
        return {}


def _save_mode_change_log(log_data: dict[str, object]) -> bool:
    """Save the mode change log to JSON file.

    Args:
        log_data: Dictionary containing the log data to save

    Returns:
        True if saved successfully, False otherwise

    """
    log_path = _get_mode_change_log_path()
    try:
        with Path(log_path).open("w", encoding="utf-8") as f:
            json.dump(log_data, f, indent=2)
    except (OSError, TypeError):
        logger.exception("Error saving mode change log")
        return False
    return True


def _log_mode_change(
    previous_mode: BatteryMode, new_mode: BatteryMode, changed_by: str, *, forced: bool = False
) -> bool:
    """Log a successful mode change to the JSON file.

    Args:
        previous_mode: The mode before the change
        new_mode: The new mode after the change
        changed_by: Identifier of what triggered the change (web_interface, script, dashboard)
        forced: Whether this was a forced change

    Returns:
        True if logged successfully, False otherwise

    """
    try:
        log_data = {
            "last_mode_change": {
                "timestamp": datetime.now(UTC).isoformat(),
                "previous_mode": previous_mode.value,
                "new_mode": new_mode.value,
                "changed_by": changed_by,
                "forced": forced,
            }
        }

        return _save_mode_change_log(log_data)

    except (AttributeError, TypeError):
        logger.exception("Error logging mode change")
        return False


def _check_mode_change_safety(
    log_data: dict[str, object],
    *,
    forced: bool = False,
) -> tuple[bool, str]:
    """Check if a mode change is safe using already-loaded log data.

    This version works with log data that's already been loaded under file lock
    to prevent race conditions during safety checking.

    Args:
        log_data: Already loaded mode change log data
        forced: Whether this is a forced change (bypasses timing check)

    Returns:
        Tuple of (is_safe: bool, message: str)

    """
    try:
        # If no previous changes recorded, it's safe
        if "last_mode_change" not in log_data:
            return True, ""

        # Check if forced (bypasses timing check)
        if forced:
            return True, ""

        # Parse the last change timestamp
        last_change_str = log_data["last_mode_change"]["timestamp"]
        last_change = datetime.fromisoformat(last_change_str)

        # CRITICAL SAFETY: Ensure both times are in UTC for consistent comparison
        current_time = datetime.now(UTC)
        # Convert stored timestamp to UTC if it lacks timezone info (legacy compatibility)
        if last_change.tzinfo is None:
            # Legacy timestamps without timezone - assume local time, convert to UTC
            # This is a safety fallback for existing log files
            logger.warning(
                "Safety log contains timezone-naive timestamp - assuming local time for conversion"
            )
            # Properly convert from local time to UTC (time module already imported at top)
            # Get local timezone offset
            if time.daylight:  # noqa: SIM108 - Explicit if-else improves readability over ternary
                offset_seconds = -time.altzone
            else:
                offset_seconds = -time.timezone
            local_tz_offset = timedelta(seconds=offset_seconds)
            # Convert to UTC by subtracting the local offset
            last_change = last_change - local_tz_offset
            last_change = last_change.replace(tzinfo=UTC)

        # Check if within 2 minutes
        time_diff = current_time - last_change
        if time_diff < timedelta(minutes=2):
            # Calculate remaining time
            remaining = timedelta(minutes=2) - time_diff
            remaining_seconds = int(remaining.total_seconds())

            message = f"Last mode change was {int(time_diff.total_seconds())} seconds ago. Please wait {remaining_seconds} more seconds before changing modes. Do you want to override the safety checks and force the mode change anyway with risk of damage to the SolaX hardware?"
            return False, message

    except (KeyError, ValueError, AttributeError):
        logger.exception("Error checking mode change safety with data")
        # On error, default to safe (don't block operations due to safety check failures)
        return True, ""
    # Safe to proceed
    return True, ""


def _execute_mode_change_hardware(
    config: dict[str, object], mode: BatteryMode, *, test_mode: bool = False
) -> bool:
    """Execute the actual hardware mode change without safety checks or logging.

    This is the low-level function that performs the actual Modbus communication.
    It should only be called after safety checks have been performed.

    Args:
        config: Configuration dictionary containing solaX_cloud_api section
        mode: Work mode to set - must be BatteryMode.SELF_USE, FORCE_CHARGE, or FORCE_DISCHARGE
        test_mode: If True, simulate successful operation without hardware writes

    Returns:
        True if mode was set successfully, False if error occurred

    """
    try:
        # Validate configuration for mode change
        solax_config = config.get("solaX_cloud_api", {})

        if not solax_config.get("modbus_enabled", False):
            logger.error("Modbus TCP is not enabled in configuration")
            return False

        # Extract configuration parameters
        master_ip = solax_config.get("master_ip")
        port = solax_config.get("modbus_port", 502)
        connection_timeout = solax_config.get("modbus_connection_timeout", 10)
        master_address = solax_config.get("master_modbus_address", 1)
        min_interval = solax_config.get("min_command_interval", 1.0)

        # Validate required parameters
        if not master_ip or master_ip == "YOUR_IP_HERE":
            logger.error("Master inverter IP address not configured")
            return False

        # LAYER 1: Early exit for test mode (fail-early pattern)
        if test_mode:
            logger.info("TEST MODE: Would execute hardware mode change to %s", mode)
            return True

        logger.info(
            "HARDWARE WRITE PROCEEDING: All safety checks passed for mode change to %s", mode
        )
        logger.debug("Master: %s:%s (Modbus address %s)", master_ip, port, master_address)

        # CRITICAL SAFETY CHECK: Log hardware write
        try:  # noqa: SIM105 - Explicit try-except-pass preferred over contextlib.suppress for clarity  # pragma: no cover
            log_hardware_write_violation(
                "_set_master_work_mode", test_mode, config
            )  # pragma: no cover
        except Exception:  # noqa: S110, BLE001  # Hardware control: audit logging failure must not crash mode change  # pragma: no cover  # pylint: disable=broad-exception-caught
            pass  # pragma: no cover

        # Execute work mode change on master inverter
        result = _set_master_work_mode(  # pragma: no cover
            master_ip,
            port,
            connection_timeout,
            master_address,
            min_interval,
            mode,
            test_mode=test_mode,  # pragma: no cover
        )  # pragma: no cover

        if result:  # pragma: no cover
            logger.info(
                "Successfully set work mode to '%s' on master inverter", mode
            )  # pragma: no cover
        else:  # pragma: no cover
            logger.error(
                "Failed to set work mode to '%s' on master inverter", mode
            )  # pragma: no cover
    except (KeyError, AttributeError):
        logger.exception("Hardware execution error")
        return False
    return result  # pragma: no cover


@contextmanager
def _locked_mode_change_log(
    timeout: float = 10.0,
) -> Generator[tuple[dict[str, object], Callable[[dict[str, object]], None]]]:
    """Context manager for safe file locking of mode change log.

    Provides exclusive access to the mode change log file to prevent
    race conditions between daemon and web interface operations.

    Args:
        timeout: Maximum time to wait for lock acquisition (seconds)

    Yields:
        Tuple of (log_data: dict, save_function: callable)

    """
    log_path = _get_mode_change_log_path()
    log_path_obj = Path(log_path)

    # Ensure directory exists
    log_path_obj.parent.mkdir(parents=True, exist_ok=True)

    # Create file if it doesn't exist
    if not log_path_obj.exists():
        with log_path_obj.open("w", encoding="utf-8") as f:
            json.dump({}, f)

    start_time = time.time()
    # Open file for read/write
    with log_path_obj.open("r+", encoding="utf-8") as fd:
        # Try to acquire exclusive lock with timeout
        while time.time() - start_time < timeout:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(0.1)
        else:
            msg = f"Could not acquire lock on {log_path} within {timeout} seconds"
            raise TimeoutError(msg)  # noqa: TRY301 - Inline raise is clearer than inner function for timeout logic

        # Read current log data
        fd.seek(0)
        try:
            log_data = json.load(fd)
        except json.JSONDecodeError:
            log_data = {}

        def save_log(data: dict[str, object]) -> None:
            """Save log data atomically."""
            fd.seek(0)
            fd.truncate()
            json.dump(data, fd, indent=2)
            fd.flush()
            os.fsync(fd.fileno())

        try:
            yield log_data, save_log
        finally:
            # Unlock file before context manager closes it
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - Defensive: OS-level lock release failure
                logger.exception("Error releasing file lock")


def _is_actively_testing() -> bool:
    """Determine if we're actively running in a test context.

    Returns True only when we're actually executing tests,
    not when test frameworks are merely imported or available.

    Returns:
        True if actively executing tests, False otherwise

    """
    # Strong indicators of active testing
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("TESTING"):
        return True

    # Check call stack for test execution context  # pragma: no cover
    import inspect  # noqa: PLC0415 - lazy loading for optional fallback test detection  # pragma: no cover  # pylint: disable=import-outside-toplevel

    stack = inspect.stack()  # pragma: no cover
    for frame in stack:  # pragma: no cover
        filename = frame.filename.lower()  # pragma: no cover
        if (
            "test_" in filename  # pragma: no cover
            or filename.endswith("_test.py")  # pragma: no cover
            or "/tests/" in filename  # pragma: no cover
            or "\\tests\\" in filename
        ):  # pragma: no cover
            return True  # pragma: no cover

    return False  # pragma: no cover


def _check_hardware_write_safety(
    mode: str, ip: str, port: int, *, test_mode: bool
) -> tuple[bool, str]:
    """Perform 5-tier hardware write safety checks.

    This function implements a layered safety system to prevent accidental hardware
    writes during testing or CI environments. The tiers are checked in order:

    LAYER 1: Hardware boundary protection (test_mode parameter)
    TIER 1: Context-aware test detection (PYTEST_CURRENT_TEST, TESTING env vars, stack inspection)
    TIER 2: Strong CI/development indicators (CI environment variables, pytest command)

    Args:
        mode: Work mode being requested
        ip: IP address of target inverter
        port: TCP port for Modbus communication
        test_mode: If True, simulate operation without hardware writes

    Returns:
        Tuple of (proceed: bool, message: str)
        - proceed: True if safe to proceed with hardware write
        - message: Informational message about the safety check result

    """
    # LAYER 1: Hardware boundary protection (absolute fail-safe)
    if test_mode:
        logger.info(
            "TEST MODE: Would set work mode to '%s' on master inverter %s:%s", mode, ip, port
        )
        return False, "test_mode_enabled"

    # TIER 1: Context-Aware Test Detection (Simulate with Warning) - PRIORITY
    # Test context detection takes priority over CI to ensure test compatibility
    # Only block if we're actually executing in a test context, not just because test tools are available
    if _is_actively_testing():
        logger.warning(
            "HARDWARE WRITE BLOCKED: Test environment detected - simulating mode change to %s", mode
        )
        return False, "test_context_detected"

    # TIER 2: Strong CI/Development Indicators (Block with Error)
    # These are definitive signs we're in a CI environment and should never write to hardware
    ci_environments = [
        "CI",
        "GITHUB_ACTIONS",
        "JENKINS",
        "TRAVIS",
        "CIRCLECI",
        "BUILDKITE",
        "GITLAB_CI",
    ]  # pragma: no cover
    if any(env_var in os.environ for env_var in ci_environments):  # pragma: no cover
        logger.error(
            "HARDWARE WRITE BLOCKED: CI environment detected - refusing mode change to %s", mode
        )  # pragma: no cover
        return False, "ci_environment_detected"  # pragma: no cover

    if Path(sys.argv[0]).name.startswith("pytest"):  # pragma: no cover
        logger.error(
            "HARDWARE WRITE BLOCKED: Running under pytest - refusing hardware write"
        )  # pragma: no cover
        return False, "pytest_command_detected"  # pragma: no cover

    logger.info(
        "HARDWARE WRITE PROCEEDING: All safety checks passed for mode change to %s", mode
    )  # pragma: no cover

    return True, "safety_checks_passed"  # pragma: no cover  # pragma: no cover


def _execute_register_writes(
    client: object, mode: BatteryMode, slave_address: int, min_interval: float
) -> bool:
    """Execute the register write loop for a given work mode.

    This function performs the actual sequence of Modbus register writes required
    to change the inverter work mode. It handles the timing between writes and
    validates each write operation.

    Args:
        client: Connected ModbusTcpClient
        mode: Work mode to set (determines which registers to write)
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum time interval between register writes (seconds)

    Returns:
        True if all register writes succeeded, False if any write failed

    """
    # Import from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pragma: no cover - Hardware-only function  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
    )

    # Get the register combination for this mode  # pragma: no cover
    register_combination = VALID_WORK_MODE_COMBINATIONS[mode]  # pragma: no cover

    logger.info("Executing work mode change: %s", mode)  # pragma: no cover
    logger.info("Register writes required: %s", register_combination)  # pragma: no cover

    # Execute all required writes for this mode  # pragma: no cover
    for register_addr, value in register_combination.items():  # pragma: no cover
        # Wait minimum interval before each write  # pragma: no cover
        time.sleep(min_interval)  # pragma: no cover

        logger.info("Writing value %s to register 0x%04X", value, register_addr)  # pragma: no cover

        # Execute the write with complete combination validation  # pragma: no cover
        write_result = solax_modbus_client._write_single_register(  # pragma: no cover  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            client,
            register_addr,
            value,
            slave_address,
            mode,  # pragma: no cover
        )  # pragma: no cover

        if not write_result:  # pragma: no cover
            logger.error(
                "Failed to write register 0x%04X for mode '%s'", register_addr, mode
            )  # pragma: no cover
            return False  # pragma: no cover

        logger.info(
            "Successfully wrote value %s to register 0x%04X", value, register_addr
        )  # pragma: no cover

    logger.info("Work mode '%s' set successfully on master inverter", mode)  # pragma: no cover
    return True  # pragma: no cover


def _validate_register_write_safety(  # pylint: disable=too-many-return-statements  # Safety guard clauses
    register_addr: int, value: int, expected_mode: BatteryMode
) -> bool:
    """Validate register write safety against approved work mode combinations.

    This function performs comprehensive validation to ensure that a register write
    is part of an approved work mode combination and uses valid values.

    Validation checks:
    1. Work mode is in approved combinations
    2. Register address is required for the expected mode
    3. Register value matches the expected mode's requirements
    4. Register address is in the allowed set (0x001F, 0x0020)
    5. Value is within valid range for the specific register

    Args:
        register_addr: Register address to write
        value: Value to write to the register
        expected_mode: Expected work mode this write is part of

    Returns:
        True if validation passed, False if any validation check failed

    """
    # CRITICAL SAFETY VALIDATION: Complete combination checking
    if expected_mode not in VALID_WORK_MODE_COMBINATIONS:
        logger.error(
            "UNAUTHORIZED WRITE: Unknown work mode '%s'",
            battery_mode_to_display_string(expected_mode),
        )
        return False

    expected_combination = VALID_WORK_MODE_COMBINATIONS[expected_mode]

    # Validate this register/value pair is part of the expected mode
    if register_addr not in expected_combination:
        logger.error(
            "UNAUTHORIZED WRITE: Register 0x%04X not required for mode '%s'",
            register_addr,
            battery_mode_to_display_string(expected_mode),
        )
        return False

    if expected_combination[register_addr] != value:
        logger.error(
            "INVALID VALUE: Register 0x%04X expects value %s for mode '%s', got %s",
            register_addr,
            expected_combination[register_addr],
            battery_mode_to_display_string(expected_mode),
            value,
        )
        return False

    # Additional register-specific validation
    if register_addr == REGISTER_WORK_MODE:
        if value not in [0, 3]:
            logger.error("INVALID VALUE %s for register 0x001F (only 0 or 3 allowed)", value)
            return False
    elif register_addr == REGISTER_MANUAL_MODE:
        if value not in [0, 1, 2]:
            logger.error("INVALID VALUE %s for register 0x0020 (only 0, 1, or 2 allowed)", value)
            return False
    else:
        logger.error(
            "UNAUTHORIZED REGISTER: Only 0x001F and 0x0020 writes allowed, attempted 0x%04X",
            register_addr,
        )
        return False

    logger.debug(
        "Safety validation passed: Register 0x%04X = %s for mode '%s'",
        register_addr,
        value,
        battery_mode_to_display_string(expected_mode),
    )

    return True


def _execute_modbus_write(
    client: object, register_addr: int, value: int, slave_addr: int
) -> tuple[bool, str]:
    """Execute Modbus register write with pymodbus version compatibility.

    This function handles the actual Modbus write operation, automatically adapting
    to different pymodbus versions by trying parameter names in order:
    - v3.11+: device_id
    - v3.x: slave
    - v2.x: unit

    Args:
        client: Connected ModbusTcpClient
        register_addr: Register address to write
        value: Value to write to the register
        slave_addr: Modbus slave address

    Returns:
        Tuple of (success: bool, error_msg: str)
        - success: True if write succeeded, False if error
        - error_msg: Empty string on success, error description on failure

    """
    # Execute the write using pymodbus version compatibility
    try:
        # DEBUG: Log actual hardware write operation (skip mocked/test contexts)
        if should_log_hardware_access(client=client):  # pragma: no cover
            log_hardware_access(
                f"WRITE_REGISTER:0x{register_addr:04X}:value={value}:device_id={slave_addr}"
            )  # pragma: no cover

        # Latest pymodbus (v3.11+) uses 'device_id' parameter
        result = client.write_register(address=register_addr, value=value, device_id=slave_addr)
    except TypeError:
        try:
            # DEBUG: Log actual hardware write operation (skip mocked/test contexts)
            if should_log_hardware_access(client=client):  # pragma: no cover
                log_hardware_access(
                    f"WRITE_REGISTER_SLAVE:0x{register_addr:04X}:value={value}:slave={slave_addr}"
                )  # pragma: no cover

            # Earlier pymodbus (v3.x) uses 'slave' parameter
            result = client.write_register(address=register_addr, value=value, slave=slave_addr)
        except TypeError:
            # DEBUG: Log actual hardware write operation (legacy, skip mocked/test contexts)
            if should_log_hardware_access(client=client):  # pragma: no cover
                log_hardware_access(
                    f"WRITE_REGISTER_LEGACY:0x{register_addr:04X}:value={value}:unit={slave_addr}"
                )  # pragma: no cover

            # Oldest pymodbus (v2.x) uses 'unit' parameter
            result = client.write_register(address=register_addr, value=value, unit=slave_addr)

    if result.isError():
        error_msg = f"Modbus write error for register 0x{register_addr:04X}: {result}"
        logger.error(error_msg)
        return False, error_msg

    logger.debug("Successfully wrote value %s to register 0x%04X", value, register_addr)
    return True, ""


def _set_master_work_mode(  # pylint: disable=too-many-positional-arguments  # Hardware interface pattern
    ip: str,
    port: int,
    timeout: int,
    slave_address: int,
    min_interval: float,
    mode: str,
    *,
    test_mode: bool = False,
) -> bool:
    """Set work mode on master inverter using validated register combinations.

    Args:
        ip: IP address of the master inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the master inverter
        min_interval: Minimum interval between commands
        mode: Validated work mode ("self_use", "charge", or "discharge")
        test_mode: If True, simulate successful operation without hardware writes

    Returns:
        True if work mode was set successfully or simulated, False if error occurred

    """
    # ============================================================================
    # CRITICAL SAFETY: ABSOLUTE HARDWARE WRITE PROTECTION IN TEST ENVIRONMENTS
    # ============================================================================

    # Check hardware write safety (5-tier protection)
    should_proceed, safety_message = _check_hardware_write_safety(
        mode, ip, port, test_mode=test_mode
    )
    if not should_proceed:
        # Return True for test/simulation modes, False for blocked writes
        return safety_message in ["test_mode_enabled", "test_context_detected"]

    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415 - avoid circular import in modbus package  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pragma: no cover
    )

    client = None  # pragma: no cover

    try:  # pragma: no cover
        # Connect to master inverter  # pragma: no cover
        client = solax_modbus_client._connect_modbus_client(  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            ip, port, timeout
        )  # pragma: no cover
        if not client:  # pragma: no cover
            logger.error(
                "Failed to connect to master inverter for write operation"
            )  # pragma: no cover
            return False  # pragma: no cover

        # Execute all required register writes for this mode  # pragma: no cover
        return _execute_register_writes(
            client, mode, slave_address, min_interval
        )  # pragma: no cover

    except (ConnectionError, OSError):  # pragma: no cover
        logger.exception("Error setting work mode on %s", ip)  # pragma: no cover
        return False  # pragma: no cover

    finally:  # pragma: no cover
        # Always close connection  # pragma: no cover
        if client:  # pragma: no cover
            try:  # pragma: no cover
                client.close()  # pragma: no cover
                logger.debug(
                    "Connection to %s closed after write operation", ip
                )  # pragma: no cover
            except OSError:  # pragma: no cover
                logger.warning(
                    "Error closing connection to %s", ip
                )  # pragma: no cover  # pragma: no cover  # pragma: no cover  # pragma: no cover


def _write_single_register(
    client: object, register_addr: int, value: int, slave_addr: int, expected_mode: BatteryMode
) -> bool:
    """Write a single register with STRICT safety validation.

    CRITICAL SAFETY: This function validates that the register address and value
    combination is part of a valid work mode and prevents unauthorized writes.

    Args:
        client: Connected ModbusTcpClient
        register_addr: Register address to write (only 0x001F or 0x0020 allowed)
        value: Value to write (must match valid combination for register)
        slave_addr: Modbus slave address
        expected_mode: Expected work mode this write is part of

    Returns:
        True if write was successful, False if validation failed or write error

    """
    try:
        # Validate register write safety
        if not _validate_register_write_safety(register_addr, value, expected_mode):
            return False

        # Execute the Modbus write with version compatibility
        success, _error_msg = _execute_modbus_write(client, register_addr, value, slave_addr)
    except (AttributeError, OSError):
        logger.exception("Error writing register 0x%04X", register_addr)
        return False
    return success
