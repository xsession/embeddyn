"""Parameter tuning: run the co-simulation repeatedly and minimise a metric objective.

```yaml
project: ../dc-motor.yaml
mock: false                     # true forces the built-in simulators
duration: 1.0                   # optional: override the project duration
parameters:                     # project parameters (they reach adapters as inputs)
  controller.kp: {initial: 0.12, min: 0.01, max: 2.0, scale: log}
  controller.ki: {initial: 0.8, min: 0.05, max: 20.0, scale: log}
objective:                      # minimise sum(weight * metric)
  - {metric: speed.itae, weight: 1.0}
constraints:                    # violations add a penalty (missing value = violated)
  - {metric: speed.overshoot, max: 5.0}
  - {metric: speed.settling_time, max: 0.6}
max_evaluations: 40
workers: 4                      # optional: parallel evaluations (processes)
method: nelder-mead             # or cmaes (population-based; keeps every worker busy)
population: 8                   # cmaes only: candidates per generation (default 4 + 3 ln n)
sigma: 0.25                     # cmaes only: initial step in the 0..1 space
seed: 1                         # cmaes only
```

The default optimiser is a bounded Nelder-Mead in a normalised space (0..1 per
parameter, logarithmic when `scale: log`). It is deterministic: the same file
gives the same evaluation sequence. Every evaluation is a full co-simulation,
so firmware gains can be tuned in real Renode firmware through bindings to the
parameter names.

With `workers` > 1 the evaluations run in a process pool. Each iteration's
reflection, expansion and contraction (and a shrink's points) run together,
and the search then takes the same decisions as with one worker. The simplex
path and the counted evaluations are identical; the extra (speculative) runs
only use idle workers. Each run gets its own Renode port and log
(`factory.use_private_endpoints`). The result's `best` is the best of all
runs, speculative ones included.
"""
from __future__ import annotations

import json
import math
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from .factory import build_adapters, use_private_endpoints
from .metrics import compute_metrics, parse_metric_specs, select_metrics
from .model import MechatronicProject
from .orchestrator import CoSimulationOrchestrator

CONSTRAINT_PENALTY = 1e3      # per unit of normalised violation
MISSING_PENALTY = 1e6         # metric not computed (e.g. never settles)


@dataclass(frozen=True)
class TunedParameter:
    name: str
    initial: float
    minimum: float
    maximum: float
    log: bool = False

    def to_unit(self, value: float) -> float:
        value = min(max(value, self.minimum), self.maximum)
        if self.log:
            return (math.log(value) - math.log(self.minimum)) / (math.log(self.maximum) - math.log(self.minimum))
        return (value - self.minimum) / (self.maximum - self.minimum)

    def from_unit(self, unit: float) -> float:
        unit = min(max(unit, 0.0), 1.0)
        if self.log:
            return math.exp(math.log(self.minimum) + unit * (math.log(self.maximum) - math.log(self.minimum)))
        return self.minimum + unit * (self.maximum - self.minimum)


@dataclass(frozen=True)
class ObjectiveTerm:
    metric: str
    weight: float = 1.0


@dataclass(frozen=True)
class Constraint:
    metric: str
    minimum: float | None = None
    maximum: float | None = None


@dataclass
class TuningSpec:
    project: Path
    parameters: list[TunedParameter]
    objective: list[ObjectiveTerm]
    constraints: list[Constraint] = field(default_factory=list)
    mock: bool = False
    duration: float | None = None
    max_evaluations: int = 40
    tolerance: float = 1e-4
    workers: int = 1
    method: str = "nelder-mead"
    population: int | None = None
    sigma: float = 0.25
    seed: int = 1


def load_tuning_spec(path: Path) -> TuningSpec:
    path = path.resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    project = Path(str(raw.get("project", "")))
    if not str(project):
        raise ValueError("tuning spec needs 'project'")
    if not project.is_absolute():
        project = (path.parent / project).resolve()
    parameters = []
    for name, entry in dict(raw.get("parameters") or {}).items():
        entry = dict(entry or {})
        minimum, maximum = float(entry["min"]), float(entry["max"])
        log = str(entry.get("scale", "linear")).lower() in {"log", "logarithmic"}
        if not minimum < maximum or (log and minimum <= 0):
            raise ValueError(f"parameter {name}: need min < max (and min > 0 for log scale)")
        initial = float(entry.get("initial", math.sqrt(minimum * maximum) if log else (minimum + maximum) / 2))
        parameters.append(TunedParameter(str(name), initial, minimum, maximum, log))
    if not parameters:
        raise ValueError("tuning spec needs at least one parameter")
    objective = [ObjectiveTerm(str(item["metric"]), float(item.get("weight", 1.0)))
                 for item in raw.get("objective") or []]
    if not objective:
        raise ValueError("tuning spec needs an objective")
    constraints = [Constraint(str(item["metric"]),
                              None if item.get("min") is None else float(item["min"]),
                              None if item.get("max") is None else float(item["max"]))
                   for item in raw.get("constraints") or []]
    return TuningSpec(project, parameters, objective, constraints, bool(raw.get("mock", False)),
                      None if raw.get("duration") is None else float(raw["duration"]),
                      int(raw.get("max_evaluations", 40)), float(raw.get("tolerance", 1e-4)),
                      max(1, int(raw.get("workers", 1))), _method(raw.get("method", "nelder-mead")),
                      None if raw.get("population") is None else int(raw["population"]),
                      float(raw.get("sigma", 0.25)), int(raw.get("seed", 1)))


def _method(value: Any) -> str:
    method = str(value).lower().replace("_", "-")
    aliases = {"nelder-mead": "nelder-mead", "neldermead": "nelder-mead", "nm": "nelder-mead",
               "cmaes": "cmaes", "cma-es": "cmaes", "cma": "cmaes"}
    if method not in aliases:
        raise ValueError(f"unknown tuning method {value!r} (nelder-mead or cmaes)")
    return aliases[method]


def simulate(spec: TuningSpec, values: Mapping[str, float], private: bool = False) -> dict[str, float | None]:
    """One full co-simulation with the given parameter values; returns its metrics.
    `private`: own ports and logs for launched simulators (runs in parallel)."""
    project = MechatronicProject.load(spec.project)
    if private:
        use_private_endpoints(project)
    if spec.duration is not None:
        project.duration_seconds = spec.duration
    project.apply_parameter_overrides(dict(values), source="tuning")
    if not project.metrics:
        raise ValueError(f"{spec.project} declares no metrics to tune against")
    rows = CoSimulationOrchestrator(project, build_adapters(project, mock=spec.mock)).run()
    return compute_metrics(rows, parse_metric_specs(project.metrics))


def score(spec: TuningSpec, metrics: Mapping[str, float | None]) -> tuple[float, list[str]]:
    total, violations = 0.0, []
    for term in spec.objective:
        value = select_metrics(metrics, [term.metric])[term.metric]
        if value is None or not math.isfinite(value):
            total += MISSING_PENALTY
            violations.append(f"{term.metric} not available")
        else:
            total += term.weight * value
    for constraint in spec.constraints:
        value = select_metrics(metrics, [constraint.metric])[constraint.metric]
        if value is None or not math.isfinite(value):
            total += MISSING_PENALTY
            violations.append(f"{constraint.metric} not available")
            continue
        for bound, excess in ((constraint.maximum, None if constraint.maximum is None else value - constraint.maximum),
                              (constraint.minimum, None if constraint.minimum is None else constraint.minimum - value)):
            if excess is not None and excess > 0:
                total += CONSTRAINT_PENALTY * (1.0 + excess / max(abs(bound), 1e-9))
                violations.append(f"{constraint.metric}={value:.6g} violates {'max' if bound == constraint.maximum else 'min'} {bound:g}")
    return total, violations


def nelder_mead(function: Callable[[list[float]], float], start: Sequence[float], max_evaluations: int,
                tolerance: float = 1e-4, initial_step: float = 0.15,
                batch: Callable[[list[list[float]]], list[float]] | None = None,
                used: list[tuple[float, ...]] | None = None) -> tuple[list[float], float]:
    """Minimise `function` over the unit box (coordinates clipped to [0, 1]).

    `batch` evaluates several points at once (in parallel). With it, each
    iteration evaluates its reflection, expansion and contraction together,
    and the shrink points together, and then takes exactly the decisions of the
    sequential search. Points that search would not have needed are
    speculative: they cost wall time on idle workers but don't count against
    `max_evaluations`. So the simplex path, and the evaluations that count,
    are the same with or without `batch`. `used` receives those counted points
    in order. Repeated points are evaluated once (the objective must be
    deterministic, as a co-simulation is).
    """
    clip = lambda point: tuple(min(max(x, 0.0), 1.0) for x in point)
    cache: dict[tuple[float, ...], float] = {}
    evaluations = 0

    def prefetch(points) -> None:
        missing = list(dict.fromkeys(key for key in map(clip, points) if key not in cache))
        if not missing:
            return
        values = batch([list(key) for key in missing]) if batch else [function(list(key)) for key in missing]
        cache.update(zip(missing, values))

    def evaluate(point) -> float:
        nonlocal evaluations
        evaluations += 1
        key = clip(point)
        if key not in cache:
            prefetch([key])
        if used is not None:
            used.append(key)
        return cache[key]

    dimension = len(start)
    simplex = [list(clip(start))]
    for i in range(dimension):
        vertex = list(simplex[0])
        vertex[i] = vertex[i] + initial_step if vertex[i] + initial_step <= 1.0 else vertex[i] - initial_step
        simplex.append(vertex)
    if batch:
        prefetch(simplex[:max_evaluations])
    values = [evaluate(point) for point in simplex]
    while evaluations < max_evaluations:
        order = sorted(range(len(simplex)), key=lambda i: values[i])
        simplex, values = [simplex[i] for i in order], [values[i] for i in order]
        size = max(max(abs(a - b) for a, b in zip(vertex, simplex[0])) for vertex in simplex[1:])
        if size < tolerance:
            break
        centroid = [sum(vertex[k] for vertex in simplex[:-1]) / dimension for k in range(dimension)]
        worst = simplex[-1]
        reflected = list(clip([c + (c - w) for c, w in zip(centroid, worst)]))
        expanded = list(clip([c + 2 * (c - w) for c, w in zip(centroid, worst)]))
        contracted = list(clip([c + 0.5 * (w - c) for c, w in zip(centroid, worst)]))
        if batch:
            prefetch([reflected, expanded, contracted])
        reflected_value = evaluate(reflected)
        if reflected_value < values[0] and evaluations < max_evaluations:
            expanded_value = evaluate(expanded)
            simplex[-1], values[-1] = (expanded, expanded_value) if expanded_value < reflected_value else (reflected, reflected_value)
        elif reflected_value < values[-2]:
            simplex[-1], values[-1] = reflected, reflected_value
        else:
            contracted_value = evaluate(contracted) if evaluations < max_evaluations else math.inf
            if contracted_value < values[-1]:
                simplex[-1], values[-1] = contracted, contracted_value
            else:
                shrunk = [list(clip([b + 0.5 * (x - b) for b, x in zip(simplex[0], vertex)])) for vertex in simplex[1:]]
                if batch:
                    prefetch(shrunk[:max(0, max_evaluations - evaluations)])
                for i in range(1, len(simplex)):
                    if evaluations >= max_evaluations:
                        break
                    simplex[i] = shrunk[i - 1]
                    values[i] = evaluate(simplex[i])
    best = min(range(len(simplex)), key=lambda i: values[i])
    return simplex[best], values[best]


def cmaes(batch: Callable[[list[list[float]]], list[float]], start: Sequence[float], max_evaluations: int,
          sigma: float = 0.25, population: int | None = None, seed: int = 1, tolerance: float = 1e-4,
          used: list[tuple[float, ...]] | None = None) -> tuple[list[float], float]:
    """Minimise over the unit box with CMA-ES (Hansen's (mu/mu_w, lambda) with rank-one and rank-mu
    updates and cumulative step-size adaptation).

    Every generation's `population` candidates go to `batch` together, so a
    process pool stays busy. Candidates outside [0, 1] are mirrored back
    inside, and the search uses the mirrored point. The generator is seeded,
    so a run is reproducible and independent of the number of workers. Stops
    after the last whole generation within `max_evaluations`, or when the
    largest step falls below `tolerance`.
    """
    import numpy as np
    n = len(start)
    lam = int(population or 4 + math.floor(3 * math.log(n)))
    if lam < 2:
        raise ValueError("cmaes needs a population of at least 2")
    mu = lam // 2
    weights = np.log(mu + 0.5) - np.log(np.arange(1, mu + 1))
    weights /= weights.sum()
    mueff = 1.0 / float(np.sum(weights ** 2))
    cc = (4 + mueff / n) / (n + 4 + 2 * mueff / n)
    cs = (mueff + 2) / (n + mueff + 5)
    c1 = 2 / ((n + 1.3) ** 2 + mueff)
    cmu = min(1 - c1, 2 * (mueff - 2 + 1 / mueff) / ((n + 2) ** 2 + mueff))
    damps = 1 + 2 * max(0.0, math.sqrt((mueff - 1) / (n + 1)) - 1) + cs
    chin = math.sqrt(n) * (1 - 1 / (4 * n) + 1 / (21 * n * n))
    rng = np.random.default_rng(seed)
    mean = np.clip(np.asarray(start, dtype=float), 0.0, 1.0)
    pc, ps = np.zeros(n), np.zeros(n)
    B, D, C = np.eye(n), np.ones(n), np.eye(n)
    best_point, best_value = list(mean), math.inf
    evaluations, generation = 0, 0

    def mirror(x):
        x = np.mod(x, 2.0)                       # reflect at 0 and 1 (period 2)
        return np.where(x > 1.0, 2.0 - x, x)

    while evaluations + lam <= max_evaluations:
        z = rng.standard_normal((lam, n))
        y = z @ np.diag(D) @ B.T                 # ~ N(0, C)
        x = mirror(mean + sigma * y)
        y = (x - mean) / sigma                   # the step actually taken, after mirroring
        values = batch([list(map(float, point)) for point in x])
        evaluations += lam
        generation += 1
        if used is not None:
            used.extend(tuple(map(float, point)) for point in x)
        order = np.argsort(np.asarray(values, dtype=float), kind="stable")
        if values[order[0]] < best_value:
            best_value, best_point = float(values[order[0]]), list(map(float, x[order[0]]))
        y_sel = y[order[:mu]]
        y_w = weights @ y_sel
        mean = mean + sigma * y_w
        inv_sqrt_c = B @ np.diag(1 / D) @ B.T
        ps = (1 - cs) * ps + math.sqrt(cs * (2 - cs) * mueff) * (inv_sqrt_c @ y_w)
        hsig = float(np.linalg.norm(ps) / math.sqrt(1 - (1 - cs) ** (2 * generation)) / chin < 1.4 + 2 / (n + 1))
        pc = (1 - cc) * pc + hsig * math.sqrt(cc * (2 - cc) * mueff) * y_w
        rank_mu = (y_sel.T * weights) @ y_sel
        C = ((1 - c1 - cmu) * C + c1 * (np.outer(pc, pc) + (1 - hsig) * cc * (2 - cc) * C) + cmu * rank_mu)
        sigma *= math.exp((cs / damps) * (np.linalg.norm(ps) / chin - 1))
        C = (C + C.T) / 2
        eigenvalues, B = np.linalg.eigh(C)
        D = np.sqrt(np.maximum(eigenvalues, 1e-20))
        if sigma * float(D.max()) < tolerance:
            break
    return best_point, best_value


def _evaluate(task: tuple[TuningSpec, dict[str, float], bool]) -> dict[str, Any]:
    """One evaluation: metrics, objective and violations, or the error. Runs in worker processes."""
    spec, values, private = task
    started = time.monotonic()
    try:
        metrics = simulate(spec, values, private)
        value, violations = score(spec, metrics)
        outcome: dict[str, Any] = {"objective": value, "violations": violations, "metrics": metrics}
    except Exception as exc:  # a diverging candidate must not stop the search
        outcome = {"objective": math.inf, "violations": [], "metrics": {}, "error": f"{type(exc).__name__}: {exc}"}
    outcome["seconds"] = round(time.monotonic() - started, 3)
    return outcome


def tune(spec: TuningSpec, progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    history: list[dict[str, Any]] = []
    started = time.monotonic()

    def record(unit_point: list[float], outcome: dict[str, Any]) -> float:
        values = {p.name: p.from_unit(u) for p, u in zip(spec.parameters, unit_point)}
        entry = {"evaluation": len(history) + 1, "parameters": values, "objective": outcome["objective"],
                 "violations": outcome["violations"], "metrics": outcome["metrics"], "seconds": outcome["seconds"],
                 "_unit": tuple(unit_point)}
        if "error" in outcome:
            entry["error"] = outcome["error"]
        history.append(entry)
        if progress:
            progress({k: v for k, v in entry.items() if k != "_unit"})
        return outcome["objective"]

    def values_of(unit_point: list[float]) -> dict[str, float]:
        return {p.name: p.from_unit(u) for p, u in zip(spec.parameters, unit_point)}

    def objective(unit_point: list[float]) -> float:
        return record(unit_point, _evaluate((spec, values_of(unit_point), False)))

    pool = None
    batch = None
    if spec.workers > 1:
        # spawn: workers start clean (no inherited sockets, threads or simulator state).
        pool = ProcessPoolExecutor(max_workers=spec.workers, mp_context=multiprocessing.get_context("spawn"))

        def batch(points: list[list[float]]) -> list[float]:
            outcomes = pool.map(_evaluate, [(spec, values_of(point), True) for point in points])
            return [record(point, outcome) for point, outcome in zip(points, outcomes)]

    used: list[tuple[float, ...]] = []
    start = [p.to_unit(p.initial) for p in spec.parameters]
    try:
        if spec.method == "cmaes":
            objective(start)                     # the baseline the result reports as `initial`
            used.append(tuple(min(max(u, 0.0), 1.0) for u in start))
            sequential = lambda points: [objective(point) for point in points]
            cmaes(batch or sequential, start, spec.max_evaluations - 1, spec.sigma, spec.population, spec.seed,
                  spec.tolerance, used=used)
        else:
            nelder_mead(objective, start, spec.max_evaluations, spec.tolerance, batch=batch, used=used)
    finally:
        if pool is not None:
            pool.shutdown(cancel_futures=True)
    counted = set(used)
    for entry in history:
        entry["counted"] = entry.pop("_unit") in counted
    finite = [record for record in history if math.isfinite(record["objective"])]
    if not finite:
        raise RuntimeError("every tuning evaluation failed; first error: " + str(history[0].get("error")))
    best = min(finite, key=lambda record: record["objective"])
    return {
        "project": str(spec.project),
        "method": spec.method,
        "workers": spec.workers,
        "evaluations": len(used),
        "runs": len(history),
        "speculative_runs": sum(not entry["counted"] for entry in history),
        "wall_seconds": round(time.monotonic() - started, 3),
        "initial": history[0],
        "best": best,
        "feasible": not best["violations"],
        "history": history,
    }


def write_result(result: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, default=lambda value: None), encoding="utf-8")
