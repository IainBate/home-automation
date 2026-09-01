"""Unit tests for src/api_clients/_modbus_data_maps.py - pure register decoding.

No Modbus I/O, no mocking - every function here is a plain register-list-in,
value-out transform, so these are pinned down with plain input/output cases.
"""

from __future__ import annotations

import pytest

from src.api_clients import _modbus_data_maps as data_maps
from src.core_logic.battery_simulation import BatteryMode


def _registers_from_string(serial: str) -> list[int]:
    """Pack a 14-char string into 7 registers the way SolaX does (2 chars/register)."""
    assert len(serial) == 14
    return [
        (ord(serial[i]) << 8) | ord(serial[i + 1])
        for i in range(0, len(serial), 2)
    ]


def test_format_serial_number_valid():
    registers = _registers_from_string("SOLAX1234567AB")
    assert data_maps._format_serial_number(registers) == "SOLAX1234567AB"


def test_format_serial_number_skips_null_bytes():
    # High byte 0 ('\x00') should be skipped, contributing only the low byte -
    # still needs exactly 7 registers to pass the length check.
    registers = [0x0041, 0x0042, 0, 0, 0, 0, 0]  # -> "A", "B", then all-null registers
    assert data_maps._format_serial_number(registers) == "AB"


@pytest.mark.parametrize("registers", [[], None, [1, 2, 3]])
def test_format_serial_number_wrong_length_is_error(registers):
    assert data_maps._format_serial_number(registers) == "ERROR"


def test_format_rtc_timestamp_valid():
    # seconds, minutes, hours, days, months, years_since_2000
    registers = [45, 30, 14, 15, 6, 26]
    assert data_maps._format_rtc_timestamp(registers) == "2026-06-15 14:30:45"


@pytest.mark.parametrize("registers", [[], None, [1, 2, 3]])
def test_format_rtc_timestamp_wrong_length_is_error(registers):
    assert data_maps._format_rtc_timestamp(registers) == "ERROR"


@pytest.mark.parametrize(
    "registers",
    [
        [60, 0, 0, 1, 1, 0],  # seconds out of range (max 59)
        [0, 60, 0, 1, 1, 0],  # minutes out of range
        [0, 0, 24, 1, 1, 0],  # hours out of range (max 23)
        [0, 0, 0, 0, 1, 0],  # days out of range (min 1)
        [0, 0, 0, 32, 1, 0],  # days out of range (max 31)
        [0, 0, 0, 1, 0, 0],  # months out of range (min 1)
        [0, 0, 0, 1, 13, 0],  # months out of range (max 12)
    ],
)
def test_format_rtc_timestamp_out_of_range_is_error(registers):
    assert data_maps._format_rtc_timestamp(registers) == "ERROR"


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (0, "Waiting"),
        (2, "Normal Mode"),
        (9, "Idle Mode"),
        (21, "TOU Self Use"),
        (25, "TOU Peak Shaving"),
    ],
)
def test_interpret_run_mode_known_values(raw_value, expected):
    assert data_maps._interpret_run_mode(raw_value) == expected


def test_interpret_run_mode_unknown_value():
    assert data_maps._interpret_run_mode(15) == "Unknown Mode (15)"


@pytest.mark.parametrize(
    ("work_mode", "expected"),
    [
        (0, BatteryMode.SELF_USE),
        (1, BatteryMode.FEED_IN_PRIORITY),
        (2, BatteryMode.BACKUP),
        (4, BatteryMode.PEAK_SHAVING),
        (5, BatteryMode.TOU_MODE),
        (6, BatteryMode.SMART_SCHEDULE),
    ],
)
def test_interpret_work_mode_known_values(work_mode, expected):
    assert data_maps._interpret_work_mode(work_mode) == expected


def test_interpret_work_mode_manual_falls_back_to_self_use():
    """Work mode 3 (manual) needs the second register - this function alone defaults to SELF_USE."""
    assert data_maps._interpret_work_mode(3) == BatteryMode.SELF_USE


def test_interpret_work_mode_unknown_value():
    assert data_maps._interpret_work_mode(99) == BatteryMode.UNKNOWN_WORK_MODE


@pytest.mark.parametrize(
    ("manual_mode", "expected"),
    [
        (0, BatteryMode.MANUAL_STOP),
        (1, BatteryMode.FORCE_CHARGE),
        (2, BatteryMode.FORCE_DISCHARGE),
    ],
)
def test_interpret_manual_mode_known_values(manual_mode, expected):
    assert data_maps._interpret_manual_mode(manual_mode) == expected


def test_interpret_manual_mode_unknown_value_raises():
    with pytest.raises(ValueError, match="Unknown manual mode register value: 5"):
        data_maps._interpret_manual_mode(5)
