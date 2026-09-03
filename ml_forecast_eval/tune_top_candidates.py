"""Light hyperparameter search on the top candidates from evaluate_models.py, on tier C features."""
from __future__ import annotations

import itertools
import warnings

import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, RandomForestRegressor

from evaluate_models import DATA_PATH, FEATURE_TIERS, expanding_window_cv

warnings.filterwarnings("ignore")

FEATURES = FEATURE_TIERS["C_+irradiance_split+cloud_levels"]


def tune_tree_model(name: str, cls, df: pd.DataFrame) -> None:
    grid = {
        "n_estimators": [100, 300],
        "max_depth": [4, 6, 8, None],
        "min_samples_leaf": [1, 3, 5],
    }
    keys = list(grid.keys())
    best = None
    for combo in itertools.product(*grid.values()):
        params = dict(zip(keys, combo))
        factory = lambda p=params: cls(random_state=42, n_jobs=-1, **p)
        mae, rmse, r2 = expanding_window_cv(df, FEATURES, factory, use_scaler=False)
        if best is None or mae < best[0]:
            best = (mae, rmse, r2, params)
    print(f"{name}: best CV MAE={best[0]:.3f} kWh RMSE={best[1]:.3f} R2={best[2]:.3f} params={best[3]}")


def tune_lightgbm(df: pd.DataFrame) -> None:
    try:
        import lightgbm as lgb
    except ImportError:
        print("LightGBM not installed, skipping")
        return
    grid = {
        "n_estimators": [50, 100, 200],
        "max_depth": [3, 4, 6],
        "learning_rate": [0.03, 0.05, 0.1],
        "min_child_samples": [5, 10, 20],
    }
    keys = list(grid.keys())
    best = None
    for combo in itertools.product(*grid.values()):
        params = dict(zip(keys, combo))
        factory = lambda p=params: lgb.LGBMRegressor(random_state=42, verbosity=-1, **p)
        mae, rmse, r2 = expanding_window_cv(df, FEATURES, factory, use_scaler=False)
        if best is None or mae < best[0]:
            best = (mae, rmse, r2, params)
    print(f"LightGBM: best CV MAE={best[0]:.3f} kWh RMSE={best[1]:.3f} R2={best[2]:.3f} params={best[3]}")


def main() -> None:
    df = pd.read_csv(DATA_PATH, parse_dates=["date"])
    tune_tree_model("ExtraTrees", ExtraTreesRegressor, df)
    tune_tree_model("RandomForest", RandomForestRegressor, df)
    tune_lightgbm(df)


if __name__ == "__main__":
    main()
