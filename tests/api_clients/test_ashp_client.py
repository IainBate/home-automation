"""Tests for ashp_client.py (backend dispatch) and _ashp_t6r_backend.py (T6R writes)."""

from __future__ import annotations

from unittest import mock

import pytest
from aiohomekit.model.characteristics import CharacteristicsTypes
from aiohomekit.model.services import ServicesTypes

from src.api_clients import _ashp_t6r_backend, ashp_client


def _thermostat_accessory(
    *,
    current_state=0,
    target_state=0,
    current_temp=20.0,
    target_temp=21.0,
    mode_writable=True,
    temp_writable=True,
):
    """Build a fake list_accessories_and_characteristics() result with iid/perms,
    matching what put_characteristics-based writes need (unlike
    test_resideo_client.py's own helper, which is read-only and omits them).
    """
    mode_perms = ["pr", "ev"] + (["pw"] if mode_writable else [])
    temp_perms = ["pr", "ev"] + (["pw"] if temp_writable else [])
    return [
        {
            "aid": 1,
            "services": [
                {
                    "type": ServicesTypes.THERMOSTAT,
                    "characteristics": [
                        {
                            "type": CharacteristicsTypes.HEATING_COOLING_CURRENT,
                            "iid": 275,
                            "value": current_state,
                            "perms": ["pr", "ev"],
                        },
                        {
                            "type": CharacteristicsTypes.HEATING_COOLING_TARGET,
                            "iid": 276,
                            "value": target_state,
                            "perms": mode_perms,
                        },
                        {
                            "type": CharacteristicsTypes.TEMPERATURE_CURRENT,
                            "iid": 277,
                            "value": current_temp,
                            "perms": ["pr", "ev"],
                        },
                        {
                            "type": CharacteristicsTypes.TEMPERATURE_TARGET,
                            "iid": 278,
                            "value": target_temp,
                            "perms": temp_perms,
                        },
                    ],
                }
            ],
        }
    ]


def _patch_pairing(accessories_sequence, *, alias="heating-automation"):
    """Same shape as test_resideo_client.py's _patch_pairing, plus a mocked
    put_characteristics and a sequence of read results (one per
    list_accessories_and_characteristics() call: the initial read, then one
    per verify attempt) so tests can control exactly when a write appears
    to have landed.
    """
    fake_pairing = mock.Mock()
    fake_pairing.list_accessories_and_characteristics = mock.AsyncMock(
        side_effect=accessories_sequence
    )
    fake_pairing.put_characteristics = mock.AsyncMock(return_value={})

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

    return (
        mock.patch.multiple(
            _ashp_t6r_backend,
            Controller=mock.Mock(return_value=fake_controller),
            AsyncZeroconf=mock.Mock(return_value=fake_zeroconf),
            AsyncServiceBrowser=mock.Mock(return_value=fake_browser),
        ),
        fake_pairing,
    )


def _config(tmp_path, **resideo_overrides):
    pairing_file = tmp_path / "pairing.json"
    pairing_file.write_text("{}")
    resideo = {"enabled": True, "pairing_file": str(pairing_file)}
    resideo.update(resideo_overrides)
    return {"resideo": resideo}


# --- ashp_client.py: backend dispatch ---------------------------------------


def test_unknown_backend_raises():
    config = {"ashp": {"control_backend": "melcloud"}}
    with pytest.raises(ValueError, match="not implemented"):
        ashp_client.set_ashp_heat_call(config, 18.0)


def test_default_backend_is_t6r():
    config = {}
    with mock.patch.object(_ashp_t6r_backend, "set_ashp_heat_call", return_value=True) as fake:
        result = ashp_client.set_ashp_heat_call(config, 18.0)
    fake.assert_called_once_with(config, 18.0)
    assert result is True


def test_set_ashp_off_delegates_to_configured_backend():
    config = {"ashp": {"control_backend": "t6r"}}
    with mock.patch.object(_ashp_t6r_backend, "set_ashp_off", return_value=True) as fake:
        result = ashp_client.set_ashp_off(config)
    fake.assert_called_once_with(config)
    assert result is True


# --- _ashp_t6r_backend.py: set_ashp_heat_call --------------------------------


@mock.patch("asyncio.sleep", new_callable=mock.AsyncMock)
def test_heat_call_verifies_on_first_attempt(_sleep, tmp_path):
    config = _config(tmp_path)
    # First read (to find aid/iid), then one verify read showing the write applied.
    reads = [
        _thermostat_accessory(current_state=0, target_state=0, target_temp=16.0),
        _thermostat_accessory(current_state=1, target_state=1, target_temp=18.0),
    ]
    patcher, fake_pairing = _patch_pairing(reads)
    with patcher:
        result = _ashp_t6r_backend.set_ashp_heat_call(config, 18.0)

    assert result is True
    fake_pairing.put_characteristics.assert_awaited_once_with([(1, 276, 1), (1, 278, 18.0)])


@mock.patch("asyncio.sleep", new_callable=mock.AsyncMock)
def test_heat_call_retries_then_verifies(_sleep, tmp_path):
    config = _config(tmp_path)
    reads = [
        _thermostat_accessory(target_state=0, target_temp=16.0),  # initial read
        _thermostat_accessory(target_state=0, target_temp=16.0),  # attempt 1: not yet applied
        _thermostat_accessory(target_state=1, target_temp=18.0),  # attempt 2: applied
    ]
    patcher, _fake_pairing = _patch_pairing(reads)
    with patcher:
        result = _ashp_t6r_backend.set_ashp_heat_call(config, 18.0)

    assert result is True


@mock.patch("asyncio.sleep", new_callable=mock.AsyncMock)
def test_heat_call_gives_up_after_max_attempts(_sleep, tmp_path):
    config = _config(tmp_path)
    # Initial read + WRITE_VERIFY_MAX_ATTEMPTS verify reads, none showing the change.
    reads = [_thermostat_accessory(target_state=0, target_temp=16.0)] * (
        1 + _ashp_t6r_backend.WRITE_VERIFY_MAX_ATTEMPTS
    )
    patcher, fake_pairing = _patch_pairing(reads)
    with patcher:
        result = _ashp_t6r_backend.set_ashp_heat_call(config, 18.0)

    assert result is False
    assert (
        fake_pairing.list_accessories_and_characteristics.await_count
        == 1 + _ashp_t6r_backend.WRITE_VERIFY_MAX_ATTEMPTS
    )


@mock.patch("asyncio.sleep", new_callable=mock.AsyncMock)
def test_heat_call_false_when_not_paired_writable(_sleep, tmp_path):
    config = _config(tmp_path)
    reads = [_thermostat_accessory(mode_writable=False)]
    patcher, fake_pairing = _patch_pairing(reads)
    with patcher:
        result = _ashp_t6r_backend.set_ashp_heat_call(config, 18.0)

    assert result is False
    fake_pairing.put_characteristics.assert_not_awaited()


def test_heat_call_false_when_pairing_file_missing(tmp_path):
    config = {"resideo": {"enabled": True, "pairing_file": str(tmp_path / "missing.json")}}

    assert _ashp_t6r_backend.set_ashp_heat_call(config, 18.0) is False


@mock.patch("asyncio.sleep", new_callable=mock.AsyncMock)
def test_heat_call_false_when_no_thermostat_service(_sleep, tmp_path):
    config = _config(tmp_path)
    reads = [[{"aid": 1, "services": [{"type": "some-other-service", "characteristics": []}]}]]
    patcher, _fake_pairing = _patch_pairing(reads)
    with patcher:
        result = _ashp_t6r_backend.set_ashp_heat_call(config, 18.0)

    assert result is False


# --- _ashp_t6r_backend.py: set_ashp_off --------------------------------------


@mock.patch("asyncio.sleep", new_callable=mock.AsyncMock)
def test_off_writes_mode_off_and_leaves_current_target_unchanged(_sleep, tmp_path):
    config = _config(tmp_path)
    reads = [
        _thermostat_accessory(target_state=1, target_temp=22.5),  # currently heat-calling
        _thermostat_accessory(target_state=0, target_temp=22.5),  # verified off
    ]
    patcher, fake_pairing = _patch_pairing(reads)
    with patcher:
        result = _ashp_t6r_backend.set_ashp_off(config)

    assert result is True
    # target stays at whatever it currently read (22.5), not overwritten to a new value.
    fake_pairing.put_characteristics.assert_awaited_once_with([(1, 276, 0), (1, 278, 22.5)])


@mock.patch("asyncio.sleep", new_callable=mock.AsyncMock)
def test_off_gives_up_after_max_attempts(_sleep, tmp_path):
    config = _config(tmp_path)
    reads = [_thermostat_accessory(target_state=1, target_temp=22.5)] * (
        1 + _ashp_t6r_backend.WRITE_VERIFY_MAX_ATTEMPTS
    )
    patcher, _fake_pairing = _patch_pairing(reads)
    with patcher:
        result = _ashp_t6r_backend.set_ashp_off(config)

    assert result is False


def test_public_functions_never_raise_on_unexpected_error(tmp_path):
    """Circuit Breaker, matching resideo_client.fetch_resideo_status's own
    convention - a caller must never see an unexpected exception from these.
    """
    config = _config(tmp_path)
    with mock.patch.object(_ashp_t6r_backend, "Controller", side_effect=RuntimeError("boom")):
        assert _ashp_t6r_backend.set_ashp_heat_call(config, 18.0) is False
        assert _ashp_t6r_backend.set_ashp_off(config) is False
