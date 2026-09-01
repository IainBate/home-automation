"""Tests for poller.py's background caching and failure isolation."""

from __future__ import annotations

import time

from src.dashboard.poller import StatusPoller


def test_start_returns_immediately_without_waiting_for_first_poll():
    """start() must not block on the first collection - a slow/unreachable
    integration must not delay Flask from listening (see poller.py docstring).
    """

    def slow_collect():
        time.sleep(1.0)
        return {"value": 1}

    poller = StatusPoller(slow_collect, poll_interval_seconds=60)

    start_time = time.monotonic()
    poller.start()
    try:
        elapsed = time.monotonic() - start_time
        assert elapsed < 0.5
    finally:
        poller.stop()


def test_first_poll_populates_cache_once_the_background_thread_runs():
    poller = StatusPoller(lambda: {"value": 1}, poll_interval_seconds=60)

    poller.start()
    try:
        deadline = time.monotonic() + 2.0
        snapshot = None
        while time.monotonic() < deadline:
            snapshot, age_seconds = poller.latest()
            if snapshot is not None:
                break
            time.sleep(0.02)
    finally:
        poller.stop()

    assert snapshot == {"value": 1}
    assert age_seconds is not None


def test_latest_before_start_returns_none():
    poller = StatusPoller(lambda: {"value": 1}, poll_interval_seconds=60)

    snapshot, age_seconds = poller.latest()

    assert snapshot is None
    assert age_seconds is None


def test_poll_loop_survives_collector_exceptions():
    calls = {"count": 0}

    def flaky_collect():
        calls["count"] += 1
        if calls["count"] == 1:
            msg = "simulated transient failure"
            raise RuntimeError(msg)
        return {"value": calls["count"]}

    poller = StatusPoller(flaky_collect, poll_interval_seconds=0.05)
    poller.start()
    try:
        deadline = time.monotonic() + 2.0
        snapshot = None
        while time.monotonic() < deadline:
            snapshot, _ = poller.latest()
            if snapshot is not None:
                break
            time.sleep(0.02)
    finally:
        poller.stop()

    assert snapshot is not None
    assert snapshot["value"] >= 2
