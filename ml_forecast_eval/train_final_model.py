"""Train and save the winning config from evaluate_models.py / tune_top_candidates.py:
ExtraTreesRegressor on feature tier C (irradiance split + multi-level cloud cover)."""
from __future__ import annotations

import joblib
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor

from evaluate_models import DATA_PATH, FEATURE_TIERS, TARGET

FEATURES = FEATURE_TIERS["C_+irradiance_split+cloud_levels"]

if __name__ == "__main__":
    df = pd.read_csv(DATA_PATH, parse_dates=["date"]).sort_values("date")

    model = ExtraTreesRegressor(n_estimators=300, max_depth=8, min_samples_leaf=1, random_state=42, n_jobs=-1)
    model.fit(df[FEATURES], df[TARGET])

    joblib.dump({"model": model, "features": FEATURES}, "data/best_model.joblib")
    print(f"Trained on {len(df)} rows, features: {FEATURES}")
    print("Saved to data/best_model.joblib")

    importances = sorted(zip(FEATURES, model.feature_importances_), key=lambda x: -x[1])
    print("\nFeature importances:")
    for name, imp in importances:
        print(f"  {name:28s} {imp:.3f}")
