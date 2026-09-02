"""Tests for the MELCloud login-token cache and its fallbacks.

The cache must never be load-bearing: every failure path (missing, corrupt,
expired, wrong account, or a token the server has since rejected) has to end
in a normal login rather than a broken hot water automation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest

from src.api_clients import melcloud_client, melcloud_token_cache


def _patch_path(tmp_path):
    return mock.patch.object(
        melcloud_token_cache,
        "get_melcloud_token_cache_path",
        lambda: str(tmp_path / "melcloud_token.json"),
    )


def test_read_cached_token_returns_none_when_missing(tmp_path):
    with _patch_path(tmp_path):
        assert melcloud_token_cache.read_cached_token("a@example.com") is None


def test_write_then_read_round_trips(tmp_path):
    with _patch_path(tmp_path):
        melcloud_token_cache.write_cached_token("a@example.com", "tok-123")
        assert melcloud_token_cache.read_cached_token("a@example.com") == "tok-123"


def test_read_cached_token_ignores_a_different_account(tmp_path):
    """A credentials change must not silently keep using the old account's token."""
    with _patch_path(tmp_path):
        melcloud_token_cache.write_cached_token("old@example.com", "tok-123")
        assert melcloud_token_cache.read_cached_token("new@example.com") is None


def test_read_cached_token_ignores_an_expired_token(tmp_path):
    old = {
        "email": "a@example.com",
        "token": "tok-123",
        "obtained_at": (datetime.now(tz=UTC) - timedelta(days=3)).isoformat(),
    }
    (tmp_path / "melcloud_token.json").write_text(json.dumps(old), encoding="utf-8")

    with _patch_path(tmp_path):
        assert melcloud_token_cache.read_cached_token("a@example.com") is None


def test_read_cached_token_ignores_malformed_records(tmp_path):
    path = tmp_path / "melcloud_token.json"
    for record in (
        {"email": "a@example.com", "token": "tok"},  # no obtained_at
        {"email": "a@example.com", "obtained_at": datetime.now(tz=UTC).isoformat()},  # no token
        {"email": "a@example.com", "token": "tok", "obtained_at": "nonsense"},
        {"email": "a@example.com", "token": "tok", "obtained_at": "2026-09-03 01:00:00"},  # naive
    ):
        path.write_text(json.dumps(record), encoding="utf-8")
        with _patch_path(tmp_path):
            assert melcloud_token_cache.read_cached_token("a@example.com") is None


def test_clear_cached_token_forces_a_fresh_login(tmp_path):
    with _patch_path(tmp_path):
        melcloud_token_cache.write_cached_token("a@example.com", "tok-123")
        melcloud_token_cache.clear_cached_token()
        assert melcloud_token_cache.read_cached_token("a@example.com") is None


# --- MelCloudClient.connect() integration ---------------------------------


def _client_with_config():
    client = melcloud_client.MelCloudClient.__new__(melcloud_client.MelCloudClient)
    client.melcloud_config = {"email": "a@example.com", "password": "pw"}
    client.config_path = "config.yaml"
    client._session = None
    client._token = None
    client.device = None
    client.session_established = False
    client._cached_status = None
    client._cache_timestamp = 0.0
    return client


class _FakeAtwDevice:
    name = "Tank"

    async def update(self):
        return True


def _patch_status_warmup(client):
    """connect() warms its status cache before returning; these tests are about
    the login path, not status parsing."""
    return mock.patch.object(client, "get_tank_status", new=mock.AsyncMock(return_value={}))


@pytest.mark.asyncio
async def test_connect_skips_login_when_a_cached_token_works():
    client = _client_with_config()
    devices = {melcloud_client.DEVICE_TYPE_ATW: [_FakeAtwDevice()]}

    with (
        mock.patch.object(melcloud_client, "ClientSession", return_value=mock.AsyncMock()),
        mock.patch.object(melcloud_client, "read_cached_token", return_value="cached-tok"),
        _patch_status_warmup(client),
        mock.patch.object(melcloud_client.pymelcloud, "login", new=mock.AsyncMock()) as login,
        mock.patch.object(
            melcloud_client.pymelcloud, "get_devices", new=mock.AsyncMock(return_value=devices)
        ) as get_devices,
    ):
        await client.connect()

    login.assert_not_awaited()  # the whole point: no login round-trip
    assert get_devices.await_args[0][0] == "cached-tok"


@pytest.mark.asyncio
async def test_connect_falls_back_to_login_when_cached_token_is_rejected():
    """A stale token must self-heal, not wedge hot water automation until
    someone deletes the cache file by hand."""
    client = _client_with_config()
    devices = {melcloud_client.DEVICE_TYPE_ATW: [_FakeAtwDevice()]}
    get_devices = mock.AsyncMock(side_effect=[RuntimeError("401 unauthorized"), devices])

    with (
        mock.patch.object(melcloud_client, "ClientSession", return_value=mock.AsyncMock()),
        mock.patch.object(melcloud_client, "read_cached_token", return_value="stale-tok"),
        _patch_status_warmup(client),
        mock.patch.object(melcloud_client, "clear_cached_token") as clear_cache,
        mock.patch.object(melcloud_client, "write_cached_token"),
        mock.patch.object(
            melcloud_client.pymelcloud, "login", new=mock.AsyncMock(return_value="fresh-tok")
        ) as login,
        mock.patch.object(melcloud_client.pymelcloud, "get_devices", new=get_devices),
    ):
        await client.connect()

    clear_cache.assert_called_once()
    login.assert_awaited_once()
    assert get_devices.await_args[0][0] == "fresh-tok"


@pytest.mark.asyncio
async def test_connect_reports_a_real_failure_when_there_was_no_cached_token():
    """Without a cached token to blame, a device-list failure is a real error -
    it must not be retried as if it were a stale-token problem."""
    client = _client_with_config()

    with (
        mock.patch.object(melcloud_client, "ClientSession", return_value=mock.AsyncMock()),
        mock.patch.object(melcloud_client, "read_cached_token", return_value=None),
        mock.patch.object(melcloud_client, "write_cached_token"),
        mock.patch.object(
            melcloud_client.pymelcloud, "login", new=mock.AsyncMock(return_value="tok")
        ),
        mock.patch.object(
            melcloud_client.pymelcloud,
            "get_devices",
            new=mock.AsyncMock(side_effect=RuntimeError("network down")),
        ),
        pytest.raises(melcloud_client.MelCloudConnectionError),
    ):
        await client.connect()


@pytest.mark.asyncio
async def test_login_failure_still_raises_authentication_error():
    client = _client_with_config()

    with (
        mock.patch.object(melcloud_client, "ClientSession", return_value=mock.AsyncMock()),
        mock.patch.object(melcloud_client, "read_cached_token", return_value=None),
        mock.patch.object(
            melcloud_client.pymelcloud,
            "login",
            new=mock.AsyncMock(side_effect=RuntimeError("bad password")),
        ),
        pytest.raises(melcloud_client.MelCloudAuthenticationError),
    ):
        await client.connect()
