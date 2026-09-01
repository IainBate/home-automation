"""End-to-end test of the SolaX hardware mode-change write path against a
real (fake) Modbus TCP server - the gap directly flagged by the user: no
test previously exercised solax_modbus_set_work_mode() itself.

Runs the real solax_modbus_client.py / _modbus_mode_controller.py code -
register writes, the mode-change log, and the safety-interval check - over
an actual TCP connection to a local pymodbus server standing in for a SolaX
inverter. Never touches a real IP.

Register map note: SolaX uses SEPARATE registers for reading current status
(0x008B work mode / 0x008C manual mode, read by solax_modbus_work_mode) and
for writing a mode change (0x001F / 0x0020, written by
solax_modbus_set_work_mode via _modbus_mode_controller's
VALID_WORK_MODE_COMBINATIONS). Real hardware presumably updates the status
registers internally in response to a command write; this fake server has
no such internal linkage, so tests that need "current mode" to reflect an
earlier write do so explicitly via write_holding_register on the status
registers - see test_second_rapid_change_blocked_by_safety_interval_unless_forced.

_check_hardware_write_safety() auto-blocks any real register write whenever
it detects it's running under pytest (see
src/api_clients/_modbus_mode_controller.py's _is_actively_testing()) - a
deliberate defense-in-depth net against accidental hardware writes from a
test suite. To genuinely exercise the write path here, this module
monkeypatches that one function to allow writes through - scoped to this
file only, and only ever against the local fake server constructed above,
never a real IP. This is the one sanctioned bypass of that safety net in
the whole test suite.
"""

from __future__ import annotations

import json

import pytest

from solax_fake_server import fake_solax_server_factory, make_solax_config  # noqa: F401 - fake_solax_server_factory is a pytest fixture, used via injection
from src.api_clients import _modbus_mode_controller as controller
from src.api_clients import solax_modbus_client as client_module
from src.core_logic.battery_simulation import BatteryMode

# Command registers - written by solax_modbus_set_work_mode to request a mode change.
REGISTER_WORK_MODE_COMMAND = 0x001F
REGISTER_MANUAL_MODE_COMMAND = 0x0020

# Status registers - read by solax_modbus_work_mode to report the current mode.
REGISTER_WORK_MODE_STATUS = 0x008B
REGISTER_MANUAL_MODE_STATUS = 0x008C

REGISTER_SOC = 0x001C


@pytest.fixture(autouse=True)
def _bypass_test_context_block(monkeypatch):
    """Let real register writes through to the fake server for this file only.

    Without this, every write in this file would be silently "simulated"
    (see _check_hardware_write_safety's TIER 1 check) rather than actually
    reaching the fake server - which would defeat the entire point of these
    tests. Never do this against anything but a local fake server.
    """
    monkeypatch.setattr(
        controller, "_check_hardware_write_safety", lambda *a, **k: (True, "safety_checks_passed")
    )


@pytest.fixture(autouse=True)
def _isolate_mode_change_log(tmp_path, monkeypatch):
    """Redirect the mode-change log to a tmp file - never touch the real repo's."""
    log_path = tmp_path / "solax_mode_change_log.json"
    monkeypatch.setattr(controller, "_get_mode_change_log_path", lambda: str(log_path))
    return log_path


def test_read_work_mode_self_use(fake_solax_server_factory):
    server = fake_solax_server_factory(initial_holding={REGISTER_WORK_MODE_STATUS: 0})
    config = make_solax_config(server)

    assert client_module.solax_modbus_work_mode(config) == BatteryMode.SELF_USE


def test_read_work_mode_manual_force_charge(fake_solax_server_factory):
    server = fake_solax_server_factory(
        initial_holding={REGISTER_WORK_MODE_STATUS: 3, REGISTER_MANUAL_MODE_STATUS: 1}
    )
    config = make_solax_config(server)

    assert client_module.solax_modbus_work_mode(config) == BatteryMode.FORCE_CHARGE


def test_read_soc(fake_solax_server_factory):
    server = fake_solax_server_factory(initial_input={REGISTER_SOC: 77})
    config = make_solax_config(server)

    soc = client_module.solax_modbus_soc(config)

    assert soc == {"master": 77, "slave": 77}


@pytest.mark.parametrize(
    ("target_mode", "expected_registers"),
    [
        (BatteryMode.FORCE_CHARGE, {REGISTER_WORK_MODE_COMMAND: 3, REGISTER_MANUAL_MODE_COMMAND: 1}),
        (
            BatteryMode.FORCE_DISCHARGE,
            {REGISTER_WORK_MODE_COMMAND: 3, REGISTER_MANUAL_MODE_COMMAND: 2},
        ),
        (BatteryMode.MANUAL_STOP, {REGISTER_WORK_MODE_COMMAND: 3, REGISTER_MANUAL_MODE_COMMAND: 0}),
    ],
)
def test_set_work_mode_writes_correct_registers(
    fake_solax_server_factory, target_mode, expected_registers, _isolate_mode_change_log
):
    # Status registers default to 0 (SELF_USE) - every target here differs, so
    # solax_modbus_set_work_mode's "already in target mode" short-circuit
    # never fires and a real write always happens.
    server = fake_solax_server_factory(initial_holding={REGISTER_WORK_MODE_STATUS: 0})
    config = make_solax_config(server)

    result = client_module.solax_modbus_set_work_mode(
        config, target_mode, changed_by="test", force_unsafe=True
    )

    assert result["success"] is True
    for register_addr, expected_value in expected_registers.items():
        assert server.read_holding_registers(register_addr) == [expected_value]

    # And the mode-change log recorded it.
    log_data = json.loads(_isolate_mode_change_log.read_text())
    assert log_data["last_mode_change"]["new_mode"] == target_mode.value


def test_set_work_mode_noop_when_already_in_target_mode(fake_solax_server_factory):
    server = fake_solax_server_factory(initial_holding={REGISTER_WORK_MODE_STATUS: 0})
    config = make_solax_config(server)

    result = client_module.solax_modbus_set_work_mode(
        config, BatteryMode.SELF_USE, changed_by="test", force_unsafe=True
    )

    assert result["success"] is True
    # Command registers never touched - already in target mode.
    assert server.read_holding_registers(REGISTER_WORK_MODE_COMMAND) == [0]


def test_second_rapid_change_blocked_by_safety_interval_unless_forced(
    fake_solax_server_factory, _isolate_mode_change_log
):
    server = fake_solax_server_factory(initial_holding={REGISTER_WORK_MODE_STATUS: 0})
    config = make_solax_config(server)

    first = client_module.solax_modbus_set_work_mode(
        config, BatteryMode.FORCE_CHARGE, changed_by="test", force_unsafe=True
    )
    assert first["success"] is True

    # Simulate real hardware reflecting the command in its status registers -
    # this fake server has no internal command->status linkage of its own.
    server.write_holding_register(REGISTER_WORK_MODE_STATUS, 3)
    server.write_holding_register(REGISTER_MANUAL_MODE_STATUS, 1)

    # Without force_unsafe, a second change within the 2-minute window is blocked.
    blocked = client_module.solax_modbus_set_work_mode(
        config, BatteryMode.SELF_USE, changed_by="test", force_unsafe=False
    )
    assert blocked["success"] is False
    assert blocked["error_type"] == "safety_interval"
    # Command registers unchanged from the first (successful) write.
    assert server.read_holding_registers(REGISTER_WORK_MODE_COMMAND) == [3]
    assert server.read_holding_registers(REGISTER_MANUAL_MODE_COMMAND) == [1]

    # With force_unsafe, it proceeds anyway.
    forced = client_module.solax_modbus_set_work_mode(
        config, BatteryMode.SELF_USE, changed_by="test", force_unsafe=True
    )
    assert forced["success"] is True
    assert server.read_holding_registers(REGISTER_WORK_MODE_COMMAND) == [0]


def test_test_mode_true_returns_simulated_success_without_writing(fake_solax_server_factory):
    """test_mode=True must never touch the fake server's registers either."""
    server = fake_solax_server_factory(initial_holding={REGISTER_WORK_MODE_STATUS: 0})
    config = make_solax_config(server)

    result = client_module.solax_modbus_set_work_mode(
        config, BatteryMode.FORCE_CHARGE, changed_by="test", test_mode=True, force_unsafe=True
    )

    assert result["success"] is True
    # Command registers never touched - test_mode short-circuits before any real write.
    assert server.read_holding_registers(REGISTER_WORK_MODE_COMMAND) == [0]
