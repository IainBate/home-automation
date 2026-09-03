"""Join hourly weather with daily SolaX Cloud PV totals into a daily training dataset.

Adds deterministic solar-geometry features (pvlib) alongside the weather-API
fields, since geometry is known exactly in advance (no forecast error) unlike
cloud cover/radiation forecasts.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pvlib

LATITUDE = 53.8804244
LONGITUDE = -1.0435382
TIMEZONE = "Europe/London"
ALTITUDE_M = 20  # approx for York, UK - not critical, small effect on clearsky

DATA_DIR = Path(__file__).parent / "data"


def load_weather_hourly() -> pd.DataFrame:
    payload = json.loads((DATA_DIR / "weather_hourly.json").read_text())
    hourly = payload["hourly"]
    df = pd.DataFrame(hourly)
    df["time"] = pd.to_datetime(df["time"])
    return df


def load_pv_daily() -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / "solax_cloud_daily_combined.csv")
    df["date"] = pd.to_datetime(df["date"])
    return df


def add_solar_geometry(daily: pd.DataFrame) -> pd.DataFrame:
    """Add day length, max solar elevation, and clear-sky GHI (deterministic, no forecast error)."""
    location = pvlib.location.Location(LATITUDE, LONGITUDE, tz=TIMEZONE, altitude=ALTITUDE_M)

    times = pd.date_range(
        daily["date"].min(), daily["date"].max() + pd.Timedelta(days=1), freq="15min", tz=TIMEZONE, inclusive="left"
    )
    solpos = location.get_solarposition(times)
    clearsky = location.get_clearsky(times, model="ineichen")

    geo = pd.DataFrame({
        "date": times.tz_localize(None).floor("D") if times.tz is None else times.tz_convert(None).floor("D"),
        "elevation": solpos["apparent_elevation"].values,
        "clearsky_ghi": clearsky["ghi"].values,
    })
    # date column from tz-aware times needs local-date bucketing, not UTC - use tz-aware floor then drop tz
    geo["date"] = times.floor("D").tz_localize(None)

    daily_geo = geo.groupby("date").agg(
        max_elevation=("elevation", "max"),
        day_length_hours=("elevation", lambda s: (s > 0).sum() * 0.25),
        clearsky_ghi_sum=("clearsky_ghi", "sum"),  # W/m2 summed over 15-min steps (proxy units, consistent across days)
    ).reset_index()

    return daily.merge(daily_geo, on="date", how="left")


def build() -> pd.DataFrame:
    weather = load_weather_hourly()
    pv = load_pv_daily()

    weather["date"] = weather["time"].dt.floor("D")
    daily_weather = weather.groupby("date").agg(
        shortwave_radiation_sum=("shortwave_radiation", "sum"),
        direct_radiation_sum=("direct_radiation", "sum"),
        diffuse_radiation_sum=("diffuse_radiation", "sum"),
        cloud_cover_mean=("cloud_cover", "mean"),
        cloud_cover_low_mean=("cloud_cover_low", "mean"),
        cloud_cover_mid_mean=("cloud_cover_mid", "mean"),
        cloud_cover_high_mean=("cloud_cover_high", "mean"),
        temperature_mean=("temperature_2m", "mean"),
        temperature_max=("temperature_2m", "max"),
        humidity_mean=("relative_humidity_2m", "mean"),
        precipitation_sum=("precipitation", "sum"),
        wind_speed_mean=("wind_speed_10m", "mean"),
    ).reset_index()

    merged = pv.merge(daily_weather, on="date", how="inner")
    merged = add_solar_geometry(merged)

    merged["clear_sky_index"] = (
        merged["shortwave_radiation_sum"] / merged["clearsky_ghi_sum"].replace(0, pd.NA)
    ).fillna(0).clip(0, 1.5)
    merged["day_of_year"] = merged["date"].dt.dayofyear
    merged["month"] = merged["date"].dt.month

    return merged.sort_values("date").reset_index(drop=True)


if __name__ == "__main__":
    df = build()
    out_path = DATA_DIR / "daily_training_dataset.csv"
    df.to_csv(out_path, index=False)
    print(f"Built {len(df)} daily rows -> {out_path}")
    print(df[["date", "daily_pv_yield_kwh", "shortwave_radiation_sum", "cloud_cover_mean", "clear_sky_index"]].head())
    print("\nMissing values per column:")
    print(df.isna().sum()[df.isna().sum() > 0])
