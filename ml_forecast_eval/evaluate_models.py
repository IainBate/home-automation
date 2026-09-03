"""Compare model + feature-set combinations for daily solar PV forecasting.

Methodology:
- Expanding-window chronological CV (train on everything before a month,
  predict that month) across the 8 latest months, so every fold is a
  realistic "predict the unseen future" test - never random-shuffled, since
  adjacent days are correlated and randomizing would leak information.
- Also reports a single last-15% holdout (matches production's current
  train_model() methodology) for direct comparability with the deployed
  model's own reported metrics.
- Tracks train/predict wall-clock time alongside accuracy, since the target
  deploy is a Pi4 doing this weekly/hourly, not a training cluster.
"""
from __future__ import annotations

import time
import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.neural_network import MLPRegressor
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")

DATA_PATH = "data/daily_training_dataset.csv"
TARGET = "daily_pv_yield_kwh"

FEATURE_TIERS = {
    "A_baseline (current prod analog)": [
        "day_of_year", "shortwave_radiation_sum", "cloud_cover_mean", "temperature_mean",
    ],
    "B_+solar_geometry": [
        "day_of_year", "shortwave_radiation_sum", "cloud_cover_mean", "temperature_mean",
        "max_elevation", "day_length_hours",
    ],
    "C_+irradiance_split+cloud_levels": [
        "day_of_year", "shortwave_radiation_sum", "cloud_cover_mean", "temperature_mean",
        "max_elevation", "day_length_hours",
        "direct_radiation_sum", "diffuse_radiation_sum",
        "cloud_cover_low_mean", "cloud_cover_mid_mean", "cloud_cover_high_mean",
    ],
    "D_+full_weather+clearsky_index": [
        "day_of_year", "shortwave_radiation_sum", "cloud_cover_mean", "temperature_mean",
        "max_elevation", "day_length_hours",
        "direct_radiation_sum", "diffuse_radiation_sum",
        "cloud_cover_low_mean", "cloud_cover_mid_mean", "cloud_cover_high_mean",
        "humidity_mean", "precipitation_sum", "wind_speed_mean", "clear_sky_index",
    ],
}


@dataclass
class Result:
    model_name: str
    feature_tier: str
    cv_mae: float
    cv_rmse: float
    cv_r2: float
    holdout_mae: float
    holdout_r2: float
    train_time_ms: float
    predict_time_ms: float


def climatology_predict(y_train: pd.Series, doy_train: pd.Series, doy_test: pd.Series, window: int = 15) -> np.ndarray:
    """Baseline: mean actual generation for +-window days-of-year across training history."""
    preds = []
    for doy in doy_test:
        dist = np.minimum(np.abs(doy_train - doy), 365 - np.abs(doy_train - doy))
        mask = dist <= window
        preds.append(y_train[mask].mean() if mask.any() else y_train.mean())
    return np.array(preds)


def make_models() -> dict:
    return {
        "Ridge": lambda: Ridge(alpha=1.0),
        "RandomForest (current prod)": lambda: RandomForestRegressor(
            n_estimators=200, max_depth=12, random_state=42, n_jobs=-1
        ),
        "ExtraTrees": lambda: ExtraTreesRegressor(n_estimators=200, max_depth=12, random_state=42, n_jobs=-1),
        "MLP (small)": lambda: MLPRegressor(
            hidden_layer_sizes=(32, 16), max_iter=2000, random_state=42, early_stopping=True
        ),
    }


def try_import_boosted():
    models = {}
    try:
        import lightgbm as lgb
        models["LightGBM"] = lambda: lgb.LGBMRegressor(
            n_estimators=200, max_depth=6, learning_rate=0.05, random_state=42, verbosity=-1
        )
    except ImportError:
        pass
    try:
        import xgboost as xgb
        models["XGBoost"] = lambda: xgb.XGBRegressor(
            n_estimators=200, max_depth=5, learning_rate=0.05, random_state=42, verbosity=0
        )
    except ImportError:
        pass
    return models


def expanding_window_cv(df: pd.DataFrame, features: list[str], model_factory, use_scaler: bool) -> tuple[float, float, float]:
    """Predict each of the last 8 months using only prior data. Returns (MAE, RMSE, R2) pooled across folds."""
    df = df.sort_values("date").reset_index(drop=True)
    months = sorted(df["date"].dt.to_period("M").unique())
    test_months = months[-8:] if len(months) > 8 else months[len(months) // 2:]

    all_true, all_pred = [], []
    for m in test_months:
        train_mask = df["date"].dt.to_period("M") < m
        test_mask = df["date"].dt.to_period("M") == m
        if train_mask.sum() < 30:
            continue
        x_train, y_train = df.loc[train_mask, features], df.loc[train_mask, TARGET]
        x_test, y_test = df.loc[test_mask, features], df.loc[test_mask, TARGET]

        if use_scaler:
            scaler = StandardScaler().fit(x_train)
            x_train = scaler.transform(x_train)
            x_test = scaler.transform(x_test)

        model = model_factory()
        model.fit(x_train, y_train)
        preds = model.predict(x_test)
        all_true.extend(y_test.tolist())
        all_pred.extend(preds.tolist())

    mae = mean_absolute_error(all_true, all_pred)
    rmse = mean_squared_error(all_true, all_pred) ** 0.5
    r2 = r2_score(all_true, all_pred)
    return mae, rmse, r2


def holdout_eval(df: pd.DataFrame, features: list[str], model_factory, use_scaler: bool) -> tuple[float, float, float, float]:
    """Single chronological last-15% holdout, matching production train_model()'s own methodology."""
    df = df.sort_values("date").reset_index(drop=True)
    split = int(len(df) * 0.85)
    x_train, y_train = df.loc[:split - 1, features], df.loc[:split - 1, TARGET]
    x_test, y_test = df.loc[split:, features], df.loc[split:, TARGET]

    if use_scaler:
        scaler = StandardScaler().fit(x_train)
        x_train = scaler.transform(x_train)
        x_test = scaler.transform(x_test)

    model = model_factory()
    t0 = time.perf_counter()
    model.fit(x_train, y_train)
    train_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    preds = model.predict(x_test)
    predict_ms = (time.perf_counter() - t0) * 1000

    mae = mean_absolute_error(y_test, preds)
    r2 = r2_score(y_test, preds)
    return mae, r2, train_ms, predict_ms


def main() -> None:
    df = pd.read_csv(DATA_PATH, parse_dates=["date"])

    all_models = {**make_models(), **try_import_boosted()}
    use_scaler_for = {"Ridge", "MLP (small)"}

    results: list[Result] = []
    for tier_name, features in FEATURE_TIERS.items():
        for model_name, factory in all_models.items():
            use_scaler = model_name in use_scaler_for
            cv_mae, cv_rmse, cv_r2 = expanding_window_cv(df, features, factory, use_scaler)
            h_mae, h_r2, train_ms, predict_ms = holdout_eval(df, features, factory, use_scaler)
            results.append(Result(
                model_name=model_name, feature_tier=tier_name,
                cv_mae=cv_mae, cv_rmse=cv_rmse, cv_r2=cv_r2,
                holdout_mae=h_mae, holdout_r2=h_r2,
                train_time_ms=train_ms, predict_time_ms=predict_ms,
            ))
            print(f"{tier_name:38s} | {model_name:28s} | CV MAE={cv_mae:6.2f} kWh  CV R2={cv_r2:6.3f}  "
                  f"| holdout MAE={h_mae:6.2f} R2={h_r2:6.3f} | train={train_ms:7.1f}ms predict={predict_ms:5.2f}ms")

    # Climatology + persistence baselines (feature-set independent, computed once)
    df_sorted = df.sort_values("date").reset_index(drop=True)
    months = sorted(df_sorted["date"].dt.to_period("M").unique())
    test_months = months[-8:]
    clim_true, clim_pred = [], []
    for m in test_months:
        train_mask = df_sorted["date"].dt.to_period("M") < m
        test_mask = df_sorted["date"].dt.to_period("M") == m
        if train_mask.sum() < 30:
            continue
        preds = climatology_predict(
            df_sorted.loc[train_mask, TARGET].reset_index(drop=True),
            df_sorted.loc[train_mask, "day_of_year"].reset_index(drop=True),
            df_sorted.loc[test_mask, "day_of_year"],
        )
        clim_true.extend(df_sorted.loc[test_mask, TARGET].tolist())
        clim_pred.extend(preds.tolist())
    clim_mae = mean_absolute_error(clim_true, clim_pred)
    clim_r2 = r2_score(clim_true, clim_pred)
    print(f"\n{'BASELINE: climatology (+-15 day-of-year avg)':38s} | {'':28s} | CV MAE={clim_mae:6.2f} kWh  CV R2={clim_r2:6.3f}")

    results_df = pd.DataFrame([r.__dict__ for r in results])
    results_df.to_csv("results.csv", index=False)
    print("\nSaved full results to results.csv")

    print("\n=== Top 10 by CV MAE (lower is better) ===")
    print(results_df.sort_values("cv_mae").head(10).to_string(index=False))


if __name__ == "__main__":
    main()
