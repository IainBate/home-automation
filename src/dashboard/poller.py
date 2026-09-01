"""Background thread that keeps a cached dashboard status snapshot fresh.

Flask requests never trigger a live hardware/API read themselves - they just
read whatever this poller last cached. That keeps the dashboard's load on the
SolaX inverters/Ohme/MELCloud constant regardless of how many phones have the
page open, and keeps a slow/stuck upstream call from blocking a page load.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class StatusPoller:
    """Runs collect_status(config) on a timer in a background thread."""

    def __init__(
        self,
        collect_status: Callable[[], dict[str, Any]],
        *,
        poll_interval_seconds: float,
    ) -> None:
        self._collect_status = collect_status
        self._poll_interval_seconds = poll_interval_seconds
        self._lock = threading.Lock()
        self._latest: dict[str, Any] | None = None
        self._last_poll_monotonic: float | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background poll thread and return immediately.

        The first collection happens inside the thread, not here - if a
        hardware/API integration is slow or unreachable, that must not block
        Flask from starting to listen at all. Until the first poll finishes,
        latest() returns (None, None) and the page shows "waiting for first
        reading" (see static_page.py).
        """
        self._thread = threading.Thread(target=self._run, name="dashboard-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval_seconds)

    def latest(self) -> tuple[dict[str, Any] | None, float | None]:
        """Return (snapshot, age_seconds) - age_seconds is None if never polled yet."""
        with self._lock:
            if self._latest is None or self._last_poll_monotonic is None:
                return None, None
            age_seconds = time.monotonic() - self._last_poll_monotonic
            return self._latest, age_seconds

    def _poll_once(self) -> None:
        try:
            snapshot = self._collect_status()
        except Exception:
            # Circuit Breaker: a bug in the collector must not kill the poll
            # thread and freeze the dashboard on stale data forever.
            logger.exception("Unexpected error collecting dashboard status")
            return

        with self._lock:
            self._latest = snapshot
            self._last_poll_monotonic = time.monotonic()

    def _run(self) -> None:
        self._poll_once()
        while not self._stop_event.wait(self._poll_interval_seconds):
            self._poll_once()
