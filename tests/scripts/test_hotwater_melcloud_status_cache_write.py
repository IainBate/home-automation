"""The force-heat check must write the shared MELCloud status cache.

scripts/hotwater_automation_core.py's force-heat check already fetches tank
status every hotwater_automation.poll_interval_seconds (10 min default) for
its own decision - writing that same fetch to melcloud_status_cache.py is a
free byproduct, not a new API call, and is the one thing that keeps the
dashboard's cache fresh (see that module's docstring). This pins the write
actually happening, and that a cache-write failure can't take down the real
force-heat decision.
"""

from __future__ import annotations

import asyncio
import json
from unittest import mock

import hotwater_automation_core as core


class _FakeMelCloudClient:
    async def connect(self) -> None:
        return None

    async def get_tank_status(self) -> dict:
        return {
            "tank_temperature": 47.0,
            "target_tank_temperature": 50.0,
            "operation_mode": core.HotWaterOperationMode.AUTO,
        }

    async def set_force_hot_water(self, *, enabled: bool) -> bool:  # noqa: ARG002
        return True

    async def set_target_tank_temperature(self, temp: float) -> None:  # noqa: ARG002
        return None

    async def close(self) -> None:
        return None


def _run(tmp_path, monkeypatch, *, write_side_effect=None):
    state_path = tmp_path / "hotwater_automation_state.json"
    state_path.write_text(json.dumps({}), encoding="utf-8")
    hw_config = {
        "tank_temp_threshold_c": 45.0,
        "trigger_hour": 21.5,
        "battery_soc_min_percent": 50.0,
    }
    config = {"location": {"default_timezone_str": "UTC"}}

    write_mock = mock.Mock(side_effect=write_side_effect)
    with (
        mock.patch.object(core, "get_hotwater_automation_state_path", lambda: str(state_path)),
        mock.patch.object(core, "MelCloudClient", lambda config_path=None: _FakeMelCloudClient()),
        mock.patch.object(core, "write_melcloud_status_cache", write_mock),
        mock.patch.object(
            core, "_get_ohme_charging_power_watts", mock.AsyncMock(return_value=None)
        ),
        mock.patch.object(core, "get_battery_soc_percent", lambda cfg: None),
    ):
        exit_code = asyncio.run(
            core.run_force_heat_check(config, hw_config, dry_run=True, quiet=True)
        )

    return exit_code, write_mock


def test_force_heat_check_writes_the_melcloud_status_cache(tmp_path, monkeypatch):
    exit_code, write_mock = _run(tmp_path, monkeypatch)

    assert exit_code == 0
    write_mock.assert_called_once()
    (written_status,), _kwargs = write_mock.call_args
    assert written_status["tank_temperature"] == 47.0


def test_force_heat_check_survives_a_cache_write_failure(tmp_path, monkeypatch):
    """A broken cache write must not break the real force-heat decision -
    Circuit Breaker convention (see CLAUDE.md): this is a display-only
    side effect, not load-bearing for the automation itself.
    """
    exit_code, write_mock = _run(tmp_path, monkeypatch, write_side_effect=OSError("disk full"))

    assert exit_code == 0
    write_mock.assert_called_once()
