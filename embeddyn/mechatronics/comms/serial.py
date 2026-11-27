"""UART and RS-485: a bit-level line model, NMEA and Modbus RTU decoders, and boards (python plants).

Payloads: ``{"proto": "uart"|"rs485", "data": "<hex>", "baud": 115200, "format": "8N1",
"src": "gps"}``, one event per frame (an NMEA sentence, a Modbus RTU frame), sent at the
event's time. After an impaired medium (`comms.faults`) a payload also carries
``"errors": [{"i": byte index, "kind": "framing"|"parity"|"noise"|"break"|"overrun"}]``,
the flags a real UART raises for the bytes it received.

**Line model.** `transmit` turns bytes into line levels (idle high, start bit, data LSB
first, parity, stop bits) and `receive` decodes them like a UART peripheral: it waits for a
falling edge (seen on its 16× oversampling clock), checks the start bit at its middle,
samples every bit at the middle with a 3-sample majority vote (noise flag when the samples
disagree) at *its own* baud rate, checks parity and the stop bit (framing error; all zeros
is a break) and re-arms in the middle of the stop bit. So a baud-rate mismatch drifts the
sample point until bits are read twice or skipped, a glitch on an idle line starts a
garbage byte, a late driver enable makes a data bit look like the start bit: the same
garbage a logic analyzer shows on a real bench.

**Modbus RTU** frames: unit, function, data, CRC-16 (0xA001, low byte first). Requests and
responses are told apart by the pending request of the bus (`decode_rtu` keeps it in the
analyzer's context). `ModbusMaster` and `ModbusSlave` are a PLC and a drive on RS-485.
**NMEA 0183** sentences (``$GPGGA,...*hh``) carry an XOR checksum; `GpsReceiver` sends
GGA and RMC, `NmeaReader` is the MCU that parses them and counts what went wrong.
"""
from __future__ import annotations

import math
import struct
from bisect import bisect_left, bisect_right
from typing import Any, Mapping, Sequence

# -- line model ---------------------------------------------------------------------------------------------


def parse_format(fmt: Any) -> tuple[int, str, float]:
    """"8N1" → (8 data bits, parity "N"|"E"|"O"|"M"|"S", 1 stop bit); also "7E1", "8N2", "8N1.5"."""
    text = str(fmt or "8N1").upper()
    return int(text[0]), text[1], float(text[2:] or 1)


def char_bits(fmt: Any) -> float:
    bits, parity, stop = parse_format(fmt)
    return 1 + bits + (parity != "N") + stop


def char_time(baud: float, fmt: Any = "8N1") -> float:
    return char_bits(fmt) / float(baud)


def parity_bit(value: int, bits: int, kind: str) -> int:
    ones = bin(value & ((1 << bits) - 1)).count("1") & 1
    return {"E": ones, "O": ones ^ 1, "M": 1, "S": 0}.get(kind, 0)


class Line:
    """Line levels as edges: the level is `levels[i]` from `times[i]` on, idle (1) before the first edge."""

    def __init__(self, times: list[float], levels: list[int], end: float) -> None:
        self.times, self.levels, self.end = times, levels, end

    def level(self, t: float) -> int:
        i = bisect_right(self.times, t) - 1
        return 1 if i < 0 else self.levels[i]

    def next_low(self, t: float) -> float | None:
        """Time of the first falling edge at or after t (the line must go high first if it is low)."""
        i = bisect_left(self.times, t)
        while i < len(self.times):
            if self.levels[i] == 0:
                return self.times[i]
            i += 1
        return None

    def overlay(self, windows: Sequence[tuple[float, float, int]]) -> "Line":
        """The line with levels forced in [t0, t1) windows (glitches, a driver that is off, contention)."""
        if not windows:
            return self
        points = sorted({*self.times, *(w[0] for w in windows), *(w[1] for w in windows)})
        times: list[float] = []
        levels: list[int] = []
        for k, t in enumerate(points):
            probe = t + (points[k + 1] - t) / 2 if k + 1 < len(points) else t + 1e-12
            level = self.level(probe)
            for t0, t1, forced in windows:
                if t0 <= probe < t1:
                    level = forced
            if not levels or levels[-1] != level:
                if not levels and level == 1:
                    continue
                times.append(t)
                levels.append(level)
        return Line(times, levels, max(self.end, max(w[1] for w in windows)))


def transmit(data: bytes, baud: float, fmt: Any = "8N1", gaps: Sequence[float] | None = None,
             flips: set[int] | None = None, start: float = 0.0) -> tuple[Line, list[float]]:
    """The line levels of `data` sent at `baud` from `start`: (line, start time of every character).
    `gaps` are idle times before each character (s); `flips` are bit cells (numbered over the whole
    stream: start, data, parity, stop of every character) inverted on the wire."""
    bits, parity, stop = parse_format(fmt)
    cell = 1.0 / float(baud)
    times: list[float] = []
    levels: list[int] = []
    starts: list[float] = []
    flips = flips or set()
    t, index = start, 0

    def put(at: float, level: int) -> None:
        if (levels[-1] if levels else 1) != level:
            times.append(at)
            levels.append(level)

    for i, byte in enumerate(data):
        if gaps:
            t += max(0.0, float(gaps[i]))
        starts.append(t)
        cells = [0] + [(byte >> k) & 1 for k in range(bits)] + ([parity_bit(byte, bits, parity)] if parity != "N" else [])
        for level in cells:
            put(t, level ^ (index in flips))
            t += cell
            index += 1
        put(t, 1 ^ (index in flips))                      # stop bit(s)
        t += stop * cell
        index += 1
    put(t, 1)
    return Line(times, levels, t), starts


def receive(line: Line, baud: float, fmt: Any = "8N1", phase: float = 0.0, after: float = -1.0) -> list[dict[str, Any]]:
    """Characters a UART at `baud` reads from `line`: ``{"t": start edge, "end": stop sample, "byte",
    "errors": [...]}``; `phase` is where its 16× sampling clock ticks (0..1 of a tick)."""
    bits, parity, _ = parse_format(fmt)
    cell = 1.0 / float(baud)
    tick = cell / 16
    out: list[dict[str, Any]] = []
    pos = after

    def sample(at: float) -> tuple[int, bool]:
        votes = line.level(at - tick) + line.level(at) + line.level(at + tick)
        return int(votes >= 2), votes not in (0, 3)

    while True:
        edge = line.next_low(pos)
        if edge is None or edge > line.end + cell:
            break
        te = math.ceil((edge - phase * tick) / tick) * tick + phase * tick     # seen on the next sampling tick
        level, noisy = sample(te + 0.5 * cell - tick)
        if level:                                          # not a start bit after all: a glitch
            pos = te + tick
            continue
        value, errors = 0, []
        for k in range(bits):
            v, n = sample(te + (1.5 + k) * cell - tick)
            value |= v << k
            noisy |= n
        c = 1.5 + bits
        p_ok = True
        if parity != "N":
            v, n = sample(te + c * cell - tick)
            noisy |= n
            p_ok = v == parity_bit(value, bits, parity)
            c += 1
        stop_level, n = sample(te + c * cell - tick)
        noisy |= n
        if not stop_level:
            errors.append("break" if value == 0 else "framing")
        if not p_ok:
            errors.append("parity")
        if noisy:
            errors.append("noise")
        out.append({"t": te, "end": te + c * cell, "byte": value, "errors": errors})
        pos = te + c * cell
    return out


# -- NMEA 0183 ----------------------------------------------------------------------------------------------


def nmea(body: str) -> bytes:
    checksum = 0
    for ch in body.encode():
        checksum ^= ch
    return f"${body}*{checksum:02X}\r\n".encode()


def _coord(value: float, lat: bool) -> tuple[str, str]:
    hemi = ("N" if value >= 0 else "S") if lat else ("E" if value >= 0 else "W")
    value = abs(value)
    degrees = int(value)
    minutes = (value - degrees) * 60
    return (f"{degrees:02d}{minutes:07.4f}" if lat else f"{degrees:03d}{minutes:07.4f}"), hemi


def _parse_coord(text: str, hemi: str) -> float | None:
    try:
        dot = text.index(".")
        value = float(text[:dot - 2]) + float(text[dot - 2:]) / 60
    except ValueError:
        return None
    return -value if hemi in ("S", "W") else value


def decode_nmea(text: str) -> dict[str, Any]:
    """One sentence: talker, type, checksum check, the GGA/RMC/VTG values."""
    line = text.strip("\r\n")
    if not line.startswith("$") or "*" not in line:
        return {"ok": False, "type": "text", "summary": repr(line[:80]), "fields": [], "why": "not an NMEA sentence"}
    body, _, given = line[1:].partition("*")
    checksum = 0
    for ch in body.encode(errors="replace"):
        checksum ^= ch
    try:
        ok = int(given[:2], 16) == checksum
    except ValueError:
        ok = False
    parts = body.split(",")
    head = parts[0]
    kind = head[2:] if len(head) == 5 and head.isalnum() and head.isupper() else "garbled"
    fields = [{"label": "talker", "value": parts[0][:2]}, {"label": "sentence", "value": kind},
              {"label": "checksum", "value": f"{given[:2]} ({'ok' if ok else f'BAD, computed {checksum:02X}'})"}]
    values: dict[str, Any] = {}
    try:
        if kind == "GGA" and len(parts) >= 10:
            values = {"utc": parts[1], "lat": _parse_coord(parts[2], parts[3]), "lon": _parse_coord(parts[4], parts[5]),
                      "fix": int(parts[6] or 0), "sats": int(parts[7] or 0), "hdop": float(parts[8] or 0),
                      "alt_m": float(parts[9] or 0)}
        elif kind == "RMC" and len(parts) >= 9:
            values = {"utc": parts[1], "status": parts[2], "lat": _parse_coord(parts[3], parts[4]),
                      "lon": _parse_coord(parts[5], parts[6]), "speed_kn": float(parts[7] or 0), "course": float(parts[8] or 0)}
    except ValueError:
        values = {}
    fields += [{"label": k, "value": f"{v:.6f}" if isinstance(v, float) and k in ("lat", "lon") else v} for k, v in values.items()]
    summary = f"${parts[0]} " + " ".join(f"{k}={v:.5f}" if isinstance(v, float) and k in ("lat", "lon") else f"{k}={v}"
                                        for k, v in values.items() if k != "utc")
    return {"ok": ok, "type": kind or "NMEA", "summary": summary if values else line[:80], "fields": fields,
            "values": values, "why": None if ok else "checksum"}


class GpsReceiver:
    """A GPS module: GGA and RMC sentences every `period_ms` on a UART (driving in a circle)."""

    inputs = ()
    outputs = ("sentences",)
    states = ()
    event_inputs = ()
    event_outputs = ("tx",)

    def __init__(self, baud: float = 115200, fmt: str = "8N1", period_ms: float = 100.0, lat: float = 47.4979,
                 lon: float = 19.0402, radius_m: float = 200.0, speed_mps: float = 12.0, name: str = "gps") -> None:
        self.baud, self.fmt, self.period, self.name = float(baud), str(fmt), period_ms / 1000.0, name
        self.lat0, self.lon0, self.radius, self.speed = float(lat), float(lon), float(radius_m), float(speed_mps)
        self.next = 0.01
        self.count = 0
        self._out: list[tuple[float, str, Any]] = []

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t, x, u):
        return []

    def position(self, t: float) -> tuple[float, float, float]:
        angle = self.speed * t / self.radius
        lat = self.lat0 + self.radius * math.sin(angle) / 111_320.0
        lon = self.lon0 + self.radius * (1 - math.cos(angle)) / (111_320.0 * math.cos(math.radians(self.lat0)))
        return lat, lon, (math.degrees(angle) + 90) % 360

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        while self.next <= t1:
            at = self.next
            lat, lon, course = self.position(at)
            seconds = 12 * 3600 + 30 * 60 + at
            utc = f"{int(seconds // 3600):02d}{int(seconds % 3600 // 60):02d}{seconds % 60:05.2f}"
            la, ns = _coord(lat, True)
            lo, ew = _coord(lon, False)
            for body in (f"GPGGA,{utc},{la},{ns},{lo},{ew},1,09,0.9,121.4,M,40.1,M,,",
                         f"GPRMC,{utc},A,{la},{ns},{lo},{ew},{self.speed * 1.943844:.1f},{course:.1f},270626,,,A"):
                data = nmea(body)
                self._out.append((at, "tx", {"proto": "uart", "data": data.hex(), "baud": self.baud, "format": self.fmt,
                                             "src": self.name}))
                at += len(data) * char_time(self.baud, self.fmt)
                self.count += 1
            self.next += self.period

    def output(self, t, x, u):
        return {"sentences": float(self.count)}

    def pop_events(self) -> list[tuple[float, str, Any]]:
        out, self._out = self._out, []
        return out


class NmeaReader:
    """The MCU on the other end: parses sentences and counts checksum, framing, parity, noise and overrun errors."""

    inputs = ()
    outputs = ("lat", "lon", "speed_kn", "good", "checksum_errors", "uart_errors")
    states = ()
    event_inputs = ("rx",)
    event_outputs = ()

    def __init__(self, name: str = "mcu") -> None:
        self.name = name
        self.lat = self.lon = self.speed = 0.0
        self.good = self.bad = self.uart_errors = self.seen = 0

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t, x, u):
        return []

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, Mapping) or payload.get("proto") != "uart":
            return
        self.seen += 1
        self.uart_errors += len(payload.get("errors") or [])
        decoded = decode_nmea(bytes.fromhex(str(payload.get("data", ""))).decode("latin-1"))
        if not decoded["ok"]:
            self.bad += 1
            return
        self.good += 1
        values = decoded["values"]
        if values.get("lat") is not None:
            self.lat, self.lon = float(values["lat"]), float(values["lon"] or 0.0)
        if "speed_kn" in values:
            self.speed = float(values["speed_kn"])

    def advance(self, t0, t1, u) -> None:
        pass

    def output(self, t, x, u):
        return {"lat": self.lat, "lon": self.lon, "speed_kn": self.speed, "good": float(self.good),
                "checksum_errors": float(self.bad), "uart_errors": float(self.uart_errors)}

    def pop_events(self) -> list[tuple[float, str, Any]]:
        return []


# -- Modbus -------------------------------------------------------------------------------------------------

FUNCTIONS = {1: "Read Coils", 2: "Read Discrete Inputs", 3: "Read Holding Registers", 4: "Read Input Registers",
             5: "Write Single Coil", 6: "Write Single Register", 8: "Diagnostics", 15: "Write Multiple Coils",
             16: "Write Multiple Registers", 17: "Report Server ID", 23: "Read/Write Multiple Registers"}
EXCEPTIONS = {1: "illegal function", 2: "illegal data address", 3: "illegal data value", 4: "server device failure",
              5: "acknowledge", 6: "server device busy", 8: "memory parity error", 10: "gateway path unavailable",
              11: "gateway target device failed to respond"}


def crc16(data: bytes) -> bytes:
    """Modbus CRC-16 (reflected 0x8005, init 0xFFFF), low byte first as on the wire."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


def rtu(unit: int, pdu: bytes) -> bytes:
    frame = bytes([unit & 0xFF]) + pdu
    return frame + crc16(frame)


def _f(label: str, value: Any, start: int | None = None, end: int | None = None, children=None) -> dict[str, Any]:
    item: dict[str, Any] = {"label": label, "value": value}
    if start is not None:
        item["bytes"] = [start, end if end is not None else start + 1]
    if children:
        item["children"] = children
    return item


def _register_text(unit_cfg: Mapping[str, Any], address: int, raw: int) -> str:
    spec = (unit_cfg.get("registers") or {}).get(str(address)) or (unit_cfg.get("registers") or {}).get(address)
    if not spec:
        return f"{raw} (0x{raw:04X})"
    value = (raw - 65536 if spec.get("signed") and raw >= 32768 else raw) * float(spec.get("scale", 1.0))
    shown = f"{value:g} {spec.get('unit', '')}".strip()
    return f"{spec.get('name', address)} = {shown}"


def decode_pdu(pdu: bytes, offset: int, response: bool, request: Mapping[str, Any] | None,
               unit_cfg: Mapping[str, Any]) -> tuple[str, str, list[dict[str, Any]], dict[str, Any]]:
    """(type, summary, fields, request info) of a Modbus PDU starting at `offset` of the row's bytes."""
    if not pdu:
        return "empty", "empty PDU", [], {}
    fn = pdu[0]
    name = FUNCTIONS.get(fn & 0x7F, f"function {fn & 0x7F}")
    fields = [_f("function", f"{fn & 0x7F} {name}" + (" (exception)" if fn & 0x80 else ""), offset)]
    if fn & 0x80:
        code = pdu[1] if len(pdu) > 1 else 0
        fields.append(_f("exception", f"{code} {EXCEPTIONS.get(code, '?')}", offset + 1))
        return "exception", f"exception {code} {EXCEPTIONS.get(code, '?')} to FC{fn & 0x7F}", fields, {}
    base = (request or {}).get("address", 0)
    if not response:
        info: dict[str, Any] = {"fn": fn}
        if fn in (1, 2, 3, 4) and len(pdu) >= 5:
            address, count = struct.unpack_from(">HH", pdu, 1)
            info.update(address=address, count=count)
            fields += [_f("address", address, offset + 1, offset + 3), _f("quantity", count, offset + 3, offset + 5)]
            return f"FC{fn} request", f"FC{fn} {name} {address}..{address + count - 1}", fields, info
        if fn in (5, 6) and len(pdu) >= 5:
            address, value = struct.unpack_from(">HH", pdu, 1)
            info.update(address=address, count=1)
            text = ("ON" if value == 0xFF00 else "OFF") if fn == 5 else _register_text(unit_cfg, address, value)
            fields += [_f("address", address, offset + 1, offset + 3), _f("value", text, offset + 3, offset + 5)]
            return f"FC{fn} request", f"FC{fn} {name} [{address}] ← {text}", fields, info
        if fn in (15, 16) and len(pdu) >= 6:
            address, count, size = struct.unpack_from(">HHB", pdu, 1)
            info.update(address=address, count=count)
            fields += [_f("address", address, offset + 1, offset + 3), _f("quantity", count, offset + 3, offset + 5),
                       _f("byte count", size, offset + 5)]
            if fn == 16:
                regs = [_f(f"[{address + i}]", _register_text(unit_cfg, address + i, v), offset + 6 + 2 * i, offset + 8 + 2 * i)
                        for i, (v,) in enumerate(struct.iter_unpack(">H", pdu[6:6 + 2 * (len(pdu[6:]) // 2)]))]
                fields.append(_f("values", f"{len(regs)} registers", offset + 6, offset + len(pdu), regs))
            return f"FC{fn} request", f"FC{fn} {name} {address}..{address + count - 1}", fields, info
        fields.append(_f("data", pdu[1:].hex(" "), offset + 1, offset + len(pdu)))
        return f"FC{fn} request", f"FC{fn} {name}", fields, info
    if fn in (3, 4) and len(pdu) >= 2:
        size = pdu[1]
        regs = []
        for i in range(min(size, len(pdu) - 2) // 2):
            (value,) = struct.unpack_from(">H", pdu, 2 + 2 * i)
            regs.append(_f(f"[{base + i}]", _register_text(unit_cfg, base + i, value), offset + 2 + 2 * i, offset + 4 + 2 * i))
        fields += [_f("byte count", size, offset + 1), _f("registers", f"{len(regs)}", offset + 2, offset + len(pdu), regs)]
        return f"FC{fn} response", "FC{} → {}".format(fn, ", ".join(str(r["value"]) for r in regs[:4]) + (" …" if len(regs) > 4 else "")), fields, {}
    if fn in (1, 2) and len(pdu) >= 2:
        fields += [_f("byte count", pdu[1], offset + 1), _f("status", pdu[2:].hex(" "), offset + 2, offset + len(pdu))]
        return f"FC{fn} response", f"FC{fn} → {pdu[2:].hex(' ')}", fields, {}
    if fn in (5, 6, 15, 16) and len(pdu) >= 5:
        address, value = struct.unpack_from(">HH", pdu, 1)
        fields += [_f("address", address, offset + 1, offset + 3), _f("value" if fn in (5, 6) else "quantity", value, offset + 3, offset + 5)]
        return f"FC{fn} response", f"FC{fn} ok [{address}]", fields, {}
    fields.append(_f("data", pdu[1:].hex(" "), offset + 1, offset + len(pdu)))
    return f"FC{fn} response", f"FC{fn} {name}", fields, {}


def _looks_like_response(frame: bytes, request: Mapping[str, Any]) -> bool:
    fn = frame[1]
    if fn & 0x80:
        return True
    if fn in (1, 2, 3, 4):
        return len(frame) == 5 + frame[2]
    return True


def decode_rtu(data: bytes, config: Mapping[str, Any] | None, context: dict[str, Any], src: str | None = None) -> dict[str, Any]:
    """A Modbus RTU frame; `context` holds the pending request of the bus."""
    config = config or {}
    units = {int(k): v for k, v in (config.get("units") or {}).items()}
    if len(data) < 4:
        context.pop("request", None)
        return {"type": "runt", "summary": f"{len(data)} bytes: too short for Modbus RTU ({data.hex(' ')})",
                "fields": [_f("data", data.hex(" "), 0, len(data))], "ok": False, "unit": data[0] if data else None}
    crc_ok = crc16(data[:-2]) == data[-2:]
    unit, fn = data[0], data[1]
    unit_cfg = units.get(unit) or {}
    pending = context.get("request")
    response = bool(pending and pending["unit"] == unit and pending["fn"] == fn & 0x7F
                    and (src is None or pending.get("src") != src) and _looks_like_response(data, pending))
    kind, summary, fields, info = decode_pdu(data[1:-2], 1, response, pending if response else None, unit_cfg)
    name = unit_cfg.get("name") or f"unit {unit}"
    fields = [_f("unit", f"{unit} {name if unit_cfg.get('name') else ''}".strip(), 0), *fields,
              _f("CRC", f"{data[-2:].hex(' ')} ({'ok' if crc_ok else f'BAD, computed {crc16(data[:-2]).hex(" ")}'})",
                 len(data) - 2, len(data))]
    if response:
        context.pop("request", None)
    elif crc_ok and unit != 0:
        context["request"] = {"unit": unit, "fn": fn, "src": src, **info}
    return {"type": kind if crc_ok else f"{kind} (CRC error)", "summary": f"{name}: {summary}" + ("" if crc_ok else " — CRC error"),
            "fields": fields, "ok": crc_ok, "unit": unit, "response": response, "name": name}


class ModbusMaster:
    """A PLC polling a drive over RS-485 (Modbus RTU): reads holding registers 0..3, writes the set-point
    to register 10 every 5th cycle; counts timeouts, CRC errors and exceptions."""

    inputs = ("setpoint",)
    outputs = ("temperature", "pressure", "good", "timeouts", "crc_errors", "exceptions")
    states = ()
    event_inputs = ("rx",)
    event_outputs = ("tx",)

    def __init__(self, unit: int = 1, period_ms: float = 20.0, baud: float = 19200, fmt: str = "8E1",
                 timeout_ms: float = 15.0, name: str = "plc") -> None:
        self.unit, self.period, self.baud, self.fmt = int(unit), period_ms / 1000.0, float(baud), str(fmt)
        self.timeout, self.name = timeout_ms / 1000.0, name
        self.next = 0.005
        self.cycle = 0
        self.pending: dict[str, Any] | None = None
        self.temperature = self.pressure = 0.0
        self.good = self.timeouts = self.crc_errors = self.exceptions = 0
        self.setpoint = 0.0
        self._out: list[tuple[float, str, Any]] = []

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t, x, u):
        return []

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, Mapping) or payload.get("proto") != "rs485" or self.pending is None:
            return
        data = bytes.fromhex(str(payload.get("data", "")))
        if len(data) < 4 or crc16(data[:-2]) != data[-2:]:
            self.crc_errors += 1                       # a real master drops it and waits for the timeout
            return
        if data[0] != self.unit:
            return
        fn = data[1]
        if fn & 0x80:
            self.exceptions += 1
        elif fn == 3 and len(data) >= 3 + 8:
            regs = struct.unpack_from(">4h", data, 3)
            self.temperature, self.pressure = regs[0] / 10.0, regs[1] / 100.0
            self.good += 1
        elif fn == 6:
            self.good += 1
        self.pending = None

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        self.setpoint = float(u.get("setpoint", self.setpoint))
        if self.pending is not None and t1 > self.pending["deadline"]:
            self.timeouts += 1
            self.pending = None
        while self.next <= t1:
            at = self.next
            self.next += self.period
            if self.pending is not None:
                continue
            self.cycle += 1
            if self.cycle % 5 == 0:
                pdu = struct.pack(">BHH", 6, 10, int(round(self.setpoint * 10)) & 0xFFFF)
            else:
                pdu = struct.pack(">BHH", 3, 0, 4)
            frame = rtu(self.unit, pdu)
            self._out.append((at, "tx", {"proto": "rs485", "data": frame.hex(), "baud": self.baud, "format": self.fmt,
                                         "src": self.name}))
            self.pending = {"deadline": at + len(frame) * char_time(self.baud, self.fmt) + self.timeout}

    def output(self, t, x, u):
        return {"temperature": self.temperature, "pressure": self.pressure, "good": float(self.good),
                "timeouts": float(self.timeouts), "crc_errors": float(self.crc_errors), "exceptions": float(self.exceptions)}

    def pop_events(self) -> list[tuple[float, str, Any]]:
        out, self._out = self._out, []
        return out


class ModbusSlave:
    """A drive on RS-485: registers 0 temperature (0.1 degC), 1 pressure (0.01 bar), 2 status, 3 counter,
    10 set-point (writable); other addresses answer exception 2. Answers after `response_us`."""

    inputs = ("temperature", "pressure")
    outputs = ("setpoint", "requests", "rx_crc_errors")
    states = ()
    event_inputs = ("rx",)
    event_outputs = ("tx",)

    def __init__(self, unit: int = 1, baud: float = 19200, fmt: str = "8E1", response_us: float = 2500.0,
                 name: str = "drive") -> None:
        self.unit, self.baud, self.fmt, self.name = int(unit), float(baud), str(fmt), name
        self.response = response_us * 1e-6
        self.registers = {0: 0, 1: 0, 2: 0x0001, 3: 0, 10: 0}
        self.requests = self.crc_errors = 0
        self.u: Mapping[str, float] = {}
        self._out: list[tuple[float, str, Any]] = []

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t, x, u):
        return []

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, Mapping) or payload.get("proto") != "rs485":
            return
        data = bytes.fromhex(str(payload.get("data", "")))
        if len(data) < 4 or crc16(data[:-2]) != data[-2:]:
            self.crc_errors += 1                       # silently ignored, as the standard says
            return
        if data[0] != self.unit:
            return
        self.requests += 1
        self.registers[3] = (self.registers[3] + 1) & 0xFFFF
        fn = data[1]
        reply: bytes
        if fn == 3 and len(data) == 8:
            address, count = struct.unpack_from(">HH", data, 2)
            if all(a in self.registers for a in range(address, address + count)) and 1 <= count <= 125:
                values = b"".join(struct.pack(">H", self.registers[a] & 0xFFFF) for a in range(address, address + count))
                reply = bytes([3, len(values)]) + values
            else:
                reply = bytes([0x83, 2])
        elif fn == 6 and len(data) == 8:
            address, value = struct.unpack_from(">HH", data, 2)
            if address == 10:
                self.registers[10] = value
                reply = data[1:6]
            else:
                reply = bytes([0x86, 2])
        else:
            reply = bytes([fn | 0x80, 1])
        frame = rtu(self.unit, reply)
        at = t + len(data) * char_time(self.baud, self.fmt) + self.response
        self._out.append((at, "tx", {"proto": "rs485", "data": frame.hex(), "baud": self.baud, "format": self.fmt,
                                     "src": self.name}))

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        self.registers[0] = int(round(float(u.get("temperature", 25.0)) * 10)) & 0xFFFF
        self.registers[1] = int(round(float(u.get("pressure", 1.0)) * 100)) & 0xFFFF

    def output(self, t, x, u):
        value = self.registers[10]
        return {"setpoint": (value - 65536 if value >= 32768 else value) / 10.0, "requests": float(self.requests),
                "rx_crc_errors": float(self.crc_errors)}

    def pop_events(self) -> list[tuple[float, str, Any]]:
        out, self._out = self._out, []
        return out
