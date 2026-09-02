"""SolaX Cloud API Client Module.

Talks to TWO genuinely different SolaX Cloud APIs, discovered the hard way
(2026-09-02) after a "token invalid!" chase across both:

1. The old, officially-documented "Residential API" (getRealtimeInfo.do,
   solax_cloud_get_realtime_snapshot() below) - authenticated with the plain
   numeric tokenID an end-user gets for free from their own account's
   "Residential API (OLD)" page. Per SolaX's own SolaXCloud_User_Monitoring_API
   PDF, this endpoint is REAL-TIME ONLY - it has no historical/daily data of
   any kind, by design.
2. The undocumented "app" API (/app/station/list, /app/inverter/list,
   /app/powerPlant/getPowerData - _get_station_id()/_get_inverter_serials()/
   solax_cloud_get_daily_yield() below) that this module's original author
   reverse-engineered from the SolaX Cloud mobile app's own network traffic
   (hence "these can be found ... by inspecting network traffic" below) to
   get historical 5-minute data, which (1) can't provide. It needs the
   mobile app's own login-session token, NOT the account page's tokenID -
   confirmed by that tokenID coming back {"exception": "no auth!"} against
   this API even once it's valid enough to work against (1). Nobody has
   captured that app session token, so every function in this second group
   is currently unusable - kept only in case that token is captured later.

Because of that gap, scripts/solax_realtime_logger.py polls (1) every few
minutes going forward instead - it can never backfill the past (a live
snapshot has nothing to say about a day that's already over), but it's the
only source data/solax_historical_data.json actually has right now.

API Documentation:
- Base URL (this file's REALTIME_* functions): https://global.solaxcloud.com
  (NOT www.solaxcloud.com - that also returns "token invalid!" for a
  perfectly valid tokenID; the working domain is account/region-specific)
- Base URL (this file's app-API functions, currently unusable - see above):
  https://www.solaxcloud.com/api
- Authentication: Token-based (token_id from config) - but see above, it's
  two different kinds of token depending which base URL/endpoint group.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import json
import requests

from src.config_manager.config_manager import load_static_config

logger = logging.getLogger(__name__)


# =============================================================================
# SolaX Cloud API Constants
# =============================================================================

SOLAX_CLOUD_BASE_URL = "https://www.solaxcloud.com/api"
STATION_LIST_ENDPOINT = "/app/station/list"
INVERTER_LIST_ENDPOINT = "/app/inverter/list"
HISTORICAL_YIELD_ENDPOINT = "/app/powerPlant/getYieldInfo"
HISTORICAL_POWER_ENDPOINT = "/app/powerPlant/getPowerData"

# The OLD Residential API's own base - deliberately separate from
# SOLAX_CLOUD_BASE_URL above (different domain, different endpoint, and a
# different kind of token - see this module's docstring).
SOLAX_CLOUD_REALTIME_BASE_URL = "https://global.solaxcloud.com/proxyApp/proxy/api"
REALTIME_INFO_ENDPOINT = "/getRealtimeInfo.do"


# =============================================================================
# SolaXCloudAPIError Exception
# =============================================================================


class SolaXCloudAPIError(Exception):
    """Exception raised for SolaX Cloud API errors."""

    def __init__(self, message: str, error_code: str | None = None):
        self.message = message
        self.error_code = error_code or "UNKNOWN_ERROR"
        super().__init__(self.message)


# =============================================================================
# Helper Functions
# =============================================================================


def _get_station_id(config: dict[str, Any]) -> str | None:
    """Get the station ID from SolaX Cloud API.

    Args:
        config: Configuration dictionary with solax_cloud_api section

    Returns:
        Station ID string or None if not found
    """
    try:
        token_id = config.get("solaX_cloud_api", {}).get("token_id")
        if not token_id or token_id == "NOT_USED_FOR_MODBUS":
            logger.warning("Token ID not configured for Cloud API access")
            return None

        # Get station list
        url = f"{SOLAX_CLOUD_BASE_URL}{STATION_LIST_ENDPOINT}"
        response = requests.get(
            url,
            params={"tokenId": token_id},
            timeout=30,
        )
        response.raise_for_status()

        data = response.json()
        if data.get("success") != 1:
            logger.error("Failed to get station list: %s", data)
            return None

        # Return first station ID
        stations = data.get("result", {}).get("records", [])
        if not stations:
            logger.warning("No stations found in account")
            return None

        station_id = stations[0].get("id")
        logger.debug("Found station ID: %s", station_id)
        return station_id

    except requests.exceptions.RequestException as e:
        logger.error("Error fetching station list: %s", e)
        return None
    except Exception as e:
        logger.exception("Unexpected error in _get_station_id: %s", e)
        return None


def _get_inverter_serials(config: dict[str, Any]) -> list[str] | None:
    """Get list of inverter serial numbers from Cloud API.

    Args:
        config: Configuration dictionary with solax_cloud_api section

    Returns:
        List of inverter serial numbers or None if not found
    """
    try:
        token_id = config.get("solaX_cloud_api", {}).get("token_id")
        station_id = _get_station_id(config)

        if not token_id or not station_id:
            return None

        url = f"{SOLAX_CLOUD_BASE_URL}{INVERTER_LIST_ENDPOINT}"
        response = requests.get(
            url,
            params={"tokenId": token_id, "stationId": station_id},
            timeout=30,
        )
        response.raise_for_status()

        data = response.json()
        if data.get("success") != 1:
            logger.error("Failed to get inverter list: %s", data)
            return None

        inverters = data.get("result", {}).get("records", [])
        serials = [inv.get("sn") for inv in inverters if inv.get("sn")]
        logger.debug("Found inverter serials: %s", serials)
        return serials

    except requests.exceptions.RequestException as e:
        logger.error("Error fetching inverter list: %s", e)
        return None
    except Exception as e:
        logger.exception("Unexpected error in _get_inverter_serials: %s", e)
        return None


# =============================================================================
# Historical Yield Data (5-minute granularity)
# =============================================================================


def solax_cloud_get_daily_yield(
    config: dict[str, Any],
    target_date: date,
) -> list[dict[str, Any]] | None:
    """Get 5-minute yield data for a specific day from SolaX Cloud API.

    The SolaX Cloud API provides historical energy data with different
    granularity depending on the endpoint used. For daily analysis with
    fine granularity, we use the power data endpoint.

    Args:
        config: Configuration dictionary with solax_cloud_api section
                Must contain token_id and at least one wifisn (serial)
        target_date: Date to fetch data for

    Returns:
        List of data points with format:
        [
            {
                "timestamp": "2025-01-15 10:30:00",
                "timestamp_utc": "2025-01-15T10:30:00Z",
                "datetime_obj": datetime,
                "pv_power_kw": float,      # PV generation in kW
                "battery_power_kw": float, # Battery power (negative = discharge)
                "grid_power_kw": float,    # Grid power (positive = export)
                "load_power_kw": float,    # Load consumption in kW
                "soc_percent": int,        # State of Charge percentage
            },
            ...
        ]
        Returns None if API call fails or no data available

    Notes:
        - Data is returned in 5-minute intervals when available
        - Times are in the timezone configured on the SolaX Cloud account
        - Missing values are represented as None
    """
    try:
        token_id = config.get("solaX_cloud_api", {}).get("token_id")
        wifisn = config.get("solaX_cloud_api", {}).get("master_wifisn")

        if not token_id or not wifisn:
            logger.error(
                "Cloud API credentials missing: token_id=%s, wifisn=%s",
                bool(token_id),
                bool(wifisn),
            )
            return None

        # Format date for API (YYYY-MM-DD)
        date_str = target_date.strftime("%Y-%m-%d")

        # Use the power data endpoint which provides 5-minute granularity
        url = f"{SOLAX_CLOUD_BASE_URL}{HISTORICAL_POWER_ENDPOINT}"

        payload = {
            "sn": wifisn,
            "startDate": date_str,
            "endDate": date_str,
            "type": "1",  # Type 1 = 5-minute intervals
        }

        logger.info(
            "Fetching SolaX Cloud data for %s (SN: %s, type: 5-min)",
            date_str,
            wifisn,
        )

        response = requests.post(
            url,
            json=payload,
            headers={"tokenId": token_id},
            timeout=60,
        )
        response.raise_for_status()

        data = response.json()
        logger.debug("Raw API response: %s", data)

        if data.get("success") != 1:
            # A failure response has no "result" key at all - it's a
            # top-level {"exception": "...", "code": ...} shape (confirmed
            # against the real API, e.g. {"exception": "token invalid!",
            # "code": 103}), not {"result": {"msg": ...}}. The old
            # result.msg lookup always missed this and logged a useless
            # "Unknown error", masking the real reason for every failure.
            error_msg = data.get("exception") or data.get("result", {}).get("msg", "Unknown error")
            logger.error("API returned error for %s: %s", date_str, error_msg)
            return None

        # Parse the result
        result = data.get("result", {})
        power_data_list = result.get("powerDataList", [])

        if not power_data_list:
            logger.info("No power data found for %s", date_str)
            return []

        # Parse and transform each data point
        parsed_data = []
        for entry in power_data_list:
            try:
                timestamp_str = entry.get("dt", "")
                if not timestamp_str:
                    continue

                # Parse timestamp (format: "2025-01-15 10:30:00")
                dt_obj = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")

                parsed_data.append({
                    "timestamp": timestamp_str,
                    "timestamp_utc": dt_obj.replace(tzinfo=UTC).isoformat(),
                    "datetime_obj": dt_obj,
                    "pv_power_kw": _safe_float(entry.get("pvPower"), 0) / 1000,
                    "battery_power_kw": _safe_float(entry.get("batteryPower"), 0) / 1000,
                    "grid_power_kw": _safe_float(entry.get("feedInPower"), 0) / 1000,
                    "load_power_kw": _safe_float(entry.get("consumptionPower"), 0) / 1000,
                    "soc_percent": _safe_int(entry.get("soc", 0)),
                })
            except (ValueError, TypeError) as e:
                logger.warning("Skipping malformed data entry: %s - %s", entry, e)
                continue

        logger.info(
            "Parsed %d data points for %s",
            len(parsed_data),
            date_str,
        )
        return parsed_data

    except requests.exceptions.RequestException as e:
        logger.error("HTTP error fetching yield data: %s", e)
        return None
    except Exception as e:
        logger.exception("Unexpected error in solax_cloud_get_daily_yield: %s", e)
        return None


# =============================================================================
# Realtime Snapshot (the only currently-working data source - see module
# docstring for why the historical endpoints above can't be used instead)
# =============================================================================


def solax_cloud_get_realtime_snapshot(config: dict[str, Any]) -> dict[str, Any] | None:
    """Get a single live snapshot from the OLD Residential API's getRealtimeInfo.do.

    Shaped to match one entry of data/solax_historical_data.json's "data"
    list (see solax_cloud_get_daily_yield()'s docstring), minus
    "load_power_kw" - this endpoint has no instantaneous consumption figure,
    only cumulative to-date energy totals a single snapshot can't turn into
    a power reading. Every consumer that reads that field already treats it
    as optional (e.g. power_usage_analysis.py's `.get("load_power_kw", ...)`
    falls back to a default), so omitting it here is the same "missing means
    unknown" contract those callers already handle, not a new special case.

    Args:
        config: Configuration dictionary with solax_cloud_api section
                (token_id from the account's "Residential API (OLD)" page,
                NOT the OAuth2 developer-app "API" page - see module
                docstring - plus master_wifisn).

    Returns:
        {"timestamp": "2026-09-02 21:37:53", "pv_power_kw": float,
        "battery_power_kw": float, "grid_power_kw": float, "soc_percent":
        int, "yield_today_kwh": float | None}, or None if not configured,
        the API call fails, or the response can't be parsed.
        yield_today_kwh is the API's own cumulative today-so-far energy
        total (resets at local midnight on SolaX's side) - a ground-truth
        figure independent of how many samples happen to exist for the day,
        unlike pv_power_kw. compute_actual_daily_kwh()
        (solar_forecast_logic.py) prefers it for exactly that reason: a day
        with only a late/partial sample (a cron gap, or the very first day
        this logger ever ran) would otherwise sum to a fraction of the
        day's real total instead of the true figure.

    """
    try:
        token_id = config.get("solaX_cloud_api", {}).get("token_id")
        wifisn = config.get("solaX_cloud_api", {}).get("master_wifisn")

        if not token_id or not wifisn:
            logger.error(
                "Cloud API credentials missing: token_id=%s, wifisn=%s",
                bool(token_id),
                bool(wifisn),
            )
            return None

        url = f"{SOLAX_CLOUD_REALTIME_BASE_URL}{REALTIME_INFO_ENDPOINT}"
        response = requests.get(
            url,
            params={"tokenId": token_id, "sn": wifisn},
            timeout=30,
        )
        response.raise_for_status()

        data = response.json()
        logger.debug("Raw realtime API response: %s", data)

        # This API returns a real JSON boolean (true/false), not the app
        # API's integer 1/0 - `!= 1` would silently treat every success as
        # a failure here, so this checks truthiness directly instead.
        if not data.get("success"):
            error_msg = data.get("exception", "Unknown error")
            logger.error("Realtime API returned error: %s", error_msg)
            return None

        # Unlike the app API's failure shape (top-level, no "result" key -
        # see solax_cloud_get_daily_yield()), THIS API nests every actual
        # field under "result" even on success - only success/exception/code
        # are top-level. Confirmed against the real API 2026-09-02.
        result = data.get("result", {})

        timestamp_str = result.get("uploadTime", "")
        if not timestamp_str:
            logger.warning("Realtime snapshot has no uploadTime, discarding")
            return None

        pv_power_w = sum(
            _safe_float(result.get(field), 0)
            for field in ("powerdc1", "powerdc2", "powerdc3", "powerdc4")
        )

        return {
            "timestamp": timestamp_str,
            "pv_power_kw": pv_power_w / 1000,
            "battery_power_kw": _safe_float(result.get("batPower"), 0) / 1000,
            "grid_power_kw": _safe_float(result.get("feedinpower"), 0) / 1000,
            "soc_percent": _safe_int(result.get("soc", 0)),
            "yield_today_kwh": _safe_float(result.get("yieldtoday"), None),
        }

    except requests.exceptions.RequestException as e:
        logger.error("HTTP error fetching realtime snapshot: %s", e)
        return None
    except Exception:
        # Circuit Breaker: matches this codebase's convention (see CLAUDE.md)
        # of never letting an unexpected response shape crash the cron job
        # that calls this.
        logger.exception("Unexpected error in solax_cloud_get_realtime_snapshot")
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert value to float."""
    try:
        if value is None:
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    """Safely convert value to int."""
    try:
        if value is None:
            return default
        return int(float(value))
    except (ValueError, TypeError):
        return default


def merge_realtime_snapshot(existing_record: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Append one solax_cloud_get_realtime_snapshot() reading to a historical record.

    Pure/no I/O, so scripts/solax_realtime_logger.py's own read-transform-
    write is easy to test without mocking the filesystem. A duplicate
    timestamp (the device only updates every few minutes; a cron poll can
    land between updates and see the same uploadTime twice) is a no-op
    rather than a second identical row - existing_record is returned
    unchanged, not a copy with a repeated entry.

    Args:
        existing_record: Current data/solax_historical_data.json contents
            (or {} for a fresh file) - {"meta": {...}, "data": [...]}.
        snapshot: One solax_cloud_get_realtime_snapshot() return value.

    Returns:
        The updated record, with "meta" (last_updated/data_points/
        date_range) recomputed to match "data".

    """
    data = list(existing_record.get("data", []))
    if data and data[-1].get("timestamp") == snapshot["timestamp"]:
        return existing_record

    data.append(snapshot)
    data.sort(key=lambda entry: entry.get("timestamp", ""))

    return {
        "meta": {
            "last_updated": datetime.now(UTC).isoformat(),
            "data_points": len(data),
            "date_range": {
                "start": data[0]["timestamp"].split()[0],
                "end": data[-1]["timestamp"].split()[0],
            },
        },
        "data": data,
    }


# =============================================================================
# Incremental Data Logger
# =============================================================================


class SolaxDataLogger:
    """Incremental data logger for SolaX inverter historical data.

    This class manages the collection and storage of 5-minute granularity
    energy data from SolaX Cloud API. It supports incremental updates -
    only fetching data that hasn't been recorded yet.

    Data is stored in JSON format with the following structure:
    {
        "meta": {
            "last_updated": "2025-01-15T12:34:56Z",
            "data_points": 1234,
            "date_range": {
                "start": "2024-07-01",
                "end": "2025-01-15"
            }
        },
        "data": [
            {
                "timestamp": "2025-01-15 10:30:00",
                "pv_power_kw": 4.2,
                "battery_power_kw": -1.5,
                "grid_power_kw": 2.7,
                "load_power_kw": 5.4,
                "soc_percent": 85
            },
            ...
        ]
    }
    """

    def __init__(
        self,
        config: dict[str, Any],
        data_dir: str = "./data",
        data_file: str = "solax_historical_data.json",
    ):
        """Initialize the data logger.

        Args:
            config: Configuration dictionary with SolaX cloud API credentials
            data_dir: Directory to store log files
            data_file: Filename for the main data file
        """
        self.config = config
        self.data_dir = Path(data_dir)
        self.data_file = self.data_dir / data_file

        # Ensure data directory exists
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Load existing data
        self.existing_data = self._load_existing_data()

    def _load_existing_data(self) -> dict[str, Any]:
        """Load existing data from file."""
        if not self.data_file.exists():
            logger.info("No existing data file found, starting fresh")
            return {"meta": {}, "data": []}

        try:
            with open(self.data_file, "r") as f:
                data = json.load(f)
            logger.info(
                "Loaded %d existing data points from %s",
                len(data.get("data", [])),
                self.data_file,
            )
            return data
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Failed to load existing data: %s, starting fresh", e)
            return {"meta": {}, "data": []}

    def _save_data(self):
        """Save current data to file."""
        try:
            with open(self.data_file, "w") as f:
                json.dump(self.existing_data, f, indent=2)
            logger.info(
                "Saved %d data points to %s",
                len(self.existing_data.get("data", [])),
                self.data_file,
            )
        except IOError as e:
            logger.error("Failed to save data: %s", e)

    def get_missing_dates(
        self, start_date: date | None = None, end_date: date | None = None
    ) -> list[date]:
        """Get list of dates that need data collection.

        Args:
            start_date: Start date (default: earliest date in existing data or today - 1yr)
            end_date: End date (default: yesterday)

        Returns:
            List of dates needing data collection
        """
        current_dates = set()
        for entry in self.existing_data.get("data", []):
            ts = entry.get("timestamp", "")
            if ts:
                try:
                    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                    current_dates.add(dt.date())
                except ValueError:
                    continue

        # Determine date range
        if start_date is None:
            if current_dates:
                # Start from the day after our last data point
                last_date = max(current_dates)
                start_date = last_date + timedelta(days=1)
            else:
                # No existing data - go back 1 year as default
                start_date = date.today() - timedelta(days=365)

        if end_date is None:
            end_date = date.today() - timedelta(days=1)  # Yesterday

        # Generate list of missing dates
        missing_dates = []
        current = start_date
        while current <= end_date:
            if current not in current_dates:
                missing_dates.append(current)
            current += timedelta(days=1)

        logger.info(
            "Found %d missing dates from %s to %s",
            len(missing_dates),
            start_date,
            end_date,
        )
        return missing_dates

    def fetch_and_merge_data(self, batch_size: int = 30) -> dict[str, Any]:
        """Fetch missing data and merge with existing data.

        Args:
            batch_size: Number of days to fetch in each API call (for progress)

        Returns:
            Updated data dictionary
        """
        missing_dates = self.get_missing_dates()

        if not missing_dates:
            logger.info("No missing dates - data is up to date")
            return self.existing_data

        # Fetch data for each missing date
        new_entries = []
        total_to_fetch = len(missing_dates)

        for i, fetch_date in enumerate(missing_dates, 1):
            logger.info(
                "Fetching data for %s (%d/%d)",
                fetch_date,
                i,
                total_to_fetch,
            )

            try:
                daily_data = solax_cloud_get_daily_yield(self.config, fetch_date)

                if daily_data is None:
                    logger.warning("Failed to fetch data for %s", fetch_date)
                    continue

                new_entries.extend(daily_data)

                # Small delay between API calls
                time.sleep(0.5)

            except Exception as e:
                logger.error("Error fetching date %s: %s", fetch_date, e)
                continue

        if not new_entries:
            logger.warning("No new data entries collected")
            return self.existing_data

        # Merge with existing data
        existing_map = {
            entry["timestamp"]: entry
            for entry in self.existing_data.get("data", [])
        }

        # Add or update entries
        for entry in new_entries:
            existing_map[entry["timestamp"]] = entry

        # Sort by timestamp
        sorted_data = sorted(
            existing_map.values(),
            key=lambda x: x.get("timestamp", ""),
        )

        # Update metadata
        self.existing_data = {
            "meta": {
                "last_updated": datetime.now(UTC).isoformat(),
                "data_points": len(sorted_data),
                "date_range": {
                    "start": sorted_data[0]["timestamp"].split()[0] if sorted_data else None,
                    "end": sorted_data[-1]["timestamp"].split()[0] if sorted_data else None,
                },
            },
            "data": sorted_data,
        }

        self._save_data()

        logger.info(
            "Added %d new data points. Total: %d",
            len(new_entries),
            len(sorted_data),
        )

        return self.existing_data

    def get_data_summary(self) -> dict[str, Any]:
        """Get summary statistics of collected data."""
        data = self.existing_data.get("data", [])

        if not data:
            return {"summary": "No data available"}

        # Group by date
        daily_stats: dict[str, list] = {}
        for entry in data:
            date_str = entry["timestamp"].split()[0]
            if date_str not in daily_stats:
                daily_stats[date_str] = []
            daily_stats[date_str].append(entry)

        # Calculate summary stats
        summaries = []
        for date_str, entries in sorted(daily_stats.items()):
            pv_values = [e.get("pv_power_kw", 0) or 0 for e in entries]
            battery_values = [e.get("battery_power_kw", 0) or 0 for e in entries]
            grid_values = [e.get("grid_power_kw", 0) or 0 for e in entries]

            summaries.append({
                "date": date_str,
                "data_points": len(entries),
                "pv_energy_kwh": round(sum(pv_values) * (5/60), 2),  # 5-min intervals
                "battery_discharge_kwh": round(
                    sum(max(0, -b) for b in battery_values) * (5/60), 2
                ),
                "grid_export_kwh": round(sum(max(0, g) for g in grid_values) * (5/60), 2),
                "max_soc_percent": max(e.get("soc_percent", 0) or 0 for e in entries),
                "min_soc_percent": min(e.get("soc_percent", 0) or 0 for e in entries),
            })

        return {
            "summary": {
                "total_data_points": len(data),
                "date_range_start": self.existing_data.get(
                    "meta", {}
                ).get("date_range", {}).get("start"),
                "date_range_end": self.existing_data.get(
                    "meta", {}
                ).get("date_range", {}).get("end"),
                "dates_collected": len(summaries),
            },
            "daily_summaries": summaries,
        }

    def export_to_csv(self, output_file: str | None = None) -> str:
        """Export data to CSV format.

        Args:
            output_file: Output file path (default: data/solax_data.csv)

        Returns:
            Path to exported file
        """
        if output_file is None:
            output_file = str(self.data_dir / "solax_data.csv")

        import csv

        data = self.existing_data.get("data", [])

        if not data:
            logger.warning("No data to export")
            return ""

        fieldnames = [
            "timestamp",
            "pv_power_kw",
            "battery_power_kw",
            "grid_power_kw",
            "load_power_kw",
            "soc_percent",
        ]

        with open(output_file, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for entry in data:
                row = {
                    "timestamp": entry.get("timestamp"),
                    "pv_power_kw": entry.get("pv_power_kw"),
                    "battery_power_kw": entry.get("battery_power_kw"),
                    "grid_power_kw": entry.get("grid_power_kw"),
                    "load_power_kw": entry.get("load_power_kw"),
                    "soc_percent": entry.get("soc_percent"),
                }
                writer.writerow(row)

        logger.info("Exported data to %s", output_file)
        return output_file


# =============================================================================
# Utility Functions
# =============================================================================


def main():
    """Main entry point for command-line data logging."""
    import argparse

    parser = argparse.ArgumentParser(
        description="SolaX Cloud API Data Logger - Collect 5-minute granularity energy data"
    )
    parser.add_argument(
        "--config",
        "-c",
        default="./config.yaml",
        help="Path to config file (default: ./config.yaml)",
    )
    parser.add_argument(
        "--data-dir",
        default="./data",
        help="Directory for data files (default: ./data)",
    )
    parser.add_argument(
        "--start-date",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        help="Start date (YYYY-MM-DD format)",
    )
    parser.add_argument(
        "--end-date",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        help="End date (YYYY-MM-DD format)",
    )
    parser.add_argument(
        "--export-csv",
        action="store_true",
        help="Export collected data to CSV after collection",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Show summary statistics of collected data",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Load config
    config = load_static_config(args.config)
    if not config:
        print("Error: Failed to load configuration")
        return 1

    # Verify cloud API is configured
    token_id = config.get("solaX_cloud_api", {}).get("token_id")
    wifisn = config.get("solaX_cloud_api", {}).get("master_wifisn")

    if not token_id or token_id == "NOT_USED_FOR_MODBUS":
        print(
            "Error: Cloud API token_id not configured. "
            "Please edit config.yaml with your SolaX Cloud credentials."
        )
        return 1

    if not wifisn or wifisn == "NOT_USED_FOR_MODBUS":
        print(
            "Warning: master_wifisn not configured. "
            "Some API features may not work correctly."
        )

    # Initialize logger and fetch data
    logger_client = SolaxDataLogger(config, args.data_dir)

    print(f"Starting data collection...")
    print(f"Target date range: {args.start_date or 'from last known'} to {args.end_date or 'yesterday'}")

    try:
        logger_client.fetch_and_merge_data()

        if args.summary:
            summary = logger_client.get_data_summary()
            print("\n" + "=" * 60)
            print("DATA SUMMARY")
            print("=" * 60)
            meta = summary.get("summary", {})
            print(f"Total data points: {meta.get('total_data_points', 0)}")
            print(f"Date range: {meta.get('date_range_start')} to {meta.get('date_range_end')}")
            print(f"Days collected: {meta.get('dates_collected', 0)}")

            if "daily_summaries" in summary:
                print("\nDaily summaries (first 5 days):")
                for d in summary["daily_summaries"][:5]:
                    print(
                        f"  {d['date']}: "
                        f"{d['data_points']} points, "
                        f"PV: {d['pv_energy_kwh']}kWh, "
                        f"Export: {d['grid_export_kwh']}kWh"
                    )

        if args.export_csv:
            csv_path = logger_client.export_to_csv()
            print(f"\nCSV exported to: {csv_path}")

        print("\nDone!")

    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 1
    except Exception as e:
        logger.exception("Unexpected error during collection: %s", e)
        print(f"Error: {e}")
        return 1

    return 0


if __name__ == "__main__":
    import json
    from pathlib import Path

    exit(main())
