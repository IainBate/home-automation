"""Tests for the shared MELCloud hot water tank status cache.

Mirrors tests/api_clients/test_ohme_status_cache.py's shape and the same
property that matters most: a missing, malformed or stale cache must read as
"no cached answer" (None) so callers fall back to their own direct MELCloud
call - never as any particular tank state.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from enum import Enum
from unittest import mock

from src.api_clients import melcloud_status_cache


class _FakeOperationMode(Enum):
    AUTO = "auto"


class _FakeStatus(Enum):
    IDLE = "idle"


def _patch_path(tmp_path):
    return mock.patch.object(
        melcloud_status_cache,
        "get_melcloud_status_path",
        lambda: str(tmp_path / "melcloud_status.json"),
    )


def test_read_fresh_status_returns_none_when_cache_missing(tmp_path):
    with _patch_path(tmp_path):
        assert melcloud_status_cache.read_fresh_status() is None


def test_write_then_read_round_trips_and_serializes_enums(tmp_path):
    status = {
        "tank_temperature": 48.5,
        "target_tank_temperature": 50.0,
        "operation_mode": _FakeOperationMode.AUTO,
        "status": _FakeStatus.IDLE,
        "power": True,
        "holiday_mode": False,
    }

    with _patch_path(tmp_path):
        melcloud_status_cache.write_status_cache(status)
        cached = melcloud_status_cache.read_fresh_status()

    assert cached is not None
    assert cached["tank_temperature_c"] == 48.5
    assert cached["target_tank_temperature_c"] == 50.0
    assert cached["operation_mode"] == "auto"
    assert cached["status"] == "idle"
    assert cached["power_on"] is True
    assert cached["holiday_mode"] is False
    assert "fetched_at" in cached


def test_serialize_status_tolerates_missing_enum_fields():
    """A partial/unexpected status shape must not raise - matches this
    codebase's fail-fast convention for hardware/API field extraction.
    """
    result = melcloud_status_cache.serialize_status({"tank_temperature": 40.0})

    assert result["tank_temperature_c"] == 40.0
    assert result["operation_mode"] is None
    assert result["status"] is None


def test_read_fresh_status_returns_none_when_stale(tmp_path):
    stale = {
        "fetched_at": (datetime.now(tz=UTC) - timedelta(seconds=1000)).isoformat(),
        "tank_temperature_c": 48.5,
    }
    (tmp_path / "melcloud_status.json").write_text(json.dumps(stale), encoding="utf-8")

    with _patch_path(tmp_path):
        assert melcloud_status_cache.read_fresh_status(max_age_seconds=900) is None


def test_read_fresh_status_accepts_a_recent_reading(tmp_path):
    recent = {
        "fetched_at": (datetime.now(tz=UTC) - timedelta(seconds=60)).isoformat(),
        "tank_temperature_c": 48.5,
    }
    (tmp_path / "melcloud_status.json").write_text(json.dumps(recent), encoding="utf-8")

    with _patch_path(tmp_path):
        cached = melcloud_status_cache.read_fresh_status(max_age_seconds=900)

    assert cached is not None
    assert cached["tank_temperature_c"] == 48.5


def test_read_fresh_status_returns_none_for_malformed_timestamps(tmp_path):
    path = tmp_path / "melcloud_status.json"

    for record in (
        {"tank_temperature_c": 48.5},  # no fetched_at at all
        {"fetched_at": "not-a-timestamp", "tank_temperature_c": 48.5},
        {"fetched_at": "2026-09-03 01:00:00", "tank_temperature_c": 48.5},  # naive, no offset
    ):
        path.write_text(json.dumps(record), encoding="utf-8")
        with _patch_path(tmp_path):
            assert melcloud_status_cache.read_fresh_status() is None


def test_read_fresh_status_returns_none_for_corrupt_json(tmp_path):
    (tmp_path / "melcloud_status.json").write_text("{not json", encoding="utf-8")

    with _patch_path(tmp_path):
        assert melcloud_status_cache.read_fresh_status() is None
