from __future__ import annotations

from pathlib import Path

from .adapters import (
    LTspiceAdapter,
    MockElectricalAdapter,
    MockFirmwareAdapter,
    MockMechanicalAdapter,
    NgspiceAdapter,
    NgspiceSharedAdapter,
    PythonModelAdapter,
    RenodeAdapter,
    WebotsAdapter,
)
from .model import Domain, MechatronicProject, SimulatorConfig


def renode_kwargs(project: MechatronicProject, cfg) -> dict:
    """Translate `simulators.firmware` options into RenodeAdapter keyword arguments."""
    options = dict(cfg.options)

    def path(key: str) -> str | None:
        resolved = project.resolve(options.get(key))
        return str(resolved) if resolved else None

    return {
        "host": str(options.get("host", "127.0.0.1")),
        "port": int(options.get("port", 21234)),
        "timeout": float(options.get("timeout", 10.0)),
        "machine": options.get("machine"),
        "bindings": path("bindings"),
        "inline_bindings": list(options.get("inline_bindings", [])),
        "launch": bool(options.get("launch", False)),
        "script": path("script") or (str(project.resolve(cfg.model)) if cfg.model else None),
        "firmware": path("firmware"),
        "variables": dict(options.get("variables", {})),
        "executable": cfg.executable or options.get("executable"),
        "renode_args": options.get("renode_args"),
        "server_name": str(options.get("server_name", "embeddyn")),
        "startup_timeout": float(options.get("startup_timeout", 120.0)),
        "log_path": path("log"),
        "uarts": list(options.get("uarts", [])),
        "api": str(options.get("api", "auto")),
        "can": dict(options.get("can") or {}),
        "spi": list(options.get("spi") or []),
    }


def ngspice_shared_kwargs(project: MechatronicProject, cfg) -> dict:
    """Translate `simulators.electrical` options into NgspiceSharedAdapter keyword arguments."""
    options = dict(cfg.options)
    bindings = project.resolve(options.get("bindings"))
    max_step = options.get("max_step")
    return {
        "netlist": str(project.resolve(cfg.model) or Path("circuit.cir")),
        "bindings": str(bindings) if bindings else None,
        "inline_bindings": list(options.get("inline_bindings", [])),
        "library": options.get("library") or cfg.executable,
        "max_step": float(max_step) if max_step is not None else project.step_seconds / 50,
        "duration": project.duration_seconds,
        "options": dict(options.get("spice_options", {})),
    }


def python_model_kwargs(project: MechatronicProject, cfg) -> dict:
    """Translate simulator options into PythonModelAdapter keyword arguments."""
    options = dict(cfg.options)
    if not cfg.model:
        raise ValueError("python backend needs `model: module:Class` or `file.py:Class`")
    return {
        "model": str(cfg.model),
        "root": str(project.root),
        "parameters": dict(options.get("parameters", {})),
        "inputs": dict(options.get("inputs", {})),
        "outputs": dict(options.get("outputs", {})),
        "max_step": float(options.get("max_step", project.step_seconds / 10)),
    }


def _build_instance(project: MechatronicProject, cfg: SimulatorConfig, mock: bool):
    """Adapter for one simulator instance, or None when the backend builds nothing."""
    domain = cfg.domain
    core = domain in (Domain.FIRMWARE, Domain.ELECTRICAL, Domain.MECHANICAL)
    backend = str(cfg.options.get("backend", "mock" if mock else "auto")).lower()
    options = dict(cfg.options)
    if mock and core:
        backend = "mock"
    if backend == "python":
        return PythonModelAdapter(**python_model_kwargs(project, cfg))
    if backend == "fmu":
        from .adapters.fmu import FmuAdapter
        return FmuAdapter(**fmu_kwargs(project, cfg))
    if backend == "replay":
        from .adapters.replay import ReplayAdapter
        return ReplayAdapter(**replay_kwargs(project, cfg))
    if backend == "mujoco":
        from .adapters.mujoco_adapter import MujocoAdapter
        if not cfg.model:
            raise ValueError(f"{cfg.name}: mujoco backend needs `model: path/to/model.xml` (MJCF)")
        timestep = options.get("timestep")
        return MujocoAdapter(str(project.resolve(cfg.model)), inputs=dict(options.get("inputs", {})),
                             outputs=dict(options.get("outputs", {})),
                             timestep=float(timestep) if timestep is not None else None,
                             initial=dict(options.get("initial") or {}))
    if not core:
        raise ValueError(f"{cfg.name}: {domain.value} instances support backends python, fmu and replay (got {backend})")
    if domain is Domain.FIRMWARE:
        if backend == "mock":
            return MockFirmwareAdapter()
        if backend == "renode":
            return RenodeAdapter(**renode_kwargs(project, cfg))
        return None
    if domain is Domain.ELECTRICAL:
        if backend == "mock":
            return MockElectricalAdapter()
        if backend == "ltspice":
            return LTspiceAdapter(project.resolve(cfg.model) or Path("circuit.cir"), executable=cfg.executable,
                                  output_csv=str(options.get("output_csv", "cosim.csv")))
        if backend == "ngspice-shared":
            return NgspiceSharedAdapter(**ngspice_shared_kwargs(project, cfg))
        if backend in {"ngspice", "auto"}:
            return NgspiceAdapter(project.resolve(cfg.model) or Path("circuit.cir"), executable=cfg.executable,
                                  output_csv=str(options.get("output_csv", "cosim.csv")))
        return None
    if backend == "mock":
        return MockMechanicalAdapter()
    if backend in {"webots", "auto"}:
        return WebotsAdapter(
            world=project.resolve(cfg.model) or Path("world.wbt"),
            executable=cfg.executable,
            host=str(options.get("host", "127.0.0.1")),
            port=int(options.get("port", 8765)),
            headless=bool(options.get("headless", True)),
            launch=bool(options.get("launch", True)),
        )
    return None


def fmu_kwargs(project: MechatronicProject, cfg) -> dict:
    """Translate simulator options into FmuAdapter keyword arguments."""
    options = dict(cfg.options)
    if not cfg.model:
        raise ValueError(f"{cfg.name}: fmu backend needs `model: path/to/model.fmu`")
    return {
        "fmu": str(project.resolve(cfg.model)),
        "parameters": dict(options.get("parameters", {})),
        "inputs": dict(options.get("inputs", {})),
        "outputs": dict(options.get("outputs", {})),
        "max_step": float(options["max_step"]) if options.get("max_step") is not None else None,
        "instance_name": cfg.name or "fmu",
    }


def replay_kwargs(project: MechatronicProject, cfg) -> dict:
    options = dict(cfg.options)
    if not cfg.model:
        raise ValueError(f"{cfg.name}: replay backend needs `model: recording.csv`")
    return {
        "file": str(project.resolve(cfg.model)),
        "columns": dict(options.get("columns", {})),
        "interpolation": str(options.get("interpolation", "previous")),
        "time_column": str(options.get("time_column", "time")),
    }


def build_adapters(project: MechatronicProject, mock: bool = False, isolate: bool = False):
    """Adapters keyed by simulator instance name (legacy instances are named
    after their domain, and Domain keys work for lookups)."""
    from .runtime import InstanceMap
    adapters = InstanceMap()
    configs = project.simulator_instances()
    for name, cfg in configs.items():
        if not cfg.enabled:
            continue
        adapter = _build_instance(project, cfg, mock)
        if adapter is not None:
            adapters[name] = adapter
    if isolate:
        from .runtime import AdapterWorkerSpec, ProcessAdapterProxy
        isolated = InstanceMap()
        for name, adapter in adapters.items():
            cfg = configs[name]
            kwargs = {}
            if isinstance(adapter, (LTspiceAdapter, NgspiceAdapter)):
                kwargs = {"netlist": adapter.netlist, "executable": adapter.executable,
                          "output_csv": adapter.output_csv, "timeout": adapter.timeout}
            elif isinstance(adapter, WebotsAdapter):
                kwargs = {"world": adapter.world, "executable": adapter.executable, "host": adapter.host,
                          "port": adapter.port, "headless": adapter.headless, "launch": adapter.launch,
                          "timeout": adapter.timeout}
            elif isinstance(adapter, NgspiceSharedAdapter):
                kwargs = ngspice_shared_kwargs(project, cfg)
            elif isinstance(adapter, PythonModelAdapter):
                kwargs = python_model_kwargs(project, cfg)
            elif isinstance(adapter, RenodeAdapter):
                kwargs = renode_kwargs(project, cfg)
            elif adapter.__class__.__name__ == "FmuAdapter":
                kwargs = fmu_kwargs(project, cfg)
            elif adapter.__class__.__name__ == "ReplayAdapter":
                kwargs = replay_kwargs(project, cfg)
            spec = AdapterWorkerSpec(adapter.__class__.__module__, adapter.__class__.__name__, kwargs)
            timeout = float(cfg.options.get("worker_timeout", 30.0))
            isolated[name] = ProcessAdapterProxy(spec, timeout_seconds=timeout)
        return isolated
    return adapters
