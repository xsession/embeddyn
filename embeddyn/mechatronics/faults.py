"""Fault injection: corrupt what a signal's consumers see during a time window.

Declared in a project's `faults:` list (and in verification scenarios):

```yaml
faults:
  - name: feedback-pot-stuck      # optional; also the event topic fault.<name>
    signal: servo.feedback_voltage
    type: stuck                   # stuck | offset | gain | noise | drift | dropout
    start: 1.2 s                  # default 0
    end: 1.8 s                    # default: until the end of the run
    value: 0.4 V                  # stuck only: stuck at this value instead of the last one
  - {signal: mcu.servo_pulse, type: dropout, probability: 0.2, seed: 7, start: 0.5 s}
  - {signal: sensor.temp, type: noise, std: 0.5 degC, seed: 1}
  - {signal: encoder.count, type: offset, offset: 12, start: 1 s, period: 0.1 s, duty: 0.3}   # intermittent
```

| type | effect while active | fields |
|---|---|---|
| stuck | holds the value the signal had when the fault began, or `value` | `value` |
| offset | true value + `offset` | `offset` |
| gain | true value * `gain` | `gain` |
| noise | true value + Gaussian noise | `std`, `seed` |
| drift | true value + `rate` * (t - start) | `rate` (signal unit per second) |
| dropout | each update is lost with `probability`: the previous value stays | `probability`, `seed` |

`period` and `duty` make any fault intermittent: active for the first
`duty * period` of every `period` after `start`. Values with units are
converted to the signal's unit. Several faults on one signal apply in the
order they are listed.

Frame faults apply to `event` and `bus` signals that carry bus traffic (a
Renode UART's or CAN bus's signal): each frame (a UART byte, a CAN frame)
that crosses the boundary while the fault is active is lost (`dropout`) or
has bits flipped (`corrupt`), with `probability`:

```yaml
  - {signal: mcu.can, type: dropout, probability: 0.5, id: 0x101, start: 1 s}
  - {signal: mcu.console, type: corrupt, bits: 1, direction: output, seed: 4}
```

`direction` is `output` (frames the firmware sends), `input` (frames sent to
the firmware) or `both` (default); `id` limits a CAN fault to one frame id or
a list of them.

The faulted value is what every consumer sees and what is recorded: adapter
inputs, couplings, telemetry. The orchestrator keeps the true value apart and
re-applies the faults each macro step, so a fault also reaches signals that
nothing writes (constants, initial values). Each fault publishes a
`fault.<name>` event with `state: start` / `end` when it switches.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .quantities import Quantity

FAULT_TYPES = ("stuck", "offset", "gain", "noise", "drift", "dropout", "corrupt")
VALUE_TYPES = ("stuck", "offset", "gain", "noise", "drift", "dropout")
FRAME_TYPES = ("dropout", "corrupt")
DIRECTIONS = ("output", "input", "both")
_EPS = 1e-12


@dataclass(frozen=True)
class Fault:
    name: str
    signal: str
    type: str
    start: float = 0.0
    end: float | None = None
    value: float | None = None       # stuck at
    offset: float = 0.0
    gain: float = 1.0
    std: float = 0.0
    rate: float = 0.0
    probability: float = 1.0
    seed: int = 0
    period: float | None = None
    duty: float = 0.5
    frames: bool = False             # on bus traffic (UART bytes, CAN frames) instead of a value
    direction: str = "both"          # frames: output (from the firmware) | input (to it) | both
    ids: tuple[int, ...] | None = None   # frames: CAN ids
    bits: int = 1                    # corrupt: bits flipped per frame

    def active(self, time: float) -> bool:
        if time + _EPS < self.start or (self.end is not None and time + _EPS >= self.end):
            return False
        if self.period is None:
            return True
        phase = math.fmod(time - self.start + _EPS, self.period)
        return phase < self.duty * self.period

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "signal": self.signal, "type": self.type, "start": self.start}
        if self.end is not None:
            out["end"] = self.end
        extra = {"stuck": {"value": self.value}, "offset": {"offset": self.offset}, "gain": {"gain": self.gain},
                 "noise": {"std": self.std, "seed": self.seed}, "drift": {"rate": self.rate},
                 "dropout": {"probability": self.probability, "seed": self.seed},
                 "corrupt": {"probability": self.probability, "bits": self.bits, "seed": self.seed}}[self.type]
        out.update({key: value for key, value in extra.items() if value is not None})
        if self.frames:
            out["direction"] = self.direction
            if self.ids is not None:
                out["id"] = list(self.ids)
        if self.period is not None:
            out.update(period=self.period, duty=self.duty)
        return out


def parse_faults(items: Iterable[Mapping[str, Any]] | None, signals: Mapping[str, Any],
                 *, start_index: int = 0) -> list[Fault]:
    """Faults from project-file entries; `signals` maps names to `Signal`s (for units and checks)."""
    faults = []
    for index, item in enumerate(items or [], start=start_index):
        if not isinstance(item, Mapping):
            raise ValueError(f"fault {index + 1}: expected a mapping, got {type(item).__name__}")
        name = str(item.get("name") or f"fault{index + 1}")
        signal_name = str(item.get("signal") or "")
        where = f"fault {name}"
        if signal_name not in signals:
            raise ValueError(f"{where}: unknown signal {signal_name or '(none)'}")
        signal = signals[signal_name]
        frames = getattr(signal, "kind", None) is not None and signal.kind.value in {"event", "bus"}
        kind = str(item.get("type") or "")
        if kind not in FAULT_TYPES:
            raise ValueError(f"{where}: unknown type {kind or '(none)'} (use {', '.join(FAULT_TYPES)})")
        allowed = FRAME_TYPES if frames else VALUE_TYPES
        if kind not in allowed:
            what = f"{signal.kind.value} signal" if frames else "continuous or discrete signal"
            raise ValueError(f"{where}: a {kind} fault does not apply to {signal_name}, a {what} "
                             f"(use {', '.join(allowed)})")
        frame_keys = sorted({"direction", "id", "bits"} & set(item))
        if frame_keys and not frames:
            raise ValueError(f"{where}: {', '.join(frame_keys)} only apply to faults on event or bus signals")
        unit = "" if frames else (signal.unit or "")

        def number(key: str, default: Any, unit: str = unit) -> float | None:
            raw = item.get(key, default)
            if raw is None:
                return None
            try:
                return Quantity.parse(raw, unit).value
            except (TypeError, ValueError) as error:
                raise ValueError(f"{where}: invalid {key}: {error}") from error

        rate_unit = f"{unit}/s" if unit else "Hz"
        fault = Fault(
            name=name, signal=signal_name, type=kind,
            start=number("start", 0.0, "s"), end=number("end", None, "s"),
            value=number("value", None), offset=number("offset", 0.0), gain=number("gain", 1.0, ""),
            std=number("std", 0.0), rate=number("rate", 0.0, rate_unit),
            probability=number("probability", 1.0, ""), seed=int(item.get("seed", index)),
            period=number("period", None, "s"), duty=number("duty", 0.5, ""),
            frames=frames, direction=str(item.get("direction", "both")), ids=_ids(item.get("id"), where),
            bits=_integer(item.get("bits", 1), "bits", where),
        )
        _check(fault, item, where)
        faults.append(fault)
    names = [fault.name for fault in faults]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate fault name: {', '.join(duplicates)}")
    return faults


def _integer(raw: Any, key: str, where: str) -> int:
    try:
        return int(raw, 0) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{where}: invalid {key}: {raw!r}") from error


def _ids(raw: Any, where: str) -> tuple[int, ...] | None:
    if raw is None:
        return None
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    return tuple(_integer(item, "id", where) for item in items)


def _check(fault: Fault, item: Mapping[str, Any], where: str) -> None:
    required = {"offset": "offset", "gain": "gain", "noise": "std", "drift": "rate"}.get(fault.type)
    if required and required not in item:
        raise ValueError(f"{where}: a {fault.type} fault needs {required}")
    numbers = [fault.start, fault.offset, fault.gain, fault.std, fault.rate, fault.probability, fault.duty]
    numbers += [x for x in (fault.end, fault.value, fault.period) if x is not None]
    if not all(math.isfinite(x) for x in numbers):
        raise ValueError(f"{where}: values must be finite")
    if fault.start < 0 or (fault.end is not None and fault.end <= fault.start):
        raise ValueError(f"{where}: needs 0 <= start < end")
    if fault.std < 0:
        raise ValueError(f"{where}: std must not be negative")
    if not 0.0 <= fault.probability <= 1.0:
        raise ValueError(f"{where}: probability must be within 0..1")
    if fault.period is not None and (fault.period <= 0 or not 0.0 < fault.duty <= 1.0):
        raise ValueError(f"{where}: needs period > 0 and 0 < duty <= 1")
    if fault.direction not in DIRECTIONS:
        raise ValueError(f"{where}: direction must be one of {', '.join(DIRECTIONS)}")
    if fault.bits < 1:
        raise ValueError(f"{where}: bits must be at least 1")


class FaultInjector:
    """Run-time state of a project's faults (held values, random streams, switch state)."""

    def __init__(self, faults: Iterable[Fault]) -> None:
        self.faults = list(faults)
        self.by_signal: dict[str, list[int]] = {}        # value faults
        self.frames_by_signal: dict[str, list[int]] = {}  # frame faults
        for index, fault in enumerate(self.faults):
            (self.frames_by_signal if fault.frames else self.by_signal).setdefault(fault.signal, []).append(index)
        self._rngs = [random.Random(fault.seed) for fault in self.faults]
        self._held: dict[int, float] = {}
        self._was_active = [False] * len(self.faults)
        self.frame_counts: dict[str, dict[str, int]] = {
            fault.name: {"dropped": 0, "corrupted": 0} for fault in self.faults if fault.frames}

    def __bool__(self) -> bool:
        return bool(self.faults)

    def signals(self) -> list[str]:
        """Signals with value faults (frame faults are applied by the adapters that carry the traffic)."""
        return list(self.by_signal)

    def frame_signals(self) -> list[str]:
        return list(self.frames_by_signal)

    def apply(self, signal: str, true_value: float, previous: float, time: float) -> float:
        """The value consumers see: `true_value` with the signal's active faults applied in order;
        `previous` is what they saw until now (stuck and dropout keep it)."""
        value = float(true_value)
        for index in self.by_signal.get(signal, ()):
            fault = self.faults[index]
            if not fault.active(time):
                self._held.pop(index, None)
                continue
            if fault.type == "stuck":
                if fault.value is not None:
                    value = fault.value
                else:
                    value = self._held.setdefault(index, float(previous))
            elif fault.type == "offset":
                value += fault.offset
            elif fault.type == "gain":
                value *= fault.gain
            elif fault.type == "noise":
                value += self._rngs[index].gauss(0.0, fault.std)
            elif fault.type == "drift":
                value += fault.rate * (time - fault.start)
            elif fault.type == "dropout":
                if self._rngs[index].random() < fault.probability:
                    value = float(previous)
        return value

    def frame(self, signal: str, data: bytes, time: float, *, direction: str,
              frame_id: int | None = None) -> bytes | None:
        """One frame (a UART byte, a CAN frame's data) crossing the boundary at `time`:
        the bytes that arrive, or None if the frame is lost. `direction` is "output"
        (sent by the firmware) or "input" (sent to it)."""
        data = bytes(data)
        for index in self.frames_by_signal.get(signal, ()):
            fault = self.faults[index]
            if fault.direction not in {"both", direction} or not fault.active(time):
                continue
            if fault.ids is not None and frame_id not in fault.ids:
                continue
            rng = self._rngs[index]
            if rng.random() >= fault.probability:
                continue
            counts = self.frame_counts[fault.name]
            if fault.type == "dropout":
                counts["dropped"] += 1
                return None
            if data:                                   # corrupt: flip distinct bits
                flipped = bytearray(data)
                for bit in rng.sample(range(len(data) * 8), min(fault.bits, len(data) * 8)):
                    flipped[bit // 8] ^= 1 << (bit % 8)
                data = bytes(flipped)
                counts["corrupted"] += 1
        return data

    def transitions(self, time: float) -> list[tuple[Fault, str]]:
        """Faults that switched on or off since the last call, with "start" / "end"."""
        changed = []
        for index, fault in enumerate(self.faults):
            now = fault.active(time)
            if now != self._was_active[index]:
                changed.append((fault, "start" if now else "end"))
                self._was_active[index] = now
        return changed

    def snapshot(self) -> dict[str, Any]:
        return {"held": {str(k): v for k, v in self._held.items()}, "active": list(self._was_active),
                "rngs": [_state_to_json(rng.getstate()) for rng in self._rngs],
                "frames": {name: dict(counts) for name, counts in self.frame_counts.items()}}

    def restore(self, state: Mapping[str, Any]) -> None:
        if not state:
            return
        self._held = {int(k): float(v) for k, v in dict(state.get("held", {})).items()}
        active = list(state.get("active", []))
        if len(active) == len(self.faults):
            self._was_active = [bool(x) for x in active]
        for rng, saved in zip(self._rngs, state.get("rngs", [])):
            rng.setstate(_state_from_json(saved))
        for name, counts in dict(state.get("frames", {})).items():
            if name in self.frame_counts:
                self.frame_counts[name] = {key: int(value) for key, value in dict(counts).items()}


def _state_to_json(state: Any) -> Any:
    return [_state_to_json(x) for x in state] if isinstance(state, tuple) else state


def _state_from_json(state: Any) -> Any:
    return tuple(_state_from_json(x) for x in state) if isinstance(state, list) else state
