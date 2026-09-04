"""Tests for airstage_client.py's status fetch, error handling, and (2026-09-04) write path.

Write-path tests mock ApiLocal.set_parameter/get_parameters directly rather
than pyairstage's higher-level AirstageAC, matching the module's own design:
writes bypass AirstageAC's optimistic caching entirely (see module
docstring) and are verified by re-reading the raw parameter.
"""

from __future__ import annotations

import asyncio
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


# ---------------------------------------------------------------------------
# Write path (2026-09-04)
# ---------------------------------------------------------------------------


@mock.patch("asyncio.sleep", new_callable=mock.AsyncMock)
def test_set_airstage_power_verifies_on_first_read(mock_sleep):
    fake_api = _fake_api([{"iu_onoff": "1"}])

    with mock.patch.object(airstage_client, "ApiLocal", return_value=fake_api):
        result = airstage_client.set_airstage_power(_TWO_ZONES_CONFIG, True, zone_name="Landing")

    assert result == {"Landing": True}
    fake_api.set_parameter.assert_awaited_once_with(
        "AABBCC112233", airstage_client.ACParameter.ONOFF_MODE, "1"
    )


@mock.patch("asyncio.sleep", new_callable=mock.AsyncMock)
def test_write_verification_retries_until_the_value_settles(mock_sleep):
    """A write that genuinely succeeded can read back stale on the first attempt or
    two (device propagation lag, see module docstring) - must not be reported as
    failed just because the first read-back doesn't match yet."""
    fake_api = _fake_api(
        [{"iu_set_tmp": "190"}, {"iu_set_tmp": "190"}, {"iu_set_tmp": "210"}]
    )

    with mock.patch.object(airstage_client, "ApiLocal", return_value=fake_api):
        result = airstage_client.set_airstage_temperature(_TWO_ZONES_CONFIG, 21.0, zone_name="Landing")

    assert result == {"Landing": True}
    assert fake_api.get_parameters.await_count == 3


@mock.patch("asyncio.sleep", new_callable=mock.AsyncMock)
def test_write_verification_fails_after_max_attempts_never_raises_to_caller(mock_sleep):
    """A write acked by the device (result: OK) but never actually applied - see
    module docstring on why the HTTP response can't be trusted - must be reported
    as a verified failure (False), not silently treated as success, and must not
    raise out of set_airstage_temperature (Circuit Breaker convention)."""
    fake_api = _fake_api([{"iu_set_tmp": "190"}] * airstage_client.WRITE_VERIFY_MAX_ATTEMPTS)

    with mock.patch.object(airstage_client, "ApiLocal", return_value=fake_api):
        result = airstage_client.set_airstage_temperature(_TWO_ZONES_CONFIG, 21.0, zone_name="Landing")

    assert result == {"Landing": False}
    assert fake_api.get_parameters.await_count == airstage_client.WRITE_VERIFY_MAX_ATTEMPTS


@mock.patch("asyncio.sleep", new_callable=mock.AsyncMock)
def test_write_and_verify_raises_airstage_write_error_internally(mock_sleep):
    """Unit test of the internal helper directly, to pin down the exact exception
    type _write_zone_parameter's broad except is expected to catch."""
    fake_api = _fake_api([{"iu_set_tmp": "190"}] * airstage_client.WRITE_VERIFY_MAX_ATTEMPTS)

    with pytest.raises(AirstageWriteError):
        asyncio.run(
            airstage_client._write_and_verify(
                fake_api, "AABBCC112233", airstage_client.ACParameter.TARGET_TEMPERATURE, "210"
            )
        )


@mock.patch("asyncio.sleep", new_callable=mock.AsyncMock)
def test_set_airstage_mode_targets_all_zones_with_no_zone_parameter(mock_sleep):
    """set_airstage_mode has no zone_name parameter at all - the shared outdoor
    unit means mode is a whole-system property, enforced structurally here."""
    fake_api = _fake_api([{"iu_op_mode": "4"}])

    with mock.patch.object(airstage_client, "ApiLocal", return_value=fake_api):
        result = airstage_client.set_airstage_mode(_TWO_ZONES_CONFIG, "heat")

    assert result == {"Landing": True, "Playroom": True}
    assert fake_api.set_parameter.await_count == 2
    for call in fake_api.set_parameter.await_args_list:
        assert call.args[1] == airstage_client.ACParameter.OPERATION_MODE
        assert call.args[2] == "4"  # OperationMode.HEAT


@mock.patch("asyncio.sleep", new_callable=mock.AsyncMock)
def test_set_airstage_mode_one_zone_failing_does_not_hide_the_others_result(mock_sleep):
    """One zone's write failing must still report the other zone's real result -
    the caller (future hvac_decision_logic.py) needs per-zone truth to implement
    the spec's retry/revert without one zone masking the other (plan doc §8.7)."""

    calls = {"n": 0}

    def make_api(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Landing: write silently discarded (mirrors the real firmware bug).
            return _fake_api([{"iu_op_mode": "2"}] * airstage_client.WRITE_VERIFY_MAX_ATTEMPTS)
        return _fake_api([{"iu_op_mode": "4"}])  # Playroom: verifies fine

    with mock.patch.object(airstage_client, "ApiLocal", side_effect=make_api):
        result = airstage_client.set_airstage_mode(_TWO_ZONES_CONFIG, "heat")

    assert result == {"Landing": False, "Playroom": True}


def test_set_airstage_mode_rejects_invalid_mode():
    with pytest.raises(ValueError, match="Invalid mode"):
        airstage_client.set_airstage_mode(_TWO_ZONES_CONFIG, "blizzard")


@mock.patch("asyncio.sleep", new_callable=mock.AsyncMock)
def test_set_airstage_temperature_rounds_to_nearest_half_degree_and_scales_by_ten(mock_sleep):
    """21.3°C should round to 21.5°C, then scale by 10 for the wire format ("215")."""
    fake_api = _fake_api([{"iu_set_tmp": "215"}])

    with mock.patch.object(airstage_client, "ApiLocal", return_value=fake_api):
        result = airstage_client.set_airstage_temperature(_TWO_ZONES_CONFIG, 21.3, zone_name="Landing")

    assert result == {"Landing": True}
    fake_api.set_parameter.assert_awaited_once_with(
        "AABBCC112233", airstage_client.ACParameter.TARGET_TEMPERATURE, "215"
    )


@mock.patch("asyncio.sleep", new_callable=mock.AsyncMock)
def test_set_airstage_minimum_heat_wire_values(mock_sleep):
    fake_api = _fake_api([{"iu_min_heat": "1"}])

    with mock.patch.object(airstage_client, "ApiLocal", return_value=fake_api):
        result = airstage_client.set_airstage_minimum_heat(_TWO_ZONES_CONFIG, True, zone_name="Landing")

    assert result == {"Landing": True}
    fake_api.set_parameter.assert_awaited_once_with(
        "AABBCC112233", airstage_client.ACParameter.MINIMUM_HEAT, "1"
    )


def test_set_airstage_power_returns_empty_dict_when_disabled():
    assert airstage_client.set_airstage_power({"airstage": {"enabled": False}}, True) == {}


def test_set_airstage_power_returns_empty_dict_for_unknown_zone():
    assert airstage_client.set_airstage_power(_TWO_ZONES_CONFIG, True, zone_name="Attic") == {}
