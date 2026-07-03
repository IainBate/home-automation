"""Custom Exception Types for Solar Energy Management System.

This module defines domain-specific exception classes to provide more
precise error handling and better error context throughout the system.
"""

from __future__ import annotations


class SolarSystemError(Exception):
    """Base exception class for all solar system related errors."""

    def __init__(
        self,
        message: str,
        error_code: str | None = None,
        context: dict[str, object] | None = None,
    ) -> None:
        """Initialize solar system error.

        Args:
            message: Human-readable error description
            error_code: Optional machine-readable error code
            context: Optional additional context data

        """
        super().__init__(message)
        self.error_code = error_code
        self.context = context or {}


class BatterySimulationError(SolarSystemError):
    """Exception raised for battery simulation related errors."""

    def __init__(
        self,
        message: str,
        soc_value: float | None = None,
        power_value: float | None = None,
        **kwargs: object,
    ) -> None:
        """Initialize battery simulation error.

        Args:
            message: Human-readable error description
            soc_value: State of charge value when error occurred
            power_value: Power value when error occurred
            **kwargs: Additional context passed to base class

        """
        context = kwargs.get("context", {})
        if soc_value is not None:
            context["soc_value"] = soc_value
        if power_value is not None:
            context["power_value"] = power_value
        kwargs["context"] = context
        super().__init__(message, **kwargs)


_SENTINEL = object()  # Sentinel value to detect when parameter was explicitly passed


class ConfigurationError(SolarSystemError):
    """Exception raised for configuration validation errors."""

    def __init__(
        self,
        message: str,
        config_key: str | None = None,
        config_value: object = _SENTINEL,
        **kwargs: object,
    ) -> None:
        """Initialize configuration error.

        Args:
            message: Human-readable error description
            config_key: Configuration key that caused the error
            config_value: Configuration value that was invalid
            **kwargs: Additional context passed to base class

        """
        context = kwargs.get("context", {})
        if config_key is not None:
            context["config_key"] = config_key
        if config_value is not _SENTINEL:
            context["config_value"] = config_value
        kwargs["context"] = context
        super().__init__(message, **kwargs)


class DataSourceError(SolarSystemError):
    """Exception raised for data source and API related errors."""

    def __init__(
        self,
        message: str,
        data_source: str | None = None,
        http_status: int | None = None,
        **kwargs: object,
    ) -> None:
        """Initialize data source error.

        Args:
            message: Human-readable error description
            data_source: Name of the data source that failed
            http_status: HTTP status code if applicable
            **kwargs: Additional context passed to base class

        """
        context = kwargs.get("context", {})
        if data_source is not None:
            context["data_source"] = data_source
        if http_status is not None:
            context["http_status"] = http_status
        kwargs["context"] = context
        super().__init__(message, **kwargs)


class OptimizationError(SolarSystemError):
    """Exception raised for optimization algorithm errors."""

    def __init__(
        self,
        message: str,
        algorithm: str | None = None,
        slot_id: int | None = None,
        **kwargs: object,
    ) -> None:
        """Initialize optimization error.

        Args:
            message: Human-readable error description
            algorithm: Name of the optimization algorithm that failed
            slot_id: Time slot ID where error occurred
            **kwargs: Additional context passed to base class

        """
        context = kwargs.get("context", {})
        if algorithm is not None:
            context["algorithm"] = algorithm
        if slot_id is not None:
            context["slot_id"] = slot_id
        kwargs["context"] = context
        super().__init__(message, **kwargs)


class ValidationError(SolarSystemError):
    """Exception raised for data validation errors."""

    def __init__(
        self,
        message: str,
        field_name: str | None = None,
        field_value: object | None = None,
        **kwargs: object,
    ) -> None:
        """Initialize validation error.

        Args:
            message: Human-readable error description
            field_name: Name of the field that failed validation
            field_value: Value that failed validation
            **kwargs: Additional context passed to base class

        """
        context = kwargs.get("context", {})
        if field_name is not None:
            context["field_name"] = field_name
        if field_value is not None:
            context["field_value"] = field_value
        kwargs["context"] = context
        super().__init__(message, **kwargs)


class ModbusError(DataSourceError):
    """Exception raised for Modbus communication errors."""

    def __init__(
        self,
        message: str,
        register_address: int | None = None,
        device_ip: str | None = None,
        **kwargs: object,
    ) -> None:
        """Initialize Modbus error.

        Args:
            message: Human-readable error description
            register_address: Modbus register address that failed
            device_ip: IP address of the device
            **kwargs: Additional context passed to base class

        """
        context = kwargs.get("context", {})
        if register_address is not None:
            context["register_address"] = register_address
        if device_ip is not None:
            context["device_ip"] = device_ip
        kwargs["context"] = context
        kwargs["data_source"] = "modbus"
        super().__init__(message, **kwargs)


class JSONGenerationError(SolarSystemError):
    """Exception raised for JSON generation errors."""

    def __init__(
        self,
        message: str,
        generation_stage: str | None = None,
        data_type: str | None = None,
        **kwargs: object,
    ) -> None:
        """Initialize JSON generation error.

        Args:
            message: Human-readable error description
            generation_stage: Stage of JSON generation that failed
            data_type: Type of data being processed
            **kwargs: Additional context passed to base class

        """
        context = kwargs.get("context", {})
        if generation_stage is not None:
            context["generation_stage"] = generation_stage
        if data_type is not None:
            context["data_type"] = data_type
        kwargs["context"] = context
        super().__init__(message, **kwargs)
