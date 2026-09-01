"""Tests for resideo_client.py - token refresh/rotation, status parsing, error handling."""

from __future__ import annotations

import json
from unittest import mock

from src.api_clients import resideo_client


def _fake_response(payload, *, ok=True, status_code=200):
    response = mock.Mock()
    response.json.return_value = payload
    response.ok = ok
    response.status_code = status_code
    response.raise_for_status.side_effect = None if ok else Exception("HTTP error")
    return response


def test_refresh_access_token_returns_tokens_on_success():
    payload = {"access_token": "new-access", "refresh_token": "new-refresh"}
    with mock.patch.object(resideo_client.requests, "post", return_value=_fake_response(payload)):
        result = resideo_client.refresh_access_token("cid", "csecret", "old-refresh")

    assert result == {"access_token": "new-access", "refresh_token": "new-refresh"}


def test_refresh_access_token_falls_back_to_input_refresh_token_if_absent_from_response():
    payload = {"access_token": "new-access"}
    with mock.patch.object(resideo_client.requests, "post", return_value=_fake_response(payload)):
        result = resideo_client.refresh_access_token("cid", "csecret", "old-refresh")

    assert result["refresh_token"] == "old-refresh"


def test_refresh_access_token_returns_none_on_invalid_grant():
    response = mock.Mock()
    response.raise_for_status.side_effect = resideo_client.requests.HTTPError("400 invalid_grant")
    with mock.patch.object(resideo_client.requests, "post", return_value=response):
        result = resideo_client.refresh_access_token("cid", "csecret", "stale-refresh")

    assert result is None


def test_fetch_thermostat_status_converts_fahrenheit_and_parses_first_device():
    payload = [
        {
            "devices": [
                {
                    "userDefinedDeviceName": "Living Room",
                    "indoorTemperature": 68.0,
                    "units": "Fahrenheit",
                    "changeableValues": {"mode": "Heat", "heatSetpoint": 70.0, "coolSetpoint": 75.0},
                }
            ]
        }
    ]
    with mock.patch.object(resideo_client.requests, "get", return_value=_fake_response(payload)):
        result = resideo_client.fetch_thermostat_status("access-token")

    assert result["device_name"] == "Living Room"
    assert result["mode"] == "Heat"
    assert round(result["current_temperature_c"], 1) == 20.0
    assert round(result["heat_setpoint_c"], 1) == 21.1


def test_fetch_thermostat_status_returns_none_when_no_devices():
    with mock.patch.object(resideo_client.requests, "get", return_value=_fake_response([{"devices": []}])):
        result = resideo_client.fetch_thermostat_status("access-token")

    assert result is None


def test_fetch_resideo_status_disabled_returns_none():
    assert resideo_client.fetch_resideo_status({"resideo": {"enabled": False}}) is None


def test_fetch_resideo_status_uses_rotated_token_from_state_file_over_bootstrap(tmp_path):
    state_path = tmp_path / "resideo_token_state.json"
    state_path.write_text(json.dumps({"refresh_token": "rotated-refresh"}), encoding="utf-8")

    config = {
        "resideo": {
            "enabled": True,
            "client_id": "cid",
            "client_secret": "csecret",
            "refresh_token": "bootstrap-refresh",
        }
    }

    with (
        mock.patch.object(resideo_client, "get_resideo_token_state_path", lambda: str(state_path)),
        mock.patch.object(resideo_client, "refresh_access_token") as fake_refresh,
        mock.patch.object(resideo_client, "fetch_thermostat_status", return_value={"mode": "Heat"}),
    ):
        fake_refresh.return_value = {"access_token": "access", "refresh_token": "rotated-refresh"}
        result = resideo_client.fetch_resideo_status(config)

    fake_refresh.assert_called_once()
    assert fake_refresh.call_args[0][2] == "rotated-refresh"
    assert result == {"mode": "Heat"}


def test_fetch_resideo_status_persists_newly_rotated_token(tmp_path):
    state_path = tmp_path / "resideo_token_state.json"
    config = {
        "resideo": {
            "enabled": True,
            "client_id": "cid",
            "client_secret": "csecret",
            "refresh_token": "bootstrap-refresh",
        }
    }

    with (
        mock.patch.object(resideo_client, "get_resideo_token_state_path", lambda: str(state_path)),
        mock.patch.object(resideo_client, "refresh_access_token") as fake_refresh,
        mock.patch.object(resideo_client, "fetch_thermostat_status", return_value={"mode": "Heat"}),
    ):
        fake_refresh.return_value = {"access_token": "access", "refresh_token": "newly-rotated"}
        resideo_client.fetch_resideo_status(config)

    saved_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved_state["refresh_token"] == "newly-rotated"


def test_fetch_resideo_status_returns_none_instead_of_raising_on_unexpected_error(tmp_path):
    """Circuit Breaker: a caller collecting several subsystems in one pass (see
    status_collector.py) must not have one integration's unexpected exception
    (e.g. a state-file lock timeout) blank the whole snapshot.
    """
    config = {
        "resideo": {
            "enabled": True,
            "client_id": "cid",
            "client_secret": "csecret",
            "refresh_token": "bootstrap-refresh",
        }
    }

    with mock.patch.object(
        resideo_client, "get_resideo_token_state_path", side_effect=RuntimeError("disk exploded")
    ):
        result = resideo_client.fetch_resideo_status(config)

    assert result is None


def test_fetch_resideo_status_returns_none_when_misconfigured():
    config = {"resideo": {"enabled": True, "client_id": "cid"}}  # missing client_secret/refresh_token

    result = resideo_client.fetch_resideo_status(config)

    assert result is None
