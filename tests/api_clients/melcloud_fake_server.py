"""Fake MELCloud HTTP server for aioresponses - mocks the real endpoints
pymelcloud calls (Login/ClientLogin, User/ListDevices, User/GetUserDetails,
Device/Get, Device/SetAtw) so melcloud_client.py's real connect()/
get_tank_status()/set_force_hot_water()/set_target_tank_temperature() code,
and the real (installed, v2.11.0) pymelcloud library code underneath it,
run against scripted fake responses - never a real MELCloud account.

Endpoint shapes and the exact state dict keys (TankWaterTemperature,
SetTankWaterTemperature, ForcedHotWaterMode, OperationMode, EffectiveFlags,
LastCommunication, ...) were taken directly from the installed pymelcloud
2.11.0 source (site-packages/pymelcloud/{client,device,atw_device}.py), not
guessed - the version installed here matches requirements.txt's floor.
"""

from __future__ import annotations

from typing import Any

from aioresponses import CallbackResult

BASE_URL = "https://app.melcloud.com/Mitsubishi.Wifi.Client"
DEVICE_ID = 12345
BUILDING_ID = 999
ACCESS_LEVEL_OWNER = 4  # not GUEST(3), so fetch_device_units() would be skipped anyway
DEVICE_GET_URL = f"{BASE_URL}/Device/Get?id={DEVICE_ID}&buildingID={BUILDING_ID}"


def make_device_conf(*, max_tank_temperature: float = 75.0, device_name: str = "Hot Water Tank") -> dict:
    return {
        "DeviceID": DEVICE_ID,
        "BuildingID": BUILDING_ID,
        "MacAddress": "AA:BB:CC:DD:EE:FF",
        "SerialNumber": "SN123",
        "AccessLevel": ACCESS_LEVEL_OWNER,
        "DeviceName": device_name,
        "Device": {
            "DeviceType": 1,  # ATW
            "MaxTankTemperature": max_tank_temperature,
            "TemperatureIncrement": 0.5,
        },
    }


def make_device_state(
    *,
    tank_temperature: float = 30.0,
    target_tank_temperature: float = 45.0,
    forced_hot_water: bool = False,
    operation_mode_state: int = 0,  # 0=idle per atw_device.py's _STATE_LOOKUP
    last_communication: str = "2026-01-01T12:00:00.000000",
    power: bool = True,
) -> dict:
    return {
        "DeviceID": DEVICE_ID,
        "BuildingID": BUILDING_ID,
        "DeviceType": 1,
        "TankWaterTemperature": tank_temperature,
        "SetTankWaterTemperature": target_tank_temperature,
        "ForcedHotWaterMode": forced_hot_water,
        "OperationMode": operation_mode_state,
        "HasError": False,
        "LastCommunication": last_communication,
        "Power": power,
        "EffectiveFlags": 0,
    }


class FakeMelCloudServer:
    """Registers itself against an aioresponses() mock and serves stateful responses.

    Device/Get always reflects the current `state`; Device/SetAtw applies the
    posted body onto `state` and returns the updated state - mirroring how
    pymelcloud's own Device._write() treats the SetAtw response as the new
    authoritative state.
    """

    def __init__(
        self,
        *,
        tank_temperature: float = 30.0,
        target_tank_temperature: float = 45.0,
        max_tank_temperature: float = 75.0,
        forced_hot_water: bool = False,
        token: str = "fake-melcloud-token",
    ) -> None:
        self.device_conf = make_device_conf(max_tank_temperature=max_tank_temperature)
        self.state = make_device_state(
            tank_temperature=tank_temperature,
            target_tank_temperature=target_tank_temperature,
            forced_hot_water=forced_hot_water,
        )
        self.token = token
        self.set_calls: list[dict[str, Any]] = []

    def register(self, mocked: Any) -> None:
        mocked.post(
            f"{BASE_URL}/Login/ClientLogin",
            payload={"LoginData": {"ContextKey": self.token}},
            repeat=True,
        )
        mocked.get(
            f"{BASE_URL}/User/ListDevices",
            payload=[{"Structure": {"Devices": [self.device_conf], "Areas": [], "Floors": []}}],
            repeat=True,
        )
        mocked.get(
            f"{BASE_URL}/User/GetUserDetails",
            payload={"UseFahrenheit": False},
            repeat=True,
        )
        mocked.get(DEVICE_GET_URL, callback=self._handle_get_state, repeat=True)
        # Fetched once by Device.update() for any non-GUEST account.
        mocked.post(f"{BASE_URL}/Device/ListDeviceUnits", payload=[], repeat=True)
        mocked.post(f"{BASE_URL}/Device/SetAtw", callback=self._handle_set_state, repeat=True)

    def _handle_get_state(self, _url: Any, **_kwargs: Any) -> CallbackResult:
        return CallbackResult(payload=dict(self.state))

    def _handle_set_state(self, _url: Any, **kwargs: Any) -> CallbackResult:
        posted = kwargs.get("json") or {}
        self.set_calls.append(posted)
        self.state.update(posted)
        return CallbackResult(payload=dict(self.state))
