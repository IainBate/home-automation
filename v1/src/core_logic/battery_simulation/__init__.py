"""Battery Simulation Module.

This module provides battery mode definitions for the SolaX controller.

MINIMAL EXTRACTION:
This is a minimal extraction containing only the BatteryMode enum and related
constants needed for Modbus operations. The full simulation and optimization
modules are not included.
"""

# Re-export constants and models needed for Modbus operations
from .constants_and_models import (
    # BatteryMode enum (CRITICAL - Enum Boundary Pattern!)
    BatteryMode,
    # Conversion function
    battery_mode_to_display_string,
    # Constants that may be referenced
    MIN_SOC_PERCENT,
    MAX_SOC_PERCENT,
)

# Define __all__ for explicit public API
__all__ = [
    "BatteryMode",
    "battery_mode_to_display_string",
    "MIN_SOC_PERCENT",
    "MAX_SOC_PERCENT",
]
