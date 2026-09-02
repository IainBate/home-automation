"""Tests for the Ohme status poller daemon.

The behaviours that matter: it logs in ONCE and reuses the session (the whole
reason it exists), it never overwrites a good cache with an error, and it
survives any Ohme/network failure rather than dying and leaving every
consumer permanently falling back to its own direct calls.
"""

from __future__ import annotations

from unittest import mock

import ohme_status_daemon as daemon_module
import pytest


def _daemon():
    return daemon_module.OhmeStatusDaemon("config.yaml", poll_interval_seconds=0.01)


@pytest.mark.asyncio
async def test_poll_writes_the_cache_and_connects_only_once():
    daemon = _daemon()
    client = mock.AsyncMock()
    client.get_charger_status.return_value = {"power_watts": 7200}

    with (
        mock.patch.object(daemon_module, "OhmeEVClient", return_value=client) as client_cls,
        mock.patch.object(daemon_module, "write_status_cache") as write_cache,
    ):
        assert await daemon._poll_once() is True
        assert await daemon._poll_once() is True
        assert await daemon._poll_once() is True

    # One login for three polls - this is the point of the daemon.
    client_cls.assert_called_once()
    client.connect.assert_awaited_once()
    assert client.get_charger_status.await_count == 3
    assert write_cache.call_count == 3


@pytest.mark.asyncio
async def test_a_failed_poll_leaves_the_previous_cache_untouched():
    """Readers age the cache out on their own; overwriting it with an error
    would throw away a perfectly good recent reading."""
    daemon = _daemon()
    client = mock.AsyncMock()
    client.get_charger_status.side_effect = RuntimeError("Ohme 503")

    with (
        mock.patch.object(daemon_module, "OhmeEVClient", return_value=client),
        mock.patch.object(daemon_module, "write_status_cache") as write_cache,
    ):
        assert await daemon._poll_once() is False

    write_cache.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_failures_drop_the_session_so_the_next_poll_logs_in_fresh():
    """Covers an expired or revoked token, which otherwise looks identical to
    a network blip forever."""
    daemon = _daemon()
    client = mock.AsyncMock()
    client.get_charger_status.side_effect = RuntimeError("401 unauthorized")

    with (
        mock.patch.object(daemon_module, "OhmeEVClient", return_value=client) as client_cls,
        mock.patch.object(daemon_module, "write_status_cache"),
    ):
        for _ in range(daemon_module.RECONNECT_AFTER_CONSECUTIVE_FAILURES):
            await daemon._poll_once()

        assert daemon._client is None  # session dropped
        client.close.assert_awaited()

        # ...and the next poll builds a new client rather than reusing nothing.
        client.get_charger_status.side_effect = None
        client.get_charger_status.return_value = {"power_watts": 0}
        assert await daemon._poll_once() is True

    assert client_cls.call_count == 2


@pytest.mark.asyncio
async def test_a_connect_failure_is_not_fatal():
    daemon = _daemon()

    with (
        mock.patch.object(daemon_module, "OhmeEVClient", side_effect=RuntimeError("no network")),
        mock.patch.object(daemon_module, "write_status_cache") as write_cache,
    ):
        assert await daemon._poll_once() is False

    write_cache.assert_not_called()


def test_backoff_grows_on_failure_and_is_capped():
    """A sustained Ohme outage must settle into occasional retries, not keep
    hammering the failing endpoint at full cadence."""
    daemon = daemon_module.OhmeStatusDaemon("config.yaml", poll_interval_seconds=30.0)

    assert daemon._sleep_seconds(succeeded=True) == 30.0

    daemon._consecutive_failures = 1
    first = daemon._sleep_seconds(succeeded=False)
    daemon._consecutive_failures = 3
    later = daemon._sleep_seconds(succeeded=False)
    assert later > first

    daemon._consecutive_failures = 99
    assert daemon._sleep_seconds(succeeded=False) == daemon_module.MAX_BACKOFF_SECONDS


@pytest.mark.asyncio
async def test_run_stops_promptly_on_shutdown_request():
    daemon = _daemon()
    client = mock.AsyncMock()
    client.get_charger_status.return_value = {"power_watts": 0}

    with (
        mock.patch.object(daemon_module, "OhmeEVClient", return_value=client),
        mock.patch.object(daemon_module, "write_status_cache") as write_cache,
    ):
        # SIGTERM arrives during the first poll: the loop must finish that
        # poll, then exit rather than sleeping out the full interval.
        async def poll_then_shutdown(*_args, **_kwargs):
            daemon.request_shutdown(15)
            return {"power_watts": 0}

        client.get_charger_status.side_effect = poll_then_shutdown
        await daemon.run()

    write_cache.assert_called_once()
    # The session is always closed on the way out.
    client.close.assert_awaited()
