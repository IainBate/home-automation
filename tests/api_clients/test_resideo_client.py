"""Tests for resideo_client.py - config validation, evohome-async integration, zone parsing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from src.api_clients import resideo_client


def _make_client(zones):
    system = SimpleNamespace(zones=zones)
    gateway = SimpleNamespace(systems=[system])
    location = SimpleNamespace(gateways=[gateway])
    client = mock.Mock()
    client.update = mock.AsyncMock()
    client.locations = [location]
    return client


def test_fetch_resideo_status_disabled_returns_none():
    assert resideo_client.fetch_resideo_status({"resideo": {"enabled": False}}) is None


def test_fetch_resideo_status_returns_none_when_credentials_missing():
    config = {"resideo": {"enabled": True, "username": "user@example.com"}}  # no password

    assert resideo_client.fetch_resideo_status(config) is None


def test_fetch_resideo_status_returns_zone_snapshot():
    zone = SimpleNamespace(name="Hall", mode="Heat", temperature=20.0, target_heat_temperature=21.0)
    fake_client = _make_client([zone])
    config = {"resideo": {"enabled": True, "username": "user@example.com", "password": "secret"}}

    with mock.patch.object(resideo_client, "EvohomeClient", return_value=fake_client):
        result = resideo_client.fetch_resideo_status(config)

    fake_client.update.assert_awaited_once()
    assert result == {
        "device_name": "Hall",
        "mode": "Heat",
        "current_temperature_c": 20.0,
        "target_temperature_c": 21.0,
    }


def test_fetch_resideo_status_unwraps_enum_mode():
    zone = SimpleNamespace(
        name="Hall", mode=SimpleNamespace(value="Heat"), temperature=20.0, target_heat_temperature=21.0
    )
    fake_client = _make_client([zone])
    config = {"resideo": {"enabled": True, "username": "user@example.com", "password": "secret"}}

    with mock.patch.object(resideo_client, "EvohomeClient", return_value=fake_client):
        result = resideo_client.fetch_resideo_status(config)

    assert result["mode"] == "Heat"


def test_fetch_resideo_status_returns_none_when_no_zones():
    fake_client = _make_client([])
    config = {"resideo": {"enabled": True, "username": "user@example.com", "password": "secret"}}

    with mock.patch.object(resideo_client, "EvohomeClient", return_value=fake_client):
        result = resideo_client.fetch_resideo_status(config)

    assert result is None


def test_fetch_resideo_status_returns_none_instead_of_raising_on_unexpected_error():
    """Circuit Breaker: a caller collecting several subsystems in one pass (see
    status_collector.py) must not have one integration's unexpected exception
    blank the whole snapshot.
    """
    config = {"resideo": {"enabled": True, "username": "user@example.com", "password": "secret"}}

    with mock.patch.object(resideo_client, "EvohomeClient", side_effect=RuntimeError("boom")):
        result = resideo_client.fetch_resideo_status(config)

    assert result is None
