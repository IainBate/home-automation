"""Combined scenario simulation: BatteryModeDaemon driven through realistic
operating conditions with BOTH a real fake SolaX Modbus TCP server (Phase A)
and a fake Ohme HTTP server (Phase C) wired in together, asserting the mode
that actually lands in the fake inverter's registers. This is the
"comprehensive simulation" centerpiece of the test plan - everything else
tests one layer in isolation; this exercises the daemon's real decision loop
driving real (fake) hardware and a real (fake) cloud API at once.

Drives BatteryModeDaemon._perform_hardware_cycle() directly (plain sync test
functions, not async - _perform_hardware_cycle internally calls
asyncio.run() for the Ohme check, which requires no event loop already be
running). aioresponses() is entered from sync code, which is fine - it just
patches ClientSession._request at the class level; the actual mocked HTTP
calls happen later, inside asyncio.run()'s fresh event loop.

Same sanctioned safety-check bypass as test_solax_work_mode_change.py (only
reachable here too, and only ever against the local fake server).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from aioresponses import aioresponses
from battery_mode_daemon import BatteryModeDaemon
from ohme_fake_server import FakeOhmeServer
from solax_fake_server import fake_solax_server_factory, make_solax_config  # noqa: F401 - fixture used via injection
from src.api_clients import _modbus_mode_controller as controller

REGISTER_WORK_MODE_STATUS = 0x008B
REGISTER_MANUAL_MODE_STATUS = 0x008C
REGISTER_WORK_MODE_COMMAND = 0x001F
REGISTER_MANUAL_MODE_COMMAND = 0x0020

PROJECT_ROOT_CONFIG = Path(__file__).resolve().parent.parent.parent / "config.yaml"


def _make_daemon(tmp_path, monkeypatch, fake_solax_server, *, ohme_charging_threshold_watts=500):
    """A BatteryModeDaemon wired to a fake SolaX server, with its own
    mode-change log and the SolaX safety-check bypass isolated to tmp_path.

    Based on the real project config.yaml (like other tests this session) -
    a hand-built dict with only solaX_cloud_api/ohme_ev fails schema
    validation, since ~9 other top-level sections are required.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        controller, "_check_hardware_write_safety", lambda *a, **k: (True, "safety_checks_passed")
    )
    monkeypatch.setattr(
        controller, "_get_mode_change_log_path", lambda: str(tmp_path / "solax_mode_change_log.json")
    )

    with PROJECT_ROOT_CONFIG.open(encoding="utf-8") as f:
        system_config = yaml.safe_load(f)
    # Merge (not replace) - the real config.yaml's solaX_cloud_api already
    # has the schema-required base_url/token_id/master_wifisn/slave_wifisn;
    # only override what points it at the fake server.
    system_config["solaX_cloud_api"].update(make_solax_config(fake_solax_server)["solaX_cloud_api"])
    system_config["ohme_ev"] = {"enabled": True, "username": "test@example.com", "password": "dummy"}
    system_config_path = tmp_path / "system_config.yaml"
    system_config_path.write_text(yaml.safe_dump(system_config), encoding="utf-8")

    daemon = BatteryModeDaemon(
        config_path=str(tmp_path / "daemon_config.json"), system_config_path=str(system_config_path)
    )
    daemon.system_config = system_config
    daemon.daemon_config = {
        "daemon_settings": {
            "hardware_poll_interval_seconds": 60,
            "min_mode_change_interval_seconds": 600,
            "ohme_charging_threshold_watts": ohme_charging_threshold_watts,
            "min_discharge_soc_percent": 20,
        },
        "ohme_charging": {"enabled": True, "force_charge_mode": "FORCE_CHARGE"},
        "schedule": {
            "enabled": True,
            "default_mode": "SELF_USE",
            "time_ranges": [
                {"start_time": "02:00", "end_time": "04:00", "battery_mode": "FORCE_DISCHARGE"},
            ],
        },
        "logging": {"level": "INFO", "file_path": "logs/battery_mode_daemon.log"},
    }
    return daemon


def _read_status_registers(server) -> dict:
    work_mode = server.read_holding_registers(REGISTER_WORK_MODE_STATUS)[0]
    manual_mode = server.read_holding_registers(REGISTER_MANUAL_MODE_STATUS)[0]
    return {"work_mode": work_mode, "manual_mode": manual_mode}


def _read_command_registers(server) -> dict:
    work_mode = server.read_holding_registers(REGISTER_WORK_MODE_COMMAND)[0]
    manual_mode = server.read_holding_registers(REGISTER_MANUAL_MODE_COMMAND)[0]
    return {"work_mode": work_mode, "manual_mode": manual_mode}


def test_car_starts_charging_forces_charge_mode(tmp_path, monkeypatch, fake_solax_server_factory):
    server = fake_solax_server_factory(initial_holding={REGISTER_WORK_MODE_STATUS: 0})  # SELF_USE
    daemon = _make_daemon(tmp_path, monkeypatch, server)
    ohme_server = FakeOhmeServer(power_watts=7300)  # well above the 500W threshold

    with aioresponses() as mocked:
        ohme_server.register(mocked)

        # First cycle: charging detected but not yet confirmed (needs 2
        # consecutive cycles) - no register write expected yet.
        daemon._perform_hardware_cycle()
        assert _read_command_registers(server)["work_mode"] == 0

        # Second consecutive cycle: confirmed -> FORCE_CHARGE written.
        daemon._perform_hardware_cycle()

    command = _read_command_registers(server)
    assert command == {"work_mode": 3, "manual_mode": 1}  # FORCE_CHARGE combination

    log_data = json.loads((tmp_path / "solax_mode_change_log.json").read_text())
    assert log_data["last_mode_change"]["new_mode"] == "FORCE_CHARGE"


def test_scheduled_window_sets_mode_when_car_not_charging(
    tmp_path, monkeypatch, fake_solax_server_factory
):
    server = fake_solax_server_factory(
        initial_holding={REGISTER_WORK_MODE_STATUS: 0},  # SELF_USE
        initial_input={0x001C: 50},  # healthy SoC - must not trip battery protection here
    )
    daemon = _make_daemon(tmp_path, monkeypatch, server)

    class _FrozenDateTime(__import__("datetime").datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return cls(2026, 1, 1, 3, 0)  # 03:00 - inside the 02:00-04:00 FORCE_DISCHARGE window

    monkeypatch.setattr("battery_mode_daemon.datetime", _FrozenDateTime)

    ohme_server = FakeOhmeServer(power_watts=0)  # not charging

    with aioresponses() as mocked:
        ohme_server.register(mocked)
        daemon._perform_hardware_cycle()

    command = _read_command_registers(server)
    assert command == {"work_mode": 3, "manual_mode": 2}  # FORCE_DISCHARGE combination


def test_low_soc_blocks_scheduled_force_discharge(tmp_path, monkeypatch, fake_solax_server_factory):
    server = fake_solax_server_factory(
        initial_holding={REGISTER_WORK_MODE_STATUS: 3, REGISTER_MANUAL_MODE_STATUS: 2},  # already FORCE_DISCHARGE
        initial_input={0x001C: 10},  # SoC 10% - below the 20% min_discharge_soc_percent
    )
    daemon = _make_daemon(tmp_path, monkeypatch, server)

    class _FrozenDateTime(__import__("datetime").datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return cls(2026, 1, 1, 3, 0)

    monkeypatch.setattr("battery_mode_daemon.datetime", _FrozenDateTime)

    ohme_server = FakeOhmeServer(power_watts=0)

    with aioresponses() as mocked:
        ohme_server.register(mocked)
        daemon._perform_hardware_cycle()

    # Battery protection should have overridden FORCE_DISCHARGE to SELF_USE.
    command = _read_command_registers(server)
    assert command == {"work_mode": 0, "manual_mode": 0}


def test_hardware_read_failure_falls_back_to_self_use(tmp_path, monkeypatch, fake_solax_server_factory):
    server = fake_solax_server_factory(initial_holding={REGISTER_WORK_MODE_STATUS: 3, REGISTER_MANUAL_MODE_STATUS: 1})
    daemon = _make_daemon(tmp_path, monkeypatch, server)
    ohme_server = FakeOhmeServer(power_watts=0)

    def broken_ohme_check():
        raise RuntimeError("simulated hardware failure")

    monkeypatch.setattr(daemon, "_check_ohme_status", broken_ohme_check)

    with aioresponses() as mocked:
        ohme_server.register(mocked)
        daemon._perform_hardware_cycle()

    command = _read_command_registers(server)
    assert command == {"work_mode": 0, "manual_mode": 0}  # SELF_USE - the circuit-breaker fallback
