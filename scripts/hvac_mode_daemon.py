#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""HVAC Mode Daemon - Continuous Airstage Control Manager.

Continuous daemon driving the Airstage units' mode/temperature from the T6R's
room reading and schedule.yaml's house target, built on
src/daemon_support/base_daemon.py's shared two-tier polling loop - the same
scaffolding battery_mode_daemon.py and hotwater_mode_daemon.py use. See
docs/hvac_thermostat_automation_plan.md for the full design and
scripts/hvac_automation_core.py for the actual read-decide-apply-persist
logic this daemon just schedules.

Three registered checks, each on its own config.yaml-configurable interval
(hvac_automation.poll_intervals):
- thermostat_poll (thermostat_seconds, 10 min default): reads the T6R's room
  temperature and caches it in memory - kept separate from the decision
  check below because the spec polls the thermostat faster than it re-runs
  the mode/temperature decision.
- hvac_target_update (hvac_target_seconds, 30 min default): the actual
  decision-and-apply cycle. Runs faster than the spec's own 60-minute
  mode-change dwell window, which is what makes plan doc §8.7's "mode
  divergence is corrected immediately, bypassing the normal cadence" true in
  practice - there is no separate, faster tier for that specifically; this
  is simply the fastest tier that evaluates the decision function at all,
  and mode divergence is checked unconditionally at the top of every call to
  it (see hvac_decision_logic.determine_hvac_decision), not gated by any
  dwell timer.
- hvac_time_sync (hvac_time_sync_seconds, 60 min default): a permanent
  no-op stub - see hvac_automation_core.run_hvac_time_sync_check's docstring.

Rather than a hard-coded staleness rule of its own, the cached room
temperature is treated as unavailable (None) once it's older than
_ROOM_TEMPERATURE_STALE_MULTIPLIER * thermostat_seconds - tolerating a
couple of missed polls without immediately suspending the room-temperature-
driven parts of the decision (see HvacDecisionContext.room_temperature_c's
docstring: None doesn't block schedule propagation, only the dwell-driven
adjustment/mode-change logic).

Usage:
    python3 scripts/hvac_mode_daemon.py [--config config.yaml]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import time as time_module
from typing import Any

from ashp_automation_core import run_ashp_decision_check
from hvac_automation_core import (
    get_config_path,
    get_hvac_automation_config_error,
    read_room_temperature_c,
    run_hvac_decision_check,
    run_hvac_time_sync_check,
)

from src.config_manager.config_manager import get_ashp_config_error, load_static_config
from src.daemon_support.base_daemon import TwoTierPollingDaemon, setup_rotating_logger

DEFAULT_THERMOSTAT_POLL_SECONDS = 600
DEFAULT_HVAC_TARGET_SECONDS = 1800
DEFAULT_HVAC_TIME_SYNC_SECONDS = 3600
FAST_POLL_INTERVAL_SECONDS = 30
_ROOM_TEMPERATURE_STALE_MULTIPLIER = 3


class HvacModeDaemon(TwoTierPollingDaemon):
    """Autonomous Airstage mode/temperature manager with two-tier polling."""

    def __init__(self, config_path: str | None = None) -> None:
        """Initialize the HVAC mode daemon.

        Args:
            config_path: Path to config.yaml. Defaults to the project root's
                config.yaml (cwd-independent, safe under systemd/cron).

        """
        super().__init__()
        self.config_path = config_path or get_config_path()
        self.config: dict[str, Any] | None = None
        self._config_mtime: float | None = None
        self._config_error_logged: str | None = None
        self._room_temperature_c: float | None = None
        self._room_temperature_read_at: float | None = None
        self.logger = setup_rotating_logger("hvac_mode_daemon", "hvac_mode_daemon.log")

    def _get_config_mtime(self) -> float | None:
        """Return config.yaml's mtime, or None if it can't be stat'd right now."""
        try:
            return Path(self.config_path).stat().st_mtime
        except OSError:
            return None

    def load_config(self) -> None:
        """Load config.yaml, raising if it's missing/invalid - fatal at startup."""
        self.config = load_static_config(self.config_path)
        if self.config is None:
            msg = f"Failed to load configuration from {self.config_path}"
            raise ValueError(msg)
        self._config_mtime = self._get_config_mtime()
        self.logger.info("Configuration loaded from %s", self.config_path)

    def reload_config(self) -> None:
        """Reload config.yaml (fast poll operation) - keep old config if reload fails.

        Identical mtime-skip shape to hotwater_mode_daemon.py's own
        reload_config() - see that method's docstring for why.
        """
        try:
            current_mtime = self._get_config_mtime()
            if current_mtime is not None and current_mtime == self._config_mtime:
                return

            new_config = load_static_config(self.config_path)
            if new_config is None:
                self.logger.warning("Config reload failed validation - keeping old config")
                return
            self._config_mtime = current_mtime
            if new_config != self.config:
                self.config = new_config
                self.logger.info("Configuration reloaded from %s", self.config_path)
        except Exception:
            self.logger.exception("Failed to reload config - keeping old config")

    def should_run_checks_this_tick(self) -> bool:
        """Gate all checks on hvac_automation.enabled and config validity.

        Same "skipped tick doesn't count against any check's due-time
        bookkeeping" property as hotwater_mode_daemon.py's identical method -
        checks resume on their normal cadence as soon as this returns True
        again, no catch-up burst.

        ashp.enabled is checked here too, only when true - ashp.enabled
        false (the default) leaves this exactly as it was before ASHP
        existed, requiring nothing new of hvac_automation.
        """
        hvac_config = self.config.get("hvac_automation", {})

        if not hvac_config.get("enabled", False):
            self.logger.debug("HVAC automation disabled, idling")
            self._config_error_logged = None
            return False

        config_error = get_hvac_automation_config_error(self.config)
        if not config_error and self._ashp_config().get("enabled", False):
            config_error = get_ashp_config_error(self.config)
        if config_error:
            if config_error != self._config_error_logged:
                self.logger.error(
                    "HVAC automation misconfigured, idling until fixed: %s", config_error
                )
                self._config_error_logged = config_error
            return False

        self._config_error_logged = None
        return True

    def _hvac_config(self) -> dict[str, Any]:
        return self.config.get("hvac_automation", {})

    def _ashp_config(self) -> dict[str, Any]:
        return self.config.get("ashp", {})

    def _run_thermostat_poll_cycle(self) -> None:
        """Read the T6R's room temperature and cache it. Never raises."""
        try:
            reading = read_room_temperature_c(self.config)
            if reading is not None:
                self._room_temperature_c = reading
                self._room_temperature_read_at = time_module.time()
            else:
                self.logger.warning("Thermostat poll: room temperature unavailable this poll")
        except Exception:
            self.logger.exception("Thermostat poll cycle failed")

    def _current_room_temperature_c(self, thermostat_poll_seconds: float) -> float | None:
        """The cached room temperature, or None if never read or too stale."""
        if self._room_temperature_read_at is None:
            return None
        age = time_module.time() - self._room_temperature_read_at
        if age > thermostat_poll_seconds * _ROOM_TEMPERATURE_STALE_MULTIPLIER:
            self.logger.warning(
                "Cached room temperature is %.0fs old (> %.0fs limit) - treating as unavailable",
                age,
                thermostat_poll_seconds * _ROOM_TEMPERATURE_STALE_MULTIPLIER,
            )
            return None
        return self._room_temperature_c

    def _run_hvac_target_update_cycle(self, hvac_config: dict[str, Any]) -> None:
        """Run one full decide-and-apply cycle. Never raises.

        ashp.enabled false (the default) calls run_hvac_decision_check
        directly, exactly as before ASHP existed. Only when true does
        ashp_automation_core.run_ashp_decision_check take over - it is the
        one place that then decides, every cycle, whether
        run_hvac_decision_check itself gets called at all (docs/ASHP.md's
        "No Double Control" constraint) - see that module's own docstring.
        """
        try:
            thermostat_poll_seconds = hvac_config.get("poll_intervals", {}).get(
                "thermostat_seconds", DEFAULT_THERMOSTAT_POLL_SECONDS
            )
            room_temperature_c = self._current_room_temperature_c(thermostat_poll_seconds)
            ashp_config = self._ashp_config()
            if ashp_config.get("enabled", False):
                run_ashp_decision_check(self.config, ashp_config, hvac_config, dry_run=False, quiet=True)
            else:
                run_hvac_decision_check(
                    self.config, hvac_config, room_temperature_c, dry_run=False, quiet=True
                )
        except Exception:
            self.logger.exception("HVAC target update cycle failed")

    def _run_hvac_time_sync_cycle(self) -> None:
        """Run the (permanently no-op) HVAC time/date sync check. Never raises."""
        try:
            run_hvac_time_sync_check()
        except Exception:
            self.logger.exception("HVAC time sync check failed")

    def _register_checks(self) -> None:
        """Register the three HVAC checks. Split out from run() for testability."""
        self.register_check(
            "thermostat_poll",
            lambda: self._run_thermostat_poll_cycle(),
            lambda: self._hvac_config().get("poll_intervals", {}).get(
                "thermostat_seconds", DEFAULT_THERMOSTAT_POLL_SECONDS
            ),
        )
        self.register_check(
            "hvac_target_update",
            lambda: self._run_hvac_target_update_cycle(self._hvac_config()),
            lambda: self._hvac_config().get("poll_intervals", {}).get(
                "hvac_target_seconds", DEFAULT_HVAC_TARGET_SECONDS
            ),
        )
        self.register_check(
            "hvac_time_sync",
            lambda: self._run_hvac_time_sync_cycle(),
            lambda: self._hvac_config().get("poll_intervals", {}).get(
                "hvac_time_sync_seconds", DEFAULT_HVAC_TIME_SYNC_SECONDS
            ),
        )

    def run(self) -> None:
        """Register the HVAC checks, then run the shared two-tier polling loop."""
        self.logger.info("HVAC Mode Daemon starting...")

        self._register_checks()
        super().run(fast_poll_interval_seconds=FAST_POLL_INTERVAL_SECONDS)

        self.logger.info("HVAC Mode Daemon shutdown complete")


def main() -> None:
    """Execute main entry point."""
    parser = argparse.ArgumentParser(description="HVAC Mode Daemon")
    parser.add_argument(
        "--config", type=str, default=None, help="Path to config.yaml (default: project root)"
    )
    args = parser.parse_args()

    daemon = HvacModeDaemon(config_path=args.config)
    daemon.run()


if __name__ == "__main__":
    main()
