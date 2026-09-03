# Solar forecast ML evaluation — results

## Data
- **Target (ground truth)**: `data/solax_cloud_daily_combined.csv` — 365 days (2025-09-01 to 2026-08-31),
  manually collected from the SolaX Cloud "Statistical Report" web portal. This replaces the
  production system's own `data/solax_historical_data.json` for Jan–Aug 2026, which was found to be
  synthetic/unreliable (see below).
- **Weather**: Open-Meteo archive API, same free/no-key source production already uses, expanded to
  11 hourly fields (radiation split, cloud cover by altitude, temperature, humidity, precipitation,
  wind), aggregated to daily.
- **Solar geometry**: added via `pvlib` — max solar elevation and day length per day. Deterministic
  (no forecast uncertainty, unlike weather), and turned out to matter a lot (see feature importances).

## Why daily-level, not hourly
Cross-checking the local `solax_historical_data.json` against the SolaX Cloud portal found it doesn't
track real generation at all for Jan–Aug 2026 (e.g. local computed ~43 kWh on both a 9.6 kWh cloud-reported
day and a 68.5 kWh day). Git history shows this was bulk-inserted in one commit (2026-07-13) via an API
endpoint later confirmed broken. So there's no trustworthy *hourly* ground truth before 2026-09-01 (when
the real `solax_realtime_logger.py` started). The cloud portal only exposes daily totals via manual
export. This evaluation is therefore daily-total forecasting, not hourly — a change from the current
production model's granularity, discussed below.

## Method
- Expanding-window chronological CV: train on all data before a month, predict that month, across the
  last 8 months. Never random-shuffled (adjacent days are correlated).
- Also a single last-15%-holdout metric, matching production `train_model()`'s own methodology.
- Compared 4 feature-set tiers (baseline → +solar geometry → +irradiance split/cloud altitude →
  +full weather) against 6 model types, plus climatology and current-production-config baselines.
- Tracked train/predict wall-clock time — turned out not to matter: every candidate trains in
  <250ms and predicts in <15ms on this laptop; a Pi4 has ample headroom for any of them at this data size.

## Results (full table: `results.csv`)

| Rank | Model | Features | CV MAE (kWh) | CV R² |
|---|---|---|---|---|
| 1 | ExtraTrees | C: +irradiance split/cloud altitude | **4.89** | 0.900 |
| 2 | ExtraTrees | D: +full weather | 5.01 | 0.896 |
| 3 | RandomForest | C | 5.13 | 0.894 |
| — | RandomForest | A (current production's feature set) | 5.83 | 0.877 |
| — | Climatology baseline (±15-day average) | — | 15.43 | -0.008 |

Hyperparameter tuning (`tune_top_candidates.py`) barely moved ExtraTrees (4.89 → 4.91 kWh) — the
defaults were already close to optimal for a ~300-row training set. RandomForest, LightGBM, XGBoost
all landed within ~1 kWh of each other; Ridge (linear) was surprisingly competitive on the single
holdout split (4.73 kWh) though noisier across CV folds (5.66 kWh) — worth keeping in mind as a cheap
fallback, though not the top pick here.

**Winner: ExtraTreesRegressor, feature tier C** (300 trees, depth 8, min_samples_leaf 1).
Saved to `data/best_model.joblib`.

## Feature importance (winning model)
```
shortwave_radiation_sum   0.396
day_length_hours          0.232   <- solar geometry: 44% combined importance
max_elevation              0.211   <- deterministic, zero forecast error, free to compute
direct_radiation_sum       0.111
diffuse_radiation_sum      0.027
cloud_cover_mid_mean       0.005
day_of_year                0.005
cloud_cover_low_mean       0.005
temperature_mean           0.003
cloud_cover_mean           0.003
cloud_cover_high_mean      0.003
```
Solar geometry (day length + max elevation) — free, deterministic, `pvlib`-computed — carries almost
as much weight as the radiation forecast itself. Cloud-cover-by-altitude and temperature contribute
little individually once radiation is present (radiation already encodes most of the cloud effect),
but tier C (which added them alongside the irradiance split) still beat tier B in CV, so the ensemble
benefits from the fuller picture even where individual feature importance is small.

## Recommendation for integration
1. Root-cause fix first, independent of model choice: `data/solax_historical_data.json` for
   Jan–Aug 2026 should not be used for training — it's synthetic. This alone likely explains a good
   chunk of the recent ~20 kWh/day forecast errors, since the current production model was trained on it.
2. Switch the trained target from hourly to daily-total (or retrain hourly once enough real 5-minute
   history accumulates from `solax_realtime_logger.py`, which only started 2026-09-01).
3. Add `pvlib` as a dependency; compute `max_elevation`/`day_length_hours` deterministically per
   forecast day (no API call needed).
4. Expand `weather_client.py`'s requested fields to add `direct_radiation`, `diffuse_radiation`, and
   `cloud_cover_low/mid/high` (same Open-Meteo call, no new endpoint).
5. Swap `RandomForestRegressor` → `ExtraTreesRegressor(n_estimators=300, max_depth=8, min_samples_leaf=1)`
   in `solar_forecast_logic.py`.
6. Compute cost is a non-issue at this data volume on a Pi4 either way — the choice comes down entirely
   to accuracy and how much weather-fetch complexity is worth carrying, not training/inference time.
