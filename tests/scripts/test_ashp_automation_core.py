"""Tests for scripts/ashp_automation_core.py - orchestration and the
'No Double Control' branch point (delegating to hvac_automation_core vs
owning the HVAC units directly).
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

import ashp_automation_core as core  # noqa: E402
import hvac_automation_core  # noqa: E402


class _FrozenDateTime(datetime):
    """Subclass, not a bare Mock, so classmethods like fromisoformat still work -
    matches tests/scripts/test_battery_mode_daemon_decisions.py's own idiom.
    """

    _frozen_now: datetime

    @classmethod
    def now(cls, tz=None):
        return cls._frozen_now if tz is None else cls._frozen_now.astimezone(tz)


def _write_state(tmp_path: Path, state: dict) -> Path:
    state_path = tmp_path / "hvac_automation_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return state_path


def _master_status(**overrides):
    base = {
        "name": "Playroom",
        "available": True,
        "mode": "dry",
        "powered_on": True,
        "target_temperature_c": 25.0,
        "outdoor_temperature_c": 5.0,
    }
    base.update(overrides)
    return base


def _mirror_status(**overrides):
    base = {"name": "Landing", "available": True, "mode": "dry", "powered_on": True}
    base.update(overrides)
    return base


def _patch_common(
    tmp_path,
    *,
    state,
    room_temp=17.0,
    house_target=20.0,
    statuses=None,
    forecast_records=None,
):
    state_path = _write_state(tmp_path, state)
    if statuses is None:
        statuses = [_master_status(), _mirror_status()]
    if forecast_records is None:
        forecast_records = [{"temperature_2m": 5.0}, {"temperature_2m": 6.0}]

    return (
        mock.patch.object(
            hvac_automation_core, "get_hvac_automation_state_path", lambda: str(state_path)
        ),
        mock.patch.object(core, "read_room_temperature_c", lambda cfg: room_temp),
        mock.patch.object(core, "load_schedule", lambda *a, **k: ({"sched": []}, {})),
        mock.patch.object(core, "get_house_targets", lambda *a, **k: (house_target, house_target + 4)),
        mock.patch.object(core, "fetch_airstage_status", lambda cfg: statuses),
        mock.patch.object(core, "fetch_forecast_weather_hourly", lambda *a, **k: forecast_records),
    )


def _config():
    return {
        "location": {"default_timezone_str": "UTC", "latitude": 53.0, "longitude": -1.0},
        "ashp": {"day_start_time": "06:00", "night_start_time": "22:00"},
        "hvac_automation": {"master_zone": "Playroom", "mirror_zone": "Landing"},
    }


def test_activates_ashp_and_suppresses_hvac_automation(tmp_path):
    config = _config()
    patches = _patch_common(
        tmp_path,
        state={"ashp": {"below_target_since": (datetime.now(UTC) - timedelta(hours=3)).isoformat()}},
    )
    with mock.patch.object(core, "set_ashp_heat_call", return_value=True) as fake_heat_call, mock.patch.object(
        core, "set_airstage_power", return_value={"Playroom": True, "Landing": True}
    ) as fake_power, mock.patch.object(core, "_run_hvac_decision_check") as fake_hvac_check:
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    fake_heat_call.assert_called_once()
    fake_power.assert_called_once_with(config, False)  # day period -> HVAC off
    fake_hvac_check.assert_not_called()  # No Double Control


def test_ashp_off_delegates_to_hvac_automation(tmp_path):
    config = _config()
    patches = _patch_common(tmp_path, state={}, room_temp=21.0)  # room above target, never triggers

    with mock.patch.object(core, "set_ashp_off", return_value=True) as fake_off, mock.patch.object(
        core, "_run_hvac_decision_check", return_value=0
    ) as fake_hvac_check:
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    fake_off.assert_called_once()
    fake_hvac_check.assert_called_once()


def test_deactivation_powers_hvac_back_on(tmp_path):
    config = _config()
    now = datetime.now(UTC)
    state = {
        "ashp": {
            "ashp_active": True,
            "activated_at": (now - timedelta(hours=7)).isoformat(),
            "activation_baseline_outdoor_c": 5.0,
        }
    }
    patches = _patch_common(
        tmp_path,
        state=state,
        statuses=[_master_status(outdoor_temperature_c=8.0), _mirror_status()],
        forecast_records=[{"temperature_2m": 9.0}, {"temperature_2m": 10.0}],
    )
    with mock.patch.object(core, "set_ashp_off", return_value=True), mock.patch.object(
        core, "set_airstage_power", return_value={"Playroom": True, "Landing": True}
    ) as fake_power, mock.patch.object(core, "_run_hvac_decision_check", return_value=0):
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    fake_power.assert_called_once_with(config, True)


def test_night_period_sets_fixed_hvac_targets_not_normal_schedule(tmp_path):
    config = _config()
    now = datetime.now(UTC)
    state = {
        "ashp": {
            "ashp_active": True,
            "activated_at": (now - timedelta(hours=1)).isoformat(),
            "activation_baseline_outdoor_c": 5.0,
        }
    }
    patches = _patch_common(tmp_path, state=state)

    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = now.replace(hour=23, minute=0, second=0, microsecond=0)
    with mock.patch.object(core, "datetime", frozen), mock.patch.object(
        core, "set_ashp_heat_call", return_value=True
    ), mock.patch.object(
        core, "set_airstage_temperature", return_value={"Landing": True, "Playroom": True}
    ) as fake_temp, mock.patch.object(core, "_run_hvac_decision_check") as fake_hvac_check:
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    assert fake_temp.call_count == 2
    fake_hvac_check.assert_not_called()


def test_dry_run_applies_nothing(tmp_path):
    config = _config()
    patches = _patch_common(
        tmp_path,
        state={"ashp": {"below_target_since": (datetime.now(UTC) - timedelta(hours=3)).isoformat()}},
    )
    with mock.patch.object(core, "set_ashp_heat_call") as fake_heat, mock.patch.object(
        core, "set_airstage_power"
    ) as fake_power, mock.patch.object(core, "_run_hvac_decision_check") as fake_hvac_check:
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(
                config, config["ashp"], config["hvac_automation"], dry_run=True, quiet=True
            )
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    fake_heat.assert_not_called()
    fake_power.assert_not_called()
    fake_hvac_check.assert_not_called()


def test_state_persists_across_calls(tmp_path):
    config = _config()
    state_path = _write_state(tmp_path, {})
    statuses = [_master_status(), _mirror_status()]

    with mock.patch.object(
        hvac_automation_core, "get_hvac_automation_state_path", lambda: str(state_path)
    ), mock.patch.object(core, "read_room_temperature_c", lambda cfg: 17.0), mock.patch.object(
        core, "load_schedule", lambda *a, **k: ({"sched": []}, {})
    ), mock.patch.object(
        core, "get_house_targets", lambda *a, **k: (20.0, 24.0)
    ), mock.patch.object(
        core, "fetch_airstage_status", lambda cfg: statuses
    ), mock.patch.object(
        core, "fetch_forecast_weather_hourly", lambda *a, **k: [{"temperature_2m": 5.0}]
    ), mock.patch.object(
        core, "set_ashp_off", return_value=True
    ), mock.patch.object(
        core, "_run_hvac_decision_check", return_value=0
    ):
        core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)

    saved = json.loads(state_path.read_text())
    assert "ashp" in saved
    assert saved["ashp"]["below_target_since"] is not None


# --- Interference detection (docs/ASHP.md §6) --------------------------------


def _active_ashp_state(now, **overrides):
    base = {
        "ashp_active": True,
        "activated_at": (now - timedelta(hours=1)).isoformat(),
        "activation_baseline_outdoor_c": 5.0,
    }
    base.update(overrides)
    return base


def test_repeated_command_matching_observed_state_needs_no_interference_warning(tmp_path):
    """The common case: we re-assert the same target every cycle, and the T6R
    still shows it - no divergence, no warning, reassert_count stays 0."""
    config = _config()
    now = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)  # day period (06:00-22:00)
    state = {
        "ashp": _active_ashp_state(now),
        "ashp_interference": {"commanded_value": "heat@18.0", "commanded_at": now.isoformat()},
    }
    patches = _patch_common(tmp_path, state=state)

    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = now
    state_path = tmp_path / "hvac_automation_state.json"

    with mock.patch.object(core, "datetime", frozen), mock.patch.object(
        core, "set_ashp_heat_call", return_value=True
    ), mock.patch.object(core, "set_airstage_power", return_value={"Playroom": True, "Landing": True}), \
         mock.patch.object(
        core, "fetch_resideo_status", return_value={"mode": "heat", "target_temperature_c": 18.0}
    ) as fake_fetch, mock.patch.object(core, "logger") as fake_logger:
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    fake_fetch.assert_called_once()
    fake_logger.warning.assert_not_called()
    saved = json.loads(state_path.read_text())
    assert saved["ashp_interference"]["reassert_count"] == 0
    assert saved["ashp_interference"]["diverged_since"] is None


def test_repeated_divergence_accumulates_reassert_count(tmp_path):
    """T6R keeps showing a different value than commanded, despite our repeated
    re-assertion - reassert_count should climb rather than reset."""
    config = _config()
    now = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)  # day period (06:00-22:00)
    state = {
        "ashp": _active_ashp_state(now),
        "ashp_interference": {
            "commanded_value": "heat@18.0",
            "commanded_at": (now - timedelta(minutes=30)).isoformat(),
            "diverged_since": (now - timedelta(minutes=25)).isoformat(),
            "diverged_to": "heat@21.0",
            "reassert_count": 1,
        },
    }
    patches = _patch_common(tmp_path, state=state)

    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = now
    state_path = tmp_path / "hvac_automation_state.json"

    with mock.patch.object(core, "datetime", frozen), mock.patch.object(
        core, "set_ashp_heat_call", return_value=True
    ), mock.patch.object(core, "set_airstage_power", return_value={"Playroom": True, "Landing": True}), \
         mock.patch.object(
        core, "fetch_resideo_status", return_value={"mode": "heat", "target_temperature_c": 21.0}
    ), mock.patch.object(core, "logger") as fake_logger:
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    saved = json.loads(state_path.read_text())
    interference = saved["ashp_interference"]
    assert interference["reassert_count"] == 2
    assert interference["diverged_to"] == "heat@21.0"
    # 25 min diverged + reassert_count>=1 -> past the default 30min dwell? Not
    # yet (25 < 30 default) - this call itself pushes it to 2 reasserts but
    # dwell_minutes is checked against diverged_since, still short of 30min,
    # so no warning THIS cycle - confirms the dwell gate, not just the count.
    fake_logger.warning.assert_not_called()


def test_sustained_interference_logs_a_warning(tmp_path):
    """Both the dwell time AND reassert count satisfied -> flagged, matching
    interference_logic.evaluate's own external_override_suspected contract.
    """
    config = _config()
    config["ashp"]["interference_dwell_minutes"] = 10
    config["ashp"]["interference_min_reasserts"] = 1
    now = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)  # day period (06:00-22:00)
    state = {
        "ashp": _active_ashp_state(now),
        "ashp_interference": {
            "commanded_value": "heat@18.0",
            "commanded_at": (now - timedelta(minutes=30)).isoformat(),
            "diverged_since": (now - timedelta(minutes=20)).isoformat(),
            "diverged_to": "heat@21.0",
            "reassert_count": 1,
        },
    }
    patches = _patch_common(tmp_path, state=state)

    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = now
    state_path = tmp_path / "hvac_automation_state.json"

    with mock.patch.object(core, "datetime", frozen), mock.patch.object(
        core, "set_ashp_heat_call", return_value=True
    ), mock.patch.object(core, "set_airstage_power", return_value={"Playroom": True, "Landing": True}), \
         mock.patch.object(
        core, "fetch_resideo_status", return_value={"mode": "heat", "target_temperature_c": 21.0}
    ), mock.patch.object(core, "logger") as fake_logger:
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    fake_logger.warning.assert_called_once()
    warning_args = fake_logger.warning.call_args[0]
    assert "heat@21.0" in str(warning_args)
    # The write still happens regardless - automation keeps re-asserting, per
    # the efficiency-not-safety framing (docs/ASHP.md §6).
    saved = json.loads(state_path.read_text())
    assert saved["ashp"]["ashp_active"] is True


def test_fresh_command_skips_interference_check_entirely(tmp_path):
    """A genuinely new command (e.g. day->night transition) doesn't compare
    against stale divergence history - and shouldn't even call
    fetch_resideo_status, since is_fresh_command short-circuits first."""
    config = _config()
    now = datetime(2026, 1, 15, 23, 0, 0, tzinfo=UTC)  # night period (22:00-06:00)
    state = {
        "ashp": _active_ashp_state(now),
        "ashp_interference": {
            "commanded_value": "heat@18.0",  # day target - about to change to night target
            "commanded_at": (now - timedelta(hours=1)).isoformat(),
        },
    }
    patches = _patch_common(tmp_path, state=state)

    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = now
    state_path = tmp_path / "hvac_automation_state.json"

    with mock.patch.object(core, "datetime", frozen), mock.patch.object(
        core, "set_ashp_heat_call", return_value=True
    ), mock.patch.object(
        core, "set_airstage_temperature", return_value={"Landing": True, "Playroom": True}
    ), mock.patch.object(core, "fetch_resideo_status") as fake_fetch:
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    fake_fetch.assert_not_called()
    saved = json.loads(state_path.read_text())
    assert saved["ashp_interference"]["commanded_value"] == "heat@14.0"  # night target now
    assert saved["ashp_interference"]["reassert_count"] == 0


# --- ASHP/MELCloud response corroboration -----------------------------------


def test_response_check_settles_on_first_activation_tick(tmp_path):
    config = _config()
    now = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    state = {"ashp": _active_ashp_state(now)}  # no ashp_response_check yet - first tick
    patches = _patch_common(tmp_path, state=state)
    state_path = tmp_path / "hvac_automation_state.json"

    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = now

    with (
        mock.patch.object(core, "datetime", frozen),
        mock.patch.object(core, "set_ashp_heat_call", return_value=True),
        mock.patch.object(core, "set_airstage_power", return_value={"Playroom": True, "Landing": True}),
        mock.patch.object(core, "fetch_resideo_status", return_value={"mode": "heat", "target_temperature_c": 18.0}),
        mock.patch.object(core, "read_fresh_status", return_value={"status": "idle"}),
        mock.patch.object(core, "logger") as fake_logger,
    ):
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    fake_logger.warning.assert_not_called()
    saved = json.loads(state_path.read_text())
    assert saved["ashp_response_check"]["active_since"] == now.isoformat()


def test_response_check_flags_sustained_non_response(tmp_path):
    config = _config()
    config["ashp"]["response_window_minutes"] = 20.0
    now = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    state = {
        "ashp": _active_ashp_state(now),
        "ashp_response_check": {"active_since": (now - timedelta(minutes=25)).isoformat()},
    }
    patches = _patch_common(tmp_path, state=state)

    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = now

    with (
        mock.patch.object(core, "datetime", frozen),
        mock.patch.object(core, "set_ashp_heat_call", return_value=True),
        mock.patch.object(core, "set_airstage_power", return_value={"Playroom": True, "Landing": True}),
        mock.patch.object(core, "fetch_resideo_status", return_value={"mode": "heat", "target_temperature_c": 18.0}),
        mock.patch.object(core, "read_fresh_status", return_value={"status": "idle"}),
        mock.patch.object(core, "logger") as fake_logger,
    ):
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    fake_logger.warning.assert_called_once()
    assert "no_response_suspected" not in str(fake_logger.warning.call_args)  # human reason, not the raw status code
    assert "25" in str(fake_logger.warning.call_args)


def test_response_check_does_not_flag_when_busy_heating_the_tank(tmp_path):
    config = _config()
    config["ashp"]["response_window_minutes"] = 20.0
    now = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    state = {
        "ashp": _active_ashp_state(now),
        "ashp_response_check": {"active_since": (now - timedelta(minutes=25)).isoformat()},
    }
    patches = _patch_common(tmp_path, state=state)

    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = now

    with (
        mock.patch.object(core, "datetime", frozen),
        mock.patch.object(core, "set_ashp_heat_call", return_value=True),
        mock.patch.object(core, "set_airstage_power", return_value={"Playroom": True, "Landing": True}),
        mock.patch.object(core, "fetch_resideo_status", return_value={"mode": "heat", "target_temperature_c": 18.0}),
        mock.patch.object(core, "read_fresh_status", return_value={"status": "heat_water"}),
        mock.patch.object(core, "logger") as fake_logger,
    ):
        for p in patches:
            p.start()
        try:
            core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    fake_logger.warning.assert_not_called()


def test_response_check_deactivates_cleanly_when_ashp_turns_off(tmp_path):
    """Same deactivation setup as test_deactivation_powers_hvac_back_on (outdoor
    risen 5.0C -> 8.0C, past deactivation_margin_c=2.0, forecast staying above
    baseline too) - confirms the response-check clock resets to None the
    moment ashp_active goes False, not left dangling from the prior activation."""
    config = _config()
    now = datetime.now(UTC)
    state = {
        "ashp": {
            "ashp_active": True,
            "activated_at": (now - timedelta(hours=7)).isoformat(),
            "activation_baseline_outdoor_c": 5.0,
        },
        "ashp_response_check": {"active_since": (now - timedelta(hours=7)).isoformat()},
    }
    patches = _patch_common(
        tmp_path,
        state=state,
        statuses=[_master_status(outdoor_temperature_c=8.0), _mirror_status()],
        forecast_records=[{"temperature_2m": 9.0}, {"temperature_2m": 10.0}],
    )
    state_path = tmp_path / "hvac_automation_state.json"

    with (
        mock.patch.object(core, "set_ashp_off", return_value=True),
        mock.patch.object(core, "set_airstage_power", return_value={"Playroom": True, "Landing": True}),
        mock.patch.object(core, "_run_hvac_decision_check", return_value=0),
        mock.patch.object(core, "read_fresh_status") as fake_read_fresh_status,
        mock.patch.object(core, "logger") as fake_logger,
    ):
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    fake_logger.warning.assert_not_called()
    fake_read_fresh_status.assert_not_called()
    saved = json.loads(state_path.read_text())
    assert saved["ashp_response_check"]["active_since"] is None


def test_response_check_logs_debug_on_unknown_verdict(tmp_path):
    """When ashp_active but MELCloud status can't be freshly read (e.g. hot
    water automation disabled), the check should still leave a debug trail -
    otherwise it looks indistinguishable from not running at all."""
    config = _config()
    config["ashp"]["response_window_minutes"] = 20.0
    now = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    state = {
        "ashp": _active_ashp_state(now),
        "ashp_response_check": {"active_since": (now - timedelta(minutes=25)).isoformat()},
    }
    patches = _patch_common(tmp_path, state=state)

    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = now

    with (
        mock.patch.object(core, "datetime", frozen),
        mock.patch.object(core, "set_ashp_heat_call", return_value=True),
        mock.patch.object(core, "set_airstage_power", return_value={"Playroom": True, "Landing": True}),
        mock.patch.object(core, "fetch_resideo_status", return_value={"mode": "heat", "target_temperature_c": 18.0}),
        mock.patch.object(core, "read_fresh_status", return_value=None),
        mock.patch.object(core, "logger") as fake_logger,
    ):
        for p in patches:
            p.start()
        try:
            rc = core.run_ashp_decision_check(config, config["ashp"], config["hvac_automation"], quiet=True)
        finally:
            for p in patches:
                p.stop()

    assert rc == 0
    fake_logger.warning.assert_not_called()
    fake_logger.debug.assert_called_once()
