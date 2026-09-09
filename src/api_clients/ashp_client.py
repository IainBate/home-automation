"""ASHP (Air Source Heat Pump) Control - public API, backend-swappable.

Facade over whichever control path actually drives the ASHP, mirroring
`solax_modbus_client.py`'s own role as a public API over internal
`_modbus_*` implementation modules (see this project's CLAUDE.md). Callers
(`ashp_decision_logic.py`/`hvac_automation_core.py`) only ever import from
here, never from a `_ashp_*_backend` module directly - see `ashp.yaml`'s
`control_backend` config key below for why this indirection exists.

Background (ASHP.md Open Question 1): two candidate control paths were
identified - MELCloud zone control on the Ecodan, and writing to the T6R
thermostat over local HomeKit. Both were probed against the real household
hardware 2026-09-09 (`scripts/ashp_control_probe.py`):

- **T6R (confirmed working):** writing HEATING_COOLING_TARGET=heat together
  with a TEMPERATURE_TARGET above the current room temperature reliably
  made the thermostat's own `calling_for_heat` signal go true and hold for
  a full 90s test window. This is the backend actually implemented today
  (`_ashp_t6r_backend.py`).
- **MELCloud (not confirmed, looks unpromising):** the Ecodan's Zone 1
  reports `operation_mode: "heat-flow"`, not `"heat-thermostat"` - it's
  following a water flow-temperature setpoint, not a room-temperature
  target, so a plain `target_temperature` write there is likely inert.
  Not implemented.

**Why the backend seam exists at all**, given only one backend is built:
this project's owner explicitly asked for the architecture to isolate the
control path, specifically so that *if* the T6R route turns out to have a
problem once physically deployed (a permission revoked, a firmware update,
a case the probe didn't cover), moving to MELCloud means writing a new
`_ashp_melcloud_backend.py` with these same two function signatures and
flipping `ashp.control_backend` in config.yaml - `ashp_decision_logic.py`,
`hvac_automation_core.py`, and their tests would not need to change at all.
"""

from __future__ import annotations

import logging
from typing import Any

from . import _ashp_t6r_backend

logger = logging.getLogger(__name__)

DEFAULT_CONTROL_BACKEND = "t6r"

_BACKENDS = {
    "t6r": _ashp_t6r_backend,
    # "melcloud": _ashp_melcloud_backend,  # not yet implemented - see module docstring
}


def _backend(config: dict[str, Any]):  # noqa: ANN202 - returns a backend module
    name = config.get("ashp", {}).get("control_backend", DEFAULT_CONTROL_BACKEND)
    backend = _BACKENDS.get(name)
    if backend is None:
        msg = (
            f"ashp.control_backend {name!r} is not implemented - available: "
            f"{sorted(_BACKENDS)}"
        )
        raise ValueError(msg)
    return backend


def set_ashp_heat_call(config: dict[str, Any], target_temp_c: float) -> bool:
    """Set the ASHP calling for heat at target_temp_c, via the configured backend.

    Returns:
        True if the write verified as applied, False on any failure. Never
        raises for a hardware/connection failure (matches this codebase's
        fail-fast-to-False convention) - only an unknown/misconfigured
        `ashp.control_backend` name raises, since that's a config error the
        caller should not silently swallow.

    """
    return _backend(config).set_ashp_heat_call(config, target_temp_c)


def set_ashp_off(config: dict[str, Any]) -> bool:
    """Turn the ASHP off, via the configured backend.

    Returns:
        True if the write verified as applied, False on any failure. See
        set_ashp_heat_call's docstring for the same raise/return contract.

    """
    return _backend(config).set_ashp_off(config)
