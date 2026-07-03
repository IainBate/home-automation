# Battery Protection Feature - Patch Information

## Overview

This patch implements SOC (State of Charge) protection to prevent FORCE_DISCHARGE mode when battery levels are critically low.

## Patch Statistics

- **Files Modified**: 4
- **Lines Added**: 209
- **Lines Removed**: 15
- **Patch File**: `battery_protection.patch`

## Modified Files

1. **scripts/battery_mode_daemon.py** - Main daemon implementation
2. **battery_mode_daemon_config.json** - Configuration with new threshold setting
3. **docs/daemon_user_guide.md** - User documentation
4. **docs/daemon_design.md** - Technical design documentation

## Features Implemented

### 1. Battery Protection System
- Checks both master and slave battery SOC levels
- Uses minimum SOC value for protection decisions
- Configurable threshold (default 20%, range 10-80%)
- Two-level protection:
  - **Preventive**: Blocks entering FORCE_DISCHARGE when SOC is low
  - **Emergency**: Exits FORCE_DISCHARGE if SOC drops during operation

### 2. Configuration
- New parameter: `min_discharge_soc_percent` (optional, default 20%)
- JSON schema validation ensures valid range (10-80%)
- Backward compatible - works without the parameter

### 3. Safety Features
- Fail-safe: Blocks FORCE_DISCHARGE if SOC cannot be read
- Override to SELF_USE instead of hard blocking
- Battery emoji (🪫) logging for visibility

## How to Apply the Patch

### Option 1: Using patch command

```bash
cd /path/to/home_automation
patch -p0 < battery_protection.patch
```

### Option 2: Using git apply

```bash
cd /path/to/home_automation
git apply battery_protection.patch
```

### Option 3: Manual review and apply

```bash
# Review the changes
less battery_protection.patch

# Apply to specific files manually
patch scripts/battery_mode_daemon.py < battery_protection.patch
```

## Verification After Applying

1. **Check syntax**:
   ```bash
   python3 -m py_compile scripts/battery_mode_daemon.py
   ```

2. **Validate configuration**:
   ```bash
   python3 -m json.tool battery_mode_daemon_config.json
   ```

3. **Test daemon startup**:
   ```bash
   python3 scripts/battery_mode_daemon.py battery_mode_daemon_config.json --version
   ```

## Configuration Update

Add to your `battery_mode_daemon_config.json`:

```json
{
  "daemon_settings": {
    "hardware_poll_interval_seconds": 60,
    "min_mode_change_interval_seconds": 600,
    "ohme_charging_threshold_watts": 500,
    "min_discharge_soc_percent": 20
  }
}
```

## Behavior

| Scenario | Result |
|----------|--------|
| Schedule requests FORCE_DISCHARGE, SOC=25% (both batteries) | Allow |
| Schedule requests FORCE_DISCHARGE, master=25%, slave=18% | Block (use SELF_USE) |
| Already in FORCE_DISCHARGE, battery drops to 20% | Switch to SELF_USE |
| Cannot read battery SOC | Block FORCE_DISCHARGE (fail-safe) |

## Log Messages

Watch for these new log messages:

- `🪫 Battery protection: SOC X% at/below threshold Y% - blocking FORCE_DISCHARGE`
- `🪫 Battery protection: SOC X% at/below threshold Y% - switching back to SELF_USE`

Search logs:
```bash
grep "🪫" logs/battery_mode_daemon.log
```

## Rollback

To remove the changes:

```bash
cd /path/to/home_automation
patch -R -p0 < battery_protection.patch
```

## Questions or Issues

- Review `docs/daemon_user_guide.md` for user documentation
- Review `docs/daemon_design.md` for technical details
- Check the troubleshooting section for common issues

---

**Implementation Date**: 2026-01-18
**Patch Version**: 1.0
