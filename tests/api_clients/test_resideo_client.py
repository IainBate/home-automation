"""Tests for resideo_client.py - config validation, aiohomekit wiring, characteristic parsing."""

from __future__ import annotations

from unittest import mock

from src.api_clients import resideo_client


def _thermostat_accessory(
    *, current_state=0, target_state=0, current_temp=20.0, target_temp=21.0, name="Lyric T6 Thermostat"
):
    """Build a fake aiohomekit list_accessories_and_characteristics() result for one Thermostat service."""
    from aiohomekit.model.characteristics import CharacteristicsTypes
    from aiohomekit.model.services import ServicesTypes

    return [
        {
            "aid": 1,
            "services": [
                {
                    "type": ServicesTypes.THERMOSTAT,
                    "characteristics": [
                        {"type": CharacteristicsTypes.HEATING_COOLING_CURRENT, "value": current_state},
                        {"type": CharacteristicsTypes.HEATING_COOLING_TARGET, "value": target_state},
                        {"type": CharacteristicsTypes.TEMPERATURE_CURRENT, "value": current_temp},
                        {"type": CharacteristicsTypes.TEMPERATURE_TARGET, "value": target_temp},
                        {"type": CharacteristicsTypes.NAME, "value": name},
                    ],
                }
            ],
        }
    ]


def _patch_pairing(accessories, *, alias="heating-automation"):
    """Patch Controller/AsyncZeroconf/AsyncServiceBrowser so fetch_resideo_status()'s async
    plumbing runs for real (via asyncio.run) but talks to a fake pairing instead of the network.
    """
    fake_pairing = mock.Mock()
    fake_pairing.list_accessories_and_characteristics = mock.AsyncMock(return_value=accessories)

    fake_controller = mock.MagicMock()
    fake_controller.__aenter__ = mock.AsyncMock(return_value=fake_controller)
    fake_controller.__aexit__ = mock.AsyncMock(return_value=False)
    fake_controller.load_data = mock.Mock()
    fake_controller.aliases = {alias: fake_pairing}

    fake_zeroconf = mock.MagicMock()
    fake_zeroconf.__aenter__ = mock.AsyncMock(return_value=fake_zeroconf)
    fake_zeroconf.__aexit__ = mock.AsyncMock(return_value=False)

    fake_browser = mock.Mock()
    fake_browser.async_cancel = mock.AsyncMock()

    return mock.patch.multiple(
        resideo_client,
        Controller=mock.Mock(return_value=fake_controller),
        AsyncZeroconf=mock.Mock(return_value=fake_zeroconf),
        AsyncServiceBrowser=mock.Mock(return_value=fake_browser),
    )


def test_fetch_resideo_status_disabled_returns_none():
    assert resideo_client.fetch_resideo_status({"resideo": {"enabled": False}}) is None


def test_fetch_resideo_status_returns_none_when_pairing_file_missing(tmp_path):
    config = {"resideo": {"enabled": True, "pairing_file": str(tmp_path / "does-not-exist.json")}}

    assert resideo_client.fetch_resideo_status(config) is None


def test_fetch_resideo_status_returns_none_when_alias_not_found(tmp_path):
    pairing_file = tmp_path / "pairing.json"
    pairing_file.write_text("{}")
    config = {"resideo": {"enabled": True, "pairing_file": str(pairing_file), "pairing_alias": "wrong-alias"}}

    with _patch_pairing(_thermostat_accessory(), alias="heating-automation"):
        result = resideo_client.fetch_resideo_status(config)

    assert result is None


def test_fetch_resideo_status_returns_full_snapshot(tmp_path):
    pairing_file = tmp_path / "pairing.json"
    pairing_file.write_text("{}")
    config = {"resideo": {"enabled": True, "pairing_file": str(pairing_file)}}

    with _patch_pairing(_thermostat_accessory(current_state=1, target_state=1, current_temp=19.5, target_temp=21.0)):
        result = resideo_client.fetch_resideo_status(config)

    assert result == {
        "device_name": "Lyric T6 Thermostat",
        "mode": "heat",
        "calling_for_heat": True,
        "current_temperature_c": 19.5,
        "target_temperature_c": 21.0,
    }


def test_fetch_resideo_status_returns_none_instead_of_raising_on_unexpected_error(tmp_path):
    """Circuit Breaker: a caller collecting several subsystems in one pass (see
    status_collector.py) must not have one integration's unexpected exception
    blank the whole snapshot.
    """
    pairing_file = tmp_path / "pairing.json"
    pairing_file.write_text("{}")
    config = {"resideo": {"enabled": True, "pairing_file": str(pairing_file)}}

    with mock.patch.object(resideo_client, "Controller", side_effect=RuntimeError("boom")):
        result = resideo_client.fetch_resideo_status(config)

    assert result is None


def test_parse_thermostat_status_off_and_not_calling_for_heat():
    result = resideo_client._parse_thermostat_status(
        _thermostat_accessory(current_state=0, target_state=0)
    )

    assert result["mode"] == "off"
    assert result["calling_for_heat"] is False


def test_parse_thermostat_status_heat_mode_but_idle():
    """Mode "heat" and calling_for_heat are independent: the T6R can be set to heat
    but not currently calling for it (already at temperature)."""
    result = resideo_client._parse_thermostat_status(
        _thermostat_accessory(current_state=0, target_state=1)
    )

    assert result["mode"] == "heat"
    assert result["calling_for_heat"] is False


def test_parse_thermostat_status_returns_none_when_no_thermostat_service():
    accessories = [{"aid": 1, "services": [{"type": "some-other-service", "characteristics": []}]}]

    assert resideo_client._parse_thermostat_status(accessories) is None
