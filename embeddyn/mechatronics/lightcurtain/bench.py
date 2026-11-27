"""Light-curtain test bench: scan algorithms against a scene, without or with Webots.

A bench file (YAML) describes the scene (primitives), the curtain heads and
their mounting on a SPECT-like gantry, the LEDs and receivers, the receiver
chain, the scan algorithm and a scenario:

* ``static``   heads parked; noise and margin statistics over many frames.
* ``approach`` heads move in at constant speed until the scan reports a dark
               beam; how far the head travelled past the first real interruption.
* ``rod``      a test rod crosses the curtain plane in small steps; detection
               rate per rod diameter, i.e. the resolution of the curtain.
* ``orbit``    the gantry turns and each head follows the body contour
               (step in while clear, back off while dark); the gap kept.

`run_bench()` returns metrics and per-frame rows; `sweep()` runs a grid of
variants (LED half angle, intensity, algorithm, ...). The Webots controller in
examples/mechatronics/controllers/light_curtain uses the same `HeadScanner`
and `ContourFollower`, with geometry read from the Webots scene instead.
"""
from __future__ import annotations

import copy
import itertools
import math
import time as _time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from ..optics import CurtainOptics, LightCurtain
from .array import (CurtainHead, CurtainLayout, EmitterSpec, LazyCoupling, ReceiverSpec, ReflectionModel,
                    chain_optics)
from .scanning import Sample, ScanAlgorithm, Slot, make_algorithm
from .scene import Cylinder, Scene, rotation_matrix, scene_from_mapping


def _q(raw: Any, unit: str) -> float:
    from ..quantities import Quantity
    return Quantity.parse(raw, unit).to(unit)


# -- configuration ---------------------------------------------------------------------------
@dataclass
class HeadMount:
    """A curtain on a detector head: gantry angle about the patient axis (x), radius of the face."""
    name: str
    angle: float = 0.0              # rad, head direction measured from +z about +x
    radius: float = 0.35            # m, patient axis to detector face
    below_face: float = 0.012       # m, curtain plane under the face
    radius_min: float = 0.10
    radius_max: float = 0.50

    def pose(self, gantry: float = 0.0, radius: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        rot = rotation_matrix([1, 0, 0], self.angle + gantry)
        r = self.radius if radius is None else radius
        return rot @ np.array([0.0, 0.0, r - self.below_face]), rot


@dataclass
class BenchConfig:
    name: str
    scene: Scene
    mounts: list[HeadMount]
    layout: CurtainLayout
    emitter: EmitterSpec
    receiver: ReceiverSpec
    chain: CurtainOptics
    algorithm: Any
    slot: float = 200e-6
    settle: float | None = None
    adc_threshold: float | None = None
    rays: int = 1
    seed: int = 0
    scenario: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    reflections: ReflectionModel = field(default_factory=ReflectionModel)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "BenchConfig":
        raw = copy.deepcopy(dict(raw))
        curtain = dict(raw.get("curtain") or {})
        scan = dict(raw.get("scan") or {})
        emitter = EmitterSpec.from_mapping(curtain.get("emitter"))
        receiver = ReceiverSpec.from_mapping(curtain.get("receiver"))
        chain_raw = dict(curtain.get("chain") or {})
        chain_raw.setdefault("seed", int(raw.get("seed", 0)))
        mounts = []
        for i, head in enumerate(curtain.get("heads") or [{"name": "a"}]):
            mounts.append(HeadMount(name=str(head.get("name", f"h{i}")),
                                    angle=_q(head.get("angle", 0.0), "rad"),
                                    radius=_q(head.get("radius", 0.35), "m"),
                                    below_face=_q(head.get("below_face", curtain.get("below_face", 0.012)), "m"),
                                    radius_min=_q(head.get("radius_min", 0.10), "m"),
                                    radius_max=_q(head.get("radius_max", 0.50), "m")))
        algorithm = scan.get("algorithm", "sequential")
        params = dict(scan.get("params") or {})
        if isinstance(algorithm, Mapping):
            params = {**{k: v for k, v in algorithm.items() if k != "name"}, **params}
            algorithm = algorithm["name"]
        return cls(
            name=str(raw.get("name", "light-curtain")),
            scene=scene_from_mapping((raw.get("scene") or {}).get("obstacles")),
            mounts=mounts,
            layout=CurtainLayout.from_mapping(curtain),
            emitter=emitter, receiver=receiver,
            chain=chain_optics(chain_raw, emitter, receiver),
            algorithm={"name": algorithm, **params},
            slot=_q(scan.get("slot", 200e-6), "s"),
            settle=None if scan.get("settle") is None else _q(scan["settle"], "s"),
            adc_threshold=None if scan.get("adc_threshold") is None else _q(scan["adc_threshold"], "V"),
            rays=int(scan.get("rays", 1)),
            seed=int(raw.get("seed", 0)),
            scenario=dict(raw.get("scenario") or {"type": "static"}),
            raw=raw,
            reflections=ReflectionModel.from_mapping(curtain.get("reflections")),
        )

    def head(self, mount: HeadMount, gantry: float = 0.0, radius: float | None = None) -> CurtainHead:
        origin, rot = mount.pose(gantry, radius)
        return CurtainHead.build(mount.name, self.layout, self.emitter, self.receiver, origin, rot, self.seed)

    def make_algorithm(self) -> ScanAlgorithm:
        e, r = self.layout.count, self.layout.receiver_count
        return make_algorithm(dict(self.algorithm), e, r, slot=self.slot, settle=self.settle,
                              threshold=self.adc_threshold)

    def scanner(self) -> "HeadScanner":
        return HeadScanner(self.make_algorithm(), self.chain, self.rays, self.reflections)


# -- one head, frame by frame --------------------------------------------------------------
# A beam whose pencil is only partly blocked may read either way: the comparator's
# thresholds decide. Errors are counted outside a don't-care band of transmittance.
MISSED_BELOW = 0.25     # reported clear while less than this fraction gets through: missed
FALSE_ABOVE = 0.75      # reported dark while more than this gets through: false dark


@dataclass
class FrameResult:
    head: str
    start: float
    end: float
    dark: list[bool]
    truth: list[bool]
    slots: int
    shadowed: list[bool]    # beam pencil at least partly blocked (transmittance < 1)
    clear_min: float        # lowest voltage of a single-beam slot whose beam is fully clear (V)
    dark_max: float         # highest voltage of a single-beam slot whose beam is fully blocked (V)
    transmittance: list[float] = field(default_factory=list)

    @property
    def missed(self) -> int:
        return sum(1 for d, t in zip(self.dark, self.transmittance) if not d and t < MISSED_BELOW)

    @property
    def false_dark(self) -> int:
        return sum(1 for d, t in zip(self.dark, self.transmittance) if d and t > FALSE_ABOVE)

    @property
    def any_dark(self) -> bool:
        return any(self.dark)

    @property
    def any_truth(self) -> bool:
        return any(self.truth)


class HeadScanner:
    """Runs a scan algorithm on one curtain head: LEDs, receiver node, comparator."""

    def __init__(self, algorithm: ScanAlgorithm, chain: CurtainOptics, rays: int = 1,
                 reflections: ReflectionModel | None = None) -> None:
        self.algorithm = algorithm
        self.chain = LightCurtain(chain)
        self.rays = rays
        self.reflections = reflections
        self._stray: tuple[np.ndarray, np.ndarray, dict] | None = None
        self.time = 0.0
        self._pairs: set[tuple[int, int]] = set(algorithm.beams)
        self.last_samples: list[Sample] = []

    def frame(self, head: CurtainHead, scene: Scene, groups=None) -> FrameResult:
        """Run one whole frame of the algorithm against `head` in `scene`."""
        steps = self.iter_frame(head, scene, groups)
        while True:
            try:
                next(steps)
            except StopIteration as done:
                return done.value

    def iter_frame(self, head: CurtainHead, scene: Scene, groups=None):
        """Generator form of `frame()`: yields each `Sample` as it is taken, returns the `FrameResult`."""
        algo, chain = self.algorithm, self.chain
        stray = None
        if self.reflections is not None and self.reflections.enabled and self._stray is not None:
            origin, rot, rows = self._stray
            if np.linalg.norm(origin - head.origin) <= self.reflections.reuse and np.allclose(rot, head.rotation, atol=1e-3):
                stray = rows
        lazy = LazyCoupling(head, scene, self.rays, groups, warm=self._pairs, reflections=self.reflections, stray=stray)
        if self.reflections is not None and self.reflections.enabled and stray is None:
            self._stray = (head.origin.copy(), head.rotation.copy(), lazy.stray)
        start = self.time
        samples: list[Sample] = []
        clear_min, dark_max = math.inf, -math.inf
        generator = algo.scan()
        try:
            slot = next(generator)
            while True:
                duration = slot.duration if slot.duration is not None else algo.slot
                settle = slot.settle if slot.settle is not None else (algo.settle if algo.settle is not None else duration)
                settle = min(max(settle, 0.0), duration)
                power = lazy.power(slot.lit, slot.read) * slot.drive
                current = chain.optics.responsivity * power
                chain.drive(current, self.time + settle, settle, receivers=len(slot.read))
                sample = Sample(slot, self.time + settle, chain.voltage, chain.detect, power)
                samples.append(sample)
                if len(slot.lit) == 1 and len(slot.read) == 1:
                    t = lazy.transmittance.get((slot.lit[0], slot.read[0]))
                    if t is not None and t >= 0.999:
                        clear_min = min(clear_min, chain.voltage)
                    elif t is not None and t <= 0.001:
                        dark_max = max(dark_max, chain.voltage)
                if duration > settle:
                    chain.drive(current, self.time + duration, duration - settle, receivers=len(slot.read))
                self.time += duration
                yield sample
                slot = generator.send(sample)
        except StopIteration as stop:
            dark = list(stop.value or [])
        if len(dark) != len(algo.beams):
            raise ValueError(f"scan algorithm {algo.name!r} returned {len(dark)} decisions for {len(algo.beams)} beams")
        self._pairs = set(lazy.transmittance)
        self.last_samples = samples
        self.last_coupling = lazy
        truth = lazy.truth_dark(algo.beams)
        transmittance = [lazy.transmittance[(e, r)] for e, r in algo.beams]
        shadowed = [t < 0.999 for t in transmittance]
        return FrameResult(head.name, start, self.time, [bool(d) for d in dark], truth, len(samples),
                           shadowed, clear_min, dark_max, transmittance)


@dataclass
class ContourFollower:
    """Head radial drive: step in while the curtain is clear, back off while a beam is dark."""
    approach: float = 0.03          # m/s inward while clear
    retract: float = 0.03           # m/s outward while dark
    radius_min: float = 0.10
    radius_max: float = 0.50

    def speed(self, any_dark: bool) -> float:
        return self.retract if any_dark else -self.approach

    def advance(self, radius: float, any_dark: bool, dt: float) -> float:
        return min(max(radius + self.speed(any_dark) * dt, self.radius_min), self.radius_max)


def contact_radius(config: BenchConfig, mount: HeadMount, gantry: float, scene: Scene | None = None,
                   low: float | None = None, high: float | None = None, iterations: int = 24) -> float:
    """Largest head radius at which a straight beam's centre ray is blocked (ground truth contour)."""
    scene = config.scene if scene is None else scene
    low = mount.radius_min if low is None else low
    high = mount.radius_max if high is None else high
    template = config.head(mount, gantry, high)

    def blocked(radius: float) -> bool:
        origin, rot = mount.pose(gantry, radius)
        head = template.moved(origin, rot)
        n = min(len(head.emitters), len(head.receivers))
        return bool(scene.blocked(head.emitters[:n], head.receivers[:n]).any())

    if not blocked(low):
        return low
    if blocked(high):
        return high
    for _ in range(iterations):
        mid = (low + high) / 2
        if blocked(mid):
            low = mid
        else:
            high = mid
    return (low + high) / 2


# -- scenarios ---------------------------------------------------------------------------------
def _frame_row(result: FrameResult, **extra: Any) -> dict[str, Any]:
    return {"head": result.head, "t": round(result.start, 9), "frame_time": result.end - result.start,
            "slots": result.slots, "dark": sum(result.dark), "truth_dark": sum(result.truth),
            "missed": result.missed, "false": result.false_dark,
            "clear_min": None if math.isinf(result.clear_min) else result.clear_min,
            "dark_max": None if math.isinf(result.dark_max) else result.dark_max, **extra}


def _summary(rows: list[dict[str, Any]], chain: CurtainOptics) -> dict[str, Any]:
    beams = sum(r["slots"] for r in rows) or 1
    decisions = rows
    clear = [r["clear_min"] for r in rows if r["clear_min"] is not None]
    dark = [r["dark_max"] for r in rows if r["dark_max"] is not None]
    times = [r["frame_time"] for r in rows]
    return {
        "frames": len(rows),
        "frame_time": float(np.mean(times)) if times else 0.0,
        "frame_time_max": float(np.max(times)) if times else 0.0,
        "missed_beams": int(sum(r["missed"] for r in decisions)),
        "false_beams": int(sum(r["false"] for r in decisions)),
        "frames_with_error": int(sum(1 for r in decisions if r["missed"] or r["false"])),
        # Excess gain: the weakest clear beam over the "light" threshold (>1 needed, 2..3 is comfortable).
        "clear_margin": (min(clear) / chain.threshold_on) if clear else None,
        # Dark margin: the "dark" threshold over the brightest blocked beam (>1 needed).
        "dark_margin": (chain.threshold_off / max(dark)) if dark and max(dark) > 0 else None,
        "slots_per_frame": beams / max(1, len(rows)),
    }


def scenario_static(config: BenchConfig) -> dict[str, Any]:
    spec = config.scenario
    frames = int(spec.get("frames", 50))
    gantry = _q(spec.get("gantry", 0.0), "rad")
    rows = []
    for mount in config.mounts:
        radius = _q(spec.get("radius", mount.radius), "m")
        scanner = config.scanner()
        head = config.head(mount, gantry, radius)
        for _ in range(frames):
            rows.append(_frame_row(scanner.frame(head, config.scene), radius=radius))
    return {"metrics": _summary(rows, config.chain), "rows": rows}


def scenario_approach(config: BenchConfig) -> dict[str, Any]:
    spec = config.scenario
    speed = _q(spec.get("speed", 0.05), "m/s")
    angles = [_q(a, "rad") for a in spec.get("angles", [0.0])]
    start = spec.get("start")
    rows, results = [], []
    for mount in config.mounts:
        for gantry in angles:
            truth = contact_radius(config, mount, gantry)
            radius = _q(start, "m") if start is not None else min(mount.radius_max, truth + _q(spec.get("lead", 0.02), "m"))
            scanner = config.scanner()
            template = config.head(mount, gantry, radius)
            stopped = None
            first_truth = None
            for _ in range(int(spec.get("max_frames", 20000))):
                origin, rot = mount.pose(gantry, radius)
                result = scanner.frame(template.moved(origin, rot), config.scene)
                rows.append(_frame_row(result, radius=radius, gantry=gantry))
                if result.any_truth and first_truth is None:
                    first_truth = radius
                if result.any_dark:
                    stopped = radius
                    break
                radius -= speed * (result.end - result.start)
                if radius <= mount.radius_min:
                    break
            results.append({"head": mount.name, "gantry_deg": math.degrees(gantry), "contact_radius": truth,
                            "stop_radius": stopped, "first_truth_radius": first_truth,
                            # >0: the head went past the geometric contact before stopping.
                            "overshoot": None if stopped is None else truth - stopped})
    metrics = _summary(rows, config.chain)
    shoots = [r["overshoot"] for r in results if r["overshoot"] is not None]
    metrics.update({"stops": len(shoots), "no_stop": len(results) - len(shoots),
                    "overshoot_max": max(shoots) if shoots else None,
                    "overshoot_mean": float(np.mean(shoots)) if shoots else None,
                    "early_max": -min(shoots) if shoots and min(shoots) < 0 else 0.0})
    return {"metrics": metrics, "rows": rows, "approaches": results}


def scenario_rod(config: BenchConfig) -> dict[str, Any]:
    """Rods of several diameters cross the curtain plane perpendicular to it."""
    spec = config.scenario
    diameters = [_q(d, "m") for d in spec.get("diameters", [0.010, 0.015, 0.020, 0.030])]
    steps = int(spec.get("steps", 97))
    across = _q(spec.get("across", 0.0), "m")         # position along the beams, 0 = mid span
    length = _q(spec.get("length", 0.2), "m")
    mount = config.mounts[0]
    gantry = _q(spec.get("gantry", 0.0), "rad")
    head = config.head(mount, gantry, _q(spec.get("radius", mount.radius), "m"))
    rot, origin = head.rotation, head.origin
    layout = config.layout
    reach = (layout.count - 1) * layout.pitch / 2
    positions = np.linspace(-reach, reach, steps)
    rows, per_diameter = [], []
    for diameter in diameters:
        scanner = config.scanner()
        scene = Scene([] if not spec.get("keep_scene") else list(config.scene.primitives))
        rod = Cylinder("rod", origin, rot, "obstacle", radius=diameter / 2, half_height=length / 2)
        detected = 0
        warm = True
        for x in positions:
            centre = origin + rot @ np.array([x, across, 0.0])
            scene.replace("rod", rod.moved(centre))
            if warm:                      # one frame to settle the node and learn references
                scanner.frame(head, Scene([]))
                warm = False
            result = scanner.frame(head, scene)
            # Detected: a dark decision on a beam the rod really shadows, in a frame without
            # dark decisions on unshadowed beams (a curtain that reads dark everywhere sees nothing).
            spurious = any(d and not s for d, s in zip(result.dark, result.shadowed))
            detected += (not spurious) and any(d and s for d, s in zip(result.dark, result.shadowed))
            rows.append(_frame_row(result, diameter=diameter, x=float(x)))
        per_diameter.append({"diameter": diameter, "detection_rate": detected / len(positions)})
    metrics = _summary(rows, config.chain)
    full = [d["diameter"] for d in per_diameter if d["detection_rate"] >= 1.0]
    metrics.update({"resolution": min(full) if full else None,
                    **{f"detect_{d['diameter'] * 1000:g}mm": d["detection_rate"] for d in per_diameter}})
    return {"metrics": metrics, "rows": rows, "rods": per_diameter}


def scenario_orbit(config: BenchConfig) -> dict[str, Any]:
    spec = config.scenario
    omega = _q(spec.get("orbit_speed", 0.3), "rad/s")
    duration = _q(spec.get("duration", 3.0), "s")
    follower = ContourFollower(_q(spec.get("approach", 0.03), "m/s"), _q(spec.get("retract", 0.03), "m/s"))
    truth_every = int(spec.get("truth_every", 10))
    rows = []
    gaps: dict[str, list[float]] = {}
    for mount in config.mounts:
        scanner = config.scanner()
        follow = replace(follower, radius_min=mount.radius_min, radius_max=mount.radius_max)
        radius = _q(spec.get("start", mount.radius), "m")
        template = config.head(mount, 0.0, radius)
        k = 0
        while scanner.time < duration:
            gantry = omega * scanner.time
            origin, rot = mount.pose(gantry, radius)
            result = scanner.frame(template.moved(origin, rot), config.scene)
            extra: dict[str, Any] = {"radius": radius, "gantry": gantry}
            if k % truth_every == 0:
                contact = contact_radius(config, mount, gantry)
                extra["gap"] = radius - contact
                gaps.setdefault(mount.name, []).append(radius - contact)
            rows.append(_frame_row(result, **extra))
            radius = follow.advance(radius, result.any_dark, result.end - result.start)
            k += 1
    metrics = _summary(rows, config.chain)
    every = [g for values in gaps.values() for g in values]
    settled = [g for values in gaps.values() for g in values[len(values) // 4:]]   # after the first close-in
    metrics.update({"gap_mean": float(np.mean(settled)) if settled else None,
                    "gap_min": float(np.min(settled)) if settled else None,
                    "gap_max": float(np.max(settled)) if settled else None,
                    "intrusion": max(0.0, -float(min(every))) if every else 0.0})
    return {"metrics": metrics, "rows": rows}


SCENARIOS = {"static": scenario_static, "approach": scenario_approach, "rod": scenario_rod, "orbit": scenario_orbit}


def _merge(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(value, Mapping) and isinstance(out.get(key), dict) else copy.deepcopy(value)
    return out


def load_bench(path) -> dict[str, Any]:
    """Read a bench YAML; `include: other.yaml` (relative to the file) is merged underneath.

    A `scenario` or `sweep` in the including file replaces the included one whole.
    """
    import yaml
    from pathlib import Path
    path = Path(path)
    raw = yaml.safe_load(path.read_text()) or {}
    include = raw.pop("include", None)
    if include:
        base = load_bench(path.parent / str(include))
        for key in ("scenario", "sweep"):
            if key in raw:
                base.pop(key, None)
        raw = _merge(base, raw)
    return raw


def run_bench(raw: Mapping[str, Any] | BenchConfig) -> dict[str, Any]:
    config = raw if isinstance(raw, BenchConfig) else BenchConfig.from_mapping(raw)
    kind = str(config.scenario.get("type", "static"))
    if kind not in SCENARIOS:
        raise ValueError(f"unknown light-curtain scenario {kind!r}: {', '.join(SCENARIOS)}")
    started = _time.perf_counter()
    result = SCENARIOS[kind](config)
    algorithm = config.make_algorithm()
    result["metrics"].update({"scenario": kind, "algorithm": algorithm.name, "beams": len(algorithm.beams),
                              "emitter_on_axis_W_sr": config.emitter.on_axis,
                              "emitter_half_angle_deg": config.emitter.half_angle,
                              "wall_time": _time.perf_counter() - started})
    return result


# -- sweeps ------------------------------------------------------------------------------------
def set_path(raw: dict[str, Any], path: str, value: Any) -> None:
    """Set `a.b.c` in a nested mapping; `curtain.` may be left out for emitter/receiver/chain."""
    keys = path.split(".")
    if keys[0] in ("emitter", "receiver", "chain", "heads", "count", "pitch", "span"):
        keys = ["curtain", *keys]
    node = raw
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    if keys[-1] == "algorithm" and keys[0] == "scan":
        node.pop("params", None) if isinstance(value, Mapping) else None
    node[keys[-1]] = value


def sweep_variants(raw: Mapping[str, Any], grid: Mapping[str, Sequence[Any]] | None = None) -> list[tuple[dict, dict]]:
    grid = dict(grid if grid is not None else raw.get("sweep") or {})
    if not grid:
        return [({}, dict(raw))]
    keys = list(grid)
    variants = []
    for values in itertools.product(*(grid[k] for k in keys)):
        variant = copy.deepcopy(dict(raw))
        variant.pop("sweep", None)
        point = dict(zip(keys, values))
        for key, value in point.items():
            set_path(variant, key, value)
        variants.append((point, variant))
    return variants


def _run_variant(item: tuple[dict, dict]) -> dict[str, Any]:
    point, variant = item
    try:
        metrics = run_bench(variant)["metrics"]
        return {**{k: (v if not isinstance(v, Mapping) else v.get("name")) for k, v in point.items()}, **metrics}
    except Exception as error:      # one bad variant must not sink the sweep
        return {**{k: str(v) for k, v in point.items()}, "error": f"{type(error).__name__}: {error}"}


def sweep(raw: Mapping[str, Any], grid: Mapping[str, Sequence[Any]] | None = None, workers: int = 1) -> list[dict[str, Any]]:
    variants = sweep_variants(raw, grid)
    if workers > 1 and len(variants) > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(_run_variant, variants))
    return [_run_variant(v) for v in variants]
