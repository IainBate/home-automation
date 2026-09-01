"""Fake Ohme HTTP server for aioresponses - mocks the real endpoints the
installed ohme 1.9.1 library (and ohme_ev_client.py's own raw-aiohttp
helpers) call, so the real client code runs against scripted fake responses,
never a real Ohme account. Endpoint shapes taken directly from the installed
package source (site-packages/ohme/{ohme,utils}.py), not guessed.

Endpoint map:
- POST googleapis identitytoolkit verifyPassword  -> login
- GET  api.ohme.io/v1/users/me/account            -> device info (async_update_device_info)
- GET  api.ohme.io/v1/chargeSessions               -> charge session (async_get_charge_session)
- GET  api.ohme.io/v1/chargeSessions/nextSessionInfo
- GET  api.ohme.io/v1/chargeDevices/{serial}/status -> _fetch_device_status (direct call, no AppCheck needed)
- GET  api.ohme.io/v2/users/me/settings/max-price   -> _fetch_price_cap_settings (direct call)
- PUT  api-beta.ohme.io/v2/users/me/settings/max-price -> _set_price_cap_v2 (pause_charge's workaround)
- POST api.ohme.io/v1/chargeSessions/{serial}/stop  -> async_pause_charge (library, unused by
  ohme_ev_client.pause_charge directly, but exercised via ohme_ev_control.py's diagnostic
  handlers - not in scope here)
- PUT  api.ohme.io/v2/charge-devices/{serial}/charge-sessions/active/{serial}/max-charge?enabled=true|false
  -> async_max_charge (set_max_charge)
- GET/PUT api.ohme.io/v1/users/me/settings          -> async_change_price_cap (set_price_cap)
- PATCH api.ohme.io/v2/users/me/charge-rules/{id}    -> async_set_target (set_target)
- PUT  api.ohme.io/v1/car/{id}/select                -> async_set_vehicle (select_vehicle)

No appcheck_token is configured in these tests, so _fetch_advanced_settings()
returns {} without making any HTTP call at all (ohme_ev_client.py's own
graceful-degradation path) - nothing to mock there.
"""

from __future__ import annotations

from typing import Any

from aioresponses import CallbackResult
from ohme.const import GOOGLE_API_KEY

BASE_URL = "https://api.ohme.io"
BETA_BASE_URL = "https://api-beta.ohme.io"
GOOGLE_LOGIN_URL = (
    f"https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyPassword?key={GOOGLE_API_KEY}"
)
SERIAL = "CHARGER123"


def make_account_response(
    *, serial: str = SERIAL, cars: list[dict] | None = None, price_cap_enabled: bool = False
) -> dict:
    return {
        "cars": cars or [],
        "userSettings": {"chargeSettings": [{"enabled": price_cap_enabled}]},
        "chargeDevices": [
            {
                "id": serial,
                "modelCapabilities": {"solarModes": ["ZERO_EXPORT"]},
                "optionalSettings": {},
                "modelTypeDisplayName": "Ohme Home Pro",
                "firmwareVersionLabel": "1.2.3",
            }
        ],
        "tariff": None,
    }


class FakeOhmeServer:
    """Registers itself against an aioresponses() mock and serves stateful responses.

    session_mode drives both the chargeSessions GET response and is updated
    by the max-charge PUT endpoints, so set_max_charge()'s subsequent
    verification poll sees the change - same stateful-callback pattern as
    melcloud_fake_server.py.
    """

    def __init__(
        self,
        *,
        serial: str = SERIAL,
        session_mode: str = "SMART_CHARGE",
        power_watts: float = 0.0,
        online: bool = True,
        device_online: bool = True,
        plugged_in: bool = True,
        battery_percent: int = 50,
        cars: list[dict] | None = None,
        rule_id: str = "rule123",
    ) -> None:
        self.serial = serial
        self.session_mode = session_mode
        self.power_watts = power_watts
        self.online = online
        self.device_online = device_online
        self.plugged_in = plugged_in
        self.battery_percent = battery_percent
        self.cars = cars or []
        self.rule_id = rule_id
        self.price_cap_v1 = {"enabled": False, "value": None}
        self.price_cap_v2 = {"enabled": False, "value": None}
        self.max_charge_calls: list[bool] = []
        self.pause_calls = 0

    def register(self, mocked: Any) -> None:
        mocked.post(
            GOOGLE_LOGIN_URL,
            payload={"idToken": "fake-id-token", "refreshToken": "fake-refresh-token"},
            repeat=True,
        )
        mocked.get(
            f"{BASE_URL}/v1/users/me/account",
            payload=make_account_response(serial=self.serial, cars=self.cars),
            repeat=True,
        )
        mocked.get(f"{BASE_URL}/v1/chargeSessions", callback=self._handle_charge_sessions, repeat=True)
        mocked.get(
            f"{BASE_URL}/v1/chargeSessions/nextSessionInfo",
            payload={"rule": {}},
            repeat=True,
        )
        mocked.get(
            f"{BASE_URL}/v1/chargeDevices/{self.serial}/status",
            callback=self._handle_device_status,
            repeat=True,
        )
        mocked.get(
            f"{BASE_URL}/v2/users/me/settings/max-price",
            callback=self._handle_get_price_cap_v2,
            repeat=True,
        )
        mocked.put(
            f"{BETA_BASE_URL}/v2/users/me/settings/max-price",
            callback=self._handle_set_price_cap_v2,
            repeat=True,
        )
        mocked.post(
            f"{BASE_URL}/v1/chargeSessions/{self.serial}/stop",
            callback=self._handle_pause,
            repeat=True,
        )
        mocked.put(
            f"{BASE_URL}/v2/charge-devices/{self.serial}/charge-sessions/active/{self.serial}/max-charge?enabled=true",
            callback=self._handle_max_charge_true,
            repeat=True,
        )
        mocked.put(
            f"{BASE_URL}/v2/charge-devices/{self.serial}/charge-sessions/active/{self.serial}/max-charge?enabled=false",
            callback=self._handle_max_charge_false,
            repeat=True,
        )
        mocked.get(
            f"{BASE_URL}/v1/users/me/settings",
            callback=self._handle_get_price_cap_v1,
            repeat=True,
        )
        mocked.put(f"{BASE_URL}/v1/users/me/settings", status=200, repeat=True)
        mocked.patch(
            f"{BASE_URL}/v2/users/me/charge-rules/{self.rule_id}?persist=true&recalculateSession=true",
            payload={},
            repeat=True,
        )

    def _charge_session_payload(self) -> dict:
        return {
            "mode": self.session_mode,
            "chargerStatus": {"online": self.online},
            "power": {
                "watt": self.power_watts,
                "amp": self.power_watts / 230 if self.power_watts else 0,
                "volt": 230,
            },
            "batterySoc": {"wh": 1000, "percent": self.battery_percent},
            "appliedRule": {"id": self.rule_id},
        }

    def _handle_charge_sessions(self, _url: Any, **_kwargs: Any) -> CallbackResult:
        return CallbackResult(payload=[self._charge_session_payload()])

    def _handle_device_status(self, _url: Any, **_kwargs: Any) -> CallbackResult:
        return CallbackResult(
            payload={
                "online": self.device_online,
                "pluggedIn": self.plugged_in,
                "lastConnectDisconnect": 1234567890,
            }
        )

    def _handle_get_price_cap_v2(self, _url: Any, **_kwargs: Any) -> CallbackResult:
        return CallbackResult(payload=dict(self.price_cap_v2))

    def _handle_set_price_cap_v2(self, _url: Any, **kwargs: Any) -> CallbackResult:
        posted = kwargs.get("json") or {}
        self.price_cap_v2["enabled"] = posted.get("enabled", self.price_cap_v2["enabled"])
        self.price_cap_v2["value"] = posted.get("value", self.price_cap_v2["value"])
        return CallbackResult(status=200)

    def _handle_get_price_cap_v1(self, _url: Any, **_kwargs: Any) -> CallbackResult:
        return CallbackResult(payload={"chargeSettings": [dict(self.price_cap_v1)]})

    def _handle_pause(self, _url: Any, **_kwargs: Any) -> CallbackResult:
        self.pause_calls += 1
        self.session_mode = "STOPPED"
        return CallbackResult(status=200, body="OK", content_type="text/plain")

    def _handle_max_charge_true(self, _url: Any, **_kwargs: Any) -> CallbackResult:
        self.max_charge_calls.append(True)
        self.session_mode = "MAX_CHARGE"
        return CallbackResult(status=200)

    def _handle_max_charge_false(self, _url: Any, **_kwargs: Any) -> CallbackResult:
        self.max_charge_calls.append(False)
        self.session_mode = "SMART_CHARGE"
        return CallbackResult(status=200)
