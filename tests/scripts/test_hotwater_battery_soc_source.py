"""Tests for get_effective_battery_soc_percent / _read_fresh_evening_prediction
in hotwater_automation_core.py.

Regression test for a docstring/behavior mismatch: get_effective_battery_soc_percent
claims it "falls back to a live read... whenever the predictor is disabled",
but never actually checked battery_evening_prediction.enabled before reading
the prediction file - a fresh prediction file left over from before the
feature was disabled would keep being used regardless.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest import mock

import hotwater_automation_core as core


def _write_prediction(tmp_path, *, predicted_soc_percent=70.0, computed_at=None):
    path = tmp_path / "battery_evening_prediction.json"
    record = {
        "predicted_soc_percent": predicted_soc_percent,
        "computed_at": (computed_at or datetime.now(tz=UTC)).isoformat(),
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _get_effective_soc(tmp_path, config, hw_config=None, *, live_soc=42.0):
    prediction_path = tmp_path / "battery_evening_prediction.json"
    hw_config = hw_config or {}
    now_local = datetime.now(tz=UTC)

    with mock.patch.object(
        core, "get_battery_evening_prediction_path", lambda: str(prediction_path)
    ), mock.patch.object(core, "get_battery_soc_percent", lambda cfg: live_soc):
        return core.get_effective_battery_soc_percent(config, hw_config, now_local)


def test_disabled_predictor_falls_back_to_live_even_with_a_fresh_prediction_file(tmp_path):
    """The regression case: enabled=False (or absent), but a fresh valid
    prediction file exists anyway (e.g. left over from before it was
    disabled) - must not be used.
    """
    _write_prediction(tmp_path)
    config = {"battery_evening_prediction": {"enabled": False}}

    soc, source = _get_effective_soc(tmp_path, config, live_soc=42.0)

    assert source == "live"
    assert soc == 42.0


def test_missing_battery_evening_prediction_section_defaults_to_disabled(tmp_path):
    _write_prediction(tmp_path)
    config = {}  # no battery_evening_prediction section at all

    soc, source = _get_effective_soc(tmp_path, config, live_soc=42.0)

    assert source == "live"
    assert soc == 42.0


def test_enabled_predictor_with_fresh_prediction_is_used(tmp_path):
    _write_prediction(tmp_path, predicted_soc_percent=88.0)
    config = {"battery_evening_prediction": {"enabled": True}}

    soc, source = _get_effective_soc(tmp_path, config, live_soc=42.0)

    assert source == "predicted"
    assert soc == 88.0


def test_enabled_predictor_with_no_file_falls_back_to_live(tmp_path):
    config = {"battery_evening_prediction": {"enabled": True}}

    soc, source = _get_effective_soc(tmp_path, config, live_soc=42.0)

    assert source == "live"
    assert soc == 42.0


def test_enabled_predictor_with_stale_prediction_falls_back_to_live(tmp_path):
    stale_time = datetime.now(tz=UTC) - timedelta(hours=10)
    _write_prediction(tmp_path, computed_at=stale_time)
    config = {"battery_evening_prediction": {"enabled": True}}
    hw_config = {"max_prediction_age_hours": 3.0}

    soc, source = _get_effective_soc(tmp_path, config, hw_config, live_soc=42.0)

    assert source == "live"
    assert soc == 42.0


def test_enabled_predictor_with_prediction_from_a_different_day_falls_back_to_live(tmp_path):
    yesterday = datetime.now(tz=UTC) - timedelta(days=1)
    _write_prediction(tmp_path, computed_at=yesterday)
    config = {"battery_evening_prediction": {"enabled": True}}

    soc, source = _get_effective_soc(tmp_path, config, live_soc=42.0)

    assert source == "live"
    assert soc == 42.0
