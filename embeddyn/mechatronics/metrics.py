"""Response metrics computed from a co-simulation run.

A project declares named metrics:

```yaml
metrics:
  speed:
    type: step_response          # settling/rise time, overshoot, steady-state error, IAE/ISE/ITAE
    signal: mechanical.velocity
    target: firmware.target_velocity   # signal name or a number
    settling_band: 0.02          # fraction of the step size (default 2 %)
    start: 0.0                   # time of the step (default: first sample)
  current:
    type: stats                  # peak (max |x|), rms, mean, min, max
    signal: electrical.motor_current
  energy:
    type: integral               # time integral (e.g. power -> energy in J)
    signal: electrical.input_power
  trip:
    type: threshold              # first crossing: time, delay (from the window start), crossings
    signal: mcu.fault_led
    threshold: 0.5
    direction: rising            # rising (default), falling or either
    start: 1.2                   # e.g. when a fault is injected: delay = detection time
```

`compute_metrics` returns a flat mapping such as `speed.settling_time`,
`current.peak`, `energy.integral`. Missing quantities (a response that never
settles) are reported as None, not as a number.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

METRIC_TYPES = ("step_response", "stats", "integral", "threshold")
DIRECTIONS = ("rising", "falling", "either")


@dataclass(frozen=True)
class MetricSpec:
    name: str
    type: str
    signal: str
    target: str | float | None = None
    settling_band: float = 0.02
    start: float | None = None
    end: float | None = None
    threshold: float = 0.5
    direction: str = "rising"


def parse_metric_specs(raw: Mapping[str, Any] | None) -> dict[str, MetricSpec]:
    specs: dict[str, MetricSpec] = {}
    for name, entry in dict(raw or {}).items():
        entry = dict(entry or {})
        kind = str(entry.get("type", "stats"))
        if kind not in METRIC_TYPES:
            raise ValueError(f"metric {name}: type must be one of {', '.join(METRIC_TYPES)}")
        if not entry.get("signal"):
            raise ValueError(f"metric {name}: 'signal' is required")
        target = entry.get("target")
        if kind == "step_response" and target is None:
            raise ValueError(f"metric {name}: step_response needs 'target' (signal name or number)")
        direction = str(entry.get("direction", "rising"))
        if direction not in DIRECTIONS:
            raise ValueError(f"metric {name}: direction must be one of {', '.join(DIRECTIONS)}")
        band = float(entry.get("settling_band", 0.02))
        if not 0 < band < 1:
            raise ValueError(f"metric {name}: settling_band must be between 0 and 1")
        specs[name] = MetricSpec(
            name=str(name), type=kind, signal=str(entry["signal"]),
            target=target if isinstance(target, str) or target is None else float(target),
            settling_band=band,
            start=None if entry.get("start") is None else float(entry["start"]),
            end=None if entry.get("end") is None else float(entry["end"]),
            threshold=float(entry.get("threshold", 0.5)), direction=direction,
        )
    return specs


def _column(rows: Sequence[Mapping[str, Any]], name: str) -> list[float]:
    try:
        return [float(row[name]) for row in rows]
    except KeyError as error:
        raise ValueError(f"metric signal {name!r} is not in the run output") from error


def _trapezoid(times: Sequence[float], values: Sequence[float]) -> float:
    return sum((times[i + 1] - times[i]) * (values[i] + values[i + 1]) / 2 for i in range(len(times) - 1))


def step_response(times: Sequence[float], values: Sequence[float], target: float | Sequence[float],
                  settling_band: float = 0.02) -> dict[str, float | None]:
    """Classic step-response figures for a response `values` toward `target`.

    `target` may be a constant or a per-sample sequence (its last value is the
    reference). Times are relative to the first sample.
    """
    if len(times) < 2:
        raise ValueError("step response needs at least two samples")
    targets = [float(target)] * len(values) if isinstance(target, (int, float)) else [float(x) for x in target]
    t0, y0, reference = times[0], values[0], targets[-1]
    step = reference - y0
    errors = [r - y for r, y in zip(targets, values)]
    tail = max(1, len(values) // 20)
    final_value = sum(values[-tail:]) / tail
    result: dict[str, float | None] = {
        "final_value": final_value,
        "steady_state_error": reference - final_value,
        "iae": _trapezoid(times, [abs(e) for e in errors]),
        "ise": _trapezoid(times, [e * e for e in errors]),
        "itae": _trapezoid(times, [(t - t0) * abs(e) for t, e in zip(times, errors)]),
        "rise_time": None, "overshoot": None, "peak_time": None, "settling_time": None,
    }
    if abs(step) < 1e-12:
        return result
    direction = 1.0 if step > 0 else -1.0
    progress = [(y - y0) / step for y in values]

    def first_crossing(level: float) -> float | None:
        for i in range(1, len(progress)):
            if progress[i - 1] < level <= progress[i]:
                fraction = (level - progress[i - 1]) / (progress[i] - progress[i - 1])
                return times[i - 1] + fraction * (times[i] - times[i - 1])
        return None

    t10, t90 = first_crossing(0.1), first_crossing(0.9)
    result["rise_time"] = t90 - t10 if t10 is not None and t90 is not None else None
    peak_index = max(range(len(values)), key=lambda i: direction * values[i])
    result["overshoot"] = max(0.0, direction * (values[peak_index] - reference) / abs(step)) * 100.0
    result["peak_time"] = times[peak_index] - t0
    band = settling_band * abs(step)
    outside = [i for i, y in enumerate(values) if abs(y - reference) > band]
    if not outside:
        result["settling_time"] = 0.0
    elif outside[-1] < len(values) - 1:
        result["settling_time"] = times[outside[-1] + 1] - t0
    return result


def threshold_crossings(times: Sequence[float], values: Sequence[float], threshold: float,
                        direction: str = "rising") -> dict[str, float | None]:
    """When `values` first crosses `threshold` (interpolated), the delay from the first sample and
    how often it crosses; time and delay are None if it never does."""
    crossings: list[float] = []
    for i in range(1, len(values)):
        a, b = values[i - 1] - threshold, values[i] - threshold
        rising, falling = a < 0 <= b, a >= 0 > b
        if (rising and direction != "falling") or (falling and direction != "rising"):
            fraction = a / (a - b) if a != b else 1.0
            crossings.append(times[i - 1] + fraction * (times[i] - times[i - 1]))
    first = crossings[0] if crossings else None
    return {"time": first, "delay": None if first is None else first - times[0], "crossings": float(len(crossings))}


def signal_stats(times: Sequence[float], values: Sequence[float]) -> dict[str, float]:
    span = times[-1] - times[0] if len(times) > 1 else 0.0
    rms = math.sqrt(_trapezoid(times, [v * v for v in values]) / span) if span > 0 else abs(values[-1])
    mean = _trapezoid(times, values) / span if span > 0 else values[-1]
    return {"peak": max(abs(v) for v in values), "rms": rms, "mean": mean, "min": min(values), "max": max(values)}


def compute_metrics(rows: Sequence[Mapping[str, Any]], specs: Mapping[str, MetricSpec],
                    time_key: str = "time") -> dict[str, float | None]:
    """Evaluate metric specs over run rows (dicts with `time` and signal values)."""
    if not rows:
        return {}
    all_times = _column(rows, time_key)
    output: dict[str, float | None] = {}
    for name, spec in specs.items():
        indices = [i for i, t in enumerate(all_times)
                   if (spec.start is None or t >= spec.start - 1e-12) and (spec.end is None or t <= spec.end + 1e-12)]
        if len(indices) < 2:
            raise ValueError(f"metric {name}: fewer than two samples in the selected time window")
        window = [rows[i] for i in indices]
        times = [all_times[i] for i in indices]
        values = _column(window, spec.signal)
        if spec.type == "integral":
            figures: Mapping[str, float | None] = {"integral": _trapezoid(times, values)}
        elif spec.type == "stats":
            figures = signal_stats(times, values)
        elif spec.type == "threshold":
            figures = threshold_crossings(times, values, spec.threshold, spec.direction)
        else:
            target = spec.target if isinstance(spec.target, float) else _column(window, str(spec.target))
            figures = step_response(times, values, target, spec.settling_band)
        for key, value in figures.items():
            output[f"{name}.{key}"] = value
    return output


def select_metrics(metrics: Mapping[str, float | None], names: Iterable[str]) -> dict[str, float | None]:
    """Pick metrics by full name (`speed.overshoot`) or by suffix (`overshoot`) when unique."""
    selected: dict[str, float | None] = {}
    for name in names:
        if name in metrics:
            selected[name] = metrics[name]
            continue
        matches = [key for key in metrics if key.endswith("." + name)]
        if len(matches) != 1:
            raise KeyError(f"metric {name!r} is {'ambiguous' if matches else 'not computed'}"
                           + (f": {', '.join(matches)}" if matches else ""))
        selected[name] = metrics[matches[0]]
    return selected
