"""Resideo (Honeywell Home) thermostat client - official OAuth2 API, read-only.

Unlike this project's other cloud clients (Ohme, MELCloud), Resideo's refresh
tokens are known to go stale after extended outages - Home Assistant users
report needing to re-authenticate "every month or two" (see
scripts/resideo_oauth_setup.py's docstring for sources). A refresh failure
here is a real, expected operating state, not a bug: fetch_resideo_status()
returns None and logs a clear "re-run resideo_oauth_setup.py" message rather
than raising, so a stale token degrades the dashboard's Resideo card to
"unavailable" without taking anything else down.

IMPORTANT: the exact Locations/device response field names below
(indoorTemperature, changeableValues.mode/heatSetpoint/coolSetpoint) are
based on this API's long-documented v2 schema, not a response verified
against a live account by this codebase - there was no way to do that without
completing the interactive OAuth consent step, which only the account owner
can do. Treat the first real run's output as the thing to check field names
against, not an assumption to trust blindly.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import requests

from src.utils.paths import get_resideo_token_state_path
from src.utils.state_store import locked_json_state, read_json_state

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_URL = "https://api.honeywellhome.com/oauth2/token"
DEFAULT_API_BASE_URL = "https://api.honeywellhome.com"
DEFAULT_TIMEOUT_SECONDS = 30


def refresh_access_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    token_url: str = DEFAULT_TOKEN_URL,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Exchange a refresh token for a fresh access token.

    Args:
        client_id: OAuth2 client ID from developer.honeywellhome.com.
        client_secret: OAuth2 client secret (keep in secrets.yaml, not config.yaml).
        refresh_token: Long-lived refresh token from resideo_oauth_setup.py
            (keep in secrets.yaml).
        token_url: Token endpoint - configurable in case Resideo's documented
            host changes; see module docstring.
        timeout_seconds: HTTP request timeout.

    Returns:
        Dict with "access_token" and "refresh_token" (Resideo may rotate the
        refresh token on each use - callers should persist the new one), or
        None if the refresh failed (commonly an expired/invalid refresh
        token - see module docstring).

    """
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    body = {"grant_type": "refresh_token", "refresh_token": refresh_token}

    try:
        response = requests.post(token_url, headers=headers, data=body, timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning(
            "Resideo token refresh failed (%s) - if this persists, the refresh token has "
            "likely expired; re-run scripts/resideo_oauth_setup.py",
            e,
        )
        return None

    access_token = payload.get("access_token")
    if not access_token:
        logger.warning("Resideo token refresh response had no access_token: %s", payload)
        return None

    return {
        "access_token": access_token,
        "refresh_token": payload.get("refresh_token", refresh_token),
    }


def fetch_thermostat_status(
    access_token: str,
    api_base_url: str = DEFAULT_API_BASE_URL,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Fetch the first thermostat device found across all locations on the account.

    Args:
        access_token: A fresh access token from refresh_access_token().
        api_base_url: Resideo API base URL - configurable; see module docstring.
        timeout_seconds: HTTP request timeout.

    Returns:
        Dict with "device_name", "mode", "current_temperature_c",
        "heat_setpoint_c", "cool_setpoint_c" (all temperatures converted from
        the API's Fahrenheit to Celsius), or None on error / no device found.

    """
    try:
        response = requests.get(
            f"{api_base_url}/v2/locations",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        locations = response.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("Failed to fetch Resideo locations: %s", e)
        return None

    for location in locations or []:
        devices = location.get("devices") or []
        if devices:
            return _parse_device(devices[0])

    logger.warning("Resideo account has no locations/devices")
    return None


def _fahrenheit_to_celsius(fahrenheit: float | None) -> float | None:
    return None if fahrenheit is None else (fahrenheit - 32) * 5.0 / 9.0


def _parse_device(device: dict[str, Any]) -> dict[str, Any]:
    changeable = device.get("changeableValues") or {}
    units = device.get("units")
    if units is None:
        # Fahrenheit is this API's documented convention, but the schema was
        # never verified against a live account (see module docstring) - log
        # so a wrong-looking temperature on the dashboard has a diagnostic
        # trail rather than silently guessing with no trace of the guess.
        logger.warning("Resideo device response has no 'units' field - assuming Fahrenheit")
        units = "Fahrenheit"

    indoor_temperature = device.get("indoorTemperature")
    heat_setpoint = changeable.get("heatSetpoint")
    cool_setpoint = changeable.get("coolSetpoint")

    if units == "Fahrenheit":
        indoor_temperature = _fahrenheit_to_celsius(indoor_temperature)
        heat_setpoint = _fahrenheit_to_celsius(heat_setpoint)
        cool_setpoint = _fahrenheit_to_celsius(cool_setpoint)

    return {
        "device_name": device.get("userDefinedDeviceName") or device.get("name"),
        "mode": changeable.get("mode"),
        "current_temperature_c": indoor_temperature,
        "heat_setpoint_c": heat_setpoint,
        "cool_setpoint_c": cool_setpoint,
    }


def fetch_resideo_status(config: dict[str, Any]) -> dict[str, Any] | None:
    """Read-only Resideo thermostat snapshot: refresh token, then fetch status.

    Args:
        config: Full static config - reads its "resideo" section (client_id,
            client_secret, refresh_token expected merged in from secrets.yaml).

    Returns:
        Dict from fetch_thermostat_status(), or None if disabled, misconfigured,
        or anything else failed (fail-fast, matches this codebase's other
        hardware/cloud clients) - including a token-state file lock timeout,
        which is why this wraps _fetch_resideo_status_unsafe() in a broad
        except rather than only handling the two HTTP calls' own narrower
        errors: a caller collecting several subsystems in one pass (see
        src/dashboard/status_collector.py) must not have one integration's
        unexpected exception blank the whole snapshot.

    """
    try:
        return _fetch_resideo_status_unsafe(config)
    except Exception:
        # Circuit Breaker: see docstring above.
        logger.exception("Unexpected error reading Resideo status")
        return None


def _fetch_resideo_status_unsafe(config: dict[str, Any]) -> dict[str, Any] | None:
    resideo_config = config.get("resideo", {})
    if not resideo_config.get("enabled", False):
        return None

    client_id = resideo_config.get("client_id")
    client_secret = resideo_config.get("client_secret")
    bootstrap_refresh_token = resideo_config.get("refresh_token")
    if not client_id or not client_secret or not bootstrap_refresh_token:
        logger.error(
            "resideo.client_id/client_secret/refresh_token are not fully set - "
            "see config.yaml's resideo section and scripts/resideo_oauth_setup.py"
        )
        return None

    token_url = resideo_config.get("token_url", DEFAULT_TOKEN_URL)
    api_base_url = resideo_config.get("api_base_url", DEFAULT_API_BASE_URL)
    timeout_seconds = resideo_config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)

    # The token state file holds whichever refresh token is currently valid,
    # since Resideo commonly rotates it on every use - see get_resideo_token_state_path().
    token_state = read_json_state(get_resideo_token_state_path())
    refresh_token = token_state.get("refresh_token") or bootstrap_refresh_token

    tokens = refresh_access_token(client_id, client_secret, refresh_token, token_url, timeout_seconds)
    if tokens is None:
        return None

    if tokens["refresh_token"] != refresh_token:
        with locked_json_state(get_resideo_token_state_path()) as state:
            state["refresh_token"] = tokens["refresh_token"]

    return fetch_thermostat_status(tokens["access_token"], api_base_url, timeout_seconds)
