"""Each Ohme consumer must prefer the shared cache but still work without it.

The cache (scripts/ohme_status_daemon.py) is an optimisation, not a
dependency: if it's missing, stale, or that daemon was never deployed, every
consumer has to fall back to the direct Ohme call it made before the cache
existed. These tests pin both halves of that for all three consumers.
"""

from __future__ import annotations

from unittest import mock

import battery_mode_daemon as battery_daemon_module
import hotwater_automation_core as core
import pytest

from src.dashboard import status_collector


# --- Battery daemon --------------------------------------------------------


def _closing_asyncio_run(return_value=None, side_effect=None):
    """A stand-in for asyncio.run that closes the coroutine it's handed.

    Without this the un-awaited coroutine surfaces as a RuntimeWarning at
    garbage-collection time, in whichever unrelated test happens to trigger
    the GC - noise that makes real warnings easy to miss.
    """

    def _run(coro, *_args, **_kwargs):
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        if side_effect is not None:
            raise side_effect
        return return_value

    return _run


def _battery_daemon(tmp_path):
    daemon = battery_daemon_module.BatteryModeDaemon.__new__(
        battery_daemon_module.BatteryModeDaemon
    )
    daemon.logger = mock.Mock()
    daemon.system_config_path = str(tmp_path / "config.yaml")
    return daemon


def test_battery_daemon_uses_cached_ohme_status_without_connecting(tmp_path):
    daemon = _battery_daemon(tmp_path)
    cached = {"power_watts": 7200, "status": "charging"}

    with (
        mock.patch.object(battery_daemon_module, "read_fresh_status", return_value=cached),
        mock.patch.object(battery_daemon_module.asyncio, "run", side_effect=_closing_asyncio_run()) as asyncio_run,
    ):
        status = daemon._check_ohme_status()

    assert status == cached
    asyncio_run.assert_not_called()  # no login performed


def test_battery_daemon_falls_back_to_direct_read_when_cache_cold(tmp_path):
    daemon = _battery_daemon(tmp_path)
    live = {"power_watts": 3000}

    with (
        mock.patch.object(battery_daemon_module, "read_fresh_status", return_value=None),
        mock.patch.object(battery_daemon_module.asyncio, "run", side_effect=_closing_asyncio_run(return_value=live)) as asyncio_run,
    ):
        status = daemon._check_ohme_status()

    assert status == live
    asyncio_run.assert_called_once()


def test_battery_daemon_returns_none_when_both_cache_and_direct_read_fail(tmp_path):
    """None means "unknown", which _is_ohme_charging treats as not-confirmed -
    it must never be conjured out of a cache miss alone."""
    daemon = _battery_daemon(tmp_path)

    with (
        mock.patch.object(battery_daemon_module, "read_fresh_status", return_value=None),
        mock.patch.object(battery_daemon_module.asyncio, "run", side_effect=_closing_asyncio_run(side_effect=OSError("no network"))),
    ):
        assert daemon._check_ohme_status() is None


# --- Hot water automation --------------------------------------------------


@pytest.mark.asyncio
async def test_hotwater_uses_cached_ohme_power_without_connecting():
    config = {"ohme_ev": {"enabled": True}}

    with (
        mock.patch.object(core, "read_fresh_status", return_value={"power_watts": 6800}),
        mock.patch.object(core, "OhmeEVClient") as client_cls,
    ):
        power = await core._get_ohme_charging_power_watts(config)

    assert power == 6800
    client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_hotwater_falls_back_to_direct_read_when_cache_cold():
    config = {"ohme_ev": {"enabled": True}}
    client = mock.AsyncMock()
    client.get_charger_status.return_value = {"power_watts": 1234}

    with (
        mock.patch.object(core, "read_fresh_status", return_value=None),
        mock.patch.object(core, "OhmeEVClient", return_value=client),
    ):
        power = await core._get_ohme_charging_power_watts(config)

    assert power == 1234
    client.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_hotwater_ignores_cache_when_ohme_disabled():
    with mock.patch.object(core, "read_fresh_status") as read_cache:
        assert await core._get_ohme_charging_power_watts({"ohme_ev": {"enabled": False}}) is None
    read_cache.assert_not_called()


# --- Dashboard -------------------------------------------------------------


def test_dashboard_uses_cached_ohme_status_without_connecting():
    cached = {
        "plugged_in": True,
        "status": "charging",
        "mode": "smart_charge",
        "power_watts": 7200,
        "battery_percent": 55,
        "target_soc": 80,
        "current_vehicle": "MG ZS",
    }

    with (
        mock.patch.object(status_collector, "read_fresh_status", return_value=cached),
        mock.patch.object(status_collector.asyncio, "run", side_effect=_closing_asyncio_run()) as asyncio_run,
    ):
        result = status_collector._collect_ev_charging({"ohme_ev": {"enabled": True}}, "config.yaml")

    assert result["available"] is True
    assert result["power_watts"] == 7200
    assert result["status"] == "charging"
    asyncio_run.assert_not_called()


def test_dashboard_falls_back_to_direct_read_when_cache_cold():
    live = {"plugged_in": False, "power_watts": 0, "status": None, "mode": None}

    with (
        mock.patch.object(status_collector, "read_fresh_status", return_value=None),
        mock.patch.object(status_collector.asyncio, "run", side_effect=_closing_asyncio_run(return_value=live)) as asyncio_run,
    ):
        result = status_collector._collect_ev_charging({"ohme_ev": {"enabled": True}}, "config.yaml")

    assert result["available"] is True
    asyncio_run.assert_called_once()
