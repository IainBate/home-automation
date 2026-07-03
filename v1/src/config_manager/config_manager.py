"""Configuration Manager Module.

This module handles loading and validating static configuration from a YAML file.
Provides comprehensive schema validation to catch configuration errors early.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TypedDict

import jsonschema
import yaml
from jsonschema import ValidationError

# Setup basic logging
logger = logging.getLogger(__name__)

# Validation thresholds for business rules
BATTERY_CAPACITY_DIFFERENCE_WARNING = 1.0  # kWh difference threshold for warning
BATTERY_EFFICIENCY_LOW_THRESHOLD = 85  # Percent - below this is unusually low
API_TIMEOUT_LOW_WARNING = 10  # Seconds - below this is very short
SOLCAST_CALLS_HIGH_WARNING = 10  # Calls per day - above this check rate limits


class ConfigValidationResult(TypedDict):
    """Result of configuration validation."""

    is_valid: bool
    errors: list[str]
    warnings: list[str]


# Comprehensive configuration schema for validation
CONFIG_SCHEMA = {
    "type": "object",
    "required": [
        "solaX_cloud_api",
        "battery_system",
        "financial_costs",
        "household_load",
        "car_charging",
        "api_settings",
        "location",
        "system_settings",
        "web_interface",
        "logging",
    ],
    "properties": {
        "solaX_cloud_api": {
            "type": "object",
            "required": [
                "base_url",
                "token_id",
                "master_wifisn",
                "master_ip",
                "slave_wifisn",
                "slave_ip",
            ],
            "properties": {
                "base_url": {"type": "string", "pattern": "^https?://"},
                "token_id": {"type": "string", "minLength": 1},
                "master_wifisn": {"type": "string", "minLength": 1},
                "master_ip": {"type": "string", "pattern": "^(?:[0-9]{1,3}\\.){3}[0-9]{1,3}$"},
                "slave_wifisn": {"type": "string", "minLength": 1},
                "slave_ip": {"type": "string", "pattern": "^(?:[0-9]{1,3}\\.){3}[0-9]{1,3}$"},
                "modbus_enabled": {"type": "boolean"},
                "modbus_port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "modbus_connection_timeout": {"type": "number", "minimum": 1},
                "modbus_read_timeout": {"type": "number", "minimum": 1},
                "master_modbus_address": {"type": "integer", "minimum": 1, "maximum": 247},
                "slave_modbus_address": {"type": "integer", "minimum": 1, "maximum": 247},
                "min_command_interval": {"type": "number", "minimum": 0},
            },
        },
        "battery_system": {
            "type": "object",
            "required": ["master_capacity_kwh", "slave_capacity_kwh", "absolute_min_soc_percent"],
            "properties": {
                "master_capacity_kwh": {"type": "number", "minimum": 0.1, "maximum": 100},
                "slave_capacity_kwh": {"type": "number", "minimum": 0.1, "maximum": 100},
                "absolute_min_soc_percent": {"type": "number", "minimum": 0, "maximum": 50},
                "standby_power_threshold_w": {"type": "number", "minimum": 0},
                "simulation": {
                    "type": "object",
                    "required": [
                        "enabled",
                        "charge_efficiency_percent",
                        "discharge_efficiency_percent",
                    ],
                    "properties": {
                        "enabled": {"type": "boolean"},
                        "charge_efficiency_percent": {
                            "type": "number",
                            "minimum": 50,
                            "maximum": 100,
                        },
                        "discharge_efficiency_percent": {
                            "type": "number",
                            "minimum": 50,
                            "maximum": 100,
                        },
                        "max_charge_rate_kw": {"type": "number", "minimum": 0.1, "maximum": 50},
                        "max_discharge_rate_kw": {"type": "number", "minimum": 0.1, "maximum": 50},
                        "self_discharge_percent_per_hour": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "soc_charge_taper_threshold_percent": {
                            "type": "number",
                            "minimum": 80,
                            "maximum": 100,
                        },
                        "soc_charge_taper_rate_percent": {
                            "type": "number",
                            "minimum": 10,
                            "maximum": 90,
                        },
                    },
                },
            },
        },
        "financial_costs": {
            "type": "object",
            "required": ["fixed_export_price_per_kwh"],
            "properties": {
                "fixed_export_price_per_kwh": {"type": "number", "minimum": 0, "maximum": 1},
                "battery_cycle_cost_per_kwh": {"type": "number", "minimum": 0, "maximum": 0.5},
                "cheap_charge_threshold_per_kwh": {"type": "number", "minimum": 0, "maximum": 1},
                "high_price_reserve_threshold_per_kwh": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 2,
                },
            },
        },
        "household_load": {
            "type": "object",
            "required": ["base_load_daytime_kw", "base_load_nighttime_kw"],
            "properties": {
                "base_load_daytime_kw": {"type": "number", "minimum": 0, "maximum": 20},
                "base_load_nighttime_kw": {"type": "number", "minimum": 0, "maximum": 20},
                "daytime_start_hour": {"type": "integer", "minimum": 0, "maximum": 23},
                "daytime_end_hour": {"type": "integer", "minimum": 0, "maximum": 23},
                "hot_water_power_kw": {"type": "number", "minimum": 0, "maximum": 10},
                "appliance_power_kw": {"type": "number", "minimum": 0, "maximum": 5},
            },
        },
        "car_charging": {
            "type": "object",
            "required": ["charger_demand_kw"],
            "properties": {
                "charger_demand_kw": {"type": "number", "minimum": 1.5, "maximum": 24.0}
            },
        },
        "api_settings": {
            "type": "object",
            "required": ["timeout_seconds"],
            "properties": {
                "agilepredict_endpoint": {"type": "string", "pattern": "^https?://"},
                "octopus_api_key": {"type": "string", "minLength": 1},
                "octopus_product_code": {"type": "string", "minLength": 1},
                "octopus_tariff_region_code": {"type": "string", "minLength": 1},
                "solcast_rooftop_resource_id": {"type": "string", "minLength": 1},
                "solcast_api_key": {"type": "string", "minLength": 1},
                "timeout_seconds": {"type": "number", "minimum": 5, "maximum": 300},
                "calls_per_day": {"type": "integer", "minimum": 1, "maximum": 50},
                "solcast_cache": {
                    "type": "object",
                    "properties": {
                        "enabled": {"type": "boolean"},
                        "cache_duration_hours": {"type": "number", "minimum": 0.5, "maximum": 24},
                        "cache_file_path": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
        "location": {
            "type": "object",
            "required": ["default_timezone_str"],
            "properties": {
                "default_timezone_str": {"type": "string", "minLength": 1},
                "city_name": {"type": "string", "minLength": 1},
                "country_name": {"type": "string", "minLength": 1},
            },
        },
        "system_settings": {
            "type": "object",
            "properties": {
                "max_inverter_output_from_own_sources_kw": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 50,
                },
                "significant_pv_threshold_kw": {"type": "number", "minimum": 0.1, "maximum": 10},
                "min_significant_pv_duration_hours": {
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 12,
                },
                "min_cheap_charge_duration_hours": {
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 12,
                },
                "file_check_interval_seconds": {"type": "integer", "minimum": 10, "maximum": 3600},
                "main_logic_interval_minutes": {"type": "integer", "minimum": 5, "maximum": 180},
                "cheap_period_top_up_window_hours": {"type": "number", "minimum": 1, "maximum": 12},
                "daily_input_file_path": {"type": "string"},
            },
        },
        "web_interface": {
            "type": "object",
            "required": ["enabled", "host", "port"],
            "properties": {
                "enabled": {"type": "boolean"},
                "host": {"type": "string", "minLength": 1},
                "port": {"type": "integer", "minimum": 1000, "maximum": 65535},
                "debug_mode": {"type": "boolean"},
            },
        },
        "logging": {
            "type": "object",
            "required": ["console_level", "file_level"],
            "properties": {
                "console_level": {
                    "type": "string",
                    "enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                },
                "file_level": {
                    "type": "string",
                    "enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                },
            },
        },
    },
}


def validate_config_schema(config_data: dict[str, Any]) -> list[str]:
    """Validate configuration against schema, return list of validation errors.

    Args:
        config_data: Configuration dictionary to validate

    Returns:
        List of validation error messages (empty list if valid)

    """
    errors = []

    try:
        # Validate against JSON schema
        jsonschema.validate(config_data, CONFIG_SCHEMA)
        logger.debug("Configuration schema validation passed")

    except ValidationError as e:
        # Extract meaningful error message
        error_path = " -> ".join(str(p) for p in e.absolute_path) if e.absolute_path else "root"
        error_msg = f"Configuration error at '{error_path}': {e.message}"
        errors.append(error_msg)
        logger.exception("Schema validation failed: %s", error_msg)

        # Add additional context for common errors
        if "is a required property" in e.message:
            missing_field = e.message.split("'")[1] if "'" in e.message else "unknown"
            errors.append(f"  Hint: Add the missing '{missing_field}' field to your configuration")
        elif "is not of type" in e.message:
            expected_type = e.schema.get("type", "specific type")
            errors.append(f"  Hint: Check the data type of the value - expected {expected_type}")
        elif "does not match" in e.message and "pattern" in str(e.schema):
            errors.append("  Hint: Value format is invalid - check the pattern requirements")

    except (AttributeError, KeyError, IndexError, TypeError) as e:
        # Catches errors from accessing ValidationError attributes (e.message, e.schema, etc.)
        error_msg = f"Unexpected validation error: {e!s}"
        errors.append(error_msg)
        logger.exception(error_msg)

    return errors


def validate_business_rules(  # pylint: disable=too-many-locals
    config_data: dict[str, Any],
) -> list[str]:
    """Validate business rules and logical consistency beyond schema validation.

    Justification for too-many-locals (21/20): Configuration validation involves extracting
    battery config, pricing config, device configs, and performing cross-validation checks.
    All variables serve distinct purposes in sequential validation logic.

    Args:
        config_data: Configuration dictionary to validate

    Returns:
        List of business rule violation messages (empty list if valid)

    """
    warnings = []

    try:
        # Battery capacity consistency
        battery_config = config_data.get("battery_system", {})
        master_capacity = battery_config.get("master_capacity_kwh", 0)
        slave_capacity = battery_config.get("slave_capacity_kwh", 0)

        if abs(master_capacity - slave_capacity) > BATTERY_CAPACITY_DIFFERENCE_WARNING:
            warnings.append(
                "Warning: Master and slave battery capacities differ significantly - is this intentional?"
            )

        # Household load consistency
        household_config = config_data.get("household_load", {})
        daytime_load = household_config.get("base_load_daytime_kw", 0)
        nighttime_load = household_config.get("base_load_nighttime_kw", 0)

        if daytime_load < nighttime_load:
            warnings.append("Warning: Daytime load is less than nighttime load - this is unusual")

        # Time range validation
        daytime_start = household_config.get("daytime_start_hour", 7)
        daytime_end = household_config.get("daytime_end_hour", 23)

        if daytime_start >= daytime_end:
            warnings.append("Error: Daytime start hour must be before daytime end hour")

        # Financial thresholds consistency
        financial_config = config_data.get("financial_costs", {})
        export_price = financial_config.get("fixed_export_price_per_kwh", 0)
        cheap_threshold = financial_config.get("cheap_charge_threshold_per_kwh", 0)
        high_threshold = financial_config.get("high_price_reserve_threshold_per_kwh", 1)

        if cheap_threshold >= high_threshold:
            warnings.append(
                "Warning: Cheap charge threshold should be less than high price reserve threshold"
            )

        if cheap_threshold > export_price:
            warnings.append(
                "Warning: Cheap charge threshold is higher than export price - this will cause losses by importing expensive energy to potentially export at a lower price"
            )

        # Battery simulation parameters
        simulation_config = battery_config.get("simulation", {})
        charge_efficiency = simulation_config.get("charge_efficiency_percent", 100)
        discharge_efficiency = simulation_config.get("discharge_efficiency_percent", 100)

        if (
            charge_efficiency < BATTERY_EFFICIENCY_LOW_THRESHOLD
            or discharge_efficiency < BATTERY_EFFICIENCY_LOW_THRESHOLD
        ):
            warnings.append(
                "Warning: Battery efficiency below 85% is unusually low for modern systems"
            )

        # API settings validation
        api_config = config_data.get("api_settings", {})
        timeout = api_config.get("timeout_seconds", 30)
        calls_per_day = api_config.get("calls_per_day", 8)

        if timeout < API_TIMEOUT_LOW_WARNING:
            warnings.append("Warning: API timeout is very short - may cause request failures")

        if calls_per_day > SOLCAST_CALLS_HIGH_WARNING:
            warnings.append("Warning: High number of Solcast API calls per day - check rate limits")

    except (KeyError, ValueError, TypeError, AttributeError) as e:
        warnings.append(f"Error validating business rules: {e!s}")
        logger.exception("Business rule validation error")

    return warnings


def load_static_config(config_file_path: str) -> dict[str, Any] | None:
    """Load and validate static configuration parameters from a YAML file.

    This function now includes comprehensive schema validation and business rule checking
    to catch configuration errors early and provide helpful error messages.

    Args:
        config_file_path: Path to the YAML configuration file

    Returns:
        Dictionary containing the configuration parameters or None if validation fails

    """
    try:
        # Check if file exists
        if not Path(config_file_path).exists():
            logger.error("Configuration file not found: %s", config_file_path)
            return None

        # Open and parse the YAML file
        with Path(config_file_path).open(encoding="utf-8") as file:
            config = yaml.safe_load(file)

        if config is None:
            logger.error("Configuration file is empty or invalid: %s", config_file_path)
            return None

        logger.debug("Successfully loaded YAML from %s", config_file_path)

        # Perform comprehensive schema validation
        schema_errors = validate_config_schema(config)
        if schema_errors:
            logger.error("Configuration schema validation failed:")
            for error in schema_errors:
                logger.error("  %s", error)
            return None

        # Perform business rule validation (warnings only)
        business_warnings = validate_business_rules(config)
        if business_warnings:
            logger.warning("Configuration business rule validation warnings:")
            for warning in business_warnings:
                logger.warning("  %s", warning)

        # Calculate derived values (validated by schema)
        config["battery_system"]["total_capacity_kwh"] = (
            config["battery_system"]["master_capacity_kwh"]
            + config["battery_system"]["slave_capacity_kwh"]
        )

        logger.info("Configuration successfully loaded and validated from %s", config_file_path)

    except yaml.YAMLError:
        logger.exception("Error parsing YAML configuration file")
        logger.info(
            "Please check YAML syntax - common issues include incorrect indentation or special characters"
        )
        return None
    except (OSError, KeyError, TypeError, ValueError):
        # File operations, dictionary access, and calculation errors
        logger.exception("Unexpected error loading configuration")
        return None

    return config
