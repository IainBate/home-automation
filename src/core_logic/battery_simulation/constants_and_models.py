"""Battery Simulation Constants and Data Models.

Part of battery_simulation split for LLM-optimal file sizes.

This module contains:
- Configuration constants
- BatteryMode enum (CRITICAL - Enum Boundary Pattern!)
- Data classes for simulation input/output
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from src.utils.exceptions import ValidationError

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)

# Configuration constants
MIN_SOC_PERCENT = 0.0
MAX_SOC_PERCENT = 100.0
MINUTES_PER_30MIN_SLOT = 30
SECONDS_PER_HOUR = 3600.0
PERCENT_TO_FRACTION = 100.0

# Validation tolerances
ENERGY_CONSERVATION_TOLERANCE_KWH = 0.01  # Allow 0.01 kWh tolerance for rounding errors

# Partial charge limits
PARTIAL_CHARGE_MIN_MINUTES = 1
PARTIAL_CHARGE_MAX_MINUTES = 29

# Configuration path constants
CONFIG_BATTERY_SYSTEM = "battery_system"
CONFIG_SIMULATION = "simulation"
CONFIG_MASTER_CAPACITY = "master_capacity_kwh"
CONFIG_SLAVE_CAPACITY = "slave_capacity_kwh"
CONFIG_MIN_SOC = "absolute_min_soc_percent"
CONFIG_CHARGE_EFFICIENCY = "charge_efficiency_percent"
CONFIG_DISCHARGE_EFFICIENCY = "discharge_efficiency_percent"
CONFIG_MAX_CHARGE_RATE = "max_charge_rate_kw"
CONFIG_MAX_DISCHARGE_RATE = "max_discharge_rate_kw"
CONFIG_SELF_DISCHARGE_RATE = "self_discharge_percent_per_hour"
CONFIG_CHARGE_TAPER_THRESHOLD = "soc_charge_taper_threshold_percent"
CONFIG_CHARGE_TAPER_RATE = "soc_charge_taper_rate_percent"
CONFIG_FINANCIAL_COSTS = "financial_costs"
CONFIG_EXPORT_PRICE = "fixed_export_price_per_kwh"

# Default values
DEFAULT_EXPORT_PRICE = 0.15


class BatteryMode(Enum):
    """Battery operating modes for simulation and hardware interface.

    This enum serves as the EXCLUSIVE internal representation of battery modes throughout the entire codebase.
    It consolidates all previous string-based mode representations into a single, type-safe enum.

    CRITICAL DESIGN PRINCIPLES:
    1. This enum is the ONLY internal representation - no string modes should exist elsewhere
    2. Conversion to/from this enum occurs in EXACTLY TWO PLACES:
       a) Hardware interface (Modbus numeric values) - see solax_modbus_client.py
       b) User-facing output (display strings) - see battery_mode_to_display_string()
    3. All internal code MUST use BatteryMode enum values directly
    4. No other conversion functions should exist outside these two boundary points

    REFACTORING NOTE: This design eliminates the previous inconsistent string representations
    (self-use, charging, discharging, etc.) that were scattered throughout the codebase.
    """

    # Modes we actively write to hardware (primary operational modes)
    SELF_USE = "SELF_USE"  # Default mode - battery follows natural charge/discharge
    FORCE_CHARGE = "FORCE_CHARGE"  # Force battery to charge from grid
    FORCE_DISCHARGE = "FORCE_DISCHARGE"  # Force battery to discharge to grid
    MANUAL_STOP = "MANUAL_STOP"  # Work mode 3, Manual mode 0: Manual Stop - battery disconnected, PV+grid only

    # Modes we read from hardware but never write (read-only hardware states)
    FEED_IN_PRIORITY = "FEED_IN_PRIORITY"  # Work mode 1: Feed-in Priority - export excess to grid
    BACKUP = "BACKUP"  # Work mode 2: Backup - reserve battery for emergencies
    PEAK_SHAVING = "PEAK_SHAVING"  # Work mode 4: Peak Shaving - reduce peak consumption
    TOU_MODE = "TOU_MODE"  # Work mode 5: TOU Mode - time of use optimization
    SMART_SCHEDULE = "SMART_SCHEDULE"  # Work mode 6: Smart Schedule - pre-programmed schedule
    UNKNOWN_WORK_MODE = "UNKNOWN_WORK_MODE"  # For work mode values 7+ (future hardware modes)

    # Simulation-only modes (never appear in hardware, used for modeling purposes)
    IDLE = "IDLE"  # Battery neither charging nor discharging significantly
    PARTIAL_CHARGE = "PARTIAL_CHARGE"  # Charge for partial period (1-29 minutes)


def battery_mode_to_display_string(
    mode: BatteryMode, partial_charge_minutes: int | None = None
) -> str:
    """Convert BatteryMode enum to standardized user-friendly display string.

    CRITICAL: This is the SINGLE canonical function for enum-to-string conversion throughout the entire codebase.
    This is one of only TWO places where BatteryMode enum values are converted (the other being hardware interface).

    REFACTORING PRINCIPLE: All user-facing outputs (logs, JSON, web interface, CLI output, reports, etc.)
    MUST use this function. No other string conversion should exist anywhere else in the codebase.

    This function eliminates the previous inconsistent string representations that existed across different
    modules and ensures uniform user experience.

    Args:
        mode: BatteryMode enum to convert to display string
        partial_charge_minutes: Optional minutes for partial charging (1-29), only used with PARTIAL_CHARGE mode

    Returns:
        Standardized user-friendly display string for all user-facing outputs

    Raises:
        None - Unknown modes fall back to "Self-Use" for safety

    """
    if mode == BatteryMode.PARTIAL_CHARGE:
        if partial_charge_minutes is not None:
            return f"Partial Charge ({partial_charge_minutes} mins)"
        func_logger = logging.getLogger(__name__)
        func_logger.warning(
            "PARTIAL_CHARGE mode with missing minutes - this indicates invalid data or validation failure"
        )
        return "Partial Charge"

    # Canonical 1:1 mapping - each enum has exactly one standardized display string
    display_mapping = {
        BatteryMode.SELF_USE: "Self-Use",
        BatteryMode.FORCE_CHARGE: "Charging",
        BatteryMode.FORCE_DISCHARGE: "Discharging",
        BatteryMode.IDLE: "Idle",
        BatteryMode.MANUAL_STOP: "Holding",
        BatteryMode.FEED_IN_PRIORITY: "Feed-in Priority",
        BatteryMode.BACKUP: "Backup",
        BatteryMode.PEAK_SHAVING: "Peak Shaving",
        BatteryMode.TOU_MODE: "TOU Mode",
        BatteryMode.SMART_SCHEDULE: "Smart Schedule",
        BatteryMode.UNKNOWN_WORK_MODE: "Unknown Mode",
    }
    return display_mapping.get(mode, "Self-Use")


@dataclass
class BatterySimulationPeriod:
    """Input data for a single simulation period (typically 30 minutes)."""

    start_time_utc: datetime
    end_time_utc: datetime

    # Operating mode for this period
    battery_mode: BatteryMode

    # Energy situation for this period (average kW over the period)
    pv_generation_kw: float
    house_background_load_kw: float
    appliance_load_kw: float

    # Economic context
    electricity_price_gbp_per_kwh: float

    # NEW: Partial charging support
    partial_charge_minutes: int | None = None  # 1-29, only used with PARTIAL_CHARGE mode

    # Temperature data (for thermal model integration - BT6)
    battery_temp_celsius: float | None = (
        None  # Battery temperature predicted by thermal model (BT4), None if unavailable
    )
    outdoor_temp_celsius: float | None = (
        None  # Outdoor temperature from weather forecast (BT3), None if unavailable
    )

    def __post_init__(self) -> None:
        """Validate inputs."""
        if self.end_time_utc <= self.start_time_utc:
            msg = "End time must be after start time"
            raise ValidationError(
                msg,
                field_name="time_range",
                field_value=f"{self.start_time_utc} to {self.end_time_utc}",
                error_code="INVALID_TIME_RANGE",
            )
        if self.pv_generation_kw < 0:
            msg = "PV generation cannot be negative"
            raise ValidationError(
                msg,
                field_name="pv_generation_kw",
                field_value=self.pv_generation_kw,
                error_code="NEGATIVE_PV_GENERATION",
            )
        if self.house_background_load_kw < 0:
            msg = "House background load cannot be negative"
            raise ValidationError(
                msg,
                field_name="house_background_load_kw",
                field_value=self.house_background_load_kw,
                error_code="NEGATIVE_HOUSE_LOAD",
            )
        if self.appliance_load_kw < 0:
            msg = "Appliance load cannot be negative"
            raise ValidationError(
                msg,
                field_name="appliance_load_kw",
                field_value=self.appliance_load_kw,
                error_code="NEGATIVE_APPLIANCE_LOAD",
            )

    @property
    def duration_hours(self) -> float:
        """Calculate period duration in hours."""
        return (self.end_time_utc - self.start_time_utc).total_seconds() / SECONDS_PER_HOUR


@dataclass
class BatterySimulationResult:  # pylint: disable=too-many-instance-attributes
    # Justification: This is a data model representing complete simulation results.
    # All 15 attributes are necessary and distinct. Splitting into nested objects
    # would complicate access patterns throughout the codebase without benefit.
    """Output data for a single simulation period."""

    period: BatterySimulationPeriod

    # SoC progression
    starting_soc_percent: float
    ending_soc_percent: float
    soc_change_percent: float

    # Energy flows (kW averages over the period)
    battery_charge_kw: float  # Positive when charging
    battery_discharge_kw: float  # Positive when discharging
    grid_import_kw: float  # Positive for import, negative for export
    battery_efficiency_used: float

    # Energy calculations (kWh over the period)
    energy_stored_kwh: float  # Energy added to battery
    energy_discharged_kwh: float  # Energy removed from battery
    energy_balance_kwh: float  # Net energy flow (positive = surplus)

    # Economic impact
    grid_cost_gbp: float  # Cost of grid import (negative for export income)

    # Validation
    is_valid: bool
    error_message: str | None = None
    warnings: list[str] = None

    def __post_init__(self) -> None:
        """Initialize warnings list if not provided."""
        if self.warnings is None:
            self.warnings = []
