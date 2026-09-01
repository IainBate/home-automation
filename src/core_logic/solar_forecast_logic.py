"""Solar generation forecasting - feature engineering and model training/prediction.

Unlike battery_evening_prediction_logic.py's deliberately simple "analog day"
statistic, this trains an actual regression model (scikit-learn
RandomForestRegressor) on this system's own historical PV output joined with
historical weather - Solcast's generic forecast doesn't capture this roof's
specific shading/orientation, and a model trained on this system's own data
can.

Design Principles (mirrors ohme_charging_logic.py / hotwater_decision_logic.py
where practical): pure functions taking already-loaded data in and returning
results out - no file I/O or network calls here, so this is testable with
small synthetic datasets. The one exception to "no I/O" is the scikit-learn
model object itself, which callers persist/load (see
scripts/solar_forecast_trainer.py, scripts/solar_forecast_predictor.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, r2_score

FEATURE_COLUMNS = ["hour_of_day", "day_of_year", "shortwave_radiation", "cloud_cover", "temperature_2m"]

DEFAULT_N_ESTIMATORS = 200
DEFAULT_MAX_DEPTH = 12
DEFAULT_RANDOM_STATE = 42
DEFAULT_HOLDOUT_FRACTION = 0.15
DEFAULT_MIN_TRAINING_ROWS = 200  # A little under 2 weeks of daylight hours


@dataclass
class TrainingResult:
    """A trained solar forecast model plus its holdout validation metrics.

    Attributes:
        model: Fitted RandomForestRegressor, ready for predict_hourly_kw().
        mae_kw: Mean absolute error (kW) on the chronological holdout split.
        r2: R-squared on the same holdout split (1.0 = perfect, 0.0 = no
            better than predicting the mean).
        train_rows: Number of rows the model was actually fit on.
        holdout_rows: Number of rows held out for validation.

    """

    model: RandomForestRegressor
    mae_kw: float
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


def build_training_rows(
    pv_records: list[dict[str, Any]], weather_records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Join hourly-aggregated PV output with historical weather into training rows.

    Args:
        pv_records: solax_historical_data.json's "data" list.
        weather_records: weather_client.fetch_historical_weather_hourly() output.

    Returns:
        One dict per hour with a matching PV reading and complete weather
        data: {"timestamp", "hour_of_day", "day_of_year", "shortwave_radiation",
        "cloud_cover", "temperature_2m", "pv_power_kw"} - the last being the
        training target. Hours missing either PV data or any weather field
        are skipped rather than imputed, to avoid teaching the model on made-up
        values.

    """
    hourly_pv = aggregate_pv_to_hourly(pv_records)

    rows = []
    for weather_record in weather_records:
        hour_key = _hour_key_from_weather_timestamp(weather_record.get("timestamp", ""))
        if hour_key is None or hour_key not in hourly_pv:
            continue

        radiation = weather_record.get("shortwave_radiation")
        cloud_cover = weather_record.get("cloud_cover")
        temperature = weather_record.get("temperature_2m")
        if radiation is None or cloud_cover is None or temperature is None:
            continue

        hour_dt = datetime.strptime(hour_key, "%Y-%m-%d %H:00")
        rows.append(
            {
                "timestamp": hour_key,
                "hour_of_day": hour_dt.hour,
                "day_of_year": hour_dt.timetuple().tm_yday,
                "shortwave_radiation": float(radiation),
                "cloud_cover": float(cloud_cover),
                "temperature_2m": float(temperature),
                "pv_power_kw": hourly_pv[hour_key],
            }
        )

    rows.sort(key=lambda row: row["timestamp"])
    return rows


def build_forecast_rows(weather_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert a weather forecast into feature rows ready for predict_hourly_kw().

    Args:
        weather_records: weather_client.fetch_forecast_weather_hourly() output.

    Returns:
        One dict per hour with complete weather data: {"timestamp",
        "hour_of_day", "day_of_year", "shortwave_radiation", "cloud_cover",
        "temperature_2m"}. Hours with any missing weather field are skipped.

    """
    rows = []
    for weather_record in weather_records:
        hour_key = _hour_key_from_weather_timestamp(weather_record.get("timestamp", ""))
        radiation = weather_record.get("shortwave_radiation")
        cloud_cover = weather_record.get("cloud_cover")
        temperature = weather_record.get("temperature_2m")
        if hour_key is None or radiation is None or cloud_cover is None or temperature is None:
            continue

        hour_dt = datetime.strptime(hour_key, "%Y-%m-%d %H:00")
        rows.append(
            {
                "timestamp": hour_key,
                "hour_of_day": hour_dt.hour,
                "day_of_year": hour_dt.timetuple().tm_yday,
                "shortwave_radiation": float(radiation),
                "cloud_cover": float(cloud_cover),
                "temperature_2m": float(temperature),
            }
        )
    return rows


def train_model(
    training_rows: list[dict[str, Any]],
    *,
    n_estimators: int = DEFAULT_N_ESTIMATORS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    random_state: int = DEFAULT_RANDOM_STATE,
    holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION,
) -> TrainingResult:
    """Train a RandomForestRegressor on build_training_rows() output.

    The holdout split is the most recent `holdout_fraction` of rows by
    timestamp (not a random shuffle) - adjacent hours are highly correlated,
    so a random split would let the model "cheat" by seeing near-duplicates
    of holdout rows during training and understate real-world error.

    Args:
        training_rows: Output of build_training_rows(), already sorted by timestamp.
        n_estimators: Number of trees.
        max_depth: Max tree depth (bounds overfitting on a training set this size).
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

    x_train = [[row[col] for col in FEATURE_COLUMNS] for row in train_rows]
    y_train = [row["pv_power_kw"] for row in train_rows]
    x_holdout = [[row[col] for col in FEATURE_COLUMNS] for row in holdout_rows]
    y_holdout = [row["pv_power_kw"] for row in holdout_rows]

    model = RandomForestRegressor(
        n_estimators=n_estimators,
        max_depth=max_depth,
        random_state=random_state,
        n_jobs=-1,
    )
    model.fit(x_train, y_train)

    predictions = model.predict(x_holdout)
    return TrainingResult(
        model=model,
        mae_kw=mean_absolute_error(y_holdout, predictions),
        r2=r2_score(y_holdout, predictions),
        train_rows=len(train_rows),
        holdout_rows=len(holdout_rows),
    )


def predict_hourly_kw(
    model: RandomForestRegressor, forecast_rows: list[dict[str, Any]]
) -> list[float]:
    """Predict PV power (kW, equivalently that hour's kWh) for each forecast hour.

    Args:
        model: A fitted RandomForestRegressor from train_model().
        forecast_rows: Rows shaped like build_training_rows() output but
            without "pv_power_kw" (the value being predicted).

    Returns:
        One non-negative kW prediction per row, in the same order. Hours with
        zero forecast irradiance are forced to exactly 0.0 regardless of what
        the model predicts, as a defensive floor against nighttime artifacts.

    """
    if not forecast_rows:
        return []

    x = [[row[col] for col in FEATURE_COLUMNS] for row in forecast_rows]
    raw_predictions = model.predict(x)

    return [
        0.0 if row["shortwave_radiation"] <= 0 else max(0.0, float(prediction))
        for row, prediction in zip(forecast_rows, raw_predictions)
    ]
