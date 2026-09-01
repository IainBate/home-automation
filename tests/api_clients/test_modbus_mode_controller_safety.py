"""Unit tests for the pure safety-decision functions in
src/api_clients/_modbus_mode_controller.py - SAFETY-CRITICAL, but these
specific functions take already-loaded data and make no Modbus I/O
themselves, so no fake server is needed here (see test_solax_work_mode_change.py
for the fake-server integration test of the actual hardware-write path).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.api_clients import _modbus_mode_controller as controller
from src.core_logic.battery_simulation import BatteryMode

ALL_CI_ENV_VARS = ["CI", "GITHUB_ACTIONS", "JENKINS", "TRAVIS", "CIRCLECI", "BUILDKITE", "GITLAB_CI"]


# --- _check_mode_change_safety --------------------------------------------


def test_no_previous_change_is_safe():
    is_safe, message = controller._check_mode_change_safety({})
    assert is_safe is True
    assert message == ""


def test_forced_bypasses_timing_check_even_with_recent_change():
    log_data = {
        "last_mode_change": {"timestamp": datetime.now(UTC).isoformat()}
    }
    is_safe, message = controller._check_mode_change_safety(log_data, forced=True)
    assert is_safe is True
    assert message == ""


def test_recent_change_within_two_minutes_is_unsafe():
    recent = datetime.now(UTC) - timedelta(seconds=30)
    log_data = {"last_mode_change": {"timestamp": recent.isoformat()}}
    is_safe, message = controller._check_mode_change_safety(log_data)
    assert is_safe is False
    assert "seconds ago" in message
    assert "wait" in message


def test_change_over_two_minutes_ago_is_safe():
    old = datetime.now(UTC) - timedelta(minutes=5)
    log_data = {"last_mode_change": {"timestamp": old.isoformat()}}
    is_safe, message = controller._check_mode_change_safety(log_data)
    assert is_safe is True
    assert message == ""


def test_timezone_naive_timestamp_is_still_treated_as_recent():
    """Legacy log entries have no tzinfo - treated as local time and converted."""
    recent_local_naive = datetime.now() - timedelta(seconds=5)  # noqa: DTZ005 - deliberately naive
    log_data = {"last_mode_change": {"timestamp": recent_local_naive.isoformat()}}
    is_safe, _message = controller._check_mode_change_safety(log_data)
    assert is_safe is False


def test_malformed_timestamp_fails_open_to_safe():
    log_data = {"last_mode_change": {"timestamp": "not-a-real-timestamp"}}
    is_safe, message = controller._check_mode_change_safety(log_data)
    assert is_safe is True
    assert message == ""


def test_missing_timestamp_key_fails_open_to_safe():
    log_data = {"last_mode_change": {}}
    is_safe, message = controller._check_mode_change_safety(log_data)
    assert is_safe is True


# --- _validate_register_write_safety --------------------------------------


def test_valid_combinations_pass_for_every_writable_mode():
    for mode, combination in controller.VALID_WORK_MODE_COMBINATIONS.items():
        for register_addr, value in combination.items():
            assert controller._validate_register_write_safety(register_addr, value, mode) is True


def test_unknown_mode_is_rejected():
    assert (
        controller._validate_register_write_safety(
            controller.REGISTER_WORK_MODE, 0, BatteryMode.IDLE
        )
        is False
    )


def test_register_not_required_for_mode_is_rejected():
    # SELF_USE only needs REGISTER_WORK_MODE=0 - REGISTER_MANUAL_MODE isn't part of it.
    assert (
        controller._validate_register_write_safety(
            controller.REGISTER_MANUAL_MODE, 1, BatteryMode.SELF_USE
        )
        is False
    )


def test_wrong_value_for_register_and_mode_is_rejected():
    # SELF_USE expects REGISTER_WORK_MODE=0, not 3.
    assert (
        controller._validate_register_write_safety(
            controller.REGISTER_WORK_MODE, 3, BatteryMode.SELF_USE
        )
        is False
    )


# --- _check_hardware_write_safety -----------------------------------------


def test_test_mode_blocks_before_any_other_check():
    proceed, reason = controller._check_hardware_write_safety(
        "FORCE_CHARGE", "127.0.0.1", 502, test_mode=True
    )
    assert proceed is False
    assert reason == "test_mode_enabled"


def test_running_under_pytest_blocks_by_default():
    """No monkeypatching needed - PYTEST_CURRENT_TEST is genuinely set right now."""
    proceed, reason = controller._check_hardware_write_safety(
        "FORCE_CHARGE", "127.0.0.1", 502, test_mode=False
    )
    assert proceed is False
    assert reason == "test_context_detected"


def test_ci_environment_blocks_when_test_detection_is_bypassed(monkeypatch):
    monkeypatch.setattr(controller, "_is_actively_testing", lambda: False)
    monkeypatch.setenv("CI", "true")
    proceed, reason = controller._check_hardware_write_safety(
        "FORCE_CHARGE", "127.0.0.1", 502, test_mode=False
    )
    assert proceed is False
    assert reason == "ci_environment_detected"


def test_pytest_argv_blocks_when_test_and_ci_detection_are_bypassed(monkeypatch):
    monkeypatch.setattr(controller, "_is_actively_testing", lambda: False)
    for env_var in ALL_CI_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setattr("sys.argv", ["pytest", "some_test.py"])
    proceed, reason = controller._check_hardware_write_safety(
        "FORCE_CHARGE", "127.0.0.1", 502, test_mode=False
    )
    assert proceed is False
    assert reason == "pytest_command_detected"


def test_passes_when_every_safety_net_is_bypassed(monkeypatch):
    """Only reachable by explicitly overriding the test-detection itself, as here."""
    monkeypatch.setattr(controller, "_is_actively_testing", lambda: False)
    for env_var in ALL_CI_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setattr("sys.argv", ["not_pytest_at_all"])
    proceed, reason = controller._check_hardware_write_safety(
        "FORCE_CHARGE", "127.0.0.1", 502, test_mode=False
    )
    assert proceed is True
    assert reason == "safety_checks_passed"
