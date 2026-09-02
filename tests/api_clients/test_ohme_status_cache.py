"""Tests for the shared Ohme status cache.

The property that matters most here is the fallback contract: a missing,
malformed or stale cache must read as "no cached answer" (None) so callers
fall back to their own direct Ohme call - never as "the car isn't charging",
which would silently drop the battery daemon out of FORCE_CHARGE and make
the hot water automation miss its trigger.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from enum import Enum
from unittest import mock

from src.api_clients import ohme_status_cache


class _FakeEnum(Enum):
    CHARGING = "charging"
    SMART_CHARGE = "smart_charge"


def _patch_path(tmp_path):
    return mock.patch.object(
        ohme_status_cache, "get_ohme_status_path", lambda: str(tmp_path / "ohme_status.json")
    )


def test_read_fresh_status_returns_none_when_cache_missing(tmp_path):
    with _patch_path(tmp_path):
        assert ohme_status_cache.read_fresh_status() is None


def test_write_then_read_round_trips_and_serializes_enums(tmp_path):
    status = {
        "plugged_in": True,
        "status": _FakeEnum.CHARGING,
        "mode": _FakeEnum.SMART_CHARGE,
        "power_watts": 7200,
        "battery_percent": 62,
        "target_soc": 80,
        "current_vehicle": "MG ZS",
    }

    with _patch_path(tmp_path):
        ohme_status_cache.write_status_cache(status)
        cached = ohme_status_cache.read_fresh_status()

    assert cached is not None
    assert cached["power_watts"] == 7200
    assert cached["status"] == "charging"
    assert cached["mode"] == "smart_charge"
    assert cached["current_vehicle"] == "MG ZS"
    assert "fetched_at" in cached


def test_read_fresh_status_returns_none_when_stale(tmp_path):
    stale = {
        "fetched_at": (datetime.now(tz=UTC) - timedelta(seconds=600)).isoformat(),
        "power_watts": 7200,
    }
    (tmp_path / "ohme_status.json").write_text(json.dumps(stale), encoding="utf-8")

    with _patch_path(tmp_path):
        assert ohme_status_cache.read_fresh_status(max_age_seconds=150) is None


def test_read_fresh_status_accepts_a_recent_reading(tmp_path):
    recent = {
        "fetched_at": (datetime.now(tz=UTC) - timedelta(seconds=20)).isoformat(),
        "power_watts": 7200,
    }
    (tmp_path / "ohme_status.json").write_text(json.dumps(recent), encoding="utf-8")

    with _patch_path(tmp_path):
        cached = ohme_status_cache.read_fresh_status(max_age_seconds=150)

    assert cached is not None
    assert cached["power_watts"] == 7200


def test_read_fresh_status_returns_none_for_malformed_timestamps(tmp_path):
    path = tmp_path / "ohme_status.json"

    for record in (
        {"power_watts": 7200},  # no fetched_at at all
        {"fetched_at": "not-a-timestamp", "power_watts": 7200},
        {"fetched_at": "2026-09-03 01:00:00", "power_watts": 7200},  # naive, no offset
    ):
        path.write_text(json.dumps(record), encoding="utf-8")
        with _patch_path(tmp_path):
            assert ohme_status_cache.read_fresh_status() is None


def test_read_fresh_status_returns_none_for_corrupt_json(tmp_path):
    (tmp_path / "ohme_status.json").write_text("{not json", encoding="utf-8")

    with _patch_path(tmp_path):
        assert ohme_status_cache.read_fresh_status() is None
