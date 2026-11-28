"""Renode External Control client for the message protocol of Renode >= 1.17.

Renode 1.17 replaced the handshake-and-opcode protocol (``renode_api.py``) with
framed messages, matching ``tools/external_control_client`` upstream:

```text
message  = id:u16 | payload_size:i32 | payload
payload  = command:u16 | type:u8 | data          type: 0 request, 1 event request,
                                                      2 success, 3 invalid command, 4 error
```

Requests from the client set bit 15 of the id; the server echoes the id in
its response. The server also sends its own requests while a command runs,
for example GPIO edges and CAN frames during ``RunFor``. Like the server, the
client tells them apart by payload type, not by id. They are event requests
``ed:i32 | event data``, each answered with a success message that echoes
the event's id. Virtual time is in nanoseconds.

The public methods mirror ``RenodeExternalControl`` (``run_for`` and
``get_time_us`` still take and return microseconds), so the Renode adapter
works with either protocol. Only available here:

* CAN frames in and out (``CAN.CANExternalControlBus``).
* SPI devices answered by the client (``SPI.ExternalControlSPIPeripheral``):
  every byte the MCU clocks out arrives as an event, and the reply is the
  byte it clocks in.

Not used: the ``CustomCommand`` event request (run a Monitor command) fails
with a NullReferenceException unless a Monitor-side
``AttachCustomCommandCallbackToMonitor`` ran first, which itself needs a
connected client. Sensor models are fed through a sysbus bridge instead (see
``docs/reference/renode.md``).
"""
from __future__ import annotations

import enum
import socket
import struct
from dataclasses import dataclass
from typing import Callable

from .renode_api import GpioEvent, RenodeApiError, ReturnCode

PROTOCOL_VERSION = 0
CLIENT_INITIATED = 0x8000


class Command(enum.IntEnum):
    CHECK_VERSION = 0
    RUN_FOR = 1
    GET_TIME = 2
    GET_MACHINE = 3
    ADC = 4
    GPIO = 5
    SYSTEM_BUS = 6
    TIME_ELAPSED_CALLBACK = 7
    SPI = 8
    CAN_BUS = 9
    CUSTOM_COMMAND = 10
    EMULATION = 11


class Type(enum.IntEnum):
    REQUEST = 0
    EVENT_REQUEST = 1
    SUCCESS = 2
    INVALID_COMMAND = 3
    ERROR = 4


_ADC_GET_COUNT, _ADC_GET_VALUE, _ADC_SET_VALUE = 0, 1, 2
_GPIO_GET, _GPIO_SET, _GPIO_REGISTER = 0, 1, 2
_BUS_READ, _BUS_WRITE = 0, 1
_CAN_SEND, _CAN_REGISTER = 0, 2
_SPI_TRANSMIT, _SPI_FINISH = 0, 1
_WIDTH_FORMATS = {1: "<B", 2: "<H", 4: "<I", 8: "<Q"}
_MULTI_BYTE = 128                     # AccessWidth.MultiByte: count is in bytes


@dataclass(frozen=True)
class CanFrame:
    time_ns: int
    frame_id: int
    data: bytes


class RenodeMessageClient:
    """Blocking client for one TCP connection (Renode >= 1.17)."""

    protocol = "message"

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._next_id = 0
        self._machines: dict[str, int] = {}
        self._instances: dict[tuple[int, int, str], int] = {}
        self._handlers: list[Callable[[bytes], bytes | None]] = []

    @classmethod
    def connect(cls, host: str, port: int, timeout: float = 10.0) -> "RenodeMessageClient":
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client = cls(sock)
        try:
            client.request(Command.CHECK_VERSION, struct.pack("<I", PROTOCOL_VERSION))
        except BaseException:
            sock.close()
            raise
        sock.settimeout(None)          # RunFor may legitimately take long in wall-clock time
        return client

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------ framing
    def _recv_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self.sock.recv(size - len(chunks))
            if not chunk:
                raise ConnectionError("Renode closed the External Control connection")
            chunks.extend(chunk)
        return bytes(chunks)

    def _send(self, message_id: int, command: int, kind: Type, data: bytes = b"") -> None:
        payload = struct.pack("<HB", int(command), int(kind)) + data
        self.sock.sendall(struct.pack("<Hi", message_id, len(payload)) + payload)

    def _read(self) -> tuple[int, int, Type, bytes]:
        message_id, size = struct.unpack("<Hi", self._recv_exact(6))
        if size < 3:
            raise ConnectionError(f"malformed External Control message (payload size {size})")
        payload = self._recv_exact(size)
        command, kind = struct.unpack_from("<HB", payload)
        return message_id, command, Type(kind), payload[3:]

    def _serve(self, message_id: int, command: int, kind: Type, data: bytes) -> None:
        """Answer a request Renode sent us (events during RunFor)."""
        if kind is not Type.EVENT_REQUEST or len(data) < 4:
            self._send(message_id, command, Type.ERROR, b"embeddyn client serves event requests only")
            return
        (descriptor,) = struct.unpack_from("<i", data)
        handler = self._handlers[descriptor] if 0 <= descriptor < len(self._handlers) else None
        reply = handler(data[4:]) if handler else None
        self._send(message_id, command, Type.SUCCESS, reply or b"")

    def request(self, command: Command, data: bytes = b"") -> bytes:
        message_id = CLIENT_INITIATED | self._next_id
        self._next_id = (self._next_id + 1) & 0x7FFF
        self._send(message_id, command, Type.REQUEST, data)
        while True:
            reply_id, reply_command, kind, reply = self._read()
            if kind in (Type.REQUEST, Type.EVENT_REQUEST):    # Renode's own requests (events during RunFor)
                self._serve(reply_id, reply_command, kind, reply)
                continue
            if reply_id != message_id:
                raise RenodeApiError(ReturnCode.FATAL_ERROR, int(command), f"response id {reply_id:#x} for {message_id:#x}")
            if kind is Type.SUCCESS:
                return reply
            text = reply.decode("utf-8", "replace") or kind.name
            code = ReturnCode.INVALID_COMMAND if kind is Type.INVALID_COMMAND else ReturnCode.COMMAND_FAILED
            raise RenodeApiError(code, int(command), text)

    # ------------------------------------------------------------------ handles
    def machine(self, name: str) -> int:
        if name not in self._machines:
            encoded = name.encode()
            (descriptor,) = struct.unpack("<i", self.request(Command.GET_MACHINE, struct.pack("<I", len(encoded)) + encoded))
            if descriptor < 0:
                raise RenodeApiError(ReturnCode.COMMAND_FAILED, Command.GET_MACHINE, f"no machine {name!r}")
            self._machines[name] = descriptor
        return self._machines[name]

    def _instance(self, command: Command, machine: str, name: str) -> int:
        key = (int(command), self.machine(machine), name)
        if key not in self._instances:
            encoded = name.encode()
            data = struct.pack("<iiI", -1, key[1], len(encoded)) + encoded
            (self._instances[key],) = struct.unpack("<i", self.request(command, data))
        return self._instances[key]

    def _register(self, handler: Callable[[bytes], bytes | None]) -> int:
        self._handlers.append(handler)
        return len(self._handlers) - 1

    # ------------------------------------------------------------------ time
    def run_for(self, microseconds: int | float) -> None:
        if microseconds < 0:
            raise ValueError("cannot run for a negative time")
        self.request(Command.RUN_FOR, struct.pack("<Q", int(round(microseconds * 1000))))

    def get_time_us(self) -> int:
        (nanoseconds,) = struct.unpack("<Q", self.request(Command.GET_TIME, bytes(8)))
        return nanoseconds // 1000

    # ------------------------------------------------------------------ ADC
    def adc_channel_count(self, machine: str, peripheral: str) -> int:
        instance = self._instance(Command.ADC, machine, peripheral)
        return struct.unpack("<i", self.request(Command.ADC, struct.pack("<iB", instance, _ADC_GET_COUNT)))[0]

    def adc_get(self, machine: str, peripheral: str, channel: int) -> int:
        instance = self._instance(Command.ADC, machine, peripheral)
        return struct.unpack("<I", self.request(Command.ADC, struct.pack("<iBi", instance, _ADC_GET_VALUE, channel)))[0]

    def adc_set(self, machine: str, peripheral: str, channel: int, value: int) -> None:
        instance = self._instance(Command.ADC, machine, peripheral)
        self.request(Command.ADC, struct.pack("<iBiI", instance, _ADC_SET_VALUE, channel, int(value) & 0xFFFFFFFF))

    # ------------------------------------------------------------------ GPIO
    def gpio_get(self, machine: str, peripheral: str, pin: int) -> bool:
        instance = self._instance(Command.GPIO, machine, peripheral)
        return bool(self.request(Command.GPIO, struct.pack("<ibi", instance, _GPIO_GET, pin))[0])

    def gpio_set(self, machine: str, peripheral: str, pin: int, state: bool) -> None:
        instance = self._instance(Command.GPIO, machine, peripheral)
        self.request(Command.GPIO, struct.pack("<ibiB", instance, _GPIO_SET, pin, 1 if state else 0))

    def gpio_subscribe(self, machine: str, peripheral: str, pin: int, handler: Callable[[GpioEvent], None]) -> int:
        instance = self._instance(Command.GPIO, machine, peripheral)

        def on_event(data: bytes) -> None:
            time_ns, state = struct.unpack_from("<QB", data)
            handler(GpioEvent(descriptor, time_ns / 1000, bool(state)))     # microseconds, ns resolution

        descriptor = self._register(on_event)
        self.request(Command.GPIO, struct.pack("<ibii", instance, _GPIO_REGISTER, pin, descriptor))
        return descriptor

    # ------------------------------------------------------------------ system bus
    def _bus_header(self, machine: str, context: str, operation: int, width: int, address: int, count: int) -> bytes:
        instance = self._instance(Command.SYSTEM_BUS, machine, context)
        return struct.pack("<iBBQI", instance, operation, width, address, count)

    def bus_read(self, machine: str, address: int, width: int = 4, context: str = "sysbus") -> int:
        data = self.request(Command.SYSTEM_BUS, self._bus_header(machine, context, _BUS_READ, width, address, 1))
        return struct.unpack(_WIDTH_FORMATS[width], data[:width])[0]

    def bus_read_bytes(self, machine: str, address: int, count: int, context: str = "sysbus") -> bytes:
        """Read `count` bytes in one request (multi-byte access), e.g. a frame buffer."""
        data = self.request(Command.SYSTEM_BUS, self._bus_header(machine, context, _BUS_READ, _MULTI_BYTE, address, count))
        return bytes(data[:count])

    def bus_write(self, machine: str, address: int, value: int, width: int = 4, context: str = "sysbus") -> None:
        mask = (1 << (8 * width)) - 1
        payload = self._bus_header(machine, context, _BUS_WRITE, width, address, 1)
        self.request(Command.SYSTEM_BUS, payload + struct.pack(_WIDTH_FORMATS[width], int(value) & mask))

    # ------------------------------------------------------------------ CAN
    def can_send(self, machine: str, bus: str, frame_id: int, data: bytes) -> None:
        """Put a frame on the CAN hub through a `CAN.CANExternalControlBus` peripheral."""
        if len(data) > 64:
            raise ValueError("CAN frames carry at most 64 bytes")
        instance = self._instance(Command.CAN_BUS, machine, bus)
        self.request(Command.CAN_BUS, struct.pack("<ibiI", instance, _CAN_SEND, len(data), frame_id) + bytes(data))

    def can_subscribe(self, machine: str, bus: str, handler: Callable[[CanFrame], None]) -> int:
        instance = self._instance(Command.CAN_BUS, machine, bus)

        def on_frame(data: bytes) -> None:
            time_ns, length, frame_id = struct.unpack_from("<QiI", data)
            handler(CanFrame(time_ns, frame_id, bytes(data[16:16 + length])))

        descriptor = self._register(on_frame)
        self.request(Command.CAN_BUS, struct.pack("<ibi", instance, _CAN_REGISTER, descriptor))
        return descriptor

    # ------------------------------------------------------------------ SPI
    def spi_register(self, machine: str, peripheral: str, transmit: Callable[[int], int],
                     finish: Callable[[], None] | None = None) -> int:
        """Answer an `SPI.ExternalControlSPIPeripheral`: `transmit(mosi)` returns the MISO byte;
        `finish()` runs when the controller ends a transmission (not every controller does)."""
        instance = self._instance(Command.SPI, machine, peripheral)

        def on_event(data: bytes) -> bytes:
            if data and data[0] == _SPI_TRANSMIT and len(data) >= 2:
                return bytes([int(transmit(data[1])) & 0xFF])
            if data and data[0] == _SPI_FINISH and finish is not None:
                finish()
            return b""

        descriptor = self._register(on_event)
        self.request(Command.SPI, struct.pack("<ii", instance, descriptor))
        return descriptor

