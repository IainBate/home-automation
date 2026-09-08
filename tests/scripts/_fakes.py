"""Shared test doubles for hotwater_automation_core's MELCloud-facing checks.

Consolidates what used to be ten near-identical FakeMelCloudClient definitions
(one per test file) into one. Every constructor field is optional and every
existing call site only ever specified a subset of them, so this is a
drop-in replacement - see the individual check functions in
hotwater_automation_core.py for confirmation that none of them reads
target_tank_temperature/target_tank_temperature_max/operation_mode from a
status dict that a given test's original fake never included.
"""

from __future__ import annotations

from typing import Any

from src.api_clients.melcloud_client import HotWaterOperationMode


class FakeMelCloudClient:
    """Stand-in for MelCloudClient - records calls instead of touching MELCloud."""

    def __init__(
        self,
        *,
        tank_temp: float | None = 30.0,
        target_temp: float | None = 45.0,
        max_temp: float | None = 65.0,
        operation_mode: HotWaterOperationMode = HotWaterOperationMode.AUTO,
    ) -> None:
        self.tank_temp = tank_temp
        self.target_temp = target_temp
        self.max_temp = max_temp
        self.operation_mode = operation_mode
        self.force_calls: list[bool] = []
        self.target_temp_calls: list[float] = []

    async def connect(self) -> None:
        return None

    async def get_tank_status(self) -> dict[str, Any]:
        return {
            "tank_temperature": self.tank_temp,
            "target_tank_temperature": self.target_temp,
            "target_tank_temperature_max": self.max_temp,
            "operation_mode": self.operation_mode,
        }

    async def set_force_hot_water(self, *, enabled: bool) -> bool:
        self.force_calls.append(enabled)
        return True

    async def set_target_tank_temperature(self, temp: float) -> None:
        self.target_temp_calls.append(temp)
        self.target_temp = temp

    async def close(self) -> None:
        return None
