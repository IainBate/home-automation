"""Light tests for a representative subset of scripts/ohme_ev_control.py's
pure formatting helpers - all take a plain status dict, no I/O. Not
exhaustive (the CLI's four raw-diagnostic handlers are deliberately excluded
from automated coverage entirely - see the test plan).
"""

from __future__ import annotations

from ohme_ev_control import (
    _format_battery_line,
    _format_connection_status,
    _format_power_line,
    _format_target_line,
    _get_status_emoji,
)
from src.api_clients.ohme_ev_client import OhmeChargerStatus


def test_get_status_emoji_known_and_unknown():
    assert _get_status_emoji(OhmeChargerStatus.CHARGING) == "[CHG]"
    assert _get_status_emoji(OhmeChargerStatus.UNKNOWN) == "[?]"


def test_format_power_line_with_voltage_and_ct_clamp():
    status = {
        "power_watts": 7300,
        "power_amps": 32.0,
        "power_volts": 230,
        "ct_connected": True,
        "ct_amps": 12.5,
    }
    line = _format_power_line(status)
    assert line == "Power: 7300W (32.0A @ 230V) | CT Clamp: 12.5A"


def test_format_power_line_without_voltage_or_ct_clamp():
    status = {"power_watts": 0, "power_amps": 0.0, "power_volts": None, "ct_connected": False}
    line = _format_power_line(status)
    assert line == "Power: 0W (0.0A)"


def test_format_battery_line_with_energy():
    line = _format_battery_line({"battery_percent": 80, "energy_wh": 1500.0})
    assert line == "Battery: 80% | Energy: 1500.0Wh (1.50kWh)"


def test_format_battery_line_without_energy():
    line = _format_battery_line({"battery_percent": 80, "energy_wh": 0})
    assert line == "Battery: 80%"


def test_format_target_line_with_time():
    line = _format_target_line({"target_soc": 80, "target_time": (7, 30)})
    assert line == "Target: 80% by 07:30"


def test_format_target_line_without_time():
    line = _format_target_line({"target_soc": 80, "target_time": None})
    assert line == "Target: 80%"


def test_format_connection_status_online_and_plugged_in():
    line = _format_connection_status({"online": True, "plugged_in": True})
    assert line == "Charger Online | Cable Plugged In"


def test_format_connection_status_offline_and_unplugged():
    line = _format_connection_status({"online": False, "plugged_in": False})
    assert line == "Charger Offline | Cable Unplugged"


def test_format_connection_status_unknown():
    line = _format_connection_status({})
    assert line == "Charger Status Unknown"
