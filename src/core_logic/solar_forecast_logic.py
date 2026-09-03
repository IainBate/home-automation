"""Solar generation forecasting - feature engineering and model training/prediction.

Unlike battery_evening_prediction_logic.py's deliberately simple "analog day"
statistic, this trains an actual regression model (scikit-learn
ExtraTreesRegressor) on this system's own historical PV output joined with
historical weather - Solcast's generic forecast doesn't capture this roof's
specific shading/orientation, and a model trained on this system's own data
can.

Forecasts daily totals (kWh), not hourly power - a 2026-09 evaluation
(ml_forecast_eval/RESULTS.md) found data/solax_historical_data.json was
synthetic for 2026-01-01..2026-08-31 (traced to a bulk import via a since-
confirmed-broken SolaX Cloud API endpoint - see solax_cloud_client.py's
module docstring), so there is no trustworthy *hourly* ground truth for that
whole period. The SolaX Cloud web portal only exposes daily totals, manually
collected into data/solax_cloud_daily_history.csv. Consumers that want an
hourly shape (scripts/battery_evening_predictor.py, the dashboard) get one
via distribute_daily_kwh_to_hourly(), which distributes the validated daily
total across hours using the forecast's own radiation curve - see that
function's docstring.

Design Principles (mirrors ohme_charging_logic.py / hotwater_decision_logic.py
where practical): pure functions taking already-loaded data in and returning
results out - no file I/O or network calls here, so this is testable with
small synthetic datasets. The one exception to "no I/O" is the scikit-learn
model object itself, which callers persist/load (see
scripts/solar_forecast_trainer.py, scripts/solar_forecast_predictor.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from astral import Observer
from astral.sun import elevation as sun_elevation
from astral.sun import sun as sun_info
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.metrics import mean_absolute_error, r2_score

DAILY_FEATURE_COLUMNS = [
    "day_of_year",
    "shortwave_radiation_sum",
    "cloud_cover_mean",
    "temperature_mean",
    "max_elevation",
    "day_length_hours",
    "direct_radiation_sum",
    "diffuse_radiation_sum",
    "cloud_cover_low_mean",
    "cloud_cover_mid_mean",
    "cloud_cover_high_mean",
]

# Radiation fields are summed across a day's hours (an hour's mean W/m2 is
# numerically that hour's Wh/m2, so summing 24 hours gives a daily insolation
# proxy - same principle aggregate_pv_to_hourly() uses for PV power/energy).
# Cloud/temperature fields are averaged instead - a day's *total* cloud cover
# has no physical meaning.
_SUM_WEATHER_FIELDS = ("shortwave_radiation", "direct_radiation", "diffuse_radiation")
_MEAN_WEATHER_FIELDS = {
    "cloud_cover": "cloud_cover_mean",
    "cloud_cover_low": "cloud_cover_low_mean",
    "cloud_cover_mid": "cloud_cover_mid_mean",
    "cloud_cover_high": "cloud_cover_high_mean",
    "temperature_2m": "temperature_mean",
}

DEFAULT_N_ESTIMATORS = 300
DEFAULT_MAX_DEPTH = 8
DEFAULT_MIN_SAMPLES_LEAF = 1
DEFAULT_RANDOM_STATE = 42
DEFAULT_HOLDOUT_FRACTION = 0.15
# ~2 months of days - ml_forecast_eval/RESULTS.md trained/validated on 365
# daily rows; this is a floor below which a holdout split isn't meaningful,
# not a target.
DEFAULT_MIN_TRAINING_ROWS = 60


@dataclass
class TrainingResult:
    """A trained solar forecast model plus its holdout validation metrics.

    Attributes:
        model: Fitted ExtraTreesRegressor, ready for predict_daily_kwh().
        mae_kwh: Mean absolute error (kWh/day) on the chronological holdout split.
        r2: R-squared on the same holdout split (1.0 = perfect, 0.0 = no
            better than predicting the mean).
        train_rows: Number of rows the model was actually fit on.
        holdout_rows: Number of rows held out for validation.

    """

    model: ExtraTreesRegressor
    mae_kwh: float
    r2: float
    train_rows: int
    holdout_rows: int


def _hour_key_from_solax_timestamp(timestamp: str) -> str | None:
    """"YYYY-MM-DD HH:MM:SS" (solax_historical_data.json) -> "YYYY-MM-DD HH:00"."""
    try:
        parsed = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    return parsed.strftime("%Y-%m-%d %H:00")


def _hour_key_from_weather_timestamp(timestamp: str) -> str | None:
    """"YYYY-MM-DDTHH:MM" (Open-Meteo) -> "YYYY-MM-DD HH:00"."""
    try:
        parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        return None
    return parsed.strftime("%Y-%m-%d %H:00")


def aggregate_pv_to_hourly(pv_records: list[dict[str, Any]]) -> dict[str, float]:
    """Average solax_historical_data.json's 5-minute pv_power_kw readings into hourly buckets.

    An hour's mean power (kW) is numerically equal to that hour's energy
    (kWh), which is what makes summing a day's hourly predictions later give
    a sensible daily total.

    Args:
        pv_records: Records shaped like solax_historical_data.json's "data"
            list - each a dict with "timestamp" and "pv_power_kw". Malformed
            records are skipped.

    Returns:
        Dict of {"YYYY-MM-DD HH:00": mean_pv_power_kw}.

    """
    buckets: dict[str, list[float]] = {}
    for record in pv_records:
        hour_key = _hour_key_from_solax_timestamp(record.get("timestamp", ""))
        pv_power_kw = record.get("pv_power_kw")
        if hour_key is None or pv_power_kw is None:
            continue
        buckets.setdefault(hour_key, []).append(float(pv_power_kw))

    return {hour_key: sum(values) / len(values) for hour_key, values in buckets.items()}


def compute_actual_daily_kwh(pv_records: list[dict[str, Any]], date_str: str) -> float | None:
    """Get a completed day's actual generation from historical PV readings.

    Used to score a past forecast against what really happened (see
    scripts/solar_forecast_predictor.py's yesterday_actual_kwh/
    yesterday_error_kwh) and to extend the training set with new real data as
    it accumulates (see merge_daily_pv_history()).

    Prefers the day's last record's "yield_today_kwh" (solax_cloud_client.py's
    solax_cloud_get_realtime_snapshot() - the API's own cumulative
    today-so-far total) when present, since that's a ground-truth figure
    independent of how many samples exist for the day. Falls back to
    summing hourly-averaged "pv_power_kw" (aggregate_pv_to_hourly() - an
    hour's mean kW is numerically that hour's kWh, same as in
    build_daily_training_rows()) for older records that predate that field, or
    any other source that never sets it - a day with only a late/partial
    sample would otherwise silently undercount instead of falling back.

    Args:
        pv_records: solax_historical_data.json's "data" list.
        date_str: "YYYY-MM-DD" to sum.

    Returns:
        kWh generated that day, or None if there's no historical data for it
        at all - or none that's actually usable (vs. a misleading 0.0, which
        would be scored as a real "the sun produced nothing" reading).

    """
    # `r.get("timestamp") or ""` rather than `r.get("timestamp", "")`: the
    # two-arg default only applies when the key is ABSENT, so a record with
    # an explicit {"timestamp": None} would reach .startswith() as None and
    # raise AttributeError, crashing solar_forecast_predictor.py's cron run
    # (its call site has no try/except). aggregate_pv_to_hourly() below
    # already skips such records rather than raising, so this filter must be
    # at least as forgiving as the fallback it guards.
    day_records = [r for r in pv_records if (r.get("timestamp") or "").startswith(date_str)]
    if not day_records:
        return None

    last_record = max(day_records, key=lambda r: r.get("timestamp") or "")
    yield_today_kwh = last_record.get("yield_today_kwh")
    if yield_today_kwh is not None:
        return yield_today_kwh

    hourly = aggregate_pv_to_hourly(day_records)
    if not hourly:
        # Records exist for this date but none carry usable pv_power_kw (all
        # None/malformed), so there is nothing to sum. sum({}.values()) is
        # 0.0, which this function's contract explicitly rules out - a real
        # zero and "no usable data" must stay distinguishable, since the
        # caller scores the difference against a forecast.
        return None
    return sum(hourly.values())


def merge_daily_pv_history(
    seed_daily_kwh: dict[str, float], pv_history_records: list[dict[str, Any]]
) -> dict[str, float]:
    """Combine the bundled SolaX Cloud seed dataset with accumulating local telemetry.

    data/solax_cloud_daily_history.csv (loaded by the caller into
    seed_daily_kwh) covers 2025-09-01..2026-08-31, manually collected from the
    SolaX Cloud portal after data/solax_historical_data.json was found to be
    synthetic for that whole period (see ml_forecast_eval/RESULTS.md).
    scripts/solax_realtime_logger.py has produced genuine telemetry from
    2026-09-01 onward, so this folds in compute_actual_daily_kwh() for every
    date in pv_history_records not already covered by the seed set - the
    training set grows on its own as real data accumulates, with no more
    manual collection needed.

    Args:
        seed_daily_kwh: {"YYYY-MM-DD": kwh} loaded from the bundled seed CSV.
        pv_history_records: solax_historical_data.json's "data" list.

    Returns:
        Combined {"YYYY-MM-DD": kwh}. The seed value wins on any overlapping
        date (it's the known-real source for that period; the local file is
        not, for dates it already covers).

    """
    candidate_dates = {
        (record.get("timestamp") or "")[:10]
        for record in pv_history_records
        if record.get("timestamp")
    }

    combined: dict[str, float] = {}
    for date_str in candidate_dates:
        if len(date_str) != 10 or date_str in seed_daily_kwh:
            continue
        actual_kwh = compute_actual_daily_kwh(pv_history_records, date_str)
        if actual_kwh is not None:
            combined[date_str] = actual_kwh

    combined.update(seed_daily_kwh)
    return combined


def solar_geometry_for_date(target_date: date, latitude: float, longitude: float, timezone: str) -> dict[str, float]:
    """Deterministic per-day solar geometry (astral) - day length and max elevation.

    Known exactly in advance, unlike a weather forecast, and
    ml_forecast_eval/RESULTS.md found these carry ~44% combined feature
    importance in the trained model despite costing nothing to compute
    (astral is pure Python, no numpy/scipy/pandas transitive deps - see
    requirements.txt).

    Args:
        target_date: The local calendar date to compute geometry for.
        latitude: Site latitude (decimal degrees).
        longitude: Site longitude (decimal degrees).
        timezone: IANA timezone name (e.g. "Europe/London").

    Returns:
        {"max_elevation": degrees at solar noon, "day_length_hours": sunset
        minus sunrise}. {"max_elevation": 0.0, "day_length_hours": 0.0} for a
        polar day/night where astral has no sunrise/sunset - not reachable at
        UK latitudes, but keeps this from crashing if config.yaml's location
        is ever changed.

    """
    observer = Observer(latitude=latitude, longitude=longitude)
    try:
        info = sun_info(observer, date=target_date, tzinfo=timezone)
    except ValueError:
        return {"max_elevation": 0.0, "day_length_hours": 0.0}

    day_length_hours = (info["sunset"] - info["sunrise"]).total_seconds() / 3600.0
    max_elevation = sun_elevation(observer, info["noon"])
    return {"max_elevation": max_elevation, "day_length_hours": day_length_hours}


def aggregate_weather_hourly_to_daily(weather_records: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Aggregate hourly weather into per-day features for the daily PV forecast model.

    Args:
        weather_records: weather_client.py fetch output - one dict per hour
            with "timestamp" ("YYYY-MM-DDTHH:MM") plus the radiation/cloud/
            temperature fields in _SUM_WEATHER_FIELDS/_MEAN_WEATHER_FIELDS.

    Returns:
        Dict of {"YYYY-MM-DD": {feature_name: value}} using the
        "*_sum"/"*_mean" names in DAILY_FEATURE_COLUMNS. A day with no usable
        shortwave_radiation reading at all is omitted rather than given a
        misleading 0.0 - build_daily_training_rows()/build_daily_forecast_rows()
        then naturally skip it.

    """
    buckets: dict[str, dict[str, list[float]]] = {}
    all_fields = _SUM_WEATHER_FIELDS + tuple(_MEAN_WEATHER_FIELDS)
    for record in weather_records:
        date_str = (record.get("timestamp") or "")[:10]
        if len(date_str) != 10:
            continue
        bucket = buckets.setdefault(date_str, {field: [] for field in all_fields})
        for field in all_fields:
            value = record.get(field)
            if value is not None:
                bucket[field].append(float(value))

    daily: dict[str, dict[str, float]] = {}
    for date_str, fields in buckets.items():
        if not fields["shortwave_radiation"]:
            continue
        row = {f"{field}_sum": sum(fields[field]) for field in _SUM_WEATHER_FIELDS}
        for source_field, out_name in _MEAN_WEATHER_FIELDS.items():
            values = fields[source_field]
            row[out_name] = sum(values) / len(values) if values else 0.0
        daily[date_str] = row

    return daily


def build_daily_training_rows(
    daily_pv_kwh: dict[str, float],
    weather_records: list[dict[str, Any]],
    latitude: float,
    longitude: float,
    timezone: str,
) -> list[dict[str, Any]]:
    """Join daily PV totals with daily-aggregated weather and solar geometry.

    Args:
        daily_pv_kwh: {"YYYY-MM-DD": actual_kwh} - see merge_daily_pv_history().
        weather_records: weather_client.fetch_historical_weather_hourly() output.
        latitude/longitude/timezone: for solar_geometry_for_date().

    Returns:
        One dict per day with a matching PV total and complete weather: all
        of DAILY_FEATURE_COLUMNS plus "date" and "pv_kwh" (the training
        target). Days missing either are skipped rather than imputed, to
        avoid teaching the model on made-up values.

    """
    daily_weather = aggregate_weather_hourly_to_daily(weather_records)

    rows = []
    for date_str, pv_kwh in daily_pv_kwh.items():
        weather = daily_weather.get(date_str)
        if weather is None:
            continue
        try:
            parsed_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        geometry = solar_geometry_for_date(parsed_date, latitude, longitude, timezone)
        rows.append(
            {
                "date": date_str,
                "day_of_year": parsed_date.timetuple().tm_yday,
                **weather,
                **geometry,
                "pv_kwh": pv_kwh,
            }
        )

    rows.sort(key=lambda row: row["date"])
    return rows


def build_daily_forecast_rows(
    weather_records: list[dict[str, Any]], latitude: float, longitude: float, timezone: str
) -> list[dict[str, Any]]:
    """Convert an hourly weather forecast into one feature row per forecast day.

    Args:
        weather_records: weather_client.fetch_forecast_weather_hourly() output
            (default: today + tomorrow).
        latitude/longitude/timezone: for solar_geometry_for_date().

    Returns:
        One dict per day with complete weather data: "date" plus all of
        DAILY_FEATURE_COLUMNS, ready for predict_daily_kwh(). Days with no
        usable radiation reading at all are skipped.

    """
    daily_weather = aggregate_weather_hourly_to_daily(weather_records)

    rows = []
    for date_str, weather in daily_weather.items():
        try:
            parsed_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        geometry = solar_geometry_for_date(parsed_date, latitude, longitude, timezone)
        rows.append(
            {
                "date": date_str,
                "day_of_year": parsed_date.timetuple().tm_yday,
                **weather,
                **geometry,
            }
        )

    rows.sort(key=lambda row: row["date"])
    return rows


def train_model(
    training_rows: list[dict[str, Any]],
    *,
    n_estimators: int = DEFAULT_N_ESTIMATORS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    min_samples_leaf: int = DEFAULT_MIN_SAMPLES_LEAF,
    random_state: int = DEFAULT_RANDOM_STATE,
    holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION,
) -> TrainingResult:
    """Train an ExtraTreesRegressor on build_daily_training_rows() output.

    ExtraTreesRegressor with this configuration was the most accurate of six
    model types and four feature-set tiers compared in ml_forecast_eval/
    RESULTS.md (4.89 kWh CV MAE vs 5.83 kWh for a RandomForestRegressor on
    the previous, narrower feature set) - hyperparameter tuning barely moved
    it further, so the defaults here are already close to that evaluation's
    tuned optimum for a dataset this size.

    The holdout split is the most recent `holdout_fraction` of rows by date
    (not a random shuffle) - adjacent days are correlated, so a random split
    would let the model "cheat" by seeing near-duplicates of holdout rows
    during training and understate real-world error.

    Args:
        training_rows: Output of build_daily_training_rows(), already sorted by date.
        n_estimators: Number of trees.
        max_depth: Max tree depth (bounds overfitting on a training set this size).
        min_samples_leaf: Minimum samples per leaf (also bounds overfitting).
        random_state: Fixed for reproducible training runs.
        holdout_fraction: Fraction of rows (chronologically last) held out for validation.

    Returns:
        TrainingResult with the fitted model and holdout metrics.

    Raises:
        ValueError: If there are too few training rows to fit and validate meaningfully.

    """
    if len(training_rows) < DEFAULT_MIN_TRAINING_ROWS:
        msg = (
            f"Only {len(training_rows)} training rows available "
            f"(need >= {DEFAULT_MIN_TRAINING_ROWS}) - not enough historical "
            "data to train a solar forecast model yet"
        )
        raise ValueError(msg)

    split_index = int(len(training_rows) * (1 - holdout_fraction))
    split_index = min(max(split_index, 1), len(training_rows) - 1)
    train_rows, holdout_rows = training_rows[:split_index], training_rows[split_index:]

    x_train = [[row[col] for col in DAILY_FEATURE_COLUMNS] for row in train_rows]
    y_train = [row["pv_kwh"] for row in train_rows]
    x_holdout = [[row[col] for col in DAILY_FEATURE_COLUMNS] for row in holdout_rows]
    y_holdout = [row["pv_kwh"] for row in holdout_rows]

    model = ExtraTreesRegressor(
        n_estimators=n_estimators,
        max_depth=max_depth,
        min_samples_leaf=min_samples_leaf,
        random_state=random_state,
        n_jobs=-1,
    )
    model.fit(x_train, y_train)

    predictions = model.predict(x_holdout)
    return TrainingResult(
        model=model,
        mae_kwh=mean_absolute_error(y_holdout, predictions),
        r2=r2_score(y_holdout, predictions),
        train_rows=len(train_rows),
        holdout_rows=len(holdout_rows),
    )


def predict_daily_kwh(model: ExtraTreesRegressor, forecast_rows: list[dict[str, Any]]) -> list[float]:
    """Predict total PV generation (kWh) for each forecast day.

    Args:
        model: A fitted ExtraTreesRegressor from train_model().
        forecast_rows: Rows shaped like build_daily_forecast_rows() output.

    Returns:
        One non-negative kWh prediction per row, in the same order.

    """
    if not forecast_rows:
        return []

    x = [[row[col] for col in DAILY_FEATURE_COLUMNS] for row in forecast_rows]
    raw_predictions = model.predict(x)
    return [max(0.0, float(prediction)) for prediction in raw_predictions]


def distribute_daily_kwh_to_hourly(
    daily_kwh: float, hourly_weather_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Split a day's predicted total kWh across its hours, shaped by forecast radiation.

    The trained model only predicts a daily total - the only granularity
    data/solax_cloud_daily_history.csv has trustworthy ground truth for (see
    this module's docstring). scripts/battery_evening_predictor.py and the
    dashboard still want an hourly breakdown, so this distributes the
    accuracy-checked daily total across hours in proportion to each hour's
    forecast shortwave_radiation share, rather than predicting each hour
    independently and hoping the results sum to something sensible.

    Args:
        daily_kwh: This day's predict_daily_kwh() output.
        hourly_weather_rows: weather_client fetch output for this day's hours
            (e.g. pre-filtered to one date) - each needs "timestamp" and
            "shortwave_radiation".

    Returns:
        One {"timestamp": "YYYY-MM-DD HH:00", "predicted_kw": float} per hour
        with a parseable timestamp, summing to daily_kwh (an hour's kW here
        is numerically that hour's kWh share, same convention as the rest of
        this module). Hours with zero/missing radiation get exactly 0.0. If
        every hour has zero radiation (e.g. malformed input), the total is
        split evenly instead, so a nonzero daily_kwh is never silently lost.

    """
    hour_keys = []
    radiations = []
    for row in hourly_weather_rows:
        hour_key = _hour_key_from_weather_timestamp(row.get("timestamp", ""))
        if hour_key is None:
            continue
        hour_keys.append(hour_key)
        radiations.append(max(0.0, float(row.get("shortwave_radiation") or 0.0)))

    if not hour_keys:
        return []

    total_radiation = sum(radiations)
    if total_radiation > 0:
        shares = [radiation / total_radiation for radiation in radiations]
    else:
        shares = [1.0 / len(hour_keys)] * len(hour_keys)

    return [
        {"timestamp": hour_key, "predicted_kw": round(daily_kwh * share, 3)}
        for hour_key, share in zip(hour_keys, shares)
    ]
