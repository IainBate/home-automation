"""Custom Exception Types for Solar Energy Management System.

Deliberately small. This module used to define eight exception classes -
SolarSystemError, BatterySimulationError, ConfigurationError,
DataSourceError, OptimizationError, ModbusError, JSONGenerationError and
ValidationError - of which seven were never raised or caught anywhere in
src/ or scripts/. They described a different (and larger) system than the one
this repo actually is: OptimizationError in particular belonged to the
"optimizer" that ohme_charging_logic.py's docstring also used to reference
and which has never existed here.

They were removed rather than kept "in case", because unused error taxonomy
is actively misleading: it suggests a raise-and-catch error strategy, when
this codebase deliberately uses the opposite convention - hardware/API
functions return None on failure and daemons wrap checks in broad
except-and-log Circuit Breakers (see CLAUDE.md).

ValidationError stays because it is genuinely used, by
core_logic/battery_simulation/constants_and_models.py.
"""

from __future__ import annotations


class ValidationError(Exception):
    """Raised when a value fails a domain validation rule.

    Note the name deliberately collides with jsonschema.ValidationError,
    which config_manager.py and battery_mode_daemon.py import for config
    schema validation. They are unrelated: this one is for battery
    simulation inputs. Import one or the other in a given module, never both
    unqualified.

    Attributes:
        error_code: Optional machine-readable code for the failure.
        context: Optional structured detail about what failed, including
            field_name/field_value when the caller supplies them.

    """

    def __init__(
        self,
        message: str,
        field_name: str | None = None,
        field_value: object | None = None,
        error_code: str | None = None,
        context: dict[str, object] | None = None,
    ) -> None:
        """Initialize validation error.

        Args:
            message: Human-readable error description
            field_name: Name of the field that failed validation
            field_value: Value that failed validation
            error_code: Optional machine-readable error code
            context: Optional additional context data

        """
        super().__init__(message)
        self.error_code = error_code
        self.context = dict(context or {})
        if field_name is not None:
            self.context["field_name"] = field_name
        if field_value is not None:
            self.context["field_value"] = field_value
