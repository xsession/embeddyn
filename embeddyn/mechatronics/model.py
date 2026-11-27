from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import math
import re
from pathlib import Path
from typing import Any

import yaml

from .faults import Fault, parse_faults
from .quantities import Quantity


class Domain(str, Enum):
    FIRMWARE = "firmware"
    MECHANICAL = "mechanical"
    ELECTRICAL = "electrical"
    THERMAL = "thermal"
    ENVIRONMENT = "environment"


class SignalKind(str, Enum):
    CONTINUOUS = "continuous"
    DISCRETE = "discrete"
    EVENT = "event"
    BUS = "bus"


@dataclass(frozen=True)
class Signal:
    name: str
    domain: Domain
    unit: str = ""
    initial: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    data_type: str = "float64"
    kind: SignalKind = SignalKind.CONTINUOUS
    quantity: str | None = None
    retention: str = "full"

    def clamp(self, value: float) -> float:
        if self.minimum is not None:
            value = max(self.minimum, value)
        if self.maximum is not None:
            value = min(self.maximum, value)
        return value


@dataclass(frozen=True)
class Transform:
    type: str = "affine"
    gain: float = 1.0
    offset: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    deadband: float = 0.0

    def apply(self, value: float) -> float:
        if self.type == "identity":
            result = value
        elif self.type == "affine":
            result = self.gain * value + self.offset
        elif self.type == "clamp":
            result = value
        elif self.type == "deadband":
            result = 0.0 if abs(value) < self.deadband else value
        elif self.type == "quantize":
            quantum = self.gain
            if quantum <= 0:
                raise ValueError("quantize transform requires gain/quantum > 0")
            result = round(value / quantum) * quantum + self.offset
        else:
            raise ValueError(f"unsupported coupling transform: {self.type}")
        if self.minimum is not None:
            result = max(self.minimum, result)
        if self.maximum is not None:
            result = min(self.maximum, result)
        return result


@dataclass(frozen=True)
class Coupling:
    source: str
    target: str
    transform: Transform = field(default_factory=Transform)
    delay_steps: int = 0
    interpolation: str = "zero_order_hold"
    extrapolation: str = "hold"
    # Transform fields taken from project parameters: (field, reference) with field one of
    # gain, offset, minimum, maximum, deadband and reference "@name", "-@name" or "1/@name".
    references: tuple[tuple[str, str], ...] = ()

    @property
    def gain(self) -> float:
        return self.transform.gain

    @property
    def offset(self) -> float:
        return self.transform.offset


@dataclass
class SimulatorConfig:
    enabled: bool = True
    executable: str | None = None
    model: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    # Named-instance fields. Legacy `simulators:` entries get name = domain.
    name: str = ""
    domain: "Domain | None" = None
    step_seconds: float | None = None


CORE_DOMAINS = ("firmware", "electrical", "mechanical")
_INSTANCE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


def _parse_simulator(name: str, domain: "Domain", spec: dict[str, Any]) -> SimulatorConfig:
    step = spec.get("step", spec.get("step_seconds"))
    return SimulatorConfig(
        enabled=bool(spec.get("enabled", True)),
        executable=spec.get("executable"),
        model=spec.get("model"),
        options=dict(spec.get("options") or {}),
        name=name,
        domain=domain,
        step_seconds=None if step is None else Quantity.parse(step, "s").value,
    )


@dataclass(frozen=True)
class ConvergenceConfig:
    enabled: bool = False
    tolerance: float = 1e-9
    max_iterations: int = 8
    failure_policy: str = "error"


@dataclass(frozen=True)
class RealTimeConfig:
    enabled: bool = False
    speed_factor: float = 1.0
    deadline_tolerance_seconds: float = 0.01
    missed_deadline_policy: str = "continue"


@dataclass(frozen=True)
class SchedulerConfig:
    algorithm: str = "deterministic-fixed-step"
    macro_step_seconds: float = 0.001
    domain_steps: dict[Domain, float] = field(default_factory=dict)
    convergence: ConvergenceConfig = field(default_factory=ConvergenceConfig)
    real_time: RealTimeConfig = field(default_factory=RealTimeConfig)

    def step_for(self, domain: Domain) -> float:
        return self.domain_steps.get(domain, self.macro_step_seconds)


@dataclass
class MechatronicProject:
    name: str
    step_seconds: float
    duration_seconds: float
    signals: dict[str, Signal]
    couplings: list[Coupling]
    simulators: dict[Domain, SimulatorConfig]
    root: Path
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    scheduler: SchedulerConfig | None = None
    parameters: dict[str, Quantity] = field(default_factory=dict)
    parameter_provenance: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    component_definitions: dict[str, dict[str, Any]] = field(default_factory=dict)
    component_instances: dict[str, dict[str, Any]] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    instances: dict[str, SimulatorConfig] = field(default_factory=dict)
    declares_instances: bool = False
    verification: dict[str, Any] = field(default_factory=dict)
    faults: list[Fault] = field(default_factory=list)
    media: dict[str, Any] = field(default_factory=dict)        # impaired event topics, see comms.medium

    def declared_instances(self) -> dict[str, SimulatorConfig]:
        """Instances written in the project file (legacy domains included), no defaults."""
        result = dict(self.instances)
        for domain, config in self.simulators.items():
            if config.domain is None:
                config.domain, config.name = domain, domain.value
            result.setdefault(config.name or domain.value, config)
        return result

    def simulator_instances(self) -> dict[str, SimulatorConfig]:
        """Every simulator instance in declaration order, keyed by name.

        Projects without an `instances:` section keep their historic
        behaviour: one instance per configured domain, plus a default (auto
        backend) instance for each core domain that isn't configured.
        """
        result = dict(self.instances)
        for domain, config in self.simulators.items():
            if config.domain is None:
                config.domain, config.name = domain, domain.value
            result.setdefault(config.name or domain.value, config)
        if not self.declares_instances:
            present = {config.domain for config in result.values()}
            for name in CORE_DOMAINS:
                if Domain(name) not in present:
                    result[name] = SimulatorConfig(name=name, domain=Domain(name))
        return result

    @classmethod
    def load(cls, path: Path) -> "MechatronicProject":
        path = path.resolve()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.from_mapping(raw, path.parent, path.stem)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any], root: Path, default_name: str = "project") -> "MechatronicProject":
        """Project from an already parsed project document; relative paths
        resolve against `root` (the directory the document belongs to)."""
        raw = raw or {}
        schema_version = int(raw.get("schema_version", 1))
        signals: dict[str, Signal] = {}
        for name, spec in (raw.get("signals") or {}).items():
            spec = spec or {}
            initial = Quantity.parse(spec.get("initial", 0.0), str(spec.get("unit", "")))
            signals[name] = Signal(
                name=name,
                domain=Domain(spec.get("domain", "environment")),
                unit=initial.unit or str(spec.get("unit", "")),
                initial=initial.value,
                minimum=_optional_quantity(spec.get("min"), initial.unit),
                maximum=_optional_quantity(spec.get("max"), initial.unit),
                data_type=str(spec.get("data_type", "float64")),
                kind=SignalKind(spec.get("kind", "continuous")),
                quantity=spec.get("quantity"),
                retention=str(spec.get("retention", "full")),
            )
        couplings = [_parse_coupling(item) for item in (raw.get("couplings") or [])]
        sims: dict[Domain, SimulatorConfig] = {}
        simulator_instances: dict[str, SimulatorConfig] = {}
        for key, spec in (raw.get("simulators") or {}).items():
            domain = Domain(key)
            sims[domain] = simulator_instances[domain.value] = _parse_simulator(domain.value, domain, spec or {})
        for name, spec in (raw.get("instances") or {}).items():
            spec = dict(spec or {})
            if not isinstance(name, str) or not _INSTANCE_NAME.match(name):
                raise ValueError(f"invalid simulator instance name: {name!r}")
            if name in simulator_instances:
                raise ValueError(f"duplicate simulator instance name: {name}")
            if "domain" not in spec:
                raise ValueError(f"simulator instance {name} needs a domain")
            domain = Domain(spec["domain"])
            simulator_instances[name] = _parse_simulator(name, domain, spec)
            if name == domain.value:
                sims[domain] = simulator_instances[name]
        step_seconds = Quantity.parse(raw.get("step_seconds", raw.get("scheduler", {}).get("macro_step", 0.001)), "s").value
        scheduler = _parse_scheduler(raw.get("scheduler") or {}, step_seconds)
        if (any(config.step_seconds is not None for config in simulator_instances.values())
                and "algorithm" not in (raw.get("scheduler") or {}) and scheduler.algorithm == "deterministic-fixed-step"):
            from dataclasses import replace as _replace
            scheduler = _replace(scheduler, algorithm="multirate-fixed-step")
        parameters: dict[str, Quantity] = {}
        provenance: dict[str, list[dict[str, Any]]] = {}
        for name, value in (raw.get("parameters") or {}).items():
            quantity = Quantity.parse(value.get("value", value) if isinstance(value, dict) else value,
                                      str(value.get("unit", "")) if isinstance(value, dict) else "")
            parameters[name] = quantity
            provenance[name] = list(value.get("provenance") or []) if isinstance(value, dict) else []
            if not provenance[name]:
                provenance[name] = [{"source": "project", "value": quantity.value, "unit": quantity.unit}]
        components = raw.get("components") or {}
        definitions = dict(components.get("definitions") or {})
        instances = dict(components.get("instances") or {})
        # Instance parameter overrides participate in the resolved parameter set.
        for instance_name, instance in instances.items():
            for param_name, raw_value in dict((instance or {}).get("parameters") or {}).items():
                key = f"{instance_name}.{param_name}"
                q = Quantity.parse(raw_value.get("value", raw_value) if isinstance(raw_value, dict) else raw_value,
                                   str(raw_value.get("unit", "")) if isinstance(raw_value, dict) else "")
                parameters[key] = q
                provenance[key] = [{"source": "component-instance", "component": instance_name,
                                    "value": q.value, "unit": q.unit}]
        project = cls(
            name=str(raw.get("name", default_name)),
            step_seconds=step_seconds,
            duration_seconds=Quantity.parse(raw.get("duration_seconds", 1.0), "s").value,
            signals=signals,
            couplings=couplings,
            simulators=sims,
            root=Path(root).resolve(),
            metadata=dict(raw.get("metadata") or {}),
            schema_version=schema_version,
            scheduler=scheduler,
            parameters=parameters,
            parameter_provenance=provenance,
            component_definitions=definitions,
            component_instances=instances,
            metrics=dict(raw.get("metrics") or {}),
            instances=simulator_instances,
            declares_instances=bool(raw.get("instances")),
            verification=dict(raw.get("verification") or {}),
            faults=parse_faults(raw.get("faults"), signals),
            media={str(k): dict(v or {}) for k, v in (raw.get("media") or {}).items()},
        )
        project.validate()
        return project

    def validate(self) -> None:
        if self.schema_version not in {1, 2}:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        for name, config in self.simulator_instances().items():
            if config.step_seconds is not None:
                if not math.isfinite(config.step_seconds) or config.step_seconds <= 0:
                    raise ValueError(f"step for simulator instance {name} must be a finite positive number")
                ratio = self.step_seconds / config.step_seconds
                if abs(ratio - round(ratio)) > 1e-9:
                    raise ValueError(f"step for simulator instance {name} must divide the macro step")
        if self.metrics:
            from .metrics import parse_metric_specs
            for spec in parse_metric_specs(self.metrics).values():
                for name in (spec.signal, spec.target if isinstance(spec.target, str) else None):
                    if name and name not in self.signals:
                        raise ValueError(f"metric {spec.name} references unknown signal {name}")
        if not math.isfinite(self.step_seconds) or self.step_seconds <= 0:
            raise ValueError("step_seconds must be a finite positive number")
        if not math.isfinite(self.duration_seconds) or self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be a finite positive number")
        if self.scheduler and self.scheduler.algorithm not in {"deterministic-fixed-step", "multirate-fixed-step", "real-time-fixed-step", "real-time-multirate"}:
            raise ValueError(f"unsupported scheduler algorithm: {self.scheduler.algorithm}")
        if self.scheduler and (
            not math.isfinite(self.scheduler.macro_step_seconds)
            or not math.isclose(self.scheduler.macro_step_seconds, self.step_seconds, rel_tol=0.0, abs_tol=1e-15)
        ):
            raise ValueError("scheduler macro_step must match step_seconds")
        for domain, step in (self.scheduler.domain_steps if self.scheduler else {}).items():
            if not math.isfinite(step) or step <= 0:
                raise ValueError(f"scheduler step for {domain.value} must be a finite positive number")
            ratio = self.step_seconds / step
            if abs(ratio - round(ratio)) > 1e-9:
                raise ValueError(f"domain step for {domain.value} must divide macro step")
        if self.scheduler:
            if not math.isfinite(self.scheduler.convergence.tolerance) or self.scheduler.convergence.tolerance <= 0 or self.scheduler.convergence.max_iterations < 1:
                raise ValueError("invalid scheduler convergence configuration")
            if self.scheduler.convergence.failure_policy not in {"error", "warn", "accept"}:
                raise ValueError("unsupported convergence failure policy")
            if not math.isfinite(self.scheduler.real_time.speed_factor) or self.scheduler.real_time.speed_factor <= 0:
                raise ValueError("real-time speed_factor must be a finite positive number")
            if not math.isfinite(self.scheduler.real_time.deadline_tolerance_seconds) or self.scheduler.real_time.deadline_tolerance_seconds < 0:
                raise ValueError("real-time deadline_tolerance must be finite and non-negative")
            if self.scheduler.real_time.missed_deadline_policy not in {"continue", "warn", "abort"}:
                raise ValueError("unsupported missed deadline policy")
        for name, signal in self.signals.items():
            if not math.isfinite(signal.initial):
                raise ValueError(f"signal {name} initial value must be finite")
            if signal.minimum is not None and not math.isfinite(signal.minimum):
                raise ValueError(f"signal {name} minimum must be finite")
            if signal.maximum is not None and not math.isfinite(signal.maximum):
                raise ValueError(f"signal {name} maximum must be finite")
            if signal.minimum is not None and signal.maximum is not None and signal.minimum > signal.maximum:
                raise ValueError(f"signal {name} minimum exceeds maximum")
            if signal.minimum is not None and signal.initial < signal.minimum:
                raise ValueError(f"signal {name} initial value is below minimum")
            if signal.maximum is not None and signal.initial > signal.maximum:
                raise ValueError(f"signal {name} initial value is above maximum")
        for name, quantity in self.parameters.items():
            if not math.isfinite(quantity.value):
                raise ValueError(f"parameter {name} must be finite")
        for name, instance in self.component_instances.items():
            definition = (instance or {}).get("definition")
            if definition and definition not in self.component_definitions:
                raise ValueError(f"component instance {name} references unknown definition {definition}")
        for coupling in self.couplings:
            for label, reference in coupling.references:
                name = _REFERENCE.match(reference).group("name")
                if name not in self.parameters:
                    raise ValueError(f"coupling {coupling.source} -> {coupling.target}: {label} refers to "
                                     f"unknown parameter {name}")
        for coupling in self.resolved_couplings():
            if coupling.source not in self.signals:
                raise ValueError(f"unknown coupling source: {coupling.source}")
            if coupling.target not in self.signals:
                raise ValueError(f"unknown coupling target: {coupling.target}")
            if coupling.delay_steps < 0:
                raise ValueError("delay_steps cannot be negative")
            if coupling.delay_steps and self.signals[coupling.source].kind not in {SignalKind.CONTINUOUS, SignalKind.DISCRETE}:
                raise ValueError(f"delayed coupling source {coupling.source} must be continuous or discrete")
            transform = coupling.transform
            if transform.type not in {"identity", "affine", "clamp", "deadband", "quantize"}:
                raise ValueError(f"unsupported coupling transform: {transform.type}")
            for label, value in (("gain", transform.gain), ("offset", transform.offset), ("deadband", transform.deadband)):
                if not math.isfinite(value):
                    raise ValueError(f"coupling transform {label} must be finite")
            if transform.deadband < 0:
                raise ValueError("coupling transform deadband cannot be negative")
            if transform.type == "quantize" and transform.gain <= 0:
                raise ValueError("quantize transform requires gain/quantum > 0")
            if transform.minimum is not None and not math.isfinite(transform.minimum):
                raise ValueError("coupling transform minimum must be finite")
            if transform.maximum is not None and not math.isfinite(transform.maximum):
                raise ValueError("coupling transform maximum must be finite")
            if transform.minimum is not None and transform.maximum is not None and transform.minimum > transform.maximum:
                raise ValueError("coupling transform minimum exceeds maximum")
            source_unit = self.signals[coupling.source].unit
            target_unit = self.signals[coupling.target].unit
            if coupling.transform.type in {"identity", "clamp", "deadband"} and source_unit and target_unit and source_unit != target_unit:
                raise ValueError(f"unit mismatch for {coupling.source} -> {coupling.target}: {source_unit} != {target_unit}")

    def resolved_couplings(self) -> list[Coupling]:
        """Couplings with parameter references replaced by the parameters' current values, so
        overrides (tuning, tolerance, estimation, the CLI) reach every coupling that uses them."""
        resolved = []
        for coupling in self.couplings:
            if not coupling.references:
                resolved.append(coupling)
                continue
            values = {label: self.parameter_reference(reference) for label, reference in coupling.references}
            resolved.append(replace(coupling, transform=replace(coupling.transform, **values)))
        return resolved

    def parameter_reference(self, reference: str) -> float:
        """Value of "@name", "-@name" or "1/@name"."""
        match = _REFERENCE.match(reference)
        if match is None:
            raise ValueError(f"invalid parameter reference {reference!r} (use @name, -@name or 1/@name)")
        name = match.group("name")
        if name not in self.parameters:
            raise ValueError(f"unknown parameter {name}")
        value = float(self.parameters[name].value)
        if match.group("inverse"):
            if value == 0:
                raise ValueError(f"1/@{name}: parameter is zero")
            value = 1.0 / value
        return -value if match.group("sign") else value

    def apply_parameter_overrides(self, overrides: dict[str, Any], *, source: str = "runtime-override") -> None:
        for name, raw_value in overrides.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("parameter override names must be non-empty strings")
            existing = self.parameters.get(name)
            default_unit = existing.unit if existing is not None else ""
            quantity = Quantity.parse(raw_value, default_unit)
            if not math.isfinite(quantity.value):
                raise ValueError(f"parameter {name} must be finite")
            self.parameters[name] = quantity
            history = self.parameter_provenance.setdefault(name, [])
            history.append({"source": source, "value": quantity.value, "unit": quantity.unit})
        self.validate()

    def resolve(self, value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value)
        return path if path.is_absolute() else (self.root / path).resolve()


_REFERENCE = re.compile(r"^\s*(?P<sign>-)?\s*(?P<inverse>1\s*/\s*)?@(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*)\s*$")
_TRANSFORM_FIELDS = (("gain", ("gain", "quantum"), 1.0), ("offset", ("offset",), 0.0),
                     ("minimum", ("min", "minimum"), None), ("maximum", ("max", "maximum"), None),
                     ("deadband", ("deadband",), 0.0))


def _parse_coupling(item: dict[str, Any]) -> Coupling:
    if item.get("expression"):
        raise ValueError("unsafe coupling expressions are no longer supported; use a typed transform")
    transform_raw = item.get("transform") or {}
    if not transform_raw:
        transform_raw = {"type": "affine", "gain": item.get("gain", 1.0), "offset": item.get("offset", 0.0)}
    timing = item.get("timing") or {}
    values: dict[str, float | None] = {}
    references: list[tuple[str, str]] = []
    for label, keys, default in _TRANSFORM_FIELDS:
        raw = next((transform_raw[key] for key in keys if transform_raw.get(key) is not None), default)
        if isinstance(raw, str) and "@" in raw:
            if not _REFERENCE.match(raw):
                raise ValueError(f"coupling {item.get('source')} -> {item.get('target')}: invalid {label} "
                                 f"{raw!r} (use @name, -@name or 1/@name)")
            references.append((label, raw.strip()))
            values[label] = 1.0 if default is None else default        # placeholder until resolved
        else:
            # Numbers, or numbers with units ("0.08 N*m/A"), normalised to SI.
            values[label] = None if raw is None else Quantity.parse(raw).value
    return Coupling(
        source=item["source"],
        target=item["target"],
        transform=Transform(type=str(transform_raw.get("type", "affine")), **values),
        delay_steps=int(item.get("delay_steps", timing.get("delay_steps", 0))),
        interpolation=str(timing.get("interpolation", "zero_order_hold")),
        extrapolation=str(timing.get("extrapolation", "hold")),
        references=tuple(references),
    )


def _parse_scheduler(raw: dict[str, Any], default_step: float) -> SchedulerConfig:
    domain_steps: dict[Domain, float] = {}
    for key, spec in (raw.get("domains") or {}).items():
        value = spec.get("step") if isinstance(spec, dict) else spec
        domain_steps[Domain(key)] = Quantity.parse(value, "s").value
    convergence = raw.get("convergence") or {}
    real_time = raw.get("real_time") or {}
    algorithm = str(raw.get("algorithm", "multirate-fixed-step" if domain_steps else "deterministic-fixed-step"))
    return SchedulerConfig(
        algorithm=algorithm,
        macro_step_seconds=Quantity.parse(raw.get("macro_step", default_step), "s").value,
        domain_steps=domain_steps,
        convergence=ConvergenceConfig(
            enabled=bool(convergence.get("enabled", False)),
            tolerance=float(convergence.get("tolerance", 1e-9)),
            max_iterations=int(convergence.get("max_iterations", 8)),
            failure_policy=str(convergence.get("failure_policy", "error")),
        ),
        real_time=RealTimeConfig(
            enabled=bool(real_time.get("enabled", algorithm.startswith("real-time"))),
            speed_factor=float(real_time.get("speed_factor", 1.0)),
            deadline_tolerance_seconds=Quantity.parse(real_time.get("deadline_tolerance", 0.01), "s").value,
            missed_deadline_policy=str(real_time.get("missed_deadline_policy", "continue")),
        ),
    )


def _optional_quantity(value: Any, unit: str) -> float | None:
    return None if value is None else Quantity.parse(value, unit).value
