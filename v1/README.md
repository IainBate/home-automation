# SolaX Modbus + Ohme EV Package

Standalone package for controlling SolaX inverters and Ohme EV chargers. Provides direct Modbus TCP access to SolaX inverters and Python API client for Ohme Home Pro chargers.

---

## What's Included

### CLI Scripts
- **solax_modbus_status_report.py** - Read comprehensive inverter status via Modbus TCP
- **solax_modbus_read_and_set_workmode.py** - Read/set inverter work mode (Self-Use, Charge, Discharge, Hold)
- **ohme_ev_control.py** - Control Ohme Home Pro EV charger (status, mode, settings)

### API Clients
- **SolaX Modbus Client** - Direct Modbus TCP access to SolaX X3 Hybrid G4 inverters
  - Read PV generation, battery SoC, grid power, work mode
  - Set work mode with hardware safety mechanisms
  - Support for dual-inverter systems (master + slave)
- **Ohme EV Client** - Ohme Home Pro charger control
  - Read charger status, power, battery SoC
  - Control charging modes (max charge, smart charge)
  - Set target SoC, price caps, target times

### Core Capabilities
- **Read-only monitoring**: Get real-time status without cloud services
- **Safe mode control**: Hardware protection mechanisms for inverter mode changes
- **EV charging optimization**: Programmatic control of Ohme charger settings
- **Dual-battery support**: Full support for master+slave inverter configurations

---

## System Requirements

### Hardware
- SolaX X3 Hybrid G4 inverters (master and/or slave) with WiFi dongles
  - Modbus TCP must be enabled (via SolaX cloud or local web UI)
  - WiFi dongles must be on local network with fixed IP addresses
- Ohme Home Pro EV charger (for EV control features)
  - Internet connection required (uses Ohme cloud API)

### Software
- **Python**: 3.11 or later (Raspberry Pi OS Bookworm ships with 3.11)
- **Operating System**: Linux (Raspberry Pi OS recommended), macOS, or any Unix-like OS
- **Network**:
  - Local network access to SolaX inverter WiFi dongles
  - Internet access for Ohme API

### Raspberry Pi Compatibility
✅ All dependencies work on Raspberry Pi (ARM architecture)
✅ No Mac-specific code
✅ Tested on Raspberry Pi OS (Debian-based)

---

## Installation

### Quick Start

```bash
# 1. Extract the package
unzip solax_ohme_package.zip
cd solax_ohme_package/

# 2. Create virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
vi config.yaml  # Fill in your settings
```

### Detailed Steps

#### Step 1: Extract Package
```bash
unzip solax_ohme_package.zip
cd solax_ohme_package/
```

#### Step 2: Create Virtual Environment (Optional but Recommended)
```bash
# Create virtual environment
python3 -m venv venv

# Activate (Linux/Mac)
source venv/bin/activate

# Deactivate when done
deactivate
```

#### Step 3: Install Dependencies
```bash
pip install -r requirements.txt
```

Expected packages (7 total):
- `pyyaml` - YAML config parsing
- `pymodbus` - Modbus TCP communication
- `ohme` - Ohme API client
- `pydantic` - Config validation
- `pytz` - Timezone handling
- `tabulate` - Table formatting
- `jsonschema` - JSON schema validation

#### Step 4: Configure
```bash
# Edit configuration file with your settings
vi config.yaml
```

See [Configuration Guide](#configuration-guide) below for details.

---

## Configuration Guide

The `config.yaml` file contains all settings for SolaX and Ohme integration.

**Note**: The config file includes additional sections (financial_costs, household_load, etc.) with stub values. These are required by the config schema but **not used** for basic Modbus operations. You only need to edit the sections marked as REQUIRED.

### Required Settings

#### 1. SolaX Inverter IP Addresses

```yaml
solaX_cloud_api:
  master_ip: "192.168.1.100"  # Your master inverter IP
  slave_ip: "192.168.1.101"   # Your slave inverter IP
  modbus_enabled: true        # Must be true
```

**How to find inverter IP addresses:**
1. Check your router's DHCP client list
2. Look for devices named "Pocket WiFi" or SolaX serial numbers
3. **Recommended**: Configure static DHCP reservations to prevent IP changes

**How to enable Modbus TCP:**
- Via SolaX cloud: Settings → Advanced Settings → Modbus TCP → Enable
- Via local web UI: Access WiFi dongle web interface (http://INVERTER_IP) → Enable Modbus

#### 2. Ohme Credentials

```yaml
ohme_ev:
  username: "your-email@example.com"  # Your Ohme app login (email)
  password: "your-password"           # Your Ohme app password
```

**Where to get credentials:**
- Use the same email/password you use for the Ohme mobile app
- Same credentials work for app.ohme.io web interface

### Optional Settings

#### Battery Capacity (for display calculations)

```yaml
battery_system:
  master_capacity_kwh: 11.52  # Usable capacity in kWh
  slave_capacity_kwh: 11.52
```

If you don't know these values, you can:
- Leave them at default (11.52 kWh typical for SolaX Triple Power batteries)
- Check SolaX app or cloud for battery specifications
- Omit this section entirely (only affects capacity percentage displays)

#### Modbus Connection Parameters

```yaml
solaX_cloud_api:
  modbus_port: 502                     # Standard port (rarely needs changing)
  modbus_connection_timeout: 10        # Connection timeout in seconds
  modbus_read_timeout: 5               # Read timeout in seconds
  master_modbus_address: 1             # Modbus address (default: 1)
  slave_modbus_address: 2              # Modbus address (default: 2)
  min_command_interval: 1.0            # Min seconds between commands
```

#### Timezone

```yaml
location:
  default_timezone_str: "Europe/London"  # Your timezone (pytz format)
  city_name: "London"                    # City (informational)
  country_name: "United Kingdom"         # Country (informational)
```

#### Logging Verbosity

```yaml
logging:
  console_level: "WARNING"  # DEBUG, INFO, WARNING, or ERROR
  file_level: "DEBUG"       # DEBUG, INFO, WARNING, or ERROR
```

Use `DEBUG` for troubleshooting, `WARNING` for normal operation.

---

## Usage Examples

### SolaX Modbus Status Report

**Purpose**: Read comprehensive real-time status from SolaX inverters via direct Modbus TCP access. Provides system overview, battery status, PV generation, grid totals, and individual inverter details. This is a **read-only** diagnostic tool - it never modifies inverter settings.

**Use this when**: You want to check current system status, monitor power flows, verify battery state, or diagnose issues without using the SolaX cloud service.

**Common usage**:

```bash
# Read current inverter status (clean output)
python3 scripts/solax_modbus_status_report.py

# Enable debug logging to see Modbus communication details
python3 scripts/solax_modbus_status_report.py --log-level DEBUG

# Compare Modbus data with Cloud API (requires cloud credentials in config)
python3 scripts/solax_modbus_status_report.py --compare-cloud
```

**Full help** (`--help`):

```
usage: solax_modbus_status_report.py [-h] [--config CONFIG]
                                     [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}]
                                     [--compare-cloud] [--performance-logging]

Read comprehensive status from SolaX inverters via Modbus TCP

Options:
  -h, --help            show this help message and exit
  --config CONFIG, -c CONFIG
                        Path to config file (default: config.yaml)
  --log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}, -l {DEBUG,INFO,WARNING,ERROR,CRITICAL}
                        Set logging level (default: ERROR for clean output)
  --compare-cloud       Add Cloud API comparison columns to output tables
  --performance-logging
                        Enable detailed performance logging
```

**Example output**:

```
=== SOLAX INVERTER STATUS (MODBUS) ===

System Overview:
  Current Mode: Self Use
  Total PV: 4.32 kW
  Total Battery: +2.15 kW (charging)
  Grid: -1.85 kW (exporting)

Battery Status:
  Master: 87% (10.02 kWh) | Slave: 89% (10.25 kWh)
  Average: 88% (20.27 kWh total)

[Detailed tables follow...]
```

---

### SolaX Modbus Work Mode Control

**Purpose**: Read and safely change the inverter work mode (Self-Use, Force Charge, Force Discharge, Hold). This tool implements safety timing restrictions to protect inverter hardware per SolaX requirements (2-minute delays between operations). **Changes affect the MASTER INVERTER ONLY**.

**Use this when**: You need to manually override the inverter mode - force charging from grid during cheap rates, force discharging to grid during peak rates, or return to normal self-use operation.

**⚠️ SAFETY**: Work mode changes modify inverter behavior. The tool enforces **2-minute safety intervals** between operations. Use `--force-mode-change` to bypass (use with caution - risk of hardware damage).

**Common usage**:

```bash
# Read current work mode (no changes)
python3 scripts/solax_modbus_read_and_set_workmode.py

# Set to self-use mode (normal operation)
python3 scripts/solax_modbus_read_and_set_workmode.py --self-use

# Set to force charge (charge batteries from grid)
python3 scripts/solax_modbus_read_and_set_workmode.py --charge

# Set to force discharge (discharge batteries to grid)
python3 scripts/solax_modbus_read_and_set_workmode.py --discharge

# Set to hold mode (stop battery charge/discharge, PV+grid only)
python3 scripts/solax_modbus_read_and_set_workmode.py --hold
```

**Full help** (`--help`):

```
usage: solax_modbus_read_and_set_workmode.py [-h] [--config CONFIG]
                                             [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}]
                                             [--self-use | --charge | --discharge | --hold]
                                             [--force-mode-change]

Read and set SolaX inverter work mode via Modbus TCP

Options:
  -h, --help            show this help message and exit
  --config CONFIG, -c CONFIG
                        Path to config file (default: config.yaml)
  --log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}, -l {DEBUG,INFO,WARNING,ERROR,CRITICAL}
                        Set logging level (default: INFO)

Work Mode Options (mutually exclusive):
  --self-use            Set to Self-Use mode (normal operation)
  --charge              Set to Force Charge mode (charge from grid)
  --discharge           Set to Force Discharge mode (discharge to grid)
  --hold                Set to Hold mode (stop charge/discharge, PV+grid only)
  --force-mode-change   Bypass safety timing (USE WITH CAUTION)

SAFETY WARNING:
  Write operations modify inverter behavior and affect MASTER INVERTER ONLY.
  The tool enforces a 2-minute minimum interval between mode changes to
  protect inverter hardware. Only use when you understand the implications.
```

**Example output**:

```
Current work mode: Self Use
Changing to Force Charge...
✅ Mode changed successfully: Self Use → Force Charge
```

---

### Ohme EV Control

**Purpose**: Control Ohme Home Pro EV charger via the Ohme API. Read charger status, control charging (max charge/smart charge modes), set target SoC, configure price caps, and manage vehicle selection. Wraps the official `ohmepy` library.

**Use this when**: You want to programmatically control your Ohme charger - check if car is plugged in and charging, start/stop charging, set charge targets, or configure price-based smart charging.

**⚠️ LIMITATION**: The native pause/resume commands don't work (require Firebase AppCheck tokens). **Workaround**: Use smart charge mode with negative price cap (-100p) to effectively pause charging.

**Common usage**:

```bash
# Read current charger status
python3 scripts/ohme_ev_control.py --status

# Read detailed status (verbose mode)
python3 scripts/ohme_ev_control.py --status --verbose

# Start max charge (charge immediately to target SoC)
python3 scripts/ohme_ev_control.py --max-charge

# Set charge target to 80%
python3 scripts/ohme_ev_control.py --set-target 80

# Set target finish time to 07:30
python3 scripts/ohme_ev_control.py --set-target-time 07:30

# Enable smart charge with price cap of £0.15/kWh
python3 scripts/ohme_ev_control.py --smart-charge
python3 scripts/ohme_ev_control.py --set-price-cap 0.15

# Workaround to pause charging (smart charge + negative price cap)
python3 scripts/ohme_ev_control.py --smart-charge
python3 scripts/ohme_ev_control.py --set-price-cap -1.00
```

**Full help** (`--help`):

```
usage: ohme_ev_control.py [-h] (--status | --pause | --resume | --max-charge |
                          --smart-charge | --set-target PERCENT |
                          --set-target-time HH:MM | --set-price-cap VALUE |
                          --list-vehicles | --select-vehicle NAME)
                          [--verbose] [--quiet]

Control Ohme Home Pro EV charger via Ohme API

Actions (required, choose one):
  --status              Read current charger status
  --max-charge          Enable max charge mode (charge immediately)
  --smart-charge        Enable smart charge mode (price-based)
  --set-target PERCENT  Set charge target percentage (0-100)
  --set-target-time HH:MM
                        Set target finish time (e.g., 07:30)
  --set-price-cap VALUE Set price cap in GBP/kWh (e.g., 0.15)
  --list-vehicles       List available vehicles
  --select-vehicle NAME Select vehicle to charge

Options:
  -h, --help            show this help message and exit
  --verbose, -v         Show detailed status information
  --quiet, -q           Minimal output (success/failure only)

Note: Pause/resume commands require Firebase AppCheck tokens (not implemented).
      Workaround: Use --smart-charge with --set-price-cap -1.00 to pause.
```

**Example output** (`--status`):

```
Ohme Charger Status:
  Car plugged in: Yes
  Charging: Yes
  Power: 7200W (32.0A)
  Battery: 67%
  Energy delivered: 12.45 kWh
  Status: charging
```

---

## API Usage Examples

For Python developers who want to use the API clients programmatically.

### Ohme API - Read Status

**Extract key status information** (plugged in, charging/not-charging, power, SoC):

```python
import asyncio
from src.api_clients.ohme_ev_client import OhmeEVClient

async def check_ohme_status():
    """Check if car is plugged in and charging, get power and SoC."""
    # Create and connect client
    client = OhmeEVClient()
    await client.connect()

    try:
        # Get charger status
        status = await client.get_charger_status(use_cache=False)

        # Extract plugged in status (boolean)
        plugged_in = status.get("plugged_in")
        print(f"Car plugged in: {plugged_in}")

        # Extract charging status (OhmeChargerStatus enum)
        # Possible values: CHARGING, PLUGGED_IN, UNPLUGGED, PAUSED, FINISHED, PENDING_APPROVAL, UNKNOWN
        charger_status = status["status"]  # OhmeChargerStatus enum
        is_charging = (charger_status.value == "charging")
        print(f"Currently charging: {is_charging}")
        print(f"Status: {charger_status.value}")

        # Extract delivered power (watts and amps)
        power_watts = status["power_watts"]
        power_amps = status["power_amps"]
        print(f"Charging power: {power_watts:.0f}W ({power_amps:.1f}A)")

        # Extract battery SoC percentage
        soc_percent = status["battery_percent"]
        print(f"Battery charge level: {soc_percent}%")

        # Extract energy delivered this session (Wh)
        energy_wh = status["energy_wh"]
        energy_kwh = energy_wh / 1000
        print(f"Energy delivered: {energy_kwh:.2f} kWh")

    finally:
        await client.close()

# Run the async function
asyncio.run(check_ohme_status())
```

**Output example**:
```
Car plugged in: True
Currently charging: True
Status: charging
Charging power: 7200W (32.0A)
Battery charge level: 67%
Energy delivered: 12.45 kWh
```

---

### Ohme API - Change Mode

**Control Ohme charging modes and parameters**:

```python
import asyncio
from src.api_clients.ohme_ev_client import OhmeEVClient

async def control_ohme_charging():
    """Examples of controlling Ohme charging."""
    client = OhmeEVClient()
    await client.connect()

    try:
        # Example 1: Start max charge (charge immediately to target SoC)
        print("Starting max charge...")
        success = await client.set_max_charge(enabled=True)
        print(f"Max charge enabled: {success}")

        # Example 2: Set charge target to 80%
        print("\nSetting charge target to 80%...")
        success = await client.set_target(target_percent=80)
        print(f"Target set: {success}")

        # Example 3: Enable smart charge mode
        print("\nEnabling smart charge mode...")
        await client.set_mode("smart_charge")
        print("Smart charge mode enabled")

        # Example 4: Set price cap to £0.15/kWh
        print("\nSetting price cap to £0.15/kWh...")
        success = await client.set_price_cap(cap=0.15)
        print(f"Price cap set: {success}")

        # Example 5: PAUSE WORKAROUND (pause doesn't work - use negative price cap)
        # The native pause_charge() method doesn't work (requires Firebase AppCheck token)
        # Workaround: Use smart charge with negative price cap to prevent charging
        print("\nPausing charging (workaround: smart charge + negative price cap)...")
        await client.set_mode("smart_charge")
        success = await client.set_price_cap(cap=-1.00)  # -£1/kWh = never charge
        print(f"Charging effectively paused: {success}")
        print("Note: This sets smart charge with -£1.00/kWh price cap")
        print("      Ohme won't charge if electricity price is above this")

        # To resume: either set max charge or use positive price cap
        print("\nResuming charging (remove price cap restriction)...")
        success = await client.set_max_charge(enabled=True)
        print(f"Charging resumed: {success}")

    finally:
        await client.close()

asyncio.run(control_ohme_charging())
```

**⚠️ IMPORTANT - Pause Limitation**:
The Ohme API's native `pause_charge()` and `resume_charge()` methods **do not work** - they require Firebase AppCheck tokens that expire quickly.

**Workaround for pausing**: Use smart charge mode with a negative price cap (-£1.00/kWh). This tells Ohme to only charge if electricity is cheaper than -£1/kWh, which will never happen, effectively pausing charging.

---

### SolaX API - Read Current Status

**Extract key system information** (mode, PV, grid flow, battery SoC, charge/discharge rate):

```python
from src.config_manager.config_manager import load_static_config
from src.api_clients.solax_modbus_client import (
    solax_modbus_work_mode,
    solax_modbus_pv_power,
    solax_modbus_grid_power,
    solax_modbus_soc,
    solax_modbus_battery_power,
)

# Load configuration
config = load_static_config("config.yaml")

# Read current work mode
mode = solax_modbus_work_mode(config)  # Returns BatteryMode enum
print(f"Current work mode: {mode.value}")  # e.g., "Self Use", "Force Charge"

# Read PV generation
pv = solax_modbus_pv_power(config)
master_pv = pv["master"]["pv1"] + pv["master"]["pv2"]  # Watts
slave_pv = pv["slave"]["pv1"] + pv["slave"]["pv2"]
total_pv_kw = (master_pv + slave_pv) / 1000
print(f"Total PV generation: {total_pv_kw:.2f} kW")

# Read grid power (positive = exporting, negative = importing)
grid = solax_modbus_grid_power(config)
grid_power_w = grid["master"]  # Watts (only master inverter has grid meter)
grid_power_kw = grid_power_w / 1000

if grid_power_w > 0:
    print(f"Exporting to grid: {grid_power_kw:.2f} kW")
elif grid_power_w < 0:
    print(f"Importing from grid: {abs(grid_power_kw):.2f} kW")
else:
    print("No grid import/export (0.00 kW)")

# Read battery state of charge
soc = solax_modbus_soc(config)
print(f"Battery SoC - Master: {soc['master']}%, Slave: {soc['slave']}%")
avg_soc = (soc['master'] + soc['slave']) / 2
print(f"Average SoC: {avg_soc:.1f}%")

# Read battery charge/discharge rate (positive = charging, negative = discharging)
battery = solax_modbus_battery_power(config)
master_bat = battery["master"]["power"]  # Watts
slave_bat = battery["slave"]["power"]
total_battery_w = master_bat + slave_bat
total_battery_kw = total_battery_w / 1000

if total_battery_w > 0:
    print(f"Battery charging at: {total_battery_kw:.2f} kW")
elif total_battery_w < 0:
    print(f"Battery discharging at: {abs(total_battery_kw):.2f} kW")
else:
    print("Battery idle (0.00 kW)")
```

**Output example**:
```
Current work mode: Self Use
Total PV generation: 4.32 kW
Exporting to grid: 1.85 kW
Battery SoC - Master: 87%, Slave: 89%
Average SoC: 88.0%
Battery charging at: 2.15 kW
```

**Understanding Sign Conventions**:
- **Grid Power**: `+` = exporting to grid, `-` = importing from grid
- **Battery Power**: `+` = charging, `-` = discharging
- All power values are in Watts (W) - divide by 1000 for kilowatts (kW)

---

### SolaX API - Set Mode

**Change battery work mode** with safety mechanisms:

```python
from src.config_manager.config_manager import load_static_config
from src.api_clients.solax_modbus_client import solax_modbus_set_work_mode
from src.core_logic.battery_simulation import BatteryMode

# Load configuration
config = load_static_config("config.yaml")

# Set to Force Charge mode (charge from grid)
print("Changing to Force Charge mode...")
result = solax_modbus_set_work_mode(
    config,
    BatteryMode.FORCE_CHARGE,  # Use BatteryMode enum
    changed_by="my_script",    # Identify what changed the mode
    force_unsafe=False         # Respect safety timing (default)
)

if result["success"]:
    print(f"✅ Mode changed from {result['from_mode']} to {result['to_mode']}")
else:
    print(f"❌ Mode change failed: {result['error_message']}")
    print(f"   Error type: {result.get('error_type')}")

    # Handle specific error types
    if result.get('error_type') == 'safety_interval':
        print("   Safety restriction: Too soon after last mode change")
        print("   Options:")
        print("   1. Wait 3+ seconds and try again")
        print("   2. Use force_unsafe=True to bypass (⚠️ RISKY)")
```

**Available Modes** (BatteryMode enum):
- `BatteryMode.SELF_USE` - Normal operation (self-consumption)
- `BatteryMode.FORCE_CHARGE` - Force charge from grid
- `BatteryMode.FORCE_DISCHARGE` - Force discharge to grid
- `BatteryMode.MANUAL_STOP` - Hold mode (stop battery, PV+grid only)

**Safety Mechanism**:
The API enforces a **2-minute minimum interval** between mode changes to protect inverter hardware (per SolaX requirements). This prevents rapid mode switching that could damage the inverter.

**To bypass safety** (use with caution):
```python
result = solax_modbus_set_work_mode(
    config,
    BatteryMode.FORCE_DISCHARGE,
    changed_by="my_script",
    force_unsafe=True  # ⚠️ BYPASSES safety timing - risk of hardware damage
)
```

**⚠️ WARNING**: Only use `force_unsafe=True` if you understand the risks. Rapid mode changes can damage inverter hardware. The 2-minute safety interval exists for hardware protection.

---

## Troubleshooting

### Import Errors

**Problem**: `ModuleNotFoundError: No module named 'src'`

**Solution**:
1. Verify you're running from the package root directory (`solax_ohme_package/`)
2. Ensure virtual environment is activated: `source venv/bin/activate`
3. Check Python path includes `src/`: Add to script if needed:
   ```python
   import sys
   from pathlib import Path
   sys.path.insert(0, str(Path(__file__).parent.parent))
   ```

### Config Errors

**Problem**: `FileNotFoundError: config.yaml not found`

**Solution**:
1. Verify file exists: `ls -l config.yaml`
2. Check you're in the correct directory
3. If missing, extract package again

**Problem**: `ValidationError: Invalid configuration`

**Solution**:
1. Verify all required fields are filled (not placeholders)
2. Check IP addresses are valid (format: `"192.168.1.100"`)
3. Ensure `modbus_enabled: true` (not `false`)
4. Validate YAML syntax (use YAML validator online)

### SolaX Connection Errors

**Problem**: `ModbusTcpConnectionError: Failed to connect to inverter`

**Solutions**:
1. **Check IP addresses**: Ping inverters to verify they're reachable
   ```bash
   ping 192.168.1.100
   ```
2. **Verify Modbus enabled**: Check SolaX cloud or local web UI
3. **Check firewall**: Ensure port 502 is open
4. **Verify network**: Ensure Raspberry Pi is on same network as inverters
5. **Check WiFi dongles**: Ensure they're powered on and connected

**Problem**: `ModbusTimeoutError: Read timeout`

**Solutions**:
1. Increase timeout in config:
   ```yaml
   solaX_cloud_api:
     modbus_read_timeout: 10  # Increase from 5 to 10
   ```
2. Check network congestion
3. Verify inverters aren't busy with other operations

### Ohme Connection Errors

**Problem**: `AuthenticationError: Invalid credentials`

**Solutions**:
1. Verify email/password are correct (same as Ohme app)
2. Try logging into app.ohme.io with same credentials
3. Check for typos in config.yaml
4. Ensure no extra spaces around email/password

**Problem**: `ConnectionError: Failed to connect to Ohme API`

**Solutions**:
1. Check internet connection: `ping api-beta.ohme.io`
2. Verify firewall isn't blocking HTTPS
3. Check if Ohme API is down (check Ohme app)

### Dependency Errors

**Problem**: `ModuleNotFoundError: No module named 'pymodbus'`

**Solution**:
```bash
# Activate virtual environment
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Verify installation
pip list | grep pymodbus
```

**Problem**: Compilation errors during `pip install`

**Solution (Raspberry Pi)**:
```bash
# Update system packages first
sudo apt update
sudo apt upgrade

# Install build dependencies
sudo apt install python3-dev gcc

# Retry installation
pip install -r requirements.txt
```

### Runtime Errors

**Problem**: `Permission denied` when running scripts

**Solution**:
```bash
# Make scripts executable
chmod +x scripts/*.py

# Or run with python explicitly
python3 scripts/solax_modbus_status_report.py
```

**Problem**: Scripts can't find config.yaml when run from different directory

**Solution**:
- Always run scripts from package root: `cd /path/to/solax_ohme_package/`
- Or use absolute path: `python3 scripts/script.py --config /full/path/to/config.yaml`

---

## Advanced Topics

### Running Scripts on Boot (Raspberry Pi)

Create a systemd service:

```bash
# Create service file
sudo vi /etc/systemd/system/solax-monitor.service
```

Service file content:
```ini
[Unit]
Description=SolaX Monitoring Service
After=network.target

[Service]
Type=oneshot
User=pi
WorkingDirectory=/home/pi/solax_ohme_package
ExecStart=/home/pi/solax_ohme_package/venv/bin/python3 scripts/solax_modbus_status_report.py

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable solax-monitor.service
sudo systemctl start solax-monitor.service
```

### Scheduled Monitoring with Cron

Add to crontab (`crontab -e`):

```bash
# Run status report every 5 minutes
*/5 * * * * cd /home/pi/solax_ohme_package && /home/pi/solax_ohme_package/venv/bin/python3 scripts/solax_modbus_status_report.py >> /var/log/solax.log 2>&1

# Charge from grid at 02:00 daily
0 2 * * * cd /home/pi/solax_ohme_package && /home/pi/solax_ohme_package/venv/bin/python3 scripts/solax_modbus_read_and_set_workmode.py --charge

# Return to self-use at 05:00 daily
0 5 * * * cd /home/pi/solax_ohme_package && /home/pi/solax_ohme_package/venv/bin/python3 scripts/solax_modbus_read_and_set_workmode.py --self-use
```

---

## Package Structure

```
solax_ohme_package/
├── src/                          # Python package
│   ├── api_clients/              # SolaX and Ohme API clients
│   ├── config_manager/           # Configuration loading
│   ├── utils/                    # Utilities (exceptions, paths)
│   └── core_logic/               # Battery models and constants
├── scripts/                      # CLI tools
├── config/                       # Runtime data (mode change log)
├── config.yaml                   # Configuration file (edit with your settings)
├── requirements.txt              # Python dependencies
└── README.md                     # This file
```

---

## Getting Help

### Common Issues
1. **Modbus not connecting**: Verify Modbus TCP is enabled on WiFi dongles
2. **Ohme authentication fails**: Check credentials match Ohme app
3. **Import errors**: Ensure virtual environment activated and dependencies installed
4. **Permission errors**: Run scripts from package root directory

### Debug Mode

Enable detailed logging to diagnose issues:

```bash
# Enable DEBUG logging
python3 scripts/solax_modbus_status_report.py --log-level DEBUG
```

This will show:
- Modbus register reads/writes
- Network communication details
- API request/response data
- Detailed error traces

---

## License and Disclaimer

This package is provided as-is for personal use.

**Hardware Safety**: Modbus write operations modify inverter behavior. Use caution when changing work modes. The safety timing mechanisms exist to protect your hardware - only bypass them if you understand the risks.

**Security**: Keep `config.yaml` secure - it contains network addresses and API credentials. Do not commit it to version control or share it publicly.

**Support**: This is a standalone extraction - no official support provided. For questions about original codebase, contact the package maintainer.

---

## Version

**Package Version**: 1.0.0
**Extraction Date**: 2025
**Compatible Hardware**: SolaX X3 Hybrid G4 inverters, Ohme Home Pro chargers
**Python Version**: 3.9+
