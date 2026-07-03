# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Structure

```
home_automation_software/
├── original_ home_automation/          # Main project: SolaX + Ohme integration
│   ├── config.yaml                     # System configuration (edit for your hardware)
│   ├── battery_mode_daemon_config.json # Daemon-specific JSON config
│   ├── requirements.txt                # Python dependencies
│   ├── scripts/                        # CLI entry points
│   │   ├── solax_modbus_status_report.py           # Read inverter status via Modbus TCP
│   │   ├── solax_modbus_read_and_set_workmode.py   # Read/set inverter work mode
│   │   ├── ohme_ev_control.py                      # Control Ohme Home Pro EV charger
│   │   └── battery_mode_daemon.py                  # Autonomous battery mode manager
│   ├── src/                            # Python package
│   │   ├── api_clients/              # Hardware API clients
│   │   │   ├── solax_modbus_client.py          # SolaX Modbus TCP facade (public API)
│   │   │   ├── ohme_ev_client.py               # Ohme Home Pro EV charger client (async)
│   │   │   ├── _modbus_reader.py               # Internal: Modbus register read helpers
│   │   │   ├── _modbus_mode_controller.py      # Internal: Mode change safety/hardware
│   │   │   ├── _modbus_data_maps.py            # Internal: Register value parsing
│   │   │   ├── _modbus_protocol.py             # Internal: Modbus TCP protocol layer
│   │   │   └── _modbus_validator.py            # Internal: Power data validation
│   │   ├── config_manager/
│   │   │   └── config_manager.py               # YAML loading + JSON schema validation
│   │   ├── core_logic/
│   │   │   ├── battery_simulation/
│   │   │   │   └── constants_and_models.py     # BatteryMode enum, simulation dataclasses
│   │   │   └── ohme_charging_logic.py          # Charging decision logic (pure functions)
│   │   └── utils/
│   │       ├── exceptions.py                   # Domain-specific exception classes
│   │       └── paths.py                        # Centralized path resolution utilities
│   ├── config/                           # Runtime data (mode change log, etc.)
│   ├── data/                             # Daemon state (mode change history)
│   ├── docs/                             # Design documentation
│   └── README.md                         # Installation, usage, troubleshooting
├── saic-python-client-ng-master/         # Separate library: MG iSmart vehicle API (SAIC)
└── updates to the home automation software/ # Patches and zip archives
```

## Key Architecture Concepts

### Two Subsystems

1. **SolaX Inverter Control** — Direct Modbus TCP communication with SolaX X3 Hybrid G4 inverters (master + optional slave). Read-only monitoring (PV, battery SoC, grid power, etc.) and restricted write access for work mode changes (Self-Use, Charge, Discharge, Hold). All writes go to the **master inverter only**.

2. **Ohme EV Charger Control** — Async API client for Ohme Home Pro chargers via the `ohme` library. Provides charger status, mode control, price cap management, and vehicle selection. Includes a monkey-patch to ensure production `api.ohme.io` endpoint is used.

### Data Flow

```
config.yaml (YAML)
    → config_manager.load_static_config() → dict (validated)
        → solax_modbus_client.* (synchronous Modbus TCP)
        → ohme_ev_client.* (async HTTP via ohme library)
        → battery_mode_daemon.py (orchestrates both, priority: Ohme > Schedule > Default)
```

### Critical Patterns

- **BatteryMode enum** (`src/core_logic/battery_simulation/constants_and_models.py`): The single internal representation of battery operating modes. Conversion to/from display strings happens in exactly two places: hardware interface (Modbus) and `battery_mode_to_display_string()`. No other string conversion should exist.
- **Fail-fast**: All hardware API functions return `None` on error rather than raising. Callers must check for `None`.
- **Safety intervals**: Mode changes enforce timing restrictions (configurable `min_command_interval`). Use `force_unsafe=True` to bypass (documented risk).
- **Circuit breaker**: All hardware interface functions wrap in broad `except Exception` to prevent crashes from propagating to daemon.
- **Config validation**: `config.yaml` is validated against a comprehensive JSON schema at load time. Invalid config causes silent failure (returns `None`).

### Daemon (battery_mode_daemon.py)

Autonomous service that manages battery mode based on:
1. **Priority 1**: Ohme EV charging detected (power > threshold) → FORCE_CHARGE
2. **Priority 2**: Time-based schedule → configured mode
3. **Priority 3**: Default mode (SELF_USE)

Uses two-tier polling: 30s config reload + configurable hardware poll interval (default 60s). Hot-reloads daemon config from JSON. Graceful shutdown on SIGTERM/SIGINT. Falls back to SELF_USE on any hardware cycle error.

## Running the Project

### Setup

```bash
cd "original_ home_automation"
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Dependencies: `pyyaml`, `pymodbus`, `ohme`, `aiohttp`, `pydantic`, `pytz`, `tabulate`, `jsonschema`

### CLI Scripts (run from project root)

```bash
# Read inverter status
python3 scripts/solax_modbus_status_report.py [--log-level DEBUG] [--compare-cloud]

# Read/set inverter work mode
python3 scripts/solax_modbus_read_and_set_workmode.py [--self-use | --charge | --discharge | --hold] [--force-mode-change]

# Control Ohme EV charger
python3 scripts/ohme_ev_control.py --status [--verbose]
python3 scripts/ohme_ev_control.py --max-charge | --smart-charge | --set-target N | --set-price-cap VALUE

# Run the daemon
python3 scripts/battery_mode_daemon.py battery_mode_daemon_config.json [--config config.yaml]
```

### Configuration

Edit `config.yaml` with your inverter IP addresses, Ohme credentials, and timezone. The `battery_mode_daemon_config.json` controls daemon behavior (poll intervals, schedule, charging thresholds).

## Codebase Conventions

- Python 3.11+ (`.python-version` specifies 3.13.7)
- `from __future__ import annotations` at top of every file
- Type hints on all function signatures
- Docstrings on all public functions/classes
- `# pylint: disable=...` comments justify complexity exceptions (C901, PLR0915, etc.)
- Internal modules prefixed with `_` (e.g., `_modbus_reader.py`)
- Guard clause pattern: early returns for independent validation checks (avoid deep nesting)
- `# Circuit Breaker:` comments mark broad exception handlers that protect daemon stability

## SAIC Python Client (separate library)

`saic-python-client-ng-master/` is an independent library for MG iSmart vehicles (SAIC). Not part of the home automation system. Uses Poetry for build, Ruff for linting, mypy for type checking. Run tests with `pytest` from that directory.
