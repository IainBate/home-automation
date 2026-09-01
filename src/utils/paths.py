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


def get_hotwater_automation_state_path() -> str:
    """Get absolute path to the MELCloud hot water automation state file.

    Tracks when force hot water heating was last activated by
    scripts/hotwater_auto_check.py, so a periodic --revert-if-due run can fall
    back to auto mode after hotwater_automation.force_heat_max_duration_hours.
    """
    return get_config_path("hotwater_automation_state.json")


def get_data_dir() -> str:
    """Get absolute path to the data directory."""
    return str(Path(get_project_root()) / "data")


def get_solax_historical_data_path() -> str:
    """Get absolute path to the SolaX historical energy data file.

    Populated incrementally by scripts/solax_cloud_data_logger.py (5-minute
    granularity PV/battery/grid/load/SoC data). Read-only from the perspective
    of anything other than that logger, e.g.
    scripts/battery_evening_predictor.py uses it purely as training data.
    """
    return str(Path(get_data_dir()) / "solax_historical_data.json")


def get_battery_evening_prediction_path() -> str:
    """Get absolute path to the battery evening SoC prediction status file.

    Written once daily (near hotwater_automation.trigger_hour) by
    scripts/battery_evening_predictor.py, and read by
    scripts/hotwater_automation_core.py as a same-evening forecast of battery
    SoC - preferred over a live SoC snapshot for the force-heat decision since
    heating can run for up to force_heat_max_duration_hours, longer than a
    single reading can vouch for. Entirely decoupled from
    battery_mode_daemon.py: this only ever reads solax_historical_data.json
    and a live SoC value, never touches the battery daemon's own state.
    """
    return get_config_path("battery_evening_prediction.json")


def get_resideo_token_state_path() -> str:
    """Get absolute path to the Resideo OAuth token state file.

    Resideo commonly rotates the refresh token on every use - the previous
    one stops working the moment a new one is issued. This file holds
    whichever refresh token is currently valid, so
    src/api_clients/resideo_client.py doesn't need to rewrite secrets.yaml
    (a hand-maintained file) on every poll. secrets.yaml's resideo.refresh_token
    is only the bootstrap value scripts/resideo_oauth_setup.py produces; this
    file takes precedence once it exists.
    """
    return get_config_path("resideo_token_state.json")


def get_claude_usage_token_state_path() -> str:
    """Get absolute path to the cached Claude Code access token state file.

    Written by scripts/claude_usage_token_sync.py (run on whichever machine
    has `claude` logged in, over SSH - see that script's docstring), so the
    dashboard's claude_usage.access_token can be kept fresh automatically
    rather than requiring a manual scripts/claude_usage_token_extract.py +
    secrets.yaml paste every ~8 hours. secrets.yaml's claude_usage.access_token
    is only the bootstrap/recovery value; this file takes precedence once it
    exists - same pattern as get_resideo_token_state_path().
    """
    return get_config_path("claude_usage_token_state.json")


def get_claude_usage_path() -> str:
    """Get absolute path to the cached Claude Code usage status file.

    Written periodically (e.g. every 10 minutes via cron - see
    config.yaml's claude_usage comments) by scripts/claude_usage_poller.py,
    and read by the dashboard for display only. Deliberately NOT fetched
    inline by the dashboard's own fast poll loop - the usage endpoint is
    rate-limited per-account and shared with real Claude Code sessions using
    the same login token, so this needs its own slow, independent cadence.
    """
    return get_config_path("claude_usage.json")


def get_mg_saic_status_path() -> str:
    """Get absolute path to the cached MG SAIC (MG iSmart) EV status file.

    Written periodically (e.g. hourly via cron - see config.yaml's mg_saic
    comments) by scripts/mg_saic_poller.py, and read by the dashboard for
    display only. Deliberately not fetched inline by the dashboard's own
    fast poll loop - see src/api_clients/saic_client.py's module docstring
    for why (shared account session with the household's phones).
    """
    return get_config_path("mg_saic_status.json")


def get_solar_forecast_path() -> str:
    """Get absolute path to the solar generation forecast status file.

    Written periodically (e.g. hourly via cron) by
    scripts/solar_forecast_predictor.py, and read by the dashboard
    (src/dashboard/status_collector.py) for display only. Entirely decoupled
    from both battery_mode_daemon.py and hotwater_mode_daemon.py - neither
    reads nor is affected by this file.
    """
    return get_config_path("solar_forecast.json")


def get_solar_forecast_model_path() -> str:
    """Get absolute path to the trained solar forecast model artifact.

    Written by scripts/solar_forecast_trainer.py (run periodically, e.g.
    weekly, via cron - retraining needs fresh historical data to be worth
    doing more often than that) and loaded read-only by
    scripts/solar_forecast_predictor.py.
    """
    return str(Path(get_data_dir()) / "solar_forecast_model.joblib")


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
