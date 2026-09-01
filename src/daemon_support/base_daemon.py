"""Shared two-tier polling loop and logging setup for the project's daemons.

battery_mode_daemon.py and hotwater_mode_daemon.py both run the same shape of
loop: a fast tick (default 30s) that always reloads config, plus one or more
slower checks that each run on their own configurable interval, doing real
hardware/API work. Both also set up an identical rotating-file-plus-console
logger and identical SIGTERM/SIGINT handling. This module holds that
scaffolding once, so it can't drift between the two daemons - each daemon
keeps its own config shape, its own checks, and its own decision logic;
only the "how often do I run what" plumbing lives here.
"""

from __future__ import annotations

import logging
import signal
import time as time_module
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Callable

DEFAULT_FAST_POLL_INTERVAL_SECONDS = 30.0


def setup_rotating_logger(
    logger_name: str, log_filename: str, *, level: int = logging.INFO
) -> logging.Logger:
    """Create a logger with the project's standard daemon handler setup.

    Midnight-rotating file handler (7-day retention) under logs/, plus a
    console handler with matching formatting - shared by
    battery_mode_daemon.py and hotwater_mode_daemon.py so the two can't drift
    apart (different retention, different format, etc.) by accident.

    Args:
        logger_name: Name passed to logging.getLogger().
        log_filename: Bare filename under logs/ (e.g. "battery_mode_daemon.log").
        level: Logger level - the file/console handlers have no level of
            their own, so this is the effective floor.

    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)

    Path("logs").mkdir(exist_ok=True)

    handler = TimedRotatingFileHandler(
        f"logs/{log_filename}",
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


@dataclass
class _ScheduledCheck:
    """One periodic check registered with a TwoTierPollingDaemon.

    get_interval_seconds is called fresh on every due() check (not cached at
    registration) so a hot-reloaded config interval takes effect without
    restarting the daemon - matching what both daemons already did with a
    plain `config.get(...)` read inside their loop.
    """

    name: str
    run: Callable[[], None]
    get_interval_seconds: Callable[[], float]
    _last_run: float = field(default=0.0, init=False)

    def due(self, now: float) -> bool:
        return now - self._last_run >= self.get_interval_seconds()

    def mark_run(self, now: float) -> None:
        self._last_run = now


class TwoTierPollingDaemon(ABC):
    """Fast-config-reload / slow-scheduled-checks daemon loop.

    Concrete daemons:
    1. Set self.logger (typically via setup_rotating_logger) before run().
    2. Implement load_config() (raise on failure - fatal at startup) and
       reload_config() (must not raise; keep the old config on failure).
    3. Call register_check() for each periodic check, then call run().
    4. Optionally override should_run_checks_this_tick() to gate all checks
       on some condition (e.g. "automation disabled in config") without
       touching each check's own due-time bookkeeping.

    A check registered here is expected to already guard its own body in a
    broad except Exception, per this project's Circuit Breaker convention
    (see CLAUDE.md) - this loop does not add a second layer of exception
    handling around check.run(), so a check that raises will stop the daemon
    rather than being silently swallowed twice.
    """

    def __init__(self) -> None:
        self.shutdown_requested = False
        self.logger: logging.Logger = logging.getLogger(self.__class__.__name__)
        self._checks: list[_ScheduledCheck] = []

    def register_check(
        self,
        name: str,
        run: Callable[[], None],
        get_interval_seconds: Callable[[], float],
    ) -> None:
        """Register a periodic check to run whenever get_interval_seconds() have elapsed.

        Runs immediately on the first tick after startup (its internal
        "last run" starts at 0.0), matching how both daemons behaved before
        this was extracted.
        """
        self._checks.append(_ScheduledCheck(name, run, get_interval_seconds))

    @abstractmethod
    def load_config(self) -> None:
        """Load configuration for the first time. Raise if it's invalid - fatal at startup."""

    @abstractmethod
    def reload_config(self) -> None:
        """Reload configuration. Must not raise; keep the old config on failure."""

    def should_run_checks_this_tick(self) -> bool:
        """Whether the scheduled checks should be evaluated this fast-tick.

        Default: always. Override to gate all checks at once (e.g. hot water
        automation being disabled in config) without touching each check's
        own interval bookkeeping - a tick skipped this way does not count
        against any check's next-due calculation, so checks fire on their
        normal cadence again as soon as this returns True.
        """
        return True

    def _handle_shutdown(self, signum: int, _frame: Any) -> None:
        self.logger.info("Received shutdown signal (%d), shutting down gracefully...", signum)
        self.shutdown_requested = True

    def _run_one_tick(self) -> None:
        """Reload config, then run any checks that are due. Split out from run() for testability."""
        self.reload_config()

        if self.should_run_checks_this_tick():
            now = time_module.time()
            for check in self._checks:
                if check.due(now):
                    check.run()
                    check.mark_run(time_module.time())

    def run(
        self, *, fast_poll_interval_seconds: float = DEFAULT_FAST_POLL_INTERVAL_SECONDS
    ) -> None:
        """Main two-tier polling loop. Call after registering all checks."""
        self.load_config()

        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

        while not self.shutdown_requested:
            loop_start = time_module.time()

            self._run_one_tick()

            elapsed = time_module.time() - loop_start
            time_module.sleep(max(0.0, fast_poll_interval_seconds - elapsed))
