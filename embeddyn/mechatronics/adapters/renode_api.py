"""Pure-Python client for Renode's External Control API.

The server lives in Renode (``src/Renode/Network/ExternalControl``) and is
started from the Monitor with::

    emulation CreateExternalControlServer "<name>" <port>

Wire format (all integers little endian):

* handshake: ``u16 count`` followed by ``count`` pairs of ``(u8 command, u8 version)``;
  the server answers with a single ``SuccessfulHandshake`` byte.
* request: ``"RE"`` magic, ``u8 command``, ``u32 size``, ``size`` payload bytes.
* response: ``u8 return_code`` then, depending on the code, ``u8 command``,
  ``i32 event_descriptor`` and ``u32 size`` + ``size`` data bytes.

Asynchronous GPIO events are only emitted while a ``RunFor`` command executes,
so they arrive *before* the ``RunFor`` response and are collected by
:meth:`RenodeExternalControl.run_for`.
"""
from __future__ import annotations

import enum
import socket
import struct
from dataclasses import dataclass
from typing import Callable, Iterable

MAGIC = b"RE"


class Command(enum.IntEnum):
    RUN_FOR = 1
    GET_TIME = 2
    GET_MACHINE = 3
    ADC = 4
    GPIO_PORT = 5
    SYSTEM_BUS = 6


class ReturnCode(enum.IntEnum):
    COMMAND_FAILED = 0
    FATAL_ERROR = 1
    INVALID_COMMAND = 2
    SUCCESS_WITH_DATA = 3
    SUCCESS_WITHOUT_DATA = 4
    SUCCESS_HANDSHAKE = 5
    ASYNC_EVENT = 6


# Versions implemented by the custom-cores Renode branch.
COMMAND_VERSIONS: dict[Command, int] = {
    Command.RUN_FOR: 0x0,
    Command.GET_TIME: 0x0,
    Command.GET_MACHINE: 0x0,
    Command.ADC: 0x0,
    Command.GPIO_PORT: 0x1,
    Command.SYSTEM_BUS: 0x0,
}

_NEW_INSTANCE = -1
_ADC_GET_COUNT, _ADC_GET_VALUE, _ADC_SET_VALUE = 0, 1, 2
_GPIO_GET_STATE, _GPIO_SET_STATE, _GPIO_REGISTER_EVENT = 0, 1, 2
_BUS_READ, _BUS_WRITE = 0, 1
ACCESS_WIDTHS = {"multibyte": 0, "byte": 1, "word": 2, "dword": 4, "doubleword": 4, "qword": 8, "quadword": 8}
_WIDTH_FORMATS = {1: "<B", 2: "<H", 4: "<I", 8: "<Q"}
_MULTI_BYTE = 128                     # AccessWidth.MultiByte: count is in bytes


class RenodeApiError(RuntimeError):
    """Raised when Renode reports a failed, invalid or fatal command."""

    def __init__(self, code: ReturnCode, command: int | None, message: str) -> None:
        self.code, self.command = code, command
        label = Command(command).name if command in Command._value2member_map_ else command
        super().__init__(f"Renode {code.name.lower()} ({label}): {message}")


@dataclass(frozen=True)
class GpioEvent:
    descriptor: int
    timestamp_us: int
    state: bool


@dataclass(frozen=True)
class Response:
    code: ReturnCode
    command: int | None
    data: bytes = b""
    descriptor: int | None = None


def _name_payload(name: str) -> bytes:
    raw = name.encode("utf-8")
    if not raw:
        raise ValueError("Renode element name must not be empty")
    return struct.pack("<i", len(raw)) + raw


class RenodeExternalControl:
    """Blocking client; one instance per TCP connection."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._machines: dict[str, int] = {}
        self._instances: dict[tuple[Command, int, str], int] = {}
        self._event_handlers: dict[int, Callable[[GpioEvent], None]] = {}
        self._next_descriptor = 0

    # ------------------------------------------------------------------ connection
    @classmethod
    def connect(cls, host: str, port: int, timeout: float = 10.0,
                commands: Iterable[Command] = tuple(COMMAND_VERSIONS)) -> "RenodeExternalControl":
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(None)  # RunFor may legitimately take long in wall-clock time
        client = cls(sock)
        try:
            client._handshake(list(commands))
        except BaseException:
            sock.close()
            raise
        return client

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _handshake(self, commands: list[Command]) -> None:
        payload = struct.pack("<H", len(commands))
        for command in commands:
            payload += struct.pack("<BB", int(command), COMMAND_VERSIONS[Command(command)])
        self.sock.sendall(payload)
        response = self._read_response()
        if response.code is not ReturnCode.SUCCESS_HANDSHAKE:
            raise RenodeApiError(response.code, response.command, response.data.decode("ascii", "replace"))

    # ------------------------------------------------------------------ framing
    def _recv_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self.sock.recv(size - len(chunks))
            if not chunk:
                raise ConnectionError("Renode closed the External Control connection")
            chunks.extend(chunk)
        return bytes(chunks)

    def _read_response(self) -> Response:
        code = ReturnCode(self._recv_exact(1)[0])
        if code is ReturnCode.SUCCESS_HANDSHAKE:
            return Response(code, None)
        if code is ReturnCode.FATAL_ERROR:
            (size,) = struct.unpack("<I", self._recv_exact(4))
            return Response(code, None, self._recv_exact(size))
        command = self._recv_exact(1)[0]
        if code is ReturnCode.ASYNC_EVENT:
            descriptor, size = struct.unpack("<iI", self._recv_exact(8))
            return Response(code, command, self._recv_exact(size), descriptor)
        if code in (ReturnCode.SUCCESS_WITH_DATA, ReturnCode.COMMAND_FAILED):
            (size,) = struct.unpack("<I", self._recv_exact(4))
            return Response(code, command, self._recv_exact(size))
        return Response(code, command)

    def _dispatch_event(self, response: Response) -> None:
        if response.command != Command.GPIO_PORT or len(response.data) < 9:
            return
        timestamp_us = struct.unpack_from("<Q", response.data, 0)[0]
        event = GpioEvent(int(response.descriptor or 0), timestamp_us, bool(response.data[8]))
        handler = self._event_handlers.get(event.descriptor)
        if handler is not None:
            handler(event)

    def execute(self, command: Command, payload: bytes = b"") -> bytes:
        self.sock.sendall(MAGIC + struct.pack("<BI", int(command), len(payload)) + payload)
        while True:
            response = self._read_response()
            if response.code is ReturnCode.ASYNC_EVENT:
                self._dispatch_event(response)
                continue
            if response.code in (ReturnCode.SUCCESS_WITH_DATA, ReturnCode.SUCCESS_WITHOUT_DATA):
                if response.command != command:
                    raise RenodeApiError(ReturnCode.FATAL_ERROR, response.command,
                                         f"response for unexpected command (sent {command.name})")
                return response.data
            raise RenodeApiError(response.code, response.command, response.data.decode("ascii", "replace"))

    # ------------------------------------------------------------------ time
    def run_for(self, microseconds: int) -> None:
        if microseconds < 0:
            raise ValueError("RunFor interval must be non-negative")
        self.execute(Command.RUN_FOR, struct.pack("<Q", int(microseconds)))

    def get_time_us(self) -> int:
        return struct.unpack("<Q", self.execute(Command.GET_TIME))[0]

    # ------------------------------------------------------------------ instances
    def machine(self, name: str) -> int:
        if name not in self._machines:
            self._machines[name] = struct.unpack("<i", self.execute(Command.GET_MACHINE, _name_payload(name)))[0]
        return self._machines[name]

    def _instance(self, command: Command, machine: str, name: str) -> int:
        key = (command, self.machine(machine), name)
        if key not in self._instances:
            payload = struct.pack("<ii", _NEW_INSTANCE, key[1]) + _name_payload(name)
            self._instances[key] = struct.unpack("<i", self.execute(command, payload))[0]
        return self._instances[key]

    # ------------------------------------------------------------------ ADC
    def adc_channel_count(self, machine: str, peripheral: str) -> int:
        instance = self._instance(Command.ADC, machine, peripheral)
        return struct.unpack("<i", self.execute(Command.ADC, struct.pack("<iB", instance, _ADC_GET_COUNT)))[0]

    def adc_get(self, machine: str, peripheral: str, channel: int) -> int:
        instance = self._instance(Command.ADC, machine, peripheral)
        data = self.execute(Command.ADC, struct.pack("<iBi", instance, _ADC_GET_VALUE, channel))
        return struct.unpack("<I", data)[0]

    def adc_set(self, machine: str, peripheral: str, channel: int, value: int) -> None:
        instance = self._instance(Command.ADC, machine, peripheral)
        value = max(0, min(0xFFFFFFFF, int(value)))
        self.execute(Command.ADC, struct.pack("<iBiI", instance, _ADC_SET_VALUE, channel, value))

    # ------------------------------------------------------------------ GPIO
    def gpio_get(self, machine: str, peripheral: str, pin: int) -> bool:
        instance = self._instance(Command.GPIO_PORT, machine, peripheral)
        data = self.execute(Command.GPIO_PORT, struct.pack("<iBi", instance, _GPIO_GET_STATE, pin))
        return bool(data[0])

    def gpio_set(self, machine: str, peripheral: str, pin: int, state: bool) -> None:
        instance = self._instance(Command.GPIO_PORT, machine, peripheral)
        self.execute(Command.GPIO_PORT, struct.pack("<iBiB", instance, _GPIO_SET_STATE, pin, 1 if state else 0))

    def gpio_subscribe(self, machine: str, peripheral: str, pin: int,
                       handler: Callable[[GpioEvent], None]) -> int:
        instance = self._instance(Command.GPIO_PORT, machine, peripheral)
        descriptor = self._next_descriptor
        self._next_descriptor += 1
        self._event_handlers[descriptor] = handler
        self.execute(Command.GPIO_PORT, struct.pack("<iBii", instance, _GPIO_REGISTER_EVENT, pin, descriptor))
        return descriptor

    # ------------------------------------------------------------------ system bus
    def _bus_header(self, machine: str, context: str, operation: int, width: int, address: int, count: int) -> bytes:
        instance = self._instance(Command.SYSTEM_BUS, machine, context)
        return struct.pack("<iBBQI", instance, operation, width, address, count)

    def bus_read(self, machine: str, address: int, width: int = 4, context: str = "sysbus") -> int:
        if width not in _WIDTH_FORMATS:
            raise ValueError(f"unsupported access width: {width}")
        data = self.execute(Command.SYSTEM_BUS, self._bus_header(machine, context, _BUS_READ, width, address, 1))
        return struct.unpack(_WIDTH_FORMATS[width], data[:width])[0]

    def bus_read_bytes(self, machine: str, address: int, count: int, context: str = "sysbus") -> bytes:
        """Read `count` bytes in one request (multi-byte access), e.g. a frame buffer."""
        data = self.execute(Command.SYSTEM_BUS, self._bus_header(machine, context, _BUS_READ, _MULTI_BYTE, address, count))
        return bytes(data[:count])

    def bus_write(self, machine: str, address: int, value: int, width: int = 4, context: str = "sysbus") -> None:
        if width not in _WIDTH_FORMATS:
            raise ValueError(f"unsupported access width: {width}")
        mask = (1 << (8 * width)) - 1
        payload = self._bus_header(machine, context, _BUS_WRITE, width, address, 1)
        payload += struct.pack(_WIDTH_FORMATS[width], int(value) & mask)
        self.execute(Command.SYSTEM_BUS, payload)
