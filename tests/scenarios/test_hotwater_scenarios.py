"""Combined scenario simulation for the hot water automation, mirroring
test_battery_daemon_scenarios.py's approach for the battery daemon.

Everything else tests one hot water decision in isolation, with a hand-built
context object. This drives the REAL run_force_heat_check /
run_revert_check / run_legionella_progress_check code through several
polling cycles against a stateful fake MELCloud server, a stateful fake Ohme
server and a real (fake) Modbus TCP server for battery SoC - so the
interactions between them are what's under test, not any single decision:

- a force-heat window opened by the car charging must survive the car
  stopping mid-window (the deliberate split between "start" and "revert"),
- it must end when the tank reaches target,
- the max-duration safety net must end it even if the tank never does,
- a legionella cycle must suppress the normal checks while it runs, and
  restore the original target temperature when it finishes.

These are exactly the paths where a wrong interaction would leave the
immersion heater running, or the tank cold, for hours.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest import mock

import hotwater_automation_core as core
import pytest
import yaml
from aioresponses import aioresponses
from melcloud_fake_server import FakeMelCloudServer
from ohme_fake_server import FakeOhmeServer
from solax_fake_server import fake_solax_server_factory, make_solax_config  # noqa: F401 - pytest fixture, used via injection

REGISTER_SOC = 0x001C

# Before hotwater_automation's default 21:30 trigger hour, so the car-charging
# branch is live; and outside the 23:30-05:30 off-peak window.
BEFORE_TRIGGER = datetime(2026, 9, 3, 19, 0, tzinfo=UTC)
# After the trigger hour, when the decision switches to battery/off-peak.
AFTER_TRIGGER = datetime(2026, 9, 3, 22, 0, tzinfo=UTC)


@pytest.fixture
def hotwater_env(tmp_path, monkeypatch, fake_solax_server_factory):  # noqa: F811 - pytest fixture injection
    """Config + isolated state file + a fake inverter, wired for hot water.

    Based on the real config.yaml (like the battery scenario test) - a
    hand-built dict fails schema validation, which requires ~9 other
    top-level sections.
    """
    from pathlib import Path

    project_config = Path(__file__).resolve().parent.parent.parent / "config.yaml"
    with project_config.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)

    solax_server = fake_solax_server_factory(initial_input={REGISTER_SOC: 80})
    config["solaX_cloud_api"].update(make_solax_config(solax_server)["solaX_cloud_api"])
    config["solaX_cloud_api"]["min_command_interval"] = 0
    config["ohme_ev"] = {"enabled": True, "username": "test@example.com", "password": "dummy"}
    config["melcloud"] = {
        "enabled": True,
        "email": "test@example.com",
        "password": "dummy",
        # The real 15s-per-attempt verify delay would make this file take
        # minutes; the fake server applies changes instantly, so one second
        # is plenty. 1 is also the schema's own minimum.
        "mode_change_retry": {"max_attempts": 2, "check_delay_seconds": 1},
    }
    config["battery_evening_prediction"] = {"enabled": False}

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    state_path = tmp_path / "hotwater_automation_state.json"
    monkeypatch.setattr(core, "get_config_path", lambda: str(config_path))
    monkeypatch.setattr(core, "get_hotwater_automation_state_path", lambda: str(state_path))
    # The shared Ohme cache belongs to the real machine, not this test - force
    # the direct-read path so the fake Ohme server is what answers.
    monkeypatch.setattr(core, "read_fresh_status", lambda *a, **k: None)

    def seed_recent_legionella_cycle():
        """Mark a cycle as just completed, so force-heat takes the NORMAL path.

        "Never completed" is treated as due (see _is_legionella_due), which
        means the very first force-heat on a fresh state file is always a
        legionella cycle - correct, but not what the normal-path scenarios
        below are about.
        """
        import json

        state_path.write_text(
            json.dumps(
                {
                    "legionella": {
                        "cycle_in_progress": False,
                        "last_completed_at": (BEFORE_TRIGGER - timedelta(days=1)).isoformat(),
                    }
                }
            ),
            encoding="utf-8",
        )

    hw_config = config.get("hotwater_automation", {})
    hw_config.update(
        {
            "enabled": True,
            "tank_temp_threshold_c": 45.0,
            "battery_soc_min_percent": 50.0,
            "trigger_hour": 21.5,
            "force_heat_max_duration_hours": 3.0,
            "ohme_charging_threshold_watts": 500.0,
            "legionella_interval_days": 90,
            "legionella_target_temp_c": 60.0,
            "legionella_max_cycle_duration_hours": 6.0,
        }
    )
    return {
        "config": config,
        "hw_config": hw_config,
        "state_path": state_path,
        "seed_recent_legionella_cycle": seed_recent_legionella_cycle,
    }


def _at(moment: datetime):
    """Freeze core's view of "now" (it reads UTC and converts to local)."""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    return mock.patch.object(core, "datetime", _FrozenDatetime)


async def _force_heat(env, melcloud, ohme, *, at):
    with aioresponses() as mocked:
        melcloud.register(mocked)
        ohme.register(mocked)
        with _at(at):
            return await core.run_force_heat_check(
                env["config"], env["hw_config"], dry_run=False, quiet=True
            )


async def _revert(env, melcloud, *, at):
    with aioresponses() as mocked:
        melcloud.register(mocked)
        with _at(at):
            return await core.run_revert_check(env["hw_config"], dry_run=False, quiet=True)


async def _legionella_progress(env, melcloud, *, at):
    with aioresponses() as mocked:
        melcloud.register(mocked)
        with _at(at):
            return await core.run_legionella_progress_check(
                env["hw_config"], dry_run=False, quiet=True
            )


@pytest.mark.asyncio
async def test_car_charging_opens_a_window_that_survives_the_car_stopping(hotwater_env):
    """The whole reason starting and reverting are separate operations: a
    window opened because the car was charging must run to completion even
    when the car unplugs two minutes later."""
    env = hotwater_env
    melcloud = FakeMelCloudServer(tank_temperature=38.0, target_tank_temperature=45.0)
    ohme = FakeOhmeServer(power_watts=7000.0)
    env["seed_recent_legionella_cycle"]()

    # Cycle 1: charging detected, but confirmation needs two consecutive cycles.
    await _force_heat(env, melcloud, ohme, at=BEFORE_TRIGGER)
    assert melcloud.state["ForcedHotWaterMode"] is False

    # Cycle 2: confirmed - force heat starts.
    await _force_heat(env, melcloud, ohme, at=BEFORE_TRIGGER + timedelta(minutes=10))
    assert melcloud.state["ForcedHotWaterMode"] is True
    assert core.read_state().get("force_heat_activated_at")

    # The car stops. The revert check must NOT cancel the window - the tank
    # is still cold.
    ohme.power_watts = 0.0
    melcloud.state["TankWaterTemperature"] = 41.0
    await _revert(env, melcloud, at=BEFORE_TRIGGER + timedelta(minutes=30))
    assert melcloud.state["ForcedHotWaterMode"] is True

    # Tank reaches target - now it reverts.
    melcloud.state["TankWaterTemperature"] = 45.5
    await _revert(env, melcloud, at=BEFORE_TRIGGER + timedelta(minutes=60))
    assert melcloud.state["ForcedHotWaterMode"] is False
    assert "force_heat_activated_at" not in core.read_state()


@pytest.mark.asyncio
async def test_max_duration_safety_net_ends_a_window_the_tank_never_finishes(hotwater_env):
    """If MELCloud never reports the tank reaching target (a stuck sensor, a
    unit that isn't actually heating), the window must still end."""
    env = hotwater_env
    melcloud = FakeMelCloudServer(tank_temperature=38.0, target_tank_temperature=45.0)
    ohme = FakeOhmeServer(power_watts=7000.0)
    env["seed_recent_legionella_cycle"]()

    await _force_heat(env, melcloud, ohme, at=BEFORE_TRIGGER)
    await _force_heat(env, melcloud, ohme, at=BEFORE_TRIGGER + timedelta(minutes=10))
    assert melcloud.state["ForcedHotWaterMode"] is True

    # Still cold, but past force_heat_max_duration_hours (3.0).
    await _revert(env, melcloud, at=BEFORE_TRIGGER + timedelta(hours=4))
    assert melcloud.state["ForcedHotWaterMode"] is False


@pytest.mark.asyncio
async def test_stored_solar_triggers_heating_after_the_trigger_hour(hotwater_env):
    """After trigger_hour the car is ignored entirely and the decision switches
    to battery SoC / off-peak."""
    env = hotwater_env
    melcloud = FakeMelCloudServer(tank_temperature=38.0, target_tank_temperature=45.0)
    ohme = FakeOhmeServer(power_watts=0.0)  # car not charging at all
    env["seed_recent_legionella_cycle"]()

    await _force_heat(env, melcloud, ohme, at=AFTER_TRIGGER)

    # Battery SoC is 80% (fake inverter), above the 50% minimum.
    assert melcloud.state["ForcedHotWaterMode"] is True


@pytest.mark.asyncio
async def test_a_warm_tank_is_never_heated(hotwater_env):
    env = hotwater_env
    melcloud = FakeMelCloudServer(tank_temperature=48.0, target_tank_temperature=45.0)
    ohme = FakeOhmeServer(power_watts=7000.0)

    await _force_heat(env, melcloud, ohme, at=BEFORE_TRIGGER)
    await _force_heat(env, melcloud, ohme, at=BEFORE_TRIGGER + timedelta(minutes=10))

    assert melcloud.state["ForcedHotWaterMode"] is False


@pytest.mark.asyncio
async def test_legionella_cycle_raises_the_target_then_restores_it(hotwater_env):
    """A due legionella cycle rides the same trigger, suppresses the normal
    checks while it runs, and puts the original target temperature back."""
    env = hotwater_env
    melcloud = FakeMelCloudServer(
        tank_temperature=38.0, target_tank_temperature=45.0, max_tank_temperature=75.0
    )
    ohme = FakeOhmeServer(power_watts=7000.0)

    # No cycle has ever completed, so one is due.
    await _force_heat(env, melcloud, ohme, at=BEFORE_TRIGGER)
    await _force_heat(env, melcloud, ohme, at=BEFORE_TRIGGER + timedelta(minutes=10))

    state = core.read_state()
    assert state["legionella"]["cycle_in_progress"] is True
    assert state["legionella"]["original_target_temp_c"] == 45.0
    assert melcloud.state["SetTankWaterTemperature"] == 60.0
    assert melcloud.state["ForcedHotWaterMode"] is True

    # While it runs, the normal force-heat and revert checks must stand aside.
    await _force_heat(env, melcloud, ohme, at=BEFORE_TRIGGER + timedelta(minutes=20))
    await _revert(env, melcloud, at=BEFORE_TRIGGER + timedelta(minutes=25))
    assert melcloud.state["SetTankWaterTemperature"] == 60.0
    assert melcloud.state["ForcedHotWaterMode"] is True

    # Not there yet.
    melcloud.state["TankWaterTemperature"] = 55.0
    await _legionella_progress(env, melcloud, at=BEFORE_TRIGGER + timedelta(hours=1))
    assert melcloud.state["ForcedHotWaterMode"] is True

    # Reaches the legionella target - reverts, and restores the 45C target.
    melcloud.state["TankWaterTemperature"] = 60.5
    await _legionella_progress(env, melcloud, at=BEFORE_TRIGGER + timedelta(hours=2))
    assert melcloud.state["SetTankWaterTemperature"] == 45.0
    assert melcloud.state["ForcedHotWaterMode"] is False

    state = core.read_state()
    assert state["legionella"]["cycle_in_progress"] is False
    assert state["legionella"]["last_completed_at"]


@pytest.mark.asyncio
async def test_legionella_timeout_reverts_without_marking_it_complete(hotwater_env):
    """A cycle that never reaches temperature must still release the tank, but
    must not count as done - it has to be retried."""
    env = hotwater_env
    melcloud = FakeMelCloudServer(tank_temperature=38.0, target_tank_temperature=45.0)
    ohme = FakeOhmeServer(power_watts=7000.0)

    await _force_heat(env, melcloud, ohme, at=BEFORE_TRIGGER)
    await _force_heat(env, melcloud, ohme, at=BEFORE_TRIGGER + timedelta(minutes=10))
    assert core.read_state()["legionella"]["cycle_in_progress"] is True

    # Past legionella_max_cycle_duration_hours (6.0), still short of 60C.
    melcloud.state["TankWaterTemperature"] = 52.0
    await _legionella_progress(env, melcloud, at=BEFORE_TRIGGER + timedelta(hours=7))

    assert melcloud.state["SetTankWaterTemperature"] == 45.0
    assert melcloud.state["ForcedHotWaterMode"] is False
    state = core.read_state()
    assert state["legionella"]["cycle_in_progress"] is False
    assert not state["legionella"].get("last_completed_at")
