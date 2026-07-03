"""Low-level Modbus TCP Protocol Module.

Handles pymodbus library integration and raw Modbus READ-ONLY communication operations.
Provides pymodbus version compatibility and hardware debug instrumentation.

SECURITY: This module intentionally does NOT provide write operations.
All Modbus write operations must go through _modbus_mode_controller.py which
enforces VALID_WORK_MODE_COMBINATIONS and prevents unauthorized hardware control.

INTERNAL MODULE: Only for use by solax_modbus_client.py and related modules.
"""

# pylint: disable=cyclic-import
# Justification: Intentional modbus client split architecture. This helper module
# is imported by solax_modbus_client.py and uses type hints from it. The cycle
# is resolved at runtime and doesn't cause actual circular dependency issues.
from __future__ import annotations

import logging
from pathlib import Path

# Debug instrumentation for detecting unmarked hardware tests
try:
    import sys

    # Add dev_scripts to path for debug module import
    project_root = str(Path(__file__).parent.parent.parent)  # pylint: disable=invalid-name  # Local variable, not constant
    dev_scripts_path = str(  # pylint: disable=invalid-name  # Local variable
        Path(project_root) / "dev_scripts"
    )  # pragma: no cover
    if dev_scripts_path not in sys.path:  # pragma: no cover
        sys.path.insert(0, dev_scripts_path)  # pragma: no cover

    from hardware_debug_access import (  # pragma: no cover
        log_hardware_access,
        should_log_hardware_access,
    )

    DEBUG_ENABLED = True  # pragma: no cover
except ImportError:  # pragma: no cover
    DEBUG_ENABLED = False  # pragma: no cover

    def log_hardware_access(  # pylint: disable=missing-function-docstring  # Stub function
        *_args: object, **_kwargs: object
    ) -> None:  # pragma: no cover
        pass  # pragma: no cover

    def should_log_hardware_access(*_args: object, **_kwargs: object) -> bool:  # noqa: ARG001  # pragma: no cover  # pylint: disable=missing-function-docstring  # Stub
        return False  # pragma: no cover


# Setup basic logging - use main module logger when available for test compatibility
def _get_logger() -> logging.Logger:
    """Get logger from main module if available, otherwise use local logger."""
    try:
        from . import (  # noqa: PLC0415  # pylint: disable=import-outside-toplevel
            solax_modbus_client,  # pylint: disable=import-outside-toplevel
        )

    except ImportError:  # pragma: no cover - Defensive: modbus package structure prevents this
        # Fallback to local logger if main module not available
        return logging.getLogger(__name__)
    # Try to access logger attribute, fallback if AttributeError
    try:
        return solax_modbus_client.logger
    except AttributeError:
        # Module exists but no logger attribute - use local logger
        return logging.getLogger(__name__)


# Don't create static logger - get it dynamically for test compatibility


def _connect_modbus_client(ip: str, port: int, timeout: int) -> object | None:
    """Create and connect Modbus TCP client with version compatibility.

    Args:
        ip: IP address of the device
        port: TCP port for connection
        timeout: Connection timeout in seconds

    Returns:
        Connected ModbusTcpClient or None if failed

    """
    try:
        # Check pymodbus dependency
        try:
            # Try modern pymodbus import first (v3.0+)
            from pymodbus.client import (  # noqa: PLC0415 - conditional import for version compatibility  # pylint: disable=import-outside-toplevel
                ModbusTcpClient,
            )

        except ImportError:
            # Fallback to older pymodbus structure (v2.x)
            from pymodbus.client.sync import (  # noqa: PLC0415 - conditional import for version compatibility  # pylint: disable=import-outside-toplevel
                ModbusTcpClient,
            )

        # Create and connect client
        client = ModbusTcpClient(host=ip, port=port, timeout=timeout)

        # DEBUG: Log actual hardware TCP connection attempt (skip mocked/test contexts)
        if should_log_hardware_access(ip=ip):  # pragma: no cover
            log_hardware_access(f"TCP_CONNECT:{ip}:{port}")  # pragma: no cover

        connection_result = client.connect()
        if not connection_result:
            _get_logger().error("Failed to connect to %s:%s", ip, port)
            return None
    except ImportError as e:
        _get_logger().error("pymodbus library not available: %s", e)
        return None
    except (ConnectionError, OSError, TimeoutError) as e:
        # Network/connection failures during TCP connection
        _get_logger().error("Error connecting to %s:%s: %s", ip, port, e)
        return None
    _get_logger().debug("Successfully connected to %s:%s", ip, port)
    return client


def _read_holding_registers(
    client: object, start_addr: int, count: int, slave_addr: int
) -> list[int] | None:
    """Read holding registers with pymodbus version compatibility.

    Args:
        client: Connected ModbusTcpClient
        start_addr: Starting register address
        count: Number of registers to read
        slave_addr: Modbus slave address

    Returns:
        List of register values or None if error

    """
    try:
        # Handle pymodbus version compatibility for parameter naming
        try:
            # DEBUG: Log actual hardware read operation (skip mocked/test contexts)
            if should_log_hardware_access(client=client):  # pragma: no cover
                log_hardware_access(
                    f"READ_HOLDING:0x{start_addr:04X}:count={count}:device_id={slave_addr}"
                )  # pragma: no cover

            # Latest pymodbus (v3.11+) uses 'device_id' parameter and keyword-only count
            result = client.read_holding_registers(
                address=start_addr, count=count, device_id=slave_addr
            )
        except TypeError:
            try:
                # DEBUG: Log actual hardware read operation (skip mocked/test contexts)
                if should_log_hardware_access(client=client):  # pragma: no cover
                    log_hardware_access(
                        f"READ_HOLDING_SLAVE:0x{start_addr:04X}:count={count}:slave={slave_addr}"
                    )  # pragma: no cover

                # Earlier pymodbus (v3.x) uses 'slave' parameter
                result = client.read_holding_registers(
                    address=start_addr, count=count, slave=slave_addr
                )
            except TypeError:
                # DEBUG: Log actual hardware read operation (legacy, skip mocked/test contexts)
                if should_log_hardware_access(client=client):  # pragma: no cover
                    log_hardware_access(
                        f"READ_HOLDING_LEGACY:0x{start_addr:04X}:count={count}:unit={slave_addr}"
                    )  # pragma: no cover

                # Oldest pymodbus (v2.x) uses 'unit' parameter
                result = client.read_holding_registers(
                    address=start_addr, count=count, unit=slave_addr
                )

        if result.isError():
            _get_logger().error("Modbus error reading registers: %s", result)
            return None

        registers = result.registers
        _get_logger().debug("Successfully read %s registers: %s", len(registers), registers)
    except AttributeError as e:
        # result.isError() or result.registers failed
        _get_logger().error("Invalid Modbus response structure: %s", e)
        return None
    except (ConnectionError, OSError, TimeoutError) as e:
        # Network/connection failures during read operation
        _get_logger().error("Error reading holding registers: %s", e)
        return None
    return registers


def _read_input_registers(
    client: object, start_addr: int, count: int, slave_addr: int
) -> list[int] | None:
    """Read input registers with pymodbus version compatibility.

    Args:
        client: Connected ModbusTcpClient
        start_addr: Starting register address
        count: Number of registers to read
        slave_addr: Modbus slave address

    Returns:
        List of register values or None if error

    """
    try:
        # Handle pymodbus version compatibility
        try:
            # DEBUG: Log actual hardware read operation (skip mocked/test contexts)
            if should_log_hardware_access(client=client):  # pragma: no cover
                log_hardware_access(
                    f"READ_INPUT:0x{start_addr:04X}:count={count}:device_id={slave_addr}"
                )  # pragma: no cover

            # Latest pymodbus (v3.11+) uses 'device_id' parameter and keyword-only count
            result = client.read_input_registers(
                address=start_addr, count=count, device_id=slave_addr
            )
        except TypeError:
            try:
                # DEBUG: Log actual hardware read operation (skip mocked/test contexts)
                if should_log_hardware_access(client=client):  # pragma: no cover
                    log_hardware_access(
                        f"READ_INPUT_SLAVE:0x{start_addr:04X}:count={count}:slave={slave_addr}"
                    )  # pragma: no cover

                # Earlier pymodbus (v3.x) uses 'slave' parameter
                result = client.read_input_registers(
                    address=start_addr, count=count, slave=slave_addr
                )
            except TypeError:
                # DEBUG: Log actual hardware read operation (legacy, skip mocked/test contexts)
                if should_log_hardware_access(client=client):  # pragma: no cover
                    log_hardware_access(
                        f"READ_INPUT_LEGACY:0x{start_addr:04X}:count={count}:unit={slave_addr}"
                    )  # pragma: no cover

                # Oldest pymodbus (v2.x) uses 'unit' parameter
                result = client.read_input_registers(
                    address=start_addr, count=count, unit=slave_addr
                )

        if result.isError():
            _get_logger().error("Modbus error reading input registers: %s", result)
            return None

        registers = result.registers
        _get_logger().debug("Successfully read %s input registers: %s", len(registers), registers)
    except AttributeError as e:
        # result.isError() or result.registers failed
        _get_logger().error("Invalid Modbus response structure: %s", e)
        return None
    except (ConnectionError, OSError, TimeoutError) as e:
        # Network/connection failures during read operation
        _get_logger().error("Error reading input registers: %s", e)
        return None
    return registers


# NOTE: _write_single_register function intentionally removed for security reasons.
# All Modbus write operations must go through the safety-validated function in
# _modbus_mode_controller.py which enforces VALID_WORK_MODE_COMBINATIONS.
# This prevents accidental bypass of critical safety validations.
