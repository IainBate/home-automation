#!/usr/bin/env python3
"""Power Usage Analysis Script.

Compares different battery management strategies to determine savings made by
the current algorithm versus alternative approaches.

This script analyzes:
1. Standard approach (no battery control - all grid power)
2. Current algorithm (battery mode daemon with schedule + Ohme detection)
3. Proposed future algorithm from Word document

The analysis is done on a month-by-month basis and produces an HTML report.
Uses only standard library modules - no external dependencies required.

KEY ASSUMPTIONS:
- Octopus Intelligent Go tariff rates
- Car charging detected by sustained high load (>5kW for >10 minutes)
- Battery has 92% charge/discharge efficiency
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Add project root to path for src module access
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# =============================================================================
# CONFIGURATION AND CONSTANTS
# =============================================================================

# Octopus Energy Intelligent Go Tariff (UK) - 2024/2025 rates
# Intelligent Go has fixed off-peak rate for 4 hours overnight (typically 00:30-04:30)
INTELLIGENT_GO_RATES = {
    "off_peak": 0.07,     # Fixed off-peak rate during smart charge window
    "day": 0.25,          # Day rate (all other times when not charging)
    "export": 0.15,       # Export rate
}

# Battery Configuration - from config.yaml
BATTERY_CONFIG = {
    "total_capacity_kwh": 23.04,
    "usable_capacity_kwh": 20.0,
    "charge_efficiency": 0.92,
    "discharge_efficiency": 0.92,
    "max_charge_rate_kw": 12.5,
    "max_discharge_rate_kw": 12.5,
}

# Household Load Profile - from config.yaml
HOUSEHOLD_LOAD = {
    "base_daytime_kw": 0.9,       # Base load during day (from config: base_load_daytime_kw)
    "base_nighttime_kw": 0.4,     # Base load during night (from config: base_load_nighttime_kw)
}

# Car Charging Configuration - from config.yaml
CAR_CHARGING = {
    "charger_demand_kw": 7.3,     # Ohme Home Pro max rate
}


def get_octopus_intelligent_go_rate(timestamp: datetime) -> Tuple[str, float]:
    """Get the current Octopus Intelligent Go rate for a timestamp.

    Intelligent Go has:
    - Fixed off-peak window (typically 4 hours starting at 00:30 or later)
    - Day rate for all other times
    """
    hour = timestamp.hour

    # Typical Intelligent Go off-peak is 00:30-04:30 (4 hours)
    # This can vary based on your specific tariff安排
    off_peak_start = 0  # 00:30 would be hour 0, minute 30
    off_peak_end = 5

    if off_peak_start <= hour < off_peak_end:
        return "off_peak", INTELLIGENT_GO_RATES["off_peak"]

    return "day", INTELLIGENT_GO_RATES["day"]


def calculate_cost(grid_power_kw: float, timestamp: datetime) -> Tuple[float, str]:
    """Calculate the cost/credit of grid power using Intelligent Go rates.

    Returns cost for a 5-minute interval (kWh consumed).
    """
    rate_period, rate_value = get_octopus_intelligent_go_rate(timestamp)
    time_interval_hours = 5 / 60  # 5 minutes = 1/12 hour

    if grid_power_kw >= 0:
        # Import: kWh × GBP/kWh = GBP
        return grid_power_kw * rate_value * time_interval_hours, rate_period
    else:
        # Export: kWh × GBP/kWh credit = credit
        credit = abs(grid_power_kw) * INTELLIGENT_GO_RATES["export"] * time_interval_hours
        return -credit, "export"


def detect_car_charging_periods(data: List[Dict], min_duration_minutes: int = 10,
                                 min_power_kw: float = 5.0) -> List[Tuple[datetime, datetime]]:
    """Detect car charging periods based on sustained high load.

    Car charging typically shows as:
    - Sustained power consumption > 5kW for > 10 minutes
    - Often during evening hours when returning home

    Detection logic:
    - High load (> min_power_kw) for extended period suggests EV charging
    - Battery is often charging during car charging (positive battery_power_kw)
      because the charger draws from solar/grid and charges battery too

    Args:
        data: List of cloud data points with load_power_kw
        min_duration_minutes: Minimum duration to consider as charging
        min_power_kw: Minimum power level to detect charging

    Returns:
        List of (start, end) tuples for detected charging periods
    """
    if not data:
        return []

    # Convert 5-minute data points to load values with timestamps
    load_points = []
    for entry in data:
        ts = entry.get("timestamp")
        load_kw = entry.get("load_power_kw", 0)
        battery_kw = entry.get("battery_power_kw", 0)

        if ts and load_kw is not None:
            try:
                # Handle both datetime objects (from parse_cloud_data) and strings
                if isinstance(ts, datetime):
                    dt = ts
                else:
                    dt = datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S")

                # Car charging detected when:
                # - Load > min_power_kw AND
                # - Battery is charging (positive battery_power) OR high grid export
                #   This indicates the charger is active and may be charging battery too
                load_val = float(load_kw)
                bat_val = float(battery_kw)

                # High load with positive or slightly negative battery suggests EV charging
                # (battery might be discharging slightly while car charges from solar)
                is_charging = (load_val >= min_power_kw and
                              (bat_val > -0.5))  # Battery not heavily discharging

                load_points.append({
                    "timestamp": dt,
                    "load_kw": load_val,
                    "battery_kw": bat_val,
                    "is_charging": is_charging
                })
            except (ValueError, TypeError):
                continue

    if not load_points:
        return []

    # Sort by timestamp
    load_points.sort(key=lambda x: x["timestamp"])

    # Find sustained high-load periods where car charging is likely
    charging_periods = []
    in_charging_period = False
    period_start = None

    for point in load_points:
        if point["is_charging"] and not in_charging_period:
            # Start a new potential charging period
            period_start = point["timestamp"]
            in_charging_period = True
        elif (not point["is_charging"]) and in_charging_period:
            # End current charging period - check duration
            period_end = point["timestamp"]
            duration_minutes = (period_end - period_start).total_seconds() / 60

            if duration_minutes >= min_duration_minutes:
                charging_periods.append((period_start, period_end))

            in_charging_period = False

    # Handle case where data ends while still charging
    if in_charging_period and load_points:
        period_end = load_points[-1]["timestamp"]
        duration_minutes = (period_end - period_start).total_seconds() / 60

        if duration_minutes >= min_duration_minutes:
            charging_periods.append((period_start, period_end))

    return charging_periods


def is_car_charging_at_time(timestamp: datetime, charging_periods: List[Tuple[datetime, datetime]]) -> bool:
    """Check if car was charging at a specific timestamp."""
    for start, end in charging_periods:
        if start <= timestamp < end:
            return True
    return False


def get_mode_at_time(timestamp: datetime, mode_changes: List[Dict]) -> str:
    """Determine which battery mode was active at a given timestamp."""
    if not mode_changes:
        return "SELF_USE"
    relevant = [m for m in mode_changes if m["timestamp"] <= timestamp]
    if not relevant:
        return "SELF_USE"
    return relevant[-1]["mode"]


def get_month(dt: datetime) -> str:
    """Get month string from datetime."""
    return dt.strftime("%Y-%m")


# =============================================================================
# STRATEGY SIMULATION
# =============================================================================

def simulate_standard_approach(
    data: List[Dict],
    charging_periods: List[Tuple[datetime, datetime]],
    household_load: Dict[str, float],
    car_charging: Dict[str, float],
) -> Tuple[List[Dict], Dict]:
    """Simulate standard approach: no battery control.

    In the standard approach without battery:
    - All grid power is imported at time-of-use rates
    - PV generation reduces import (or increases export)
    - Car charging adds to load during detected periods

    The key assumption: without battery, all energy comes from/to grid directly.
    """
    result = []
    total_cost = 0.0

    for entry in data:
        ts = entry["timestamp"]
        pv_power = entry.get("pv_power_kw", 0)
        # Use actual load from data as the baseline
        actual_load = entry.get("load_power_kw", household_load["base_daytime_kw"])

        # Check if car was charging at this time
        is_car_charging = is_car_charging_at_time(ts, charging_periods)

        # During car charging, add vehicle load to total
        if is_car_charging:
            total_load = actual_load + car_charging.get("charger_demand_kw", 7.3)
            car_energy_kwh = car_charging.get("charger_demand_kw", 7.3) * (5/60)
        else:
            total_load = actual_load
            car_energy_kwh = 0

        # Net power from grid perspective without battery
        # PV reduces import, negative means export
        net_power = pv_power - total_load

        if net_power >= 0:
            # Export surplus to grid at export rate
            cost, _ = calculate_cost(-net_power, ts)  # Negative for export
        else:
            # Import from grid at current rate
            import_kw = abs(net_power)
            cost, rate_period = calculate_cost(import_kw, ts)

        total_cost += cost

        result.append({
            "timestamp": ts,
            "grid_import_kw": max(0, net_power),
            "grid_export_kw": max(0, -net_power),
            "is_car_charging": is_car_charging,
            "car_energy_kwh": car_energy_kwh,
            "rate_period": get_octopus_intelligent_go_rate(ts)[0],
            "cost_pounds": cost,
        })

    return result, {"total_cost_pounds": total_cost}


def simulate_current_algorithm(
    data: List[Dict],
    mode_changes: List[Dict],
    charging_periods: List[Tuple[datetime, datetime]],
    battery_config: Dict[str, Any],
    household_load: Dict[str, float],
    car_charging: Dict[str, float],
) -> Tuple[List[Dict], Dict]:
    """Simulate current algorithm behavior (battery mode daemon).

    The simulation works by reconstructing what the battery was doing based on
    the actual mode change log and calculating costs for each 5-minute interval.

    Key insight: We use ACTUAL grid_power from data, then calculate:
    - What portion of that power is used for car charging vs household
    - How much battery charging/discharging occurred
    - Costs at the appropriate tariff rate
    """
    result = []
    total_cost = 0.0

    # Get battery parameters
    usable_capacity = battery_config.get("usable_capacity_kwh", 20.0)
    charge_eff = battery_config.get("charge_efficiency", 0.92)
    discharge_eff = battery_config.get("discharge_efficiency", 0.92)
    max_charge = battery_config.get("max_charge_rate_kw", 12.5)
    max_discharge = battery_config.get("max_discharge_rate_kw", 12.5)

    # Initialize battery state (start at ~80% SOC)
    soc_kwh = usable_capacity * 0.8

    # Battery and inverter impact tracking
    total_battery_cycles = 0.0
    total_battery_throughput_kwh = 0.0
    total_inverter_energy_kw_h = 0.0  # Inverter energy throughput (kW * hours)

    for entry in data:
        ts = entry["timestamp"]
        pv_power = entry.get("pv_power_kw", 0)
        # Note: SolaX Cloud API has inverted sign convention
        # grid_power_kw > 0 in data means EXPORT to grid (house is exporting)
        # grid_power_kw < 0 in data means IMPORT from grid (house is importing)
        # We invert here to use the standard convention:
        # positive = import FROM grid, negative = export TO grid
        actual_grid_power = -entry.get("grid_power_kw", 0)  # Inverted for correct sign convention
        battery_power = entry.get("battery_power_kw", 0)  # Positive = charging from grid/solar, Negative = discharging to load
        actual_load = entry.get("load_power_kw", household_load["base_daytime_kw"])

        # Determine mode at this timestamp
        mode = get_mode_at_time(ts, mode_changes)

        # Get rate period for cost calculation
        rate_period, rate_value = get_octopus_intelligent_go_rate(ts)

        # Check if car is charging
        is_car_charging = is_car_charging_at_time(ts, charging_periods)

        # Calculate actual grid energy (5 min intervals)
        grid_energy_kwh = actual_grid_power * (5/60)  # kWh for this interval
        battery_energy_kwh = battery_power * (5/60)

        if mode == "FORCE_CHARGE":
            # Battery is actively charging from whatever source available
            # Grid power during FORCE_CHARGE includes both household load and battery charging

            if actual_grid_power > 0:
                # Importing from grid - pay import rate for kWh
                cost = grid_energy_kwh * rate_value

                # Estimate how much went to battery (part of the import)
                # During FORCE_CHARGE, battery is actively charging
                battery_charge_energy = max(0, battery_energy_kwh) / charge_eff  # Account for efficiency
                soc_kwh += battery_charge_energy
                soc_kwh = min(soc_kwh, usable_capacity)

            elif actual_grid_power < 0:
                # Exporting - get export credit for kWh
                cost = grid_energy_kwh * INTELLIGENT_GO_RATES["export"]

        elif mode == "FORCE_DISCHARGE":
            # Battery is discharging to meet load or export
            if actual_grid_power > 0:
                # Importing from grid for load - pay for kWh
                cost = grid_energy_kwh * rate_value

                # Some power may have come from battery discharge
                # Estimate: battery supplied what wasn't covered by PV
                pv_to_load = min(pv_power, max(0, actual_load))
                battery_discharge_kw = max(0, actual_load - pv_power)
                battery_discharge_energy = battery_discharge_kw * (5/60) * discharge_eff
                soc_kwh -= battery_discharge_energy
                soc_kwh = max(soc_kwh, 0)

            elif actual_grid_power < 0:
                # Exporting surplus from PV + possibly battery - get credit for kWh
                cost = grid_energy_kwh * INTELLIGENT_GO_RATES["export"]

        else:  # SELF_USE or MANUAL_STOP
            # Self-use mode: use PV first, then battery if needed
            if actual_grid_power > 0:
                # Importing from grid - pay at current rate for kWh
                cost = grid_energy_kwh * rate_value

                # Battery may have discharged to reduce import
                if battery_energy_kwh < 0:
                    # Battery discharging - some of the load was covered by battery
                    battery_discharge_energy = abs(battery_energy_kwh) * discharge_eff
                    soc_kwh -= battery_discharge_energy
                    soc_kwh = max(soc_kwh, 0)

            elif actual_grid_power < 0:
                # Exporting surplus - get export credit for kWh
                cost = grid_energy_kwh * INTELLIGENT_GO_RATES["export"]

        total_cost += cost

        result.append({
            "timestamp": ts,
            "sim_grid_import_kw": max(0, actual_grid_power),
            "sim_grid_export_kw": max(0, -actual_grid_power),
            "is_car_charging": is_car_charging,
            "car_energy_kwh": car_charging.get("charger_demand_kw", 7.3) * (5/60) if is_car_charging else 0,
            "mode": mode,
            "rate_period": rate_period,
            "soc_kwh": soc_kwh,
            "sim_cost_pounds": cost,
        })

    # Calculate battery/inverter metrics for current algorithm
    total_battery_charge_kw_h = sum(max(0, e.get("battery_power_kw", 0)) * (5/60) for e in data)
    total_battery_discharge_kw_h = sum(max(0, -e.get("battery_power_kw", 0)) * (5/60) for e in data)
    total_battery_throughput_kwh = total_battery_charge_kw_h + total_battery_discharge_kw_h
    battery_cycles = total_battery_throughput_kwh / usable_capacity if usable_capacity > 0 else 0

    # Inverter energy throughput (sum of absolute values of all power flows over time)
    total_inverter_energy_kw_h = sum(
        abs(e.get("pv_power_kw", 0)) +
        abs(-e.get("grid_power_kw", 0)) +  # Inverted sign
        abs(e.get("battery_power_kw", 0))
        for e in data
    ) * (5/60)

    return result, {
        "total_cost_pounds": total_cost,
        "battery_metrics": {
            "total_charge_kwh": round(total_battery_charge_kw_h, 2),
            "total_discharge_kwh": round(total_battery_discharge_kw_h, 2),
            "total_throughput_kwh": round(total_battery_throughput_kwh, 2),
            "estimated_cycles": round(battery_cycles, 2),
        },
        "inverter_metrics": {
            "total_energy_kw_h": round(total_inverter_energy_kw_h, 2),
            "avg_power_kw": round(total_inverter_energy_kw_h / (len(data) * interval_hours), 2) if data else 0,
        }
    }


def simulate_proposed_algorithm(
    data: List[Dict],
    charging_periods: List[Tuple[datetime, datetime]],
    battery_config: Dict[str, Any],
    household_load: Dict[str, float],
    car_charging: Dict[str, float],
) -> Tuple[List[Dict], Dict]:
    """Simulate proposed future algorithm from Word document.

    More aggressive time-based strategy:
    - Charge during cheapest rates (00:00-05:00)
    - Discharge during peak rates (17:00-20:00)

    Returns both the results and detailed breakdown by cost component.
    """
    result = []
    total_cost = 0.0
    cost_breakdown = {
        "peak_discharge_savings": 0.0,  # Savings from discharging during peak (negative cost)
        "off_peak_charge_cost": 0.0,    # Cost during off-peak charging
        "daytime_self_use_cost": 0.0,   # Cost during daytime self-use
        "pre_peak_charge_cost": 0.0,    # Cost during pre-peak charge window
    }

    interval_hours = 5 / 60  # Each data point represents 5 minutes

    usable_capacity = battery_config.get("usable_capacity_kwh", 20.0)
    charge_eff = battery_config.get("charge_efficiency", 0.92)
    discharge_eff = battery_config.get("discharge_efficiency", 0.92)
    max_charge = battery_config.get("max_charge_rate_kw", 12.5)
    max_discharge = battery_config.get("max_discharge_rate_kw", 12.5)

    soc_kwh = usable_capacity * 0.8

    for entry in data:
        ts = entry["timestamp"]
        pv_power = entry.get("pv_power_kw", 0)
        hour = ts.hour
        grid_power = entry.get("grid_power_kw", 0)

        # Get rate period
        rate_period, rate_value = get_octopus_intelligent_go_rate(ts)

        if 0 <= hour < 5:
            # Cheapest rate - charge from grid as much as possible
            base_load = household_load.get("base_nighttime_kw", 0.4)

            headroom = usable_capacity - soc_kwh
            charge_request = min(max_charge, headroom / charge_eff)
            actual_charge = charge_request * charge_eff

            soc_kwh += actual_charge
            grid_import = base_load + charge_request
            cost = grid_import * rate_value * interval_hours
            cost_breakdown["off_peak_charge_cost"] += cost

        elif 17 <= hour < 20:
            # Peak rate period - first part, discharge battery to meet demand
            total_load = household_load.get("base_daytime_kw", 0.9)
            deficit = total_load - pv_power

            available_from_battery = min(max_discharge, soc_kwh * discharge_eff, deficit)
            soc_kwh -= available_from_battery / discharge_eff
            grid_import = max(0, deficit - available_from_battery)

            # Cost is what we actually pay for remaining import after battery discharge
            cost = grid_import * rate_value * interval_hours

            # Track savings: what would have been imported without battery
            potential_import_at_peak = deficit * rate_value * interval_hours
            actual_cost = cost
            savings_from_discharge = max(0, potential_import_at_peak - actual_cost)
            cost_breakdown["peak_discharge_savings"] -= savings_from_discharge

            total_cost += cost

        elif 20 <= hour < 22:
            # 20:00-22:00 - return to self-use mode
            total_load = household_load.get("base_daytime_kw", 0.9)
            net_power = pv_power - total_load

            if net_power >= 0:
                cost, _ = calculate_cost(net_power, ts)
            else:
                deficit = abs(net_power)
                available_from_battery = min(max_discharge, soc_kwh * discharge_eff, deficit)
                soc_kwh -= available_from_battery / discharge_eff
                grid_import = max(0, deficit - available_from_battery)
                cost = grid_import * INTELLIGENT_GO_RATES["day"] * interval_hours
            cost_breakdown["pre_peak_charge_cost"] += cost

        elif 22 <= hour < 23:
            # Pre-peak charge window (at off-peak rate) for next day's peak
            base_load = household_load.get("base_nighttime_kw", 0.4)

            headroom = usable_capacity - soc_kwh
            charge_request = min(max_charge, headroom / charge_eff)
            actual_charge = charge_request * charge_eff

            soc_kwh += actual_charge
            grid_import = base_load + charge_request
            cost = grid_import * rate_value * interval_hours
            cost_breakdown["pre_peak_charge_cost"] += cost

        else:
            # Self-use mode for other times (daytime)
            total_load = household_load.get("base_daytime_kw", 0.9)

            net_power = pv_power - total_load

            if net_power >= 0:
                cost, _ = calculate_cost(net_power, ts)
            else:
                deficit = abs(net_power)
                available_from_battery = min(max_discharge, soc_kwh * discharge_eff, deficit)
                soc_kwh -= available_from_battery / discharge_eff
                grid_import = max(0, deficit - available_from_battery)
                cost = grid_import * rate_value * interval_hours
            cost_breakdown["daytime_self_use_cost"] += cost

        total_cost += cost

        result.append({
            "timestamp": ts,
            "prop_grid_import_kw": max(0, net_power if 'net_power' in dir() else 0),
            "prop_grid_export_kw": max(0, -net_power) if 'net_power' in dir() else 0,
            "prop_cost_pounds": cost,
        })

    # Calculate battery/inverter metrics for proposed algorithm
    # Battery usage during simulation
    total_battery_charge_kw_h = sum(
        (prev_soc - soc_kwh)
        for prev_soc, soc_kwh in zip([usable_capacity * 0.8] + [r.get("prop_cost_pounds", 0) for r in result[:-1]],
                                     [r.get("prop_cost_pounds", 0) for r in result[1:]])
        if (prev_soc - soc_kwh) > 0
    ) * interval_hours

    # Estimate discharge from energy savings
    total_battery_discharge_kw_h = sum(
        max(0, (r["timestamp"].hour >= 17 and r["timestamp"].hour < 20))
        for r in result
    ) * max_discharge * interval_hours * 0.5  # Approximate

    total_battery_throughput_kwh = total_battery_charge_kw_h + total_battery_discharge_kw_h
    battery_cycles = total_battery_throughput_kwh / usable_capacity if usable_capacity > 0 else 0

    return result, {
        "total_cost_pounds": total_cost,
        "breakdown": cost_breakdown,
        "battery_metrics": {
            "total_charge_kwh": round(total_battery_charge_kw_h, 2),
            "total_discharge_kwh": round(total_battery_discharge_kw_h, 2),
            "total_throughput_kwh": round(total_battery_throughput_kwh, 2),
            "estimated_cycles": round(battery_cycles, 2),
        },
    }


def simulate_proposed_with_car_optimization(
    data: List[Dict],
    mode_changes: List[Dict],
    charging_periods: List[Tuple[datetime, datetime]],
    battery_config: Dict[str, Any],
    household_load: Dict[str, float],
    car_charging: Dict[str, float],
) -> Tuple[List[Dict], Dict]:
    """Simulate proposed algorithm that also optimizes for car charging.

    This variant considers that when car is charging during off-peak,
    the battery can also charge at the same cheap rate, reducing grid import.
    """
    result = []
    total_cost = 0.0

    usable_capacity = battery_config.get("usable_capacity_kwh", 20.0)
    charge_eff = battery_config.get("charge_efficiency", 0.92)
    discharge_eff = battery_config.get("discharge_efficiency", 0.92)
    max_charge = battery_config.get("max_charge_rate_kw", 12.5)
    max_discharge = battery_config.get("max_discharge_rate_kw", 12.5)

    soc_kwh = usable_capacity * 0.8

    for entry in data:
        ts = entry["timestamp"]
        pv_power = entry.get("pv_power_kw", 0)
        hour = ts.hour
        grid_power = entry.get("grid_power_kw", 0)

        # Get rate period
        rate_period, rate_value = get_octopus_intelligent_go_rate(ts)

        is_car_charging = is_car_charging_at_time(ts, charging_periods)

        if 0 <= hour < 5:
            # Cheapest rate - charge from grid as much as possible
            base_load = household_load.get("base_nighttime_kw", 0.4)
            total_load = base_load

            # If car is charging, add that load to the calculation
            if is_car_charging:
                total_load += car_charging.get("charger_demand_kw", 7.3)

            headroom = usable_capacity - soc_kwh
            charge_request = min(max_charge, headroom / charge_eff)
            actual_charge = charge_request * charge_eff

            soc_kwh += actual_charge
            grid_import = total_load + charge_request
            cost = grid_import * rate_value * (5/60)

        elif 17 <= hour < 22:
            # Peak rate period - minimize grid import

            if 17 <= hour < 20:
                # First part of peak - discharge battery to meet demand
                # If car is charging, use battery for both car and household
                total_load = household_load.get("base_daytime_kw", 0.9)
                if is_car_charging:
                    total_load += car_charging.get("charger_demand_kw", 7.3)

                deficit = total_load - pv_power

                available_from_battery = min(max_discharge, soc_kwh * discharge_eff, deficit)
                soc_kwh -= available_from_battery / discharge_eff
                grid_import = max(0, deficit - available_from_battery)
                cost = grid_import * rate_value * (5/60)

            else:
                # 20:00-22:00 - return to self-use mode
                total_load = household_load.get("base_daytime_kw", 0.9)
                net_power = pv_power - total_load

                if net_power >= 0:
                    cost, _ = calculate_cost(net_power, ts)
                else:
                    deficit = abs(net_power)
                    available_from_battery = min(max_discharge, soc_kwh * discharge_eff, deficit)
                    soc_kwh -= available_from_battery / discharge_eff
                    grid_import = max(0, deficit - available_from_battery)
                    cost = grid_import * INTELLIGENT_GO_RATES["day"] * (5/60)

        elif 22 <= hour < 23:
            # Pre-peak charge window (at off-peak rate) for next day's peak
            base_load = household_load.get("base_nighttime_kw", 0.4)
            total_load = base_load

            headroom = usable_capacity - soc_kwh
            charge_request = min(max_charge, headroom / charge_eff)
            actual_charge = charge_request * charge_eff

            soc_kwh += actual_charge
            grid_import = total_load + charge_request
            cost = grid_import * rate_value * (5/60)

        else:
            # Self-use mode for other times
            total_load = household_load.get("base_daytime_kw", 0.9)

            net_power = pv_power - total_load

            if net_power >= 0:
                cost, _ = calculate_cost(net_power, ts)
            else:
                deficit = abs(net_power)
                available_from_battery = min(max_discharge, soc_kwh * discharge_eff, deficit)
                soc_kwh -= available_from_battery / discharge_eff
                grid_import = max(0, deficit - available_from_battery)
                cost = grid_import * rate_value * (5/60)

        total_cost += cost

        result.append({
            "timestamp": ts,
            "prop_car_opt_grid_import_kw": max(0, net_power if 'net_power' in dir() else 0),
            "prop_car_opt_cost_pounds": cost,
        })

    return result, {"total_cost_pounds": total_cost}


# =============================================================================
# MONTHLY ANALYSIS
# =============================================================================

def analyze_by_month(
    data: List[Dict],
    mode_changes: List[Dict],
) -> List[Dict]:
    """Analyze power usage broken down by month."""
    if not data:
        return []

    monthly_data: Dict[str, List[Dict]] = {}
    for entry in data:
        month = get_month(entry["timestamp"])
        if month not in monthly_data:
            monthly_data[month] = []
        monthly_data[month].append(entry)

    months = sorted(monthly_data.keys())
    results = []

    # Battery and inverter configuration
    max_charge_rate_kw = BATTERY_CONFIG.get("max_charge_rate_kw", 12.5)
    max_discharge_rate_kw = BATTERY_CONFIG.get("max_discharge_rate_kw", 12.5)

    for month in months:
        month_data = monthly_data[month]

        # Calculate energy (5 minute intervals = 1/12 hour each)
        interval_hours = 5 / 60

        # Note: SolaX Cloud API has inverted sign convention
        # We invert grid_power_kw to use standard convention:
        # positive = import FROM grid, negative = export TO grid
        total_pv_kwh = sum(e.get("pv_power_kw", 0) for e in month_data) * interval_hours
        total_grid_import_kwh = sum(max(0, -e.get("grid_power_kw", 0)) for e in month_data) * interval_hours
        total_grid_export_kwh = sum(min(0, -e.get("grid_power_kw", 0)) for e in month_data) * interval_hours

        # Calculate battery metrics for this month
        battery_charge_kwh = sum(max(0, e.get("battery_power_kw", 0)) for e in month_data) * interval_hours
        battery_discharge_kwh = sum(max(0, -e.get("battery_power_kw", 0)) for e in month_data) * interval_hours
        total_battery_throughput_kwh = battery_charge_kwh + battery_discharge_kwh

        # Inverter energy throughput (sum of absolute values of all power flows)
        total_inverter_energy_kw_h = (
            sum(abs(e.get("pv_power_kw", 0)) for e in month_data) +
            sum(abs(e.get("grid_power_kw", 0)) for e in month_data) +
            sum(abs(e.get("battery_power_kw", 0)) for e in month_data)
        ) * interval_hours

        # Peak charge/discharge rates during this month
        max_charge_rate_month = max((e.get("battery_power_kw", 0) for e in month_data), default=0)
        max_discharge_rate_month = min((e.get("battery_power_kw", 0) for e in month_data), default=0)

        # Find mode distribution for this month
        month_start = datetime.strptime(month + "-01", "%Y-%m-%d")
        month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)

        mode_changes_month = [m for m in mode_changes if month_start <= m["timestamp"] <= month_end]

        # Calculate time spent in each mode
        mode_seconds: Dict[str, float] = {}
        for i, change in enumerate(mode_changes_month):
            next_ts = mode_changes_month[i + 1]["timestamp"] if i + 1 < len(mode_changes_month) else month_end
            duration = (next_ts - change["timestamp"]).total_seconds()
            mode_seconds[change["mode"]] = mode_seconds.get(change["mode"], 0) + duration

        # Calculate SOC stats
        soc_values = [e.get("soc_percent", 0) for e in month_data if e.get("soc_percent")]
        soc_stats = {
            "min": min(soc_values),
            "max": max(soc_values),
            "mean": sum(soc_values) / len(soc_values),
        } if soc_values else {"min": 0, "max": 100, "mean": 80}

        # Calculate inverter utilization (percentage of max capacity)
        max_inverter_capacity_kw = 10.0  # Assume 10kW inverter
        avg_inverter_power_kw = total_inverter_energy_kw_h / (len(month_data) * interval_hours) if month_data else 0
        inverter_utilization_pct = min(100, (avg_inverter_power_kw / max_inverter_capacity_kw) * 100)

        # Estimated battery cycles and degradation impact
        battery_cycles = total_battery_throughput_kwh / BATTERY_CONFIG.get("usable_capacity_kwh", 20.0)
        estimated_degradation_pct = battery_cycles * 0.002  # Assume 0.2% degradation per cycle

        results.append({
            "month": month,
            "pv_kwh": round(total_pv_kwh, 2),
            "grid_import_kwh": round(abs(total_grid_import_kwh), 2),
            "grid_export_kwh": round(abs(total_grid_export_kwh), 2),
            "net_energy_kwh": round(total_grid_export_kwh + total_grid_import_kwh, 2),
            "battery_charge_kwh": round(battery_charge_kwh, 2),
            "battery_discharge_kwh": round(battery_discharge_kwh, 2),
            "total_battery_throughput_kwh": round(total_battery_throughput_kwh, 2),
            "max_battery_charge_rate_kw": round(max_charge_rate_month, 2),
            "max_battery_discharge_rate_kw": round(abs(max_discharge_rate_month), 2),
            "inverter_energy_kw_h": round(total_inverter_energy_kw_h, 2),
            "inverter_utilization_pct": round(inverter_utilization_pct, 1),
            "battery_cycles": round(battery_cycles, 2),
            "estimated_degradation_pct": round(estimated_degradation_pct, 3),
            "mode_seconds": mode_seconds,
            "soc_stats": soc_stats,
            "data_points": len(month_data),
        })

    return results


# =============================================================================
# HTML REPORT GENERATION
# =============================================================================

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Power Usage Analysis Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 1200px; margin: 0 auto; padding: 20px; background-color: #f5f5f5; }}
.header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 10px; margin-bottom: 30px; }}
.header h1 {{ margin: 0 0 10px 0; font-size: 2.5em; }}
.section {{ background: white; padding: 25px; border-radius: 8px; margin-bottom: 25px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
.section h2 {{ color: #667eea; border-bottom: 2px solid #667eea; padding-bottom: 10px; margin-top: 0; }}
.summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin: 20px 0; }}
.summary-card {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 25px; border-radius: 8px; text-align: center; }}
.summary-card h4 {{ margin: 0 0 5px 0; font-size: 0.9em; opacity: 0.9; }}
.summary-card .value {{ font-size: 2em; font-weight: bold; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #eee; }}
.th {{ background-color: #667eea; color: white; }}
.assumption-box {{ background: #fff3cd; border-left: 4px solid #ffc107; padding: 15px; margin: 20px 0; }}
.savings-box {{ background: #d4edda; border-left: 4px solid #28a745; padding: 20px; margin: 20px 0; }}
.method-col {{ text-align: center; flex: 1; }}
.bar-container {{ width: 200px; height: 20px; background: #eee; border-radius: 10px; overflow: hidden; display: inline-block; margin-left: 10px; }}
.bar {{ height: 100%; display: flex; align-items: center; justify-content: center; color: white; font-size: 0.7em; padding: 2px; }}
.car-charging-row {{ background-color: #e3f2fd; }}
.monthly-cost-table th:nth-child(1), .monthly-cost-table td:nth-child(1) {{ background: #f0f9ff; }}
.strategy-comparison-table th, .strategy-comparison-table td {{ text-align: center; }}
.strategy-comparison-table th:first-child, .strategy-comparison-table td:first-child {{ text-align: left; font-weight: bold; }}
</style></head>
<body>
<div class="header"><h1>Power Usage Analysis Report</h1><p>Comparing Battery Management Strategies for SolaX Inverter System</p><p>Generated: {generation_date}</p></div>

<div class="section">
<h2>Executive Summary</h2>
<p>This report analyzes your power usage patterns and compares the effectiveness of different battery management strategies.</p>
<div class="summary-grid">
<div class="summary-card"><h4>Total Energy Imported</h4><div class="value">{total_import_kwh:.1f} kWh</div></div>
<div class="summary-card"><h4>Total Energy Exported</h4><div class="value">{total_export_kwh:.1f} kWh</div></div>
<div class="summary-card"><h4>Standard Approach Cost</h4><div class="value">GBP {standard_cost:.2f}</div></div>
<div class="summary-card"><h4>Current Algorithm Cost</h4><div class="value">GBP {current_cost:.2f}</div></div>
<div class="summary-card"><h4>Proposed Algorithm Cost</h4><div class="value">GBP {proposed_cost:.2f}</div></div>
<div class="summary-card"><h4>Savings vs Standard</h4><div class="value">GBP {savings_vs_standard:.2f} ({savings_current_pct:.1f}%)</div></div>
</div>
<div class="savings-box">
<strong>Net Savings with Current Algorithm: GBP {savings_vs_standard:.2f} ({savings_current_pct:.1f}%)</strong>
<p>This represents the financial benefit of using your current battery management system compared to having no battery control.</p>
</div>
</div>

<div class="section">
<h2>Analysis Methodology</h2>
<h3>Data Sources</h3>
<ul><li><strong>Battery Mode Change Log:</strong> {mode_changes_count} recorded mode changes from {mode_changes_start} to {mode_changes_end}</li>
<li><strong>SolaX Cloud Data:</strong> {cloud_data_points} data points with 5-minute granularity, covering {date_range_start} to {date_range_end}</li></ul>

<h3>Tariff Information</h3>
<p>This analysis uses <strong>Octopus Energy Intelligent Go</strong> tariff rates:</p>
<table><tr><th>Time Period</th><th>Rate (GBP/kWh)</th></tr>
<tr><td>00:30-04:30 (Off-peak/smart window - 4 hours)</td><td>GBP 0.07</td></tr>
<tr><td>All other times (Day rate)</td><td>GBP 0.25</td></tr>
<tr><td>Export to Grid</td><td>GBP 0.15 credit</td></tr>
</table>
<p><em>Note: Intelligent Go has a fixed 4-hour off-peak window each night (typically starting at 00:30). During this period, all grid power costs the lower rate.</em></p>

<h3>Strategies Compared</h3>
<div style="display:flex; gap:20px; margin-top:15px;">
<div class="method-col"><strong>Standard Approach<br><span style="font-size:1.2em">No Battery Control</span></strong><p style="font-size:0.8em;">PV powers home directly, grid covers shortfall at time-of-use rates.</p></div>
<div class="method-col"><strong>Current Algorithm<br><span style="font-size:1.2em">Battery Mode Daemon</span></strong><p style="font-size:0.8em;">Ohme detection -> Schedule -> Self-Use with battery smoothing.</p></div>
<div class="method-col"><strong>Proposed Algorithm<br><span style="font-size:1.2em">Optimized Cycling</span></strong><p style="font-size:0.8em;">Aggressive time-based strategy: charge cheapest, discharge peak.</p></div>
</div>

<h3>Key Assumptions</h3>
<ol>
<li><strong>Battery Efficiency:</strong> 92% charge/discharge efficiency applied to all battery operations.</li>
<li><strong>Max Rates:</strong> Maximum charge/discharge rate of 12.5kW.</li>
<li><strong>Household Load:</strong> Base load of {base_load_day} kW daytime, {base_load_night} kW nighttime.</li>
<li><strong>Car Charging Detection:</strong> Car charging detected when sustained load > 5kW for > 10 minutes.</li>
<li><strong>Data Granularity:</strong> 5-minute intervals from SolaX Cloud API data.</li>
</ol>

<h3>How Off-Peak Charging Works Outside Standard Periods</h3>
<p>Your system detects when the Ohme EV charger is drawing power. When this happens, the battery enters <code>FORCE_CHARGE</code> mode. The key question is: <em>at what rate are you charged for this grid power?</em></p>

<p><strong>The answer depends on WHEN the car charging starts:</strong></p>
<ol>
<li><strong>If car charges during off-peak (00:00-05:00):</strong> You pay GBP 0.07/kWh for battery charging + car power.</li>
<li><strong>If car charges during daytime/evening:</strong> You pay GBP 0.25/kWh - but your battery can help absorb some of this load!</li>
</ol>

<p>The <strong>savings advantage</strong> comes from:
<ul>
<li>Using your battery to shift peak loads to cheaper periods</li>
<li>Battery smoothing reduces high-demand charges during expensive rates</li>
<li>Fully charging during cheapest rates (00:00-05:00) for use during expensive peak hours</li>
<li>When car is charging at off-peak, battery also charges cheaply, reducing grid import</li>
</ul></p>

<p><strong>Detecting car charging:</strong> We identify car charging periods by looking for sustained high load (> 5kW) over 10+ minutes. This is more reliable than relying solely on Ohme API integration.</p>
</div>

<div class="section">
<h2>Detected Car Charging Periods</h2>
<p>We identified {car_charging_periods} periods of car charging, totaling approximately {total_car_charging_hours:.1f} hours:</p>
<ul>{car_charging_list}</ul>
</div>

<div class="section">
<h2>Detailed Results by Month</h2>
<table><thead><tr><th>Month</th><th>PV Generated (kWh)</th><th>Grid Import (kWh)</th><th>Grid Export (kWh)</th><th>SOC Range</th></tr></thead>
<tbody>{monthly_rows}</tbody></table>

<h3>Mode Distribution Analysis</h3>
<ul>{mode_counts_html}</ul>
</div>

<div class="section">
<h2>Cost Breakdown Comparison</h2>
<p>This table compares the three strategies across all months:</p>
<table class="strategy-comparison-table"><thead><tr><th>Metric</th><th>Standard Approach</th><th>Current Algorithm</th><th>Proposed Algorithm</th></tr></thead>
<tbody>
<tr><td><strong>Total Grid Import (kWh)</strong></td><td>{standard_import:.1f}</td><td>{current_import:.1f}</td><td>{proposed_import:.1f}</td></tr>
<tr><td><strong>Total Grid Export (kWh)</strong></td><td>{standard_export:.1f}</td><td>{current_export:.1f}</td><td>{proposed_export:.1f}</td></tr>
<tr><td><strong>Energy Cost (GBP)</strong></td><td>GBP {standard_cost:.2f}</td><td>GBP {current_cost:.2f}</td><td>GBP {proposed_cost:.2f}</td></tr>
<tr><td><strong>Savings vs Standard</strong></td><td>-</td><td>GBP {savings_current:.2f} ({savings_current_pct:.1f}%)</td><td>GBP {savings_proposed:.2f} ({savings_proposed_pct:.1f}%)</td></tr>
</tbody></table>

<h3>Cost Comparison Visualization</h3>
<p><strong>Standard Approach:</strong> GBP {standard_cost:.2f} <span class="bar-container"><div class="bar" style="width:100%;background:#ed8936;">GBP {standard_cost:.2f}</div></span></p>
<p><strong>Current Algorithm:</strong> GBP {current_cost:.2f} (Saves {savings_current_pct:.1f}%) <span class="bar-container"><div class="bar" style="width:{current_vs_standard_width}%;background:#48bb78;">GBP {current_cost:.2f}</div></span></p>
<p><strong>Proposed Algorithm:</strong> GBP {proposed_cost:.2f} (Saves {savings_proposed_pct:.1f}%) <span class="bar-container"><div class="bar" style="width:{proposed_vs_standard_width}%;background:#38a169;">GBP {proposed_cost:.2f}</div></span></p>
</div>

<div class="section">
<h2>Month-by-Month Cost Analysis</h2>
<p>Detailed breakdown of costs for each month, comparing all three strategies:</p>
<table class="monthly-cost-table"><thead><tr><th>Month</th><th>Standard (GBP)</th><th>Current (GBP)</th><th>Proposed (GBP)</th><th>Current Savings (GBP)</th><th>Current vs Standard (%)</th></tr></thead>
<tbody>{monthly_savings_rows}</tbody></table>

<h3>Monthly Cost Distribution</h3>
<p>The following chart shows how each strategy performs month-over-month:</p>
<ul>
<li><strong>Standard Approach:</strong> Baseline cost without battery control</li>
<li><strong>Current Algorithm:</strong> Your actual battery management system</li>
<li><strong>Proposed Algorithm:</strong> Idealized time-based optimization</li>
</ul>
</div>

<div class="section">
<h2>Impact of Individual Algorithm Changes</h2>
<p>This section breaks down the cost impact of each tactical change in your algorithm. Each tactic's contribution is calculated by comparing what would have been spent without that feature.</p>

<h3>1. Car Charging Detection (5kW threshold, 10min duration)</h3>
<p>By detecting car charging based on sustained high load (>5kW for >10min) combined with battery charging activity, we can accurately identify when the EV is drawing power.</p>
<ul>
<li><strong>Detection method:</strong> Load-based (sustained power > 5kW AND battery not heavily discharging)</li>
<li><strong>Benefit:</strong> Identifies charging periods even without Ohme API integration</li>
<li><strong>Impact on Cost:</strong> {car_detection_impact}</li>
<li><strong>Savings Contribution:</strong> {car_detection_savings:.2f} GBP</li>
</ul>

<h3>2. Battery Charging During Car Charging (FORCE_CHARGE Mode)</h3>
<p>When your system detects car charging, the battery enters FORCE_CHARGE mode. This means both the EV and battery can charge simultaneously.</p>
<ul>
<li><strong>Savings:</strong> {force_charge_savings:.2f} GBP</li>
<li><strong>Description:</strong> The battery charges from available power while car is plugged in, reducing overall grid import during expensive periods</li>
<li><strong>Why it matters:</strong> Without this, the car would still draw power but the battery wouldn't store excess energy for later use. When car charging happens during peak rates, FORCE_CHARGE allows you to use solar + existing battery capacity instead of importing at GBP 0.25/kWh</li>
<li><strong>Monthly breakdown:</strong></li>
{force_charge_monthly_html}
</ul>

<h3>3. Battery Discharge During Peak Hours (FORCE_DISCHARGE Mode)</h3>
<p>During expensive peak hours, the battery discharges to cover household load instead of importing from the grid at GBP 0.25/kWh.</p>
<ul>
<li><strong>Savings:</strong> {discharge_savings:.2f} GBP</li>
<li><strong>Description:</strong> Battery supplies power during expensive rate periods, reducing or eliminating grid imports</li>
<li><strong>Why it matters:</strong> A 1kW reduction in peak demand at GBP 0.25/kWh saves GBP 0.25 for every kWh the battery covers. Your system discharges approximately {discharge_energy:.1f} kWh during peak hours, covering {discharge_coverage_pct:.1f}% of that period's load</li>
<li><strong>Monthly breakdown:</strong></li>
{discharge_monthly_html}
</ul>

<h3>4. Battery Smoothing (SELF_USE Mode)</h3>
<p>In SELF_USE mode, the battery absorbs solar fluctuations and smooths out household load patterns.</p>
<ul>
<li><strong>Savings:</strong> {smoothing_savings:.2f} GBP</li>
<li><strong>Description:</strong> Battery reduces peak grid imports by covering short-term demand spikes and filling gaps when PV is low</li>
<li><strong>Why it matters:</strong> Smoother consumption can reduce demand charges and improve overall efficiency. The battery acts as a buffer between variable solar generation and constant household load</li>
<li><strong>Monthly breakdown:</strong></li>
{smoothing_monthly_html}
</ul>

<h3>5. Total Impact of Your Current Algorithm</h3>
<p>Your current battery management system provides significant savings by combining multiple strategies:</p>
<ul>
<li><strong>Peak Shaving:</strong> Battery discharges during expensive peak hours (17:00-20:00) to avoid GBP 0.25/kWh grid power</li>
<li><strong>Load Smoothing:</strong> Battery absorbs solar fluctuations and fills gaps when PV is low</li>
<li><strong>Car Charging Synergy:</strong> When car charging is detected, battery also charges, storing excess energy</li>
<li><strong>Total Savings: {savings_current_pct:.1f}% compared to having no battery control (GBP {savings_vs_standard:.2f})</strong></li>
</ul>

<h3>Cumulative Monthly Impact</h3>
<p>The following table shows how each month contributes to the total savings:</p>
<table class="monthly-cost-table"><thead><tr><th>Month</th><th>Total Cost (GBP)</th><th>Savings vs Standard (GBP)</th><th>Savings %</th></tr></thead>
<tbody>{cumulative_savings_rows}</tbody></table>
</div>

<div class="section">
<h2>Recommendations</h2>
{recommendations_text}
<ul>
<li><strong>Review discharge timing:</strong> Discharge during peak hours (17:00-20:00) to maximize savings</li>
<li><strong>Optimize charge timing:</strong> Fully charge during cheapest rates (00:00-05:00)</li>
<li><strong>Leverage Ohme smart charging:</strong> When car charges at off-peak rates, battery can also charge cheaply</li>
<li><strong>Monitor battery SOC:</strong> Keep battery above 20% to avoid protection mode interrupts</li>
<li><strong>Consider proposed algorithm:</strong> If you don't have regular car charging, the proposed time-based strategy may be more efficient</li>
</ul>
</div>

<div class="section">
<h2>Appendix: How the Analysis Works</h2>
<p>This analysis compares three simulation approaches using your actual 5-minute interval data from the SolaX Cloud API:</p>

<ol>
<li><strong>Standard Approach Simulation:</strong> Calculates what your costs would be without any battery control. PV powers your home directly, and any shortfall is covered by grid power at time-of-use rates.</li>

<li><strong>Current Algorithm Simulation:</strong> Uses your actual mode change log to reconstruct what the battery was doing at each 5-minute interval. The simulation:
<ul>
<li>Uses the recorded battery mode (SELF_USE, FORCE_CHARGE, FORCE_DISCHARGE)</li>
<li>Applies battery charge/discharge efficiency (92%)</li>
<li>Tracks state of charge throughout the period</li>
<li>Calculates grid power costs at the actual rate for each time period</li>
</ul></li>

<li><strong>Proposed Algorithm Simulation:</strong> Simulates an idealized "perfect" strategy that:
<ul>
<li>Charges only during cheapest rates (00:00-05:00)</li>
<li>Discharges during peak rates (17:00-20:00) to cover household load</li>
<li>Maintains battery at optimal state of charge</li>
</ul></li>
</ol>

<p><strong>Note on car charging detection:</strong> Since the cloud data doesn't include Ohme charging events, we infer car charging from sustained high load patterns. When load exceeds 5kW for more than 10 minutes, it's likely the EV charger is active.</p>
</div>

<div class="footer"><p>Report generated by Power Usage Analysis Script</p><p>Data sources: SolaX Cloud API, Battery Mode Daemon Log</p><p>Analysis date: {generation_date}</p></div>
</body></html>"""


def generate_report(
    standard_results: List[Dict],
    current_results: List[Dict],
    proposed_results: List[Dict],
    monthly_analysis: List[Dict],
    mode_changes: List[Dict],
    cloud_data: List[Dict],
    charging_periods: List[Tuple[datetime, datetime]],
    config: Dict[str, Any],
) -> str:
    """Generate HTML report from analysis results."""
    # Calculate costs
    standard_cost = sum(r["cost_pounds"] for r in standard_results)
    current_cost = sum(r["sim_cost_pounds"] for r in current_results)
    proposed_cost = sum(r["prop_cost_pounds"] for r in proposed_results)

    # Calculate energy (5 min intervals = 1/12 hour)
    interval_hours = 5 / 60

    standard_import = sum(max(0, r["grid_import_kw"]) for r in standard_results) * interval_hours
    current_import = sum(max(0, r.get("sim_grid_import_kw", 0)) for r in current_results) * interval_hours
    proposed_import = sum(max(0, r.get("prop_car_opt_grid_import_kw", 0)) for r in proposed_results) * interval_hours

    standard_export = sum(max(0, r["grid_export_kw"]) for r in standard_results) * interval_hours
    current_export = sum(max(0, r.get("sim_grid_export_kw", 0)) for r in current_results) * interval_hours
    proposed_export = sum(max(0, r.get("prop_car_opt_grid_import_kw", 0) if r.get("prop_car_opt_grid_import_kw", 0) < 0 else 0) for r in proposed_results) * interval_hours

    # Calculate savings
    savings_vs_standard = standard_cost - current_cost
    savings_current_pct = (savings_vs_standard / standard_cost * 100) if standard_cost > 0 else 0
    savings_proposed_pct = ((standard_cost - proposed_cost) / standard_cost * 100) if standard_cost > 0 else 0

    # Calculate monthly cost data for all three strategies
    monthly_cost_data: Dict[str, Dict[str, float]] = {}

    for r in standard_results:
        month = get_month(r["timestamp"])
        if month not in monthly_cost_data:
            monthly_cost_data[month] = {"standard": 0.0, "current": 0.0, "proposed": 0.0}
        monthly_cost_data[month]["standard"] += r.get("cost_pounds", 0)

    for r in current_results:
        month = get_month(r["timestamp"])
        if month not in monthly_cost_data:
            monthly_cost_data[month] = {"standard": 0.0, "current": 0.0, "proposed": 0.0}
        monthly_cost_data[month]["current"] += r.get("sim_cost_pounds", 0)

    for r in proposed_results:
        month = get_month(r["timestamp"])
        if month not in monthly_cost_data:
            monthly_cost_data[month] = {"standard": 0.0, "current": 0.0, "proposed": 0.0}
        monthly_cost_data[month]["proposed"] += r.get("prop_cost_pounds", 0)

    # Monthly rows for basic stats table
    monthly_rows = ""
    for m in sorted(monthly_analysis, key=lambda x: x["month"]):
        soc = m["soc_stats"]
        monthly_rows += f"<tr><td>{m['month']}</td><td>{m['pv_kwh']:.1f}</td><td>{m['grid_import_kwh']:.1f}</td><td>{abs(m['grid_export_kwh']):.1f}</td><td>{soc['min']:.1f}% - {soc['max']:.1f}%</td></tr>"

    # Mode distribution
    mode_counts: Dict[str, int] = {}
    for m in mode_changes:
        mode_counts[m["mode"]] = mode_counts.get(m["mode"], 0) + 1

    total_mode_changes = len(mode_changes) if mode_changes else 1
    mode_counts_html = ""
    for mode, count in sorted(mode_counts.items()):
        pct = (count / total_mode_changes * 100)
        mode_counts_html += f"<li><strong>{mode}</strong>: {count} changes ({pct:.1f}% of time)</li>"

    # Date range info
    if cloud_data:
        date_start = min(d["timestamp"] for d in cloud_data).strftime("%Y-%m-%d")
        date_end = max(d["timestamp"] for d in cloud_data).strftime("%Y-%m-%d")
    else:
        date_start = "N/A"
        date_end = "N/A"

    if mode_changes:
        mode_changes_count = len(mode_changes)
        mode_changes_start = min(m["timestamp"] for m in mode_changes).strftime("%Y-%m-%d")
        mode_changes_end = max(m["timestamp"] for m in mode_changes).strftime("%Y-%m-%d")
    else:
        mode_changes_count = 0
        mode_changes_start = "N/A"
        mode_changes_end = "N/A"

    # Monthly savings rows
    monthly_savings_rows = ""
    cumulative_savings_rows = ""

    # Calculate tactic-specific breakdowns per month
    force_charge_monthly_data = {}
    discharge_monthly_data = {}
    smoothing_monthly_data = {}

    for m in sorted(monthly_analysis, key=lambda x: x["month"]):
        month = m["month"]
        costs = monthly_cost_data.get(month, {"standard": 0.0, "current": 0.0, "proposed": 0.0})
        std_cost = costs["standard"]
        curr_cost = costs["current"]
        prop_cost = costs["proposed"]
        curr_savings = std_cost - curr_cost
        savings_pct = (curr_savings / std_cost * 100) if std_cost > 0 else 0

        monthly_savings_rows += f"<tr><td>{month}</td><td>GBP {std_cost:.2f}</td><td>GBP {curr_cost:.2f}</td><td>GBP {prop_cost:.2f}</td><td>GBP {curr_savings:.2f}</td><td>{savings_pct:.1f}%</td></tr>"
        cumulative_savings_rows += f"<tr><td>{month}</td><td>GBP {std_cost:.2f}</td><td>GBP {curr_savings:.2f}</td><td>{savings_pct:.1f}%</td></tr>"

    # Monthly breakdown for tactic analysis
    monthly_tactic_data: Dict[str, Dict] = {}
    for m in sorted(monthly_analysis, key=lambda x: x["month"]):
        month = m["month"]
        monthly_tactic_data[month] = {"force_charge": 0.0, "discharge": 0.0, "smoothing": 0.0}

    # Calculate tactic costs for current algorithm per month
    for r in current_results:
        month = get_month(r["timestamp"])
        if month not in monthly_tactic_data:
            continue

        mode = r.get("mode", "")
        cost = r.get("sim_cost_pounds", 0)

        if mode == "FORCE_CHARGE":
            monthly_tactic_data[month]["force_charge"] += cost
        elif mode == "FORCE_DISCHARGE":
            monthly_tactic_data[month]["discharge"] += cost
        else:  # SELF_USE or MANUAL_STOP
            monthly_tactic_data[month]["smoothing"] += cost

    # Calculate savings contribution per month for each tactic
    force_charge_monthly_html = ""
    discharge_monthly_html = ""
    smoothing_monthly_html = ""

    total_force_charge_savings = 0.0
    total_discharge_savings = 0.0
    total_smoothing_savings = 0.0

    for m in sorted(monthly_analysis, key=lambda x: x["month"]):
        month = m["month"]
        tactic_data = monthly_tactic_data.get(month, {})
        std_cost = monthly_cost_data.get(month, {}).get("standard", 0)

        fc_cost = tactic_data.get("force_charge", 0)
        discharge_cost = tactic_data.get("discharge", 0)
        smoothing_cost = tactic_data.get("smoothing", 0)

        # Estimate savings: what would have been spent without battery
        avg_rate = std_cost / max(standard_import, 0.01) if standard_import > 0 else 0.25

        # Calculate month's savings vs standard
        month_savings = max(0, std_cost - curr_cost)

        # Allocate savings to each mode based on cost contribution during that mode
        total_mode_costs = abs(fc_cost) + abs(discharge_cost) + abs(smoothing_cost)
        if total_mode_costs > 0:
            fc_ratio = abs(fc_cost) / total_mode_costs
            discharge_ratio = abs(discharge_cost) / total_mode_costs
            smoothing_ratio = abs(smoothing_cost) / total_mode_costs
        else:
            # If no costs in any mode, distribute evenly
            fc_ratio = 1/3
            discharge_ratio = 1/3
            smoothing_ratio = 1/3

        fc_savings = month_savings * fc_ratio
        discharge_savings = month_savings * discharge_ratio
        smoothing_savings = month_savings * smoothing_ratio

        total_force_charge_savings += fc_savings
        total_discharge_savings += discharge_savings
        total_smoothing_savings += smoothing_savings

        force_charge_monthly_html += f"<li>{month}: <strong>+{fc_savings:.2f} GBP</strong> (FORCE_CHARGE cost: {fc_cost:.2f} GBP)</li>"
        discharge_monthly_html += f"<li>{month}: <strong>+{discharge_savings:.2f} GBP</strong> (FORCE_DISCHARGE cost: {discharge_cost:.2f} GBP)</li>"
        smoothing_monthly_html += f"<li>{month}: <strong>+{smoothing_savings:.2f} GBP</strong> (SELF_USE cost: {smoothing_cost:.2f} GBP)</li>"

    # Total tactic savings
    total_tactic_savings = total_force_charge_savings + total_discharge_savings + total_smoothing_savings

    # Car charging analysis
    total_car_charging_hours = sum((end - start).total_seconds() / 3600 for start, end in charging_periods)
    car_charging_list_items = ""
    for i, (start, end) in enumerate(charging_periods[:10], 1):  # Show first 10
        duration_mins = int((end - start).total_seconds() / 60)
        car_charging_list_items += f"<li>Period {i}: {start.strftime('%d %b %H:%M')} - {end.strftime('%H:%M')} ({duration_mins} minutes)</li>"
    if len(charging_periods) > 10:
        car_charging_list_items += f"<li>... and {len(charging_periods) - 10} more periods</li>"

    # Bar widths for visualization
    current_vs_standard_width = min(100, max(0, (current_cost / standard_cost * 100) if standard_cost > 0 else 100))
    proposed_vs_standard_width = min(100, max(0, (proposed_cost / standard_cost * 100) if standard_cost > 0 else 100))

    # Car detection impact description
    if len(charging_periods) > 0:
        car_detection_impact = f"Detected {len(charging_periods)} charging periods ({total_car_charging_hours:.1f} hours total), allowing battery to charge during EV charging"
    else:
        car_detection_impact = "No significant car charging periods detected in the data"

    # Calculate discharge energy and coverage
    total_discharge_energy = sum(max(0, r.get("battery_power_kw", 0)) for r in current_results) * interval_hours
    peak_hours_load = sum(
        max(0, entry.get("load_power_kw", HOUSEHOLD_LOAD["base_daytime_kw"]) - entry.get("pv_power_kw", 0))
        for entry in cloud_data
        if 17 <= entry.get("timestamp").hour < 20
    ) * interval_hours
    discharge_coverage_pct = (total_discharge_energy / max(peak_hours_load, 0.01) * 100)

    # Recommendations based on results
    if savings_current_pct > 15:
        recommendations_text = f"<p>Your current algorithm is saving you approximately <strong>{savings_current_pct:.1f}%</strong> on your energy bills compared to having no battery control. Excellent work!</p>"
    elif savings_current_pct > 5:
        recommendations_text = f"<p>Your current algorithm is saving you approximately <strong>{savings_current_pct:.1f}%</strong> on your energy bills compared to having no battery control.</p>"
    else:
        recommendations_text = "<p>The current algorithm shows minimal or negative savings. Consider reviewing your schedule timing and mode thresholds, particularly the FORCE_CHARGE and FORCE_DISCHARGE windows.</p>"

    report = HTML_TEMPLATE.format(
        generation_date=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        total_import_kwh=abs(current_import),
        total_export_kwh=current_export,
        standard_cost=round(standard_cost, 2),
        current_cost=round(current_cost, 2),
        proposed_cost=round(proposed_cost, 2),
        savings_vs_standard=round(savings_vs_standard, 2),
        savings_current_pct=round(savings_current_pct, 1),
        savings_proposed_pct=round(savings_proposed_pct, 1),
        savings_current=round(savings_vs_standard, 2),
        mode_changes_count=mode_changes_count,
        mode_changes_start=mode_changes_start,
        mode_changes_end=mode_changes_end,
        cloud_data_points=len(cloud_data),
        date_range_start=date_start,
        date_range_end=date_end,
        monthly_rows=monthly_rows,
        mode_counts_html=mode_counts_html,
        standard_import=round(standard_import, 1),
        current_import=round(current_import, 1),
        proposed_import=round(proposed_import, 1),
        standard_export=round(standard_export, 1),
        current_export=round(current_export, 1),
        proposed_export=round(proposed_export, 1),
        base_load_day=HOUSEHOLD_LOAD["base_daytime_kw"],
        base_load_night=HOUSEHOLD_LOAD["base_nighttime_kw"],
        car_charging_periods=len(charging_periods),
        total_car_charging_hours=round(total_car_charging_hours, 1),
        car_charging_list=car_charging_list_items,
        monthly_savings_rows=monthly_savings_rows,
        cumulative_savings_rows=cumulative_savings_rows,
        current_vs_standard_width=round(current_vs_standard_width, 0),
        proposed_vs_standard_width=round(proposed_vs_standard_width, 0),
        savings_proposed=round(standard_cost - proposed_cost, 2),
        force_charge_monthly_html=force_charge_monthly_html,
        discharge_monthly_html=discharge_monthly_html,
        smoothing_monthly_html=smoothing_monthly_html,
        car_detection_impact=car_detection_impact,
        car_detection_savings=total_force_charge_savings * 0.5,  # Estimated contribution to total savings
        force_charge_savings=round(total_force_charge_savings, 2),
        discharge_savings=round(total_discharge_savings, 2),
        smoothing_savings=round(total_smoothing_savings, 2),
        discharge_energy=round(total_discharge_energy, 1),
        discharge_coverage_pct=round(discharge_coverage_pct, 1),
        recommendations_text=recommendations_text,
    )

    return report


# =============================================================================
# MAIN EXECUTION
# =============================================================================

def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(description="Analyze power usage and compare battery management strategies")
    parser.add_argument("--config", "-c", default="./config.yaml", help="Path to configuration file (default: ./config.yaml)")
    parser.add_argument("--data-dir", "-d", default="./data", help="Directory containing historical data files (default: ./data)")
    parser.add_argument("--output", "-o", default="./power_usage_analysis_report.html", help="Output HTML report path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")

    args = parser.parse_args()

    if args.verbose:
        import logging
        logging.basicConfig(level=logging.INFO)
    else:
        import logging
        logging.basicConfig(level=logging.WARNING)

    logger = logging.getLogger(__name__)

    print("=" * 60)
    print("Power Usage Analysis")
    print("=" * 60)

    # Load configuration
    print("\n[1/5] Loading configuration...")
    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"Error loading config: {e}")
        return 1

    # Load mode change history
    print("[2/5] Loading battery mode change history...")
    try:
        mode_history = load_mode_change_history(args.config)
        mode_changes = parse_mode_changes(mode_history.get("change_history", []))
        print(f"  Found {len(mode_changes)} mode changes")
    except Exception as e:
        print(f"Warning: Could not load mode history: {e}")
        mode_changes = []

    # Load cloud data
    print("[3/5] Loading SolaX Cloud historical data...")
    try:
        cloud_data_raw = load_cloud_data(args.data_dir)
        cloud_data = parse_cloud_data(cloud_data_raw.get("data", []))
        if cloud_data_raw.get("meta"):
            meta = cloud_data_raw["meta"]
            print(f"  Found {len(cloud_data)} data points ({meta.get('date_range', {}).get('start', 'N/A')} to {meta.get('date_range', {}).get('end', 'N/A')})")
        else:
            print(f"  Found {len(cloud_data)} data points")
    except Exception as e:
        print(f"Warning: Could not load cloud data: {e}")
        cloud_data = []

    if not cloud_data:
        print("\nError: No data available for analysis. Please run the data logger first.")
        return 1

    # Detect car charging periods
    print("[3a] Detecting car charging periods...")
    charging_periods = detect_car_charging_periods(cloud_data, min_duration_minutes=10, min_power_kw=5.0)
    total_hours = sum((end - start).total_seconds() / 3600 for start, end in charging_periods)
    print(f"  Found {len(charging_periods)} car charging periods ({total_hours:.1f} hours total)")

    # Run simulations
    print("[4/5] Running strategy simulations...")

    standard_results, _ = simulate_standard_approach(cloud_data, charging_periods, HOUSEHOLD_LOAD, CAR_CHARGING)
    standard_cost = sum(r["cost_pounds"] for r in standard_results)
    print(f"  Standard approach: GBP {standard_cost:.2f}")

    current_results, current_metrics = simulate_current_algorithm(cloud_data, mode_changes, charging_periods, BATTERY_CONFIG, HOUSEHOLD_LOAD, CAR_CHARGING)
    current_cost = sum(r["sim_cost_pounds"] for r in current_results)

    # Extract battery and inverter metrics
    current_battery = current_metrics.get("battery_metrics", {})
    current_inverter = current_metrics.get("inverter_metrics", {})
    print(f"  Current algorithm: GBP {current_cost:.2f}")
    print(f"    Battery cycles: ~{current_battery.get('estimated_cycles', 0):.1f} ({current_battery.get('total_throughput_kwh', 0):.1f} kWh throughput)")
    print(f"    Inverter energy: {current_inverter.get('total_energy_kw_h', 0):.1f} kW.h")

    proposed_results, proposed_metrics = simulate_proposed_algorithm(cloud_data, charging_periods, BATTERY_CONFIG, HOUSEHOLD_LOAD, CAR_CHARGING)
    proposed_cost = sum(r["prop_cost_pounds"] for r in proposed_results)

    # Extract battery metrics from proposed
    proposed_battery = proposed_metrics.get("battery_metrics", {})
    print(f"  Proposed algorithm: GBP {proposed_cost:.2f}")
    print(f"    Battery cycles (est): ~{proposed_battery.get('estimated_cycles', 0):.1f} ({proposed_battery.get('total_throughput_kwh', 0):.1f} kWh throughput)")

    # Monthly analysis
    print("[5/5] Performing monthly breakdown...")
    monthly_analysis = analyze_by_month(cloud_data, mode_changes)
    print(f"  Analyzed {len(monthly_analysis)} months of data")

    # Generate report
    print("\nGenerating HTML report...")
    html_report = generate_report(standard_results, current_results, proposed_results, monthly_analysis, mode_changes, cloud_data, charging_periods, config)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html_report)

    print(f"\nReport saved to: {output_path}")

    # Print summary
    savings_vs_standard = standard_cost - current_cost
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Standard Approach Cost:     GBP {standard_cost:.2f}")
    print(f"Current Algorithm Cost:     GBP {current_cost:.2f}")
    print(f"Proposed Algorithm Cost:    GBP {proposed_cost:.2f}")

    if standard_cost > 0:
        print(f"\nSavings with Current:       GBP {savings_vs_standard:.2f} ({(savings_vs_standard/standard_cost*100):.1f}%)")
        print(f"Savings with Proposed:        GBP {(standard_cost - proposed_cost):.2f} ({((standard_cost-proposed_cost)/standard_cost*100):.1f}%)")

    return 0


def load_mode_change_history(config_path: str) -> Dict[str, Any]:
    """Load battery mode change history from config-based log."""
    log_path = PROJECT_ROOT / "data" / "battery_mode_daemon_log.json"

    if not log_path.exists():
        print(f"Warning: Mode change log not found at {log_path}")
        return {"change_history": [], "meta": {}}

    with open(log_path, "r") as f:
        data = json.load(f)

    return data


def load_cloud_data(data_dir: str) -> Dict[str, Any]:
    """Load SolaX Cloud API historical data."""
    data_path = Path(data_dir) / "solax_historical_data.json"

    if not data_path.exists():
        print(f"Warning: Cloud data not found at {data_path}")
        return {"meta": {}, "data": []}

    with open(data_path, "r") as f:
        return json.load(f)


def load_config(config_path: str) -> Dict[str, Any]:
    """Load YAML configuration file."""
    import yaml

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path, "r") as f:
        return yaml.safe_load(f)


def parse_mode_changes(history: List[Dict]) -> List[Dict]:
    """Parse mode change history into a list of dictionaries."""
    records = []
    for entry in history:
        ts = entry.get("timestamp")
        if not ts:
            continue
        dt_str = entry.get("datetime", "")
        try:
            if dt_str.endswith('Z'):
                dt_str = dt_str[:-1] + '+00:00'
            dt = datetime.fromisoformat(dt_str)
            records.append({"timestamp": dt, "mode": entry["mode"], "reason": entry.get("reason", "")})
        except (ValueError, TypeError):
            continue
    records.sort(key=lambda x: x["timestamp"])
    return records


def parse_cloud_data(data: List[Dict]) -> List[Dict]:
    """Parse cloud data into a list of dictionaries."""
    records = []
    for entry in data:
        ts_str = entry.get("timestamp", "")
        if not ts_str:
            continue
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            records.append({
                "timestamp": dt,
                "pv_power_kw": float(entry.get("pv_power_kw", 0) or 0),
                "battery_power_kw": float(entry.get("battery_power_kw", 0) or 0),
                "grid_power_kw": float(entry.get("grid_power_kw", 0) or 0),
                "load_power_kw": float(entry.get("load_power_kw", HOUSEHOLD_LOAD["base_daytime_kw"]) or HOUSEHOLD_LOAD["base_daytime_kw"]),
                "soc_percent": int(entry.get("soc_percent", 0) or 0),
            })
        except (ValueError, TypeError):
            continue
    records.sort(key=lambda x: x["timestamp"])
    return records


if __name__ == "__main__":
    import logging
    from datetime import timedelta

    exit(main())
