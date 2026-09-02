#!/usr/bin/env python3
"""Battery Mode Daemon - Autonomous Battery Mode Manager.

This daemon manages SolaX battery operating modes based on:
1. Ohme EV charger activity (priority 1)
2. Time-based schedules (priority 2)
3. Default mode fallback (priority 3)

Architecture:
- Built on src/daemon_support/base_daemon.py's shared two-tier polling loop
  (fast config reload (30s) + slow hardware checks, configurable), the same
  base hotwater_mode_daemon.py uses.
- Safety interval enforcement to prevent rapid mode changes
- Comprehensive error handling with fallback to SELF_USE mode
- Rotating log files with 7-day retention
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time as time_module
from datetime import datetime, time
from pathlib import Path
from typing import Any

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api_clients.ohme_ev_client import OhmeEVClient
from src.api_clients.solax_modbus_client import (
    solax_modbus_set_work_mode,
    solax_modbus_soc,
    solax_modbus_work_mode,
)
from src.config_manager import load_static_config
from src.core_logic.battery_simulation import BatteryMode
from src.core_logic.ohme_charging_logic import (
    confirm_charging_over_consecutive_cycles,
    is_charging_above_threshold,
)
from src.daemon_support.base_daemon import TwoTierPollingDaemon, setup_rotating_logger
from src.utils.state_store import locked_json_state, read_json_state

FAST_POLL_INTERVAL_SECONDS = 30  # config reload cadence

# JSON Schema for daemon configuration validation
DAEMON_CONFIG_SCHEMA = {
    "type": "object",
    "required": ["daemon_settings", "ohme_charging", "schedule", "logging"],
    "properties": {
        "daemon_settings": {
            "type": "object",
            "required": [
                "hardware_poll_interval_seconds",
                "min_mode_change_interval_seconds",
                "ohme_charging_threshold_watts",
            ],
            "properties": {
                "hardware_poll_interval_seconds": {
                    "type": "number",
                    "minimum": 60,
                    "maximum": 3600,
                },
                "min_mode_change_interval_seconds": {
                    "type": "number",
                    "minimum": 60,
                    "maximum": 7200,
                },
                "ohme_charging_threshold_watts": {"type": "number", "minimum": 0, "maximum": 10000},
                "min_discharge_soc_percent": {
                    "type": "number",
                    "minimum": 10,
                    "maximum": 80,
                },
            },
        },
        "ohme_charging": {
            "type": "object",
            "required": ["enabled", "force_charge_mode"],
            "properties": {
                "enabled": {"type": "boolean"},
                "force_charge_mode": {
                    "type": "string",
                    "enum": ["FORCE_CHARGE", "SELF_USE", "FORCE_DISCHARGE"],
                },
            },
        },
        "schedule": {
            "type": "object",
            "required": ["enabled", "default_mode", "time_ranges"],
            "properties": {
                "enabled": {"type": "boolean"},
                "default_mode": {
                    "type": "string",
                    "enum": ["SELF_USE", "FORCE_CHARGE", "FORCE_DISCHARGE", "MANUAL_STOP"],
                },
                "time_ranges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["start_time", "end_time", "battery_mode"],
                        "properties": {
                            "start_time": {"type": "string", "pattern": "^([0-1][0-9]|2[0-3]):[0-5][0-9]$"},
                            "end_time": {"type": "string", "pattern": "^([0-1][0-9]|2[0-3]):[0-5][0-9]$"},
                            "battery_mode": {
                                "type": "string",
                                "enum": ["SELF_USE", "FORCE_CHARGE", "FORCE_DISCHARGE", "MANUAL_STOP"],
                            },
                            "description": {"type": "string"},
                        },
                    },
                },
            },
        },
        "logging": {
            "type": "object",
            "required": ["level", "file_path"],
            "properties": {
                "level": {
                    "type": "string",
                    "enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                },
                "file_path": {"type": "string", "minLength": 1},
            },
        },
    },
}


def validate_daemon_config(config: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate daemon configuration against JSON schema.

    Args:
        config: Configuration dictionary to validate

    Returns:
        Tuple of (is_valid, error_messages)

    """
    try:
        import jsonschema
        from jsonschema import ValidationError

        jsonschema.validate(config, DAEMON_CONFIG_SCHEMA)
        return True, []
    except ValidationError as e:
        error_path = " -> ".join(str(p) for p in e.absolute_path) if e.absolute_path else "root"
        error_msg = f"Configuration error at '{error_path}': {e.message}"
        return False, [error_msg]
    except Exception as e:
        return False, [f"Unexpected validation error: {e}"]


class BatteryModeDaemon(TwoTierPollingDaemon):
    """Autonomous battery mode manager with two-tier polling."""

    def __init__(self, config_path: str, system_config_path: str = "config.yaml") -> None:
        """Initialize the battery mode daemon.

        Args:
            config_path: Path to daemon configuration JSON file
            system_config_path: Path to system configuration YAML file

        """
        super().__init__()
        self.config_path = Path(config_path)
        self.system_config_path = system_config_path
        self.daemon_config = None
        self.system_config = None
        self.mode_change_log_path = Path("data/battery_mode_daemon_log.json")
        self.last_mode_change_time = None
        self.startup_complete = False

        # Ohme charging detection state (requires 2 consecutive cycles)
        self.ohme_charging_count = 0

        # INFO until load_config() reads the real level from daemon_config's
        # "logging.level" - the logger has to exist before that JSON file is
        # even read, since load_config() itself logs.
        self.logger = setup_rotating_logger(
            "battery_mode_daemon", "battery_mode_daemon.log", level=logging.INFO
        )

    def load_config(self) -> None:
        """Load daemon configuration from JSON file, plus system config and the mode change log.

        Fatal (raises) if either config is missing/invalid - this is only
        ever called once, at startup, via TwoTierPollingDaemon.run().
        """
        try:
            if not self.config_path.exists():
                msg = f"Daemon configuration file not found: {self.config_path}"
                raise FileNotFoundError(msg)

            with self.config_path.open("r", encoding="utf-8") as f:
                self.daemon_config = json.load(f)

            # Validate configuration against schema
            is_valid, errors = validate_daemon_config(self.daemon_config)
            if not is_valid:
                self.logger.error("Daemon configuration validation failed:")
                for error in errors:
                    self.logger.error("  %s", error)
                msg = "Invalid daemon configuration - see errors above"
                raise ValueError(msg)

            self.logger.info("Daemon configuration loaded and validated from %s", self.config_path)

            # Load system configuration - not hot-reloaded (see reload_config()
            # below); inverter IPs etc. aren't expected to change at runtime.
            self.system_config = load_static_config(self.system_config_path)
            if self.system_config is None:
                msg = f"Failed to load system configuration from {self.system_config_path}"
                raise ValueError(msg)

        except json.JSONDecodeError as e:
            self.logger.exception("Invalid JSON in daemon configuration file")
            raise ValueError(f"Invalid JSON in daemon configuration: {e}") from e
        except Exception:
            self.logger.exception("Failed to load daemon configuration")
            raise

        self._load_mode_change_log()

    def reload_config(self) -> None:
        """Reload daemon configuration (fast poll operation).

        Only the daemon_settings/ohme_charging/schedule JSON config is
        hot-reloaded - system_config (config.yaml) is loaded once at startup
        by load_config() and not touched here, matching the pre-refactor
        behaviour.
        """
        try:
            if not self.config_path.exists():
                self.logger.warning("Configuration file disappeared: %s", self.config_path)
                return

            with self.config_path.open("r", encoding="utf-8") as f:
                new_config = json.load(f)

            # Validate new configuration
            is_valid, errors = validate_daemon_config(new_config)
            if not is_valid:
                self.logger.error("New configuration is invalid - keeping old config:")
                for error in errors:
                    self.logger.error("  %s", error)
                return

            # Check if config actually changed
            if new_config != self.daemon_config:
                self.daemon_config = new_config
                self.logger.info("Configuration reloaded from %s", self.config_path)
            else:
                self.logger.debug("Configuration unchanged")

        except json.JSONDecodeError:
            self.logger.exception("Invalid JSON during config reload - keeping old config")
        except Exception:
            self.logger.exception("Failed to reload config - keeping old config")

    @staticmethod
    def _default_mode_change_log() -> dict[str, Any]:
        """A fresh, independent default log structure (a shared dict/list would leak mutations)."""
        return {
            "last_change_timestamp": None,
            "last_change_mode": None,
            "last_change_reason": None,
            "change_history": [],
        }

    def _load_mode_change_log(self) -> dict[str, Any]:
        """Load daemon's mode change history from JSON.

        Returns:
            Dictionary with mode change history

        """
        log_data = read_json_state(self.mode_change_log_path)
        if not log_data:
            log_data = self._default_mode_change_log()

        # Restore last change time from log
        if log_data.get("last_change_timestamp"):
            self.last_mode_change_time = log_data["last_change_timestamp"]

        self.logger.debug("Loaded mode change log from %s", self.mode_change_log_path)
        return log_data

    def _save_mode_change_log(self, mode: BatteryMode, reason: str) -> None:
        """Persist mode change to log file.

        Uses the same fcntl-based locked read-modify-write
        (src.utils.state_store.locked_json_state) that
        scripts/hotwater_automation_core.py and
        src/api_clients/_modbus_mode_controller.py use for their own state
        files - a plain load-then-later-write here was not atomic across a
        concurrent load/save from another process touching this same file.

        Args:
            mode: The mode that was set
            reason: Reason for the mode change

        """
        try:
            timestamp = time_module.time()
            log_entry = {
                "timestamp": timestamp,
                "datetime": datetime.now().isoformat(),
                "mode": mode.value,
                "reason": reason,
            }

            with locked_json_state(self.mode_change_log_path) as log_data:
                if not log_data:
                    log_data.update(self._default_mode_change_log())
                log_data["last_change_timestamp"] = timestamp
                log_data["last_change_mode"] = mode.value
                log_data["last_change_reason"] = reason

                # Keep last 100 entries
                log_data["change_history"] = [*log_data.get("change_history", []), log_entry][
                    -100:
                ]

            self.logger.debug("Saved mode change to log: %s (%s)", mode.value, reason)

        except Exception:
            self.logger.exception("Failed to save mode change log (non-critical)")

    def _is_ohme_charging(self, status: dict[str, Any] | None) -> bool:
        """Check if Ohme is actively charging above threshold.

        Requires 2 consecutive cycles of charging detection before returning True.
        This prevents short transient charging on plug-in from triggering mode changes.
        Shared decision logic with hotwater_automation_core.py's own charging check
        (src/core_logic/ohme_charging_logic.py) - only the persisted cycle count
        (self.ohme_charging_count, in-memory here vs. state-file-backed there) differs.

        Args:
            status: Ohme status dictionary or None if unavailable

        Returns:
            True if Ohme has been charging above threshold for 2 consecutive cycles

        """
        if status is None:
            # Reset counter if we can't get status
            self.ohme_charging_count = 0
            return False

        threshold = self.daemon_config["daemon_settings"]["ohme_charging_threshold_watts"]
        power_watts = status.get("power_watts", 0)
        above_threshold = is_charging_above_threshold(power_watts, threshold)
        previous_count = self.ohme_charging_count

        self.ohme_charging_count, confirmed = confirm_charging_over_consecutive_cycles(
            previous_count, above_threshold
        )

        if above_threshold:
            if confirmed:
                self.logger.debug(
                    "Ohme charging confirmed (cycle %d, power: %dW)",
                    self.ohme_charging_count,
                    power_watts,
                )
            else:
                self.logger.info(
                    "Ohme charging detected but waiting for confirmation (cycle 1/2, power: %dW)",
                    power_watts,
                )
        elif previous_count > 0:
            self.logger.debug(
                "Ohme charging stopped, resetting counter (was at cycle %d)", previous_count
            )

        return confirmed

    def _get_scheduled_mode(self) -> BatteryMode | None:
        """Evaluate time ranges for current time.

        Returns:
            Scheduled battery mode if within a time range, None otherwise

        """
        if not self.daemon_config["schedule"]["enabled"]:
            return None

        now = datetime.now().time()

        for range_config in self.daemon_config["schedule"]["time_ranges"]:
            start = datetime.strptime(range_config["start_time"], "%H:%M").time()
            end = datetime.strptime(range_config["end_time"], "%H:%M").time()

            # Handle midnight crossing (e.g., 23:00-01:00)
            if start <= end:
                if start <= now <= end:
                    return BatteryMode(range_config["battery_mode"])
            else:  # Crosses midnight
                if now >= start or now <= end:
                    return BatteryMode(range_config["battery_mode"])

        return None

    def _can_change_mode(self) -> tuple[bool, int]:
        """Check minimum interval since last daemon change.

        Returns:
            Tuple of (can_change, seconds_since_last_change)

        """
        if self.last_mode_change_time is None:
            return True, 0

        elapsed = time_module.time() - self.last_mode_change_time
        min_interval = self.daemon_config["daemon_settings"]["min_mode_change_interval_seconds"]

        return elapsed >= min_interval, int(elapsed)

    def _check_battery_protection(self, target_mode: BatteryMode) -> tuple[bool, str | None]:
        """Check if battery protection should block FORCE_DISCHARGE mode.

        Args:
            target_mode: The mode being requested

        Returns:
            Tuple of (should_block, protection_reason)

        """
        if target_mode != BatteryMode.FORCE_DISCHARGE:
            return False, None

        threshold = self.daemon_config["daemon_settings"].get("min_discharge_soc_percent", 20)
        soc_data = solax_modbus_soc(self.system_config)

        if soc_data is None:
            self.logger.warning("Battery protection: Cannot read SOC - blocking FORCE_DISCHARGE")
            return True, "Cannot read battery SOC - safety block"

        master_soc = soc_data.get("master", 0)
        slave_soc = soc_data.get("slave", 0)
        min_soc = min(master_soc, slave_soc)

        if min_soc <= threshold:
            return True, f"SOC {min_soc}% at/below threshold {threshold}%"

        self.logger.debug("Battery protection check passed: SOC %d%% above threshold %d%%", min_soc, threshold)
        return False, None

    def _determine_target_mode(self, ohme_status: dict[str, Any] | None) -> tuple[BatteryMode, str]:
        """Determine target mode based on priority logic.

        Priority: Error > Ohme > Schedule > Default

        Args:
            ohme_status: Ohme status dictionary or None if error/unavailable

        Returns:
            Tuple of (target_mode, reason)

        """
        # Priority 1: Check Ohme charging
        if self.daemon_config["ohme_charging"]["enabled"]:
            if self._is_ohme_charging(ohme_status):
                mode = BatteryMode(self.daemon_config["ohme_charging"]["force_charge_mode"])
                power = ohme_status.get("power_watts", 0) if ohme_status else 0
                reason = f"Ohme charging detected ({power}W)"
                return mode, reason

        # Priority 2: Check time schedule
        if self.daemon_config["schedule"]["enabled"]:
            scheduled_mode = self._get_scheduled_mode()
            if scheduled_mode is not None:
                # Find the matching range for logging
                now = datetime.now().time()
                for range_config in self.daemon_config["schedule"]["time_ranges"]:
                    if BatteryMode(range_config["battery_mode"]) == scheduled_mode:
                        start = range_config["start_time"]
                        end = range_config["end_time"]
                        desc = range_config.get("description", "")
                        reason = f"Schedule: {start}-{end} {desc}".strip()
                        return scheduled_mode, reason

        # Priority 3: Default mode
        default_mode = BatteryMode(self.daemon_config["schedule"]["default_mode"])
        return default_mode, "Default mode (outside scheduled ranges)"

    def _set_mode_safely(self, target_mode: BatteryMode, reason: str) -> None:
        """Change mode with safety checks and logging.

        Note: This method no longer blocks rapid mode changes - it only logs warnings.
        The actual safety interval enforcement is handled by the SolaX Modbus client.

        Args:
            target_mode: The mode to set
            reason: Reason for the mode change

        """
        try:
            # Read current mode
            current_mode = solax_modbus_work_mode(self.system_config)

            # Skip if already in target mode
            if current_mode == target_mode:
                self.logger.debug("Already in %s mode, skipping change", target_mode.value)
                return

            # Check battery protection for FORCE_DISCHARGE
            should_block, protection_reason = self._check_battery_protection(target_mode)
            if should_block:
                self.logger.warning(
                    "\U0001faab Battery protection: %s - blocking FORCE_DISCHARGE",
                    protection_reason,
                )
                target_mode = BatteryMode.SELF_USE
                reason = f"Battery protection override: {protection_reason}"

            # Check safety interval (for logging only, not blocking)
            can_change, elapsed = self._can_change_mode()
            min_interval = self.daemon_config["daemon_settings"][
                "min_mode_change_interval_seconds"
            ]

            if not can_change:
                self.logger.warning(
                    "🔥 RAPID MODE CHANGE: Last change was %d seconds ago (recommended minimum: %d). "
                    "Proceeding with change from %s to %s (reason: %s). "
                    "SolaX client will enforce hardware safety limits.",
                    elapsed,
                    min_interval,
                    current_mode.value,
                    target_mode.value,
                    reason,
                )

            # Perform mode change (SolaX client enforces hardware safety)
            result = solax_modbus_set_work_mode(
                self.system_config, target_mode, changed_by="daemon", force_unsafe=False
            )

            if result["success"]:
                self.logger.info(
                    "✅ Mode changed: %s → %s (reason: %s)",
                    current_mode.value,
                    target_mode.value,
                    reason,
                )
                self._save_mode_change_log(target_mode, reason)
                self.last_mode_change_time = time_module.time()
            else:
                self.logger.error(
                    "❌ Mode change failed: %s (error: %s)",
                    reason,
                    result.get("error_message", "Unknown error"),
                )

        except Exception:
            self.logger.exception("Failed to set mode safely")

    def _check_ohme_status(self) -> dict[str, Any] | None:
        """Synchronous wrapper for async Ohme API call.

        Returns:
            Ohme status dictionary or None on error

        """
        try:
            return asyncio.run(self._check_ohme_status_async())
        except Exception:
            self.logger.exception("Failed to check Ohme status")
            return None

    async def _check_ohme_status_async(self) -> dict[str, Any]:
        """Fetch Ohme charger status asynchronously.

        Returns:
            Ohme status dictionary

        """
        client = OhmeEVClient(config_path=self.system_config_path)
        await client.connect()
        try:
            status = await client.get_charger_status(use_cache=False)
            return status
        finally:
            await client.close()

    def _perform_hardware_cycle(self) -> None:
        """Execute one hardware check and mode change cycle."""
        try:
            self.logger.debug("Starting hardware cycle")

            # Check Ohme status
            ohme_status = self._check_ohme_status()

            # Determine target mode
            target_mode, reason = self._determine_target_mode(ohme_status)

            self.logger.debug("Target mode: %s (reason: %s)", target_mode.value, reason)

            # Set mode if needed
            self._set_mode_safely(target_mode, reason)

            # Check if currently in FORCE_DISCHARGE and need emergency exit
            current_mode = solax_modbus_work_mode(self.system_config)
            if current_mode == BatteryMode.FORCE_DISCHARGE:
                should_block, protection_reason = self._check_battery_protection(current_mode)
                if should_block:
                    self.logger.warning(
                        "\U0001faab Battery protection: %s - switching back to SELF_USE",
                        protection_reason,
                    )
                    self._set_mode_safely(
                        BatteryMode.SELF_USE,
                        f"Battery protection emergency: {protection_reason}"
                    )

        except Exception:
            self.logger.exception("❌ Hardware cycle failed - setting SELF_USE for safety")
            try:
                self._set_mode_safely(BatteryMode.SELF_USE, "Error fallback - hardware cycle failed")
            except Exception:
                self.logger.exception("Failed to set safety fallback mode")

    def _hardware_check(self) -> None:
        """The daemon's one scheduled check - skips the very first tick after startup.

        Preserves the pre-refactor behaviour: the daemon waits one full
        hardware_poll_interval_seconds before making its first mode change,
        rather than potentially acting within moments of starting up.
        """
        if self.startup_complete:
            self._perform_hardware_cycle()
        else:
            self.logger.info("⏳ First startup - waiting one cycle before mode changes")
            self.startup_complete = True

    def run(self) -> None:
        """Register the hardware check, then run the shared two-tier polling loop."""
        self.logger.info("🚀 Battery Mode Daemon starting...")

        self.register_check(
            "hardware",
            self._hardware_check,
            lambda: self.daemon_config["daemon_settings"]["hardware_poll_interval_seconds"],
        )
        super().run(fast_poll_interval_seconds=FAST_POLL_INTERVAL_SECONDS)

        self.logger.info("👋 Daemon shutdown complete")


def main() -> None:
    """Entry point for the daemon."""
    parser = argparse.ArgumentParser(
        description="Battery Mode Daemon - Autonomous battery mode manager for SolaX inverters",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start daemon with default system config
  %(prog)s battery_mode_daemon_config.json

  # Start daemon with custom system config
  %(prog)s battery_mode_daemon_config.json /path/to/config.yaml

  # Validate configuration and exit, without starting the loop or touching hardware
  %(prog)s my_config.json --dry-run

Features:
  - EV charging integration (Ohme charger detection)
  - Time-based battery mode scheduling
  - Safety interval enforcement (prevents rapid mode changes)
  - Hot configuration reload (updates apply within 30 seconds)
  - Rotating log files (7-day retention)

For more information, see:
  - User Guide: docs/daemon_user_guide.md
  - Design Documentation: docs/daemon_design.md
        """,
    )

    parser.add_argument(
        "config_file",
        help="Path to daemon JSON configuration file (e.g., battery_mode_daemon_config.json)",
    )

    parser.add_argument(
        "system_config",
        nargs="?",
        default="config.yaml",
        help="Path to system YAML configuration file (default: config.yaml)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load and validate both configuration files, then exit - "
        "never starts the polling loop or touches hardware",
    )

    parser.add_argument(
        "--version",
        action="version",
        version="Battery Mode Daemon v1.0.0",
    )

    args = parser.parse_args()

    daemon = BatteryModeDaemon(args.config_file, args.system_config)

    if args.dry_run:
        try:
            daemon.load_config()
        except Exception as e:
            print(f"Configuration invalid: {e}")
            sys.exit(1)
        print(f"Configuration OK: {args.config_file} + {args.system_config}")
        return

    daemon.run()


if __name__ == "__main__":
    main()
