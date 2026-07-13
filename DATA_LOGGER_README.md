# SolaX Cloud Data Logger

This directory contains the new data logging functionality for your SolaX inverter system.

## Files Created

| File | Description |
|------|-------------|
| `src/api_clients/solax_cloud_client.py` | Core API client and logger class |
| `scripts/solax_cloud_data_logger.py` | Command-line interface script |

## What It Does

The data logger collects historical energy flow data from the SolaX Cloud API at 5-minute intervals:

- **PV generation** (kW)
- **Battery power** (negative = discharge, positive = charge)
- **Grid power** (positive = export, negative = import)
- **Load consumption** (kW)
- **State of Charge** (%)

Data is stored incrementally in `data/solax_historical_data.json` and can be exported to CSV.

## Usage

### 1. Configure Cloud API Credentials

Edit `config.yaml` with your SolaX Cloud credentials:

```yaml
solaX_cloud_api:
  token_id: "YOUR_TOKEN_ID"
  master_wifisn: "YOUR_WIFI_SERIAL_NUMBER"
```

Get these from the SolaX Cloud web interface or app (Settings > Advanced > Modbus TCP).

### 2. Run the Logger

```bash
# Collect missing data (incremental - only fetches new dates)
python3 scripts/solax_cloud_data_logger.py --config config.yaml

# Show summary statistics
python3 scripts/solax_cloud_data_logger.py --summary

# Export to CSV for analysis in Excel/Google Sheets
python3 scripts/solax_cloud_data_logger.py --export-csv

# Fetch specific date range
python3 scripts/solax_cloud_data_logger.py \
  --start-date 2024-07-01 \
  --end-date 2025-06-30
```

### 3. Schedule Regular Updates (Optional)

Add to crontab for daily updates:

```bash
# Update data every day at 8:00 AM
0 8 * * * cd /path/to/home_automation && python3 scripts/solax_cloud_data_logger.py >> /var/log/solax_data.log 2>&1
```

## Output Format

### JSON Storage (`data/solax_historical_data.json`)

```json
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
    }
  ]
}
```

### CSV Export (`data/solax_data.csv`)

```csv
timestamp,pv_power_kw,battery_power_kw,grid_power_kw,load_power_kw,soc_percent
2025-01-15 10:30:00,4.2,-1.5,2.7,5.4,85
```

## Notes

- The logger automatically tracks which dates have been collected and only fetches new data
- Data is stored with timezone-aware timestamps (UTC)
- 5-minute intervals are used when available from the SolaX Cloud API
- Run with `--verbose` flag for detailed logging during collection
