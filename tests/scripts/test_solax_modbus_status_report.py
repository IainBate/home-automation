"""Light tests for scripts/solax_modbus_status_report.py's calculate_system_overview -
pure aggregation over an already-fetched data dict, no I/O.
"""

from __future__ import annotations

from solax_modbus_status_report import calculate_system_overview


def test_full_data_produces_all_aggregates():
    data = {
        "pv_power": {"master": {"pv1": 1000, "pv2": 500}, "slave": {"pv1": 800, "pv2": 200}},
        "ac_power": {"master": 2000, "slave": 1500},
        "grid_power": {"master": 300},
        "battery_power": {"master": {"power": 500}, "slave": {"power": -200}},
        "soc": {"master": 60, "slave": 80},
        "daily_yield": {"master": 10.5, "slave": 8.5},
        "battery_capacity": {"master": 10.0, "slave": 10.0},
    }

    overview = calculate_system_overview(data)

    assert overview["total_pv_power"] == 2500
    assert overview["total_ac_power"] == 3500
    assert overview["grid_power"] == 300
    assert overview["total_battery_power"] == 300
    assert overview["average_soc"] == 70
    assert overview["total_daily_yield"] == 19.0
    assert overview["total_battery_capacity"] == 20.0
    assert overview["current_energy_stored"] == (70 / 100) * 20.0


def test_missing_sections_are_gracefully_omitted():
    overview = calculate_system_overview({})
    assert overview == {}


def test_partial_data_only_computes_available_aggregates():
    overview = calculate_system_overview({"ac_power": {"master": 1000, "slave": 0}})
    assert overview == {"total_ac_power": 1000}


def test_current_energy_stored_requires_both_soc_and_capacity():
    overview = calculate_system_overview({"soc": {"master": 50, "slave": 50}})
    assert "average_soc" in overview
    assert "current_energy_stored" not in overview
