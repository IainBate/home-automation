"""End-to-end tests of OhmeEVClient against a fake Ohme HTTP server
(aioresponses) - the real ohme_ev_client.py code AND the real installed
ohme 1.9.1 library code run against scripted fake responses, never a real
account. This is also what finally exercises _apply_domain_fix's monkey-patch
(previously `# pragma: no cover` by its own admission - see ohme_ev_client.py's
docstring on it) for real. See ohme_fake_server.py for the endpoint shapes.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from aioresponses import aioresponses

from ohme_fake_server import BASE_URL, FakeOhmeServer
from src.api_clients.ohme_ev_client import (
    OhmeChargerMode,
    OhmeChargerStatus,
    OhmeEVClient,
    OhmeNotPluggedInError,
)

PROJECT_ROOT_CONFIG = Path(__file__).resolve().parent.parent.parent / "config.yaml"


def _write_config(tmp_path) -> str:
    with PROJECT_ROOT_CONFIG.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)
    config["ohme_ev"] = {
        "enabled": True,
        "username": "test@example.com",
        "password": "dummy",
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return str(path)


async def test_connect_authenticates_and_fetches_device_info(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeOhmeServer()

    with aioresponses() as mocked:
        server.register(mocked)
        client = OhmeEVClient(config_path=config_path)
        connected = await client.connect()

        assert connected is True
        assert client.client.serial == server.serial
        await client.close()


async def test_get_charger_status_reports_real_fields(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeOhmeServer(session_mode="SMART_CHARGE", power_watts=0, plugged_in=True)

    with aioresponses() as mocked:
        server.register(mocked)
        client = OhmeEVClient(config_path=config_path)
        await client.connect()

        status = await client.get_charger_status(use_cache=False)

        assert status["mode"] == OhmeChargerMode.SMART_CHARGE
        assert status["status"] == OhmeChargerStatus.PLUGGED_IN
        assert status["power_watts"] == 0
        assert status["battery_percent"] == 50
        assert status["plugged_in"] is True
        await client.close()


async def test_get_charger_status_reports_charging_when_power_flowing(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeOhmeServer(session_mode="MAX_CHARGE", power_watts=7300, plugged_in=True)

    with aioresponses() as mocked:
        server.register(mocked)
        client = OhmeEVClient(config_path=config_path)
        await client.connect()

        status = await client.get_charger_status(use_cache=False)

        assert status["status"] == OhmeChargerStatus.CHARGING
        assert status["mode"] == OhmeChargerMode.MAX_CHARGE
        assert status["power_watts"] == 7300
        await client.close()


async def test_get_charger_status_degrades_gracefully_without_appcheck_or_device_status(tmp_path):
    """No appcheck_token configured, and this test doesn't even mock the direct-call
    endpoints (device status / price cap) - both must be swallowed internally
    rather than raising, per _make_direct_api_call's graceful degradation contract.
    """
    config_path = _write_config(tmp_path)
    server = FakeOhmeServer()

    # Register only what connect() and the charge-session fetch need -
    # deliberately omit device-status and price-cap endpoints.
    from ohme.const import GOOGLE_API_KEY
    from ohme_fake_server import make_account_response

    with aioresponses() as mocked:
        mocked.post(
            f"https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyPassword?key={GOOGLE_API_KEY}",
            payload={"idToken": "t", "refreshToken": "r"},
            repeat=True,
        )
        mocked.get(
            f"{BASE_URL}/v1/users/me/account",
            payload=make_account_response(serial=server.serial),
            repeat=True,
        )
        mocked.get(f"{BASE_URL}/v1/chargeSessions", callback=server._handle_charge_sessions, repeat=True)
        mocked.get(
            f"{BASE_URL}/v1/chargeSessions/nextSessionInfo", payload={"rule": {}}, repeat=True
        )

        client = OhmeEVClient(config_path=config_path)
        await client.connect()

        status = await client.get_charger_status(use_cache=False)

        assert status["online"] is None or isinstance(status["online"], bool)
        assert status["price_cap_enabled"] is False
        assert status["price_cap_gbp_per_kwh"] is None
        await client.close()


async def test_get_charger_status_uses_cache_within_window(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeOhmeServer()
    call_count = {"n": 0}
    original = server._handle_charge_sessions

    def counting(url, **kwargs):
        call_count["n"] += 1
        return original(url, **kwargs)

    server._handle_charge_sessions = counting

    with aioresponses() as mocked:
        server.register(mocked)
        client = OhmeEVClient(config_path=config_path)
        await client.connect()
        await client.get_charger_status(use_cache=False)
        calls_after_first = call_count["n"]

        await client.get_charger_status(use_cache=True)
        await client.get_charger_status(use_cache=True)

        assert call_count["n"] == calls_after_first
        await client.close()


async def test_pause_charge_raises_when_not_plugged_in(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeOhmeServer(plugged_in=False)

    with aioresponses() as mocked:
        server.register(mocked)
        client = OhmeEVClient(config_path=config_path)
        await client.connect()

        raised = False
        try:
            await client.pause_charge(require_plugged=True)
        except OhmeNotPluggedInError:
            raised = True
        assert raised is True
        await client.close()


async def test_pause_charge_via_price_cap_v2_verified(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeOhmeServer(plugged_in=True, power_watts=0)

    with aioresponses() as mocked:
        server.register(mocked)
        client = OhmeEVClient(config_path=config_path)
        await client.connect()

        success = await client.pause_charge(require_plugged=True, verify=True)

        assert success is True
        assert server.price_cap_v2 == {"enabled": True, "value": -100}
        await client.close()


async def test_set_max_charge_enables_and_verifies(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeOhmeServer(session_mode="SMART_CHARGE", plugged_in=True)

    with aioresponses() as mocked:
        server.register(mocked)
        client = OhmeEVClient(config_path=config_path)
        await client.connect()

        success = await client.set_max_charge(enabled=True, require_plugged=True)

        assert success is True
        assert server.max_charge_calls == [True]
        assert server.session_mode == "MAX_CHARGE"
        await client.close()


async def test_set_target_updates_the_charge_rule(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeOhmeServer()

    with aioresponses() as mocked:
        server.register(mocked)
        client = OhmeEVClient(config_path=config_path)
        await client.connect()
        # async_set_target needs _last_rule populated (from appliedRule) to know
        # which charge-rule ID to PATCH - realistic usage checks status first.
        await client.get_charger_status(use_cache=False)

        success = await client.set_target(target_percent=80, target_time=(7, 30))

        assert success is True
        await client.close()


async def test_set_target_rejects_invalid_percent(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeOhmeServer()

    with aioresponses() as mocked:
        server.register(mocked)
        client = OhmeEVClient(config_path=config_path)
        await client.connect()

        raised = False
        try:
            await client.set_target(target_percent=150)
        except ValueError:
            raised = True
        assert raised is True
        await client.close()


async def test_set_price_cap_via_v1_settings_endpoint(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeOhmeServer()

    with aioresponses() as mocked:
        server.register(mocked)
        client = OhmeEVClient(config_path=config_path)
        await client.connect()

        success = await client.set_price_cap(enabled=True, cap=0.15)

        assert success is True
        await client.close()


async def test_get_vehicles_and_current_vehicle(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeOhmeServer(cars=[{"name": "My EV"}])

    with aioresponses() as mocked:
        server.register(mocked)
        client = OhmeEVClient(config_path=config_path)
        await client.connect()

        vehicles = await client.get_vehicles()

        assert vehicles == ["My EV"]
        await client.close()
