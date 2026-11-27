"""Classic CAN: frame bit lengths (with stuff bits and CRC-15) and a bus that arbitrates.

`CanBus` is a python plant. Boards publish frames they want to send on the bus's
`tx` event port; the bus serialises them at the bit rate, lowest identifier first
when several wait (non-destructive arbitration), and publishes each frame on its
`wire` port at the moment its last bit is on the bus. Every board, and the
analyzer, listens to the wire. The `load` output is the share of the last step
the bus was busy, `load_avg` its average over `average_ms`.

Frame payloads are dicts: ``{"proto": "can", "id": 0x181, "data": "0102", "src": "axis"}``
(``"ext": true`` for 29-bit identifiers, ``"rtr": true`` for remote frames). On the
wire the bus adds ``"start"`` (first bit, s), ``"bits"`` and ``"seq"``.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence


def crc15(bits: Sequence[int]) -> int:
    crc = 0
    for bit in bits:
        top = ((crc >> 14) & 1) ^ bit
        crc = (crc << 1) & 0x7FFF
        if top:
            crc ^= 0x4599
    return crc


def _bits(value: int, count: int) -> list[int]:
    return [(value >> (count - 1 - i)) & 1 for i in range(count)]


def frame_bits(frame_id: int, data: bytes, extended: bool = False, rtr: bool = False) -> int:
    """Bits a data or remote frame occupies on the bus, stuff bits and the 3-bit intermission included."""
    dlc = min(len(data), 8)
    if extended:
        head = [0, *_bits(frame_id >> 18, 11), 1, 1, *_bits(frame_id & 0x3FFFF, 18), int(rtr), 0, 0]
    else:
        head = [0, *_bits(frame_id, 11), int(rtr), 0, 0]
    stuffed_part = head + _bits(dlc, 4) + ([] if rtr else [b for byte in data[:8] for b in _bits(byte, 8)])
    stuffed_part += _bits(crc15(stuffed_part), 15)
    stuff, run, last = 0, 0, None
    for bit in stuffed_part:                   # a complementary bit after five equal ones
        if bit == last:
            run += 1
        else:
            run, last = 1, bit
        if run == 5:
            stuff += 1
            run, last = 1, 1 - bit
    return len(stuffed_part) + stuff + 1 + 2 + 7 + 3   # CRC delimiter, ACK slot + delimiter, EOF, IFS


class CanBus:
    """A CAN segment shared by every board subscribed to it (python plant)."""

    inputs = ()
    outputs = ("load", "load_avg", "frames")
    states = ()
    event_inputs = ("tx",)
    event_outputs = ("wire",)

    def __init__(self, bitrate: float = 500_000.0, average_ms: float = 100.0) -> None:
        if bitrate <= 0:
            raise ValueError("bitrate must be positive")
        self.bitrate = float(bitrate)
        self.pending: list[tuple[float, int, dict[str, Any]]] = []   # (ready time, order, frame)
        self.free_at = 0.0
        self.busy = 0.0
        self.count = 0
        self.load = 0.0
        self.load_avg = 0.0
        self.average = float(average_ms) / 1000.0
        self._order = 0
        self._out: list[tuple[float, str, Any]] = []

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        return []

    def receive(self, t: float, port: str, payload: Any) -> None:
        if port == "tx" and isinstance(payload, dict) and "id" in payload:
            self.pending.append((float(t), self._order, dict(payload)))
            self._order += 1

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        busy = max(0.0, min(self.free_at, t1) - t0)          # a frame still on the bus from the last step
        while self.pending:
            start = max(self.free_at, min(ready for ready, _, _ in self.pending))
            if start >= t1:
                break
            # Arbitration: of the frames waiting when the bus goes idle, the lowest identifier wins
            # (an 11-bit id compares as id << 18 against 29-bit ids, dominant IDE for base frames).
            ready = [item for item in self.pending if item[0] <= start + 1e-12]
            winner = min(ready, key=lambda item: (_arbitration_key(item[2]), item[1]))
            frame = winner[2]
            data = bytes.fromhex(str(frame.get("data", "")))
            bits = frame_bits(int(frame["id"]), data, bool(frame.get("ext")), bool(frame.get("rtr")))
            end = start + bits / self.bitrate
            self.pending.remove(winner)
            self.free_at = end
            busy += min(end, t1) - max(start, t0)
            self.count += 1
            self._out.append((end, "wire", {**frame, "proto": "can", "start": start, "bits": bits,
                                            "seq": self.count}))
        span = t1 - t0
        self.load = min(1.0, busy / span) if span > 0 else 0.0
        if span > 0:                                 # first-order average over `average_ms`
            k = 1.0 - pow(2.718281828459045, -span / self.average) if self.average > 0 else 1.0
            self.load_avg += k * (self.load - self.load_avg)

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        return {"load": self.load, "load_avg": self.load_avg, "frames": float(self.count)}

    def pop_events(self) -> list[tuple[float, str, Any]]:
        out, self._out = self._out, []
        return out


def _arbitration_key(frame: Mapping[str, Any]) -> tuple[int, int, int]:
    frame_id = int(frame["id"])
    if frame.get("ext"):
        return (frame_id >> 18, 1, frame_id & 0x3FFFF)
    return (frame_id, 0, int(bool(frame.get("rtr"))))


class CanRestbus:
    """Restbus simulation: the frames of nodes that are not there, on a schedule (python plant).

    ``frames`` is a list of ``{id, data, period_ms, start, count, counter}``: `data` hex, sent every
    `period_ms` from `start` seconds (`period_ms` 0: once), `count` times (0: forever); `counter`
    names a byte whose low nibble counts up (a rolling message counter). Frames it hears on
    `rx` are counted per identifier in `heard` (the last one heard is the `last_id` output).
    """

    inputs = ()
    outputs = ("sent", "heard", "last_id")
    states = ()
    event_inputs = ("rx",)
    event_outputs = ("tx",)

    def __init__(self, frames: Sequence[Mapping[str, Any]] = (), name: str = "restbus") -> None:
        self.name = name
        self.frames = []
        for spec in frames:
            frame_id = int(str(spec["id"]), 0) if isinstance(spec["id"], str) else int(spec["id"])
            self.frames.append({"id": frame_id, "data": bytearray(bytes.fromhex(str(spec.get("data", "")))),
                                "period": float(spec.get("period_ms", 0.0)) / 1000.0, "next": float(spec.get("start", 0.0)),
                                "left": int(spec.get("count", 0)) or None, "counter": spec.get("counter")})
        self.sent = 0
        self.heard: dict[int, int] = {}
        self.last_id = 0
        self._out: list[tuple[float, str, Any]] = []

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        return []

    def receive(self, t: float, port: str, payload: Any) -> None:
        if isinstance(payload, dict) and "id" in payload and payload.get("src") != self.name:
            self.heard[int(payload["id"])] = self.heard.get(int(payload["id"]), 0) + 1
            self.last_id = int(payload["id"])

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        for frame in self.frames:
            while frame["next"] is not None and frame["next"] <= t1:
                if frame["counter"] is not None:
                    index = int(frame["counter"])
                    frame["data"][index] = (frame["data"][index] & 0xF0) | ((frame["data"][index] + 1) & 0x0F)
                payload = {"proto": "can", "id": frame["id"], "data": bytes(frame["data"]).hex(), "src": self.name}
                if frame["id"] > 0x7FF:
                    payload["ext"] = True
                self._out.append((frame["next"], "tx", payload))
                self.sent += 1
                if frame["left"] is not None:
                    frame["left"] -= 1
                    if frame["left"] <= 0:
                        frame["next"] = None
                        break
                frame["next"] = frame["next"] + frame["period"] if frame["period"] > 0 else None

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        return {"sent": float(self.sent), "heard": float(sum(self.heard.values())), "last_id": float(self.last_id)}

    def pop_events(self) -> list[tuple[float, str, Any]]:
        out, self._out = self._out, []
        return out
