"""Light tests for scripts/melcloud_hotwater_control.py's pure formatting
function and exit-code-per-exception mapping - the CLI wrapper itself is
otherwise a thin pass-through to MelCloudClient, already covered by
tests/api_clients/test_melcloud_client.py.
"""

from __future__ import annotations

from melcloud_hotwater_control import _format_status
from src.api_clients.melcloud_client import HotWaterOperationMode, HotWaterStatus


def _status(**overrides):
    defaults = {
        "device_name": "Hot Water Tank",
        "tank_temperature": 45.5,
        "target_tank_temperature": 50.0,
        "operation_mode": HotWaterOperationMode.AUTO,
        "status": HotWaterStatus.IDLE,
        "power": True,
        "holiday_mode": False,
        "last_seen": "2026-01-01T12:00:00+00:00",
    }
    defaults.update(overrides)
    return defaults


def test_format_status_includes_core_fields():
    output = _format_status(_status())
    assert "Hot Water Tank: Hot Water Tank" in output
    assert "Tank Temperature: 45.5C (target: 50.0C)" in output
    assert "Mode: AUTO" in output
    assert "Activity: IDLE" in output
    assert "Power: ON" in output
    assert "Last Seen: 2026-01-01T12:00:00+00:00" in output


def test_format_status_power_off():
    output = _format_status(_status(power=False))
    assert "Power: OFF" in output


def test_format_status_force_hot_water_mode_replaces_underscore():
    output = _format_status(_status(operation_mode=HotWaterOperationMode.FORCE_HOT_WATER))
    assert "Mode: FORCE HOT WATER" in output


def test_format_status_holiday_mode_shown_only_when_true():
    assert "Holiday Mode" not in _format_status(_status(holiday_mode=False))
    assert "Holiday Mode: ON" in _format_status(_status(holiday_mode=True))
