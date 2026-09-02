"""Tests for solar_forecast_logic.py's feature engineering and model training."""

from __future__ import annotations

import math

import pytest

from src.core_logic.solar_forecast_logic import (
    DEFAULT_MIN_TRAINING_ROWS,
    aggregate_pv_to_hourly,
    build_forecast_rows,
    build_training_rows,
    compute_actual_daily_kwh,
    predict_hourly_kw,
    train_model,
)


def test_aggregate_pv_to_hourly_averages_five_minute_samples():
    records = [
        {"timestamp": "2026-01-01 12:00:00", "pv_power_kw": 2.0},
        {"timestamp": "2026-01-01 12:05:00", "pv_power_kw": 4.0},
        {"timestamp": "2026-01-01 13:00:00", "pv_power_kw": 1.0},
        {"timestamp": "not-a-timestamp", "pv_power_kw": 99.0},
    ]

    result = aggregate_pv_to_hourly(records)

    assert result == {"2026-01-01 12:00": 3.0, "2026-01-01 13:00": 1.0}


def test_build_training_rows_joins_pv_and_weather_by_hour():
    pv_records = [{"timestamp": "2026-06-15 12:03:00", "pv_power_kw": 3.5}]
    weather_records = [
        {
            "timestamp": "2026-06-15T12:00",
            "shortwave_radiation": 500.0,
            "cloud_cover": 20.0,
            "temperature_2m": 18.0,
        },
        # No matching PV data for this hour - must be skipped.
        {
            "timestamp": "2026-06-15T13:00",
            "shortwave_radiation": 480.0,
            "cloud_cover": 25.0,
            "temperature_2m": 18.5,
        },
    ]

    rows = build_training_rows(pv_records, weather_records)

    assert len(rows) == 1
    assert rows[0]["pv_power_kw"] == 3.5
    assert rows[0]["hour_of_day"] == 12
    assert rows[0]["shortwave_radiation"] == 500.0


def test_build_forecast_rows_skips_incomplete_weather_records():
    weather_records = [
        {
            "timestamp": "2026-06-15T12:00",
            "shortwave_radiation": 500.0,
            "cloud_cover": 20.0,
            "temperature_2m": 18.0,
        },
        {"timestamp": "2026-06-15T13:00", "shortwave_radiation": None, "cloud_cover": 25.0, "temperature_2m": 18.5},
    ]

    rows = build_forecast_rows(weather_records)

    assert len(rows) == 1
    assert rows[0]["timestamp"] == "2026-06-15 12:00"


def _synthetic_training_rows(num_days: int = 20) -> list[dict]:
    """A clean, learnable synthetic relationship: pv scales with sun and inversely with cloud."""
    rows = []
    for day in range(num_days):
        for hour in range(6, 19):  # daylight hours
            sun_factor = math.sin(math.pi * (hour - 6) / 12)  # 0 at 6/18, peak at noon
            shortwave_radiation = 800.0 * sun_factor
            cloud_cover = 10.0 + (day % 5) * 15.0
            pv_power_kw = max(0.0, shortwave_radiation / 1000.0 * (1 - cloud_cover / 100.0) * 6.0)
            rows.append(
                {
                    "timestamp": f"2026-03-{day + 1:02d} {hour:02d}:00",
                    "hour_of_day": hour,
                    "day_of_year": 60 + day,
                    "shortwave_radiation": shortwave_radiation,
                    "cloud_cover": cloud_cover,
                    "temperature_2m": 12.0,
                    "pv_power_kw": pv_power_kw,
                }
            )
    return rows


def test_train_model_raises_on_too_few_rows():
    with pytest.raises(ValueError, match="not enough historical data"):
        train_model([{"hour_of_day": 1, "day_of_year": 1, "shortwave_radiation": 0, "cloud_cover": 0, "temperature_2m": 0, "pv_power_kw": 0, "timestamp": "x"}])


def test_train_model_learns_a_clean_synthetic_relationship():
    rows = _synthetic_training_rows()
    assert len(rows) >= DEFAULT_MIN_TRAINING_ROWS

    result = train_model(rows)

    assert result.train_rows + result.holdout_rows == len(rows)
    assert result.mae_kw < 1.0
    assert result.r2 > 0.7


def test_predict_hourly_kw_forces_zero_at_night_regardless_of_model_output():
    rows = _synthetic_training_rows()
    result = train_model(rows)

    night_row = {
        "timestamp": "2026-04-01 02:00",
        "hour_of_day": 2,
        "day_of_year": 91,
        "shortwave_radiation": 0.0,
        "cloud_cover": 50.0,
        "temperature_2m": 8.0,
    }

    predictions = predict_hourly_kw(result.model, [night_row])

    assert predictions == [0.0]


def test_predict_hourly_kw_never_returns_negative():
    rows = _synthetic_training_rows()
    result = train_model(rows)

    day_row = {
        "timestamp": "2026-04-01 12:00",
        "hour_of_day": 12,
        "day_of_year": 91,
        "shortwave_radiation": 1.0,  # tiny but non-zero irradiance
        "cloud_cover": 99.0,
        "temperature_2m": 8.0,
    }

    predictions = predict_hourly_kw(result.model, [day_row])

    assert predictions[0] >= 0.0


def test_predict_hourly_kw_handles_empty_input():
    assert predict_hourly_kw(model=object(), forecast_rows=[]) == []
