"""Co-simulation with ngspice through its shared library (libngspice).

One transient analysis runs for the whole co-simulation, so the circuit state
(inductor currents, capacitor voltages, switch states) carries across steps.
Each step:

1. input values are held constant for the step (zero-order hold) and handed to
   ngspice through `external` voltage/current sources (GetVSRCData/GetISRCData)
   or applied with `alter` when a device parameter changed;
2. a `stop when time >= t` breakpoint is set and the analysis is run/resumed;
   the GetSyncData callback shortens the last internal timestep so the
   analysis lands exactly on the co-simulation time;
3. outputs are read from the result vectors over the step window, as the last
   value or as the time-weighted mean / RMS / min / max (PWM ripple averages
   out instead of aliasing).

libngspice holds global state, so only one instance can run per process. Use
`--isolate` (process-isolated adapters) to run several at once.

ngspice keeps every saved vector for the whole run. With `save: bound` (the
default) only the vectors the output bindings read are saved, so memory grows
with the bound outputs, not with the size of the circuit; `save: all` keeps
ngspice's default (every node and branch). libngspice ignores `.save` cards
outside batch mode, and `save` entries share the debug list with `stop`
breakpoints, so the adapter issues the `save` command after clearing that
list each step.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import math
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .base import SimulatorAdapter, StepResult

OUTPUT_MODES = ("last", "mean", "rms", "min", "max")
SAVE_MODES = ("bound", "all")
INPUT_KINDS = ("source", "current_source", "alter")

_SendChar = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
_SendStat = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
_ControlledExit = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_bool, ctypes.c_bool, ctypes.c_int,
                                   ctypes.c_void_p)
_SendData = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
_SendInitData = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
_BGThreadRunning = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_bool, ctypes.c_int, ctypes.c_void_p)
_GetSourceData = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_double), ctypes.c_double,
                                  ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
_GetSyncData = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_double, ctypes.POINTER(ctypes.c_double), ctypes.c_double,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)


class _VectorInfo(ctypes.Structure):
    _fields_ = [
        ("v_name", ctypes.c_char_p),
        ("v_type", ctypes.c_int),
        ("v_flags", ctypes.c_short),
        ("v_realdata", ctypes.POINTER(ctypes.c_double)),
        ("v_compdata", ctypes.c_void_p),
        ("v_length", ctypes.c_int),
    ]


class NgspiceError(RuntimeError):
    pass


def find_library(explicit: str | os.PathLike[str] | None = None) -> str:
    """Locate libngspice: explicit path, $NGSPICE_LIBRARY, then the system search path."""
    for candidate in (explicit, os.environ.get("NGSPICE_LIBRARY")):
        if candidate:
            if not Path(candidate).exists():
                raise NgspiceError(f"libngspice not found at {candidate}")
            return str(candidate)
    found = ctypes.util.find_library("ngspice")
    if not found:
        raise NgspiceError("libngspice not found: install libngspice0 (Debian/Ubuntu) or set NGSPICE_LIBRARY")
    return found


class _Ngspice:
    """Process-wide libngspice binding. ngspice keeps global state, so there is
    one per process and at most one adapter may own it at a time."""

    _instance: "_Ngspice | None" = None
    _guard = threading.Lock()

    def __init__(self, path: str) -> None:
        self.path = path
        self.lib = ctypes.CDLL(path)
        self.lib.ngSpice_Command.argtypes = [ctypes.c_char_p]
        self.lib.ngSpice_Circ.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
        self.lib.ngGet_Vec_Info.argtypes = [ctypes.c_char_p]
        self.lib.ngGet_Vec_Info.restype = ctypes.POINTER(_VectorInfo)
        self.lib.ngSpice_CurPlot.restype = ctypes.c_char_p
        self.lib.ngSpice_AllVecs.argtypes = [ctypes.c_char_p]
        self.lib.ngSpice_AllVecs.restype = ctypes.POINTER(ctypes.c_char_p)
        self.owner: object | None = None
        self.output: list[str] = []
        self.exit_status: int | None = None
        # Callbacks must stay referenced for the life of the library.
        self._callbacks = [
            _SendChar(self._on_char), _SendStat(lambda *_: 0), _ControlledExit(self._on_exit),
            _SendData(lambda *_: 0), _SendInitData(lambda *_: 0), _BGThreadRunning(lambda *_: 0),
        ]
        self._sync_callbacks = [_GetSourceData(self._on_vsrc), _GetSourceData(self._on_isrc),
                                _GetSyncData(self._on_sync)]
        if self.lib.ngSpice_Init(*self._callbacks, None) != 0:
            raise NgspiceError("ngSpice_Init failed")
        ident = ctypes.c_int(0)
        if self.lib.ngSpice_Init_Sync(*self._sync_callbacks, ctypes.byref(ident), None) != 0:
            raise NgspiceError("ngSpice_Init_Sync failed")

    @classmethod
    def acquire(cls, path: str, owner: object) -> "_Ngspice":
        with cls._guard:
            if cls._instance is None:
                cls._instance = cls(path)
            elif os.path.realpath(cls._instance.path) != os.path.realpath(path):
                raise NgspiceError(f"libngspice already loaded from {cls._instance.path}; one library per process")
            if cls._instance.owner is not None and cls._instance.owner is not owner:
                raise NgspiceError("libngspice is already used by another adapter in this process; "
                                   "run with process isolation (--isolate) to use several")
            cls._instance.owner = owner
            return cls._instance

    def release(self, owner: object) -> None:
        with self._guard:
            if self.owner is owner:
                self.owner = None

    # -- callbacks ---------------------------------------------------------
    def _on_char(self, text: bytes, _ident: int, _user: Any) -> int:
        line = text.decode(errors="replace")
        self.output.append(line[7:] if line.startswith(("stdout ", "stderr ")) else line)
        del self.output[:-200]
        return 0

    def _on_exit(self, status: int, _unload: bool, _quit: bool, _ident: int, _user: Any) -> int:
        self.exit_status = status
        return 0

    def _on_vsrc(self, value, time, name, _ident, _user) -> int:
        owner = self.owner
        value[0] = owner._source_value(name.decode().lower(), time) if owner else 0.0
        return 0

    def _on_isrc(self, value, time, name, _ident, _user) -> int:
        return self._on_vsrc(value, time, name, _ident, _user)

    def _on_sync(self, time, delta, _old_delta, _redo, _ident, location, _user) -> int:
        owner = self.owner
        if owner is not None and location == 0:
            remaining = owner._stop_time - time
            if remaining > owner._time_epsilon and time + delta[0] > owner._stop_time:
                delta[0] = remaining
        return 0

    # -- commands ----------------------------------------------------------
    def command(self, text: str) -> None:
        mark = len(self.output)
        if self.lib.ngSpice_Command(text.encode()) != 0 or self.exit_status is not None:
            raise NgspiceError(f"ngspice command failed: {text}\n" + "\n".join(self.output[mark:][-20:]))

    def load(self, lines: list[str]) -> None:
        self.exit_status = None
        mark = len(self.output)
        array = (ctypes.c_char_p * (len(lines) + 1))(*[line.encode() for line in lines], None)
        if self.lib.ngSpice_Circ(array) != 0 or self.exit_status is not None or any(
                "error" in line.lower() for line in self.output[mark:]):
            raise NgspiceError("ngspice could not load the circuit:\n" + "\n".join(self.output[mark:][-20:]))

    def vector_names(self) -> list[str]:
        """Names of the vectors in the current plot (what ngspice keeps for the run)."""
        names, array = [], self.lib.ngSpice_AllVecs(self.lib.ngSpice_CurPlot())
        index = 0
        while array and array[index]:
            names.append(array[index].decode())
            index += 1
        return names

    def vector(self, name: str, start: int = 0) -> list[float]:
        # ngGet_Vec_Info returns a pointer to one static struct: copy before the next call.
        pointer = self.lib.ngGet_Vec_Info(name.encode())
        if not pointer:
            raise NgspiceError(f"ngspice has no vector {name!r}")
        info = pointer.contents
        if not info.v_realdata:
            raise NgspiceError(f"ngspice vector {name!r} is not real-valued")
        length = info.v_length
        return info.v_realdata[max(0, min(start, length)):length]


@dataclass(frozen=True)
class NgspiceInputBinding:
    signal: str
    kind: str           # source | current_source | alter
    target: str         # source name, or `alter` target such as "Rmotor" or "@Lmotor[inductance]"
    scale: float = 1.0
    offset: float = 0.0
    minimum: float | None = None
    maximum: float | None = None

    def value(self, raw: float) -> float:
        value = raw * self.scale + self.offset
        if self.minimum is not None:
            value = max(self.minimum, value)
        if self.maximum is not None:
            value = min(self.maximum, value)
        return value


@dataclass(frozen=True)
class NgspiceOutputBinding:
    signal: str
    vector: str
    mode: str = "mean"
    scale: float = 1.0
    offset: float = 0.0


@dataclass
class NgspiceBindings:
    inputs: list[NgspiceInputBinding] = field(default_factory=list)
    outputs: list[NgspiceOutputBinding] = field(default_factory=list)


def load_ngspice_bindings(path: str | os.PathLike[str] | None = None,
                          inline: Iterable[Mapping[str, Any]] = ()) -> NgspiceBindings:
    entries: list[Mapping[str, Any]] = []
    if path:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        entries.extend(data.get("bindings", data) if isinstance(data, dict) else data)
    entries.extend(inline)
    bindings = NgspiceBindings()
    for entry in entries:
        entry = dict(entry)
        signal = entry.get("signal")
        if not signal:
            raise ValueError(f"ngspice binding without signal: {entry}")
        direction = str(entry.get("direction", "output" if "vector" in entry else "input"))
        scale, offset = float(entry.get("scale", 1.0)), float(entry.get("offset", 0.0))
        if direction == "output":
            if "vector" not in entry:
                raise ValueError(f"ngspice output binding for {signal} needs 'vector'")
            mode = str(entry.get("mode", "mean"))
            if mode not in OUTPUT_MODES:
                raise ValueError(f"ngspice output {signal}: mode must be one of {', '.join(OUTPUT_MODES)}")
            bindings.outputs.append(NgspiceOutputBinding(signal, str(entry["vector"]), mode, scale, offset))
        elif direction == "input":
            kinds = [kind for kind in INPUT_KINDS if kind in entry]
            if len(kinds) != 1:
                raise ValueError(f"ngspice input binding for {signal} needs exactly one of {', '.join(INPUT_KINDS)}")
            kind = kinds[0]
            minimum, maximum = entry.get("min"), entry.get("max")
            bindings.inputs.append(NgspiceInputBinding(
                signal, kind, str(entry[kind]), scale, offset,
                None if minimum is None else float(minimum), None if maximum is None else float(maximum)))
        else:
            raise ValueError(f"ngspice binding for {signal}: direction must be input or output")
    return bindings


_PROBE = re.compile(r"([VvIi])\(\s*([^,()]+?)\s*(?:,\s*([^,()]+?)\s*)?\)")
_TEMPLATE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")
_DROP_CARDS = (".tran", ".end", ".op", ".ac", ".dc", ".noise", ".tf")


def saved_vectors(outputs: Iterable[NgspiceOutputBinding]) -> list[str]:
    """`.save` names for the vectors the output bindings read: V(a,b) -> a b, I(Vx) -> vx#branch."""
    names: list[str] = []
    for binding in outputs:
        match = _PROBE.fullmatch(binding.vector.strip())
        if not match:
            wanted = [binding.vector.strip()]
        elif match.group(1).lower() == "i":
            wanted = [f"{match.group(2).strip()}#branch"]
        else:
            wanted = [match.group(2).strip()] + ([match.group(3).strip()] if match.group(3) else [])
        names.extend(name.lower() for name in wanted if name.lower() not in names)
    return names


def prepare_netlist(text: str, values: Mapping[str, float], tran: str) -> list[str]:
    """Substitute {{signal}} placeholders with start values, drop analyses and
    .control blocks, and append the co-simulation transient analysis."""
    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise ValueError(f"netlist placeholder {{{{{name}}}}} is not a known signal")
        return format(float(values[name]), ".17g")

    lines, in_control = [], False
    for raw in text.splitlines():
        if not raw.lstrip().startswith("*"):
            raw = _TEMPLATE.sub(substitute, raw)
        card = raw.strip().lower()
        if card.startswith(".control"):
            in_control = True
            continue
        if in_control:
            in_control = not card.startswith(".endc")
            continue
        if card.split(" ", 1)[0] in _DROP_CARDS or card == ".end":
            continue
        lines.append(raw)
    if not lines or not lines[0].strip() or lines[0].lstrip().startswith((".", "+")):
        lines.insert(0, "* embeddyn co-simulation circuit")
    return [*lines, tran, ".end"]


class NgspiceSharedAdapter(SimulatorAdapter):
    """Electrical domain on libngspice with state carried across steps."""

    name = "ngspice-shared"

    def __init__(self, netlist: str | os.PathLike[str], bindings: str | os.PathLike[str] | None = None,
                 inline_bindings: Iterable[Mapping[str, Any]] = (), library: str | None = None,
                 max_step: float | None = None, duration: float | None = None,
                 options: Mapping[str, Any] | None = None, save: str = "bound") -> None:
        self.netlist = Path(netlist)
        self.bindings_path = str(bindings) if bindings else None
        self.inline_bindings = [dict(entry) for entry in inline_bindings]
        self.bindings = load_ngspice_bindings(self.bindings_path, self.inline_bindings)
        self.library = library
        self.max_step = max_step
        self.duration = duration
        self.spice_options = dict(options or {})
        if save not in SAVE_MODES:
            raise ValueError(f"ngspice save must be one of {', '.join(SAVE_MODES)}")
        self.save = save
        self.spice: _Ngspice | None = None
        self._stop_time = 0.0
        self._time_epsilon = 1e-15
        self._time = 0.0
        self._index = 0
        self._sources: dict[str, float] = {}
        self._altered: dict[str, float] = {}
        self._save_command: str | None = None

    # Called from ngspice's thread (the caller's, in foreground runs).
    def _source_value(self, name: str, _time: float) -> float:
        return self._sources.get(name, 0.0)

    def prepare(self, context: Mapping[str, Any]) -> None:
        if self.duration is None and "duration_seconds" in context:
            self.duration = float(context["duration_seconds"])
        if self.max_step is None and "step_seconds" in context:
            self.max_step = float(context["step_seconds"]) / 50

    def start(self, initial_signals: Mapping[str, float]) -> None:
        if not self.netlist.exists():
            raise FileNotFoundError(self.netlist)
        duration = float(self.duration or 1.0)
        max_step = float(self.max_step or duration / 1000)
        self._time_epsilon = max_step * 1e-9
        # Stop time beyond the co-simulation so the analysis never ends on its own.
        tran = f".tran {max_step!r} {duration * 2 + 1!r} 0 {max_step!r} uic"
        lines = prepare_netlist(self.netlist.read_text(encoding="utf-8"), initial_signals, tran)
        if self.spice_options:
            options = " ".join(f"{key}={value}" for key, value in self.spice_options.items())
            lines.insert(len(lines) - 2, f".options {options}")
        self._save_command = ("save " + " ".join(["time", *saved_vectors(self.bindings.outputs)])
                              if self.save == "bound" and self.bindings.outputs else None)
        self.spice = _Ngspice.acquire(find_library(self.library), self)
        try:
            self.spice.command("destroy all")
            self.spice.load(lines)
            self._time, self._index, self._altered = 0.0, 0, {}
            self._apply_inputs(initial_signals)
            self._started = False
        except Exception:
            self.stop()
            raise

    def _apply_inputs(self, inputs: Mapping[str, float]) -> None:
        for binding in self.bindings.inputs:
            if binding.signal not in inputs:
                continue
            value = binding.value(float(inputs[binding.signal]))
            if binding.kind == "alter":
                if self._altered.get(binding.target) != value:
                    # `alter Rmotor = v` or `alter @Lmotor[inductance] = v`
                    self.spice.command(f"alter {binding.target} = {value!r}")
                    self._altered[binding.target] = value
            else:
                self._sources[binding.target.lower()] = value

    def step(self, time_seconds: float, step_seconds: float, inputs: Mapping[str, float]) -> StepResult:
        if self.spice is None:
            raise RuntimeError("ngspice adapter is not started")
        target = float(time_seconds)
        if target <= self._time + self._time_epsilon:
            return StepResult(self._read_outputs(self._index), {"time": self._time})
        self._apply_inputs(inputs)
        self._stop_time = target
        self.spice.command("delete all")          # clears `stop` and `save` entries alike
        if self._save_command:
            self.spice.command(self._save_command)
        self.spice.command(f"stop when time >= {target!r}")
        self.spice.command("resume" if self._started else "run")
        self._started = True
        times = self.spice.vector("time", self._index)
        if not times or times[-1] < target - 1e-6 * max(step_seconds, self._time_epsilon):
            raise NgspiceError(f"ngspice stopped at {times[-1] if times else 'start'} before {target}:\n"
                               + "\n".join(self.spice.output[-20:]))
        start = self._index
        self._index += len(times) - 1
        self._time = times[-1]
        return StepResult(self._read_outputs(start), {"time": self._time, "points": len(times) - 1})

    def _read_outputs(self, start: int) -> dict[str, float]:
        if not self.bindings.outputs:
            return {}
        times = self.spice.vector("time", start)
        cache: dict[str, list[float]] = {}
        outputs = {}
        for binding in self.bindings.outputs:
            if binding.vector not in cache:
                cache[binding.vector] = self._vector(binding.vector, start)
            value = _reduce(times, cache[binding.vector], binding.mode)
            outputs[binding.signal] = value * binding.scale + binding.offset
        return outputs

    def _vector(self, expression: str, start: int) -> list[float]:
        """`V(node)`, `V(node1,node2)`, `I(Vsource)` or a raw ngspice vector name."""
        match = _PROBE.fullmatch(expression.strip())
        if not match:
            return self.spice.vector(expression, start)
        kind, first, second = match.group(1).lower(), match.group(2).strip(), match.group(3)
        if kind == "i":
            return self.spice.vector(f"{first}#branch", start)
        values = self.spice.vector(first, start)
        if second:
            other = self.spice.vector(second.strip(), start)
            values = [x - y for x, y in zip(values, other)]
        return values

    def stop(self) -> None:
        if self.spice is not None:
            try:
                self.spice.command("destroy all")
                self.spice.command("remcirc")
            except NgspiceError:
                pass
            self.spice.release(self)
            self.spice = None

    def health(self) -> dict[str, object]:
        health: dict[str, object] = {**super().health(), "time": self._time, "points": self._index, "save": self.save}
        if self.spice is not None and self._started:
            health["vectors"] = len(self.spice.vector_names())
        return health

    def capabilities(self) -> dict[str, bool]:
        return {"deterministic_step": True, "variable_step": True, "checkpoint": False, "real_time": False,
                "events": False, "electrical_transient": True}


def _reduce(times: list[float], values: list[float], mode: str) -> float:
    if not values:
        return 0.0
    if mode == "last" or len(values) == 1 or len(times) != len(values):
        return values[-1]
    if mode == "min":
        return min(values)
    if mode == "max":
        return max(values)
    span = times[-1] - times[0]
    if span <= 0:
        return values[-1]
    if mode == "mean":
        area = sum((times[i + 1] - times[i]) * (values[i] + values[i + 1]) for i in range(len(values) - 1))
        return area / (2 * span)
    area = sum((times[i + 1] - times[i]) * (values[i] ** 2 + values[i + 1] ** 2) for i in range(len(values) - 1))
    return math.sqrt(area / (2 * span))
