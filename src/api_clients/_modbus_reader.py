"""Modbus Reading Operations Module.

Internal module containing specialized reading functions for SolaX inverters.
Handles individual parameter reading, bulk data operations, and data extraction.

INTERNAL MODULE: Only for use by solax_modbus_client.py and related modules.
This module contains functions extracted from solax_modbus_client.py to improve
code organization and maintainability.

Functions include:
- Individual parameter readers (_read_single_*)
- Bulk data readers (_read_bulk_*, _parallel_bulk_*)
- Data extraction utilities (_extract_*)
"""

# pylint: disable=cyclic-import,too-many-lines
# Justification: Intentional modbus client split architecture. This helper module
# is imported by solax_modbus_client.py and uses type hints from it. The cycle
# is resolved at runtime and doesn't cause actual circular dependency issues.
# too-many-lines: Hardware interface module with comprehensive read operations (1908 lines)
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from src.core_logic.battery_simulation import BatteryMode, battery_mode_to_display_string

if TYPE_CHECKING:
    from collections.abc import Callable

# Setup basic logging
logger = logging.getLogger(__name__)

# Signed integer conversion constants
INT16_MAX_SIGNED = 32767  # Maximum value for signed 16-bit integer
INT16_UNSIGNED_OFFSET = 65536  # Offset for unsigned-to-signed conversion

# Register count constants
PV_POWER_REGISTER_COUNT = 2
WORK_MODE_REGISTER_COUNT = 2
GRID_TOTALS_REGISTER_COUNT = 2  # Grid import/export totals are 32-bit (2 registers)
MASSIVE_BLOCK_EXPECTED_SIZE = 79

# SoC validation constants
SOC_MAX_PERCENT = 100

# Battery temperature validation constants (LiFePO4 operating limits)
TEMP_MIN_CELSIUS = -20  # Minimum safe operating temperature
TEMP_MAX_CELSIUS = 60  # Maximum safe operating temperature
TEMP_WARNING_LOW_CELSIUS = 0  # Below freezing - charge rate severely limited
TEMP_WARNING_HIGH_CELSIUS = 45  # High temperature - thermal management may engage

# Work mode constants
WORK_MODE_MANUAL = 3  # Manual mode requires manual_mode register

# For test compatibility, we import functions directly from main module
# This ensures that when tests patch 'src.api_clients.solax_modbus_client._connect_modbus_client',
# the patches work correctly because we're calling the actual patched function
#
# We need to import these functions after they're defined in the main module,
# so we use dynamic imports within the functions that need them


# Individual parameter reading functions


def _read_single_inverter_serial(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> str | None:
    """Read serial number from a single inverter via Modbus TCP with retry logic.

    ENHANCED RELIABILITY: Implements 4 total attempts (1 + 3 retries) with
    connection recreation on failure and proper 1.0s, 1.5s, 2.0s, 3.0s retry timing
    for both connection failures and read failures.

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum interval between commands

    Returns:
        Serial number string or None if all attempts fail

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    max_retries = 4

    for attempt in range(max_retries):
        client = None

        try:
            # Connect to inverter (new connection each attempt)
            client = solax_modbus_client._connect_modbus_client(ip, port, timeout)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            if not client:
                logger.warning(
                    "Serial number connection failed on attempt %s/%s to %s",
                    attempt + 1,
                    max_retries,
                    ip,
                )
            else:
                # Connection successful, try the read
                time.sleep(min_interval)

                # Read serial number registers (0x0000, 7 registers) using Holding Registers
                registers = solax_modbus_client._read_holding_registers(  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    client, 0x0000, 7, slave_address
                )
                if not registers:
                    logger.warning(
                        "Serial number read failed on attempt %s/%s for %s",
                        attempt + 1,
                        max_retries,
                        ip,
                    )
                else:
                    # Success! Format serial number using existing logic
                    serial_number = solax_modbus_client._format_serial_number(registers)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    if serial_number == "ERROR":
                        logger.warning(
                            "Serial number format error on attempt %s/%s for %s",
                            attempt + 1,
                            max_retries,
                            ip,
                        )
                    else:
                        logger.debug(
                            "Serial number from %s: %s on attempt %s",
                            ip,
                            serial_number,
                            attempt + 1,
                        )
                        return serial_number

        except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
            logger.warning(
                "Serial number error on attempt %s/%s for %s: %s",
                attempt + 1,
                max_retries,
                ip,
                e,
            )

        finally:
            # Always close connection (connect → read → close → retry pattern)
            if client:
                try:
                    client.close()
                    logger.debug(
                        "Connection to %s closed after serial number attempt %s",
                        ip,
                        attempt + 1,
                    )
                except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
                    logger.warning("Error closing connection to %s: %s", ip, e)

        # Retry timing logic - ALWAYS executed for failed attempts (connection, read, OR format failures)
        if attempt < max_retries - 1:
            retry_delay = [1.0, 1.5, 2.0, 3.0][attempt]  # 1.0s, 1.5s, 2.0s, 3.0s
            logger.debug("Retrying serial number read from %s in %s...", ip, retry_delay)
            time.sleep(retry_delay)

    logger.error("All %s serial number read attempts failed for %s", max_retries, ip)
    return None


def _read_single_ac_power(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> int | None:
    """Read AC power from a single inverter via Modbus TCP with retry logic.

    ENHANCED RELIABILITY: Implements 4 total attempts (1 + 3 retries) with
    connection recreation on failure and proper 1.0s, 1.5s, 2.0s, 3.0s retry timing
    for both connection failures and read failures.

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum interval between commands

    Returns:
        AC power value in watts (signed integer) or None if all attempts fail

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    max_retries = 4

    for attempt in range(max_retries):
        client = None

        try:
            # Connect to inverter (new connection each attempt)
            client = solax_modbus_client._connect_modbus_client(ip, port, timeout)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            if not client:
                logger.warning(
                    "AC power connection failed on attempt %s/%s to %s",
                    attempt + 1,
                    max_retries,
                    ip,
                )
            else:
                # Connection successful, try the read
                time.sleep(min_interval)

                # Read AC power register (0x0002) using Input Registers (Function Code 0x04)
                registers = solax_modbus_client._read_input_registers(  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    client, 0x0002, 1, slave_address
                )
                if not registers:
                    logger.warning(
                        "AC power read failed on attempt %s/%s for %s",
                        attempt + 1,
                        max_retries,
                        ip,
                    )
                else:
                    # Success! Convert from unsigned to signed (int16)
                    raw_value = registers[0]
                    if raw_value > INT16_MAX_SIGNED:  # noqa: SIM108 - Explicit if-else improves readability over ternary
                        signed_value = raw_value - INT16_UNSIGNED_OFFSET
                    else:
                        signed_value = raw_value

                    logger.debug(
                        "AC power from %s: %sW (raw: %s) on attempt %s",
                        ip,
                        signed_value,
                        raw_value,
                        attempt + 1,
                    )
                    return signed_value

        except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
            logger.warning(
                "AC power error on attempt %s/%s for %s: %s", attempt + 1, max_retries, ip, e
            )

        finally:
            # Always close connection (connect → read → close → retry pattern)
            if client:
                try:
                    client.close()
                    logger.debug(
                        "Connection to %s closed after AC power attempt %s", ip, attempt + 1
                    )
                except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
                    logger.warning("Error closing connection to %s: %s", ip, e)

        # Retry timing logic - ALWAYS executed for failed attempts (connection OR read failures)
        if attempt < max_retries - 1:
            retry_delay = [1.0, 1.5, 2.0, 3.0][attempt]  # 1.0s, 1.5s, 2.0s, 3.0s
            logger.debug("Retrying AC power read from %s in %s...", ip, retry_delay)
            time.sleep(retry_delay)

    logger.error("All %s AC power read attempts failed for %s", max_retries, ip)
    return None


def _read_single_battery_temperature(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> int | None:
    """Read battery temperature from a single inverter via Modbus TCP with retry logic.

    ENHANCED RELIABILITY: Implements 4 total attempts (1 + 3 retries) with
    connection recreation on failure and proper 1.0s, 1.5s, 2.0s, 3.0s retry timing
    for both connection failures and read failures.

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum interval between commands

    Returns:
        Battery temperature in degrees Celsius (int16 signed, -20°C to +60°C)
        or None if all attempts fail

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    max_retries = 4

    for attempt in range(max_retries):
        client = None

        try:
            # Connect to inverter (new connection each attempt)
            client = solax_modbus_client._connect_modbus_client(ip, port, timeout)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            if not client:
                logger.warning(
                    "Battery temperature connection failed on attempt %s/%s to %s",
                    attempt + 1,
                    max_retries,
                    ip,
                )
            else:
                # Connection successful, try the read
                time.sleep(min_interval)

                # Read battery temperature register (0x0018) using Input Registers (Function Code 0x04)
                registers = solax_modbus_client._read_input_registers(  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    client, 0x0018, 1, slave_address
                )
                if not registers:
                    logger.warning(
                        "Battery temperature read failed on attempt %s/%s for %s",
                        attempt + 1,
                        max_retries,
                        ip,
                    )
                else:
                    # Success! Convert from unsigned to signed (int16)
                    raw_value = registers[0]
                    if raw_value > INT16_MAX_SIGNED:  # noqa: SIM108 - Explicit if-else improves readability over ternary
                        signed_value = raw_value - INT16_UNSIGNED_OFFSET
                    else:
                        signed_value = raw_value

                    logger.debug(
                        "Battery temperature from %s: %s°C (raw: %s) on attempt %s",
                        ip,
                        signed_value,
                        raw_value,
                        attempt + 1,
                    )
                    return signed_value

        except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
            logger.warning(
                "Battery temperature error on attempt %s/%s for %s: %s",
                attempt + 1,
                max_retries,
                ip,
                e,
            )

        finally:
            # Always close connection (connect → read → close → retry pattern)
            if client:
                try:
                    client.close()
                    logger.debug(
                        "Connection to %s closed after battery temperature attempt %s",
                        ip,
                        attempt + 1,
                    )
                except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
                    logger.warning("Error closing connection to %s: %s", ip, e)

        # Retry timing logic - ALWAYS executed for failed attempts (connection OR read failures)
        if attempt < max_retries - 1:
            retry_delay = [1.0, 1.5, 2.0, 3.0][attempt]  # 1.0s, 1.5s, 2.0s, 3.0s
            logger.debug("Retrying battery temperature read from %s in %s...", ip, retry_delay)
            time.sleep(retry_delay)

    logger.error("All %s battery temperature read attempts failed for %s", max_retries, ip)
    return None


def _read_single_rtc_timestamp(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> str | None:
    """Read RTC timestamp from a single inverter via Modbus TCP with retry logic.

    CRITICAL: Uses Function Code 0x03 (Holding Registers), not 0x04 as documented.
    Year format: register_value + 2000 = actual year.

    ENHANCED RELIABILITY: Implements 4 total attempts (1 + 3 retries) with
    connection recreation on failure and proper 1.0s, 1.5s, 2.0s, 3.0s retry timing
    for both connection failures and read failures.

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum interval between commands

    Returns:
        Formatted timestamp string or None if all attempts fail

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    max_retries = 4

    for attempt in range(max_retries):
        client = None

        try:
            # Connect to inverter (new connection each attempt)
            client = solax_modbus_client._connect_modbus_client(ip, port, timeout)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            if not client:
                logger.warning(
                    "RTC timestamp connection failed on attempt %s/%s to %s",
                    attempt + 1,
                    max_retries,
                    ip,
                )
            else:
                # Connection successful, try the read
                time.sleep(min_interval)

                # Read RTC registers (0x0085-0x008A = 6 registers) using Holding Registers
                # CRITICAL: Must use Function Code 0x03, NOT 0x04 despite documentation
                registers = solax_modbus_client._read_holding_registers(  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    client, 0x0085, 6, slave_address
                )
                if not registers:
                    logger.warning(
                        "RTC timestamp read failed on attempt %s/%s for %s",
                        attempt + 1,
                        max_retries,
                        ip,
                    )
                else:
                    # Success! Format timestamp using existing logic
                    timestamp = solax_modbus_client._format_rtc_timestamp(registers)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    if timestamp == "ERROR":
                        logger.warning(
                            "RTC timestamp format error on attempt %s/%s for %s",
                            attempt + 1,
                            max_retries,
                            ip,
                        )
                    else:
                        logger.debug(
                            "RTC timestamp from %s: %s (raw: %s) on attempt %s",
                            ip,
                            timestamp,
                            registers,
                            attempt + 1,
                        )
                        return timestamp

        except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
            logger.warning(
                "RTC timestamp error on attempt %s/%s for %s: %s",
                attempt + 1,
                max_retries,
                ip,
                e,
            )

        finally:
            # Always close connection (connect → read → close → retry pattern)
            if client:
                try:
                    client.close()
                    logger.debug(
                        "Connection to %s closed after RTC timestamp attempt %s",
                        ip,
                        attempt + 1,
                    )
                except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
                    logger.warning("Error closing connection to %s: %s", ip, e)

        # Retry timing logic - ALWAYS executed for failed attempts (connection, read, OR format failures)
        if attempt < max_retries - 1:
            retry_delay = [1.0, 1.5, 2.0, 3.0][attempt]  # 1.0s, 1.5s, 2.0s, 3.0s
            logger.debug("Retrying RTC timestamp read from %s in %s...", ip, retry_delay)
            time.sleep(retry_delay)

    logger.error("All %s RTC timestamp read attempts failed for %s", max_retries, ip)
    return None


def _read_single_run_mode(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> str | None:
    """Read run mode from a single inverter via Modbus TCP with retry logic.

    ENHANCED RELIABILITY: Implements 4 total attempts (1 + 3 retries) with
    connection recreation on failure and proper 1.0s, 1.5s, 2.0s, 3.0s retry timing
    for both connection failures and read failures.

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum interval between commands

    Returns:
        Descriptive run mode string or None if all attempts fail

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    max_retries = 4

    for attempt in range(max_retries):
        client = None

        try:
            # Connect to inverter (new connection each attempt)
            client = solax_modbus_client._connect_modbus_client(ip, port, timeout)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            if not client:
                logger.warning(
                    "Run mode connection failed on attempt %s/%s to %s",
                    attempt + 1,
                    max_retries,
                    ip,
                )
            else:
                # Connection successful, try the read
                time.sleep(min_interval)

                # Read run mode register (0x0009) using Input Registers (Function Code 0x04)
                registers = solax_modbus_client._read_input_registers(  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    client, 0x0009, 1, slave_address
                )
                if not registers:
                    logger.warning(
                        "Run mode read failed on attempt %s/%s for %s",
                        attempt + 1,
                        max_retries,
                        ip,
                    )
                else:
                    # Success! Get raw run mode value and convert to descriptive text
                    raw_mode = registers[0]
                    mode_text = solax_modbus_client._interpret_run_mode(raw_mode)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API

                    logger.debug(
                        "Run mode from %s: %s (raw: %s) on attempt %s",
                        ip,
                        mode_text,
                        raw_mode,
                        attempt + 1,
                    )
                    return mode_text

        except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
            logger.warning(
                "Run mode error on attempt %s/%s for %s: %s", attempt + 1, max_retries, ip, e
            )

        finally:
            # Always close connection (connect → read → close → retry pattern)
            if client:
                try:
                    client.close()
                    logger.debug(
                        "Connection to %s closed after run mode attempt %s", ip, attempt + 1
                    )
                except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
                    logger.warning("Error closing connection to %s: %s", ip, e)

        # Retry timing logic - ALWAYS executed for failed attempts (connection OR read failures)
        if attempt < max_retries - 1:
            retry_delay = [1.0, 1.5, 2.0, 3.0][attempt]  # 1.0s, 1.5s, 2.0s, 3.0s
            logger.debug("Retrying run mode read from %s in %s...", ip, retry_delay)
            time.sleep(retry_delay)

    logger.error("All %s run mode read attempts failed for %s", max_retries, ip)
    return None


# Helper functions for code reuse and reduced complexity


def _convert_uint16_to_int16(value: int) -> int:
    """Convert unsigned 16-bit value to signed 16-bit integer.

    Args:
        value: Unsigned 16-bit value (0-65535)

    Returns:
        Signed 16-bit integer (-32768 to 32767)

    """
    if value > INT16_MAX_SIGNED:
        return value - INT16_UNSIGNED_OFFSET
    return value


def _interpret_battery_mode_from_power(power_watts: int) -> BatteryMode:
    """Determine battery mode from power value.

    Args:
        power_watts: Battery power in watts (signed integer)

    Returns:
        BatteryMode enum value based on power sign

    """
    if power_watts > 0:
        return BatteryMode.FORCE_CHARGE
    if power_watts < 0:
        return BatteryMode.FORCE_DISCHARGE
    return BatteryMode.IDLE


def _validate_power_physical_limits(power_values: list[tuple[str, float]]) -> None:
    """Validate power values against physical limits.

    Physical limit: 26kW (100A × 240V + 2kW headroom)
    Warning level: 20kW (83A)

    Args:
        power_values: List of (name, power_kw) tuples to validate

    """  # noqa: RUF002
    MAX_POWER_KW = 26.0  # noqa: N806 - Physical constant, uppercase for clarity  # pylint: disable=invalid-name  # Constant
    WARNING_POWER_KW = 20.0  # noqa: N806 - Physical constant, uppercase for clarity  # pylint: disable=invalid-name  # Constant

    for power_name, power_kw in power_values:
        if abs(power_kw) > MAX_POWER_KW:
            logger.error(
                "⚠️  PHYSICALLY IMPOSSIBLE POWER DETECTED: %s = %.2fkW exceeds %.1fkW limit (100A supply)",
                power_name,
                power_kw,
                MAX_POWER_KW,
            )
        elif abs(power_kw) > WARNING_POWER_KW:
            logger.warning(
                "🔺 HIGH POWER WARNING: %s = %.2fkW approaching %.1fkW limit",
                power_name,
                power_kw,
                MAX_POWER_KW,
            )


def _interpret_work_mode(work_mode_raw: int, manual_mode_raw: int) -> BatteryMode:
    """Interpret work mode from register values.

    Args:
        work_mode_raw: Work mode register value (0x008B)
        manual_mode_raw: Manual mode register value (0x008C)

    Returns:
        BatteryMode enum value

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    if work_mode_raw != WORK_MODE_MANUAL:
        return BatteryMode.SELF_USE
    return solax_modbus_client._interpret_manual_mode(manual_mode_raw)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API


def _read_single_battery_power(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> dict[str, Any] | None:
    """Read battery power and interpret mode from a single inverter via Modbus TCP with retry logic.

    ENHANCED RELIABILITY: Implements 4 total attempts (1 + 3 retries) with
    connection recreation on failure and proper 1.0s, 1.5s, 2.0s, 3.0s retry timing
    for both connection failures and read failures.

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum interval between commands

    Returns:
        Dictionary with power and mode or None if all attempts fail

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    max_retries = 4

    for attempt in range(max_retries):
        client = None

        try:
            # Connect to inverter (new connection each attempt)
            client = solax_modbus_client._connect_modbus_client(ip, port, timeout)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            if not client:
                logger.warning(
                    "Battery power connection failed on attempt %s/%s to %s",
                    attempt + 1,
                    max_retries,
                    ip,
                )
            else:
                # Connection successful, try the read
                time.sleep(min_interval)

                # Read battery power register (0x0016) using Input Registers (Function Code 0x04)
                registers = solax_modbus_client._read_input_registers(  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    client, 0x0016, 1, slave_address
                )
                if not registers:
                    logger.warning(
                        "Battery power read failed on attempt %s/%s for %s",
                        attempt + 1,
                        max_retries,
                        ip,
                    )
                else:
                    # Success! Convert from unsigned to signed (int16)
                    raw_value = registers[0]
                    signed_value = _convert_uint16_to_int16(raw_value)

                    # Interpret battery mode from power value using helper
                    mode = _interpret_battery_mode_from_power(signed_value)

                    result = {"power": signed_value, "mode": mode}

                    logger.debug(
                        "Battery power from %s: %sW (%s) (raw: %s) on attempt %s",
                        ip,
                        signed_value,
                        mode,
                        raw_value,
                        attempt + 1,
                    )
                    return result

        except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
            logger.warning(
                "Battery power error on attempt %s/%s for %s: %s",
                attempt + 1,
                max_retries,
                ip,
                e,
            )

        finally:
            # Always close connection (connect → read → close → retry pattern)
            if client:
                try:
                    client.close()
                    logger.debug(
                        "Connection to %s closed after battery power attempt %s",
                        ip,
                        attempt + 1,
                    )
                except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
                    logger.warning("Error closing connection to %s: %s", ip, e)

        # Retry timing logic - ALWAYS executed for failed attempts (connection OR read failures)
        if attempt < max_retries - 1:
            retry_delay = [1.0, 1.5, 2.0, 3.0][attempt]  # 1.0s, 1.5s, 2.0s, 3.0s
            logger.debug("Retrying battery power read from %s in %s...", ip, retry_delay)
            time.sleep(retry_delay)

    logger.error("All %s battery power read attempts failed for %s", max_retries, ip)
    return None


def _read_single_grid_power(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> int | None:
    """Read grid power from a single inverter via Modbus TCP with retry logic.

    Uses register 0x0046 with Function Code 0x04 (Input Registers).

    REGISTER 0x0046 VALIDATION RESULTS:
    ===================================
    Through controlled testing across different inverter modes, this register
    provides ACCURATE and RELIABLE grid power measurements:

    - SELF_USE mode: 0W (legitimate - perfect energy balance via battery buffering)
    - FORCE_DISCHARGE mode: +11,694W (grid export - positive values)
    - FORCE_CHARGE mode: -12,501W (grid import - negative values)

    Sign Convention: Positive = Export to grid, Negative = Import from grid
    Data Format: Signed 16-bit integer (int16) requiring conversion for values > 32767

    The previous assumption that "this register always reads zero" was incorrect.
    Zero readings during SELF_USE mode indicate optimal system operation where
    batteries handle all power fluctuations, maintaining grid balance.

    ENHANCED RELIABILITY: Implements 4 total attempts (1 + 3 retries) with
    connection recreation on failure and proper 1.0s, 1.5s, 2.0s, 3.0s retry timing
    for both connection failures and read failures.

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum interval between commands

    Returns:
        Grid power value in watts (signed integer) or None if all attempts fail

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    max_retries = 4

    for attempt in range(max_retries):
        client = None

        try:
            # Connect to inverter (new connection each attempt)
            client = solax_modbus_client._connect_modbus_client(ip, port, timeout)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            if not client:
                logger.warning(
                    "Grid power connection failed on attempt %s/%s to %s",
                    attempt + 1,
                    max_retries,
                    ip,
                )
            else:
                # Connection successful, try the read
                time.sleep(min_interval)

                # Read grid power register (0x0046) using Input Registers (Function Code 0x04)
                # Based on test results: FC 0x04 provides valid data, FC 0x03 returns 0
                registers = solax_modbus_client._read_input_registers(  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    client, 0x0046, 1, slave_address
                )
                if not registers:
                    logger.warning(
                        "Grid power read failed on attempt %s/%s for %s",
                        attempt + 1,
                        max_retries,
                        ip,
                    )
                else:
                    # Success! Convert from unsigned to signed (int16)
                    raw_value = registers[0]
                    if raw_value > INT16_MAX_SIGNED:  # noqa: SIM108 - Explicit if-else improves readability over ternary
                        signed_value = raw_value - INT16_UNSIGNED_OFFSET
                    else:
                        signed_value = raw_value

                    logger.debug(
                        "Grid power from %s: %sW (raw: %s) on attempt %s",
                        ip,
                        signed_value,
                        raw_value,
                        attempt + 1,
                    )
                    return signed_value

        except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
            logger.warning(
                "Grid power error on attempt %s/%s for %s: %s", attempt + 1, max_retries, ip, e
            )

        finally:
            # Always close connection (connect → read → close → retry pattern)
            if client:
                try:
                    client.close()
                    logger.debug(
                        "Connection to %s closed after grid power attempt %s",
                        ip,
                        attempt + 1,
                    )
                except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
                    logger.warning("Error closing connection to %s: %s", ip, e)

        # Retry timing logic - ALWAYS executed for failed attempts (connection OR read failures)
        if attempt < max_retries - 1:
            retry_delay = [1.0, 1.5, 2.0, 3.0][attempt]  # 1.0s, 1.5s, 2.0s, 3.0s
            logger.debug("Retrying grid power read from %s in %s...", ip, retry_delay)
            time.sleep(retry_delay)

    logger.error("All %s grid power read attempts failed for %s", max_retries, ip)
    return None


def _read_single_soc(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> int | None:
    """Read State of Charge from a single inverter via Modbus TCP with retry logic.

    ENHANCED RELIABILITY: Implements 4 total attempts (1 + 3 retries) with
    connection recreation on failure and proper 1.0s, 1.5s, 2.0s, 3.0s retry timing
    for both connection failures and read failures.

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum interval between commands

    Returns:
        SoC percentage (0-100) or None if all attempts fail

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    max_retries = 4

    for attempt in range(max_retries):
        client = None

        try:
            # Connect to inverter (new connection each attempt)
            client = solax_modbus_client._connect_modbus_client(ip, port, timeout)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            if not client:
                logger.warning(
                    "SoC connection failed on attempt %s/%s to %s",
                    attempt + 1,
                    max_retries,
                    ip,
                )
            else:
                # Connection successful, try the read
                time.sleep(min_interval)

                # Read SoC register (0x001C) using Input Registers (Function Code 0x04)
                registers = solax_modbus_client._read_input_registers(  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    client, 0x001C, 1, slave_address
                )
                if not registers:
                    logger.warning(
                        "SoC read failed on attempt %s/%s for %s",
                        attempt + 1,
                        max_retries,
                        ip,
                    )
                else:
                    # Success! Extract SoC value (uint16, 0-100%)
                    soc_value = registers[0]

                    # Validate range using existing logic
                    if soc_value > SOC_MAX_PERCENT:
                        logger.warning(
                            "Invalid SoC value from %s: %s%% (expected 0-100%%) on attempt %s",
                            ip,
                            soc_value,
                            attempt + 1,
                        )
                        # Continue to retry for invalid range
                    else:
                        logger.debug("SoC from %s: %s%% on attempt %s", ip, soc_value, attempt + 1)
                        return soc_value

        except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
            logger.warning("SoC error on attempt %s/%s for %s: %s", attempt + 1, max_retries, ip, e)

        finally:
            # Always close connection (connect → read → close → retry pattern)
            if client:
                try:
                    client.close()
                    logger.debug("Connection to %s closed after SoC attempt %s", ip, attempt + 1)
                except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
                    logger.warning("Error closing connection to %s: %s", ip, e)

        # Retry timing logic - ALWAYS executed for failed attempts (connection, read, OR validation failures)
        if attempt < max_retries - 1:
            retry_delay = [1.0, 1.5, 2.0, 3.0][attempt]  # 1.0s, 1.5s, 2.0s, 3.0s
            logger.debug("Retrying SoC read from %s in %s...", ip, retry_delay)
            time.sleep(retry_delay)

    logger.error("All %s SoC read attempts failed for %s", max_retries, ip)
    return None


def _read_single_pv_power(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> dict[str, int] | None:
    """Read PV power from both strings of a single inverter via Modbus TCP with retry logic.

    ENHANCED RELIABILITY: Implements 4 total attempts (1 + 3 retries) with
    connection recreation on failure and proper 1.0s, 1.5s, 2.0s, 3.0s retry timing
    for both connection failures and read failures.

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum interval between commands

    Returns:
        Dictionary with PV1 and PV2 power values or None if all attempts fail

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    max_retries = 4

    for attempt in range(max_retries):
        client = None

        try:
            # Connect to inverter (new connection each attempt)
            client = solax_modbus_client._connect_modbus_client(ip, port, timeout)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            if not client:
                logger.warning(
                    "PV power connection failed on attempt %s/%s to %s",
                    attempt + 1,
                    max_retries,
                    ip,
                )
            else:
                # Connection successful, try the read
                time.sleep(min_interval)

                # Read PV power registers (0x000A, 0x000B) using Input Registers (Function Code 0x04)
                registers = solax_modbus_client._read_input_registers(  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    client, 0x000A, 2, slave_address
                )
                if not registers or len(registers) != PV_POWER_REGISTER_COUNT:
                    logger.warning(
                        "PV power read failed on attempt %s/%s for %s",
                        attempt + 1,
                        max_retries,
                        ip,
                    )
                else:
                    # Success! Extract PV string power values (uint16, watts)
                    pv1_power = registers[0]
                    pv2_power = registers[1]

                    result = {"pv1": pv1_power, "pv2": pv2_power}

                    logger.debug(
                        "PV power from %s: PV1=%sW, PV2=%sW on attempt %s",
                        ip,
                        pv1_power,
                        pv2_power,
                        attempt + 1,
                    )
                    return result

        except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
            logger.warning(
                "PV power error on attempt %s/%s for %s: %s", attempt + 1, max_retries, ip, e
            )

        finally:
            # Always close connection (connect → read → close → retry pattern)
            if client:
                try:
                    client.close()
                    logger.debug(
                        "Connection to %s closed after PV power attempt %s", ip, attempt + 1
                    )
                except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
                    logger.warning("Error closing connection to %s: %s", ip, e)

        # Retry timing logic - ALWAYS executed for failed attempts (connection OR read failures)
        if attempt < max_retries - 1:
            retry_delay = [1.0, 1.5, 2.0, 3.0][attempt]  # 1.0s, 1.5s, 2.0s, 3.0s
            logger.debug("Retrying PV power read from %s in %s...", ip, retry_delay)
            time.sleep(retry_delay)

    logger.error("All %s PV power read attempts failed for %s", max_retries, ip)
    return None


def _read_single_daily_yield(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> float | None:
    """Read daily yield from a single inverter via Modbus TCP with retry logic.

    ENHANCED RELIABILITY: Implements 4 total attempts (1 + 3 retries) with
    connection recreation on failure and proper 1.0s, 1.5s, 2.0s, 3.0s retry timing
    for both connection failures and read failures.

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum interval between commands

    Returns:
        Daily yield in kWh or None if all attempts fail

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    max_retries = 4

    for attempt in range(max_retries):
        client = None

        try:
            # Connect to inverter (new connection each attempt)
            client = solax_modbus_client._connect_modbus_client(ip, port, timeout)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            if not client:
                logger.warning(
                    "Daily yield connection failed on attempt %s/%s to %s",
                    attempt + 1,
                    max_retries,
                    ip,
                )
            else:
                # Connection successful, try the read
                time.sleep(min_interval)

                # Read daily yield register (0x0050) using Input Registers (Function Code 0x04)
                registers = solax_modbus_client._read_input_registers(  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    client, 0x0050, 1, slave_address
                )
                if not registers:
                    logger.warning(
                        "Daily yield read failed on attempt %s/%s for %s",
                        attempt + 1,
                        max_retries,
                        ip,
                    )
                else:
                    # Success! Extract daily yield value (uint16, 0.1kWh units)
                    raw_yield = registers[0]

                    # Convert from 0.1kWh units to kWh using existing logic
                    yield_kwh = raw_yield * 0.1

                    logger.debug(
                        "Daily yield from %s: %.2f kWh (raw: %s) on attempt %s",
                        ip,
                        yield_kwh,
                        raw_yield,
                        attempt + 1,
                    )
                    return yield_kwh

        except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
            logger.warning(
                "Daily yield error on attempt %s/%s for %s: %s",
                attempt + 1,
                max_retries,
                ip,
                e,
            )

        finally:
            # Always close connection (connect → read → close → retry pattern)
            if client:
                try:
                    client.close()
                    logger.debug(
                        "Connection to %s closed after daily yield attempt %s",
                        ip,
                        attempt + 1,
                    )
                except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
                    logger.warning("Error closing connection to %s: %s", ip, e)

        # Retry timing logic - ALWAYS executed for failed attempts (connection OR read failures)
        if attempt < max_retries - 1:
            retry_delay = [1.0, 1.5, 2.0, 3.0][attempt]  # 1.0s, 1.5s, 2.0s, 3.0s
            logger.debug("Retrying daily yield read from %s in %s...", ip, retry_delay)
            time.sleep(retry_delay)

    logger.error("All %s daily yield read attempts failed for %s", max_retries, ip)
    return None


def _read_single_battery_capacity(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> float | None:
    """Read battery capacity from a single inverter via Modbus TCP with retry logic.

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum interval between commands

    Returns:
        Battery capacity in kWh or None if error occurs

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    # Retry configuration: 4 total attempts (1 + 3 retries) with exponential backoff
    retry_delays = [1.0, 1.5, 2.0, 3.0]  # seconds between retry attempts
    max_attempts = len(retry_delays)

    for attempt in range(max_attempts):
        client = None

        try:
            # Connect to inverter
            client = solax_modbus_client._connect_modbus_client(ip, port, timeout)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            if not client:
                if attempt < max_attempts - 1:
                    logger.warning(
                        "Connection failed to %s (attempt %s/%s), retrying in %ss",
                        ip,
                        attempt + 1,
                        max_attempts,
                        retry_delays[attempt],
                    )
                    time.sleep(retry_delays[attempt])
                else:
                    logger.error("All connection attempts failed to %s", ip)
                    return None
            else:
                # Wait minimum interval before reading
                time.sleep(min_interval)

                # Read battery capacity registers (0x0026-0x0027) using Input Registers (Function Code 0x04)
                registers = solax_modbus_client._read_input_registers(  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    client, 0x0026, 2, slave_address
                )
                if not registers or len(registers) != PV_POWER_REGISTER_COUNT:
                    if attempt < max_attempts - 1:
                        logger.warning(
                            "Register read failed from %s (attempt %s/%s), retrying in %ss",
                            ip,
                            attempt + 1,
                            max_attempts,
                            retry_delays[attempt],
                        )
                        time.sleep(retry_delays[attempt])
                    else:
                        logger.error("All register read attempts failed from %s", ip)
                        return None
                else:
                    # Extract LSB and MSB values
                    lsb_value = registers[0]
                    msb_value = registers[1]

                    # Combine into uint32 (Wh)
                    capacity_wh = lsb_value + (msb_value << 16)

                    # Convert to kWh
                    capacity_kwh = capacity_wh / 1000.0

                    logger.debug(
                        "Battery capacity from %s: %.2f kWh (raw: LSB=%s, MSB=%s, combined=%sWh)",
                        ip,
                        capacity_kwh,
                        lsb_value,
                        msb_value,
                        capacity_wh,
                    )
                    return capacity_kwh

        except (  # pylint: disable=broad-exception-caught  # Hardware can fail unpredictably
            Exception
        ) as e:
            if attempt < max_attempts - 1:
                logger.warning(
                    "Error reading battery capacity from %s (attempt %s/%s): %s, retrying in %ss",
                    ip,
                    attempt + 1,
                    max_attempts,
                    e,
                    retry_delays[attempt],
                )
                time.sleep(retry_delays[attempt])
            else:
                logger.exception("All attempts failed reading battery capacity from %s", ip)
                return None

        finally:
            # Always close connection
            if client:
                try:
                    client.close()
                    logger.debug("Connection to %s closed", ip)
                except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
                    logger.warning("Error closing connection to %s: %s", ip, e)

    # This should never be reached due to explicit returns above
    return None  # pragma: no cover  # pragma: no cover  # pragma: no cover  # pragma: no cover


def _read_single_grid_export_total(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> float | None:
    """Read grid export total from a single inverter via Modbus TCP with retry logic.

    ENHANCED RELIABILITY: Implements 4 total attempts (1 + 3 retries) with
    connection recreation on failure and proper 1.0s, 1.5s, 2.0s, 3.0s retry timing
    for both connection failures and read failures.

    Reads registers 0x0048-0x0049 (feedin_energy_total) - cumulative energy exported
    to the grid since inverter installation. This is a 32-bit unsigned integer in
    0.01 kWh units.

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum interval between commands

    Returns:
        Grid export total in kWh or None if all attempts fail

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    max_retries = 4

    for attempt in range(max_retries):
        client = None

        try:
            # Connect to inverter (new connection each attempt)
            client = solax_modbus_client._connect_modbus_client(ip, port, timeout)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            if not client:
                logger.warning(
                    "Grid export total connection failed on attempt %s/%s to %s",
                    attempt + 1,
                    max_retries,
                    ip,
                )
            else:
                # Connection successful, try the read
                time.sleep(min_interval)

                # Read grid export registers (0x0048-0x0049) using Input Registers (Function Code 0x04)
                # uint32 stored across two registers: 0x0048 (LSB) and 0x0049 (MSB)
                registers = solax_modbus_client._read_input_registers(  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    client, 0x0048, GRID_TOTALS_REGISTER_COUNT, slave_address
                )
                if not registers or len(registers) != GRID_TOTALS_REGISTER_COUNT:
                    logger.warning(
                        "Grid export total read failed on attempt %s/%s for %s",
                        attempt + 1,
                        max_retries,
                        ip,
                    )
                else:
                    # Success! Combine uint32 from two uint16 registers: value = (MSB << 16) | LSB
                    lsb = registers[0]
                    msb = registers[1]
                    raw_value = (msb << 16) | lsb

                    # Convert from 0.01kWh units to kWh
                    export_kwh = raw_value * 0.01

                    logger.debug(
                        "Grid export total from %s: %.2f kWh (raw: %s, LSB: %s, MSB: %s) on attempt %s",
                        ip,
                        export_kwh,
                        raw_value,
                        lsb,
                        msb,
                        attempt + 1,
                    )
                    return export_kwh

        except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
            logger.warning(
                "Grid export total error on attempt %s/%s for %s: %s",
                attempt + 1,
                max_retries,
                ip,
                e,
            )

        finally:
            # Always close connection (connect → read → close → retry pattern)
            if client:
                try:
                    client.close()
                    logger.debug(
                        "Connection to %s closed after grid export total attempt %s",
                        ip,
                        attempt + 1,
                    )
                except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
                    logger.warning("Error closing connection to %s: %s", ip, e)

        # Retry timing logic - ALWAYS executed for failed attempts (connection OR read failures)
        if attempt < max_retries - 1:
            retry_delay = [1.0, 1.5, 2.0, 3.0][attempt]  # 1.0s, 1.5s, 2.0s, 3.0s
            logger.debug("Retrying grid export total read from %s in %ss...", ip, retry_delay)
            time.sleep(retry_delay)

    logger.error("All %s grid export total read attempts failed for %s", max_retries, ip)
    return None


def _read_single_grid_import_total(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> float | None:
    """Read grid import total from a single inverter via Modbus TCP with retry logic.

    ENHANCED RELIABILITY: Implements 4 total attempts (1 + 3 retries) with
    connection recreation on failure and proper 1.0s, 1.5s, 2.0s, 3.0s retry timing
    for both connection failures and read failures.

    Reads registers 0x004A-0x004B (consum_energy_total) - cumulative energy imported
    from the grid since inverter installation. This is a 32-bit unsigned integer in
    0.01 kWh units.

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum interval between commands

    Returns:
        Grid import total in kWh or None if all attempts fail

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    max_retries = 4

    for attempt in range(max_retries):
        client = None

        try:
            # Connect to inverter (new connection each attempt)
            client = solax_modbus_client._connect_modbus_client(ip, port, timeout)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            if not client:
                logger.warning(
                    "Grid import total connection failed on attempt %s/%s to %s",
                    attempt + 1,
                    max_retries,
                    ip,
                )
            else:
                # Connection successful, try the read
                time.sleep(min_interval)

                # Read grid import registers (0x004A-0x004B) using Input Registers (Function Code 0x04)
                # uint32 stored across two registers: 0x004A (LSB) and 0x004B (MSB)
                registers = solax_modbus_client._read_input_registers(  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    client, 0x004A, GRID_TOTALS_REGISTER_COUNT, slave_address
                )
                if not registers or len(registers) != GRID_TOTALS_REGISTER_COUNT:
                    logger.warning(
                        "Grid import total read failed on attempt %s/%s for %s",
                        attempt + 1,
                        max_retries,
                        ip,
                    )
                else:
                    # Success! Combine uint32 from two uint16 registers: value = (MSB << 16) | LSB
                    lsb = registers[0]
                    msb = registers[1]
                    raw_value = (msb << 16) | lsb

                    # Convert from 0.01kWh units to kWh
                    import_kwh = raw_value * 0.01

                    logger.debug(
                        "Grid import total from %s: %.2f kWh (raw: %s, LSB: %s, MSB: %s) on attempt %s",
                        ip,
                        import_kwh,
                        raw_value,
                        lsb,
                        msb,
                        attempt + 1,
                    )
                    return import_kwh

        except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
            logger.warning(
                "Grid import total error on attempt %s/%s for %s: %s",
                attempt + 1,
                max_retries,
                ip,
                e,
            )

        finally:
            # Always close connection (connect → read → close → retry pattern)
            if client:
                try:
                    client.close()
                    logger.debug(
                        "Connection to %s closed after grid import total attempt %s",
                        ip,
                        attempt + 1,
                    )
                except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
                    logger.warning("Error closing connection to %s: %s", ip, e)

        # Retry timing logic - ALWAYS executed for failed attempts (connection OR read failures)
        if attempt < max_retries - 1:
            retry_delay = [1.0, 1.5, 2.0, 3.0][attempt]  # 1.0s, 1.5s, 2.0s, 3.0s
            logger.debug("Retrying grid import total read from %s in %ss...", ip, retry_delay)
            time.sleep(retry_delay)

    logger.error("All %s grid import total read attempts failed for %s", max_retries, ip)
    return None


# Bulk reading functions and data extraction


def _extract_input_register_data(massive_block: list[int]) -> dict[str, Any]:
    """Extract needed values from massive 79-register input block (0x0002-0x0050).

    REGISTER MAPPING DOCUMENTATION:
    This function extracts only the 13 values we need from a massive 79-register
    block read. This is the key to the performance optimization - we read many
    registers but only extract what we need.

    Register Layout in massive_block:
    - Index 0 (0x0002): AC Power
    - Index 7 (0x0009): Run Mode
    - Index 8 (0x000A): PV1 Power
    - Index 9 (0x000B): PV2 Power
    - Index 20 (0x0016): Battery Power
    - Index 22 (0x0018): Battery Temperature
    - Index 26 (0x001C): Battery SoC
    - Index 36 (0x0026): Battery Capacity LSB
    - Index 37 (0x0027): Battery Capacity MSB
    - Index 68 (0x0046): Grid Power (master only)
    - Index 70 (0x0048): Grid Export Total LSB
    - Index 71 (0x0049): Grid Export Total MSB
    - Index 72 (0x004A): Grid Import Total LSB
    - Index 73 (0x004B): Grid Import Total MSB
    - Index 78 (0x0050): Daily Yield

    Args:
        massive_block: List of 79 register values from 0x0002-0x0050

    Returns:
        Dictionary with extracted values using same structure as individual functions

    """
    if not massive_block or len(massive_block) < MASSIVE_BLOCK_EXPECTED_SIZE:
        logger.error(
            "Invalid massive block size: %s, expected 79",
            len(massive_block) if massive_block else 0,
        )
        return None

    try:
        # Import functions from main module for test compatibility
        from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            solax_modbus_client,  # pylint: disable=import-outside-toplevel
        )

        # Extract raw values using calculated offsets
        extracted = {
            "ac_power": massive_block[0],  # 0x0002 - offset 0
            "run_mode": massive_block[7],  # 0x0009 - offset 7
            "pv1_power": massive_block[8],  # 0x000A - offset 8
            "pv2_power": massive_block[9],  # 0x000B - offset 9
            "battery_power_raw": massive_block[20],  # 0x0016 - offset 20
            "battery_temperature_celsius": massive_block[22],  # 0x0018 - offset 22
            "battery_soc": massive_block[26],  # 0x001C - offset 26
            "battery_cap_lsb": massive_block[36],  # 0x0026 - offset 36
            "battery_cap_msb": massive_block[37],  # 0x0027 - offset 37
            "grid_power": massive_block[68],  # 0x0046 - offset 68
            "feedin_lsb": massive_block[70],  # 0x0048 - offset 70
            "feedin_msb": massive_block[71],  # 0x0049 - offset 71
            "consum_lsb": massive_block[72],  # 0x004A - offset 72
            "consum_msb": massive_block[73],  # 0x004B - offset 73
            "daily_yield": massive_block[78],  # 0x0050 - offset 78
        }

        # Enhanced logging: Raw register block values for diagnosis
        logger.debug(
            "RAW REGISTER VALUES: 0x0002=%s, 0x0016=%s, 0x0018=%s, 0x0046=%s, 0x001C=%s",
            massive_block[0],
            massive_block[20],
            massive_block[22],
            massive_block[68],
            massive_block[26],
        )

        # Process AC power using helper
        ac_power_value = extracted["ac_power"]
        extracted["ac_power"] = _convert_uint16_to_int16(ac_power_value)
        logger.debug(
            "AC power (0x0002): %sW (raw: %s) | signed_conversion: %s",
            extracted["ac_power"],
            ac_power_value,
            ac_power_value > INT16_MAX_SIGNED,
        )

        # Convert run mode to string using existing logic
        extracted["run_mode_str"] = solax_modbus_client._interpret_run_mode(extracted["run_mode"])  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API

        # Process battery power using helpers
        battery_power_value = extracted["battery_power_raw"]
        battery_power_watts = _convert_uint16_to_int16(battery_power_value)
        battery_mode = _interpret_battery_mode_from_power(battery_power_watts)

        extracted["battery_power"] = battery_power_watts
        extracted["battery_mode"] = battery_mode

        logger.debug(
            "Battery power (0x0016): %sW (raw: %s) | signed_conversion: %s | mode: %s",
            battery_power_watts,
            battery_power_value,
            battery_power_value > INT16_MAX_SIGNED,
            battery_mode_to_display_string(battery_mode),
        )

        # Process battery temperature (int16 signed, -20°C to +60°C)
        temperature_raw = extracted["battery_temperature_celsius"]
        temperature_celsius = _convert_uint16_to_int16(temperature_raw)
        extracted["battery_temperature_celsius"] = temperature_celsius

        logger.debug(
            "Battery temperature (0x0018): %s°C (raw: %s) | signed_conversion: %s",
            temperature_celsius,
            temperature_raw,
            temperature_raw > INT16_MAX_SIGNED,
        )

        # Validate temperature range and log warnings for concerning temperatures
        if temperature_celsius < TEMP_MIN_CELSIUS or temperature_celsius > TEMP_MAX_CELSIUS:
            logger.warning(
                "Battery temperature %s°C is outside valid range (%s°C to %s°C)",
                temperature_celsius,
                TEMP_MIN_CELSIUS,
                TEMP_MAX_CELSIUS,
            )
        elif temperature_celsius < TEMP_WARNING_LOW_CELSIUS:
            logger.warning(
                "Battery temperature %s°C is below freezing - charge rate will be severely limited",
                temperature_celsius,
            )
        elif temperature_celsius > TEMP_WARNING_HIGH_CELSIUS:
            logger.warning(
                "Battery temperature %s°C is high - thermal management may engage",
                temperature_celsius,
            )

        # Process battery capacity (same logic as _read_single_battery_capacity)
        capacity_raw = (extracted["battery_cap_msb"] << 16) | extracted["battery_cap_lsb"]
        extracted["battery_capacity_kwh"] = capacity_raw / 1000.0

        # Process grid power using helper
        grid_power_value = extracted["grid_power"]
        extracted["grid_power_watts"] = _convert_uint16_to_int16(grid_power_value)
        logger.debug(
            "Grid power (0x0046): %sW (raw: %s) | signed_conversion: %s",
            extracted["grid_power_watts"],
            grid_power_value,
            grid_power_value > INT16_MAX_SIGNED,
        )

        # Process daily yield (same logic as _read_single_daily_yield)
        extracted["daily_yield_kwh"] = extracted["daily_yield"] / 100.0

        # Process grid export total (same logic as _read_single_grid_export_total)
        # Combine uint32 from two uint16 registers: value = (MSB << 16) | LSB
        feedin_raw = (extracted["feedin_msb"] << 16) | extracted["feedin_lsb"]
        extracted["grid_export_total_kwh"] = feedin_raw * 0.01

        logger.debug(
            "Grid export total (0x0048-0x0049): %.2f kWh (raw: %s, LSB: %s, MSB: %s)",
            extracted["grid_export_total_kwh"],
            feedin_raw,
            extracted["feedin_lsb"],
            extracted["feedin_msb"],
        )

        # Process grid import total (same logic as _read_single_grid_import_total)
        # Combine uint32 from two uint16 registers: value = (MSB << 16) | LSB
        consum_raw = (extracted["consum_msb"] << 16) | extracted["consum_lsb"]
        extracted["grid_import_total_kwh"] = consum_raw * 0.01

        logger.debug(
            "Grid import total (0x004A-0x004B): %.2f kWh (raw: %s, LSB: %s, MSB: %s)",
            extracted["grid_import_total_kwh"],
            consum_raw,
            extracted["consum_lsb"],
            extracted["consum_msb"],
        )

        # Validate physical limits using helper
        power_values = [
            ("AC Power (0x0002)", extracted["ac_power"] / 1000.0),
            ("Battery Power", extracted["battery_power"] / 1000.0),
            ("Grid Power (0x0046)", extracted["grid_power_watts"] / 1000.0),
        ]
        _validate_power_physical_limits(power_values)

    except Exception:  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
        logger.exception("Error extracting data from massive block")
        return None
    return extracted


def _retry_register_read_with_reconnection(  # pylint: disable=too-many-positional-arguments  # Hardware interface pattern
    ip: str,
    port: int,
    timeout: int,
    read_func: Callable[[object], list[int] | None],
    operation_name: str,
    client: object | None = None,
    min_interval: float = 0.1,
    max_retries: int = 3,
) -> tuple[list[int] | None, object | None]:
    """Retry register reads with connection recreation on failure.

    This helper encapsulates the retry logic with connection management pattern:
    - Keep connection open between successful reads (efficient)
    - Close connection only on failure (for retry with new connection)
    - Caller should close connection when done with all operations

    RETRY TIMING: 1.0s, 1.5s, 3.0s progression between retry attempts

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        read_func: Callable that takes client and returns register data
        operation_name: Description of operation for logging
        client: Existing client to reuse (optional, enables connection reuse)
        min_interval: Minimum interval between commands (default: 0.1s)
        max_retries: Maximum number of retry attempts (default: 3)

    Returns:
        Tuple of (register_data, client) where:
        - register_data: List of register values, or None if all attempts fail
        - client: Active client connection (may be same as input or new), or None on failure

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    for attempt in range(max_retries):
        try:
            # If we don't have a client, create one
            if not client:
                client = solax_modbus_client._connect_modbus_client(ip, port, timeout)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                if not client:
                    logger.warning(
                        "Connection failed for %s attempt %s/%s to %s",
                        operation_name,
                        attempt + 1,
                        max_retries,
                        ip,
                    )
                    continue

            # Wait before reading (existing pattern)
            time.sleep(min_interval)

            # Perform the register read
            result = read_func(client)
            if result:
                logger.debug("%s succeeded on attempt %s for %s", operation_name, attempt + 1, ip)
                # Return result AND client to keep connection alive
                return result, client
            logger.warning(
                "%s failed on attempt %s/%s for %s",
                operation_name,
                attempt + 1,
                max_retries,
                ip,
            )

        except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
            logger.warning(
                "%s error on attempt %s/%s for %s: %s",
                operation_name,
                attempt + 1,
                max_retries,
                ip,
                e,
            )

        # Close connection on failure (requirement: close and recreate for retry)
        if client:
            try:
                client.close()
                logger.debug(
                    "Connection to %s closed after %s failure attempt %s",
                    ip,
                    operation_name,
                    attempt + 1,
                )
            except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
                logger.warning("Error closing connection to %s: %s", ip, e)
            client = None

        # Retry timing logic (1s, 1.5s, 3s progression)
        if attempt < max_retries - 1:
            retry_delay = [1.0, 1.5, 3.0][attempt]  # 1.0s, 1.5s, 3.0s
            logger.debug("Retrying %s to %s in %ss...", operation_name, ip, retry_delay)
            time.sleep(retry_delay)

    logger.error("All %s attempts failed for %s on %s", max_retries, operation_name, ip)
    return None, None


def _process_bulk_register_data(
    input_block: list[int],
    serial_block: list[int],
    timestamp_work_block: list[int],
) -> dict[str, Any] | None:
    """Process all register blocks into formatted inverter data.

    Args:
        input_block: Input registers (power, SoC, yield data)
        serial_block: Holding registers (serial number)
        timestamp_work_block: Holding registers (RTC timestamp + work mode)

    Returns:
        Dictionary containing all inverter data or None if processing fails

    """
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    # Extract and process input register data
    extracted_data = _extract_input_register_data(input_block)
    if not extracted_data:
        logger.error("Failed to extract input register data")
        return None

    # Extract and add serial number
    serial_number = solax_modbus_client._format_serial_number(serial_block)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
    extracted_data["serial_number"] = serial_number

    # Extract and add RTC timestamp (first 6 registers)
    rtc_timestamp = solax_modbus_client._format_rtc_timestamp(timestamp_work_block[:6])  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
    extracted_data["rtc_timestamp"] = rtc_timestamp

    # Extract and add work mode data using helper
    work_mode_registers = timestamp_work_block[6:8]  # registers 0x008B-0x008C
    work_mode_raw = work_mode_registers[0]  # 0x008B
    manual_mode_raw = work_mode_registers[1]  # 0x008C

    work_mode_enum = _interpret_work_mode(work_mode_raw, manual_mode_raw)

    extracted_data["work_mode"] = work_mode_enum
    extracted_data["manual_mode"] = manual_mode_raw

    logger.debug("Successfully extracted and processed all bulk data")
    return extracted_data


def _read_bulk_input_data_single_inverter(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> dict[str, Any] | None:
    """Read comprehensive bulk data from a single inverter with granular retry logic.

    ENHANCED RELIABILITY WITH GRANULAR RETRIES:
    This function implements retry logic for each of the 3 register reads individually,
    maintaining connection efficiency while maximizing data recovery success:

    1. Input registers 0x0002-0x0050 (79 registers) - power, SoC, yield data [3 retries]
    2. Holding registers 0x0000-0x0006 (7 registers) - serial number [3 retries]
    3. Holding registers 0x0085-0x008C (8 registers) - RTC timestamp + work mode [3 retries]

    CONNECTION STRATEGY:
    - Keep connection open between successful reads (efficient)
    - Close connection only on failure (for retry with new connection)
    - Always close connection at end of process (guaranteed cleanup)

    RETRY TIMING: 1.0s, 1.5s, 3.0s progression between retry attempts

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum interval between commands

    Returns:
        Dictionary containing all inverter data including serial number and timestamp,
        or None if error occurs after all retries

    """
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    client = None

    try:
        logger.debug("Starting enhanced bulk data read from %s with granular retries", ip)

        # Read 1: Input register block (with retries)
        def read_input_block(client: object) -> list[int] | None:
            return solax_modbus_client._read_input_registers(client, 0x0002, 79, slave_address)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API

        input_block, client = _retry_register_read_with_reconnection(
            ip, port, timeout, read_input_block, "Input register block", client, min_interval
        )
        if not input_block:
            return None

        # Read 2: Serial number block (with retries, keeping connection open from successful Read 1)
        def read_serial_block(client: object) -> list[int] | None:
            return solax_modbus_client._read_holding_registers(client, 0x0000, 7, slave_address)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API

        serial_block, client = _retry_register_read_with_reconnection(
            ip, port, timeout, read_serial_block, "Serial number block", client, min_interval
        )
        if not serial_block:
            return None

        # Read 3: Timestamp+work mode block (with retries, keeping connection open from successful Read 2)
        def read_timestamp_work_block(client: object) -> list[int] | None:
            return solax_modbus_client._read_holding_registers(client, 0x0085, 8, slave_address)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API

        timestamp_work_block, client = _retry_register_read_with_reconnection(
            ip,
            port,
            timeout,
            read_timestamp_work_block,
            "Timestamp+work mode block",
            client,
            min_interval,
        )
        if not timestamp_work_block:
            return None

        logger.debug("Successfully read all bulk data blocks")

        # Process all register data into final format
        return _process_bulk_register_data(input_block, serial_block, timestamp_work_block)

    except Exception:  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
        logger.exception("Unexpected error in bulk data read")
        return None

    finally:
        if client:
            client.close()


def _read_bulk_work_mode_data(
    ip: str, port: int, timeout: int, slave_address: int, min_interval: float
) -> dict[str, Any] | None:
    """Read work mode data using optimized block read with retry logic.

    ENHANCED RELIABILITY: Implements 4 total attempts (1 + 3 retries) with
    connection recreation on failure and proper 1.0s, 1.5s, 2.0s, 3.0s retry timing.

    PERFORMANCE OPTIMIZATION DETAILS:
    Instead of 2 separate modbus operations for work mode registers,
    this function reads registers 0x008B-0x008C (2 registers) in a
    single modbus operation.

    Performance Impact:
    - Individual approach: 2 operations × 1s = 2 seconds
    - This approach: 1 operation × ~0.05s = 0.05 seconds
    - Improvement: 97.5% faster (40x speedup for work mode)

    Args:
        ip: IP address of the inverter WiFi dongle
        port: TCP port for Modbus communication
        timeout: Connection timeout in seconds
        slave_address: Modbus slave address of the inverter
        min_interval: Minimum interval between commands

    Returns:
        Dictionary containing work mode data, or None if all attempts fail

    """  # noqa: RUF002
    # Import functions from main module for test compatibility
    from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
        solax_modbus_client,  # pylint: disable=import-outside-toplevel
    )

    # Retry configuration: 4 total attempts (1 + 3 retries) with exponential backoff
    retry_delays = [1.0, 1.5, 2.0, 3.0]  # seconds between retry attempts
    max_attempts = len(retry_delays)

    for attempt in range(max_attempts):
        client = None

        try:
            # Connect to inverter (new connection each attempt)
            client = solax_modbus_client._connect_modbus_client(ip, port, timeout)  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
            if not client:
                if attempt < max_attempts - 1:
                    logger.warning(
                        "Work mode connection failed on attempt %s/%s to %s, retrying in %ss",
                        attempt + 1,
                        max_attempts,
                        ip,
                        retry_delays[attempt],
                    )
                    time.sleep(retry_delays[attempt])
                else:
                    logger.error("All work mode connection attempts failed to %s", ip)
                    return None
            else:
                # Connection successful, try the read
                time.sleep(min_interval)

                logger.debug("Reading work mode block: 2 registers from 0x008B-0x008C")

                # Read work mode register block: 0x008B to 0x008C (2 registers)
                work_block = solax_modbus_client._read_holding_registers(  # noqa: SLF001  # pylint: disable=protected-access  # Internal package API
                    client, 0x008B, 2, slave_address
                )

                if not work_block or len(work_block) != WORK_MODE_REGISTER_COUNT:
                    if attempt < max_attempts - 1:
                        logger.warning(
                            "Work mode register read failed on attempt %s/%s for %s, retrying in %ss",
                            attempt + 1,
                            max_attempts,
                            ip,
                            retry_delays[attempt],
                        )
                        time.sleep(retry_delays[attempt])
                    else:
                        logger.error("All work mode register read attempts failed for %s", ip)
                        return None
                else:
                    logger.debug("Successfully read work mode block: %s", work_block)

                    # Extract work mode values
                    work_mode_raw = work_block[0]
                    manual_mode_raw = work_block[1]

                    # Process work mode using helper
                    work_mode_enum = _interpret_work_mode(work_mode_raw, manual_mode_raw)

                    return {
                        "work_mode_raw": work_mode_raw,
                        "manual_mode_raw": manual_mode_raw,
                        "work_mode": work_mode_enum,
                    }

        except (  # pylint: disable=broad-exception-caught  # Hardware can fail unpredictably
            Exception
        ) as e:
            if attempt < max_attempts - 1:
                logger.warning(
                    "Error reading work mode from %s (attempt %s/%s): %s, retrying in %ss",
                    ip,
                    attempt + 1,
                    max_attempts,
                    e,
                    retry_delays[attempt],
                )
                time.sleep(retry_delays[attempt])
            else:
                logger.exception("All attempts failed reading work mode from %s", ip)
                return None

        finally:
            # Always close connection
            if client:
                try:
                    client.close()
                    logger.debug("Connection to %s closed", ip)
                except Exception as e:  # noqa: BLE001  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
                    logger.warning("Error closing connection to %s: %s", ip, e)

    # This should never be reached due to explicit returns above
    return None  # pragma: no cover  # pragma: no cover  # pragma: no cover  # pragma: no cover


def _parallel_bulk_read_both_inverters(config: dict[str, Any]) -> dict[str, Any] | None:
    """Read bulk data from both inverters in parallel using ThreadPoolExecutor.

    PERFORMANCE OPTIMIZATION DETAILS:
    Uses parallel execution to read master and slave inverters simultaneously,
    reducing total time from sum of both reads to max of both reads.

    ULTIMATE OPTIMIZATION - All data in 2 parallel bulk reads:
    - Master inverter: 3 block reads (input + serial + timestamp/work mode)
    - Slave inverter: 3 block reads (input + serial + timestamp)
    - Total operations: 6 (down from 15+ individual operations)
    - Parallel execution: max(master_time, slave_time) = ~3.0 seconds

    Combined with individual->bulk optimization:
    - Original individual sequential: ~24 seconds (12s master + 12s slave)
    - Original individual parallel: ~12 seconds (max of both)
    - New bulk parallel: ~3.0 seconds
    - Total improvement: 75% faster than parallel individual, 87% faster than sequential

    Args:
        config: Configuration dictionary containing solaX_cloud_api section

    Returns:
        Combined data from both inverters, or None if critical errors occur

    """
    try:
        # Extract configuration parameters
        solax_config = config.get("solaX_cloud_api", {})

        master_ip = solax_config.get("master_ip")
        slave_ip = solax_config.get("slave_ip")
        port = solax_config.get("modbus_port", 502)
        connection_timeout = solax_config.get("modbus_connection_timeout", 10)
        master_address = solax_config.get("master_modbus_address", 1)
        slave_address = solax_config.get("slave_modbus_address", 2)
        min_interval = solax_config.get("min_command_interval", 1.0)

        # Validate IP addresses
        if not master_ip or master_ip == "YOUR_IP_HERE":
            logger.error("Master inverter IP address not configured")
            return None

        if not slave_ip or slave_ip == "YOUR_IP_HERE":
            logger.error("Slave inverter IP address not configured")
            return None

        logger.info("Starting parallel bulk data read from both inverters")

        # Read both inverters in parallel
        with ThreadPoolExecutor(max_workers=2) as executor:
            # Submit both inverter reads simultaneously
            master_future = executor.submit(
                _read_bulk_input_data_single_inverter,
                master_ip,
                port,
                connection_timeout,
                master_address,
                min_interval,
            )
            slave_future = executor.submit(
                _read_bulk_input_data_single_inverter,
                slave_ip,
                port,
                connection_timeout,
                slave_address,
                min_interval,
            )

            # Wait for both to complete
            master_data = master_future.result()
            slave_data = slave_future.result()

        if not master_data:
            logger.error("Failed to read bulk data from master inverter")
            return None

        if not slave_data:
            logger.warning("Failed to read bulk data from slave inverter, using partial data")
            # Create empty slave data structure to prevent crashes
            slave_data = {
                "ac_power": 0,
                "run_mode_str": "Unknown",
                "pv1_power": 0,
                "pv2_power": 0,
                "battery_power": 0,
                "battery_mode": "Unknown",
                "battery_soc": 0,
                "battery_capacity_kwh": 0,
                "grid_power_watts": 0,  # Only master has real grid power
                "daily_yield_kwh": 0,
                "serial_number": "Unknown",
                "rtc_timestamp": "Unknown",
            }

        # Extract work mode from master inverter data (already read in bulk)
        logger.info("Extracting system work mode from master bulk data")
        work_mode_data = {
            "work_mode": master_data.get(
                "work_mode", None
            )  # BatteryMode enum or None - consistent type
        }

        # Extract serial numbers and timestamps from the already-read bulk data
        logger.info("Extracting serial numbers and timestamps from bulk data")

        # Serial numbers and timestamps are now included in the bulk data
        serial_numbers = {
            "master": master_data.get("serial_number", "Unknown"),
            "slave": slave_data.get("serial_number", "Unknown"),
        }

        timestamps = {
            "master": master_data.get("rtc_timestamp", "Unknown"),
            "slave": slave_data.get("rtc_timestamp", "Unknown"),
        }

        logger.info("Successfully completed parallel bulk data read")

    except Exception:  # Hardware can fail unpredictably  # pylint: disable=broad-exception-caught
        logger.exception("Error in parallel bulk read")
        return None

    return {
        "master_data": master_data,
        "slave_data": slave_data,
        "work_mode_data": work_mode_data,
        "serial_numbers": serial_numbers,
        "timestamps": timestamps,
    }
