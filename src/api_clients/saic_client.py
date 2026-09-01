"""MG SAIC (MG iSmart) EV client - read-only battery/range status.

Uses the `saic-ismart-client-ng` PyPI package (NOT the
"saic-python-client-ng-master/" directory CLAUDE.md describes - that
doesn't actually exist in this checkout; this is the real, currently
maintained package). Only ever reads vehicle/charging status - never sends
a control command (lock, climate, charge start/stop, etc.), so it carries
no risk of accidentally operating the car.

IMPORTANT - shared account session: MG's backend can only have so many
concurrent logged-in sessions per account; logging in here with the SAME
account as the household's phone(s) risks momentarily kicking one of those
sessions (it self-recovers - the phone app just re-logs-in). This project
deliberately polls infrequently (see scripts/mg_saic_poller.py's cron
cadence) to minimize that. If this becomes a real nuisance in practice, the
fix is a second, dedicated MG account for API access (MG supports
registering the same vehicle to multiple accounts) - just change
mg_saic.username/password in secrets.yaml, no code change needed.

IMPORTANT - no cross-run session caching: the underlying library's login
token/expiry are private to its SaicApi instance with no supported way to
inject a previously-obtained token into a new one, so every poll (a fresh
process) does a fresh login - there is no way to avoid this without relying
on the library's private internals, which this deliberately does not do.

SoC/range scaling verified against the actively-maintained reference
implementation (saic-python-mqtt-gateway's src/extractors/__init__.py),
not guessed: both bmsPackSOCDsp (-> percent) and fuelRangeElec (-> km) are
raw values scaled by dividing by 10.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from saic_ismart_client_ng import SaicApi
from saic_ismart_client_ng.model import SaicApiConfiguration

logger = logging.getLogger(__name__)

DEFAULT_REGION = "eu"
DEFAULT_TIMEOUT_SECONDS = 30.0

_REGION_BASE_URIS = {
    "eu": "https://gateway-mg-eu.soimt.com/api.app/v1/",
    "au": "https://gateway-mg-au.soimt.com/api.app/v1/",
    "tr": "https://gateway-mg-tr.soimt.com/api.app/v1/",
}

_MIN_VALID_SOC_PERCENT = 0
_MAX_VALID_SOC_PERCENT = 100
_MIN_VALID_RANGE_KM = 0
_MAX_VALID_RANGE_KM = 1000  # Generous upper bound - no production EV has more


def fetch_saic_status(config: dict[str, Any]) -> dict[str, Any] | None:
    """Read-only MG SAIC vehicle snapshot: battery SoC, range, charging status.

    Args:
        config: Full static config - reads its "mg_saic" section (username,
            password expected merged in from secrets.yaml).

    Returns:
        Dict with "battery_percent", "range_km", "is_charging", "is_parked",
        "vehicle_name", or None if disabled, misconfigured, or anything
        failed (fail-fast, matches this codebase's other cloud clients) - a
        broad except here, not just the login/HTTP calls' own errors, since
        a caller collecting several subsystems in one pass (see
        src/dashboard/status_collector.py) must not have one integration's
        unexpected exception blank the whole snapshot.

    """
    try:
        return _fetch_saic_status_unsafe(config)
    except Exception:
        # Circuit Breaker: see docstring above.
        logger.exception("Unexpected error reading MG SAIC status")
        return None


def _fetch_saic_status_unsafe(config: dict[str, Any]) -> dict[str, Any] | None:
    saic_config = config.get("mg_saic", {})
    if not saic_config.get("enabled", False):
        return None

    username = saic_config.get("username")
    password = saic_config.get("password")
    if not username or not password:
        logger.error("mg_saic.username/password are not set - see config.yaml's mg_saic comments")
        return None

    region = saic_config.get("region", DEFAULT_REGION)
    base_uri = _REGION_BASE_URIS.get(region)
    if base_uri is None:
        logger.error("mg_saic.region %r is not one of %s", region, sorted(_REGION_BASE_URIS))
        return None

    configuration = SaicApiConfiguration(
        username=username,
        password=password,
        username_is_email=saic_config.get("username_is_email", True),
        base_uri=base_uri,
        read_timeout=saic_config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
    )

    return asyncio.run(_fetch_status_async(configuration, saic_config.get("vin")))


async def _fetch_status_async(
    configuration: SaicApiConfiguration, configured_vin: str | None
) -> dict[str, Any] | None:
    api = SaicApi(configuration)
    await api.login()

    vin = configured_vin
    vehicle_name = None
    if not vin:
        vehicle_list = await api.vehicle_list()
        current = next((v for v in vehicle_list.vinList if v.isCurrentVehicle), None) or next(
            iter(vehicle_list.vinList), None
        )
        if current is None:
            logger.warning("MG SAIC account has no vehicles")
            return None
        vin = current.vin
        vehicle_name = current.name or current.modelName

    vehicle_status = await api.get_vehicle_status(vin)
    charging_data = await api.get_vehicle_charging_management_data(vin)

    basic_status = vehicle_status.basicVehicleStatus
    chrg_mgmt_data = charging_data.chrgMgmtData

    return {
        "vehicle_name": vehicle_name,
        "battery_percent": _scaled_or_none(
            chrg_mgmt_data.bmsPackSOCDsp if chrg_mgmt_data else None,
            _MIN_VALID_SOC_PERCENT,
            _MAX_VALID_SOC_PERCENT,
        ),
        "range_km": _scaled_or_none(
            basic_status.fuelRangeElec if basic_status else None,
            _MIN_VALID_RANGE_KM,
            _MAX_VALID_RANGE_KM,
        ),
        "is_charging": chrg_mgmt_data.is_bms_charging if chrg_mgmt_data else None,
        "is_parked": basic_status.is_parked if basic_status else None,
    }


def _scaled_or_none(raw_value: int | None, min_valid: float, max_valid: float) -> float | None:
    """Apply the reference implementation's raw/10.0 scaling, discarding out-of-range noise."""
    if raw_value is None:
        return None
    scaled = raw_value / 10.0
    if not (min_valid <= scaled <= max_valid):
        logger.warning("MG SAIC value %s out of expected range [%s, %s] - discarding", scaled, min_valid, max_valid)
        return None
    return scaled
