"""Resideo (Honeywell Home) thermostat client - read-only, via the evohome-async library.

Uses `evohomeasync2` (PyPI: evohome-async), an actively-maintained,
reverse-engineered client for the same TCC v2 REST API ("tccna.resideo.com")
the Resideo/Honeywell Home app itself uses for Evohome-family devices
(including the T6R this project's household uses) - confirmed against the
installed package's own hardcoded hostname and its "EMEA-V1" (Europe/Middle
East/Africa) application scope, matching a UK account. This is NOT the same
backend evohome-async's sibling `evohomeasync` (v0) or the unrelated
`AIOSomecomfort` package target - those are the older US-only Total Connect
Comfort platform and do not work for this account/device.

Deliberately used INSTEAD of Resideo's official OAuth2 developer API
(src/api_clients/resideo_client.py previously used that - see git history):
getting a developer app approved through developer.honeywellhome.com proved
impractical, whereas this authenticates with the account's normal
username/password - the same credentials the phone app uses - via the
library's embedded, already-registered EMEA application ID, no developer
registration needed. Trade-off: this is an unofficial/reverse-engineered
client, not held to any API stability contract, so it could break on a
backend change with no notice - same risk category as this project's Ohme
client.

No cross-run token caching: each call logs in fresh (password grant), same
one-shot-per-poll pattern as ohme_ev_client.py/melcloud_client.py used from
status_collector.py - simpler than persisting refresh tokens for a poller
that only runs periodically anyway.

Read-only: this module only ever reads zone status - never calls
evohomeasync2's set_mode/set_temperature/etc, so it carries no risk of
changing the thermostat's settings.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from evohomeasync2 import EvohomeClient
from evohomeasync2.auth import AbstractTokenManager

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0


class _NoCacheTokenManager(AbstractTokenManager):
    """Minimal token manager: no persistence - see module docstring."""

    async def save_access_token(self) -> None:
        """No-op: this process re-authenticates from scratch every run."""


def fetch_resideo_status(config: dict[str, Any]) -> dict[str, Any] | None:
    """Read-only Resideo thermostat snapshot: current temp, target, and mode.

    Args:
        config: Full static config - reads its "resideo" section (username,
            password expected merged in from secrets.yaml).

    Returns:
        Dict with "device_name", "mode", "current_temperature_c",
        "target_temperature_c", or None if disabled, misconfigured, or
        anything failed (fail-fast, matches this codebase's other cloud
        clients) - a broad except here, not just the login/HTTP calls' own
        errors, since a caller collecting several subsystems in one pass
        (see src/dashboard/status_collector.py) must not have one
        integration's unexpected exception blank the whole snapshot.

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

    username = resideo_config.get("username")
    password = resideo_config.get("password")
    if not username or not password:
        logger.error("resideo.username/password are not set - see config.yaml's resideo comments")
        return None

    timeout_seconds = resideo_config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    return asyncio.run(_fetch_status_async(username, password, timeout_seconds))


async def _fetch_status_async(username: str, password: str, timeout_seconds: float) -> dict[str, Any] | None:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        token_manager = _NoCacheTokenManager(username, password, session, logger=logger)
        client = EvohomeClient(token_manager, websession=session)
        await client.update()

        zone = _first_zone(client)
        if zone is None:
            logger.warning("Resideo account has no locations/gateways/systems/zones")
            return None

        return {
            "device_name": zone.name,
            "mode": zone.mode.value if hasattr(zone.mode, "value") else zone.mode,
            "current_temperature_c": zone.temperature,
            "target_temperature_c": zone.target_heat_temperature,
        }


def _first_zone(client: EvohomeClient) -> Any | None:
    """Navigate location -> gateway -> control system -> zone to the first zone found."""
    for location in client.locations:
        for gateway in location.gateways:
            for system in gateway.systems:
                if system.zones:
                    return system.zones[0]
    return None
