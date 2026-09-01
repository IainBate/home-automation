"""Unit tests for src/core_logic/battery_evening_prediction_logic.py.

compute_historical_soc_drift_samples used to group readings strictly by the
calendar day of the *trigger* reading's own timestamp, so whenever
trigger_hour + horizon_hours pushed the horizon reading past midnight (a
realistic config, e.g. trigger_hour=22 with the default 3h horizon), that
reading lived under a different day's entry and was never found - silently
producing zero samples (and therefore no prediction, permanently falling
back to a live SoC reading) for every such configuration.

Historical days are matched against a reference_day_of_year within a sliding
window (see DEFAULT_WINDOW_DAYS) rather than a hard calendar-month bucket -
day 1 (day-of-year 1) is used throughout below since "day 1" is unambiguous
and, with the default 15-day window, cleanly includes/excludes the boundary
cases these tests care about (e.g. day-of-year 32 - February 1 - is 31 days
from day 1, well outside the window).
"""

from __future__ import annotations

from src.core_logic.battery_evening_prediction_logic import (
    SocDriftSample,
    compute_historical_soc_drift_samples,
    extract_forecast_generation_kwh,
    fit_generation_drift_correction,
    predict_evening_soc,
)


def _records(entries: list[tuple[str, float]]) -> list[dict]:
    return [{"timestamp": ts, "soc_percent": soc} for ts, soc in entries]


def test_same_day_horizon_produces_samples():
    records = _records(
        [
            ("2026-01-01 18:00:00", 80.0),
            ("2026-01-01 21:00:00", 60.0),
            ("2026-01-02 18:00:00", 80.0),
            ("2026-01-02 21:00:00", 60.0),
        ]
    )
    samples = compute_historical_soc_drift_samples(
        records, trigger_hour=18, horizon_hours=3.0, reference_day_of_year=1
    )
    assert len(samples) == 2
    assert all(sample.drift_percent == -20.0 for sample in samples)


def test_midnight_crossing_horizon_still_finds_the_next_day_reading():
    """Regression test: trigger_hour=22 + horizon_hours=3.0 = 01:00 the next
    calendar day - must not silently produce zero samples.
    """
    records = _records(
        [
            ("2026-01-01 22:00:00", 90.0),
            ("2026-01-02 01:00:00", 70.0),  # horizon reading - lives under the NEXT day
            ("2026-01-02 22:00:00", 90.0),
            ("2026-01-03 01:00:00", 70.0),
        ]
    )
    samples = compute_historical_soc_drift_samples(
        records, trigger_hour=22, horizon_hours=3.0, reference_day_of_year=1
    )
    assert len(samples) == 2
    assert all(sample.drift_percent == -20.0 for sample in samples)


def test_midnight_crossing_horizon_is_found_even_outside_the_trigger_days_window():
    """The horizon reading lookup must not itself be window-filtered: even
    with window_days=0 (only the exact reference day-of-year counts as a
    valid trigger day), a horizon reading landing on the next calendar day -
    a different, out-of-window day-of-year - must still be found. This is
    the sliding-window equivalent of the old month-boundary case (trigger on
    Jan 31, horizon reading on Feb 1).
    """
    records = _records(
        [
            ("2026-01-31 22:00:00", 90.0),
            ("2026-02-01 01:00:00", 65.0),  # different day-of-year, outside window_days=0
        ]
    )
    samples = compute_historical_soc_drift_samples(
        records, trigger_hour=22, horizon_hours=3.0, reference_day_of_year=31, window_days=0
    )
    assert len(samples) == 1
    assert samples[0].date == "2026-01-31"
    assert samples[0].drift_percent == -25.0


def test_trigger_day_outside_window_is_excluded_even_though_its_data_can_serve_as_an_horizon_match():
    """A reading on Feb 1 must not itself be treated as a valid *trigger* day
    when it falls outside window_days of the reference day-of-year, even
    though (per the previous test) its data can serve as an horizon match
    for a January 31 trigger.
    """
    records = _records(
        [
            ("2026-02-01 22:00:00", 90.0),
            ("2026-02-02 01:00:00", 65.0),
        ]
    )
    samples = compute_historical_soc_drift_samples(
        records, trigger_hour=22, horizon_hours=3.0, reference_day_of_year=31, window_days=0
    )
    assert samples == []


def test_predict_evening_soc_end_to_end_with_midnight_crossing_horizon():
    records = _records(
        [
            (f"2026-01-{d:02d} 22:00:00", 90.0) for d in range(1, 6)
        ]
        + [
            (f"2026-01-{d + 1:02d} 01:00:00", 70.0) for d in range(1, 6)
        ]
    )
    result = predict_evening_soc(
        current_soc_percent=90.0,
        historical_records=records,
        trigger_hour=22,
        horizon_hours=3.0,
        reference_day_of_year=1,
        min_sample_days=5,
    )
    assert result.predicted_soc_percent == 70.0
    assert result.sample_count == 5


# --- solar-forecast correction -----------------------------------------------


def _records_with_pv(entries: list[tuple[str, float, float]]) -> list[dict]:
    return [
        {"timestamp": ts, "soc_percent": soc, "pv_power_kw": pv_kw} for ts, soc, pv_kw in entries
    ]


def test_compute_historical_samples_include_pv_generation_kwh():
    # 3 hours in the window (18:00-21:00) at a steady 2.0kW -> 6.0kWh.
    records = _records_with_pv(
        [
            ("2026-01-01 18:00:00", 80.0, 2.0),
            ("2026-01-01 19:00:00", 0.0, 2.0),
            ("2026-01-01 20:00:00", 0.0, 2.0),
            ("2026-01-01 21:00:00", 60.0, 2.0),
        ]
    )
    samples = compute_historical_soc_drift_samples(records, trigger_hour=18, horizon_hours=3.0, month=1)
    assert len(samples) == 1
    assert samples[0].pv_generation_kwh == 6.0


def test_compute_historical_samples_pv_generation_is_none_without_pv_data():
    records = _records([("2026-01-01 18:00:00", 80.0), ("2026-01-01 21:00:00", 60.0)])
    samples = compute_historical_soc_drift_samples(records, trigger_hour=18, horizon_hours=3.0, month=1)
    assert len(samples) == 1
    assert samples[0].pv_generation_kwh is None


def test_fit_generation_drift_correction_needs_minimum_paired_samples():
    samples = [
        SocDriftSample("2026-01-01", 80.0, 70.0, pv_generation_kwh=1.0),
        SocDriftSample("2026-01-02", 80.0, 60.0, pv_generation_kwh=5.0),
    ]
    assert fit_generation_drift_correction(samples, min_paired_samples=3) is None
    assert fit_generation_drift_correction(samples, min_paired_samples=2) is not None


def test_fit_generation_drift_correction_none_when_no_variance_in_generation():
    samples = [
        SocDriftSample("2026-01-01", 80.0, 70.0, pv_generation_kwh=3.0),
        SocDriftSample("2026-01-02", 80.0, 65.0, pv_generation_kwh=3.0),
        SocDriftSample("2026-01-03", 80.0, 75.0, pv_generation_kwh=3.0),
    ]
    assert fit_generation_drift_correction(samples, min_paired_samples=3) is None


def test_predict_evening_soc_applies_forecast_correction_when_data_supports_it():
    # More sun -> less negative drift (surplus solar offsets discharge). Days
    # range from 1kWh (drift -20) to 9kWh (drift -4), a clean linear relationship.
    records = []
    for day in range(1, 10):
        pv_kwh_per_hour = float(day) / 3.0  # day/3 kWh/h * 3h window = day kWh
        drift = -20.0 + (day - 1) * 2.0
        records.append(
            {
                "timestamp": f"2026-01-{day:02d} 18:00:00",
                "soc_percent": 80.0,
                "pv_power_kw": pv_kwh_per_hour,
            }
        )
        records.append(
            {
                "timestamp": f"2026-01-{day:02d} 21:00:00",
                "soc_percent": 80.0 + drift,
                "pv_power_kw": pv_kwh_per_hour,
            }
        )

    baseline = predict_evening_soc(
        current_soc_percent=80.0,
        historical_records=records,
        trigger_hour=18,
        horizon_hours=3.0,
        reference_day_of_year=1,
        min_sample_days=5,
    )
    # Forecast a well-above-average sunny window (9kWh, the sunniest historical day).
    corrected = predict_evening_soc(
        current_soc_percent=80.0,
        historical_records=records,
        trigger_hour=18,
        horizon_hours=3.0,
        reference_day_of_year=1,
        min_sample_days=5,
        forecast_generation_kwh=9.0,
    )

    assert baseline.applied_drift_percent == baseline.average_drift_percent
    assert corrected.average_drift_percent == baseline.average_drift_percent  # unadjusted, unchanged
    # A forecast well above the historical average should correct the drift
    # to be less negative (closer to 9kWh's actual -4pp) than the plain average.
    assert corrected.applied_drift_percent > corrected.average_drift_percent
    assert corrected.predicted_soc_percent > baseline.predicted_soc_percent


def test_extract_forecast_generation_kwh_returns_none_outside_forecast_range():
    from datetime import datetime

    hourly = [{"timestamp": "2026-01-15 06:00", "predicted_kw": 1.0}]
    result = extract_forecast_generation_kwh(
        hourly, datetime(2026, 1, 16, 21, 0), datetime(2026, 1, 17, 0, 0)
    )
    assert result is None
