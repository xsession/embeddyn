"""I2C transfers: a decoder and a master polling a TMP102-style temperature sensor (python plants).

Payloads: ``{"proto": "i2c", "dir": "m2s"|"s2m", "data": "<hex>", "src": "mcu"}``. A master
frame starts with the address byte (7-bit address << 1 | R/W); a slave frame carries the
bytes the slave drives during a read. I2C has no checksum: a bit error in the address
makes the slave ignore the transfer (the master sees a NACK and times out), a bit error
in data silently gives a wrong value, which is what makes long I2C cables dangerous.
"""
from __future__ import annotations

import struct
from typing import Any, Mapping, Sequence

TMP102_LSB = 0.0625


def _field(label: str, value: Any, start: int | None = None, end: int | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"label": label, "value": value}
    if start is not None:
        item["bytes"] = [start, end if end is not None else start + 1]
    return item


def decode(data: bytes, direction: str, config: Mapping[str, Any] | None = None,
           context: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx = context if context is not None else {}
    devices = {int(k, 0) if isinstance(k, str) else int(k): v for k, v in ((config or {}).get("devices") or {}).items()}
    if direction == "m2s":
        if not data:
            return {"type": "empty", "summary": "empty transfer", "fields": []}
        address, read = data[0] >> 1, bool(data[0] & 1)
        name = (devices.get(address) or {}).get("name", f"0x{address:02X}")
        fields = [_field("address", f"0x{address:02X} {name if name != f'0x{address:02X}' else ''}".strip(), 0),
                  _field("R/W", "read" if read else "write", 0)]
        ctx["address"], ctx["read"] = address, read
        if read:
            return {"type": "read", "summary": f"read {name}", "fields": fields, "address": address}
        if len(data) >= 2:
            ctx.setdefault("pointer", {})[address] = data[1]
            fields.append(_field("register", f"0x{data[1]:02X}", 1))
        if len(data) > 2:
            fields.append(_field("data", data[2:].hex(" "), 2, len(data)))
        return {"type": "write", "summary": f"write {name} reg 0x{data[1]:02X}" + (f" = {data[2:].hex(' ')}" if len(data) > 2 else "")
                if len(data) >= 2 else f"write {name}", "fields": fields, "address": address}
    address = ctx.get("address")
    spec = devices.get(address) or {}
    pointer = (ctx.get("pointer") or {}).get(address, 0)
    register = (spec.get("registers") or {}).get(str(pointer)) or (spec.get("registers") or {}).get(pointer)
    fields = [_field("data", data.hex(" "), 0, len(data))]
    summary = f"{spec.get('name', f'0x{address:02X}' if address is not None else '?')} → {data.hex(' ')}"
    if register and len(data) >= 2:
        (raw,) = struct.unpack(">h", data[:2])
        value = (raw >> int(register.get("shift", 0))) * float(register.get("scale", 1.0))
        text = f"{value:g} {register.get('unit', '')}".strip()
        fields.append(_field(str(register.get("name", f"reg 0x{pointer:02X}")), text, 0, 2))
        summary += f" ({register.get('name')} {text})"
    return {"type": "read data", "summary": summary, "fields": fields, "address": address}


class I2cMaster:
    """Polls a temperature sensor: write the pointer register, then read two bytes (python plant)."""

    inputs = ()
    outputs = ("temperature", "nacks", "reads")
    states = ()
    event_inputs = ("rx",)
    event_outputs = ("tx",)

    def __init__(self, address: int = 0x48, period_ms: float = 10.0, scl_hz: float = 400e3, name: str = "mcu") -> None:
        self.address, self.period, self.scl, self.name = int(address), period_ms / 1000.0, float(scl_hz), name
        self.next = 0.005
        self.waiting = None
        self.temperature = 0.0
        self.nacks = 0
        self.reads = 0
        self._out: list[tuple[float, str, Any]] = []

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        return []

    def _byte_time(self, count: int) -> float:
        return count * 9 / self.scl                              # 8 bits + ACK

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("dir") != "s2m" or self.waiting is None:
            return
        data = bytes.fromhex(payload["data"])
        if len(data) >= 2:
            (raw,) = struct.unpack(">h", data[:2])
            self.temperature = (raw >> 4) * TMP102_LSB
            self.reads += 1
        self.waiting = None

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        if self.waiting is not None and t1 > self.waiting:      # no answer: the address was NACKed
            self.nacks += 1
            self.waiting = None
        while self.next <= t1:
            at = self.next
            self._out.append((at, "tx", {"proto": "i2c", "dir": "m2s", "data": bytes([self.address << 1, 0x00]).hex(), "src": self.name}))
            read_at = at + self._byte_time(2) + 5e-6
            self._out.append((read_at, "tx", {"proto": "i2c", "dir": "m2s", "data": bytes([self.address << 1 | 1]).hex(), "src": self.name}))
            self.waiting = read_at + self._byte_time(3) + 0.001
            self.next += self.period

    def output(self, t, x, u):
        return {"temperature": self.temperature, "nacks": float(self.nacks), "reads": float(self.reads)}

    def pop_events(self) -> list[tuple[float, str, Any]]:
        out, self._out = self._out, []
        return out


class Tmp102:
    """TMP102-style sensor: 12-bit temperature in register 0, MSB first, 0.0625 degC per LSB."""

    inputs = ("temperature",)
    outputs = ("transfers",)
    states = ()
    event_inputs = ("rx",)
    event_outputs = ("tx",)

    def __init__(self, address: int = 0x48, scl_hz: float = 400e3, name: str = "tmp102") -> None:
        self.address, self.scl, self.name = int(address), float(scl_hz), name
        self.pointer = 0
        self.temperature = 25.0
        self.transfers = 0
        self._out: list[tuple[float, str, Any]] = []

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        return []

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("dir") != "m2s":
            return
        data = bytes.fromhex(payload["data"])
        if not data or data[0] >> 1 != self.address:
            return                                               # not our address (or a corrupted one): NACK, silence
        self.transfers += 1
        if data[0] & 1:
            raw = int(round(self.temperature / TMP102_LSB)) << 4
            reply = struct.pack(">h", max(-32768, min(32767, raw)))
            self._out.append((t + 9 / self.scl, "tx", {"proto": "i2c", "dir": "s2m", "data": reply.hex(), "src": self.name}))
        elif len(data) >= 2:
            self.pointer = data[1]

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        self.temperature = float(u.get("temperature", self.temperature))

    def output(self, t, x, u):
        return {"transfers": float(self.transfers)}

    def pop_events(self) -> list[tuple[float, str, Any]]:
        out, self._out = self._out, []
        return out
