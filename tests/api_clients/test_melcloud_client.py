"""End-to-end tests of MelCloudClient against a fake MELCloud HTTP server
(aioresponses) - the real melcloud_client.py code AND the real installed
pymelcloud 2.11.0 library code run against scripted fake responses, never a
real account. See melcloud_fake_server.py for the endpoint/state shapes.
"""

from __future__ import annotations

import yaml
from aioresponses import aioresponses

from melcloud_fake_server import BASE_URL, FakeMelCloudServer, make_device_conf
from src.api_clients.melcloud_client import (
    HotWaterOperationMode,
    HotWaterStatus,
    MelCloudClient,
    MelCloudConnectionError,
    MelCloudDeviceNotFoundError,
)

PROJECT_ROOT_CONFIG = __import__("pathlib").Path(__file__).resolve().parent.parent.parent / "config.yaml"


def _write_config(tmp_path, *, max_attempts=2, check_delay_seconds=1, device_name=None) -> str:
    # check_delay_seconds' schema minimum is 1 (config.yaml enforces this for
    # real usage), so these tests can't go faster than ~1s per retry attempt.
    with PROJECT_ROOT_CONFIG.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)
    config["melcloud"] = {
        "enabled": True,
        "email": "test@example.com",
        "password": "dummy",
        "mode_change_retry": {"max_attempts": max_attempts, "check_delay_seconds": check_delay_seconds},
    }
    if device_name:
        config["melcloud"]["device_name"] = device_name
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return str(path)


async def test_connect_selects_the_only_atw_device(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeMelCloudServer()

    with aioresponses() as mocked:
        server.register(mocked)
        client = MelCloudClient(config_path=config_path)
        connected = await client.connect()
        assert connected is True
        assert client.device.name == "Hot Water Tank"
        await client.close()


async def test_connect_selects_named_device_among_multiple(tmp_path):
    config_path = _write_config(tmp_path, device_name="Tank B")
    server = FakeMelCloudServer()
    second_device = make_device_conf(device_name="Tank B")
    second_device["DeviceID"] = 99999

    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/Login/ClientLogin", payload={"LoginData": {"ContextKey": "t"}}, repeat=True)
        mocked.get(
            f"{BASE_URL}/User/ListDevices",
            payload=[
                {"Structure": {"Devices": [server.device_conf, second_device], "Areas": [], "Floors": []}}
            ],
            repeat=True,
        )
        mocked.get(f"{BASE_URL}/User/GetUserDetails", payload={"UseFahrenheit": False}, repeat=True)
        mocked.get(
            f"{BASE_URL}/Device/Get?id=99999&buildingID=999",
            callback=server._handle_get_state,
            repeat=True,
        )
        mocked.post(f"{BASE_URL}/Device/ListDeviceUnits", payload=[], repeat=True)

        client = MelCloudClient(config_path=config_path)
        connected = await client.connect()
        assert connected is True
        assert client.device.name == "Tank B"
        await client.close()


async def test_connect_raises_device_not_found_when_no_atw_devices(tmp_path):
    config_path = _write_config(tmp_path)

    with aioresponses() as mocked:
        mocked.post(f"{BASE_URL}/Login/ClientLogin", payload={"LoginData": {"ContextKey": "t"}}, repeat=True)
        mocked.get(
            f"{BASE_URL}/User/ListDevices",
            payload=[{"Structure": {"Devices": [], "Areas": [], "Floors": []}}],
            repeat=True,
        )
        mocked.get(f"{BASE_URL}/User/GetUserDetails", payload={"UseFahrenheit": False}, repeat=True)

        client = MelCloudClient(config_path=config_path)
        try:
            await client.connect()
            raised = False
        except MelCloudDeviceNotFoundError:
            raised = True
        assert raised is True


async def test_get_tank_status_reports_real_fields(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeMelCloudServer(tank_temperature=32.5, target_tank_temperature=48.0)

    with aioresponses() as mocked:
        server.register(mocked)
        client = MelCloudClient(config_path=config_path)
        await client.connect()

        status = await client.get_tank_status(use_cache=False)

        assert status["tank_temperature"] == 32.5
        assert status["target_tank_temperature"] == 48.0
        assert status["operation_mode"] == HotWaterOperationMode.AUTO
        assert status["status"] == HotWaterStatus.IDLE
        await client.close()


async def test_get_tank_status_uses_cache_within_window(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeMelCloudServer()
    get_calls = []
    original_handler = server._handle_get_state

    def counting_handler(url, **kwargs):
        get_calls.append(1)
        return original_handler(url, **kwargs)

    server._handle_get_state = counting_handler

    with aioresponses() as mocked:
        server.register(mocked)
        client = MelCloudClient(config_path=config_path)
        await client.connect()  # this alone does one use_cache=False fetch
        calls_after_connect = len(get_calls)

        await client.get_tank_status(use_cache=True)
        await client.get_tank_status(use_cache=True)

        # No additional Device/Get calls - served from cache both times.
        assert len(get_calls) == calls_after_connect
        await client.close()


async def test_set_force_hot_water_succeeds_on_first_attempt(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeMelCloudServer()

    with aioresponses() as mocked:
        server.register(mocked)
        client = MelCloudClient(config_path=config_path)
        await client.connect()

        success = await client.set_force_hot_water(enabled=True)

        assert success is True
        assert len(server.set_calls) == 1
        assert server.state["ForcedHotWaterMode"] is True
        await client.close()


async def test_set_force_hot_water_verify_false_returns_immediately_without_checking(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeMelCloudServer()

    with aioresponses() as mocked:
        server.register(mocked)
        client = MelCloudClient(config_path=config_path)
        await client.connect()

        success = await client.set_force_hot_water(enabled=True, verify=False)

        assert success is True
        assert len(server.set_calls) == 1
        await client.close()


async def test_set_force_hot_water_retries_until_the_server_applies_it(tmp_path):
    """First SetAtw is accepted but the fake unit doesn't reflect it - the
    verify loop's next request-and-check attempt is what actually lands.
    """
    config_path = _write_config(tmp_path, max_attempts=3, check_delay_seconds=1)
    server = FakeMelCloudServer()

    apply_call_count = {"count": 0}
    real_handler = server._handle_set_state

    def flaky_handler(url, **kwargs):
        apply_call_count["count"] += 1
        if apply_call_count["count"] == 1:
            # Accepted, but pretend the physical unit hasn't applied it yet -
            # don't update state, just record the call.
            server.set_calls.append(kwargs.get("json") or {})
            from aioresponses import CallbackResult

            return CallbackResult(payload=dict(server.state))
        return real_handler(url, **kwargs)

    server._handle_set_state = flaky_handler

    with aioresponses() as mocked:
        server.register(mocked)
        client = MelCloudClient(config_path=config_path)
        await client.connect()

        success = await client.set_force_hot_water(enabled=True)

        assert success is True
        assert len(server.set_calls) == 2  # first didn't take, second did
        await client.close()


async def test_set_force_hot_water_exhausts_attempts_and_fails(tmp_path):
    config_path = _write_config(tmp_path, max_attempts=2, check_delay_seconds=1)
    server = FakeMelCloudServer()

    def never_applies(url, **kwargs):
        server.set_calls.append(kwargs.get("json") or {})
        from aioresponses import CallbackResult

        return CallbackResult(payload=dict(server.state))  # unchanged, forever

    server._handle_set_state = never_applies

    with aioresponses() as mocked:
        server.register(mocked)
        client = MelCloudClient(config_path=config_path)
        await client.connect()

        success = await client.set_force_hot_water(enabled=True)

        assert success is False
        assert len(server.set_calls) == 2  # exactly max_attempts
        await client.close()


async def test_set_target_tank_temperature_invalidates_cache(tmp_path):
    config_path = _write_config(tmp_path)
    server = FakeMelCloudServer(target_tank_temperature=45.0)

    with aioresponses() as mocked:
        server.register(mocked)
        client = MelCloudClient(config_path=config_path)
        await client.connect()
        await client.get_tank_status(use_cache=True)  # prime/confirm cache at 45.0

        await client.set_target_tank_temperature(55.0)

        # Cache was invalidated by the temperature-set call, so this cached
        # read must go fetch fresh rather than returning the stale 45.0.
        status = await client.get_tank_status(use_cache=True)
        assert status["target_tank_temperature"] == 55.0
        await client.close()


async def test_set_force_hot_water_verify_false_invalidates_cache(tmp_path):
    """Regression test: verify=False used to skip _invalidate_cache() entirely
    (unlike set_target_tank_temperature, which always called it), leaving a
    get_tank_status(use_cache=True) call made shortly afterwards serving the
    stale pre-change status instead of fetching fresh.
    """
    config_path = _write_config(tmp_path)
    server = FakeMelCloudServer(forced_hot_water=False)

    with aioresponses() as mocked:
        server.register(mocked)
        client = MelCloudClient(config_path=config_path)
        await client.connect()
        await client.get_tank_status(use_cache=True)  # primes cache: operation_mode=AUTO

        await client.set_force_hot_water(enabled=True, verify=False)

        status = await client.get_tank_status(use_cache=True)
        assert status["operation_mode"] == HotWaterOperationMode.FORCE_HOT_WATER
        await client.close()


async def test_close_resets_device_so_the_not_connected_guard_still_works(tmp_path):
    """Regression test: close() used to leave self.device set, so the "not
    connected" guard every public method checks (`if self.device is None:
    raise MelCloudConnectionError`) couldn't detect a closed client - a call
    made after close() would instead fail deep inside aiohttp/pymelcloud with
    a confusing low-level error.
    """
    config_path = _write_config(tmp_path)
    server = FakeMelCloudServer()

    with aioresponses() as mocked:
        server.register(mocked)
        client = MelCloudClient(config_path=config_path)
        await client.connect()
        assert client.device is not None

        await client.close()

        assert client.device is None
        raised = False
        try:
            await client.get_tank_status(use_cache=False)
        except MelCloudConnectionError:
            raised = True
        assert raised is True
