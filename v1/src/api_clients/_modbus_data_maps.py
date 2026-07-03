"""Modbus Data Mapping and Formatting Module.

Handles data conversion and formatting between raw Modbus register values
and human-readable formats or internal data models.

INTERNAL MODULE: Only for use by solax_modbus_client.py and related modules.
"""
# pylint: disable=cyclic-import
# Justification: Intentional modbus client split architecture. This helper module
# is imported by solax_modbus_client.py and uses type hints from it. The cycle
# is resolved at runtime and doesn't cause actual circular dependency issues.

import logging

from src.core_logic.battery_simulation import BatteryMode

# Serial number register constants
SERIAL_NUMBER_REGISTER_COUNT = 7

# RTC timestamp register constants
RTC_REGISTER_COUNT = 6
MAX_SECONDS = 59
MAX_MINUTES = 59
MAX_HOURS = 23
MAX_DAYS = 31
MAX_MONTHS = 12
MAX_YEARS_OFFSET = 255  # Years since 2000

# Work mode constants
WORK_MODE_MANUAL = 3  # Manual mode requires both registers


# Setup basic logging - use main module logger when available for test compatibility
def _get_logger() -> logging.Logger:
    """Get logger from main module if available, otherwise use local logger."""
    try:
        from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            solax_modbus_client,  # pylint: disable=import-outside-toplevel
        )
    except (
        ImportError,
        AttributeError,
    ):  # pragma: no cover - Defensive: modbus package structure prevents this
        # Fallback to local logger if main module not available or no logger attribute
        return logging.getLogger(__name__)
    return solax_modbus_client.logger


# Don't create static logger - get it dynamically for test compatibility


def _format_serial_number(registers: list[int]) -> str:
    """Convert 7 uint16 registers to 14-character serial number string.

    SolaX stores the serial number as 14 ASCII characters across 7 16-bit registers.
    Each register contains 2 characters (high byte, low byte).

    Args:
        registers: List of 7 uint16 register values

    Returns:
        Formatted serial number string or "ERROR" if invalid

    """
    try:
        if not registers or len(registers) != SERIAL_NUMBER_REGISTER_COUNT:
            _get_logger().error(
                "Expected %s registers for serial number, got %s",
                SERIAL_NUMBER_REGISTER_COUNT,
                len(registers) if registers else 0,
            )
            return "ERROR"

        serial_chars = []

        for i, register in enumerate(registers):
            # Extract high and low bytes from 16-bit register
            high_byte = (register >> 8) & 0xFF
            low_byte = register & 0xFF

            # Convert bytes to ASCII characters (skip null bytes)
            if high_byte > 0:
                serial_chars.append(chr(high_byte))
            if low_byte > 0:
                serial_chars.append(chr(low_byte))

            _get_logger().debug(
                "Register %s: 0x%04X -> '%s' + '%s'",
                i,
                register,
                chr(high_byte) if high_byte > 0 else "",
                chr(low_byte) if low_byte > 0 else "",
            )

        serial_number = "".join(serial_chars).strip()

        if not serial_number:
            _get_logger().error("Formatted serial number is empty")
            return "ERROR"

        _get_logger().debug("Formatted serial number: '%s'", serial_number)
    except (ValueError, OverflowError) as e:  # pragma: no cover - Defensive: corrupt hardware data
        # ValueError: chr() arg not in valid range
        # OverflowError: register value manipulation errors
        _get_logger().error("Error formatting serial number: %s", e)
        return "ERROR"
    return serial_number


def _format_rtc_timestamp(registers: list[int]) -> str:
    """Convert 6 uint16 registers to formatted timestamp string.

    SolaX stores RTC timestamp as 6 16-bit registers containing:
    [Seconds, Minutes, Hours, Days, Months, Years_since_2000]

    Args:
        registers: List of 6 uint16 register values for RTC timestamp

    Returns:
        Formatted timestamp string in "YYYY-MM-DD HH:MM:SS" format or "ERROR" if invalid

    """
    try:
        if not registers or len(registers) != RTC_REGISTER_COUNT:
            _get_logger().error(
                "Expected %s registers for RTC timestamp, got %s",
                RTC_REGISTER_COUNT,
                len(registers) if registers else 0,
            )
            return "ERROR"

        # Extract timestamp components
        seconds = registers[0]
        minutes = registers[1]
        hours = registers[2]
        days = registers[3]
        months = registers[4]
        years = registers[5]

        # Validate ranges
        if not (
            0 <= seconds <= MAX_SECONDS
            and 0 <= minutes <= MAX_MINUTES
            and 0 <= hours <= MAX_HOURS
            and 1 <= days <= MAX_DAYS
            and 1 <= months <= MAX_MONTHS
            and 0 <= years <= MAX_YEARS_OFFSET
        ):
            _get_logger().warning("Invalid RTC values: %s", registers)
            return "ERROR"

        # Year is stored as offset from 2000
        actual_year = 2000 + years

        # Format timestamp
        timestamp = (
            f"{actual_year:04d}-{months:02d}-{days:02d} {hours:02d}:{minutes:02d}:{seconds:02d}"
        )
        _get_logger().debug("Formatted RTC timestamp: '%s' (raw: %s)", timestamp, registers)
    except (ValueError, IndexError) as e:
        # ValueError: Invalid format operations
        # IndexError: registers list too short
        _get_logger().error("Error formatting RTC timestamp: %s", e)
        return "ERROR"
    return timestamp


def _interpret_run_mode(raw_value: int) -> str:
    """Convert numeric run mode value to descriptive text.

    Based on SolaX inverter run mode mappings from Home Assistant integration
    and community research. Covers basic operating modes, extended modes,
    and TOU (Time-of-Use) modes.

    Args:
        raw_value: Raw numeric value from register 0x0009

    Returns:
        Descriptive text for the run mode

    """
    # SolaX Inverter Run Mode Mappings
    # Source: Home Assistant SolaX Modbus Integration
    # https://github.com/wills106/homeassistant-solax-modbus

    run_mode_map = {
        # Basic Operating Modes (0-10)
        0: "Waiting",
        1: "Checking",
        2: "Normal Mode",
        3: "Fault",
        4: "Permanent Fault Mode",
        5: "Update Mode",
        6: "Off-Grid Waiting",
        7: "Off-Grid",
        8: "Self Test",
        9: "Idle Mode",
        10: "Standby",
        # Extended Modes
        20: "Normal (R)",
        # TOU (Time-of-Use) Modes (21-25)
        21: "TOU Self Use",
        22: "TOU Charging",
        23: "TOU Discharging",
        24: "TOU Battery Hold",
        25: "TOU Peak Shaving",
    }

    # Return mapped value or unknown status with raw value
    if raw_value in run_mode_map:
        return run_mode_map[raw_value]
    _get_logger().warning("Unknown run mode value: %s", raw_value)
    return f"Unknown Mode ({raw_value})"


def _interpret_work_mode(work_mode: int) -> BatteryMode:
    """Convert work mode register value to BatteryMode enum.

    HARDWARE BOUNDARY FUNCTION: This is part of the ONLY location where numeric hardware
    values are converted to/from BatteryMode enum. This maintains the refactoring principle
    that BatteryMode conversions occur at exactly two boundary points:
    1. Hardware interface (THIS MODULE)
    2. User display output (battery_mode_to_display_string)

    Args:
        work_mode: Raw work mode value from register 0x008B

    Returns:
        BatteryMode enum based on work mode register

    Note:
        Work mode 3 (Manual) requires additional manual_mode register and should not be processed here

    """
    work_mode_map = {
        0: BatteryMode.SELF_USE,  # Self-Use (most common default)
        1: BatteryMode.FEED_IN_PRIORITY,  # Feed-in Priority
        2: BatteryMode.BACKUP,  # Backup
        3: None,  # Manual mode - requires manual_mode register
        4: BatteryMode.PEAK_SHAVING,  # Peak Shaving
        5: BatteryMode.TOU_MODE,  # TOU Mode
        6: BatteryMode.SMART_SCHEDULE,  # Smart Schedule
    }

    if work_mode in work_mode_map:
        if work_mode == WORK_MODE_MANUAL:
            # This should not be called for work mode 3 - manual mode requires both registers
            _get_logger().warning(
                "_interpret_work_mode called with work_mode=3, this should use manual mode register"
            )
            return BatteryMode.SELF_USE
        return work_mode_map[work_mode]
    _get_logger().warning(
        "Unknown work mode register value: %s, defaulting to UNKNOWN_WORK_MODE", work_mode
    )
    return BatteryMode.UNKNOWN_WORK_MODE


def _interpret_manual_mode(manual_mode: int) -> BatteryMode:
    """Convert manual mode register value directly to BatteryMode enum.

    Args:
        manual_mode: Raw manual mode value from register 0x008C

    Returns:
        BatteryMode enum based on manual mode register

    Raises:
        ValueError: If manual_mode is not a recognized value

    """
    manual_mode_map = {
        0: BatteryMode.MANUAL_STOP,  # Manual Stop - battery disconnected, PV+grid only
        1: BatteryMode.FORCE_CHARGE,  # Force Charge
        2: BatteryMode.FORCE_DISCHARGE,  # Force Discharge
    }

    if manual_mode in manual_mode_map:
        return manual_mode_map[manual_mode]
    msg = f"Unknown manual mode register value: {manual_mode}"
    _get_logger().warning(msg)
    raise ValueError(msg)
