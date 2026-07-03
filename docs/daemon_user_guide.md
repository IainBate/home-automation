# Battery Mode Daemon - User Guide

## Table of Contents

1. [Introduction](#introduction)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [Quick Start](#quick-start)
5. [Configuration Guide](#configuration-guide)
6. [Running the Daemon](#running-the-daemon)
7. [Monitoring and Logs](#monitoring-and-logs)
8. [Troubleshooting](#troubleshooting)
9. [Advanced Usage](#advanced-usage)
10. [Examples](#examples)

---

## Introduction

The Battery Mode Daemon is an autonomous background service that automatically manages your SolaX battery operating modes based on:

- **EV Charging Status**: Charge battery when your EV is charging
- **Time Schedules**: Automate battery modes at specific times (e.g., cheap rate periods)
- **Default Behavior**: Safe fallback mode when no rules apply

### What It Does

- Monitors your Ohme EV charger for active charging
- Automatically switches battery to FORCE_CHARGE when EV is charging
- Executes time-based schedules for optimization (e.g., cheap overnight charging, peak export)
- Prevents rapid mode changes that could damage hardware
- Runs continuously in the background with minimal intervention

### What It Doesn't Do

- Manual mode control (use web interface or Modbus client for that)
- Optimize electricity costs (that's the optimizer's job)
- Predict future pricing (use the forecast integration)
- Control the EV charger itself (it only monitors status)

---

## Prerequisites

### Required

1. **Python 3.10 or newer**
   ```bash
   python3 --version  # Should show 3.10 or higher
   ```

2. **SolaX Inverter with Modbus TCP enabled**
   - Configured in `config.yaml`
   - Master inverter IP address accessible

3. **Ohme EV Charger (optional)**
   - API credentials in `config.yaml`
   - Enabled in configuration

4. **System Configuration File**
   - `config.yaml` with SolaX and Ohme settings
   - Located in project root

### Optional

- **systemd** (Linux) or **launchd** (macOS) for automatic startup
- **logrotate** for additional log management (daemon handles this internally)

---

## Installation

### Step 1: Install Dependencies

The daemon uses the existing project dependencies. Ensure you've installed the package:

```bash
# From project root
pip install -e .

# Or install specific dependencies
pip install asyncio jsonschema pyyaml
```

### Step 2: Verify System Configuration

Check that your `config.yaml` has required settings:

```bash
# Check SolaX Modbus is enabled
grep "modbus_enabled" config.yaml

# Check Ohme is configured (if using)
grep -A 5 "ohme_ev:" config.yaml
```

### Step 3: Create Daemon Configuration

Copy the sample configuration:

```bash
cp battery_mode_daemon_config.json my_daemon_config.json
```

Edit `my_daemon_config.json` to match your needs (see [Configuration Guide](#configuration-guide))

### Step 4: Make Script Executable (optional)

```bash
chmod +x scripts/battery_mode_daemon.py
```

---

## Quick Start

### 1. Test Configuration

Validate your configuration without running the daemon:

```bash
python3 << 'EOF'
import json
import sys
sys.path.insert(0, '.')
from scripts.battery_mode_daemon import validate_daemon_config

with open("my_daemon_config.json", "r") as f:
    config = json.load(f)

is_valid, errors = validate_daemon_config(config)
if is_valid:
    print("✅ Configuration is valid")
else:
    print("❌ Configuration errors:")
    for error in errors:
        print(f"  - {error}")
EOF
```

### 2. Start the Daemon

```bash
python scripts/battery_mode_daemon.py my_daemon_config.json config.yaml
```

You should see:
```
2024-01-18 12:00:00,123 - battery_mode_daemon - INFO - 🚀 Battery Mode Daemon starting...
2024-01-18 12:00:00,456 - battery_mode_daemon - INFO - Daemon configuration loaded and validated
2024-01-18 12:00:00,789 - battery_mode_daemon - INFO - ⏳ First startup - waiting one cycle before mode changes
```

### 3. Watch Logs

In another terminal:

```bash
tail -f logs/battery_mode_daemon.log
```

### 4. Stop the Daemon

Press `Ctrl+C` for graceful shutdown:

```
2024-01-18 12:10:00,123 - battery_mode_daemon - INFO - Received shutdown signal (2), shutting down gracefully...
2024-01-18 12:10:00,456 - battery_mode_daemon - INFO - 👋 Daemon shutdown complete
```

---

## Configuration Guide

### Configuration File Structure

The daemon uses a JSON configuration file with four main sections:

```json
{
  "daemon_settings": { /* Core daemon behavior */ },
  "ohme_charging": { /* EV charging integration */ },
  "schedule": { /* Time-based automation */ },
  "logging": { /* Log settings */ }
}
```

### Section 1: daemon_settings

Controls core daemon behavior and safety limits.

```json
{
  "daemon_settings": {
    "hardware_poll_interval_seconds": 300,
    "min_mode_change_interval_seconds": 600,
    "ohme_charging_threshold_watts": 500
  }
}
```

#### Parameters

| Parameter | Type | Range | Default | Description |
|-----------|------|-------|---------|-------------|
| `hardware_poll_interval_seconds` | number | 60-3600 | 300 | How often to check hardware and change modes (seconds) |
| `min_mode_change_interval_seconds` | number | 60-7200 | 600 | Minimum time between daemon mode changes (seconds) |
| `ohme_charging_threshold_watts` | number | 0-10000 | 500 | Power threshold to consider Ohme "charging" (watts) |

#### Recommendations

**Hardware Poll Interval**:
- **Fast (60-120s)**: Responsive to EV charging, more API calls
- **Medium (300s)**: Good balance (recommended)
- **Slow (600-1800s)**: Reduce API calls, slower response

**Min Mode Change Interval**:
- **Standard (600s / 10min)**: Recommended for hardware safety
- **Aggressive (300s / 5min)**: Only if you understand hardware limits
- **Conservative (1200s / 20min)**: Extra protection for older inverters

**Ohme Charging Threshold**:
- **Low (100-500W)**: Trigger on slow charging (3kW)
- **Medium (500-1000W)**: Normal charging detection (recommended)
- **High (1000-2000W)**: Only fast charging (7kW+)

### Section 2: ohme_charging

Controls EV charging integration.

```json
{
  "ohme_charging": {
    "enabled": true,
    "force_charge_mode": "FORCE_CHARGE"
  }
}
```

#### Parameters

| Parameter | Type | Values | Default | Description |
|-----------|------|--------|---------|-------------|
| `enabled` | boolean | true/false | true | Enable Ohme charging detection |
| `force_charge_mode` | string | See below | "FORCE_CHARGE" | Battery mode when EV is charging |

#### force_charge_mode Values

| Mode | Effect | When to Use |
|------|--------|-------------|
| `FORCE_CHARGE` | Charge battery from grid | Use cheap electricity for both EV and battery |
| `SELF_USE` | Normal operation | Don't change battery mode during EV charging |
| `FORCE_DISCHARGE` | Export to grid | Advanced: sell expensive electricity (rare) |

**Typical Setup**: `"FORCE_CHARGE"` - charge battery while EV charges on cheap rate

### Section 3: schedule

Controls time-based battery mode automation.

```json
{
  "schedule": {
    "enabled": true,
    "default_mode": "SELF_USE",
    "time_ranges": [
      {
        "start_time": "00:30",
        "end_time": "04:30",
        "battery_mode": "FORCE_CHARGE",
        "description": "Cheap rate overnight charging"
      },
      {
        "start_time": "16:00",
        "end_time": "19:00",
        "battery_mode": "FORCE_DISCHARGE",
        "description": "Peak rate battery export"
      }
    ]
  }
}
```

#### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `enabled` | boolean | Enable time-based scheduling |
| `default_mode` | string | Mode to use outside scheduled ranges |
| `time_ranges` | array | List of time-based rules (see below) |

#### Time Range Object

| Field | Type | Format | Required | Description |
|-------|------|--------|----------|-------------|
| `start_time` | string | "HH:MM" | Yes | Start time (24-hour format) |
| `end_time` | string | "HH:MM" | Yes | End time (24-hour format) |
| `battery_mode` | string | See below | Yes | Battery mode for this period |
| `description` | string | Any text | No | Human-readable note for logs |

#### Battery Modes

| Mode | Effect | Typical Use Case |
|------|--------|------------------|
| `SELF_USE` | Natural charge/discharge | Default, no intervention |
| `FORCE_CHARGE` | Charge from grid | Cheap rate periods (00:00-04:00) |
| `FORCE_DISCHARGE` | Export to grid | Peak rate periods (16:00-19:00) |
| `MANUAL_STOP` | Battery disconnected | Maintenance or testing |

#### Time Range Examples

**Overnight Charging (00:30-04:30)**:
```json
{
  "start_time": "00:30",
  "end_time": "04:30",
  "battery_mode": "FORCE_CHARGE",
  "description": "Octopus Agile cheap rate"
}
```

**Peak Export (16:00-19:00)**:
```json
{
  "start_time": "16:00",
  "end_time": "19:00",
  "battery_mode": "FORCE_DISCHARGE",
  "description": "Peak rate export period"
}
```

**Midnight Crossing (23:00-01:00)**:
```json
{
  "start_time": "23:00",
  "end_time": "01:00",
  "battery_mode": "FORCE_CHARGE",
  "description": "Late night charging"
}
```
*Daemon automatically handles midnight crossing*

#### Overlapping Ranges

If ranges overlap, **first match wins**:

```json
{
  "time_ranges": [
    {
      "start_time": "00:00",
      "end_time": "04:00",
      "battery_mode": "FORCE_CHARGE",
      "description": "This will activate first"
    },
    {
      "start_time": "02:00",
      "end_time": "06:00",
      "battery_mode": "FORCE_DISCHARGE",
      "description": "This will NOT activate (overlaps above)"
    }
  ]
}
```

**Best Practice**: Avoid overlapping ranges for predictable behavior

### Section 4: logging

Controls daemon logging behavior.

```json
{
  "logging": {
    "level": "INFO",
    "file_path": "logs/battery_mode_daemon.log"
  }
}
```

#### Parameters

| Parameter | Type | Values | Default | Description |
|-----------|------|--------|---------|-------------|
| `level` | string | See below | "INFO" | Log verbosity level |
| `file_path` | string | File path | (shown) | Log file location |

#### Log Levels

| Level | What You'll See | When to Use |
|-------|----------------|-------------|
| `DEBUG` | Everything (very verbose) | Troubleshooting, development |
| `INFO` | Normal operations | Recommended for production |
| `WARNING` | Issues that don't stop daemon | Minimal logging |
| `ERROR` | Failures only | Only critical issues |
| `CRITICAL` | Severe failures | Not recommended (too quiet) |

**Recommendation**: Use `INFO` for normal operation, `DEBUG` for troubleshooting

---

## Running the Daemon

### Command-Line Syntax

```bash
python scripts/battery_mode_daemon.py <config_file> [system_config]
```

#### Arguments

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `config_file` | Yes | - | Path to daemon JSON configuration |
| `system_config` | No | `config.yaml` | Path to system YAML configuration |

### Running Methods

#### Foreground (Testing)

Run in current terminal (Ctrl+C to stop):

```bash
python scripts/battery_mode_daemon.py my_daemon_config.json
```

**Pros**: See logs immediately, easy to stop
**Cons**: Stops when terminal closes

#### Background (Production)

Run detached from terminal:

```bash
nohup python scripts/battery_mode_daemon.py my_daemon_config.json > /dev/null 2>&1 &
echo $! > daemon.pid
```

Stop with:
```bash
kill $(cat daemon.pid)
```

**Pros**: Survives terminal close
**Cons**: Manual management, no auto-restart

#### systemd Service (Linux - Recommended)

Create `/etc/systemd/system/battery-mode-daemon.service`:

```ini
[Unit]
Description=Battery Mode Daemon
After=network.target

[Service]
Type=simple
User=your-username
WorkingDirectory=/path/to/solax_ohme_package
ExecStart=/usr/bin/python3 /path/to/solax_ohme_package/scripts/battery_mode_daemon.py battery_mode_daemon_config.json config.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable battery-mode-daemon
sudo systemctl start battery-mode-daemon
```

Check status:
```bash
sudo systemctl status battery-mode-daemon
```

View logs:
```bash
sudo journalctl -u battery-mode-daemon -f
```

#### launchd (macOS)

Create `~/Library/LaunchAgents/com.solax.battery-mode-daemon.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.solax.battery-mode-daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>/path/to/solax_ohme_package/scripts/battery_mode_daemon.py</string>
        <string>/path/to/solax_ohme_package/battery_mode_daemon_config.json</string>
        <string>/path/to/solax_ohme_package/config.yaml</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/solax_ohme_package</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/path/to/solax_ohme_package/logs/daemon_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/solax_ohme_package/logs/daemon_stderr.log</string>
</dict>
</plist>
```

Load and start:
```bash
launchctl load ~/Library/LaunchAgents/com.solax.battery-mode-daemon.plist
```

Check status:
```bash
launchctl list | grep battery-mode-daemon
```

Stop:
```bash
launchctl unload ~/Library/LaunchAgents/com.solax.battery-mode-daemon.plist
```

---

## Monitoring and Logs

### Log Files

#### Main Log File

**Location**: `logs/battery_mode_daemon.log`

**Rotation**: Automatic at midnight (keeps 7 days)

**Format**:
```
YYYY-MM-DD HH:MM:SS,mmm - logger_name - LEVEL - Message
```

**Example**:
```
2024-01-18 12:00:00,123 - battery_mode_daemon - INFO - 🚀 Battery Mode Daemon starting...
2024-01-18 12:05:00,456 - battery_mode_daemon - INFO - ✅ Mode changed: SELF_USE → FORCE_CHARGE (reason: Ohme charging detected (7200W))
```

#### Backup Logs

**Naming**: `battery_mode_daemon.log.YYYY-MM-DD`

**Retention**: 7 days (automatically deleted)

**Example**:
```
logs/
├── battery_mode_daemon.log              # Today
├── battery_mode_daemon.log.2024-01-17   # Yesterday
├── battery_mode_daemon.log.2024-01-16
└── ...
```

### State Files

#### Mode Change Log

**Location**: `data/battery_mode_daemon_log.json`

**Purpose**: Tracks daemon's mode changes for safety interval enforcement

**View Current State**:
```bash
cat data/battery_mode_daemon_log.json | jq .
```

**View Change History**:
```bash
cat data/battery_mode_daemon_log.json | jq '.change_history[]'
```

**Example Output**:
```json
{
  "last_change_timestamp": 1705582800.123,
  "last_change_mode": "FORCE_CHARGE",
  "last_change_reason": "Ohme charging detected (7200W)",
  "change_history": [
    {
      "timestamp": 1705582800.123,
      "datetime": "2024-01-18T12:00:00",
      "mode": "FORCE_CHARGE",
      "reason": "Ohme charging detected (7200W)"
    }
  ]
}
```

### Log Monitoring

#### Watch Live Logs

```bash
tail -f logs/battery_mode_daemon.log
```

#### Search for Events

**Startup Events**:
```bash
grep "🚀" logs/battery_mode_daemon.log
```

**Mode Changes**:
```bash
grep "✅" logs/battery_mode_daemon.log
```

**Safety Violations**:
```bash
grep "🔥" logs/battery_mode_daemon.log
```

**Errors**:
```bash
grep "ERROR" logs/battery_mode_daemon.log
```

**Shutdown Events**:
```bash
grep "👋" logs/battery_mode_daemon.log
```

#### Log Event Reference

| Emoji | Event | Meaning |
|-------|-------|---------|
| 🚀 | Daemon starting | Successful startup |
| ⏳ | First startup delay | Waiting before first mode change |
| ✅ | Mode changed | Successful mode change |
| ❌ | Mode change failed | Hardware error during change |
| 🔥 | Rapid change blocked | Safety interval prevented change |
| 👋 | Daemon shutdown | Graceful shutdown complete |

### Performance Monitoring

#### Count Mode Changes

```bash
grep "✅ Mode changed" logs/battery_mode_daemon.log | wc -l
```

#### Count Safety Violations

```bash
grep "🔥 RAPID MODE CHANGE" logs/battery_mode_daemon.log | wc -l
```

#### Last Mode Change

```bash
cat data/battery_mode_daemon_log.json | jq -r '.last_change_reason'
```

#### Time Since Last Change

```bash
cat data/battery_mode_daemon_log.json | jq -r '.last_change_timestamp' | \
  xargs -I {} date -r {} '+%Y-%m-%d %H:%M:%S'
```

---

## Troubleshooting

### Common Issues

#### Daemon Won't Start

**Symptom**: Exits immediately with error

**Check**:
1. Configuration file exists:
   ```bash
   ls -l my_daemon_config.json
   ```

2. Configuration is valid JSON:
   ```bash
   python3 -m json.tool my_daemon_config.json
   ```

3. System config exists:
   ```bash
   ls -l config.yaml
   ```

4. Python dependencies installed:
   ```bash
   pip list | grep -E "jsonschema|pyyaml"
   ```

**Solution**: Fix configuration or install missing dependencies

#### Configuration Validation Errors

**Symptom**: Log shows validation errors on startup

**Example Error**:
```
ERROR - Configuration error at 'daemon_settings': 'ohme_charging_threshold_watts' is a required property
```

**Solution**: Add missing field to JSON configuration

**Common Validation Errors**:

| Error | Cause | Fix |
|-------|-------|-----|
| `'X' is a required property` | Missing field | Add field to config |
| `'invalid' is not of type 'number'` | Wrong data type | Use number, not string |
| `500 is greater than maximum` | Value out of range | Use value within limits |
| `does not match pattern` | Invalid time format | Use "HH:MM" format (e.g., "09:30") |

#### Ohme Connection Failures

**Symptom**: Logs show "Failed to check Ohme status"

**Check**:
1. Ohme enabled in `config.yaml`:
   ```bash
   grep "ohme_ev:" -A 5 config.yaml
   ```

2. Credentials correct:
   ```bash
   grep "username\|password" config.yaml
   ```

3. Network connectivity:
   ```bash
   ping api.ohme.io
   ```

**Solution**: Verify Ohme credentials and network access

**Graceful Degradation**: Daemon continues with schedule-only operation

#### Mode Changes Not Happening

**Symptom**: Daemon running but battery mode doesn't change

**Check**:
1. First startup delay (5 minutes):
   ```bash
   grep "⏳ First startup" logs/battery_mode_daemon.log
   ```

2. Safety interval blocking:
   ```bash
   grep "🔥 RAPID MODE CHANGE" logs/battery_mode_daemon.log
   ```

3. Current mode already matches target:
   ```bash
   grep "Already in .* mode" logs/battery_mode_daemon.log
   ```

4. Schedule is enabled:
   ```bash
   cat my_daemon_config.json | jq '.schedule.enabled'
   ```

5. Current time is within a scheduled range:
   ```bash
   date +%H:%M
   # Compare with time_ranges in config
   ```

**Solution**: Wait for startup delay, adjust safety interval, or verify schedule

#### Rapid Mode Change Blocking (🔥)

**Symptom**: Log shows fire emoji and "RAPID MODE CHANGE BLOCKED"

**Example**:
```
ERROR - 🔥 RAPID MODE CHANGE BLOCKED: Last change was 300 seconds ago (minimum: 600).
```

**Cause**: Trying to change mode within safety interval

**Solutions**:

1. **Wait**: Normal behavior, wait for safety interval to pass
2. **Adjust Interval**: Lower `min_mode_change_interval_seconds` (carefully!)
3. **Check Schedule**: Overlapping time ranges cause frequent triggers

**Warning**: Don't disable safety interval - it protects hardware

#### Modbus Connection Failures

**Symptom**: Log shows Modbus errors

**Check**:
1. Inverter is online:
   ```bash
   ping <inverter_ip>
   ```

2. Modbus enabled in `config.yaml`:
   ```bash
   grep "modbus_enabled" config.yaml
   ```

3. Correct IP address:
   ```bash
   grep "master_ip\|slave_ip" config.yaml
   ```

**Solution**: Verify network and Modbus configuration

**Graceful Degradation**: Daemon skips cycle and retries

#### Logs Not Rotating

**Symptom**: `battery_mode_daemon.log` grows indefinitely

**Check**:
1. Permissions on logs directory:
   ```bash
   ls -ld logs/
   ```

2. Disk space:
   ```bash
   df -h .
   ```

**Solution**: Fix permissions or free disk space

**Automatic Rotation**: Daemon uses `TimedRotatingFileHandler` (should work automatically)

### Debug Mode

Enable debug logging for detailed troubleshooting:

**In Configuration**:
```json
{
  "logging": {
    "level": "DEBUG",
    "file_path": "logs/battery_mode_daemon.log"
  }
}
```

**What You'll See**:
- Configuration reload attempts
- Hardware cycle details
- Mode comparison logic
- Safety interval calculations
- Detailed error traces

**Warning**: DEBUG logs are verbose - use temporarily for troubleshooting

### Getting Help

**Before Asking**:
1. Check logs for errors
2. Verify configuration with validator
3. Review this troubleshooting section

**When Asking for Help, Include**:
1. Daemon version (Git commit)
2. Configuration file (redact credentials)
3. Last 50 lines of log:
   ```bash
   tail -50 logs/battery_mode_daemon.log
   ```
4. Mode change log:
   ```bash
   cat data/battery_mode_daemon_log.json
   ```
5. System info:
   ```bash
   python3 --version
   uname -a
   ```

---

## Advanced Usage

### Custom Polling Intervals

**Aggressive (Fast Response)**:
```json
{
  "daemon_settings": {
    "hardware_poll_interval_seconds": 120,  // 2 minutes
    "min_mode_change_interval_seconds": 300  // 5 minutes
  }
}
```

**Conservative (Minimal API Calls)**:
```json
{
  "daemon_settings": {
    "hardware_poll_interval_seconds": 900,   // 15 minutes
    "min_mode_change_interval_seconds": 1800  // 30 minutes
  }
}
```

### Multiple Configuration Scenarios

**Scenario 1: Ohme Only (No Schedule)**

```json
{
  "daemon_settings": { /* ... */ },
  "ohme_charging": {
    "enabled": true,
    "force_charge_mode": "FORCE_CHARGE"
  },
  "schedule": {
    "enabled": false,
    "default_mode": "SELF_USE",
    "time_ranges": []
  },
  "logging": { /* ... */ }
}
```

**Scenario 2: Schedule Only (No Ohme)**

```json
{
  "daemon_settings": { /* ... */ },
  "ohme_charging": {
    "enabled": false,
    "force_charge_mode": "FORCE_CHARGE"
  },
  "schedule": {
    "enabled": true,
    "default_mode": "SELF_USE",
    "time_ranges": [
      /* ... time ranges ... */
    ]
  },
  "logging": { /* ... */ }
}
```

**Scenario 3: Both Ohme and Schedule**

```json
{
  "daemon_settings": { /* ... */ },
  "ohme_charging": {
    "enabled": true,
    "force_charge_mode": "FORCE_CHARGE"
  },
  "schedule": {
    "enabled": true,
    "default_mode": "SELF_USE",
    "time_ranges": [
      /* ... time ranges ... */
    ]
  },
  "logging": { /* ... */ }
}
```

**Priority**: Ohme charging always takes precedence over schedule

### Hot Configuration Reload

**Edit config while daemon running**:

1. Modify `my_daemon_config.json`
2. Save file
3. Wait up to 30 seconds (fast poll interval)
4. Check logs for reload confirmation:
   ```bash
   tail -f logs/battery_mode_daemon.log | grep "Configuration reloaded"
   ```

**Invalid Changes**: Daemon rejects and keeps old configuration

**No Restart Needed**: Configuration updates apply automatically

### Testing Configuration Changes

**Validate Before Deploying**:

```bash
# Test new config
python3 << 'EOF'
import json, sys
sys.path.insert(0, '.')
from scripts.battery_mode_daemon import validate_daemon_config

with open("new_config.json", "r") as f:
    config = json.load(f)

is_valid, errors = validate_daemon_config(config)
print("✅ Valid" if is_valid else "❌ Invalid")
for error in errors:
    print(f"  {error}")
EOF

# If valid, deploy
mv my_daemon_config.json my_daemon_config.json.backup
mv new_config.json my_daemon_config.json
```

**Rollback**:
```bash
mv my_daemon_config.json.backup my_daemon_config.json
```

### Running Multiple Instances

**Use Case**: Multiple battery systems

**Setup**:
1. Create separate config files:
   ```bash
   cp battery_mode_daemon_config.json system1_daemon.json
   cp battery_mode_daemon_config.json system2_daemon.json
   ```

2. Configure different log files:
   ```json
   {
     "logging": {
       "level": "INFO",
       "file_path": "logs/system1_daemon.log"
     }
   }
   ```

3. Start each daemon:
   ```bash
   python scripts/battery_mode_daemon.py system1_daemon.json config_system1.yaml &
   python scripts/battery_mode_daemon.py system2_daemon.json config_system2.yaml &
   ```

**Warning**: Ensure separate log files and data directories to avoid conflicts

---

## Examples

### Example 1: Octopus Agile Overnight Charging

**Goal**: Charge battery during cheapest hours (00:30-04:30)

**Configuration**:
```json
{
  "daemon_settings": {
    "hardware_poll_interval_seconds": 300,
    "min_mode_change_interval_seconds": 600,
    "ohme_charging_threshold_watts": 500
  },
  "ohme_charging": {
    "enabled": true,
    "force_charge_mode": "FORCE_CHARGE"
  },
  "schedule": {
    "enabled": true,
    "default_mode": "SELF_USE",
    "time_ranges": [
      {
        "start_time": "00:30",
        "end_time": "04:30",
        "battery_mode": "FORCE_CHARGE",
        "description": "Octopus Agile cheap rate charging"
      }
    ]
  },
  "logging": {
    "level": "INFO",
    "file_path": "logs/battery_mode_daemon.log"
  }
}
```

**Expected Behavior**:
- 00:30: Battery switches to FORCE_CHARGE
- 04:30: Battery switches to SELF_USE
- If EV charging detected: Battery immediately switches to FORCE_CHARGE (overrides schedule)

### Example 2: Peak Export Strategy

**Goal**: Export battery power during peak rates (16:00-19:00)

**Configuration**:
```json
{
  "daemon_settings": {
    "hardware_poll_interval_seconds": 300,
    "min_mode_change_interval_seconds": 600,
    "ohme_charging_threshold_watts": 500
  },
  "ohme_charging": {
    "enabled": false,
    "force_charge_mode": "FORCE_CHARGE"
  },
  "schedule": {
    "enabled": true,
    "default_mode": "SELF_USE",
    "time_ranges": [
      {
        "start_time": "00:30",
        "end_time": "04:30",
        "battery_mode": "FORCE_CHARGE",
        "description": "Cheap rate charging"
      },
      {
        "start_time": "16:00",
        "end_time": "19:00",
        "battery_mode": "FORCE_DISCHARGE",
        "description": "Peak rate export"
      }
    ]
  },
  "logging": {
    "level": "INFO",
    "file_path": "logs/battery_mode_daemon.log"
  }
}
```

**Expected Behavior**:
- 00:30-04:30: Charge from grid
- 04:30-16:00: Normal operation (SELF_USE)
- 16:00-19:00: Force discharge to grid
- 19:00-00:30: Normal operation (SELF_USE)

### Example 3: EV-Only Automation

**Goal**: Only respond to EV charging, no time schedule

**Configuration**:
```json
{
  "daemon_settings": {
    "hardware_poll_interval_seconds": 180,
    "min_mode_change_interval_seconds": 600,
    "ohme_charging_threshold_watts": 1000
  },
  "ohme_charging": {
    "enabled": true,
    "force_charge_mode": "FORCE_CHARGE"
  },
  "schedule": {
    "enabled": false,
    "default_mode": "SELF_USE",
    "time_ranges": []
  },
  "logging": {
    "level": "INFO",
    "file_path": "logs/battery_mode_daemon.log"
  }
}
```

**Expected Behavior**:
- Normal: Battery in SELF_USE
- EV charging > 1000W: Battery switches to FORCE_CHARGE
- EV stops: Battery switches to SELF_USE (after 10 minutes)

### Example 4: Weekend vs Weekday (Manual Switch)

**Goal**: Different schedules for weekdays and weekends

**Solution**: Maintain two config files and swap them manually

**weekday_daemon.json**:
```json
{
  "schedule": {
    "enabled": true,
    "default_mode": "SELF_USE",
    "time_ranges": [
      {
        "start_time": "00:30",
        "end_time": "04:30",
        "battery_mode": "FORCE_CHARGE",
        "description": "Weekday cheap charging"
      }
    ]
  }
}
```

**weekend_daemon.json**:
```json
{
  "schedule": {
    "enabled": true,
    "default_mode": "SELF_USE",
    "time_ranges": [
      {
        "start_time": "01:00",
        "end_time": "06:00",
        "battery_mode": "FORCE_CHARGE",
        "description": "Weekend extended charging"
      }
    ]
  }
}
```

**Switch Configs**:
```bash
# Friday evening
cp weekend_daemon.json battery_mode_daemon_config.json

# Sunday evening
cp weekday_daemon.json battery_mode_daemon_config.json
```

**Automatic Switching**: Use cron job (Linux) or scheduled task (Windows)

---

## Safety Best Practices

### Do's

✅ **Start with conservative intervals**: Use default 10-minute safety interval
✅ **Test configuration**: Validate before deploying
✅ **Monitor logs**: Check for errors regularly
✅ **Backup configs**: Keep working configurations saved
✅ **Gradual changes**: Adjust one parameter at a time
✅ **Review mode changes**: Check logs match expectations

### Don'ts

❌ **Don't disable safety interval**: Minimum 60 seconds
❌ **Don't ignore 🔥 logs**: Investigate safety violations
❌ **Don't run multiple daemons**: On same battery system without coordination
❌ **Don't change modes manually**: While daemon is running (can cause conflicts)
❌ **Don't use aggressive polling**: Without understanding hardware limits
❌ **Don't skip validation**: Always validate configuration changes

### Hardware Protection

1. **Safety Interval**: Minimum 10 minutes between daemon changes
2. **Skip-If-Same**: Daemon won't change if already in target mode
3. **Error Fallback**: All failures default to SELF_USE (safest mode)
4. **Modbus Validation**: Underlying client validates mode combinations
5. **Master-Only**: Daemon only controls master inverter

### Recommended Schedule Guidelines

**Minimum Duration**: 30 minutes per scheduled period
**Maximum Changes**: ~6 per day (within safety limits)
**Gap Between Ranges**: At least 10 minutes
**Avoid Overlaps**: Can cause rapid switching

**Good Schedule**:
```
00:30-04:30: FORCE_CHARGE
16:00-19:00: FORCE_DISCHARGE
```

**Bad Schedule** (too frequent):
```
00:00-01:00: FORCE_CHARGE
01:00-02:00: SELF_USE
02:00-03:00: FORCE_CHARGE
03:00-04:00: SELF_USE
```

---

## Appendix

### Configuration Schema Reference

Complete JSON schema for validation:

```json
{
  "type": "object",
  "required": ["daemon_settings", "ohme_charging", "schedule", "logging"],
  "properties": {
    "daemon_settings": {
      "type": "object",
      "required": [
        "hardware_poll_interval_seconds",
        "min_mode_change_interval_seconds",
        "ohme_charging_threshold_watts"
      ],
      "properties": {
        "hardware_poll_interval_seconds": {
          "type": "number",
          "minimum": 60,
          "maximum": 3600
        },
        "min_mode_change_interval_seconds": {
          "type": "number",
          "minimum": 60,
          "maximum": 7200
        },
        "ohme_charging_threshold_watts": {
          "type": "number",
          "minimum": 0,
          "maximum": 10000
        }
      }
    },
    "ohme_charging": {
      "type": "object",
      "required": ["enabled", "force_charge_mode"],
      "properties": {
        "enabled": {"type": "boolean"},
        "force_charge_mode": {
          "type": "string",
          "enum": ["FORCE_CHARGE", "SELF_USE", "FORCE_DISCHARGE"]
        }
      }
    },
    "schedule": {
      "type": "object",
      "required": ["enabled", "default_mode", "time_ranges"],
      "properties": {
        "enabled": {"type": "boolean"},
        "default_mode": {
          "type": "string",
          "enum": ["SELF_USE", "FORCE_CHARGE", "FORCE_DISCHARGE", "MANUAL_STOP"]
        },
        "time_ranges": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["start_time", "end_time", "battery_mode"],
            "properties": {
              "start_time": {
                "type": "string",
                "pattern": "^([0-1][0-9]|2[0-3]):[0-5][0-9]$"
              },
              "end_time": {
                "type": "string",
                "pattern": "^([0-1][0-9]|2[0-3]):[0-5][0-9]$"
              },
              "battery_mode": {
                "type": "string",
                "enum": ["SELF_USE", "FORCE_CHARGE", "FORCE_DISCHARGE", "MANUAL_STOP"]
              },
              "description": {"type": "string"}
            }
          }
        }
      }
    },
    "logging": {
      "type": "object",
      "required": ["level", "file_path"],
      "properties": {
        "level": {
          "type": "string",
          "enum": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        },
        "file_path": {"type": "string", "minLength": 1}
      }
    }
  }
}
```

### File Locations Reference

| File/Directory | Purpose | Auto-Created |
|----------------|---------|--------------|
| `battery_mode_daemon_config.json` | Daemon configuration | No (manual) |
| `config.yaml` | System configuration | No (existing) |
| `scripts/battery_mode_daemon.py` | Daemon script | No (repository) |
| `logs/` | Log directory | Yes |
| `logs/battery_mode_daemon.log` | Current log file | Yes |
| `logs/battery_mode_daemon.log.*` | Rotated log backups | Yes |
| `data/` | State directory | Yes |
| `data/battery_mode_daemon_log.json` | Mode change history | Yes |

### Exit Codes

| Code | Meaning | Cause |
|------|---------|-------|
| 0 | Success | Graceful shutdown (SIGTERM/SIGINT) |
| 1 | Error | Missing arguments, invalid config, fatal error |

### Support Resources

- **Design Documentation**: `docs/daemon_design.md`
- **User Guide**: `docs/daemon_user_guide.md` (this file)
- **Source Code**: `scripts/battery_mode_daemon.py`
- **Project Issues**: GitHub repository issues

---

**Document Version**: 1.0
**Last Updated**: 2024-01-18
**Daemon Version**: 1.0.0
