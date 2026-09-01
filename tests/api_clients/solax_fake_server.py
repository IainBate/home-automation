"""Shared fixtures for src/api_clients/ tests - notably fake_solax_server, a
real local Modbus TCP slave (pymodbus's own server, not a mock) that tests
can point solax_modbus_client.py functions at instead of a real inverter.

Register-value indexing note: with ModbusSequentialDataBlock(1, values), the
value actually served at protocol register address N is values[N] (not
values[N - 1] as pymodbus's own "address" parameter naming might suggest) -
verified empirically against the installed pymodbus version, since its
public docs/type hints don't spell this out.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator

import pytest
from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
from pymodbus.server import ModbusTcpServer

HOLDING_REGISTER_COUNT = 300
INPUT_REGISTER_COUNT = 100


class FakeSolaxServer:
    """A real Modbus TCP slave, seeded with SolaX-shaped registers, on 127.0.0.1.

    Runs pymodbus's own async server in a background thread with its own
    event loop, so synchronous test code (and the synchronous production
    client code under test) can talk to it over a real socket without the
    test itself needing to be async.
    """

    def __init__(
        self,
        *,
        initial_holding: dict[int, int] | None = None,
        initial_input: dict[int, int] | None = None,
    ) -> None:
        hr_values = [0] * HOLDING_REGISTER_COUNT
        for addr, value in (initial_holding or {}).items():
            hr_values[addr] = value
        ir_values = [0] * INPUT_REGISTER_COUNT
        for addr, value in (initial_input or {}).items():
            ir_values[addr] = value

        hr_block = ModbusSequentialDataBlock(1, hr_values)
        ir_block = ModbusSequentialDataBlock(1, ir_values)
        device = ModbusDeviceContext(hr=hr_block, ir=ir_block)
        self._context = ModbusServerContext(devices=device, single=True)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: ModbusTcpServer | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_flag = False
        self.port: int | None = None

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _main() -> None:
            self._server = ModbusTcpServer(self._context, address=("127.0.0.1", 0))
            await self._server.serve_forever(background=True)
            self.port = self._server.transport.sockets[0].getsockname()[1]
            self._ready.set()
            while not self._stop_flag:
                await asyncio.sleep(0.02)
            await self._server.shutdown()

        self._loop.run_until_complete(_main())

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise TimeoutError("Fake SolaX server did not start within 5 seconds")
        time.sleep(0.05)  # let the listening socket settle before the first client connect

    def stop(self) -> None:
        self._stop_flag = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    def read_holding_registers(self, address: int, count: int = 1) -> list[int]:
        """Read back register values directly - for asserting what a write actually did."""
        from pymodbus.client import ModbusTcpClient

        client = ModbusTcpClient(host="127.0.0.1", port=self.port, timeout=5)
        try:
            client.connect()
            try:
                result = client.read_holding_registers(address=address, count=count, device_id=1)
            except TypeError:
                result = client.read_holding_registers(address=address, count=count, slave=1)
            return result.registers
        finally:
            client.close()

    def write_holding_register(self, address: int, value: int) -> None:
        """Directly set a register - e.g. to simulate real hardware reflecting a command
        register write (0x001F/0x0020) back into its separate status registers
        (0x008B/0x008C), which this dumb fake server has no internal logic to do itself.
        """
        from pymodbus.client import ModbusTcpClient

        client = ModbusTcpClient(host="127.0.0.1", port=self.port, timeout=5)
        try:
            client.connect()
            try:
                client.write_register(address=address, value=value, device_id=1)
            except TypeError:
                client.write_register(address=address, value=value, slave=1)
        finally:
            client.close()


@pytest.fixture
def fake_solax_server_factory() -> Iterator[callable]:
    """Factory for running fake SolaX inverters, each seeded with its own registers.

    Registers are fixed at construction time (see module docstring on why -
    no reliable live-mutation API was found for the installed pymodbus
    version's input-register store), so tests that need specific initial
    values call this factory with initial_holding/initial_input rather than
    mutating a shared server after the fact. All servers created via a given
    test's factory call are stopped automatically at teardown.
    """
    servers: list[FakeSolaxServer] = []

    def _make(
        *, initial_holding: dict[int, int] | None = None, initial_input: dict[int, int] | None = None
    ) -> FakeSolaxServer:
        server = FakeSolaxServer(initial_holding=initial_holding, initial_input=initial_input)
        server.start()
        servers.append(server)
        return server

    try:
        yield _make
    finally:
        for server in servers:
            server.stop()


@pytest.fixture
def fake_solax_server(fake_solax_server_factory: callable) -> FakeSolaxServer:
    """A running fake SolaX inverter with empty (all-zero) registers."""
    return fake_solax_server_factory()


def make_solax_config(server: FakeSolaxServer, *, min_command_interval: float = 0.01) -> dict:
    """Build a config dict pointing solaX_cloud_api at a fake server (master == slave)."""
    return {
        "solaX_cloud_api": {
            "modbus_enabled": True,
            "master_ip": "127.0.0.1",
            "slave_ip": "127.0.0.1",
            "modbus_port": server.port,
            "modbus_connection_timeout": 5,
            "master_modbus_address": 1,
            "slave_modbus_address": 1,
            "min_command_interval": min_command_interval,
        }
    }
