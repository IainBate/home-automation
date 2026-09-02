#!/usr/bin/env python3
# pylint: disable=wrong-import-position  # Imports after sys.path modification for src access
"""Ohme Charger Status Poller (continuous daemon).

Polls the Ohme charger from ONE long-lived authenticated session and caches
the result for everything else to read - see
src/api_clients/ohme_status_cache.py for why that matters: the battery
daemon, the hot water automation and the dashboard each used to open their
own client and perform a full Firebase login on every poll, roughly 3,000
logins a day between them against a third-party auth endpoint.

Deliberately a daemon rather than a cron entry, unlike this project's other
pollers (mg_saic_poller.py, claude_usage_poller.py): a one-shot script has to
log in every time it starts, so a cron-driven version at the cadence the
battery daemon needs would barely reduce the login count at all. The whole
point here is that the login happens once per daemon start and the session is
then reused for every subsequent poll.

Also deliberately NOT built on src/daemon_support/base_daemon.py: that loop
is synchronous and would need asyncio.run() per tick, which creates (and
destroys) a fresh event loop each time and so cannot hold an aiohttp session
open across polls - exactly the thing this daemon exists to do.

Nothing depends on this being up: every reader falls back to its own direct
Ohme call when the cache is missing or stale, which is precisely the
behaviour it had before this daemon existed.

Usage:
    python3 scripts/ohme_status_daemon.py [--config config.yaml]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import asyncio
import contextlib
import logging
import signal
from typing import Any

from hotwater_automation_core import get_config_path

from src.api_clients.ohme_ev_client import OhmeEVClient
from src.api_clients.ohme_status_cache import write_status_cache
from src.config_manager.config_manager import load_static_config
from src.daemon_support.base_daemon import setup_rotating_logger

logger = logging.getLogger("ohme_status_daemon")

DEFAULT_POLL_INTERVAL_SECONDS = 30.0
# After this many consecutive failed polls, drop the session and log in
# again from scratch - covers an expired/revoked token, which otherwise
# fails identically to a network blip forever.
RECONNECT_AFTER_CONSECUTIVE_FAILURES = 3
# Ceiling on the back-off between failed polls, so a sustained Ohme outage
# settles into occasional retries rather than hammering the same failing
# endpoint at full cadence (the failure mode this daemon exists to avoid).
MAX_BACKOFF_SECONDS = 300.0


class OhmeStatusDaemon:
    """Keeps one Ohme session open and refreshes the cached status from it."""

    def __init__(self, config_path: str, poll_interval_seconds: float) -> None:
        self.config_path = config_path
        self.poll_interval_seconds = poll_interval_seconds
        self.shutdown_requested = False
        self._client: OhmeEVClient | None = None
        self._consecutive_failures = 0

    def request_shutdown(self, signum: int, _frame: Any = None) -> None:
        """Signal handler - finish the current poll, then stop."""
        logger.info("Received shutdown signal (%d), shutting down gracefully...", signum)
        self.shutdown_requested = True

    async def _ensure_connected(self) -> None:
        if self._client is not None:
            return
        client = OhmeEVClient(config_path=self.config_path)
        await client.connect()
        self._client = client
        logger.info("Ohme session established (one login, reused for every poll)")

    async def _drop_connection(self) -> None:
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            await self._client.close()
        self._client = None

    async def _poll_once(self) -> bool:
        """One poll. Returns True if the cache was refreshed."""
        try:
            await self._ensure_connected()
            assert self._client is not None  # noqa: S101 - _ensure_connected sets it or raises
            status = await self._client.get_charger_status(use_cache=False)
            write_status_cache(status)
        except Exception:
            # Circuit Breaker (see CLAUDE.md): this daemon must survive any
            # Ohme/network failure. The cache is deliberately left untouched
            # rather than overwritten with an error - readers age it out on
            # their own and fall back to a direct call.
            self._consecutive_failures += 1
            logger.exception(
                "Ohme status poll failed (%d consecutive)", self._consecutive_failures
            )
            if self._consecutive_failures >= RECONNECT_AFTER_CONSECUTIVE_FAILURES:
                logger.warning("Dropping the Ohme session so the next poll logs in fresh")
                await self._drop_connection()
                self._consecutive_failures = 0
            return False

        if self._consecutive_failures:
            logger.info("Ohme status poll recovered after %d failures", self._consecutive_failures)
        self._consecutive_failures = 0
        logger.debug("Ohme status cached (power %sW)", status.get("power_watts"))
        return True

    def _sleep_seconds(self, *, succeeded: bool) -> float:
        if succeeded:
            return self.poll_interval_seconds
        # Exponential back-off on repeated failure, capped.
        backoff = self.poll_interval_seconds * (2 ** min(self._consecutive_failures, 6))
        return min(backoff, MAX_BACKOFF_SECONDS)

    async def run(self) -> None:
        """Poll until asked to stop."""
        logger.info(
            "Ohme Status Daemon starting (poll interval: %ss)...", self.poll_interval_seconds
        )
        try:
            while not self.shutdown_requested:
                succeeded = await self._poll_once()
                # Sleep in short slices so SIGTERM is acted on promptly
                # rather than after a full (possibly backed-off) interval.
                remaining = self._sleep_seconds(succeeded=succeeded)
                while remaining > 0 and not self.shutdown_requested:
                    await asyncio.sleep(min(1.0, remaining))
                    remaining -= 1.0
        finally:
            await self._drop_connection()
            logger.info("Ohme Status Daemon shutdown complete")


def _create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    return parser


def main() -> None:
    """Execute main entry point."""
    args = _create_argument_parser().parse_args()
    config_path = args.config or get_config_path()

    setup_rotating_logger("ohme_status_daemon", "ohme_status_daemon.log", level=logging.INFO)

    config = load_static_config(config_path)
    if config is None:
        print("Failed to load config.yaml (see logs above)")
        sys.exit(1)

    ohme_config = config.get("ohme_ev", {})
    if not ohme_config.get("enabled", False):
        logger.info("Ohme is disabled (ohme_ev.enabled: false in config.yaml) - exiting")
        sys.exit(0)

    poll_interval = float(
        ohme_config.get("status_poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
    )

    daemon = OhmeStatusDaemon(config_path, poll_interval)
    signal.signal(signal.SIGTERM, daemon.request_shutdown)
    signal.signal(signal.SIGINT, daemon.request_shutdown)

    asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
