"""Regression tests for _run_force_heat_check_locked's dangling-
force_heat_activated_at cleanup (see its own comment in
hotwater_automation_core.py).

Real incident (2026-09-07): a manually-activated force-heat's
force_heat_activated_at was popped by this cleanup within ~45 seconds of
being set, because the very next force-heat tick's live MELCloud read landed
inside MELCloud's normal request-then-verify propagation delay and still
read "auto" rather than "force_hot_water" - ordinary, expected delay, not a
genuinely dangling marker. That missing timestamp then made
run_safety_ceiling_check's own "can't tell how long this has been running"
fail-safe treat a freshly (and correctly) started heat as an unbounded
violation and revert it - the exact "two safety mechanisms fighting each
other" failure this whole design exists to avoid.

The fix: the cleanup only fires once the marker is older than
MODE_CHANGE_GRACE_SECONDS. These tests use a tank already at/above threshold
so determine_hotwater_decision returns should_force_heat=False and the
function returns right after the cleanup check - isolating exactly that
behaviour without a subsequent fresh activation overwriting the marker.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import hotwater_automation_core as core


class FakeMelCloudClient:
    def __init__(self, *, tank_temp: float, operation_mode) -> None:
        self.tank_temp = tank_temp
        self.operation_mode = operation_mode
        self.force_calls: list[bool] = []

    async def connect(self) -> None:
        return None

    async def get_tank_status(self) -> dict:
        return {"tank_temperature": self.tank_temp, "operation_mode": self.operation_mode}

    async def set_force_hot_water(self, *, enabled: bool) -> bool:
        self.force_calls.append(enabled)
        return True

    async def close(self) -> None:
        return None


def _run(tmp_path: Path, state: dict, client: FakeMelCloudClient):
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    hw_config = {"tank_temp_threshold_c": 45.0}
    config = {"location": {"default_timezone_str": "UTC"}}

    with (
        mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)),
        mock.patch.object(core, "MelCloudClient", lambda config_path=None: client),
        mock.patch.object(core, "is_car_charging_confirmed", mock.AsyncMock(return_value=False)),
        mock.patch.object(core, "get_effective_battery_soc_percent", lambda *a, **k: (100.0, "live")),
    ):
        asyncio.run(core.run_force_heat_check(config, hw_config, dry_run=False, quiet=True))

    return json.loads(state_path.read_text())


def test_freshly_activated_marker_survives_a_still_propagating_mode_read(tmp_path):
    """The exact race from the real incident: marker set moments ago, live
    read still shows AUTO (MELCloud hasn't applied the change yet) - must
    NOT be treated as dangling.
    """
    fresh = (datetime.now(tz=UTC) - timedelta(seconds=5)).isoformat()
    client = FakeMelCloudClient(tank_temp=50.0, operation_mode=core.HotWaterOperationMode.AUTO)

    final_state = _run(tmp_path, {"force_heat_activated_at": fresh}, client)

    assert final_state.get("force_heat_activated_at") == fresh
    assert client.force_calls == []  # tank is warm - no new activation either


def test_genuinely_stale_marker_with_mode_not_force_heating_is_cleaned_up(tmp_path):
    """A marker well past the grace period, with the tank confirmed NOT
    force-heating - this is what's left after run_safety_ceiling_check's own
    (never-writes-state) cutoff. Must be cleaned up so it can't feed a false
    "unknown duration" violation on some future tick.
    """
    stale = (datetime.now(tz=UTC) - timedelta(minutes=10)).isoformat()
    client = FakeMelCloudClient(tank_temp=50.0, operation_mode=core.HotWaterOperationMode.AUTO)

    final_state = _run(tmp_path, {"force_heat_activated_at": stale}, client)

    assert "force_heat_activated_at" not in final_state


def test_marker_right_at_the_grace_boundary_is_not_yet_cleaned_up(tmp_path):
    just_inside = (
        datetime.now(tz=UTC) - timedelta(seconds=core.MODE_CHANGE_GRACE_SECONDS - 10)
    ).isoformat()
    client = FakeMelCloudClient(tank_temp=50.0, operation_mode=core.HotWaterOperationMode.AUTO)

    final_state = _run(tmp_path, {"force_heat_activated_at": just_inside}, client)

    assert final_state.get("force_heat_activated_at") == just_inside


def test_marker_is_left_alone_while_actually_force_heating_regardless_of_age(tmp_path):
    """No cleanup at all while mode genuinely IS force-heating - the cleanup
    is specifically about a mismatch between the marker and reality.
    """
    old = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    client = FakeMelCloudClient(
        tank_temp=50.0, operation_mode=core.HotWaterOperationMode.FORCE_HOT_WATER
    )

    final_state = _run(tmp_path, {"force_heat_activated_at": old}, client)

    assert final_state.get("force_heat_activated_at") == old


def test_malformed_marker_is_left_for_revert_checks_own_error_path(tmp_path):
    client = FakeMelCloudClient(tank_temp=50.0, operation_mode=core.HotWaterOperationMode.AUTO)

    final_state = _run(tmp_path, {"force_heat_activated_at": "not-a-timestamp"}, client)

    assert final_state.get("force_heat_activated_at") == "not-a-timestamp"
