"""Tests for saic_client.py - scaling correctness, vehicle auto-selection, error handling.

Scaling factors (raw/10.0 for both SoC and range) are verified against the
actively-maintained reference implementation (saic-python-mqtt-gateway's
extractors), not guessed - see the module docstring. These tests lock in
that exact arithmetic so a future edit can't silently drift from it.
"""

from __future__ import annotations

from unittest import mock

from src.api_clients import saic_client


def test_fetch_returns_none_when_disabled():
    assert saic_client.fetch_saic_status({"mg_saic": {"enabled": False}}) is None


def test_fetch_returns_none_when_credentials_missing():
    assert saic_client.fetch_saic_status({"mg_saic": {"enabled": True}}) is None


def test_fetch_returns_none_for_unknown_region():
    config = {"mg_saic": {"enabled": True, "username": "u", "password": "p", "region": "us"}}
    assert saic_client.fetch_saic_status(config) is None


def _fake_vin_info(vin, *, is_current=True, name="Test Car"):
    info = mock.Mock()
    info.vin = vin
    info.isCurrentVehicle = is_current
    info.name = name
    info.modelName = "ZS EV"
    return info


def _make_fake_api(*, vin_list, vehicle_status, charging_data):
    fake_api = mock.AsyncMock()
    fake_api.login = mock.AsyncMock()
    fake_api.vehicle_list = mock.AsyncMock(return_value=mock.Mock(vinList=vin_list))
    fake_api.get_vehicle_status = mock.AsyncMock(return_value=vehicle_status)
    fake_api.get_vehicle_charging_management_data = mock.AsyncMock(return_value=charging_data)
    return fake_api


def _vehicle_status(fuel_range_elec, *, is_parked=True):
    basic = mock.Mock(fuelRangeElec=fuel_range_elec, is_parked=is_parked)
    return mock.Mock(basicVehicleStatus=basic)


def _charging_data(bms_pack_soc_dsp, *, is_charging=False):
    chrg = mock.Mock(bmsPackSOCDsp=bms_pack_soc_dsp, is_bms_charging=is_charging)
    return mock.Mock(chrgMgmtData=chrg)


def test_fetch_scales_soc_and_range_by_dividing_by_ten():
    config = {"mg_saic": {"enabled": True, "username": "u", "password": "p"}}
    fake_api = _make_fake_api(
        vin_list=[_fake_vin_info("VIN123")],
        vehicle_status=_vehicle_status(fuel_range_elec=2100),  # -> 210.0 km
        charging_data=_charging_data(bms_pack_soc_dsp=625, is_charging=True),  # -> 62.5%
    )

    with mock.patch.object(saic_client, "SaicApi", return_value=fake_api):
        result = saic_client.fetch_saic_status(config)

    assert result["battery_percent"] == 62.5
    assert result["range_km"] == 210.0
    assert result["is_charging"] is True


def test_fetch_auto_selects_the_current_vehicle_when_vin_not_configured():
    config = {"mg_saic": {"enabled": True, "username": "u", "password": "p"}}
    not_current = _fake_vin_info("VIN_OLD", is_current=False)
    current = _fake_vin_info("VIN_CURRENT", is_current=True, name="MG ZS")
    fake_api = _make_fake_api(
        vin_list=[not_current, current],
        vehicle_status=_vehicle_status(fuel_range_elec=1000),
        charging_data=_charging_data(bms_pack_soc_dsp=500),
    )

    with mock.patch.object(saic_client, "SaicApi", return_value=fake_api):
        result = saic_client.fetch_saic_status(config)

    fake_api.get_vehicle_status.assert_awaited_once_with("VIN_CURRENT")
    assert result["vehicle_name"] == "MG ZS"


def test_fetch_uses_configured_vin_without_calling_vehicle_list():
    config = {"mg_saic": {"enabled": True, "username": "u", "password": "p", "vin": "VIN_EXPLICIT"}}
    fake_api = _make_fake_api(
        vin_list=[],
        vehicle_status=_vehicle_status(fuel_range_elec=1000),
        charging_data=_charging_data(bms_pack_soc_dsp=500),
    )

    with mock.patch.object(saic_client, "SaicApi", return_value=fake_api):
        saic_client.fetch_saic_status(config)

    fake_api.vehicle_list.assert_not_called()
    fake_api.get_vehicle_status.assert_awaited_once_with("VIN_EXPLICIT")


def test_fetch_discards_out_of_range_soc_as_noise_rather_than_a_bad_number():
    config = {"mg_saic": {"enabled": True, "username": "u", "password": "p", "vin": "V"}}
    fake_api = _make_fake_api(
        vin_list=[],
        vehicle_status=_vehicle_status(fuel_range_elec=1000),
        charging_data=_charging_data(bms_pack_soc_dsp=5000),  # -> 500%, impossible
    )

    with mock.patch.object(saic_client, "SaicApi", return_value=fake_api):
        result = saic_client.fetch_saic_status(config)

    assert result["battery_percent"] is None


def test_fetch_returns_none_on_no_vehicles_found():
    config = {"mg_saic": {"enabled": True, "username": "u", "password": "p"}}
    fake_api = _make_fake_api(vin_list=[], vehicle_status=None, charging_data=None)

    with mock.patch.object(saic_client, "SaicApi", return_value=fake_api):
        result = saic_client.fetch_saic_status(config)

    assert result is None


def test_fetch_returns_none_instead_of_raising_on_unexpected_error():
    """Circuit Breaker: a caller collecting several subsystems in one pass (see
    status_collector.py) must not have one integration's unexpected exception
    blank the whole snapshot.
    """
    config = {"mg_saic": {"enabled": True, "username": "u", "password": "p"}}
    with mock.patch.object(saic_client, "SaicApi", side_effect=RuntimeError("boom")):
        result = saic_client.fetch_saic_status(config)

    assert result is None
