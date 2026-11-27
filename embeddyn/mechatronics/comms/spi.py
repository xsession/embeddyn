"""SPI transfers: a register-device decoder and an MCU reading a LIS3DH-style accelerometer (python plants).

Payloads: ``{"proto": "spi", "dir": "mosi"|"miso", "data": "<hex>", "cs": "acc", "mode": 0,
"hz": 4e6, "src": "mcu"}``. The master publishes what it clocks out on MOSI; the selected
slave answers in the same instant with the bytes it drove on MISO during that transfer
(the first one is whatever the shift register held, usually junk). No slave, no MISO event:
the master reads the pulled-up line, 0xFF.

Register devices (the usual sensor convention): the first MOSI byte is the register
address with a read bit (0x80) and an auto-increment bit (0x40 on ST parts); the rest of a
read clocks out data. `decode` names registers from the analyzer config::

    spi:
      devices:
        acc:
          name: LIS3DH
          read_bit: 0x80
          inc_bit: 0x40
          registers:
            "0x0F": {name: WHO_AM_I, expect: 0x33}
            "0x28": {name: [x, y, z], format: i16 LE, shift: 4, scale: 0.001, unit: g}

SPI has no acknowledge and no checksum: a wrong clock mode, a slow slave or a glitch on
SCK shift every following bit and the master happily reads nonsense. A WHO_AM_I register
with a known value (``expect``) is how firmware notices.
"""
from __future__ import annotations

import struct
from typing import Any, Mapping

SIZES = {"u8": 1, "i8": 1, "u16": 2, "i16": 2, "u32": 4, "i32": 4}


def sample_edge(mode: int) -> str:
    """The SCK edge a mode samples data on: 0 and 3 rising, 1 and 2 falling."""
    return "rising" if int(mode) in (0, 3) else "falling"


def _f(label: str, value: Any, start: int | None = None, end: int | None = None, children=None) -> dict[str, Any]:
    item: dict[str, Any] = {"label": label, "value": value}
    if start is not None:
        item["bytes"] = [start, end if end is not None else start + 1]
    if children:
        item["children"] = children
    return item


def _int(value: Any) -> int:
    return int(value, 0) if isinstance(value, str) else int(value)


def _registers(spec: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    return {_int(k): v for k, v in (spec.get("registers") or {}).items()}


def decode(data: bytes, direction: str, config: Mapping[str, Any] | None, context: dict[str, Any],
           cs: str | None = None) -> dict[str, Any]:
    devices = (config or {}).get("devices") or {}
    spec = devices.get(cs or "") or {}
    name = spec.get("name") or cs or "slave"
    read_bit, inc_bit = _int(spec.get("read_bit", 0x80)), _int(spec.get("inc_bit", 0))
    registers = _registers(spec)
    if direction == "mosi":
        if not data:
            return {"type": "empty", "summary": f"{name}: empty transfer", "fields": [], "ok": True}
        command = data[0]
        read = bool(command & read_bit)
        address = command & ~(read_bit | inc_bit) & 0xFF
        context[f"spi:{cs}"] = {"address": address, "read": read, "length": len(data)}
        reg = registers.get(address) or {}
        reg_name = reg.get("name")
        reg_text = f"0x{address:02X}" + (f" {reg_name if isinstance(reg_name, str) else '/'.join(reg_name)}" if reg_name else "")
        fields = [_f("command", f"0x{command:02X}: {'read' if read else 'write'} {reg_text}"
                     + (", auto-increment" if inc_bit and command & inc_bit else ""), 0)]
        if read:
            return {"type": "read", "summary": f"{name}: read {reg_text} ({len(data) - 1} bytes)", "fields": fields, "ok": True}
        if len(data) > 1:
            fields.append(_f("data", data[1:].hex(" "), 1, len(data)))
        return {"type": "write", "summary": f"{name}: write {reg_text} = {data[1:].hex(' ')}", "fields": fields, "ok": True}
    last = context.get(f"spi:{cs}") or {}
    fields = [_f("MISO", data.hex(" "), 0, len(data))]
    if not last.get("read") or len(data) < 2:
        return {"type": "MISO", "summary": f"{name} → {data.hex(' ')}", "fields": fields, "ok": True}
    fields[0]["children"] = [_f("dummy", f"0x{data[0]:02X} (clocked out with the command)", 0)]
    address, pos = int(last["address"]), 1
    values: list[str] = []
    ok = True
    while pos < len(data):
        reg = registers.get(address)
        if not reg:
            address += 1
            pos += 1
            continue
        kind, *order = str(reg.get("format", "u8")).split()
        size = SIZES.get(kind, 1)
        names = reg.get("name") if isinstance(reg.get("name"), list) else [reg.get("name", f"0x{address:02X}")]
        for label in names:
            if pos + size > len(data):
                break
            raw = int.from_bytes(data[pos:pos + size], "big" if order and order[0] == "BE" else "little", signed=kind[0] == "i")
            value = (raw >> int(reg.get("shift", 0))) * float(reg.get("scale", 1.0))
            text = f"{value:g} {reg.get('unit', '')}".strip()
            if "expect" in reg:
                expected = _int(reg["expect"])
                good = raw == expected
                ok &= good
                text = f"0x{raw:02X} ({'ok' if good else f'expected 0x{expected:02X}'})"
            fields.append(_f(str(label), text, pos, pos + size))
            values.append(f"{label}={text}")
            pos += size
            address += size
    summary = f"{name} → " + (" ".join(values) if values else data.hex(" "))
    return {"type": "read data" if ok else "read data (unexpected)", "summary": summary, "fields": fields, "ok": ok}


class Lis3dh:
    """A LIS3DH-style accelerometer on SPI (mode 0 or 3): WHO_AM_I 0x33 at 0x0F, CTRL_REG1 at 0x20,
    OUT_X_L..OUT_Z_H at 0x28..0x2D, 12-bit left-justified, 1 mg per digit (±2 g)."""

    inputs = ("ax", "ay", "az")
    outputs = ("transfers",)
    states = ()
    event_inputs = ("rx",)
    event_outputs = ("tx",)

    def __init__(self, cs: str = "acc", name: str = "lis3dh") -> None:
        self.cs, self.name = cs, name
        self.registers = bytearray(0x40)
        self.registers[0x0F] = 0x33
        self.registers[0x20] = 0x07
        self.transfers = 0
        self._out: list[tuple[float, str, Any]] = []

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t, x, u):
        return []

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, Mapping) or payload.get("proto") != "spi" or payload.get("dir") != "mosi" \
                or payload.get("cs") != self.cs:
            return
        data = bytes.fromhex(str(payload.get("data", "")))
        if not data:
            return
        self.transfers += 1
        command = data[0]
        address = command & 0x3F
        inc = bool(command & 0x40)
        if command & 0x80:
            out = bytearray([0xFF])                   # the shift register's content while the command came in
            for i in range(len(data) - 1):
                out.append(self.registers[(address + (i if inc else 0)) & 0x3F])
        else:
            for i, value in enumerate(data[1:]):
                target = (address + (i if inc else 0)) & 0x3F
                if target not in (0x0F,) and not 0x28 <= target <= 0x2D:
                    self.registers[target] = value
            out = bytearray([0xFF] * len(data))
        self._out.append((t, "tx", {"proto": "spi", "dir": "miso", "data": bytes(out).hex(), "cs": self.cs,
                                    "mode": payload.get("mode", 0), "hz": payload.get("hz"), "src": self.name}))

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        for i, axis in enumerate(("ax", "ay", "az")):
            raw = max(-2048, min(2047, int(round(float(u.get(axis, 1.0 if axis == "az" else 0.0)) * 1000))))
            struct.pack_into("<h", self.registers, 0x28 + 2 * i, raw << 4)

    def output(self, t, x, u):
        return {"transfers": float(self.transfers)}

    def pop_events(self) -> list[tuple[float, str, Any]]:
        out, self._out = self._out, []
        return out


class SpiMaster:
    """An MCU polling the accelerometer: WHO_AM_I and CTRL_REG1 at start, then X/Y/Z every `period_ms`
    (WHO_AM_I again every 10th poll); counts unexpected WHO_AM_I values and transfers nobody answered."""

    inputs = ()
    outputs = ("ax", "ay", "az", "id_errors", "no_answer", "polls")
    states = ()
    event_inputs = ("rx",)
    event_outputs = ("tx",)

    def __init__(self, cs: str = "acc", mode: int = 0, hz: float = 4e6, period_ms: float = 5.0, name: str = "mcu") -> None:
        self.cs, self.mode, self.hz, self.period, self.name = cs, int(mode), float(hz), period_ms / 1000.0, name
        self.next = 0.002
        self.step = 0
        self.pending: tuple[float, str] | None = None
        self.ax = self.ay = self.az = 0.0
        self.id_errors = self.no_answer = self.polls = 0
        self._out: list[tuple[float, str, Any]] = []

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t, x, u):
        return []

    def _send(self, t: float, data: bytes, kind: str) -> None:
        self._out.append((t, "tx", {"proto": "spi", "dir": "mosi", "data": data.hex(), "cs": self.cs, "mode": self.mode,
                                    "hz": self.hz, "src": self.name}))
        self.pending = (t + min(0.002, self.period / 2), kind)    # answer deadline

    def _handle(self, kind: str, miso: bytes) -> None:
        if kind == "id" and (len(miso) < 2 or miso[1] != 0x33):
            self.id_errors += 1
        elif kind == "xyz" and len(miso) >= 7:
            x, y, z = struct.unpack_from("<hhh", miso, 1)
            self.ax, self.ay, self.az = (x >> 4) / 1000.0, (y >> 4) / 1000.0, (z >> 4) / 1000.0
            self.polls += 1

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, Mapping) or payload.get("proto") != "spi" or payload.get("dir") != "miso" \
                or payload.get("cs") != self.cs or self.pending is None:
            return
        _, kind = self.pending
        self.pending = None
        self._handle(kind, bytes.fromhex(str(payload.get("data", ""))))

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        if self.pending is not None and t1 > self.pending[0]:     # nobody drove MISO: the pull-up reads 0xFF
            _, kind = self.pending
            self.pending = None
            self.no_answer += 1
            self._handle(kind, b"\xff" * 7)
        while self.next <= t1:
            at = self.next
            self.next += self.period
            if self.step == 0:
                self._send(at, bytes([0x8F, 0x00]), "id")
            elif self.step == 1:
                self._send(at, bytes([0x20, 0x57]), "write")      # 100 Hz, X/Y/Z on
            elif self.step % 10 == 0:
                self._send(at, bytes([0x8F, 0x00]), "id")
            else:
                self._send(at, bytes([0xE8]) + bytes(6), "xyz")   # read, auto-increment, OUT_X_L
            self.step += 1

    def output(self, t, x, u):
        return {"ax": self.ax, "ay": self.ay, "az": self.az, "id_errors": float(self.id_errors),
                "no_answer": float(self.no_answer), "polls": float(self.polls)}

    def pop_events(self) -> list[tuple[float, str, Any]]:
        out, self._out = self._out, []
        return out


def shift_bits(data: bytes, at_bit: int, delta: int, fill: int = 1) -> bytes:
    """Bits from `at_bit` on moved by `delta` (+1: a bit is repeated there, -1: one is skipped), MSB first."""
    bits = [(byte >> (7 - k)) & 1 for byte in data for k in range(8)]
    if delta > 0:
        bits = bits[:at_bit] + [bits[at_bit] if at_bit < len(bits) else fill] + bits[at_bit:]
        bits = bits[:len(data) * 8]
    elif delta < 0:
        bits = bits[:at_bit] + bits[at_bit + 1:] + [fill]
    out = bytearray(len(data))
    for i, bit in enumerate(bits[:len(data) * 8]):
        out[i // 8] |= bit << (7 - i % 8)
    return bytes(out)
