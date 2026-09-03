"""Tests for solar_forecast_logic.py's feature engineering and model training."""

from __future__ import annotations

import math

import pytest

from src.core_logic.solar_forecast_logic import (
    DAILY_FEATURE_COLUMNS,
    DEFAULT_MIN_TRAINING_ROWS,
    aggregate_pv_to_hourly,
    aggregate_weather_hourly_to_daily,
    build_daily_forecast_rows,
    build_daily_training_rows,
    compute_actual_daily_kwh,
    distribute_daily_kwh_to_hourly,
    merge_daily_pv_history,
    predict_daily_kwh,
    solar_geometry_for_date,
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


def test_compute_actual_daily_kwh_sums_matching_day_only():
    records = [
        {"timestamp": "2026-01-01 12:00:00", "pv_power_kw": 2.0},
        {"timestamp": "2026-01-01 13:00:00", "pv_power_kw": 1.0},
        {"timestamp": "2026-01-02 12:00:00", "pv_power_kw": 5.0},
    ]

    assert compute_actual_daily_kwh(records, "2026-01-01") == pytest.approx(3.0)


def test_compute_actual_daily_kwh_returns_none_when_no_data_for_date():
    records = [{"timestamp": "2026-01-01 12:00:00", "pv_power_kw": 2.0}]

    assert compute_actual_daily_kwh(records, "2026-01-02") is None


def test_compute_actual_daily_kwh_prefers_yield_today_from_last_record():
    """A day with only a late/partial sample must use the ground-truth cumulative
    total, not silently undercount from summing just that one sample."""
    records = [
        {"timestamp": "2026-01-01 21:00:00", "pv_power_kw": 0.0, "yield_today_kwh": 15.6},
    ]

    assert compute_actual_daily_kwh(records, "2026-01-01") == 15.6


def test_compute_actual_daily_kwh_tolerates_a_null_timestamp():
    """A record with an explicit {"timestamp": None} must be skipped, not crash.

    .get("timestamp", "") returns None (not the default) when the key exists
    with a null value, so this used to raise AttributeError and take the
    whole cron run down with it.
    """
    records = [
        {"timestamp": None, "pv_power_kw": 2.0},
        {"timestamp": "2026-01-01 12:00:00", "pv_power_kw": 3.0},
    ]

    assert compute_actual_daily_kwh(records, "2026-01-01") == pytest.approx(3.0)


def test_compute_actual_daily_kwh_returns_none_when_no_record_has_usable_power():
    """Records exist for the date but none carry usable data - that is "unknown",
    not a real zero, since the caller scores this against a forecast."""
    records = [{"timestamp": "2026-01-01 12:00:00", "pv_power_kw": None}]

    assert compute_actual_daily_kwh(records, "2026-01-01") is None


def test_compute_actual_daily_kwh_falls_back_when_yield_today_missing():
    """Older records (predating yield_today_kwh) still sum via hourly averages."""
    records = [
        {"timestamp": "2026-01-01 12:00:00", "pv_power_kw": 2.0},
        {"timestamp": "2026-01-01 13:00:00", "pv_power_kw": 1.0},
    ]

    assert compute_actual_daily_kwh(records, "2026-01-01") == pytest.approx(3.0)


def test_merge_daily_pv_history_prefers_seed_on_overlap():
    seed = {"2026-01-01": 4.5}
    pv_history = [
        {"timestamp": "2026-01-01 12:00:00", "pv_power_kw": 99.0},  # would give a wildly different value
        {"timestamp": "2026-01-02 12:00:00", "pv_power_kw": 3.0},
    ]

    result = merge_daily_pv_history(seed, pv_history)

    assert result["2026-01-01"] == 4.5  # seed wins
    assert result["2026-01-02"] == pytest.approx(3.0)  # new date, filled from local telemetry


def test_merge_daily_pv_history_skips_dates_with_no_usable_local_data():
    result = merge_daily_pv_history({}, [{"timestamp": "2026-01-01 12:00:00", "pv_power_kw": None}])

    assert result == {}


def test_solar_geometry_for_date_gives_longer_days_in_summer_than_winter():
    summer = solar_geometry_for_date(_date(2026, 6, 21), 53.88, -1.04, "Europe/London")
    winter = solar_geometry_for_date(_date(2026, 1, 15), 53.88, -1.04, "Europe/London")

    assert summer["day_length_hours"] > winter["day_length_hours"]
    assert summer["max_elevation"] > winter["max_elevation"]


def test_aggregate_weather_hourly_to_daily_sums_radiation_and_averages_cloud():
    records = [
        {
            "timestamp": "2026-06-15T12:00",
            "shortwave_radiation": 500.0,
            "direct_radiation": 400.0,
            "diffuse_radiation": 100.0,
            "cloud_cover": 20.0,
            "cloud_cover_low": 10.0,
            "cloud_cover_mid": 5.0,
            "cloud_cover_high": 5.0,
            "temperature_2m": 18.0,
        },
        {
            "timestamp": "2026-06-15T13:00",
            "shortwave_radiation": 480.0,
            "direct_radiation": 380.0,
            "diffuse_radiation": 100.0,
            "cloud_cover": 40.0,
            "cloud_cover_low": 20.0,
            "cloud_cover_mid": 10.0,
            "cloud_cover_high": 10.0,
            "temperature_2m": 19.0,
        },
        # A different day - must not leak into 2026-06-15's aggregate.
        {
            "timestamp": "2026-06-16T12:00",
            "shortwave_radiation": 100.0,
            "direct_radiation": 50.0,
            "diffuse_radiation": 50.0,
            "cloud_cover": 90.0,
            "cloud_cover_low": 90.0,
            "cloud_cover_mid": 0.0,
            "cloud_cover_high": 0.0,
            "temperature_2m": 15.0,
        },
    ]

    daily = aggregate_weather_hourly_to_daily(records)

    assert daily["2026-06-15"]["shortwave_radiation_sum"] == pytest.approx(980.0)
    assert daily["2026-06-15"]["cloud_cover_mean"] == pytest.approx(30.0)
    assert daily["2026-06-15"]["temperature_mean"] == pytest.approx(18.5)
    assert daily["2026-06-16"]["shortwave_radiation_sum"] == pytest.approx(100.0)


def test_aggregate_weather_hourly_to_daily_skips_days_with_no_usable_radiation():
    records = [{"timestamp": "2026-06-15T12:00", "shortwave_radiation": None, "cloud_cover": 20.0}]

    assert aggregate_weather_hourly_to_daily(records) == {}


def test_build_daily_training_rows_joins_pv_and_weather_by_date():
    daily_pv_kwh = {"2026-06-15": 12.5, "2026-06-16": 8.0}  # no weather for 06-16 below - must be skipped
    weather_records = _synthetic_hourly_weather("2026-06-15")

    rows = build_daily_training_rows(daily_pv_kwh, weather_records, 53.88, -1.04, "Europe/London")

    assert len(rows) == 1
    assert rows[0]["date"] == "2026-06-15"
    assert rows[0]["pv_kwh"] == 12.5
    assert set(DAILY_FEATURE_COLUMNS) <= rows[0].keys()


def test_build_daily_forecast_rows_produces_one_row_per_day():
    weather_records = _synthetic_hourly_weather("2026-06-15") + _synthetic_hourly_weather("2026-06-16")

    rows = build_daily_forecast_rows(weather_records, 53.88, -1.04, "Europe/London")

    assert [row["date"] for row in rows] == ["2026-06-15", "2026-06-16"]
    assert set(DAILY_FEATURE_COLUMNS) <= rows[0].keys()


def _date(year: int, month: int, day: int):
    from datetime import date

    return date(year, month, day)


def _synthetic_hourly_weather(date_str: str) -> list[dict]:
    """A clean, learnable synthetic relationship: pv scales with sun and inversely with cloud."""
    records = []
    for hour in range(24):
        sun_factor = max(0.0, math.sin(math.pi * (hour - 6) / 12)) if 6 <= hour <= 18 else 0.0
        shortwave_radiation = 800.0 * sun_factor
        records.append(
            {
                "timestamp": f"{date_str}T{hour:02d}:00",
                "shortwave_radiation": shortwave_radiation,
                "direct_radiation": shortwave_radiation * 0.8,
                "diffuse_radiation": shortwave_radiation * 0.2,
                "cloud_cover": 30.0,
                "cloud_cover_low": 20.0,
                "cloud_cover_mid": 5.0,
                "cloud_cover_high": 5.0,
                "temperature_2m": 15.0,
            }
        )
    return records


def _synthetic_training_rows(num_days: int = 90) -> list[dict]:
    """A clean, learnable synthetic relationship: daily pv scales with radiation, inversely with cloud."""
    rows = []
    for day in range(num_days):
        cloud_cover = 10.0 + (day % 5) * 15.0
        radiation_sum = 5000.0 * (1 - cloud_cover / 150.0)
        pv_kwh = max(0.0, radiation_sum / 1000.0 * 6.0)
        rows.append(
            {
                "date": f"2026-{(day // 28) + 1:02d}-{(day % 28) + 1:02d}",
                "day_of_year": 60 + day,
                "shortwave_radiation_sum": radiation_sum,
                "direct_radiation_sum": radiation_sum * 0.8,
                "diffuse_radiation_sum": radiation_sum * 0.2,
                "cloud_cover_mean": cloud_cover,
                "cloud_cover_low_mean": cloud_cover * 0.7,
                "cloud_cover_mid_mean": cloud_cover * 0.2,
                "cloud_cover_high_mean": cloud_cover * 0.1,
                "temperature_mean": 12.0,
                "max_elevation": 30.0,
                "day_length_hours": 10.0,
                "pv_kwh": pv_kwh,
            }
        )
    return rows


def test_train_model_raises_on_too_few_rows():
    with pytest.raises(ValueError, match="not enough historical data"):
        train_model([{col: 0 for col in DAILY_FEATURE_COLUMNS} | {"pv_kwh": 0, "date": "x"}])


def test_train_model_learns_a_clean_synthetic_relationship():
    rows = _synthetic_training_rows()
    assert len(rows) >= DEFAULT_MIN_TRAINING_ROWS

    result = train_model(rows)

    assert result.train_rows + result.holdout_rows == len(rows)
    assert result.mae_kwh < 3.0
    assert result.r2 > 0.5


def test_predict_daily_kwh_never_returns_negative():
    rows = _synthetic_training_rows()
    result = train_model(rows)

    tiny_radiation_row = {col: 0.0 for col in DAILY_FEATURE_COLUMNS}
    tiny_radiation_row["cloud_cover_mean"] = 100.0

    predictions = predict_daily_kwh(result.model, [tiny_radiation_row])

    assert predictions[0] >= 0.0


def test_predict_daily_kwh_handles_empty_input():
    assert predict_daily_kwh(model=object(), forecast_rows=[]) == []


def test_distribute_daily_kwh_to_hourly_shapes_by_radiation_share():
    hourly_weather = [
        {"timestamp": "2026-06-15T06:00", "shortwave_radiation": 100.0},
        {"timestamp": "2026-06-15T12:00", "shortwave_radiation": 300.0},
        {"timestamp": "2026-06-15T18:00", "shortwave_radiation": 0.0},
    ]

    result = distribute_daily_kwh_to_hourly(20.0, hourly_weather)

    assert result == [
        {"timestamp": "2026-06-15 06:00", "predicted_kw": 5.0},
        {"timestamp": "2026-06-15 12:00", "predicted_kw": 15.0},
        {"timestamp": "2026-06-15 18:00", "predicted_kw": 0.0},
    ]
    assert sum(r["predicted_kw"] for r in result) == pytest.approx(20.0)


def test_distribute_daily_kwh_to_hourly_splits_evenly_when_all_radiation_zero():
    """A nonzero forecast total must never be silently lost to an all-zero radiation input."""
    hourly_weather = [
        {"timestamp": "2026-06-15T06:00", "shortwave_radiation": 0.0},
        {"timestamp": "2026-06-15T07:00", "shortwave_radiation": 0.0},
    ]

    result = distribute_daily_kwh_to_hourly(10.0, hourly_weather)

    assert sum(r["predicted_kw"] for r in result) == pytest.approx(10.0)
    assert result[0]["predicted_kw"] == result[1]["predicted_kw"] == 5.0


def test_distribute_daily_kwh_to_hourly_handles_empty_input():
    assert distribute_daily_kwh_to_hourly(10.0, []) == []
