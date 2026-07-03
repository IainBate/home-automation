"""Modbus Data Validation Module.

Handles data validation against physical constraints and provides fallback reading
mechanisms for error recovery.

INTERNAL MODULE: Only for use by solax_modbus_client.py and related modules.
"""

# pylint: disable=cyclic-import
# Justification: Intentional modbus client split architecture. This helper module
# is imported by solax_modbus_client.py and uses type hints from it. The cycle
# is resolved at runtime and doesn't cause actual circular dependency issues.
from __future__ import annotations

import logging
from typing import Any

# Setup basic logging
logger = logging.getLogger(__name__)


class PowerValidationResult:
    """Result of power data physical validation for SolaX Modbus error handling.

    This class provides structured validation results for detecting physically
    impossible power values caused by Modbus communication errors or register
    overflow issues (e.g., signed 16-bit integer overflow).

    Used to identify "power spikes" that exceed the physical electrical system
    constraints (100A/24kW supply limit + 2kW headroom = 26kW maximum).
    """

    def __init__(self) -> None:
        self.physically_possible = True
        self.errors = []
        self.warnings = []

    def add_error(self, message: str) -> None:
        """Add a validation error indicating physically impossible values."""
        self.physically_possible = False
        self.errors.append(message)

    def add_warning(self, message: str) -> None:
        """Add a validation warning indicating values approaching limits."""
        self.warnings.append(message)


def validate_power_data_physical_limits(data: dict[str, Any]) -> PowerValidationResult:
    """Validate power data against physical electrical system constraints.

    This function detects physically impossible power values that can occur due to:
    - SolaX Modbus register overflow (16-bit signed integer issues)
    - Communication errors causing corrupt data
    - "Sudden peaks" documented in SolaX Modbus specifications

    Physical Constraints (based on 100A residential supply):
    - Maximum power: 26kW (100A × 240V + 2kW headroom for temporary spikes)  # noqa: RUF002
    - Warning threshold: 20kW (83A - early warning level)

    Args:
        data: Power data dictionary from solax_modbus_bulk_data() or individual functions
              Expected structure: {'ac_power': {'master': W, 'slave': W},
                                  'battery_power': {'master': W, 'slave': W},
                                  'grid_power': {'master': W, 'slave': W}}

    Returns:
        PowerValidationResult with validation status, detailed errors, and warnings

    Example:
        >>> data = {"ac_power": {"master": 50000, "slave": 2000}}  # 50kW impossible
        >>> result = validate_power_data_physical_limits(data)
        >>> result.physically_possible
        False
        >>> result.errors
        ['AC Power Master: 50.00kW exceeds 26.0kW physical limit (100A supply)']

    """  # noqa: RUF002
    result = PowerValidationResult()
    MAX_POWER_KW = 26.0  # noqa: N806 - Physical constant (circuit breaker limit)  # pylint: disable=invalid-name  # Constant
    WARNING_POWER_KW = 20.0  # noqa: N806 - Physical constant (83A early warning threshold)  # pylint: disable=invalid-name  # Constant

    # Define power measurements to validate from bulk data structure
    # Handle missing categories gracefully by providing empty dict defaults
    # Also handle None values by providing empty dict
    ac_power_data = data.get("ac_power") or {}
    battery_power_data = data.get("battery_power") or {}
    grid_power_data = data.get("grid_power") or {}

    # Extract power values, handling nested structures for battery power
    # AC and grid power are simple integers in the data structure
    # Battery power is nested: {"master": {"power": value, "mode": str}, ...}
    battery_master_data = battery_power_data.get("master", {})
    battery_slave_data = battery_power_data.get("slave", {})

    power_checks = [
        ("AC Power Master", ac_power_data.get("master", 0)),
        ("AC Power Slave", ac_power_data.get("slave", 0)),
        (
            "Battery Power Master",
            battery_master_data.get("power", 0)
            if isinstance(battery_master_data, dict)
            else battery_master_data or 0,
        ),
        (
            "Battery Power Slave",
            battery_slave_data.get("power", 0)
            if isinstance(battery_slave_data, dict)
            else battery_slave_data or 0,
        ),
        ("Grid Power Master", grid_power_data.get("master", 0)),
        ("Grid Power Slave", grid_power_data.get("slave", 0)),
    ]

    for power_name, power_watts in power_checks:
        if power_watts is None:
            continue  # Skip missing values (slave inverter might not exist)

        power_kw = abs(power_watts) / 1000.0

        if power_kw > MAX_POWER_KW:
            result.add_error(
                f"{power_name}: {power_kw:.2f}kW exceeds {MAX_POWER_KW}kW physical limit (100A supply)"
            )
        elif power_kw > WARNING_POWER_KW:
            result.add_warning(f"{power_name}: {power_kw:.2f}kW approaching {MAX_POWER_KW}kW limit")

    return result


def read_individual_registers_comprehensive(config: dict[str, Any]) -> dict[str, Any] | None:
    """Fallback function using individual register reads for all required data.

    This function replicates the bulk data structure using individual function calls
    that are known to work correctly (proper signed conversion). Used as a reliable
    fallback when bulk data validation fails due to power spike detection.

    Args:
        config: Configuration dictionary with Modbus settings

    Returns:
        Dictionary with same structure as solax_modbus_bulk_data() or None if failed
        Structure includes data_source field set to 'individual_modbus_fallback'

    Performance Note:
        This function is significantly slower than bulk reads (~12 seconds vs 1.2 seconds)
        but provides guaranteed reliable data when bulk operations encounter errors.

    """
    try:
        logger.debug("Starting comprehensive individual register reads")

        # Import the individual functions we need from the main client module
        # Note: This creates a circular import risk, so this function should be called
        # from solax_modbus_client.py, not imported elsewhere
        from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            solax_modbus_client,  # pylint: disable=import-outside-toplevel
        )

        # Use existing individual functions that already work correctly with proper signed conversion
        ac_power_data = solax_modbus_client.solax_modbus_ac_power(config)
        grid_power_data = solax_modbus_client.solax_modbus_grid_power(config)
        battery_power_data = solax_modbus_client.solax_modbus_battery_power(config)
        pv_power_data = solax_modbus_client.solax_modbus_pv_power(config)
        soc_data = solax_modbus_client.solax_modbus_soc(config)
        battery_temperature_data = solax_modbus_client.solax_modbus_battery_temperature(config)

        # Note: Not including work mode, serial numbers, daily yield, etc. for safety
        # Focus only on power validation critical data for fallback scenario

        # Add work mode data - critical for web interface and daemon functionality
        work_mode_enum = solax_modbus_client.solax_modbus_work_mode(config)
        # Note: work_mode_enum is BatteryMode enum or None - matches bulk read data type

        # Add grid totals data - critical for LivePowerMonitor functionality
        grid_totals_data = solax_modbus_client.solax_modbus_grid_totals(config)

        # Structure data to match bulk format for validation compatibility
        individual_data = {
            "ac_power": ac_power_data or {},
            "grid_power": grid_power_data or {},
            "battery_power": battery_power_data or {},
            "pv_power": pv_power_data or {},
            "soc": soc_data or {},
            "battery_temperature": battery_temperature_data or {},
            "work_mode": work_mode_enum,  # BatteryMode enum or None - matches bulk read type
            "grid_totals": grid_totals_data or {},  # Grid import/export cumulative totals
            "data_source": "individual_modbus_fallback",
        }

        logger.info("Successfully completed comprehensive individual register reads")
        logger.debug("Individual fallback data contains %s categories", len(individual_data))
    except (ImportError, AttributeError):
        # Module import or attribute access failed - should not happen in normal operation
        logger.exception("Failed to import required solax_modbus_client functions")
        return None
    except KeyError:
        # Configuration missing required keys
        logger.exception("Configuration error during individual register reads")
        return None
    return individual_data
