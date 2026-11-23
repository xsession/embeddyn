"""FMI 2.0 / 3.0 co-simulation FMUs as simulator instances (no third-party dependency).

```yaml
instances:
  wheel:
    domain: mechanical
    model: fmus/rotational_load.fmu
    options:
      backend: fmu
      max_step: 1.0e-4                 # optional: split each step into doStep calls
      parameters: {J: 0.02, b: "@wheel.damping"}   # "@name" = project parameter
      inputs: {tau: mechanical.left_torque}         # FMU variable -> project signal
      outputs: {w: mechanical.left_speed}
```

The FMU is unpacked to a temporary directory and its shared library loaded
with ctypes for the current platform (`binaries/linux64` or `x86_64-linux`,
etc.). Only real/Float64 variables are exchanged. Parameters and start values
are set before initialisation; inputs before every `doStep`.

When the FMU declares `canGetAndSetFMUstate` and `canSerializeFMUstate` (FMI 2)
or `canGetAndSetFMUState` and `canSerializeFMUState` (FMI 3), checkpoints carry
the serialized FMU state, and a run resumes exactly where it stopped. Without
these flags a checkpoint records only the time, and resuming is refused.
"""
from __future__ import annotations

import base64
import ctypes
import platform
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .base import SimulatorAdapter, StepResult

FMI_OK, FMI_WARNING = 0, 1


class FmuError(RuntimeError):
    pass


@dataclass(frozen=True)
class FmuVariable:
    name: str
    reference: int
    causality: str
    variability: str
    start: float | None


@dataclass
class ModelDescription:
    version: str              # "2.0" or "3.0"
    model_identifier: str
    token: str                # FMI2 guid / FMI3 instantiationToken
    variables: dict[str, FmuVariable]
    default_step: float | None
    can_get_set_state: bool = False
    can_serialize_state: bool = False


def read_model_description(root: Path) -> ModelDescription:
    tree = ElementTree.parse(root / "modelDescription.xml").getroot()
    version = tree.get("fmiVersion", "")
    cosim = tree.find("CoSimulation")
    if cosim is None:
        raise FmuError("FMU does not support co-simulation")
    variables: dict[str, FmuVariable] = {}
    container = tree.find("ModelVariables")
    if version.startswith("2"):
        for node in container.findall("ScalarVariable"):
            real = node.find("Real")
            if real is None:
                continue
            start = real.get("start")
            variables[node.get("name")] = FmuVariable(node.get("name"), int(node.get("valueReference")),
                                                      node.get("causality", "local"), node.get("variability", "continuous"),
                                                      None if start is None else float(start))
        token, version = tree.get("guid", ""), "2.0"
    elif version.startswith("3"):
        for node in container:
            if node.tag not in ("Float64", "Float32"):
                continue
            start = node.get("start")
            variables[node.get("name")] = FmuVariable(node.get("name"), int(node.get("valueReference")),
                                                      node.get("causality", "local"), node.get("variability", "continuous"),
                                                      None if start is None else float(start.split()[0]))
        token, version = tree.get("instantiationToken", ""), "3.0"
    else:
        raise FmuError(f"unsupported FMI version {version!r}")
    experiment = tree.find("DefaultExperiment")
    step = experiment.get("stepSize") if experiment is not None else None
    suffix = "state" if version == "2.0" else "State"         # FMI 2: ...FMUstate, FMI 3: ...FMUState
    flag = lambda name: cosim.get(name, "false").strip().lower() == "true"  # noqa: E731
    return ModelDescription(version, cosim.get("modelIdentifier"), token, variables,
                            float(step) if step else None,
                            flag(f"canGetAndSetFMU{suffix}"), flag(f"canSerializeFMU{suffix}"))


def _platform_dirs(version: str) -> list[str]:
    machine = platform.machine().lower()
    bits64 = sys.maxsize > 2 ** 32
    if sys.platform.startswith("linux"):
        fmi2, fmi3 = ("linux64" if bits64 else "linux32"), ("aarch64-linux" if machine in ("aarch64", "arm64") else "x86_64-linux")
        suffix = ".so"
    elif sys.platform == "darwin":
        fmi2, fmi3, suffix = "darwin64", ("aarch64-darwin" if machine == "arm64" else "x86_64-darwin"), ".dylib"
    else:
        fmi2, fmi3, suffix = ("win64" if bits64 else "win32"), ("x86_64-windows" if bits64 else "x86-windows"), ".dll"
    return [(fmi3 if version == "3.0" else fmi2) + "|" + suffix]


_Logger2 = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p)
_Allocate = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t)
_Free = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
_StepFinished = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int)
_Logger3 = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p)


class _Callbacks2(ctypes.Structure):
    _fields_ = [("logger", _Logger2), ("allocateMemory", _Allocate), ("freeMemory", _Free),
                ("stepFinished", ctypes.c_void_p), ("componentEnvironment", ctypes.c_void_p)]


class FmuAdapter(SimulatorAdapter):
    name = "fmu"

    def __init__(self, fmu: str, parameters: Mapping[str, Any] | None = None, inputs: Mapping[str, str] | None = None,
                 outputs: Mapping[str, str] | None = None, max_step: float | None = None,
                 instance_name: str = "fmu") -> None:
        self.fmu = Path(fmu)
        if not self.fmu.exists():
            raise FileNotFoundError(self.fmu)
        self.parameter_spec = dict(parameters or {})
        self.input_map = dict(inputs or {})
        self.output_map = dict(outputs or {})
        self.max_step = max_step
        self.instance_name = instance_name
        self.workdir: Path | None = None
        self.description: ModelDescription | None = None
        self.lib: Any = None
        self.component: Any = None
        self.time = 0.0
        self.log: list[str] = []
        self._unpack()
        unknown = [name for name in [*self.input_map, *self.output_map, *self.parameter_spec]
                   if name not in self.description.variables]
        if unknown:
            raise FmuError(f"{self.fmu.name}: no real variables named {', '.join(sorted(unknown))}; "
                           f"available: {', '.join(sorted(self.description.variables))}")

    # -- loading -------------------------------------------------------------
    def _unpack(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="embeddyn-fmu-"))
        with zipfile.ZipFile(self.fmu) as archive:
            for member in archive.namelist():
                target = (self.workdir / member).resolve()
                if self.workdir.resolve() not in target.parents and target != self.workdir.resolve():
                    raise FmuError(f"{self.fmu.name}: unsafe path in archive: {member}")
            archive.extractall(self.workdir)
        self.description = read_model_description(self.workdir)

    def _load(self) -> None:
        description = self.description
        for entry in _platform_dirs(description.version):
            folder, suffix = entry.split("|")
            library = self.workdir / "binaries" / folder / (description.model_identifier + suffix)
            if library.exists():
                self.lib = ctypes.CDLL(str(library))
                return
        raise FmuError(f"{self.fmu.name}: no binary for this platform under binaries/ "
                       f"({', '.join(p.name for p in (self.workdir / 'binaries').glob('*'))})")

    def _fn(self, name: str, restype, *argtypes):
        prefix = "fmi2" if self.description.version == "2.0" else "fmi3"
        function = getattr(self.lib, prefix + name)
        function.restype, function.argtypes = restype, list(argtypes)
        return function

    def _check(self, status: int, what: str) -> None:
        if status not in (FMI_OK, FMI_WARNING):
            raise FmuError(f"{self.fmu.name}: {what} failed with status {status}" +
                           (":\n" + "\n".join(self.log[-10:]) if self.log else ""))

    def _on_log2(self, _env, instance, status, category, message) -> None:
        self.log.append(f"[{status}] {(category or b'').decode(errors='replace')}: {(message or b'').decode(errors='replace')}")
        del self.log[:-200]

    def _on_log3(self, _env, status, category, message) -> None:
        self._on_log2(None, None, status, category, message)

    # -- values ----------------------------------------------------------------
    def _set(self, values: Mapping[str, float]) -> None:
        if not values:
            return
        refs = (ctypes.c_uint * len(values))(*[self.description.variables[name].reference for name in values])
        data = (ctypes.c_double * len(values))(*[float(value) for value in values.values()])
        if self.description.version == "2.0":
            self._check(self._fn("SetReal", ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p)(
                self.component, refs, len(values), data), "SetReal")
        else:
            self._check(self._fn("SetFloat64", ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                 ctypes.c_void_p, ctypes.c_size_t)(self.component, refs, len(values), data, len(values)), "SetFloat64")

    def _get(self, names: list[str]) -> dict[str, float]:
        if not names:
            return {}
        refs = (ctypes.c_uint * len(names))(*[self.description.variables[name].reference for name in names])
        data = (ctypes.c_double * len(names))()
        if self.description.version == "2.0":
            self._check(self._fn("GetReal", ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p)(
                self.component, refs, len(names), data), "GetReal")
        else:
            self._check(self._fn("GetFloat64", ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                 ctypes.c_void_p, ctypes.c_size_t)(self.component, refs, len(names), data, len(names)), "GetFloat64")
        return dict(zip(names, data))

    def _resolve_parameters(self, values: Mapping[str, float]) -> dict[str, float]:
        resolved = {}
        for key, value in self.parameter_spec.items():
            if isinstance(value, str) and value.startswith("@"):
                if value[1:] not in values:
                    raise FmuError(f"FMU parameter {key} refers to unknown project parameter {value[1:]}")
                value = values[value[1:]]
            resolved[key] = float(value)
        return resolved

    # -- lifecycle ---------------------------------------------------------------
    def start(self, initial_signals: Mapping[str, float]) -> None:
        self._load()
        resources = self.workdir / "resources"
        description = self.description
        guid = str(description.token).encode()
        if description.version == "2.0":
            libc = ctypes.CDLL(None)
            libc.calloc.restype, libc.calloc.argtypes = ctypes.c_void_p, [ctypes.c_size_t, ctypes.c_size_t]
            libc.free.argtypes = [ctypes.c_void_p]
            self._keep = [_Logger2(self._on_log2), _Allocate(libc.calloc), _Free(libc.free)]
            self._callbacks = _Callbacks2(self._keep[0], self._keep[1], self._keep[2], None, None)
            instantiate = self._fn("Instantiate", ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                                   ctypes.c_char_p, ctypes.POINTER(_Callbacks2), ctypes.c_int, ctypes.c_int)
            self.component = instantiate(self.instance_name.encode(), 1, guid, resources.as_uri().encode(),
                                         ctypes.byref(self._callbacks), 0, 0)
        else:
            self._keep = [_Logger3(self._on_log3)]
            instantiate = self._fn("InstantiateCoSimulation", ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                                   ctypes.c_char_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_bool, ctypes.c_bool,
                                   ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, _Logger3, ctypes.c_void_p)
            self.component = instantiate(self.instance_name.encode(), guid, (str(resources) + "/").encode(),
                                         False, False, False, False, None, 0, None, self._keep[0], None)
        if not self.component:
            raise FmuError(f"{self.fmu.name}: instantiation failed" + (":\n" + "\n".join(self.log[-10:]) if self.log else ""))
        try:
            self._set(self._resolve_parameters(initial_signals))
            if description.version == "2.0":
                self._check(self._fn("SetupExperiment", ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_double,
                                     ctypes.c_double, ctypes.c_int, ctypes.c_double)(self.component, 0, 0.0, 0.0, 0, 0.0),
                            "SetupExperiment")
                self._check(self._fn("EnterInitializationMode", ctypes.c_int, ctypes.c_void_p)(self.component),
                            "EnterInitializationMode")
            else:
                self._check(self._fn("EnterInitializationMode", ctypes.c_int, ctypes.c_void_p, ctypes.c_bool, ctypes.c_double,
                                     ctypes.c_double, ctypes.c_bool, ctypes.c_double)(self.component, False, 0.0, 0.0, False, 0.0),
                            "EnterInitializationMode")
            self._set(self._inputs(initial_signals))
            self._check(self._fn("ExitInitializationMode", ctypes.c_int, ctypes.c_void_p)(self.component),
                        "ExitInitializationMode")
        except Exception:
            self.stop()
            raise
        self.time = 0.0

    def _inputs(self, values: Mapping[str, float]) -> dict[str, float]:
        return {name: float(values.get(signal, 0.0)) for name, signal in self.input_map.items()}

    def _do_step(self, start: float, size: float) -> None:
        if self.description.version == "2.0":
            status = self._fn("DoStep", ctypes.c_int, ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_int)(
                self.component, start, size, 1)
        else:
            flags = [ctypes.c_bool(False) for _ in range(3)]
            last = ctypes.c_double(0.0)
            status = self._fn("DoStep", ctypes.c_int, ctypes.c_void_p, ctypes.c_double, ctypes.c_double, ctypes.c_bool,
                              ctypes.POINTER(ctypes.c_bool), ctypes.POINTER(ctypes.c_bool), ctypes.POINTER(ctypes.c_bool),
                              ctypes.POINTER(ctypes.c_double))(
                self.component, start, size, True, *[ctypes.byref(flag) for flag in flags], ctypes.byref(last))
            if flags[1].value:
                raise FmuError(f"{self.fmu.name}: FMU requested termination at t={start + size:.6g}")
        self._check(status, f"doStep at t={start:.6g}")

    def step(self, time_seconds: float, step_seconds: float, inputs: Mapping[str, float]) -> StepResult:
        if self.component is None:
            raise RuntimeError("FMU adapter is not started")
        self._set(self._inputs(inputs))
        span = float(time_seconds) - self.time
        if span > 0:
            limit = self.max_step or self.description.default_step or span
            count = max(1, int(-(-span // limit) if span > limit * (1 + 1e-9) else 1))
            size = span / count
            for index in range(count):
                self._do_step(self.time + index * size, size)
            self.time = float(time_seconds)
        values = self._get(list(self.output_map))
        return StepResult({signal: values[name] for name, signal in self.output_map.items()})

    def stop(self) -> None:
        if self.component is not None and self.lib is not None:
            try:
                self._fn("Terminate", ctypes.c_int, ctypes.c_void_p)(self.component)
            finally:
                self._fn("FreeInstance", None, ctypes.c_void_p)(self.component)
                self.component = None

    def __del__(self) -> None:
        if self.workdir is not None and self.component is None:
            shutil.rmtree(self.workdir, ignore_errors=True)

    # -- FMU state ---------------------------------------------------------------
    @property
    def restorable(self) -> bool:
        d = self.description
        return bool(d and d.can_get_set_state and d.can_serialize_state)

    def _state_names(self) -> tuple[str, str, str, str, str]:
        if self.description.version == "2.0":
            return "GetFMUstate", "SetFMUstate", "FreeFMUstate", "SerializedFMUstateSize", "SerializeFMUstate"
        return "GetFMUState", "SetFMUState", "FreeFMUState", "SerializedFMUStateSize", "SerializeFMUState"

    def get_state_bytes(self) -> bytes:
        """Serialized FMU state (fmi*GetFMUState + fmi*SerializeFMUState)."""
        get, _, free, size_name, serialize = self._state_names()
        state = ctypes.c_void_p()
        self._check(self._fn(get, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
            self.component, ctypes.byref(state)), get)
        try:
            size = ctypes.c_size_t()
            self._check(self._fn(size_name, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t))(
                self.component, state, ctypes.byref(size)), size_name)
            buffer = (ctypes.c_ubyte * size.value)()
            self._check(self._fn(serialize, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)(
                self.component, state, buffer, size.value), serialize)
            return bytes(buffer)
        finally:
            self._fn(free, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(self.component, ctypes.byref(state))

    def set_state_bytes(self, data: bytes) -> None:
        _, set_name, free, _, _ = self._state_names()
        deserialize = "DeSerializeFMUstate" if self.description.version == "2.0" else "DeserializeFMUState"
        state = ctypes.c_void_p()
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        self._check(self._fn(deserialize, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                             ctypes.POINTER(ctypes.c_void_p))(self.component, buffer, len(data), ctypes.byref(state)),
                    deserialize)
        try:
            self._check(self._fn(set_name, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(self.component, state), set_name)
        finally:
            self._fn(free, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(self.component, ctypes.byref(state))

    def checkpoint(self) -> object:
        record: dict[str, Any] = {"fmu": self.fmu.name, "time": self.time, "restorable": self.restorable}
        if self.restorable and self.component is not None:
            record["state"] = base64.b64encode(self.get_state_bytes()).decode("ascii")
        return record

    def restore(self, checkpoint: object) -> None:
        if checkpoint is None:
            return
        data = dict(checkpoint)
        if not data.get("restorable"):
            raise FmuError(f"{self.fmu.name}: this FMU cannot save its state (canGetAndSetFMUstate / "
                           "canSerializeFMUstate), so the run cannot resume from a checkpoint")
        if "state" not in data:
            raise FmuError(f"{self.fmu.name}: the checkpoint holds no FMU state (taken before the FMU started)")
        if self.component is None:
            raise FmuError(f"{self.fmu.name}: start the FMU before restoring a checkpoint")
        self.set_state_bytes(base64.b64decode(data["state"]))
        self.time = float(data["time"])

    def capabilities(self) -> dict[str, bool]:
        return {**super().capabilities(), "checkpoint": self.restorable}
