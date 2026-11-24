"""Tolerance analysis: Monte Carlo yield and variance-based (Sobol) sensitivity.

```yaml
project: ../servo-drivetrain.yaml
duration: 1.0
mock: false
seed: 1
workers: 4                    # parallel processes (launched Renodes get their own ports)
parameters:                   # project parameters or constant signals
  electrical.motor_resistance: {nominal: 2.0, tolerance: 0.05, distribution: normal}   # +-5 % = 3 sigma
  drivetrain.backlash: {distribution: uniform, minimum: 0.002, maximum: 0.008}
  mechanical.load_torque: {distribution: triangular, minimum: 0.2, mode: 0.3, maximum: 0.5}
outputs: [speed.overshoot, speed.settling_time, shaft.peak]
limits:                       # requirements; yield = fraction of samples meeting all of them
  - {metric: speed.overshoot, max: 10}
  - {metric: shaft.peak, max: 25}
monte_carlo: {samples: 200, sampling: lhs}       # lhs or random
sensitivity: {base_samples: 64, bootstrap: 200}  # N * (k + 2) runs
```

Distributions: normal (mean, sigma), uniform (minimum, maximum), triangular
(minimum, mode, maximum), lognormal (median, sigma_log), choice (values), and
the shorthand `nominal` + `tolerance` (relative) with `distribution: uniform`
(+-tolerance) or `normal` (tolerance = 3 sigma). Every distribution is sampled
through its inverse CDF, so Latin hypercube and Sobol designs share one path.

Sobol indices use the Saltelli (2010) first-order and Jansen total-order
estimators on matrices A, B and A_B^(i); bootstrap gives their 95 % intervals.
S_i is the share of output variance explained by input i alone, S_Ti includes
its interactions; sum(S_i) close to 1 means an additive model.
"""
from __future__ import annotations

import json
import math
import os
import random
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from statistics import NormalDist
from typing import Any, Callable, Mapping, Sequence

import yaml

from .metrics import compute_metrics, parse_metric_specs, select_metrics

_NORMAL = NormalDist()


# -- distributions -------------------------------------------------------------

@dataclass(frozen=True)
class Distribution:
    kind: str
    a: float = 0.0
    b: float = 0.0
    c: float = 0.0
    values: tuple[float, ...] = ()

    def quantile(self, u: float) -> float:
        u = min(max(u, 1e-12), 1 - 1e-12)
        if self.kind == "normal":
            return self.a + self.b * _NORMAL.inv_cdf(u)
        if self.kind == "uniform":
            return self.a + (self.b - self.a) * u
        if self.kind == "lognormal":
            return self.a * math.exp(self.b * _NORMAL.inv_cdf(u))
        if self.kind == "triangular":
            lo, mode, hi = self.a, self.c, self.b
            split = (mode - lo) / (hi - lo)
            if u < split:
                return lo + math.sqrt(u * (hi - lo) * (mode - lo))
            return hi - math.sqrt((1 - u) * (hi - lo) * (hi - mode))
        if self.kind == "choice":
            return self.values[min(int(u * len(self.values)), len(self.values) - 1)]
        raise ValueError(f"unsupported distribution {self.kind}")

    def nominal(self) -> float:
        return self.quantile(0.5) if self.kind != "choice" else self.values[0]


def parse_distribution(name: str, spec: Mapping[str, Any]) -> Distribution:
    spec = dict(spec)
    kind = str(spec.get("distribution", "normal" if "sigma" in spec or "tolerance" in spec else "uniform"))
    if "nominal" in spec and "tolerance" in spec:
        nominal, tol = float(spec["nominal"]), float(spec["tolerance"])
        if tol < 0:
            raise ValueError(f"{name}: tolerance must be >= 0")
        spread = abs(nominal) * tol
        if kind == "uniform":
            return Distribution("uniform", nominal - spread, nominal + spread)
        if kind == "normal":
            return Distribution("normal", nominal, spread / 3)
        raise ValueError(f"{name}: nominal/tolerance supports distribution normal or uniform")
    try:
        if kind == "normal":
            dist = Distribution("normal", float(spec["mean"]), float(spec["sigma"]))
            if dist.b < 0:
                raise ValueError(f"{name}: sigma must be >= 0")
            return dist
        if kind == "uniform":
            dist = Distribution("uniform", float(spec["minimum"]), float(spec["maximum"]))
        elif kind == "triangular":
            dist = Distribution("triangular", float(spec["minimum"]), float(spec["maximum"]), float(spec["mode"]))
            if not dist.a <= dist.c <= dist.b:
                raise ValueError(f"{name}: need minimum <= mode <= maximum")
        elif kind == "lognormal":
            dist = Distribution("lognormal", float(spec["median"]), float(spec["sigma_log"]))
        elif kind == "choice":
            return Distribution("choice", values=tuple(float(v) for v in spec["values"]))
        else:
            raise ValueError(f"{name}: unsupported distribution {kind}")
    except KeyError as error:
        raise ValueError(f"{name}: {kind} distribution needs '{error.args[0]}'") from None
    if dist.kind in ("uniform", "triangular") and not dist.a < dist.b:
        raise ValueError(f"{name}: need minimum < maximum")
    return dist


# -- spec ------------------------------------------------------------------------

@dataclass
class ToleranceSpec:
    project: Path
    parameters: dict[str, Distribution]
    outputs: list[str]
    limits: list[dict[str, Any]] = field(default_factory=list)
    duration: float | None = None
    mock: bool = False
    seed: int = 1
    workers: int = 1
    monte_carlo: dict[str, Any] | None = None
    sensitivity: dict[str, Any] | None = None
    fixed: dict[str, float] = field(default_factory=dict)


def load_tolerance_spec(path: Path) -> ToleranceSpec:
    path = path.resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not raw.get("project") or not raw.get("parameters"):
        raise ValueError("tolerance spec needs 'project' and 'parameters'")
    project = Path(str(raw["project"]))
    if not project.is_absolute():
        project = (path.parent / project).resolve()
    parameters = {str(n): parse_distribution(str(n), s or {}) for n, s in dict(raw["parameters"]).items()}
    limits = [dict(item) for item in raw.get("limits") or []]
    outputs = [str(x) for x in raw.get("outputs") or []]
    for item in limits:
        if str(item["metric"]) not in outputs:
            outputs.append(str(item["metric"]))
    if not outputs:
        raise ValueError("tolerance spec needs 'outputs' or 'limits'")
    if not raw.get("monte_carlo") and not raw.get("sensitivity"):
        raise ValueError("tolerance spec needs 'monte_carlo' and/or 'sensitivity'")
    return ToleranceSpec(project, parameters, outputs, limits,
                         None if raw.get("duration") is None else float(raw["duration"]), bool(raw.get("mock", False)),
                         int(raw.get("seed", 1)), max(1, int(raw.get("workers", 1))),
                         dict(raw["monte_carlo"]) if raw.get("monte_carlo") else None,
                         dict(raw["sensitivity"]) if raw.get("sensitivity") else None,
                         {str(k): float(v) for k, v in dict(raw.get("fixed") or {}).items()})


# -- evaluation (runs in worker processes) ----------------------------------------

def evaluate(task: tuple) -> dict[str, Any]:
    """One co-simulation with the given values; returns the selected metrics or an error.
    task = (project file, values, duration, mock, outputs[, private endpoints])."""
    project_file, values, duration, mock, outputs = task[:5]
    private = bool(task[5]) if len(task) > 5 else False
    from .factory import build_adapters, use_private_endpoints
    from .model import MechatronicProject
    from .orchestrator import CoSimulationOrchestrator
    try:
        project = MechatronicProject.load(Path(project_file))
        if private:
            use_private_endpoints(project)
        if duration is not None:
            project.duration_seconds = duration
        overrides = {}
        for name, value in values.items():
            if name in project.signals:
                project.signals[name] = replace(project.signals[name], initial=float(value))
            else:
                overrides[name] = value
        if overrides:
            project.apply_parameter_overrides(overrides, source="tolerance")
        rows = CoSimulationOrchestrator(project, build_adapters(project, mock=mock)).run()
        metrics = compute_metrics(rows, parse_metric_specs(project.metrics))
        return {"metrics": select_metrics(metrics, outputs)}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _run_all(spec: ToleranceSpec, samples: list[dict[str, float]],
             progress: Callable[[int, int], None] | None = None) -> list[dict[str, Any]]:
    private = spec.workers > 1
    tasks = [(str(spec.project), {**spec.fixed, **s}, spec.duration, spec.mock, spec.outputs, private) for s in samples]
    results: list[dict[str, Any]] = []
    if spec.workers <= 1:
        for index, task in enumerate(tasks):
            results.append(evaluate(task))
            if progress:
                progress(index + 1, len(tasks))
        return results
    with ProcessPoolExecutor(max_workers=spec.workers) as pool:
        for index, result in enumerate(pool.map(evaluate, tasks, chunksize=max(1, len(tasks) // (spec.workers * 8)))):
            results.append(result)
            if progress:
                progress(index + 1, len(tasks))
    return results


# -- sampling designs ---------------------------------------------------------------

def unit_samples(count: int, dimensions: int, rng: random.Random, method: str = "random") -> list[list[float]]:
    if method == "random":
        return [[rng.random() for _ in range(dimensions)] for _ in range(count)]
    if method == "lhs":
        columns = []
        for _ in range(dimensions):
            strata = [(i + rng.random()) / count for i in range(count)]
            rng.shuffle(strata)
            columns.append(strata)
        return [list(row) for row in zip(*columns)]
    raise ValueError("sampling must be 'random' or 'lhs'")


def _to_values(spec: ToleranceSpec, unit: Sequence[float]) -> dict[str, float]:
    return {name: dist.quantile(u) for (name, dist), u in zip(spec.parameters.items(), unit)}


# -- statistics ------------------------------------------------------------------------

def _percentile(values: Sequence[float], p: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (position - lo) * (ordered[hi] - ordered[lo])


def describe(values: Sequence[float]) -> dict[str, float]:
    n = len(values)
    mean = sum(values) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / max(1, n - 1))
    return {"mean": mean, "std": std, "min": min(values), "max": max(values),
            "p05": _percentile(values, 0.05), "p50": _percentile(values, 0.5), "p95": _percentile(values, 0.95)}


def wilson(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if total == 0:
        return (0.0, 1.0)
    p = successes / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return (max(0.0, centre - half), min(1.0, centre + half))


def _meets(limit: Mapping[str, Any], value: float | None) -> bool:
    if value is None or not math.isfinite(value):
        return False
    return (limit.get("min") is None or value >= float(limit["min"])) and \
        (limit.get("max") is None or value <= float(limit["max"]))


# -- Monte Carlo ------------------------------------------------------------------------

def monte_carlo(spec: ToleranceSpec, progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    options = spec.monte_carlo or {}
    count = int(options.get("samples", 100))
    rng = random.Random(spec.seed)
    units = unit_samples(count, len(spec.parameters), rng, str(options.get("sampling", "lhs")))
    samples = [_to_values(spec, u) for u in units]
    results = _run_all(spec, samples, progress)
    errors = [r["error"] for r in results if "error" in r]
    outputs: dict[str, Any] = {}
    for name in spec.outputs:
        values = [r["metrics"][name] for r in results if "metrics" in r and r["metrics"].get(name) is not None]
        outputs[name] = {**describe(values), "missing": count - len(values)} if values else {"missing": count}
    passed = [all(_meets(limit, r.get("metrics", {}).get(str(limit["metric"]))) for limit in spec.limits)
              if "metrics" in r else False for r in results]
    per_limit = []
    for limit in spec.limits:
        ok = sum(_meets(limit, r.get("metrics", {}).get(str(limit["metric"]))) for r in results)
        per_limit.append({**limit, "pass_rate": ok / count})
    failures = [{"sample": i, "parameters": samples[i], **results[i]} for i, ok in enumerate(passed) if not ok][:20]
    report = {
        "samples": count, "sampling": str(options.get("sampling", "lhs")), "errors": len(errors),
        "first_error": errors[0] if errors else None, "outputs": outputs,
        "parameters": {name: describe([s[name] for s in samples]) for name in spec.parameters},
        "sample_set": [{"parameters": s, **r} for s, r in zip(samples, results)],
    }
    if spec.limits:
        low, high = wilson(sum(passed), count)
        report.update({"yield": sum(passed) / count, "yield_ci95": [low, high], "limits": per_limit,
                       "failing_samples": failures})
    return report


# -- Sobol --------------------------------------------------------------------------------

def sobol_indices(fa: Sequence[float], fb: Sequence[float], fab: Sequence[Sequence[float]]) -> tuple[list[float], list[float]]:
    """First-order (Saltelli 2010) and total (Jansen 1999) indices."""
    n = len(fa)
    pooled = list(fa) + list(fb)
    mean = sum(pooled) / len(pooled)
    variance = sum((v - mean) ** 2 for v in pooled) / (len(pooled) - 1)
    if variance <= 0:
        return [0.0] * len(fab), [0.0] * len(fab)
    # Centre the outputs: the estimators are shift-invariant in expectation, but an
    # output with a large mean and a small spread (a temperature of 28 +- 0.2 degC)
    # otherwise makes the first-order estimate useless.
    fa = [v - mean for v in fa]
    fb = [v - mean for v in fb]
    fab = [[v - mean for v in column] for column in fab]
    first = [sum(fb[j] * (column[j] - fa[j]) for j in range(n)) / n / variance for column in fab]
    total = [sum((fa[j] - column[j]) ** 2 for j in range(n)) / (2 * n) / variance for column in fab]
    return first, total


def sensitivity(spec: ToleranceSpec, progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    options = spec.sensitivity or {}
    n = int(options.get("base_samples", 64))
    names = list(spec.parameters)
    k = len(names)
    rng = random.Random(spec.seed + 7919)
    a = unit_samples(n, k, rng, str(options.get("sampling", "lhs")))
    b = unit_samples(n, k, rng, str(options.get("sampling", "lhs")))
    ab = [[[b[j][c] if c == i else a[j][c] for c in range(k)] for j in range(n)] for i in range(k)]
    design = a + b + [row for block in ab for row in block]
    results = _run_all(spec, [_to_values(spec, u) for u in design], progress)
    report: dict[str, Any] = {"base_samples": n, "runs": len(design), "outputs": {}}
    boot_rng = random.Random(spec.seed + 104729)
    resamples = int(options.get("bootstrap", 200))
    for output in spec.outputs:
        values = [r.get("metrics", {}).get(output) for r in results]
        if any(v is None or not math.isfinite(v) for v in values):
            missing = sum(v is None or not math.isfinite(v) for v in values)
            report["outputs"][output] = {"skipped": f"{missing} of {len(values)} runs gave no value"}
            continue
        fa, fb = values[:n], values[n:2 * n]
        fab = [values[2 * n + i * n: 2 * n + (i + 1) * n] for i in range(k)]
        first, total = sobol_indices(fa, fb, fab)
        boots_first, boots_total = [[] for _ in range(k)], [[] for _ in range(k)]
        for _ in range(resamples):
            pick = [boot_rng.randrange(n) for _ in range(n)]
            s1, st = sobol_indices([fa[p] for p in pick], [fb[p] for p in pick], [[col[p] for p in pick] for col in fab])
            for i in range(k):
                boots_first[i].append(s1[i])
                boots_total[i].append(st[i])
        indices = {}
        for i, name in enumerate(names):
            indices[name] = {
                "first_order": first[i], "total": total[i],
                "first_order_ci95": [_percentile(boots_first[i], 0.025), _percentile(boots_first[i], 0.975)] if resamples else None,
                "total_ci95": [_percentile(boots_total[i], 0.025), _percentile(boots_total[i], 0.975)] if resamples else None,
            }
        ranking = sorted(names, key=lambda name: indices[name]["total"], reverse=True)
        report["outputs"][output] = {"indices": indices, "sum_first_order": sum(first), "ranking": ranking,
                                     "variance": describe(fa + fb)["std"] ** 2}
    errors = [r["error"] for r in results if "error" in r]
    report.update({"errors": len(errors), "first_error": errors[0] if errors else None})
    return report


def analyse(spec: ToleranceSpec, progress: Callable[[str, int, int], None] | None = None) -> dict[str, Any]:
    started = time.monotonic()
    report: dict[str, Any] = {"project": str(spec.project), "parameters": list(spec.parameters),
                              "outputs": spec.outputs, "workers": spec.workers}
    if spec.monte_carlo:
        report["monte_carlo"] = monte_carlo(spec, (lambda i, n: progress("monte_carlo", i, n)) if progress else None)
    if spec.sensitivity:
        report["sensitivity"] = sensitivity(spec, (lambda i, n: progress("sensitivity", i, n)) if progress else None)
    report["seconds"] = round(time.monotonic() - started, 2)
    return report


def write_result(result: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=lambda value: None), encoding="utf-8")


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)
