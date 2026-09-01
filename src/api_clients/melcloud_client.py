"""MELCloud Hot Water Tank API Client.

Provides integration with a Mitsubishi Ecodan (Air-to-Water) hot water tank via the
MELCloud cloud API, using the `pymelcloud` library. This is the same account used
by the official MELCloud iPhone app.

Key Features:
- Tank temperature and heating activity reporting
- Forced hot water heating control (on / back to auto)
- Change verification: the MELCloud app frequently needs a mode-change request
  repeated before the physical unit picks it up. `set_force_hot_water` re-issues
  the request and re-checks the reported mode until it matches, or a configurable
  number of attempts is exhausted.

Usage:
    from src.api_clients.melcloud_client import MelCloudClient

    client = MelCloudClient()
    await client.connect()
    status = await client.get_tank_status()
    print(f"Tank: {status['tank_temperature']}C, mode={status['operation_mode'].value}")
    await client.set_force_hot_water(True)
    await client.close()
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from enum import Enum
from time import time
from typing import TYPE_CHECKING, Any

import pymelcloud
from aiohttp import ClientSession
from pymelcloud.atw_device import (
    OPERATION_MODE_AUTO,
    OPERATION_MODE_FORCE_HOT_WATER,
    PROPERTY_OPERATION_MODE,
    PROPERTY_TARGET_TANK_TEMPERATURE,
)
from pymelcloud.const import DEVICE_TYPE_ATW

from src.config_manager.config_manager import load_static_config

if TYPE_CHECKING:
    from pymelcloud.atw_device import AtwDevice

logger = logging.getLogger(__name__)

# Default mode-change verification settings, used when melcloud.mode_change_retry
# is absent from config.yaml.
#
# pymelcloud's fetch_device_state() (used by Device.update()) documents that it
# "should not be called more than once a minute" for continuous polling. This
# client doesn't poll continuously - verification does exactly one status check
# per attempt, so a full set_force_hot_water() call makes at most max_attempts
# extra calls total (a handful, not a continuous stream), comfortably inside
# that guidance regardless of check_delay.
#
# There's no urgency for a force-heat/legionella mode change to land quickly,
# so check_delay favours giving each attempt's cloud round-trip (app ->
# MELCloud -> physical unit -> status refresh) more room to actually complete
# before being judged a failure, rather than checking fast and risking a false
# negative that just burns a retry. 4 x 15s is a deliberate, easy-to-reason-
# about one minute total worst case. If MELCloud starts throttling this
# account in practice, raise check_delay_seconds (or lower max_attempts)
# first.
#
# scripts/hotwater_automation_core.py's DEFAULT_HOTWATER_LOCK_TIMEOUT_SECONDS
# holds its state-file lock across this whole retry loop, sized with margin
# above this worst case - keep the two in sync if either changes.
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_CHECK_DELAY_SECONDS = 15.0

# How long a fetched status stays fresh enough to reuse without another API call.
STATUS_CACHE_DURATION_SECONDS = 5.0


class HotWaterOperationMode(str, Enum):
    """Internal representation of the tank's hot water operation mode.

    Wraps pymelcloud's operation_mode strings with an added UNKNOWN value for
    graceful degradation, following the same pattern as OhmeChargerMode.
    """

    AUTO = "auto"
    FORCE_HOT_WATER = "force_hot_water"
    UNKNOWN = "unknown"

    @classmethod
    def from_pymelcloud(cls, value: str | None) -> HotWaterOperationMode:
        """Convert a pymelcloud operation_mode string to our internal enum."""
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


class HotWaterStatus(str, Enum):
    """Internal representation of what the tank/heat pump is currently doing."""

    IDLE = "idle"
    HEAT_WATER = "heat_water"
    HEAT_ZONES = "heat_zones"
    COOL = "cool"
    DEFROST = "defrost"
    STANDBY = "standby"
    LEGIONELLA = "legionella"
    UNKNOWN = "unknown"

    @classmethod
    def from_pymelcloud(cls, value: str | None) -> HotWaterStatus:
        """Convert a pymelcloud status string to our internal enum."""
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


class MelCloudConnectionError(Exception):
    """Raised when a MELCloud API call fails."""


class MelCloudAuthenticationError(MelCloudConnectionError):
    """Raised when MELCloud authentication fails."""


class MelCloudDeviceNotFoundError(MelCloudConnectionError):
    """Raised when the configured (or only) Air-to-Water device cannot be found."""


class MelCloudClient:
    """MELCloud Air-to-Water (hot water tank) API client.

    Wraps `pymelcloud` to read tank temperature/mode and to change the forced hot
    water heating mode with request-then-verify retry logic, mirroring the manual
    workaround needed in the official iPhone app.
    """

    def __init__(self, config_path: str = "config.yaml") -> None:
        """Initialize MELCloud client.

        Args:
            config_path: Path to configuration file containing MELCloud credentials

        """
        self.config_path = config_path
        self.config: dict[str, Any] | None = None
        self.melcloud_config: dict[str, Any] = {}
        self._session: ClientSession | None = None
        self._token: str | None = None
        self.device: AtwDevice | None = None
        self.session_established = False

        self._cached_status: dict[str, Any] | None = None
        self._cache_timestamp: float = 0.0

        self._load_config()

    def _load_config(self) -> None:
        """Load MELCloud configuration from config file."""
        config_load_failed_msg_template = (
            "Failed to load configuration from {path} - see logs above for details"
        )
        integration_disabled_msg = "MELCloud integration is disabled in configuration"
        missing_fields_msg_template = "Missing MELCloud configuration fields: {fields}"

        try:
            self.config = load_static_config(self.config_path)
        except Exception:
            logger.exception("Failed to load MELCloud configuration")
            raise

        if self.config is None:
            # load_static_config() returns None (rather than raising) on schema
            # validation failure - treat that as a real error, not "disabled",
            # since an unrelated bad config section would otherwise be reported
            # as "MELCloud integration is disabled" even when melcloud.enabled: true.
            raise ValueError(config_load_failed_msg_template.format(path=self.config_path))

        self.melcloud_config = self.config.get("melcloud", {})

        if not self.melcloud_config.get("enabled", False):
            raise ValueError(integration_disabled_msg)

        required_fields = ["email", "password"]
        missing_fields = [
            field for field in required_fields if not self.melcloud_config.get(field)
        ]
        if missing_fields:
            missing_fields_msg = missing_fields_msg_template.format(fields=missing_fields)
            raise ValueError(missing_fields_msg)

    async def connect(self) -> bool:
        """Authenticate with MELCloud and select the Air-to-Water (tank) device.

        Returns:
            True if connection successful

        Raises:
            MelCloudAuthenticationError: If login fails
            MelCloudDeviceNotFoundError: If no matching ATW device is found
            MelCloudConnectionError: If any other connection failure occurs

        """
        email = self.melcloud_config["email"]
        password = self.melcloud_config["password"]

        self._session = ClientSession()

        try:
            logger.info("Authenticating with MELCloud...")
            self._token = await pymelcloud.login(email, password, self._session)
        except Exception as e:
            msg = f"Failed to authenticate with MELCloud: {e}"
            raise MelCloudAuthenticationError(msg) from e

        try:
            logger.info("Retrieving MELCloud device list...")
            devices = await pymelcloud.get_devices(self._token, self._session)
        except Exception as e:
            msg = f"Failed to retrieve MELCloud devices: {e}"
            raise MelCloudConnectionError(msg) from e

        atw_devices: list[AtwDevice] = devices.get(DEVICE_TYPE_ATW, [])
        if not atw_devices:
            msg = "No Air-to-Water (hot water tank) device found on this MELCloud account"
            raise MelCloudDeviceNotFoundError(msg)

        device_name = self.melcloud_config.get("device_name")
        if device_name:
            matching = [d for d in atw_devices if d.name == device_name]
            if not matching:
                available = ", ".join(d.name for d in atw_devices)
                msg = f"Configured device_name '{device_name}' not found. Available: {available}"
                raise MelCloudDeviceNotFoundError(msg)
            self.device = matching[0]
        else:
            if len(atw_devices) > 1:
                logger.warning(
                    "Multiple Air-to-Water devices found (%s); using the first one. "
                    "Set melcloud.device_name in config.yaml to choose explicitly.",
                    ", ".join(d.name for d in atw_devices),
                )
            self.device = atw_devices[0]

        # A fresh state fetch is required before device.set() can be used (it
        # copies the existing state internally), and doubles as populating our
        # status cache so an immediate get_tank_status() call doesn't fetch again.
        await self.get_tank_status(use_cache=False)
        self.session_established = True
        logger.info("MELCloud connection established (device: %s)", self.device.name)
        return True

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self.session_established = False
        # Reset device too - it's the "not connected" guard every other public
        # method checks (`if self.device is None: raise MelCloudConnectionError`).
        # Without this, calling a method on a closed client skipped that guard
        # and fell through to a confusing low-level aiohttp/RuntimeError from
        # using the now-closed session, instead of the clean domain error.
        self.device = None

    async def get_tank_status(self, *, use_cache: bool = True) -> dict[str, Any]:
        """Get current hot water tank status.

        Args:
            use_cache: If True (default), return the last-fetched status without
                another API call if it's less than a few seconds old. Set False
                to force a fresh fetch from MELCloud (e.g. when verifying a mode
                change).

        Returns:
            Dictionary with timestamp, device_name, tank_temperature,
            target_tank_temperature, target_tank_temperature_max, operation_mode
            (HotWaterOperationMode), status (HotWaterStatus), power, holiday_mode
            and last_seen.

        Raises:
            MelCloudConnectionError: If not connected, or the fetch fails

        """
        if self.device is None:
            msg = "Not connected to MELCloud - call connect() first"
            raise MelCloudConnectionError(msg)

        if (
            use_cache
            and self._cached_status is not None
            and (time() - self._cache_timestamp) < STATUS_CACHE_DURATION_SECONDS
        ):
            return self._cached_status

        try:
            await self.device.update()
        except Exception as e:
            msg = f"Failed to refresh MELCloud tank status: {e}"
            raise MelCloudConnectionError(msg) from e

        last_seen = self.device.last_seen

        status = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "device_name": self.device.name,
            "tank_temperature": self.device.tank_temperature,
            "target_tank_temperature": self.device.target_tank_temperature,
            "target_tank_temperature_max": self.device.target_tank_temperature_max,
            "operation_mode": HotWaterOperationMode.from_pymelcloud(self.device.operation_mode),
            "status": HotWaterStatus.from_pymelcloud(self.device.status),
            "power": self.device.power,
            "holiday_mode": self.device.holiday_mode,
            "last_seen": last_seen.isoformat() if last_seen else None,
        }

        self._cached_status = status
        self._cache_timestamp = time()
        return status

    async def _check_mode(
        self, target_mode: HotWaterOperationMode, *, check_delay_seconds: float
    ) -> bool:
        """Wait, then check (once) whether operation_mode now matches target_mode.

        Args:
            target_mode: The mode we expect to see reported back.
            check_delay_seconds: Wait this long before checking, giving
                MELCloud/the unit a moment to apply the change.

        Returns:
            True if the reported mode matches target_mode. False if it doesn't,
            or if the status check itself fails - a failed check is treated the
            same as "not yet changed" so set_force_hot_water can retry rather
            than crash on a transient error.

        """
        await asyncio.sleep(check_delay_seconds)

        try:
            status = await self.get_tank_status(use_cache=False)
        # Best-effort verification must not abort the retry loop.
        except Exception:
            logger.exception("Failed to check MELCloud mode after change request")
            return False

        current_mode = status["operation_mode"]
        if current_mode == target_mode:
            return True

        logger.warning(
            "MELCloud mode not yet applied: expected %s, got %s",
            target_mode.value,
            current_mode.value,
        )
        return False

    async def set_force_hot_water(self, *, enabled: bool, verify: bool = True) -> bool:
        """Turn forced hot water heating on or off, retrying until MELCloud confirms it.

        The MELCloud iPhone app frequently needs a mode change requested more than
        once before the physical unit applies it. This mirrors that manual
        workaround: request the mode change, wait a few seconds, check whether
        MELCloud now reports the new mode, and if not, request it again - up to
        melcloud.mode_change_retry.max_attempts times.

        Args:
            enabled: True to force hot water heating on, False to return to auto.
            verify: If True (default), confirm the change took effect - retrying
                the request per melcloud.mode_change_retry in config.yaml
                (max_attempts, check_delay_seconds) - before returning. If False,
                send the request once and return immediately.

        Returns:
            True if the mode change was applied (and verified, if requested).

        Raises:
            MelCloudConnectionError: If not connected

        """
        if self.device is None:
            msg = "Not connected to MELCloud - call connect() first"
            raise MelCloudConnectionError(msg)

        target_mode = (
            HotWaterOperationMode.FORCE_HOT_WATER if enabled else HotWaterOperationMode.AUTO
        )
        target_value = OPERATION_MODE_FORCE_HOT_WATER if enabled else OPERATION_MODE_AUTO

        retry_config = self.melcloud_config.get("mode_change_retry", {})
        max_attempts = retry_config.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
        check_delay = retry_config.get("check_delay_seconds", DEFAULT_CHECK_DELAY_SECONDS)

        for attempt in range(1, max_attempts + 1):
            logger.info(
                "Requesting MELCloud hot water mode change to %s (attempt %s/%s)",
                target_mode.value,
                attempt,
                max_attempts,
            )
            try:
                await self.device.set({PROPERTY_OPERATION_MODE: target_value})
            except Exception:
                logger.exception(
                    "MELCloud mode change request failed (attempt %s/%s)", attempt, max_attempts
                )
                if not verify:
                    # verify=False promises a single request, not a retry loop.
                    return False
                # Space retries out the same as the success path's check_delay,
                # rather than hammering MELCloud back-to-back on failure.
                if attempt < max_attempts:
                    await asyncio.sleep(check_delay)
                continue

            if not verify:
                # verify=True's success path already gets a fresh cache for
                # free via _check_mode()'s own use_cache=False status fetch -
                # verify=False skips that entirely, so the pre-change cache
                # would otherwise still be served to a get_tank_status(
                # use_cache=True) call made shortly afterwards.
                self._invalidate_cache()
                return True

            if await self._check_mode(target_mode, check_delay_seconds=check_delay):
                logger.info("MELCloud hot water mode change verified: %s", target_mode.value)
                return True

        logger.error(
            "MELCloud hot water mode change to %s failed after %s attempts",
            target_mode.value,
            max_attempts,
        )
        return False

    async def set_target_tank_temperature(self, temperature_c: float) -> bool:
        """Set the tank's target temperature.

        Used for the legionella high-temperature cycle (raise the target, then
        restore it afterwards). Unlike set_force_hot_water, this doesn't retry -
        the reported pain point with the MELCloud app is specifically mode
        toggling, not target temperature, and getting this wrong (e.g. failing
        to restore the original target) is only safe to detect by checking
        get_tank_status() afterwards, which callers should do.

        Args:
            temperature_c: Target tank temperature in Celsius.

        Returns:
            True on success.

        Raises:
            MelCloudConnectionError: If not connected, or the request fails

        """
        if self.device is None:
            msg = "Not connected to MELCloud - call connect() first"
            raise MelCloudConnectionError(msg)

        try:
            await self.device.set({PROPERTY_TARGET_TANK_TEMPERATURE: temperature_c})
        except Exception as e:
            msg = f"Failed to set MELCloud target tank temperature: {e}"
            raise MelCloudConnectionError(msg) from e

        self._invalidate_cache()
        return True

    def _invalidate_cache(self) -> None:
        """Invalidate the cached status so the next get_tank_status() fetches fresh."""
        self._cached_status = None
        self._cache_timestamp = 0.0
