"""Tests for battery_evening_predictor.py's solar-forecast-based correction.

Covers _read_forecast_generation_kwh's degrade-to-None cases (disabled,
missing/corrupt file, no matching hourly_kw entries) and run()'s end-to-end
wiring of forecast_generation_kwh into predict_evening_soc and the written
prediction_record - without a forecast, behavior must be byte-for-byte the
same as before this feature existed (see
test_battery_evening_predictor_checkpoints.py, which must not need to change).
"""

from __future__ import annotations

import datetime as datetime_module
import json
from unittest import mock

import battery_evening_predictor as predictor


class _FrozenDateTime(datetime_module.datetime):
    _frozen_now: datetime_module.datetime

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003 - matching datetime.now's signature
        return cls._frozen_now


def _freeze(monkeypatch, dt: datetime_module.datetime) -> None:
    frozen = type("_FrozenDateTime", (_FrozenDateTime,), {})
    frozen._frozen_now = dt
    monkeypatch.setattr(predictor, "datetime", frozen)


# --- _read_forecast_generation_kwh ------------------------------------------


def test_disabled_returns_none(tmp_path):
    result = predictor._read_forecast_generation_kwh(
        {"solar_forecast": {"enabled": False}},
        datetime_module.datetime(2026, 1, 15, 21, 30),
        datetime_module.datetime(2026, 1, 16, 0, 30),
    )
    assert result is None


def test_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(predictor, "get_solar_forecast_path", lambda: str(tmp_path / "missing.json"))
    result = predictor._read_forecast_generation_kwh(
        {"solar_forecast": {"enabled": True}},
        datetime_module.datetime(2026, 1, 15, 21, 30),
        datetime_module.datetime(2026, 1, 16, 0, 30),
    )
    assert result is None


def test_corrupt_file_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "solar_forecast.json"
    path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(predictor, "get_solar_forecast_path", lambda: str(path))

    result = predictor._read_forecast_generation_kwh(
        {"solar_forecast": {"enabled": True}},
        datetime_module.datetime(2026, 1, 15, 21, 30),
        datetime_module.datetime(2026, 1, 16, 0, 30),
    )
    assert result is None


def test_valid_file_sums_the_window(tmp_path, monkeypatch):
    path = tmp_path / "solar_forecast.json"
    path.write_text(
        json.dumps(
            {
                "hourly_kw": [
                    {"timestamp": "2026-01-15 20:00", "predicted_kw": 1.0},  # outside window
                    {"timestamp": "2026-01-15 21:00", "predicted_kw": 2.0},  # inside
                    {"timestamp": "2026-01-15 22:00", "predicted_kw": 3.0},  # inside
                    {"timestamp": "2026-01-16 01:00", "predicted_kw": 4.0},  # outside window
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(predictor, "get_solar_forecast_path", lambda: str(path))

    result = predictor._read_forecast_generation_kwh(
        {"solar_forecast": {"enabled": True}},
        datetime_module.datetime(2026, 1, 15, 21, 0),
        datetime_module.datetime(2026, 1, 16, 0, 0),
    )
    assert result == 5.0


# --- run() end-to-end --------------------------------------------------------


def _historical_records_with_flat_drift(drift_percent: float) -> list[dict]:
    records = []
    for day in range(1, 10):
        for hour in range(20, 24):
            soc = 70.0 + drift_percent * (hour - 21.5)
            records.append(
                {
                    "timestamp": f"2026-06-{day:02d} {hour:02d}:00:00",
                    "soc_percent": soc,
                    "pv_power_kw": 2.0,
                }
            )
    return records


def test_run_without_solar_forecast_passes_none_and_matches_pre_feature_behavior(
    tmp_path, monkeypatch
):
    _freeze(monkeypatch, datetime_module.datetime(2026, 6, 15, 21, 30, tzinfo=datetime_module.UTC))
    records = _historical_records_with_flat_drift(-5.0)
    written = {}

    monkeypatch.setattr(predictor, "get_battery_soc_percent", lambda cfg: 70.0)
    monkeypatch.setattr(predictor, "load_historical_records", lambda: records)
    monkeypatch.setattr(predictor, "write_prediction", lambda record: written.update(record))

    config = {
        "location": {"default_timezone_str": "UTC"},
        "hotwater_automation": {"trigger_hour": 21.5},
        "solar_forecast": {"enabled": False},
    }

    exit_code = predictor.run(config, quiet=True)

    assert exit_code == 0
    assert written["forecast_generation_kwh"] is None
    assert written["applied_drift_percent"] == written["average_drift_percent"]


def test_run_with_solar_forecast_applies_the_correction(tmp_path, monkeypatch):
    _freeze(monkeypatch, datetime_module.datetime(2026, 6, 15, 21, 30, tzinfo=datetime_module.UTC))
    # Historical days all generated exactly 2.0kWh in the window - no variance
    # to fit a slope from, by design: this test only needs to prove the
    # plumbing (forecast read -> passed into predict_evening_soc -> recorded),
    # not the regression math itself (covered in
    # test_battery_evening_prediction_logic.py).
    records = _historical_records_with_flat_drift(-5.0)
    written = {}

    forecast_path = tmp_path / "solar_forecast.json"
    forecast_path.write_text(
        json.dumps(
            {
                "hourly_kw": [
                    # Window is [21:30, 00:30) - 22:00/23:00/00:00 all fall inside it.
                    {"timestamp": "2026-06-15 22:00", "predicted_kw": 3.0},
                    {"timestamp": "2026-06-15 23:00", "predicted_kw": 3.0},
                    {"timestamp": "2026-06-16 00:00", "predicted_kw": 3.0},
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(predictor, "get_battery_soc_percent", lambda cfg: 70.0)
    monkeypatch.setattr(predictor, "load_historical_records", lambda: records)
    monkeypatch.setattr(predictor, "write_prediction", lambda record: written.update(record))
    monkeypatch.setattr(predictor, "get_solar_forecast_path", lambda: str(forecast_path))

    config = {
        "location": {"default_timezone_str": "UTC"},
        "hotwater_automation": {"trigger_hour": 21.5},
        "solar_forecast": {"enabled": True},
    }

    exit_code = predictor.run(config, quiet=True)

    assert exit_code == 0
    assert written["forecast_generation_kwh"] == 9.0
    # No variance among historical PV samples (all 2.0kWh) -> fit_generation_drift_correction
    # can't fit a slope -> falls back to the plain historical average, same as without forecast.
    assert written["applied_drift_percent"] == written["average_drift_percent"]


def test_run_with_trigger_hour_that_rounds_to_60_minutes_does_not_crash(tmp_path, monkeypatch):
    """Regression test: trigger_ts used to be built as
    now_local.replace(hour=int(trigger_hour), minute=round((trigger_hour % 1) * 60), ...),
    which raises ValueError for any trigger_hour whose fractional part rounds
    up to a full hour (e.g. 20.995 -> minute=60) - config.yaml's schema only
    bounds trigger_hour to a number, not its fractional part. Now built via
    timedelta addition from midnight instead, which has no such edge case.
    """
    _freeze(monkeypatch, datetime_module.datetime(2026, 6, 15, 21, 0, tzinfo=datetime_module.UTC))
    records = _historical_records_with_flat_drift(-5.0)
    written = {}

    monkeypatch.setattr(predictor, "get_battery_soc_percent", lambda cfg: 70.0)
    monkeypatch.setattr(predictor, "load_historical_records", lambda: records)
    monkeypatch.setattr(predictor, "write_prediction", lambda record: written.update(record))

    config = {
        "location": {"default_timezone_str": "UTC"},
        "hotwater_automation": {"trigger_hour": 20.995},
        "solar_forecast": {"enabled": False},
    }

    exit_code = predictor.run(config, quiet=True)

    assert exit_code == 0
    assert written["trigger_hour"] == 20.995
