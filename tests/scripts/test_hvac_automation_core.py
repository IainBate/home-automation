"""Tests for scripts/hvac_automation_core.py.

Covers schedule loading/resolution, state (de)serialization, the config
error gate, and the read-decide-apply-persist cycle including plan doc
§8.7's mode-change retry/revert - mocking at the airstage_client/
resideo_client boundary, never a real network call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import hvac_automation_core as core
import pytest
import yaml

_BASE_CONFIG = {
    "location": {"default_timezone_str": "Europe/London"},
    "airstage": {
        "enabled": True,
        "zones": [
            {"name": "Playroom", "ip_address": "1.2.3.4", "device_id": "AA"},
            {"name": "Landing", "ip_address": "1.2.3.5", "device_id": "BB"},
        ],
    },
    "resideo": {"enabled": True},
    "hvac_automation": {"master_zone": "Playroom", "mirror_zone": "Landing"},
}


def _statuses(
    playroom_mode="HEAT",
    landing_mode="HEAT",
    playroom_on=True,
    landing_on=True,
    playroom_target=18.0,
):
    return [
        {
            "name": "Playroom",
            "available": True,
            "mode": playroom_mode,
            "powered_on": playroom_on,
            "current_temperature_c": 17.0,
            "target_temperature_c": playroom_target,
            "outdoor_temperature_c": 5.0,
        },
        {
            "name": "Landing",
            "available": True,
            "mode": landing_mode,
            "powered_on": landing_on,
            "current_temperature_c": 17.0,
            "target_temperature_c": 18.0,
            "outdoor_temperature_c": 5.0,
        },
    ]


def _write_schedule(tmp_path: Path) -> Path:
    schedule_path = tmp_path / "schedule.yaml"
    schedule_path.write_text(
        yaml.safe_dump(
            {
                "schedules": {
                    "at_home_all_day": [
                        {
                            "start": "00:00",
                            "end": "24:00",
                            "heat_target_c": 18.0,
                            "cool_target_c": 20.0,
                        }
                    ]
                },
                "day_assignments": {},
            }
        ),
        encoding="utf-8",
    )
    return schedule_path


# ---------------------------------------------------------------------------
# Schedule loading / resolution
# ---------------------------------------------------------------------------


def test_load_schedule_raises_on_missing_schedules_key(tmp_path):
    path = tmp_path / "schedule.yaml"
    path.write_text(yaml.safe_dump({"day_assignments": {}}), encoding="utf-8")

    with pytest.raises(ValueError, match="no 'schedules' defined"):
        core.load_schedule(str(path))


def test_load_schedule_normalises_periods(tmp_path):
    path = tmp_path / "schedule.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schedules": {
                    "at_home_all_day": [
                        {
                            "start": "06:00",
                            "end": "22:00",
                            "heat_target_c": 17.0,
                            "cool_target_c": 19.0,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    schedules, day_assignments = core.load_schedule(str(path))

    # Rule 1: first period forced to start at 00:00.
    assert schedules["at_home_all_day"][0].start_minute == 0
    assert day_assignments == {}


def test_get_house_targets_uses_day_assignment():
    from src.core_logic.hvac_schedule_logic import SchedulePeriod

    schedules = {
        "at_home_all_day": [SchedulePeriod(0, 1440, 18.0, 20.0)],
        "at_home_part_of_day": [SchedulePeriod(0, 1440, 16.0, 22.0)],
    }
    day_assignments = {"friday": "at_home_part_of_day"}

    monday = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)  # Monday
    friday = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)  # Friday

    assert core.get_house_targets(schedules, day_assignments, monday) == (18.0, 20.0)
    assert core.get_house_targets(schedules, day_assignments, friday) == (16.0, 22.0)


def test_get_house_targets_unknown_schedule_name_returns_none_none():
    from src.core_logic.hvac_schedule_logic import SchedulePeriod

    schedules = {"at_home_all_day": [SchedulePeriod(0, 1440, 18.0, 20.0)]}
    day_assignments = {"monday": "does_not_exist"}
    monday = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)

    assert core.get_house_targets(schedules, day_assignments, monday) == (None, None)


# ---------------------------------------------------------------------------
# State (de)serialization
# ---------------------------------------------------------------------------


def test_hvac_state_roundtrip_through_dict():
    from src.core_logic.hvac_decision_logic import HvacState

    now = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    state = HvacState(
        hvac_target_c=19.5,
        heat_target_c=18.0,
        cool_target_c=20.0,
        below_heat_target_since=now,
        last_observed_mode="heat",
        away_active=True,
    )

    raw = core._hvac_state_to_dict(state)
    restored = core._hvac_state_from_dict(raw)

    assert restored == state


def test_hvac_state_from_dict_tolerates_malformed_timestamp():
    restored = core._hvac_state_from_dict({"below_heat_target_since": "not-a-timestamp"})
    assert restored.below_heat_target_since is None


def test_hvac_state_from_dict_empty_dict_gives_defaults():
    from src.core_logic.hvac_decision_logic import HvacState

    assert core._hvac_state_from_dict({}) == HvacState()


# ---------------------------------------------------------------------------
# is_away_mode_active / get_hvac_automation_config_error
# ---------------------------------------------------------------------------


def test_is_away_mode_active_false_when_absent():
    assert core.is_away_mode_active({}) is False


def test_is_away_mode_active_true_when_set():
    assert core.is_away_mode_active({"away_mode": {"active": True}}) is True


def test_get_hvac_automation_config_error_none_when_airstage_and_resideo_enabled():
    assert core.get_hvac_automation_config_error(_BASE_CONFIG) is None


def test_get_hvac_automation_config_error_when_airstage_disabled():
    config = {**_BASE_CONFIG, "airstage": {"enabled": False}}
    error = core.get_hvac_automation_config_error(config)
    assert error is not None
    assert "airstage.enabled" in error


def test_get_hvac_automation_config_error_when_resideo_disabled():
    config = {**_BASE_CONFIG, "resideo": {"enabled": False}}
    error = core.get_hvac_automation_config_error(config)
    assert error is not None
    assert "resideo.enabled" in error


# ---------------------------------------------------------------------------
# run_hvac_decision_check - end to end with mocked airstage_client
# ---------------------------------------------------------------------------


def test_run_hvac_decision_check_unavailable_zone_makes_no_changes(tmp_path):
    schedule_path = _write_schedule(tmp_path)
    state_path = tmp_path / "state.json"

    with mock.patch.object(
        core, "get_hvac_automation_state_path", lambda: str(state_path)
    ), mock.patch.object(
        core, "get_schedule_path", lambda: str(schedule_path)
    ), mock.patch.object(
        core, "fetch_airstage_status", return_value=[{"name": "Playroom", "available": False}]
    ):
        rc = core.run_hvac_decision_check(
            _BASE_CONFIG, _BASE_CONFIG["hvac_automation"], room_temperature_c=17.0
        )

    assert rc == 0
    assert not state_path.exists() or json.loads(state_path.read_text()) == {}


def test_run_hvac_decision_check_first_run_propagates_schedule_target(tmp_path):
    schedule_path = _write_schedule(tmp_path)
    state_path = tmp_path / "state.json"

    with mock.patch.object(
        core, "get_hvac_automation_state_path", lambda: str(state_path)
    ), mock.patch.object(
        core, "get_schedule_path", lambda: str(schedule_path)
    ), mock.patch.object(
        core, "fetch_airstage_status", return_value=_statuses()
    ), mock.patch.object(
        core, "set_airstage_temperature", return_value={"Playroom": True, "Landing": True}
    ) as set_temp:
        rc = core.run_hvac_decision_check(
            _BASE_CONFIG, _BASE_CONFIG["hvac_automation"], room_temperature_c=17.0
        )

    assert rc == 0
    assert set_temp.call_count == 2  # Playroom + Landing, per the schedule-propagation decision
    final_state = json.loads(state_path.read_text())
    assert final_state["hvac"]["hvac_target_c"] == 18.0


def test_run_hvac_decision_check_dry_run_makes_no_write_calls(tmp_path):
    schedule_path = _write_schedule(tmp_path)
    state_path = tmp_path / "state.json"

    with mock.patch.object(
        core, "get_hvac_automation_state_path", lambda: str(state_path)
    ), mock.patch.object(
        core, "get_schedule_path", lambda: str(schedule_path)
    ), mock.patch.object(
        core, "fetch_airstage_status", return_value=_statuses()
    ), mock.patch.object(core, "set_airstage_temperature") as set_temp, mock.patch.object(
        core, "set_airstage_mode"
    ) as set_mode:
        rc = core.run_hvac_decision_check(
            _BASE_CONFIG, _BASE_CONFIG["hvac_automation"], room_temperature_c=17.0, dry_run=True
        )

    assert rc == 0
    set_temp.assert_not_called()
    set_mode.assert_not_called()
    # Dry run still persists the decided state, matching this module's
    # "state reflects the decision, not the write outcome" design.
    final_state = json.loads(state_path.read_text())
    assert final_state["hvac"]["hvac_target_c"] == 18.0


def test_run_hvac_decision_check_reports_failure_when_write_never_verifies(tmp_path):
    schedule_path = _write_schedule(tmp_path)
    state_path = tmp_path / "state.json"

    with mock.patch.object(
        core, "get_hvac_automation_state_path", lambda: str(state_path)
    ), mock.patch.object(
        core, "get_schedule_path", lambda: str(schedule_path)
    ), mock.patch.object(
        core, "fetch_airstage_status", return_value=_statuses()
    ), mock.patch.object(
        core, "set_airstage_temperature", return_value={"Playroom": False, "Landing": True}
    ):
        rc = core.run_hvac_decision_check(
            _BASE_CONFIG, _BASE_CONFIG["hvac_automation"], room_temperature_c=17.0
        )

    assert rc == 1


def test_run_hvac_decision_check_mode_divergence_corrected_immediately(tmp_path):
    """§8.7: units disagreeing on mode are corrected on the very next check,
    with no dwell timer or prior state needed."""
    schedule_path = _write_schedule(tmp_path)
    state_path = tmp_path / "state.json"
    # Seed state so schedule propagation doesn't also fire this tick.
    state_path.write_text(
        json.dumps(
            {"hvac": {"hvac_target_c": 18.0, "heat_target_c": 18.0, "cool_target_c": 20.0}}
        ),
        encoding="utf-8",
    )

    statuses = _statuses(playroom_mode="HEAT", landing_mode="COOL")

    with mock.patch.object(
        core, "get_hvac_automation_state_path", lambda: str(state_path)
    ), mock.patch.object(
        core, "get_schedule_path", lambda: str(schedule_path)
    ), mock.patch.object(
        core, "fetch_airstage_status", return_value=statuses
    ), mock.patch.object(
        core, "set_airstage_mode", return_value={"Playroom": True, "Landing": True}
    ) as set_mode:
        rc = core.run_hvac_decision_check(
            _BASE_CONFIG, _BASE_CONFIG["hvac_automation"], room_temperature_c=17.0
        )

    assert rc == 0
    set_mode.assert_called_once_with(_BASE_CONFIG, "heat")


# ---------------------------------------------------------------------------
# _apply_mode_change - §8.7 retry/revert
# ---------------------------------------------------------------------------


def test_apply_mode_change_succeeds_immediately():
    with mock.patch.object(
        core, "set_airstage_mode", return_value={"Playroom": True, "Landing": True}
    ) as set_mode:
        ok = core._apply_mode_change({}, "heat", "dry", quiet=True)

    assert ok is True
    assert set_mode.call_count == 1


def test_apply_mode_change_retries_and_succeeds():
    results = [{"Playroom": True, "Landing": False}, {"Playroom": True, "Landing": True}]
    with mock.patch.object(core, "set_airstage_mode", side_effect=results) as set_mode:
        ok = core._apply_mode_change({}, "heat", "dry", quiet=True)

    assert ok is True
    assert set_mode.call_count == 2


def test_apply_mode_change_reverts_after_retry_still_fails():
    def fake_set_mode(_config, mode):
        if mode == "heat":
            return {"Playroom": True, "Landing": False}
        return {"Playroom": True, "Landing": True}  # revert to "dry" succeeds

    with mock.patch.object(core, "set_airstage_mode", side_effect=fake_set_mode) as set_mode:
        ok = core._apply_mode_change({}, "heat", "dry", quiet=True)

    assert ok is False
    assert set_mode.call_args_list == [
        mock.call({}, "heat"),
        mock.call({}, "heat"),
        mock.call({}, "dry"),
    ]


def test_apply_mode_change_logs_when_revert_itself_fails(caplog):
    with mock.patch.object(
        core, "set_airstage_mode", return_value={"Playroom": True, "Landing": False}
    ):
        with caplog.at_level("ERROR"):
            ok = core._apply_mode_change({}, "heat", "dry", quiet=True)

    assert ok is False
    assert any("ALSO failed" in message for message in caplog.messages)
