#!/usr/bin/env python3
"""SolaX Cloud API Data Logger - Collect 5-minute granularity energy data.

This script fetches historical energy data from the SolaX Cloud API and stores
it locally. It supports incremental updates - running it again will only fetch
data for dates that haven't been recorded yet.

Usage:
    python3 solax_cloud_data_logger.py --config config.yaml

The collected data is stored in data/solax_historical_data.json with the following
structure:

{
  "meta": {
    "last_updated": "2025-01-15T12:34:56Z",
    "data_points": 1234,
    "date_range": {"start": "2024-07-01", "end": "2025-01-15"}
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

Data is collected at 5-minute intervals when available from the Cloud API.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

# Add project root to path for src module access
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.api_clients.solax_cloud_client import SolaxDataLogger, load_static_config


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="SolaX Cloud API Data Logger - Collect 5-minute granularity energy data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fetch missing data (incremental)
  python3 solax_cloud_data_logger.py

  # Fetch specific date range
  python3 solax_cloud_data_logger.py --start-date 2025-01-01 --end-date 2025-06-30

  # Show summary after collection
  python3 solax_cloud_data_logger.py --summary

  # Export to CSV
  python3 solax_cloud_data_logger.py --export-csv

  # Verbose logging for debugging
  python3 solax_cloud_data_logger.py --verbose
""",
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
        help="Start date for collection (YYYY-MM-DD format)",
    )
    parser.add_argument(
        "--end-date",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        help="End date for collection (YYYY-MM-DD format)",
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
    parser.add_argument(
        "--force-full",
        action="store_true",
        help="Ignore existing data and fetch all dates in range",
    )

    return parser.parse_args()


def setup_logging(verbose: bool) -> None:
    """Configure logging level."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def print_separator(char: str = "=", length: int = 60) -> None:
    """Print a separator line."""
    print(char * length)


def main() -> int:
    """Main entry point for command-line data logging."""
    args = parse_args()

    # Setup logging
    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    # Load config
    config = load_static_config(args.config)
    if not config:
        print("Error: Failed to load configuration from", args.config)
        return 1

    # Verify cloud API is configured
    token_id = config.get("solaX_cloud_api", {}).get("token_id")
    wifisn = config.get("solaX_cloud_api", {}).get("master_wifisn")

    if not token_id or token_id == "NOT_USED_FOR_MODBUS":
        print(
            "Error: Cloud API token_id not configured in config.yaml.\n"
            "\n"
            "To use the Cloud API data logger, you need to provide your SolaX Cloud\n"
            "API credentials. These can be found by:\n"
            "1. Opening the SolaX Cloud app or web interface\n"
            "2. Going to Settings > Advanced > Modbus TCP\n"
            "3. Note the token_id and wifisn (WiFi serial number)\n"
            "\n"
            "Update your config.yaml with:\n"
            "  solax_cloud_api:\n"
            "    token_id: \"YOUR_TOKEN_ID\"\n"
            "    master_wifisn: \"YOUR_WIFI_SERIAL_NUMBER\"\n"
        )
        return 1

    if not wifisn or wifisn == "NOT_USED_FOR_MODBUS":
        print(
            "Warning: master_wifisn (WiFi serial number) not configured.\n"
            "This is required for the Cloud API to identify your inverter.\n"
            "Please update config.yaml.",
        )
        return 1

    # Initialize logger
    logger_client = SolaxDataLogger(config, args.data_dir)

    print()
    print_separator()
    print("SOLAX CLOUD DATA LOGGER")
    print_separator()
    print()

    # Determine date range
    start_date = args.start_date
    end_date = args.end_date

    if not args.force_full:
        # Auto-detect missing dates
        existing_dates = set()
        for entry in logger_client.existing_data.get("data", []):
            ts = entry.get("timestamp", "")
            if ts:
                try:
                    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                    existing_dates.add(dt.date())
                except ValueError:
                    continue

        if not start_date and existing_dates:
            last_date = max(existing_dates)
            start_date = last_date + timedelta(days=1)

        if not end_date:
            end_date = date.today() - timedelta(days=1)

    print(f"Configuration:")
    print(f"  Config file: {args.config}")
    print(f"  Data directory: {args.data_dir}")
    print()
    print(f"Date range:")
    print(f"  Start: {start_date or 'from last known data'}")
    print(f"  End: {end_date or 'yesterday'}")
    print()

    # Check if we have any dates to fetch
    if not args.force_full and not start_date:
        print("No new data to collect. Data is up to date.")
        print()
        if args.summary:
            show_summary(logger_client)
        return 0

    try:
        missing_dates = logger_client.get_missing_dates(start_date, end_date)

        if not missing_dates:
            print("All dates in range already collected.")
            print(f"Total data points: {len(logger_client.existing_data.get('data', []))}")
            print()
            if args.summary:
                show_summary(logger_client)
            return 0

        print(f"Found {len(missing_dates)} date(s) to fetch...")
        print()

        # Fetch and merge data
        logger_client.fetch_and_merge_data(batch_size=30)

        # Show summary if requested
        if args.summary:
            print()
            show_summary(logger_client)

        # Export to CSV if requested
        if args.export_csv:
            csv_path = logger_client.export_to_csv()
            print(f"\nCSV exported to: {csv_path}")

        print()
        print_separator()
        print("Done!")
        print_separator()

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        return 1
    except Exception as e:
        logger.exception("Unexpected error during collection: %s", e)
        print(f"\nError: {e}")
        return 1

    return 0


def show_summary(logger_client: SolaxDataLogger) -> None:
    """Display summary statistics."""
    summary = logger_client.get_data_summary()
    meta = summary.get("summary", {})

    print_separator("=")
    print("DATA SUMMARY")
    print_separator("=")

    print(f"\nOverview:")
    print(f"  Total data points: {meta.get('total_data_points', 0)}")
    print(f"  Date range: {meta.get('date_range_start')} to {meta.get('date_range_end')}")
    print(f"  Days collected: {meta.get('dates_collected', 0)}")

    if "daily_summaries" in summary:
        print(f"\nRecent daily summaries:")

        for d in summary["daily_summaries"][-7:]:  # Last 7 days
            pv_kwh = d.get("pv_energy_kwh", 0)
            bat_discharge = d.get("battery_discharge_kwh", 0)
            grid_export = d.get("grid_export_kwh", 0)

            print(
                f"  {d['date']}: "
                f"{d['data_points']:4d} points | "
                f"PV: {pv_kwh:>6.2f}kWh | "
                f"Bat:-{bat_discharge:>5.2f}kWh | "
                f"Exp:{grid_export:>6.2f}kWh | "
                f"SoC: {d['min_soc_percent']:3d}-{d['max_soc_percent']:3d}%"
            )


if __name__ == "__main__":
    from datetime import timedelta

    exit(main())
