#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Hot Water Mode Daemon - Continuous Force-Heat Manager.

Continuous-running counterpart to hotwater_auto_check.py, architecturally
mirroring battery_mode_daemon.py - both are built on
src/daemon_support/base_daemon.py's shared two-tier polling loop:
- Fast tick (30s) that always reloads config.yaml, plus slower checks, each
  on their own configurable interval (all under hotwater_automation in
  config.yaml): force-heat (poll_interval_seconds, which now also carries
  the daily legionella-eligibility snapshot and the legionella-due decision
  - see hotwater_automation_core.py) and revert-if-due/legionella-progress/
  legionella-natural-completion (revert_check_interval_seconds)
- Rotating log file (logs/hotwater_mode_daemon.log), 7-day retention
- Graceful shutdown on SIGTERM/SIGINT

Runs standalone alongside battery_mode_daemon.py - reads live battery SoC via
the same read-only solax_modbus_soc() Modbus call the battery daemon uses,
and does not touch or coordinate with the battery daemon's own process or
state file, so it carries no risk to it.

A continuous daemon (rather than a frequent cron entry) is needed because the
"EV is charging" force-heat condition can start at any moment - only a
process that's actually watching catches it promptly.

Usage:
    python3 scripts/hotwater_mode_daemon.py [--config config.yaml]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import asyncio
from typing import Any

from hotwater_automation_core import (
    check_legionella_due_warning,
    get_config_path,
    get_hotwater_automation_config_error,
    run_force_heat_check,
    run_legionella_natural_completion_check,
    run_legionella_progress_check,
    run_revert_check,
)

from src.config_manager.config_manager import load_static_config
from src.daemon_support.base_daemon import TwoTierPollingDaemon, setup_rotating_logger

DEFAULT_POLL_INTERVAL_SECONDS = 600  # 10 minutes - frequent enough to catch EV charging starting
DEFAULT_REVERT_CHECK_INTERVAL_SECONDS = 3600  # 1 hour - also used for legionella progress checks
FAST_POLL_INTERVAL_SECONDS = 30  # config.yaml reload cadence


class HotWaterModeDaemon(TwoTierPollingDaemon):
    """Autonomous hot water force-heat manager with two-tier polling."""

    def __init__(self, config_path: str | None = None) -> None:
        """Initialize the hot water mode daemon.

        Args:
            config_path: Path to config.yaml. Defaults to the project root's
                config.yaml (cwd-independent, safe under systemd/cron).

        """
        super().__init__()
        self.config_path = config_path or get_config_path()
        self.config: dict[str, Any] | None = None
        self._config_mtime: float | None = None
        self._config_error_logged: str | None = None
        self.logger = setup_rotating_logger("hotwater_mode_daemon", "hotwater_mode_daemon.log")

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

        Skips the full YAML parse + secrets-merge + jsonschema validation
        (real work, done unconditionally on every 30s tick before this) when
        config.yaml's mtime hasn't changed since the last successful load -
        the overwhelmingly common case for a file that's rarely edited. Falls
        through to the full reparse if the file can't be stat'd (e.g. a
        transient permissions/NFS hiccup) rather than trusting a possibly
        stale mtime, matching this method's existing "keep old config if
        reload fails" fail-safe stance.
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
        """Gate all checks on hotwater_automation.enabled and config validity.

        A tick skipped here doesn't count against any check's own due-time
        bookkeeping (see TwoTierPollingDaemon.should_run_checks_this_tick),
        so checks resume on their normal cadence as soon as this returns
        True again - no catch-up burst after being re-enabled or fixed.
        """
        hw_config = self.config.get("hotwater_automation", {})

        if not hw_config.get("enabled", False):
            self.logger.debug("Hot water automation disabled, idling")
            self._config_error_logged = None
            return False

        config_error = get_hotwater_automation_config_error(self.config)
        if config_error:
            # Log once per distinct error (not every 30s fast-poll tick) -
            # this is a "won't start until fixed" condition, not a
            # transient hardware failure to retry through.
            if config_error != self._config_error_logged:
                self.logger.error(
                    "Hot water automation misconfigured, idling until fixed: %s",
                    config_error,
                )
                self._config_error_logged = config_error
            return False

        self._config_error_logged = None
        return True

    def _run_force_heat_cycle(self, hw_config: dict[str, Any]) -> None:
        """Run one force-heat check cycle. Never raises - daemon must keep running."""
        try:
            asyncio.run(
                run_force_heat_check(self.config, hw_config, dry_run=False, quiet=True)
            )
        except Exception:
            self.logger.exception("Force-heat check cycle failed")

    def _run_revert_cycle(self, hw_config: dict[str, Any]) -> None:
        """Run one revert-if-due safety check cycle. Never raises."""
        try:
            asyncio.run(run_revert_check(self.config, hw_config, dry_run=False, quiet=True))
        except Exception:
            self.logger.exception("Revert-if-due check cycle failed")

    def _run_legionella_progress_cycle(self, hw_config: dict[str, Any]) -> None:
        """Run one legionella-cycle-progress check. Never raises."""
        try:
            asyncio.run(
                run_legionella_progress_check(self.config, hw_config, dry_run=False, quiet=True)
            )
        except Exception:
            self.logger.exception("Legionella progress check cycle failed")

    def _run_legionella_natural_completion_cycle(self, hw_config: dict[str, Any]) -> None:
        """Run one legionella-natural-completion check. Never raises."""
        try:
            asyncio.run(
                run_legionella_natural_completion_check(hw_config, dry_run=False, quiet=True)
            )
        except Exception:
            self.logger.exception("Legionella natural-completion check cycle failed")

    def _run_legionella_due_warning_cycle(self, hw_config: dict[str, Any]) -> None:
        """Run one legionella-due-soon warning email check. Never raises."""
        try:
            check_legionella_due_warning(self.config, hw_config, dry_run=False, quiet=True)
        except Exception:
            self.logger.exception("Legionella due-warning check cycle failed")

    def _hw_config(self) -> dict[str, Any]:
        return self.config.get("hotwater_automation", {})

    def _register_checks(self) -> None:
        """Register the three hot water checks. Split out from run() for testability."""
        self.register_check(
            "force_heat",
            lambda: self._run_force_heat_cycle(self._hw_config()),
            lambda: self._hw_config().get(
                "poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS
            ),
        )
        self.register_check(
            "revert",
            lambda: self._run_revert_cycle(self._hw_config()),
            lambda: self._hw_config().get(
                "revert_check_interval_seconds", DEFAULT_REVERT_CHECK_INTERVAL_SECONDS
            ),
        )
        # Legionella progress uses the same cadence as revert-if-due - both
        # are lower-frequency lifecycle/safety checks.
        self.register_check(
            "legionella_progress",
            lambda: self._run_legionella_progress_cycle(self._hw_config()),
            lambda: self._hw_config().get(
                "revert_check_interval_seconds", DEFAULT_REVERT_CHECK_INTERVAL_SECONDS
            ),
        )
        # Same cadence again - this one has no prior state to gate on (see
        # its docstring), so it's just another lifecycle/safety-tier check.
        self.register_check(
            "legionella_natural_completion",
            lambda: self._run_legionella_natural_completion_cycle(self._hw_config()),
            lambda: self._hw_config().get(
                "revert_check_interval_seconds", DEFAULT_REVERT_CHECK_INTERVAL_SECONDS
            ),
        )

    def run(self) -> None:
        """Register the hot water checks, then run the shared two-tier polling loop."""
        self.logger.info("Hot Water Mode Daemon starting...")

        self._register_checks()
        super().run(fast_poll_interval_seconds=FAST_POLL_INTERVAL_SECONDS)

        self.logger.info("Hot Water Mode Daemon shutdown complete")


def main() -> None:
    """Execute main entry point."""
    parser = argparse.ArgumentParser(description="Hot Water Mode Daemon")
    parser.add_argument(
        "--config", type=str, default=None, help="Path to config.yaml (default: project root)"
    )
    args = parser.parse_args()

    daemon = HotWaterModeDaemon(config_path=args.config)
    daemon.run()


if __name__ == "__main__":
    main()
