"""Centralized path utilities for configuration and data files.

Provides consistent absolute path resolution for all configuration files,
ensuring they are always located correctly regardless of script execution location.
"""

from pathlib import Path


def get_project_root() -> str:
    """Get the absolute path to the project root directory.

    Returns:
        Absolute path to project root as string

    """
    # Go up from src/utils/paths.py to project root (3 levels up)
    current_file = Path(__file__).resolve()
    # Return project root: three levels up from this file
    return str(current_file.parent.parent.parent)


def get_config_path(filename: str) -> str:
    """Get absolute path to a configuration file.

    Args:
        filename: Name of the configuration file

    Returns:
        Absolute path to file in config/ directory

    Example:
        get_config_path("solax_mode_change_log.json")
        -> "/path/to/project/config/solax_mode_change_log.json"

    """
    return str(Path(get_project_root()) / "config" / filename)


def get_config_dir() -> str:
    """Get absolute path to the config directory.

    Returns:
        Absolute path to config directory

    """
    return str(Path(get_project_root()) / "config")


# Specific file path functions for commonly used files
def get_mode_change_log_path() -> str:
    """Get absolute path to solax mode change log file (HARDWARE SAFETY CRITICAL)."""
    return get_config_path("solax_mode_change_log.json")


def get_optimization_settings_path() -> str:
    """Get absolute path to optimization settings file."""
    return get_config_path("optimization_settings.json")


def get_hotwater_schedule_path() -> str:
    """Get absolute path to hot water daily schedule file."""
    return get_config_path("hotwater_daily_schedule.json")


def get_partial_import_settings_path() -> str:
    """Get absolute path to partial import settings file (deprecated - use get_optimization_settings_path)."""
    return get_optimization_settings_path()


def get_auto_controller_config_path() -> str:
    """Get absolute path to auto controller config file."""
    return get_config_path("auto_controller_config.json")


def get_auto_controller_commands_path() -> str:
    """Get absolute path to auto controller commands file."""
    return get_config_path("auto_controller_commands.json")


def get_auto_controller_status_path() -> str:
    """Get absolute path to auto controller status file."""
    return get_config_path("auto_controller_status.json")


def get_daemon_pid_path() -> str:
    """Get absolute path to daemon PID file."""
    return get_config_path("solax_auto_daemon.pid")


def get_cache_dir() -> str:
    """Get absolute path to the cache directory."""
    return str(Path(get_project_root()) / "cache")


def get_cache_path(filename: str) -> str:
    """Get absolute path to a cache file.

    Args:
        filename: Name of the cache file

    Returns:
        Absolute path to file in cache/ directory

    """
    return str(Path(get_cache_dir()) / filename)


def get_solcast_cache_path() -> str:
    """Get absolute path to Solcast cache file."""
    return get_cache_path("solcast_data.json")


def get_bmw_oauth_cache_path() -> str:
    """Get absolute path to BMW OAuth cache file (bimmer_connected)."""
    return get_cache_path("bmw_oauth_store.json")


def get_bmw_cardata_token_cache_path() -> str:
    """Get absolute path to BMW CarData OAuth token cache file."""
    return get_cache_path("bmw_cardata_tokens.json")


def get_nest_token_cache_path() -> str:
    """Get absolute path to Nest JWT token cache file."""
    return get_cache_path("nest_token_cache.json")


def get_blink_auth_cache_path() -> str:
    """Get absolute path to Blink authentication cache file."""
    return get_cache_path("blink_auth.json")


def get_weather_forecast_cache_path() -> str:
    """Get absolute path to weather forecast cache file."""
    return get_cache_path("weather_forecast.json")


def get_energy_cost_accumulator_path() -> str:
    """Get absolute path to energy cost accumulator file."""
    return get_config_path("energy_cost_accumulator.json")
