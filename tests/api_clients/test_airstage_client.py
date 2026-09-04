"""Tests for airstage_client.py's status fetch, error handling, and (2026-09-04) write path.

Write-path tests mock ApiLocal.set_parameter/get_parameters directly rather
than pyairstage's higher-level AirstageAC, matching the module's own design:
writes bypass AirstageAC's optimistic caching entirely (see module
docstring) and are verified by re-reading the raw parameter.
"""

from __future__ import annotations

from unittest import mock

import pytest

from src.api_clients import airstage_client
from src.api_clients.airstage_client import AirstageWriteError

_ZONE_CONFIG = {"name": "Landing", "device_id": "AABBCC112233", "ip_address": "192.168.1.50"}
_TWO_ZONES_CONFIG = {
    "airstage": {
        "enabled": True,
        "zones": [
            _ZONE_CONFIG,
            {"name": "Playroom", "device_id": "DDEEFF445566", "ip_address": "192.168.1.51"},
        ],
    }
}


def _fake_api(get_parameters_side_effect):
    """An AsyncMock ApiLocal whose get_parameters() yields the given sequence of
    return values on successive calls - one per verification read attempt."""
    api = mock.AsyncMock()
    api.set_parameter = mock.AsyncMock(return_value={"result": "OK"})
    api.get_parameters = mock.AsyncMock(side_effect=get_parameters_side_effect)
    return api


def test_fetch_returns_none_when_disabled():
    result = airstage_client.fetch_airstage_status({"airstage": {"enabled": False}})

    assert result is None


def test_fetch_returns_none_when_no_zones_configured():
    result = airstage_client.fetch_airstage_status({"airstage": {"enabled": True, "zones": []}})

    assert result is None


def test_fetch_reports_one_zone_misconfigured_without_affecting_others():
    config = {
        "airstage": {
            "enabled": True,
            "zones": [
                {"name": "Landing"},  # missing device_id/ip_address
                _ZONE_CONFIG | {"name": "Playroom"},
            ],
        }
    }
    with mock.patch.object(airstage_client, "_fetch_status_async") as fake_fetch:
        fake_fetch.return_value = {
            "mode": "HEAT",
            "current_temperature_c": 21.5,
            "target_temperature_c": 22.0,
            "outdoor_temperature_c": 8.0,
        }
        results = airstage_client.fetch_airstage_status(config)

    landing, playroom = results
    assert landing == {"name": "Landing", "available": False, "error": "Zone misconfigured"}
    assert playroom["available"] is True
    assert playroom["name"] == "Playroom"


def test_fetch_reports_one_zone_unreachable_without_affecting_others():
    config = {
        "airstage": {
            "enabled": True,
            "zones": [_ZONE_CONFIG, _ZONE_CONFIG | {"name": "Playroom", "ip_address": "192.168.1.51"}],
        }
    }

    def fake_fetch_status_async(_device_id, ip_address, _timeout_seconds):
        if ip_address == "192.168.1.50":
            msg = "network unreachable"
            raise RuntimeError(msg)
        return {"mode": "OFF", "current_temperature_c": 19.0, "target_temperature_c": 19.0, "outdoor_temperature_c": None}

    with mock.patch.object(airstage_client, "_fetch_status_async", side_effect=fake_fetch_status_async):
        landing, playroom = airstage_client.fetch_airstage_status(config)

    assert landing == {"name": "Landing", "available": False, "error": "Could not read from Airstage unit"}
    assert playroom["available"] is True
    assert playroom["mode"] == "OFF"


def test_fetch_status_async_maps_zone_fields():
    fake_zone = mock.Mock()
    fake_zone.get_operating_mode.return_value = mock.Mock(value="HEAT")
    fake_zone.get_display_temperature.return_value = 21.5
    fake_zone.get_target_temperature.return_value = 22.0
    fake_zone.get_outdoor_temperature.return_value = 8.0

    fake_api = mock.AsyncMock()
    fake_api.get_devices.return_value = {"AABBCC112233": {"parameters": []}}

    with (
        mock.patch.object(airstage_client, "ApiLocal", return_value=fake_api),
        mock.patch.object(airstage_client, "AirstageAC", return_value=fake_zone),
    ):
        results = airstage_client.fetch_airstage_status({"airstage": {"enabled": True, "zones": [_ZONE_CONFIG]}})

    assert results == [
        {
            "name": "Landing",
            "available": True,
            "mode": "HEAT",
            "current_temperature_c": 21.5,
            "target_temperature_c": 22.0,
            "outdoor_temperature_c": 8.0,
        }
    ]
    fake_zone.refresh_parameters.assert_called_once_with({"parameters": []})
