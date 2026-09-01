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

# Credentials (ohme_ev.username/password, melcloud.email/password, ...) live in this
# file instead of config.yaml, so config.yaml stays safe to commit/pull normally.
# Looked for next to whatever config file is being loaded; entirely optional - its
# absence just means config.yaml's own values (real or placeholder) are used as-is.
SECRETS_FILENAME = "secrets.yaml"


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
                "latitude": {"type": "number", "minimum": -90, "maximum": 90},
                "longitude": {"type": "number", "minimum": -180, "maximum": 180},
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
                "poll_interval_seconds": {"type": "integer", "minimum": 5},
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
        # melcloud and hotwater_automation are optional integrations (like
        # ohme_ev): not in the top-level "required" list, so they're fine
        # absent entirely, but validated if present to catch typos in the
        # numeric/boolean tuning fields early rather than failing deep inside
        # melcloud_client.py or hotwater_decision_logic.py at runtime.
        "melcloud": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "email": {"type": "string", "minLength": 1},
                "password": {"type": "string", "minLength": 1},
                "device_name": {"type": "string"},
                "mode_change_retry": {
                    "type": "object",
                    "properties": {
                        "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
                        "check_delay_seconds": {"type": "number", "minimum": 1, "maximum": 120},
                    },
                },
            },
            # email/password only need to be non-empty when enabled: true - an
            # unconfigured-but-disabled melcloud section (the shipped default)
            # must stay valid, matching every other optional integration here.
            "if": {"properties": {"enabled": {"const": True}}, "required": ["enabled"]},
            "then": {"required": ["email", "password"]},
        },
        "hotwater_automation": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "trigger_hour": {"type": "number", "minimum": 0, "maximum": 23.99},
                "ohme_charging_threshold_watts": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 10000,
                },
                "tank_temp_threshold_c": {"type": "number", "minimum": 30, "maximum": 60},
                "battery_soc_min_percent": {"type": "number", "minimum": 0, "maximum": 100},
                "offpeak_start": {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
                "offpeak_end": {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
                "force_heat_max_duration_hours": {
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 24,
                },
                "poll_interval_seconds": {"type": "number", "minimum": 60, "maximum": 3600},
                "revert_check_interval_seconds": {
                    "type": "number",
                    "minimum": 300,
                    "maximum": 21600,
                },
                "legionella_interval_days": {"type": "integer", "minimum": 7, "maximum": 365},
                "legionella_target_temp_c": {"type": "number", "minimum": 50, "maximum": 65},
                "legionella_max_cycle_duration_hours": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 24,
                },
                "max_prediction_age_hours": {
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 24,
                },
            },
        },
        "battery_evening_prediction": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "trigger_hour": {"type": "number", "minimum": 0, "maximum": 23.99},
                "horizon_hours": {"type": "number", "minimum": 0.5, "maximum": 24},
                "min_sample_days": {"type": "integer", "minimum": 1, "maximum": 60},
            },
        },
        "solar_forecast": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
            },
        },
        "airstage": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 60},
                "zones": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "ip_address", "device_id"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "ip_address": {"type": "string"},
                            "device_id": {"type": "string"},
                        },
                    },
                },
            },
        },
        "resideo": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "username": {"type": "string"},
                "password": {"type": "string"},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 60},
            },
        },
        "claude_usage": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "access_token": {"type": "string"},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 60},
            },
        },
        "mg_saic": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "username": {"type": "string"},
                "password": {"type": "string"},
                "username_is_email": {"type": "boolean"},
                "region": {"type": "string", "enum": ["eu", "au", "tr"]},
                "vin": {"type": "string"},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 60},
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


def get_hotwater_melcloud_config_error(config_data: dict[str, Any]) -> str | None:
    """Return an error message if melcloud isn't properly configured for hot
    water automation, else None.

    Pure condition check, deliberately with no opinion on enforcement -
    shared by two callers that each enforce it differently:
    - validate_business_rules() below wraps this in its own
      hotwater_automation.enabled gate and only warns (config still loads).
    - scripts/hotwater_automation_core.py's get_hotwater_automation_config_error()
      uses it as a hard startup gate, refusing to start hot water automation
      at all - its callers already know hotwater_automation.enabled is true
      before calling, so this doesn't check that itself.

    Keeping the condition in exactly one place means the two enforcement
    paths can't quietly drift apart if the rule is ever extended.
    """
    melcloud_config = config_data.get("melcloud", {})
    if not melcloud_config.get("enabled", False):
        return (
            "hotwater_automation.enabled is true but melcloud.enabled is false - "
            "hot water automation requires MELCloud to be enabled and configured"
        )
    if not melcloud_config.get("email") or not melcloud_config.get("password"):
        return "hotwater_automation.enabled is true but melcloud.email/password are not set"
    return None


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

        # Hot water automation cross-check: without this, the mismatch would
        # otherwise surface only as a ValueError raised deep inside
        # MelCloudClient on the first connect() attempt - in
        # hotwater_mode_daemon.py that's caught by a blanket `except
        # Exception` and logged every poll cycle, rather than being visible
        # here at config-load time. The condition itself lives in
        # get_hotwater_melcloud_config_error() below - shared with
        # scripts/hotwater_automation_core.py's own hard startup gate for hot
        # water callers specifically, so the rule can't drift between the two
        # (this one only warns; that one refuses to start).
        hotwater_config = config_data.get("hotwater_automation", {})
        if hotwater_config.get("enabled", False):
            hotwater_melcloud_error = get_hotwater_melcloud_config_error(config_data)
            if hotwater_melcloud_error:
                warnings.append(f"Error: {hotwater_melcloud_error}")

    except (KeyError, ValueError, TypeError, AttributeError) as e:
        warnings.append(f"Error validating business rules: {e!s}")
        logger.exception("Business rule validation error")

    return warnings


def _load_secrets_overlay(config_file_path: str) -> dict[str, Any]:
    """Load secrets.yaml from alongside config_file_path, if present, else {}.

    Keeps real credentials out of config.yaml (and therefore out of git,
    since config.yaml is tracked) by storing them in a separate,
    .gitignore'd file with the same nested structure - e.g.:

        melcloud:
          email: "you@example.com"
          password: "..."

    - merged on top of config.yaml at load time, so every existing caller of
    load_static_config() sees the real values transparently. To move to
    another machine (e.g. the Pi), copy this one small file there once
    (scp/rsync) - config.yaml itself moves via git as normal.
    """
    secrets_path = Path(config_file_path).resolve().parent / SECRETS_FILENAME
    if not secrets_path.exists():
        return {}
    try:
        with secrets_path.open(encoding="utf-8") as f:
            secrets = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        logger.exception("Failed to load %s, ignoring", secrets_path)
        return {}

    if secrets is None:
        return {}
    if not isinstance(secrets, dict):
        logger.error(
            "%s must be a mapping of section names to values (e.g. 'melcloud: "
            "{email: ...}') - got %s, ignoring it entirely",
            secrets_path,
            type(secrets).__name__,
        )
        return {}
    return secrets


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `overlay` onto `base`, with overlay values winning."""
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _find_unknown_overlay_keys(
    overlay: dict[str, Any], base: dict[str, Any], prefix: str = ""
) -> list[str]:
    """Recursively find keys present in `overlay` but not anywhere in `base`.

    Used to catch a typo'd secrets.yaml key at *any* nesting level, not just
    a top-level section name - a typo'd nested key (e.g. "emial" instead of
    "email" under an otherwise-valid "melcloud:" section) previously merged
    in silently alongside the real key it was meant to override, with the
    section-name-only check finding nothing wrong since "melcloud" itself is
    a real section.

    Returns:
        Dotted-path strings (e.g. "melcloud.emial") for each unknown key,
        sorted by caller for a stable warning message.

    """
    unknown: list[str] = []
    for key, value in overlay.items():
        path = f"{prefix}.{key}" if prefix else key
        if key not in base:
            unknown.append(path)
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            unknown.extend(_find_unknown_overlay_keys(value, base[key], path))
    return unknown


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

        secrets_overlay = _load_secrets_overlay(config_file_path)
        if secrets_overlay:
            unknown_keys = _find_unknown_overlay_keys(secrets_overlay, config)
            if unknown_keys:
                # A typo'd key - at any nesting level, e.g. "melcloud.emial"
                # instead of "melcloud.email", not just a whole top-level
                # section name - would otherwise merge in harmlessly
                # alongside the real key it was meant to override, silently
                # leaving config.yaml's own value (often a placeholder) in
                # effect. The only symptom would be an auth failure at
                # runtime with nothing pointing back at the actual cause.
                logger.warning(
                    "%s has key(s) not found in %s: %s - check for typos, these "
                    "will have no effect",
                    SECRETS_FILENAME,
                    config_file_path,
                    ", ".join(sorted(unknown_keys)),
                )
            config = _deep_merge(config, secrets_overlay)
            logger.debug("Merged %s over %s", SECRETS_FILENAME, config_file_path)

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
