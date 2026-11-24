"""Model verification: energy balance, step-size convergence, golden waveforms
and scenario matrices, declared in a project's `verification:` section and
run by `embeddyn mechatronics verify`.

```yaml
verification:
  energy:                         # first law over the run: sources = sinks + stored change
    tolerance: 0.02               # |residual| / source energy
    sources: [{name: electrical_in, product: [electrical.motor_voltage, electrical.motor_current]}]
    sinks:
      - {name: copper, product: [electrical.motor_current, electrical.motor_current, electrical.motor_resistance]}
    storage:
      - {name: rotor, product: [0.5, mechanical.inertia, mechanical.velocity, mechanical.velocity]}
  convergence:                    # rerun with step/2 and step/4
    signals: [mechanical.velocity, electrical.motor_current]
    tolerance: 0.01               # RMS(x_h/2 - x_h/4) / RMS(x_h/4)
  golden:
    file: golden/dc-motor.csv     # relative to the project file
    every: 0.01
    signals: {mechanical.velocity: 0.01, electrical.motor_current: 0.05}   # absolute tolerances
  scenarios:
    - name: heavy-load
      signals: {mechanical.load_torque: 0.3}     # initial-value overrides
      parameters: {controller.kp: 0.2}          # parameter overrides
      faults: [{signal: sensor.speed, type: stuck, start: 0.5}]   # added to the project's faults
      duration: 1.0
      expect: [{metric: speed.overshoot, max: 10}, {metric: current.peak, max: 15}]
```

Product factors are signal names (per-sample values), project parameter
names, or numbers. Every check returns a dict with `passed` and its figures.
"""
from __future__ import annotations

import csv
import math
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .factory import build_adapters
from .faults import parse_faults
from .metrics import compute_metrics, parse_metric_specs, select_metrics
from .model import MechatronicProject
from .orchestrator import CoSimulationOrchestrator

Rows = list[dict[str, Any]]


# -- running -----------------------------------------------------------------

def run(project: MechatronicProject, mock: bool = False) -> Rows:
    return CoSimulationOrchestrator(project, build_adapters(project, mock=mock)).run()


def refine(project_file: Path, factor: int, duration: float | None = None) -> MechatronicProject:
    """The project with every step (macro, domain and instance) divided by `factor`."""
    project = MechatronicProject.load(project_file)
    if duration is not None:
        project.duration_seconds = duration
    if factor == 1:
        return project
    project.step_seconds /= factor
    if project.scheduler is not None:
        project.scheduler = replace(project.scheduler, macro_step_seconds=project.scheduler.macro_step_seconds / factor,
                                    domain_steps={d: s / factor for d, s in project.scheduler.domain_steps.items()})
    for config in project.declared_instances().values():
        if config.step_seconds is not None:
            config.step_seconds /= factor
    project.validate()
    return project


# -- helpers -------------------------------------------------------------------

def _factor(project: MechatronicProject, row: Mapping[str, Any], factor: Any) -> float:
    if isinstance(factor, (int, float)):
        return float(factor)
    name = str(factor)
    if name in row:
        return float(row[name])
    if name in project.parameters:
        return float(project.parameters[name].value)
    try:
        return float(name)
    except ValueError:
        raise ValueError(f"energy term factor {name!r} is not a signal, parameter or number") from None


def _term_series(project: MechatronicProject, rows: Rows, term: Mapping[str, Any]) -> list[float]:
    factors = term.get("product") or ([term["signal"]] if "signal" in term else None)
    if not factors:
        raise ValueError(f"energy term {term.get('name', term)} needs 'product' or 'signal'")
    gain = float(term.get("gain", 1.0))
    series = []
    for row in rows:
        value = gain
        for factor in factors:
            value *= _factor(project, row, factor)
        series.append(value)
    return series


def _integral(times: Sequence[float], values: Sequence[float]) -> float:
    return sum((times[i + 1] - times[i]) * (values[i] + values[i + 1]) / 2 for i in range(len(times) - 1))


def _interpolate(times: Sequence[float], values: Sequence[float], at: Sequence[float]) -> list[float]:
    out, j = [], 0
    for t in at:
        while j < len(times) - 2 and times[j + 1] < t:
            j += 1
        t0, t1 = times[j], times[j + 1]
        w = 0.0 if t1 == t0 else min(max((t - t0) / (t1 - t0), 0.0), 1.0)
        out.append(values[j] + w * (values[j + 1] - values[j]))
    return out


def _rms(values: Sequence[float]) -> float:
    return math.sqrt(sum(v * v for v in values) / len(values)) if values else 0.0


# -- checks --------------------------------------------------------------------

def energy_balance(project: MechatronicProject, rows: Rows, spec: Mapping[str, Any]) -> dict[str, Any]:
    """∫sources − ∫sinks − ΔE_storage, relative to ∫sources."""
    times = [float(row["time"]) for row in rows]
    report: dict[str, Any] = {"sources": {}, "sinks": {}, "storage": {}}
    for group in ("sources", "sinks"):
        for index, term in enumerate(spec.get(group) or []):
            report[group][str(term.get("name", f"{group}{index}"))] = _integral(times, _term_series(project, rows, term))
    for index, term in enumerate(spec.get("storage") or []):
        series = _term_series(project, rows, term)
        report["storage"][str(term.get("name", f"storage{index}"))] = series[-1] - series[0]
    supplied = sum(report["sources"].values())
    residual = supplied - sum(report["sinks"].values()) - sum(report["storage"].values())
    tolerance = float(spec.get("tolerance", 0.02))
    relative = abs(residual) / abs(supplied) if supplied else math.inf
    report.update({"supplied": supplied, "residual": residual, "relative_residual": relative,
                   "tolerance": tolerance, "passed": relative <= tolerance})
    return report


def convergence(project_file: Path, spec: Mapping[str, Any], mock: bool = False,
                duration: float | None = None) -> dict[str, Any]:
    """Run at h, h/2, h/4; report the observed order and the h/2 → h/4 change."""
    factors = [1, 2, 4]
    signals = [str(name) for name in spec.get("signals") or []]
    if not signals:
        raise ValueError("convergence check needs 'signals'")
    tolerance = float(spec.get("tolerance", 0.01))
    runs = []
    for factor in factors:
        project = refine(project_file, factor, duration if duration is not None else spec.get("duration"))
        runs.append((project.step_seconds, run(project, mock)))
    reference_times = [float(row["time"]) for row in runs[0][1]]
    sampled = {}
    for step, rows in runs:
        times = [float(row["time"]) for row in rows]
        sampled[step] = {name: _interpolate(times, [float(row[name]) for row in rows], reference_times) for name in signals}
    steps = [step for step, _ in runs]
    results: dict[str, Any] = {}
    passed = True
    for name in signals:
        coarse = [a - b for a, b in zip(sampled[steps[0]][name], sampled[steps[1]][name])]
        fine = [a - b for a, b in zip(sampled[steps[1]][name], sampled[steps[2]][name])]
        scale = _rms(sampled[steps[2]][name]) or 1.0
        e1, e2 = _rms(coarse), _rms(fine)
        order = math.log2(e1 / e2) if e1 > 0 and e2 > 0 else None
        relative = e2 / scale
        ok = relative <= tolerance
        passed &= ok
        results[name] = {"rms_change_h_to_h2": e1, "rms_change_h2_to_h4": e2, "relative_change": relative,
                         "observed_order": order, "passed": ok}
    return {"steps": steps, "tolerance": tolerance, "signals": results, "passed": passed}


def golden(project: MechatronicProject, rows: Rows, spec: Mapping[str, Any], update: bool = False) -> dict[str, Any]:
    path = project.resolve(str(spec["file"]))
    tolerances = {str(name): float(value) for name, value in dict(spec.get("signals") or {}).items()}
    every = float(spec.get("every", 0.01))
    picked, next_time = [], 0.0
    for row in rows:
        if float(row["time"]) + 1e-9 >= next_time:
            picked.append({"time": round(float(row["time"]), 9), **{name: float(row[name]) for name in tolerances}})
            next_time += every
    if update or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["time", *tolerances])
            writer.writeheader()
            writer.writerows(picked)
        return {"file": str(path), "written": True, "samples": len(picked), "passed": True}
    with path.open(newline="", encoding="utf-8") as handle:
        expected = list(csv.DictReader(handle))
    failures, worst = [], {name: 0.0 for name in tolerances}
    if len(expected) != len(picked):
        failures.append(f"{len(picked)} samples, golden has {len(expected)}")
    for got, want in zip(picked, expected):
        for name, tolerance in tolerances.items():
            if name not in want:
                failures.append(f"golden has no column {name}")
                continue
            error = abs(got[name] - float(want[name]))
            worst[name] = max(worst[name], error)
            if error > tolerance and len(failures) < 10:
                failures.append(f"{name} at t={got['time']:g}: {got[name]:.6g} vs {float(want[name]):.6g}")
    return {"file": str(path), "max_abs_error": worst, "failures": failures, "passed": not failures}


def scenarios(project_file: Path, items: Sequence[Mapping[str, Any]], mock: bool = False,
              progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    results = []
    for index, item in enumerate(items):
        name = str(item.get("name", f"scenario{index + 1}"))
        project = MechatronicProject.load(project_file)
        if item.get("duration") is not None:
            project.duration_seconds = float(item["duration"])
        for signal, value in dict(item.get("signals") or {}).items():
            if signal not in project.signals:
                raise ValueError(f"scenario {name}: unknown signal {signal}")
            project.signals[signal] = replace(project.signals[signal], initial=float(value))
        if item.get("parameters"):
            project.apply_parameter_overrides(dict(item["parameters"]), source=f"scenario:{name}")
        if item.get("faults"):
            project.faults = project.faults + parse_faults(item["faults"], project.signals, start_index=len(project.faults))
        record: dict[str, Any] = {"name": name, "checks": []}
        try:
            metrics = compute_metrics(run(project, mock), parse_metric_specs(project.metrics))
            for expectation in item.get("expect") or []:
                metric = str(expectation["metric"])
                value = select_metrics(metrics, [metric])[metric]
                lower, upper = expectation.get("min"), expectation.get("max")
                ok = value is not None and math.isfinite(value) and \
                    (lower is None or value >= float(lower)) and (upper is None or value <= float(upper))
                record["checks"].append({"metric": metric, "value": value, "min": lower, "max": upper, "passed": ok})
            record["passed"] = all(check["passed"] for check in record["checks"])
        except Exception as exc:
            record.update({"passed": False, "error": f"{type(exc).__name__}: {exc}"})
        results.append(record)
        if progress:
            progress(record)
    return {"scenarios": results, "passed": all(item["passed"] for item in results)}


def verify(project_file: Path, mock: bool = False, only: Sequence[str] | None = None, update_golden: bool = False,
           progress: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Run every check the project declares (or those named in `only`)."""
    project = MechatronicProject.load(project_file)
    spec = dict(project.verification or {})
    if not spec:
        raise ValueError(f"{project_file} has no verification section")
    wanted = set(only or spec)
    unknown = wanted - {"energy", "convergence", "golden", "scenarios"}
    if unknown:
        raise ValueError(f"unknown verification checks: {', '.join(sorted(unknown))}")
    report: dict[str, Any] = {"project": str(project_file), "checks": {}}
    nominal: Rows | None = None
    if wanted & {"energy", "golden"} & set(spec):
        nominal = run(project, mock)
    for name in ("energy", "golden", "convergence", "scenarios"):
        if name not in wanted or name not in spec:
            continue
        if name == "energy":
            result = energy_balance(project, nominal, spec["energy"])
        elif name == "golden":
            result = golden(project, nominal, spec["golden"], update_golden)
        elif name == "convergence":
            result = convergence(project_file, spec["convergence"], mock)
        else:
            result = scenarios(project_file, spec["scenarios"], mock,
                               (lambda record: progress("scenario", record)) if progress else None)
        report["checks"][name] = result
        if progress:
            progress(name, result)
    report["passed"] = all(result["passed"] for result in report["checks"].values())
    return report
