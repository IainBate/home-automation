# Battery Mode Daemon - Design Documentation

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Two-Tier Polling System](#two-tier-polling-system)
4. [Priority-Based Decision Logic](#priority-based-decision-logic)
5. [Safety Mechanisms](#safety-mechanisms)
6. [Error Handling Strategy](#error-handling-strategy)
7. [Data Structures](#data-structures)
8. [Code Organization](#code-organization)
9. [Design Decisions](#design-decisions)
10. [Implementation Details](#implementation-details)
11. [Testing Strategy](#testing-strategy)

---

## Overview

The Battery Mode Daemon is an autonomous background service that manages SolaX battery operating modes based on multiple inputs with a defined priority system. It bridges three distinct systems:

1. **Ohme EV Charger** - Real-time charging status via API
2. **SolaX Inverter** - Battery mode control via Modbus TCP
3. **User Schedule** - Time-based battery mode automation

### Key Design Principles

1. **Autonomy** - Runs continuously without user intervention
2. **Safety-First** - Multiple layers of protection against hardware damage
3. **Graceful Degradation** - Continues operation even when subsystems fail
4. **Hot-Reload Configuration** - Updates settings without restart
5. **Observability** - Comprehensive logging with structured events

### Core Capabilities

- **EV Charging Integration**: Automatically charges battery when EV is charging
- **Time-Based Scheduling**: Execute predefined battery modes at specific times
- **Safety Intervals**: Prevent rapid mode changes that could damage hardware
- **Error Recovery**: Automatic fallback to safe mode on any failure
- **Configuration Validation**: Schema-based validation prevents invalid settings

---

## Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    Battery Mode Daemon                          │
│                                                                 │
│  ┌──────────────┐         ┌──────────────┐                    │
│  │ Fast Loop    │────────▶│ Config       │                    │
│  │ (30 sec)     │         │ Reload       │                    │
│  └──────────────┘         └──────────────┘                    │
│                                                                 │
│  ┌──────────────┐         ┌──────────────┐                    │
│  │ Slow Loop    │────────▶│ Hardware     │                    │
│  │ (5 min)      │         │ Operations   │                    │
│  └──────────────┘         └──────────────┘                    │
│                                 │                               │
│                    ┌────────────┼────────────┐                 │
│                    ▼            ▼            ▼                 │
│            ┌──────────┐  ┌──────────┐  ┌──────────┐           │
│            │ Priority │  │ Safety   │  │ Mode     │           │
│            │ Logic    │  │ Check    │  │ Change   │           │
│            └──────────┘  └──────────┘  └──────────┘           │
└─────────────────────────────────────────────────────────────────┘
         │              │              │
         ▼              ▼              ▼
   ┌─────────┐    ┌─────────┐    ┌─────────┐
   │ Ohme    │    │ SolaX   │    │ JSON    │
   │ API     │    │ Modbus  │    │ Config  │
   └─────────┘    └─────────┘    └─────────┘
```

### Component Interaction

1. **Config Manager**: Loads and validates JSON configuration
2. **Ohme Client**: Async API wrapper for EV charger status
3. **Modbus Client**: Synchronous Modbus TCP for battery control
4. **Decision Engine**: Priority-based mode selection
5. **Safety Controller**: Enforces minimum intervals and validates changes
6. **Log Manager**: Rotating file handler with structured logging

### File Dependencies

```
battery_mode_daemon.py
├── src/api_clients/ohme_ev_client.py       # Async EV charger API
├── src/api_clients/solax_modbus_client.py  # Modbus battery control
├── src/config_manager/config_manager.py    # YAML config loader
├── src/core_logic/battery_simulation.py    # BatteryMode enum
└── External Libraries:
    ├── asyncio        # Async/await support
    ├── json           # Config parsing
    ├── jsonschema     # Schema validation
    └── signal         # Graceful shutdown
```

---

## Two-Tier Polling System

### Rationale

The daemon uses two independent polling loops with different frequencies to balance responsiveness and hardware stress:

1. **Fast Loop (30 seconds)**: Configuration changes need quick propagation
2. **Slow Loop (5 minutes)**: Hardware operations are expensive and slow-changing

### Design Benefits

| Aspect | Benefit |
|--------|---------|
| **Configuration Updates** | Changes apply within 30 seconds without restart |
| **Hardware Stress** | Minimizes API calls and Modbus operations |
| **Responsiveness** | Quick reaction to config changes |
| **Reliability** | Independent failure domains |

### Implementation

```python
def run(self) -> None:
    """Main daemon loop with two-tier polling."""
    last_hardware_check = 0
    fast_poll_interval = 30  # seconds

    while not self.shutdown_requested:
        loop_start = time.time()

        # Fast loop: Always reload config
        self._reload_config()

        # Slow loop: Hardware operations at configured interval
        hardware_interval = self.daemon_config["daemon_settings"][
            "hardware_poll_interval_seconds"
        ]
        if time.time() - last_hardware_check >= hardware_interval:
            if self.startup_complete:
                self._perform_hardware_cycle()
            else:
                # First startup - wait one cycle
                self.startup_complete = True

            last_hardware_check = time.time()

        # Sleep until next fast poll
        elapsed = time.time() - loop_start
        sleep_time = max(0, fast_poll_interval - elapsed)
        time.sleep(sleep_time)
```

### Fast Loop Operations

**Purpose**: Reload configuration from JSON file

**Frequency**: Every 30 seconds

**Operations**:
1. Check if config file exists
2. Parse JSON (with error handling)
3. Validate against schema
4. Compare with current config
5. Update if changed

**Failure Handling**: Keeps old configuration, logs error

**Why 30 seconds?**: Balance between responsiveness and file I/O overhead

### Slow Loop Operations

**Purpose**: Perform hardware checks and mode changes

**Frequency**: Configurable (default 300 seconds / 5 minutes)

**Operations**:
1. Fetch Ohme charger status (async API call)
2. Determine target mode based on priority logic
3. Check current battery mode (Modbus read)
4. Validate safety interval
5. Execute mode change if needed (Modbus write)

**Failure Handling**: Falls back to SELF_USE mode, continues daemon

**Why 5 minutes?**:
- Ohme API rate limiting
- Battery mode changes are slow-acting (30+ minutes to effect)
- Reduces Modbus traffic
- Balances responsiveness with hardware stress

### First Startup Delay

**Design Decision**: Wait one full hardware cycle before making any mode changes

**Rationale**:
- Allows daemon to observe current state
- Prevents startup transients
- Validates all systems are reachable
- Gives time for network connections to stabilize

**Implementation**:
```python
if self.startup_complete:
    self._perform_hardware_cycle()
else:
    self.logger.info("⏳ First startup - waiting one cycle before mode changes")
    self.startup_complete = True
```

---

## Priority-Based Decision Logic

### Priority Hierarchy

The daemon evaluates conditions in strict priority order:

```
1. ERROR STATE    → SELF_USE (safety fallback)
2. OHME CHARGING  → FORCE_CHARGE (EV integration)
3. TIME SCHEDULE  → Scheduled mode (automation)
4. DEFAULT MODE   → SELF_USE (outside ranges)
```

### Logic Flow

```
┌─────────────────────────┐
│ Check Ohme Status       │
│ (API call with retry)   │
└────────┬────────────────┘
         │
         ▼
┌─────────────────────────┐     YES     ┌──────────────────┐
│ Ohme Enabled &          │────────────▶│ FORCE_CHARGE     │
│ Power > Threshold?      │             │ (Priority 1)     │
└────────┬────────────────┘             └──────────────────┘
         │ NO
         ▼
┌─────────────────────────┐     YES     ┌──────────────────┐
│ Schedule Enabled &      │────────────▶│ Scheduled Mode   │
│ Current Time in Range?  │             │ (Priority 2)     │
└────────┬────────────────┘             └──────────────────┘
         │ NO
         ▼
┌─────────────────────────┐
│ Default Mode            │
│ (SELF_USE)              │
└─────────────────────────┘
```

### Implementation

```python
def _determine_target_mode(
    self, ohme_status: dict[str, Any] | None
) -> tuple[BatteryMode, str]:
    """Determine target mode based on priority logic.

    Priority: Error > Ohme > Schedule > Default
    """
    # Priority 1: Check Ohme charging
    if self.daemon_config["ohme_charging"]["enabled"]:
        if self._is_ohme_charging(ohme_status):
            mode = BatteryMode(
                self.daemon_config["ohme_charging"]["force_charge_mode"]
            )
            power = ohme_status.get("power_watts", 0) if ohme_status else 0
            reason = f"Ohme charging detected ({power}W)"
            return mode, reason

    # Priority 2: Check time schedule
    if self.daemon_config["schedule"]["enabled"]:
        scheduled_mode = self._get_scheduled_mode()
        if scheduled_mode is not None:
            # Build reason string with time range and description
            return scheduled_mode, reason

    # Priority 3: Default mode
    default_mode = BatteryMode(self.daemon_config["schedule"]["default_mode"])
    return default_mode, "Default mode (outside scheduled ranges)"
```

### Ohme Charging Detection

**Threshold-Based**: Uses configurable wattage threshold (default 500W)

**Why Threshold?**:
- Ohme status can be ambiguous (plugged but not charging)
- Power draw is definitive indicator of active charging
- Prevents false triggers from standby power

```python
def _is_ohme_charging(self, status: dict[str, Any] | None) -> bool:
    """Check if Ohme is actively charging above threshold."""
    if status is None:
        return False

    threshold = self.daemon_config["daemon_settings"][
        "ohme_charging_threshold_watts"
    ]
    power_watts = status.get("power_watts", 0)

    return power_watts > threshold
```

### Time Schedule Evaluation

**Midnight Crossing Handling**: Special logic for ranges like 23:00-01:00

```python
def _get_scheduled_mode(self) -> BatteryMode | None:
    """Evaluate time ranges for current time."""
    if not self.daemon_config["schedule"]["enabled"]:
        return None

    now = datetime.now().time()

    for range_config in self.daemon_config["schedule"]["time_ranges"]:
        start = datetime.strptime(range_config["start_time"], "%H:%M").time()
        end = datetime.strptime(range_config["end_time"], "%H:%M").time()

        # Handle midnight crossing (e.g., 23:00-01:00)
        if start <= end:
            # Normal range: 08:00-17:00
            if start <= now <= end:
                return BatteryMode(range_config["battery_mode"])
        else:
            # Crosses midnight: 23:00-01:00
            if now >= start or now <= end:
                return BatteryMode(range_config["battery_mode"])

    return None
```

**Edge Cases Handled**:
- Empty time ranges (returns None, falls through to default)
- Overlapping ranges (first match wins)
- Midnight crossing (23:00-01:00)
- Invalid time formats (caught by schema validation)

---

## Safety Mechanisms

### Minimum Mode Change Interval

**Purpose**: Prevent rapid mode switching that could damage hardware

**Default**: 10 minutes (600 seconds)

**Enforcement**: Daemon tracks its own mode changes, not manual changes

**Rationale**:
- Battery hardware has thermal and electrical stress limits
- Inverters need settling time between mode transitions
- Grid connection stability during mode switches

### Implementation

```python
def _can_change_mode(self) -> tuple[bool, int]:
    """Check minimum interval since last daemon change."""
    if self.last_mode_change_time is None:
        return True, 0

    elapsed = time.time() - self.last_mode_change_time
    min_interval = self.daemon_config["daemon_settings"][
        "min_mode_change_interval_seconds"
    ]

    return elapsed >= min_interval, int(elapsed)
```

### Safety Violation Logging

**Fire Emoji (🔥)**: High-visibility logging for blocked rapid changes

```python
if not can_change:
    self.logger.error(
        "🔥 RAPID MODE CHANGE BLOCKED: Last change was %d seconds ago "
        "(minimum: %d). Would have changed from %s to %s (reason: %s)",
        elapsed, min_interval, current_mode.value, target_mode.value, reason
    )
    return
```

**Why This Matters**:
- Administrators can grep logs for "🔥" to find safety events
- Indicates configuration might need adjustment
- Helps diagnose scheduling conflicts

### Daemon-Only Tracking

**Design Decision**: Only track daemon's own changes, not manual changes

**Rationale**:
- Users may need to manually intervene during daemon operation
- Manual changes indicate user intent (should not block daemon)
- Daemon only responsible for its own behavior

**Implementation**: Mode change log stored in `data/battery_mode_daemon_log.json`

### Skip-If-Already-Set Optimization

**Safety Feature**: Don't change mode if already in target mode

**Benefits**:
- Reduces unnecessary Modbus traffic
- Prevents mode change counter increment
- Avoids safety interval reset

```python
# Read current mode
current_mode = solax_modbus_work_mode(self.system_config)

# Skip if already in target mode
if current_mode == target_mode:
    self.logger.debug("Already in %s mode, skipping change", target_mode.value)
    return
```

### Mode Change Validation

**Modbus Client Safety**: The underlying `solax_modbus_set_work_mode()` function includes:
- Valid mode combination checking
- Master-only write restrictions
- Hardware-specific validation

**Daemon Layer**: Adds temporal safety (intervals)

**Result Checking**: Daemon validates success before logging

```python
result = solax_modbus_set_work_mode(
    self.system_config, target_mode,
    changed_by="daemon", force_unsafe=False
)

if result["success"]:
    self.logger.info("✅ Mode changed: %s → %s (reason: %s)", ...)
    self._save_mode_change_log(target_mode, reason)
    self.last_mode_change_time = time.time()
else:
    self.logger.error("❌ Mode change failed: %s (error: %s)", ...)
```

---

## Error Handling Strategy

### Failure Domains

The daemon identifies distinct failure domains with appropriate responses:

| Domain | Failure Mode | Response | Impact |
|--------|-------------|----------|--------|
| **Configuration** | Invalid JSON | Keep old config, log error | Continue with last valid config |
| **Schema Validation** | Missing fields | Reject config, keep old | Prevent invalid behavior |
| **Ohme API** | Connection failure | Assume not charging | Continue with schedule |
| **Ohme API** | Auth failure | Log error, return None | Fall through to schedule |
| **SolaX Modbus** | Read failure | Skip cycle, retry next | Wait for recovery |
| **SolaX Modbus** | Write failure | Log error, continue | Retry next cycle |
| **Hardware Cycle** | Any exception | Set SELF_USE, log | Safety fallback |

### Graceful Degradation Hierarchy

```
All Systems OK → Normal Operation
     │
     ├─▶ Ohme Failure → Use Schedule Only
     │
     ├─▶ Schedule Disabled → Use Ohme + Default
     │
     ├─▶ Modbus Read Failure → Skip Cycle, Retry
     │
     └─▶ Critical Failure → SELF_USE Mode
```

### Error Recovery: SELF_USE Fallback

**SELF_USE Mode**: Safest battery mode (natural charge/discharge)

**When Applied**:
- Unhandled exception in hardware cycle
- Catastrophic failure (should never happen)

```python
def _perform_hardware_cycle(self) -> None:
    """Execute one hardware check and mode change cycle."""
    try:
        # Check Ohme status
        ohme_status = self._check_ohme_status()

        # Determine target mode
        target_mode, reason = self._determine_target_mode(ohme_status)

        # Set mode if needed
        self._set_mode_safely(target_mode, reason)

    except Exception:
        self.logger.exception(
            "❌ Hardware cycle failed - setting SELF_USE for safety"
        )
        try:
            self._set_mode_safely(
                BatteryMode.SELF_USE,
                "Error fallback - hardware cycle failed"
            )
        except Exception:
            self.logger.exception("Failed to set safety fallback mode")
```

### Async Error Handling: Ohme API

**Sync Wrapper**: Daemon is synchronous, Ohme client is async

```python
def _check_ohme_status(self) -> dict[str, Any] | None:
    """Synchronous wrapper for async Ohme API call."""
    try:
        return asyncio.run(self._check_ohme_status_async())
    except Exception:
        self.logger.exception("Failed to check Ohme status")
        return None

async def _check_ohme_status_async(self) -> dict[str, Any]:
    """Fetch Ohme charger status asynchronously."""
    client = OhmeEVClient(config_path=self.system_config_path)
    await client.connect()
    try:
        status = await client.get_charger_status(use_cache=False)
        return status
    finally:
        await client.close()
```

**Error Path**: Exception in async → caught in wrapper → returns None → daemon continues

### Configuration Reload Errors

**Philosophy**: Invalid config should never crash daemon

```python
def _reload_config(self) -> None:
    """Reload daemon configuration (fast poll operation)."""
    try:
        # Load JSON
        with self.config_path.open("r", encoding="utf-8") as f:
            new_config = json.load(f)

        # Validate new configuration
        is_valid, errors = validate_daemon_config(new_config)
        if not is_valid:
            self.logger.error("New configuration is invalid - keeping old config:")
            for error in errors:
                self.logger.error("  %s", error)
            return  # Keep old config

        # Apply if valid and changed
        if new_config != self.daemon_config:
            self.daemon_config = new_config
            self.logger.info("Configuration reloaded")

    except json.JSONDecodeError:
        self.logger.exception("Invalid JSON during config reload - keeping old config")
    except Exception:
        self.logger.exception("Failed to reload config - keeping old config")
```

### Graceful Shutdown

**Signal Handling**: SIGTERM and SIGINT trigger clean shutdown

```python
def _handle_shutdown(self, signum: int, frame: Any) -> None:
    """Handle shutdown signals gracefully."""
    self.logger.info(
        "Received shutdown signal (%d), shutting down gracefully...", signum
    )
    self.shutdown_requested = True
```

**Shutdown Flow**:
1. Signal handler sets `shutdown_requested = True`
2. Main loop checks flag each iteration
3. Loop exits cleanly
4. Final log message: "👋 Daemon shutdown complete"

---

## Data Structures

### Daemon Configuration Schema

**File**: JSON format with strict schema validation

**Structure**:
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
        "description": "Cheap rate overnight charging"
      }
    ]
  },
  "logging": {
    "level": "INFO",
    "file_path": "logs/battery_mode_daemon.log"
  }
}
```

### Mode Change Log Structure

**File**: `data/battery_mode_daemon_log.json`

**Purpose**: Track daemon's mode changes for safety interval enforcement

**Structure**:
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

**History Limit**: Keeps last 100 entries (prevents unbounded growth)

**Persistence**: Written after each successful mode change

### BatteryMode Enum

**Source**: `src/core_logic/battery_simulation/constants_and_models.py`

**Enum Values**:
```python
class BatteryMode(Enum):
    # Modes daemon actively writes
    SELF_USE = "SELF_USE"
    FORCE_CHARGE = "FORCE_CHARGE"
    FORCE_DISCHARGE = "FORCE_DISCHARGE"
    MANUAL_STOP = "MANUAL_STOP"

    # Read-only modes (never written by daemon)
    FEED_IN_PRIORITY = "FEED_IN_PRIORITY"
    BACKUP = "BACKUP"
    # ... others
```

**Design**: Type-safe enum prevents string typos

### Ohme Status Dictionary

**Source**: `OhmeEVClient.get_charger_status()`

**Key Fields Used by Daemon**:
```python
{
    "power_watts": 7200,      # Used for threshold check
    "status": "charging",     # Informational
    "battery_percent": 45,    # Not used by daemon
    "plugged_in": true,       # Not used by daemon
    # ... many other fields
}
```

**None Handling**: If API fails, returns `None` → daemon treats as not charging

---

## Code Organization

### File Structure

```
scripts/battery_mode_daemon.py          # Main daemon (620 lines)
├── DAEMON_CONFIG_SCHEMA               # JSON schema constant
├── validate_daemon_config()           # Schema validation function
└── class BatteryModeDaemon:
    ├── __init__()                     # Initialize instance
    ├── _setup_logging()               # Configure rotating logs
    ├── _load_config()                 # Initial config load
    ├── _reload_config()               # Hot reload config
    ├── _load_mode_change_log()        # Restore state from disk
    ├── _save_mode_change_log()        # Persist state to disk
    ├── _is_ohme_charging()            # Threshold check
    ├── _get_scheduled_mode()          # Time range evaluation
    ├── _can_change_mode()             # Safety interval check
    ├── _determine_target_mode()       # Priority logic
    ├── _set_mode_safely()             # Mode change with safety
    ├── _check_ohme_status()           # Sync wrapper
    ├── _check_ohme_status_async()     # Async API call
    ├── _perform_hardware_cycle()      # Main cycle logic
    ├── _handle_shutdown()             # Signal handler
    └── run()                          # Main loop
```

### Function Responsibilities

| Function | Responsibility | Calls | Called By |
|----------|---------------|-------|-----------|
| `__init__()` | Instance setup | `_setup_logging()` | `main()` |
| `_setup_logging()` | Configure logs | - | `__init__()` |
| `_load_config()` | Initial load | `validate_daemon_config()` | `run()` |
| `_reload_config()` | Hot reload | `validate_daemon_config()` | `run()` (fast loop) |
| `_perform_hardware_cycle()` | Execute cycle | `_check_ohme_status()`, `_determine_target_mode()`, `_set_mode_safely()` | `run()` (slow loop) |
| `_determine_target_mode()` | Priority logic | `_is_ohme_charging()`, `_get_scheduled_mode()` | `_perform_hardware_cycle()` |
| `_set_mode_safely()` | Mode change | `_can_change_mode()`, `solax_modbus_work_mode()`, `solax_modbus_set_work_mode()`, `_save_mode_change_log()` | `_perform_hardware_cycle()` |

### Separation of Concerns

| Concern | Implementation |
|---------|---------------|
| **Configuration** | `_load_config()`, `_reload_config()`, schema validation |
| **State Persistence** | `_load_mode_change_log()`, `_save_mode_change_log()` |
| **External APIs** | `_check_ohme_status()`, `_check_ohme_status_async()` |
| **Decision Making** | `_determine_target_mode()`, `_is_ohme_charging()`, `_get_scheduled_mode()` |
| **Safety** | `_can_change_mode()`, `_set_mode_safely()` |
| **Hardware Control** | `solax_modbus_work_mode()`, `solax_modbus_set_work_mode()` (imported) |
| **Orchestration** | `run()`, `_perform_hardware_cycle()` |
| **Lifecycle** | `__init__()`, `_handle_shutdown()` |

---

## Design Decisions

### Why Two Loops Instead of Event-Driven?

**Considered**: Event-driven architecture (file watcher, API webhooks)

**Chosen**: Polling loops

**Rationale**:
- **Simplicity**: No complex event subscription management
- **Reliability**: Polling is resilient to missed events
- **File Watching**: inotify/fsevents not available on all platforms
- **API Limitations**: Ohme doesn't provide webhooks
- **Predictable Load**: Fixed polling rate easier to monitor

### Why JSON Config Instead of YAML?

**Existing System**: Uses YAML for main config (`config.yaml`)

**Daemon Config**: Uses JSON

**Rationale**:
- **Schema Validation**: jsonschema library is mature and robust
- **Hot Reload**: JSON parsing is lightweight
- **Human Editing**: JSON is sufficient for daemon settings
- **Separation**: Different format emphasizes separation of concerns

### Why Store Mode Change Log?

**Alternative**: Keep state only in memory

**Chosen**: Persist to JSON file

**Rationale**:
- **Restart Resilience**: Daemon restart doesn't reset safety interval
- **Observability**: External tools can inspect daemon state
- **Debugging**: History aids troubleshooting
- **Simplicity**: File-based storage, no database needed

**Trade-off**: File I/O overhead acceptable (only writes on mode change)

### Why Async for Ohme but Sync Main Loop?

**Ohme Client**: Async/await (inherited from library)

**Daemon Loop**: Synchronous

**Bridge**: `asyncio.run()` wrapper

**Rationale**:
- **Library Constraint**: OhmeEVClient is async-only
- **Simplicity**: Sync loop is easier to reason about
- **No Concurrency**: Daemon doesn't benefit from async (sequential operations)
- **Bridge Pattern**: `asyncio.run()` cleanly isolates async calls

**Performance**: `asyncio.run()` overhead (~1ms) negligible for 5-minute cycles

### Why Not Use Threading?

**Considered**: Separate threads for fast/slow loops

**Chosen**: Single-threaded with time-based triggers

**Rationale**:
- **Complexity**: Threading adds race conditions, locks
- **No I/O Blocking**: Operations complete quickly, no benefit from concurrency
- **Reliability**: Single-threaded eliminates entire class of bugs
- **Debugging**: Simpler to trace execution

### Why First Startup Delay?

**Alternative**: Act immediately on startup

**Chosen**: Wait one hardware cycle

**Rationale**:
- **Observability**: See current state before acting
- **Connection Stability**: Give network time to establish
- **Validation**: Verify all systems reachable
- **Safety**: Prevents startup transients

**Edge Case**: If daemon restarts during critical period, one cycle delay is acceptable

### Why Not Database for Change Log?

**Considered**: SQLite for change history

**Chosen**: JSON file

**Rationale**:
- **Simplicity**: No schema migrations, no locking
- **Sufficient**: Only stores last 100 entries
- **Portability**: JSON works everywhere
- **Observability**: Human-readable format

**When to Reconsider**: If analytics on history becomes important

---

## Implementation Details

### Logging System

**Handler**: `TimedRotatingFileHandler`

**Rotation**: Midnight (00:00 local time)

**Retention**: 7 days (backups: `battery_mode_daemon.log.2024-01-18`)

**Format**: `%(asctime)s - %(name)s - %(levelname)s - %(message)s`

**Dual Output**: File + console (for systemd/launchd)

**Example Log**:
```
2024-01-18 12:00:00,123 - battery_mode_daemon - INFO - 🚀 Battery Mode Daemon starting...
2024-01-18 12:00:00,456 - battery_mode_daemon - INFO - Daemon configuration loaded and validated
2024-01-18 12:00:00,789 - battery_mode_daemon - INFO - ⏳ First startup - waiting one cycle
2024-01-18 12:05:00,123 - battery_mode_daemon - INFO - ✅ Mode changed: SELF_USE → FORCE_CHARGE (reason: Ohme charging detected (7200W))
```

### Emoji Usage

**Purpose**: High-visibility events in logs

| Emoji | Event | Search Pattern |
|-------|-------|----------------|
| 🚀 | Daemon startup | `grep "🚀" logs/*.log` |
| ⏳ | First startup delay | `grep "⏳" logs/*.log` |
| ✅ | Successful mode change | `grep "✅" logs/*.log` |
| ❌ | Failed mode change | `grep "❌" logs/*.log` |
| 🔥 | Safety violation | `grep "🔥" logs/*.log` |
| 👋 | Graceful shutdown | `grep "👋" logs/*.log` |

### Schema Validation

**Library**: `jsonschema`

**Validation Points**:
1. Initial load (`_load_config()`)
2. Hot reload (`_reload_config()`)

**Schema Coverage**:
- Required fields
- Type checking (number, string, boolean, array)
- Range validation (min/max values)
- Pattern matching (time format: HH:MM)
- Enum validation (battery modes)

**Example Validation**:
```python
{
    "start_time": {
        "type": "string",
        "pattern": "^([0-1][0-9]|2[0-3]):[0-5][0-9]$"
    },
    "battery_mode": {
        "type": "string",
        "enum": ["SELF_USE", "FORCE_CHARGE", "FORCE_DISCHARGE", "MANUAL_STOP"]
    }
}
```

### Time Range Parsing

**Input Format**: "HH:MM" (24-hour)

**Parsing**:
```python
start = datetime.strptime(range_config["start_time"], "%H:%M").time()
end = datetime.strptime(range_config["end_time"], "%H:%M").time()
```

**Comparison**: `datetime.time` objects support `<=`, `>=`

**Midnight Crossing**: Detected by `start > end`

### Mode Change Flow

```
1. Read current mode (Modbus)
   └─▶ If error: Skip cycle, return

2. Compare with target mode
   └─▶ If same: Skip, log debug

3. Check safety interval
   └─▶ If too soon: Block, log 🔥, return

4. Call solax_modbus_set_work_mode()
   └─▶ Validates mode combination
   └─▶ Checks master-only restriction
   └─▶ Performs Modbus write

5. Check result
   ├─▶ Success: Log ✅, save to file, update timestamp
   └─▶ Failure: Log ❌, don't update timestamp
```

### State Management

**In-Memory State**:
- `daemon_config`: Current configuration
- `system_config`: System configuration (from config.yaml)
- `last_mode_change_time`: Unix timestamp of last change
- `startup_complete`: Boolean flag

**Persisted State**:
- `data/battery_mode_daemon_log.json`: Mode change history

**State Restoration**: `_load_mode_change_log()` restores `last_mode_change_time` on startup

---

## Testing Strategy

### Unit Testing Approach

**Challenges**:
- Hardware dependencies (Modbus, Ohme API)
- Async operations
- File I/O
- Time-based logic

**Recommended Mocking**:
```python
# Mock Modbus operations
@patch('scripts.battery_mode_daemon.solax_modbus_work_mode')
@patch('scripts.battery_mode_daemon.solax_modbus_set_work_mode')
def test_set_mode_safely(mock_set, mock_get):
    mock_get.return_value = BatteryMode.SELF_USE
    mock_set.return_value = {"success": True}
    # ... test logic
```

### Integration Testing

**Dry-Run Mode**: Could add flag to skip actual Modbus writes

**Test Configuration**:
```json
{
  "daemon_settings": {
    "hardware_poll_interval_seconds": 60,
    "min_mode_change_interval_seconds": 120,
    "ohme_charging_threshold_watts": 100
  }
}
```

**Validation Tests**:
1. Invalid JSON → Keeps old config
2. Missing required field → Rejects config
3. Invalid time format → Rejects config
4. Out-of-range values → Rejects config

### Manual Testing Checklist

- [ ] Daemon starts successfully
- [ ] Configuration hot reload works
- [ ] Invalid config rejected (daemon continues)
- [ ] Ohme charging triggers FORCE_CHARGE
- [ ] Time schedule activates correctly
- [ ] Midnight crossing handled correctly
- [ ] Safety interval blocks rapid changes (🔥 logged)
- [ ] First startup delay observed
- [ ] Graceful shutdown on SIGTERM
- [ ] Logs rotate at midnight
- [ ] Mode change log persists correctly

### Observability Testing

**Log Verification**:
```bash
# Check startup
grep "🚀" logs/battery_mode_daemon.log

# Check mode changes
grep "✅" logs/battery_mode_daemon.log

# Check safety violations
grep "🔥" logs/battery_mode_daemon.log

# Check errors
grep "ERROR" logs/battery_mode_daemon.log
```

**State Inspection**:
```bash
# View current state
cat data/battery_mode_daemon_log.json | jq .

# View change history
cat data/battery_mode_daemon_log.json | jq '.change_history[]'
```

---

## Future Enhancements

### Potential Improvements

1. **Metrics Endpoint**: HTTP endpoint for Prometheus scraping
2. **Health Checks**: Liveness/readiness probes for Kubernetes
3. **Dry-Run Mode**: Test configuration without hardware changes
4. **Remote Logging**: Syslog or cloud logging integration
5. **Configuration API**: REST API for runtime config changes
6. **Mode Prediction**: Log predicted mode for next cycle
7. **Historical Analytics**: Analyze mode change patterns
8. **Battery SOC Integration**: Consider current SOC in decisions
9. **Dynamic Intervals**: Adjust polling based on activity
10. **Multi-Inverter**: Support multiple battery systems

### Breaking Changes to Consider

**If Reimplementing**:
- Use database for change log (PostgreSQL/SQLite)
- Full async architecture (asyncio event loop)
- gRPC for internal communication
- Protocol Buffers for configuration
- Structured logging (JSON logs)

**Why Not Now**: Current implementation prioritizes simplicity and reliability

---

## Conclusion

The Battery Mode Daemon is designed as a robust, autonomous service that bridges EV charging, battery storage, and time-based automation. Its two-tier polling architecture balances responsiveness with hardware efficiency, while multiple layers of safety mechanisms protect against both software bugs and hardware damage.

The implementation prioritizes:
1. **Reliability** over features
2. **Observability** over performance
3. **Safety** over convenience
4. **Simplicity** over optimization

This design serves as a foundation for future enhancements while maintaining production stability.
