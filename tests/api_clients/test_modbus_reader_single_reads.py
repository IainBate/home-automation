"""End-to-end tests for every _read_single_* reading, against a real (fake)
Modbus TCP server, plus the retry/failure behaviour they all now share.

These functions were previously eleven near-identical ~90-line copies of one
connect/retry/read/close sequence, and were the least-covered substantial
code in the repository (23%) despite sitting directly underneath every
battery-safety decision. Consolidating them onto _read_register_with_retry()
means the retry ladder can be pinned down once, here, instead of needing
eleven near-identical sets of failure tests - and each reading's own register
address and unit conversion is verified against real register values served
over a real socket.
"""

from __future__ import annotations

from unittest import mock

import pytest

from solax_fake_server import fake_solax_server_factory, make_solax_config  # noqa: F401 - pytest fixture, used via injection
from src.api_clients import _modbus_reader as reader
from src.api_clients import solax_modbus_client
from src.core_logic.battery_simulation import BatteryMode


def _read_args(server):
    """Positional args every _read_single_* takes, pointed at the fake server."""
    solax = make_solax_config(server)["solaX_cloud_api"]
    return (
        solax["master_ip"],
        solax["modbus_port"],
        solax["modbus_connection_timeout"],
        solax["master_modbus_address"],
        0,  # min_interval - no need to slow the tests down
    )


# --- Per-reading register mapping and unit conversion ----------------------


def test_read_ac_power_converts_to_signed(fake_solax_server_factory):
    # 65036 as uint16 is -500 as int16 - the conversion every power reading needs.
    server = fake_solax_server_factory(initial_input={0x0002: 65036})
    assert reader._read_single_ac_power(*_read_args(server)) == -500


def test_read_battery_temperature_converts_to_signed(fake_solax_server_factory):
    server = fake_solax_server_factory(initial_input={0x0018: 65531})
    assert reader._read_single_battery_temperature(*_read_args(server)) == -5


def test_read_grid_power_converts_to_signed(fake_solax_server_factory):
    """Negative means importing from the grid - the sign must survive."""
    server = fake_solax_server_factory(initial_input={0x0046: 65036})
    assert reader._read_single_grid_power(*_read_args(server)) == -500


def test_read_grid_power_keeps_positive_export_values(fake_solax_server_factory):
    server = fake_solax_server_factory(initial_input={0x0046: 11694})
    assert reader._read_single_grid_power(*_read_args(server)) == 11694


def test_read_soc(fake_solax_server_factory):
    server = fake_solax_server_factory(initial_input={0x001C: 77})
    assert reader._read_single_soc(*_read_args(server)) == 77


def test_read_soc_rejects_an_out_of_range_value(fake_solax_server_factory):
    """A garbled SoC must not reach battery_mode_daemon.py's protection check -
    the read retries and then gives up rather than returning nonsense."""
    server = fake_solax_server_factory(initial_input={0x001C: 250})
    with mock.patch.object(reader.time, "sleep"):  # don't wait out the retry ladder
        assert reader._read_single_soc(*_read_args(server)) is None


def test_read_battery_power_reports_power_and_mode(fake_solax_server_factory):
    server = fake_solax_server_factory(initial_input={0x0016: 65036})
    result = reader._read_single_battery_power(*_read_args(server))
    assert result["power"] == -500
    assert result["mode"] == BatteryMode.FORCE_DISCHARGE


def test_read_pv_power_reads_both_strings(fake_solax_server_factory):
    server = fake_solax_server_factory(initial_input={0x000A: 1500, 0x000B: 900})
    assert reader._read_single_pv_power(*_read_args(server)) == {"pv1": 1500, "pv2": 900}


def test_read_daily_yield_scales_tenths_of_kwh(fake_solax_server_factory):
    server = fake_solax_server_factory(initial_input={0x0050: 155})
    assert reader._read_single_daily_yield(*_read_args(server)) == pytest.approx(15.5)


def test_read_battery_capacity_combines_two_registers_as_wh(fake_solax_server_factory):
    # LSB first: 20000 Wh + (1 << 16) Wh = 85536 Wh = 85.536 kWh
    server = fake_solax_server_factory(initial_input={0x0026: 20000, 0x0027: 1})
    assert reader._read_single_battery_capacity(*_read_args(server)) == pytest.approx(85.536)


def test_read_grid_export_total_combines_two_registers_as_centi_kwh(fake_solax_server_factory):
    # (MSB << 16) | LSB, in 0.01 kWh units: (1 << 16) | 34464 = 100000 -> 1000.00 kWh
    server = fake_solax_server_factory(initial_input={0x0048: 34464, 0x0049: 1})
    assert reader._read_single_grid_export_total(*_read_args(server)) == pytest.approx(1000.0)


def test_read_grid_import_total_uses_its_own_registers(fake_solax_server_factory):
    """Import and export are adjacent register pairs - a copy-paste between the
    two would read the wrong meter and go unnoticed."""
    server = fake_solax_server_factory(
        initial_input={0x0048: 1, 0x0049: 0, 0x004A: 500, 0x004B: 0}
    )
    assert reader._read_single_grid_import_total(*_read_args(server)) == pytest.approx(5.0)


def test_read_run_mode_maps_the_raw_value(fake_solax_server_factory):
    server = fake_solax_server_factory(initial_input={0x0009: 2})
    assert reader._read_single_run_mode(*_read_args(server)) == "Normal Mode"


def test_read_inverter_serial_uses_holding_registers(fake_solax_server_factory):
    """Serial and RTC are function code 0x03 reads, unlike every other reading -
    reading them from input registers would silently return zeroes."""
    chars = [ord(c) for c in "H4752AJ9008113"]
    words = [(chars[i] << 8) | chars[i + 1] for i in range(0, 14, 2)]
    server = fake_solax_server_factory(initial_holding=dict(enumerate(words)))

    assert reader._read_single_inverter_serial(*_read_args(server)) == "H4752AJ9008113"


def test_read_rtc_timestamp_uses_holding_registers(fake_solax_server_factory):
    # Register order is [Seconds, Minutes, Hours, Days, Months, Years_since_2000]
    server = fake_solax_server_factory(
        initial_holding={0x0085: 45, 0x0086: 30, 0x0087: 14, 0x0088: 3, 0x0089: 9, 0x008A: 26}
    )
    assert reader._read_single_rtc_timestamp(*_read_args(server)) == "2026-09-03 14:30:45"


# --- Shared retry behaviour (tested once, not eleven times) ----------------


def test_returns_none_after_exhausting_every_attempt(fake_solax_server_factory):
    server = fake_solax_server_factory(initial_input={0x0002: 100})
    args = _read_args(server)

    with (
        mock.patch.object(reader.time, "sleep") as sleep,
        mock.patch(
            "src.api_clients.solax_modbus_client._connect_modbus_client", return_value=None
        ) as connect,
    ):
        assert reader._read_single_ac_power(*args) is None

    assert connect.call_count == reader.MAX_READ_ATTEMPTS
    # Slept between attempts, but not after the final one.
    retry_sleeps = [c.args[0] for c in sleep.call_args_list if c.args and c.args[0] in reader.READ_RETRY_DELAYS_SECONDS]
    assert retry_sleeps == list(reader.READ_RETRY_DELAYS_SECONDS[: reader.MAX_READ_ATTEMPTS - 1])


def test_recovers_when_a_later_attempt_succeeds(fake_solax_server_factory):
    """A transient connection drop - the failure mode this hardware actually
    exhibits - must not surface to the caller as a failed read."""
    server = fake_solax_server_factory(initial_input={0x0002: 4242})
    args = _read_args(server)

    real_connect = solax_modbus_client._connect_modbus_client
    attempts = {"n": 0}

    def flaky_connect(ip, port, timeout):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return None
        return real_connect(ip, port, timeout)

    with (
        mock.patch.object(reader.time, "sleep"),
        mock.patch(
            "src.api_clients.solax_modbus_client._connect_modbus_client", side_effect=flaky_connect
        ),
    ):
        assert reader._read_single_ac_power(*args) == 4242


def test_an_exception_mid_read_is_retried_not_raised(fake_solax_server_factory):
    server = fake_solax_server_factory(initial_input={0x0002: 100})
    args = _read_args(server)

    with (
        mock.patch.object(reader.time, "sleep"),
        mock.patch(
            "src.api_clients.solax_modbus_client._connect_modbus_client",
            side_effect=OSError("connection reset"),
        ),
    ):
        assert reader._read_single_ac_power(*args) is None


def test_a_short_register_response_is_treated_as_a_failure(fake_solax_server_factory):
    """A 2-register reading that comes back with 1 word must not be silently
    misinterpreted (an IndexError, or worse, a plausible wrong number)."""
    server = fake_solax_server_factory(initial_input={0x000A: 1500, 0x000B: 900})
    args = _read_args(server)

    with (
        mock.patch.object(reader.time, "sleep"),
        mock.patch(
            "src.api_clients.solax_modbus_client._read_input_registers", return_value=[1500]
        ),
    ):
        assert reader._read_single_pv_power(*args) is None


def test_the_client_is_closed_even_when_the_read_fails(fake_solax_server_factory):
    """Leaving sockets open is what made repeated reads fail on this hardware."""
    server = fake_solax_server_factory(initial_input={0x0002: 100})
    args = _read_args(server)
    fake_client = mock.Mock()

    with (
        mock.patch.object(reader.time, "sleep"),
        mock.patch(
            "src.api_clients.solax_modbus_client._connect_modbus_client", return_value=fake_client
        ),
        mock.patch(
            "src.api_clients.solax_modbus_client._read_input_registers", return_value=None
        ),
    ):
        assert reader._read_single_ac_power(*args) is None

    assert fake_client.close.call_count == reader.MAX_READ_ATTEMPTS
