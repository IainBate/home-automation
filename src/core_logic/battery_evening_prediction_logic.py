"""Evening Battery SoC Prediction Logic.

Predicts what the battery's state of charge will be some hours after a given
"trigger hour" (e.g. 18:00), so the hot water force-heat decision
(hotwater_decision_logic.py) can judge whether stored solar will still be
there by the time a multi-hour heating run finishes, rather than trusting a
single live SoC snapshot to still hold true hours later.

This deliberately is NOT a trained ML model (no numpy/scikit-learn
dependency, nothing to retrain/version/ship) - it's a simple "analog day"
statistical method: look at how the battery's SoC has historically moved
between two times of day in the same calendar month (a cheap proxy for
season/weather/day-length) using the 5-minute historical log already
collected by scripts/solax_cloud_data_logger.py, and apply that average
drift to today's reading.

Optionally corrected by today's actual solar forecast (scripts/
solar_forecast_predictor.py's trained model), when available: the plain
historical average only ever looks backward, so it can't tell an unusually
sunny or overcast day from a typical one for the month. When a forecast
generation figure is supplied, a small linear correction (fit from the same
historical sample set, generation kWh -> SoC drift) nudges the predicted
drift toward what today's specific forecast implies, rather than swapping in
a separate trained model of its own - see fit_generation_drift_correction.
This still degrades gracefully to the plain historical average whenever no
forecast is supplied, the historical days don't have usable PV data, or
there isn't enough of it to fit - callers should never depend on it.

Design Principles (mirrors ohme_charging_logic.py / hotwater_decision_logic.py):
- Pure functions: no I/O, no API calls, testable
- Clear data contracts: explicit input/output types using dataclasses
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

DEFAULT_MATCH_TOLERANCE_MINUTES = 15.0
DEFAULT_MIN_SAMPLE_DAYS = 5
DEFAULT_WINDOW_DAYS = 15

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
_DAYS_IN_YEAR = 365  # Fine as a circular-distance constant - a leap day's off-by-one doesn't
# matter against a +-15 day window


def _day_of_year_distance(a: int, b: int) -> int:
    """Circular distance (days) between two day-of-year values, wrapping across New Year's."""
    diff = abs(a - b)
    return min(diff, _DAYS_IN_YEAR - diff)


@dataclass
class SocDriftSample:
    """One historical day's SoC change from the trigger hour to the horizon.

    Attributes:
        date: ISO date (YYYY-MM-DD) the sample was taken from, for diagnostics.
        soc_at_trigger_percent: SoC reading closest to the trigger hour.
        soc_at_horizon_percent: SoC reading closest to trigger hour + horizon.
        pv_generation_kwh: This day's approximate solar generation during the
            trigger-to-horizon window (average pv_power_kw reading in the
            window * horizon_hours), or None if there was no usable
            pv_power_kw data for it - a day can still contribute a plain
            drift sample without this, it just can't be used to fit
            fit_generation_drift_correction.

    """

    date: str
    soc_at_trigger_percent: float
    soc_at_horizon_percent: float
    pv_generation_kwh: float | None = None

    @property
    def drift_percent(self) -> float:
        """Change in SoC (percentage points) from trigger hour to horizon."""
        return self.soc_at_horizon_percent - self.soc_at_trigger_percent


@dataclass
class EveningSocPrediction:
    """Result of predicting evening battery SoC from historical drift.

    Attributes:
        predicted_soc_percent: Predicted SoC at trigger hour + horizon, or
            None if there wasn't enough historical data to predict.
        sample_count: Number of historical days used (0 if none matched).
        average_drift_percent: Mean SoC change (percentage points) across the
            matched historical days (unadjusted - the plain historical
            average, regardless of whether a forecast correction was
            applied), or None if predicted_soc_percent is None.
        applied_drift_percent: The drift actually applied to get
            predicted_soc_percent - equal to average_drift_percent unless a
            forecast-generation correction was fit and used (see
            predict_evening_soc's forecast_generation_kwh), or None if
            predicted_soc_percent is None.
        reason: Human-readable explanation, for logging.

    """

    predicted_soc_percent: float | None
    sample_count: int
    average_drift_percent: float | None
    applied_drift_percent: float | None
    reason: str


def extract_forecast_generation_kwh(
    hourly_kw: list[dict], window_start: datetime, window_end: datetime
) -> float | None:
    """Sum scripts/solar_forecast_predictor.py's hourly_kw over [window_start, window_end).

    Each hourly_kw entry is an hour-timestamped average kW figure (see
    solar_forecast_predictor.py's record format), so summing the ones whose
    hour falls in the window approximates that window's total kWh generation
    - the same units predict_evening_soc's forecast_generation_kwh expects.

    Args:
        hourly_kw: The forecast record's "hourly_kw" list - dicts with
            "timestamp" ("YYYY-MM-DD HH:00") and "predicted_kw" keys.
        window_start: Start of the trigger-to-horizon window (inclusive).
        window_end: End of the window (exclusive).

    Returns:
        Summed forecast kWh, or None if no hourly_kw entry fell in the
        window (e.g. the forecast doesn't reach far enough ahead) -
        distinguished from 0.0 (a real forecast of no generation, e.g.
        overnight) the same way _window_generation_kwh does for historical
        data.

    Examples:
        >>> from datetime import datetime
        >>> hourly = [
        ...     {"timestamp": "2026-01-15 21:00", "predicted_kw": 0.0},
        ...     {"timestamp": "2026-01-15 22:00", "predicted_kw": 0.0},
        ...     {"timestamp": "2026-01-16 06:00", "predicted_kw": 0.5},
        ... ]
        >>> extract_forecast_generation_kwh(
        ...     hourly, datetime(2026, 1, 15, 21, 30), datetime(2026, 1, 16, 0, 30)
        ... )
        0.0
        >>> extract_forecast_generation_kwh(
        ...     hourly, datetime(2026, 1, 16, 1, 0), datetime(2026, 1, 16, 4, 0)
        ... )

    """
    in_window = []
    for entry in hourly_kw:
        try:
            timestamp = datetime.strptime(entry["timestamp"], "%Y-%m-%d %H:%M")
            predicted_kw = float(entry["predicted_kw"])
        except (KeyError, ValueError, TypeError):
            continue
        if window_start <= timestamp < window_end:
            in_window.append(predicted_kw)

    if not in_window:
        return None
    return sum(in_window)


def _closest_reading_percent(
    readings: list[tuple[datetime, float]],
    target: datetime,
    tolerance_minutes: float,
) -> float | None:
    """Return the SoC reading closest to `target` within tolerance, or None."""
    best_diff_minutes: float | None = None
    best_soc: float | None = None
    for timestamp, soc_percent in readings:
        diff_minutes = abs((timestamp - target).total_seconds()) / 60.0
        if diff_minutes > tolerance_minutes:
            continue
        if best_diff_minutes is None or diff_minutes < best_diff_minutes:
            best_diff_minutes = diff_minutes
            best_soc = soc_percent
    return best_soc


def compute_historical_soc_drift_samples(
    historical_records: list[dict],
    trigger_hour: float,
    horizon_hours: float,
    reference_day_of_year: int,
    tolerance_minutes: float = DEFAULT_MATCH_TOLERANCE_MINUTES,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[SocDriftSample]:
    """Build one SoC drift sample per historical day near the given day of year.

    Each sample compares the SoC reading closest to `trigger_hour` against
    the one closest to `trigger_hour + horizon_hours` on the same calendar
    day. Restricting to days within `window_days` of `reference_day_of_year`
    is a cheap stand-in for season/day-length rather than proper
    weather-aware modelling, which isn't worth the added complexity here.

    A sliding day-of-year window rather than a hard calendar-month bucket
    deliberately avoids a real problem the latter had: the first few days of
    a month had zero same-month history to match against (even though the
    tail end of the previous month is a nearly identical day-length/season),
    only for it to work again on the 6th of the month. The window wraps
    across the New Year's boundary too (e.g. late December matches early
    January).

    Args:
        historical_records: Records shaped like solax_historical_data.json's
            "data" list - each a dict with "timestamp" ("YYYY-MM-DD HH:MM:SS"),
            "soc_percent", and (optionally, for pv_generation_kwh) "pv_power_kw"
            keys. Malformed records are skipped; a record missing/malformed
            only "pv_power_kw" still contributes its soc_percent normally.
        trigger_hour: Local hour (0-23) each day's "before" reading is taken
            from - may be fractional (e.g. 17.75 for 17:45) so a caller
            anchoring to "right now" doesn't lose precision by truncating to
            the hour (scripts/battery_evening_predictor.py's dashboard
            checkpoints do this).
        horizon_hours: Hours after trigger_hour for the "after" reading.
        reference_day_of_year: Day of year (1-366, e.g. from
            `datetime.timetuple().tm_yday`) trigger days are matched against.
        tolerance_minutes: Max distance (minutes) a reading may be from its
            target time and still count as a match.
        window_days: How many days either side of reference_day_of_year
            count as a match.

    Returns:
        One SocDriftSample per historical day that had usable readings near
        both target times.

    """
    # Grouped by day WITHOUT filtering by month yet: trigger_hour + horizon_hours
    # can push the horizon reading past midnight (e.g. trigger_hour=22 with the
    # default 3h horizon), landing on the next calendar day - and occasionally
    # the next calendar month too (the last day of a month). That reading needs
    # to be findable here regardless of which month it falls in; only the
    # *trigger* day is restricted to `month` below, as the season/day-length proxy.
    readings_by_day: dict[str, list[tuple[datetime, float]]] = {}
    pv_readings_by_day: dict[str, list[tuple[datetime, float]]] = {}
    for record in historical_records:
        try:
            timestamp = datetime.strptime(record["timestamp"], _TIMESTAMP_FORMAT)
        except (KeyError, ValueError, TypeError):
            continue

        try:
            soc_percent = float(record["soc_percent"])
        except (KeyError, ValueError, TypeError):
            pass
        else:
            readings_by_day.setdefault(timestamp.date().isoformat(), []).append(
                (timestamp, soc_percent)
            )

        try:
            pv_power_kw = float(record["pv_power_kw"])
        except (KeyError, ValueError, TypeError):
            pass
        else:
            pv_readings_by_day.setdefault(timestamp.date().isoformat(), []).append(
                (timestamp, pv_power_kw)
            )

    samples: list[SocDriftSample] = []
    for date_str, trigger_day_readings in readings_by_day.items():
        day_start = datetime.strptime(date_str, "%Y-%m-%d")
        if day_start.month != month:
            continue

        trigger_ts = day_start + timedelta(hours=trigger_hour)
        horizon_ts = trigger_ts + timedelta(hours=horizon_hours)

        soc_at_trigger = _closest_reading_percent(trigger_day_readings, trigger_ts, tolerance_minutes)

        horizon_readings = trigger_day_readings
        if horizon_ts.date() != day_start.date():
            # horizon_hours pushed past midnight - those readings live under
            # the next day's entry, not this trigger day's.
            horizon_readings = trigger_day_readings + readings_by_day.get(
                horizon_ts.date().isoformat(), []
            )

        soc_at_horizon = _closest_reading_percent(horizon_readings, horizon_ts, tolerance_minutes)
        if soc_at_trigger is None or soc_at_horizon is None:
            continue

        pv_readings = pv_readings_by_day.get(date_str, [])
        if horizon_ts.date() != day_start.date():
            pv_readings = pv_readings + pv_readings_by_day.get(horizon_ts.date().isoformat(), [])
        pv_generation_kwh = _window_generation_kwh(pv_readings, trigger_ts, horizon_ts, horizon_hours)

        samples.append(SocDriftSample(date_str, soc_at_trigger, soc_at_horizon, pv_generation_kwh))

    return samples


def _window_generation_kwh(
    pv_readings: list[tuple[datetime, float]],
    window_start: datetime,
    window_end: datetime,
    horizon_hours: float,
) -> float | None:
    """Approximate solar generation (kWh) during [window_start, window_end).

    A simple average-power * duration estimate (not trapezoidal integration)
    - readings are already ~5 minutes apart in the source data, dense enough
    that the extra precision isn't worth the complexity here. Returns None
    if there were no readings in the window at all, rather than silently
    treating an empty window as 0 generation (which would wrongly pull a
    regression fit toward "no sun that day" instead of just excluding it).
    """
    in_window = [power_kw for ts, power_kw in pv_readings if window_start <= ts < window_end]
    if not in_window:
        return None
    return (sum(in_window) / len(in_window)) * horizon_hours


def fit_generation_drift_correction(
    samples: list[SocDriftSample], min_paired_samples: int
) -> tuple[float, float] | None:
    """Fit a simple linear correction: SoC drift as a function of PV generation.

    Ordinary least squares over the (pv_generation_kwh, drift_percent) pairs
    among `samples` that have PV data - a single predictor, deliberately not
    a full model, so a forecast generation figure can nudge the historical
    average drift toward what today's specific weather implies rather than
    only ever looking backward at the month's typical day.

    Returns:
        (slope, intercept) for drift_percent ~= intercept + slope * kwh, or
        None if there are fewer than min_paired_samples usable pairs, or the
        generation values have no spread to fit against (e.g. every matched
        day happened to generate the same amount - a real possibility with a
        small sample, not just a degenerate-input edge case).

    Examples:
        >>> from src.core_logic.battery_evening_prediction_logic import SocDriftSample
        >>> samples = [
        ...     SocDriftSample("2026-01-01", 50.0, 40.0, pv_generation_kwh=1.0),
        ...     SocDriftSample("2026-01-02", 50.0, 60.0, pv_generation_kwh=5.0),
        ... ]
        >>> fit = fit_generation_drift_correction(samples, min_paired_samples=2)
        >>> round(fit[0], 2)  # slope: +5pp drift per +1kWh generation here
        5.0

    """
    pairs = [
        (sample.pv_generation_kwh, sample.drift_percent)
        for sample in samples
        if sample.pv_generation_kwh is not None
    ]
    if len(pairs) < min_paired_samples:
        return None

    xs = [kwh for kwh, _ in pairs]
    ys = [drift for _, drift in pairs]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    variance_x = sum((x - mean_x) ** 2 for x in xs)
    if variance_x == 0:
        return None

    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / variance_x
    intercept = mean_y - slope * mean_x
    return slope, intercept


def predict_evening_soc(
    current_soc_percent: float,
    historical_records: list[dict],
    trigger_hour: float,
    horizon_hours: float,
    month: int,
    min_sample_days: int = DEFAULT_MIN_SAMPLE_DAYS,
    forecast_generation_kwh: float | None = None,
) -> EveningSocPrediction:
    """Predict SoC `horizon_hours` after `trigger_hour`, from today's reading.

    Args:
        current_soc_percent: Today's live SoC reading at (or near) trigger_hour.
        historical_records: Records shaped like solax_historical_data.json's
            "data" list.
        trigger_hour: Local hour (0-23) the prediction is anchored to.
        horizon_hours: Hours ahead of trigger_hour to predict for (e.g.
            hotwater_automation.force_heat_max_duration_hours).
        month: Calendar month (1-12) to restrict historical days to.
        min_sample_days: Minimum number of matched historical days required
            to produce a prediction; below this, predicted_soc_percent is
            None so the caller can fall back to a live-only decision instead
            of trusting a prediction built from too little data. Also used
            as the minimum number of PV-paired samples required to fit
            forecast_generation_kwh's correction.
        forecast_generation_kwh: Today's forecast solar generation (kWh) for
            the trigger-to-horizon window (scripts/solar_forecast_predictor.py's
            hourly_kw, summed over that window), or None to skip the
            correction and use the plain historical average - the default,
            and always the fallback when there isn't enough PV-paired
            historical data to fit it.

    Returns:
        EveningSocPrediction with predicted_soc_percent (or None) and a
        human-readable reason.

    Examples:
        >>> current_soc_percent = 70.0
        >>> records = [
        ...     {"timestamp": f"2026-01-{d:02d} 18:00:00", "soc_percent": 80.0}
        ...     for d in range(1, 8)
        ... ] + [
        ...     {"timestamp": f"2026-01-{d:02d} 21:00:00", "soc_percent": 60.0}
        ...     for d in range(1, 8)
        ... ]
        >>> result = predict_evening_soc(current_soc_percent, records, 18, 3.0, 1)
        >>> result.predicted_soc_percent
        50.0

    """
    samples = compute_historical_soc_drift_samples(
        historical_records, trigger_hour, horizon_hours, month
    )

    if len(samples) < min_sample_days:
        return EveningSocPrediction(
            predicted_soc_percent=None,
            sample_count=len(samples),
            average_drift_percent=None,
            applied_drift_percent=None,
            reason=(
                f"Only {len(samples)} historical day(s) with usable data for month "
                f"{month} (need >= {min_sample_days}) - not enough to predict"
            ),
        )

    average_drift_percent = sum(sample.drift_percent for sample in samples) / len(samples)
    applied_drift_percent = average_drift_percent
    reason_suffix = ""

    if forecast_generation_kwh is not None:
        fit = fit_generation_drift_correction(samples, min_paired_samples=min_sample_days)
        if fit is not None:
            slope, intercept = fit
            applied_drift_percent = intercept + slope * forecast_generation_kwh
            reason_suffix = (
                f"; corrected for forecast solar generation {forecast_generation_kwh:.1f}kWh "
                f"(historical average {average_drift_percent:+.1f}pp -> "
                f"{applied_drift_percent:+.1f}pp)"
            )

    predicted_soc_percent = max(0.0, min(100.0, current_soc_percent + applied_drift_percent))

    return EveningSocPrediction(
        predicted_soc_percent=predicted_soc_percent,
        sample_count=len(samples),
        average_drift_percent=average_drift_percent,
        applied_drift_percent=applied_drift_percent,
        reason=(
            f"Predicted from {len(samples)} historical day(s) in month {month}: "
            f"average SoC drift {average_drift_percent:+.1f}pp from {trigger_hour}:00 "
            f"to +{horizon_hours:.1f}h, applied to current {current_soc_percent:.0f}%"
            f"{reason_suffix}"
        ),
    )
