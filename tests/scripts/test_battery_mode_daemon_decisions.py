"""Unit tests for BatteryModeDaemon's own decision logic: _is_ohme_charging,
_get_scheduled_mode, _can_change_mode, _check_battery_protection, and
_determine_target_mode (priority: Ohme > Schedule > Default) - the actual
"what mode should the battery be in" logic, previously completely untested.

Constructs a daemon directly (bypassing load_config()'s file I/O) and sets
daemon_config/system_config attributes by hand, so these tests never touch
a config file or real hardware - solax_modbus_soc is monkeypatched at the
daemon's own call site (the same pattern used elsewhere this session).
"""

from __future__ import annotations

import datetime as datetime_module
import time as time_module

from battery_mode_daemon import BatteryModeDaemon
from src.core_logic.battery_simulation import BatteryMode


class _FrozenDateTime(datetime_module.datetime):
    """A datetime subclass whose now() always returns a fixed instant.

    Subclassing (rather than a bare stand-in object) keeps strptime() and
    every other classmethod _get_scheduled_mode relies on working normally.
    """

    _frozen_now: datetime_module.datetime

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003 - matching datetime.now's signature
        return cls._frozen_now


def _freeze_time_of_day(monkeypatch, hour: int, minute: int) -> None:
    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = datetime_module.datetime(2026, 1, 1, hour, minute)
    monkeypatch.setattr("battery_mode_daemon.datetime", frozen)


def _make_daemon(tmp_path, monkeypatch) -> BatteryModeDaemon:
    monkeypatch.chdir(tmp_path)
    daemon = BatteryModeDaemon(config_path=str(tmp_path / "daemon_config.json"))
    daemon.daemon_config = {
        "daemon_settings": {
            "hardware_poll_interval_seconds": 60,
            "min_mode_change_interval_seconds": 600,
            "ohme_charging_threshold_watts": 500,
            "min_discharge_soc_percent": 20,
        },
        "ohme_charging": {"enabled": True, "force_charge_mode": "FORCE_CHARGE"},
        "schedule": {
            "enabled": True,
            "default_mode": "SELF_USE",
            "time_ranges": [
                {
                    "start_time": "00:00",
                    "end_time": "05:00",
                    "battery_mode": "FORCE_CHARGE",
                    "description": "Overnight cheap rate",
                },
                {
                    "start_time": "23:00",
                    "end_time": "01:00",
                    "battery_mode": "FORCE_DISCHARGE",
                    "description": "Crosses midnight",
                },
            ],
        },
        "logging": {"level": "INFO", "file_path": "logs/battery_mode_daemon.log"},
    }
    daemon.system_config = {}
    return daemon


# --- _is_ohme_charging -----------------------------------------------------


def test_is_ohme_charging_none_status_resets_counter_and_returns_false(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    daemon.ohme_charging_count = 1

    assert daemon._is_ohme_charging(None) is False
    assert daemon.ohme_charging_count == 0


def test_is_ohme_charging_below_threshold_returns_false(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)

    assert daemon._is_ohme_charging({"power_watts": 100}) is False
    assert daemon.ohme_charging_count == 0


def test_is_ohme_charging_first_cycle_above_threshold_waits_for_confirmation(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)

    assert daemon._is_ohme_charging({"power_watts": 1000}) is False
    assert daemon.ohme_charging_count == 1


def test_is_ohme_charging_second_consecutive_cycle_confirms(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)

    daemon._is_ohme_charging({"power_watts": 1000})
    assert daemon._is_ohme_charging({"power_watts": 1000}) is True
    assert daemon.ohme_charging_count == 2


def test_is_ohme_charging_drop_below_threshold_resets_counter(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)

    daemon._is_ohme_charging({"power_watts": 1000})
    assert daemon._is_ohme_charging({"power_watts": 100}) is False
    assert daemon.ohme_charging_count == 0


# --- _get_scheduled_mode -----------------------------------------------------


def test_get_scheduled_mode_disabled_returns_none(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    daemon.daemon_config["schedule"]["enabled"] = False

    assert daemon._get_scheduled_mode() is None


def test_get_scheduled_mode_within_normal_range(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    _freeze_time_of_day(monkeypatch, 2, 0)

    assert daemon._get_scheduled_mode() == BatteryMode.FORCE_CHARGE


def test_get_scheduled_mode_within_midnight_crossing_range(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    _freeze_time_of_day(monkeypatch, 23, 30)

    assert daemon._get_scheduled_mode() == BatteryMode.FORCE_DISCHARGE


def test_get_scheduled_mode_outside_all_ranges_returns_none(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    _freeze_time_of_day(monkeypatch, 12, 0)

    assert daemon._get_scheduled_mode() is None


# --- _can_change_mode --------------------------------------------------------


def test_can_change_mode_never_changed_before(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    daemon.last_mode_change_time = None

    can_change, elapsed = daemon._can_change_mode()

    assert can_change is True
    assert elapsed == 0


def test_can_change_mode_blocked_within_min_interval(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    daemon.last_mode_change_time = time_module.time() - 10  # 10s ago, min interval is 600s

    can_change, elapsed = daemon._can_change_mode()

    assert can_change is False
    assert elapsed == 10


def test_can_change_mode_allowed_after_min_interval(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    daemon.last_mode_change_time = time_module.time() - 700  # past the 600s min interval

    can_change, _elapsed = daemon._can_change_mode()

    assert can_change is True


# --- _check_battery_protection -----------------------------------------------


def test_check_battery_protection_ignores_non_discharge_targets(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "battery_mode_daemon.solax_modbus_soc",
        lambda config: pytest_fail_if_called(),  # should never be called
    )

    should_block, reason = daemon._check_battery_protection(BatteryMode.FORCE_CHARGE)

    assert should_block is False
    assert reason is None


def pytest_fail_if_called():
    raise AssertionError("solax_modbus_soc should not have been called")


def test_check_battery_protection_blocks_when_soc_unreadable(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    monkeypatch.setattr("battery_mode_daemon.solax_modbus_soc", lambda config: None)

    should_block, reason = daemon._check_battery_protection(BatteryMode.FORCE_DISCHARGE)

    assert should_block is True
    assert "Cannot read" in reason


def test_check_battery_protection_blocks_when_soc_at_or_below_threshold(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "battery_mode_daemon.solax_modbus_soc", lambda config: {"master": 20, "slave": 25}
    )

    should_block, reason = daemon._check_battery_protection(BatteryMode.FORCE_DISCHARGE)

    assert should_block is True
    assert "20" in reason


def test_check_battery_protection_allows_when_soc_above_threshold(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "battery_mode_daemon.solax_modbus_soc", lambda config: {"master": 50, "slave": 55}
    )

    should_block, reason = daemon._check_battery_protection(BatteryMode.FORCE_DISCHARGE)

    assert should_block is False
    assert reason is None


# --- _determine_target_mode (priority: Ohme > Schedule > Default) -----------


def test_determine_target_mode_ohme_charging_wins_over_schedule(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    _freeze_time_of_day(monkeypatch, 2, 0)
    daemon.ohme_charging_count = 1  # one more confirming cycle -> charging confirmed

    mode, reason = daemon._determine_target_mode({"power_watts": 1000})

    assert mode == BatteryMode.FORCE_CHARGE
    assert "Ohme" in reason


def test_determine_target_mode_schedule_wins_when_ohme_not_charging(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    _freeze_time_of_day(monkeypatch, 23, 30)

    mode, reason = daemon._determine_target_mode(None)

    assert mode == BatteryMode.FORCE_DISCHARGE
    assert "Schedule" in reason


def test_determine_target_mode_default_when_ohme_off_and_no_schedule_match(tmp_path, monkeypatch):
    daemon = _make_daemon(tmp_path, monkeypatch)
    daemon.daemon_config["ohme_charging"]["enabled"] = False
    _freeze_time_of_day(monkeypatch, 12, 0)

    mode, reason = daemon._determine_target_mode({"power_watts": 1000})

    assert mode == BatteryMode.SELF_USE
    assert "Default" in reason
