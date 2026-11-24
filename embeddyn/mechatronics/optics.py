"""Infrared light curtains: beam physics between emitters and receivers.

A light curtain is a row of IR emitters facing a row of receivers. A scanning
controller (the firmware) fires one emitter at a time through an address bus
and reads the matching receiver through an analog multiplexer, so beams never
see each other's light. That is how body-contour curtains on SPECT camera
heads keep the detector a few millimetres off the patient: the head moves in
until a beam under its face is interrupted.

The model is geometry-agnostic: it asks a `raycast(origin, end)` function
whether the straight segment between two points is blocked. The MuJoCo adapter
answers with `mj_ray` against the scene (`mujoco_adapter.py`).

Per selected beam (emitter e, receiver r, distance d):

* **Partial blocking.** The beam is the pencil of light between the emitter
  lens and the receiver aperture. It is sampled as rays between points on the
  two discs; the transmittance T is the unblocked fraction, so an edge that
  grazes the beam dims it smoothly instead of switching it.
* **Optical power.** Radiant intensity I(θe) = I0·cos^me(θe) (generalised
  Lambertian, me = -ln 2 / ln cos θ½), receiver sensitivity cos^mr(θr) with
  its own half angle, inverse square:
  P = T·I(θe)·A·cos^mr(θr) / d².
* **Receiver chain.** Photocurrent i = R·P. The emitter is pulse-modulated and
  the receiver demodulates synchronously, so ambient IR (lamps, daylight)
  leaks through only as `ambient_rejection`; shot noise of signal plus ambient
  current and an amplifier noise floor are added. The transimpedance output
  is low-pass filtered with `tau` (settling after the multiplexer switches),
  offset and clamped to the rails: the voltage the MCU's ADC samples.
* **Comparator.** A Schmitt trigger on that voltage (`threshold_on` /
  `threshold_off`) gives the digital "light received" level for a GPIO input.

The multiplexer output is one node shared by all beams, so its settling is a
property of the scan: a firmware that samples too soon after switching beams
reads part of the previous beam.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

Vector = Sequence[float]
Raycast = Callable[[Vector, Vector, int], bool]      # (start, end, head index) -> blocked
# Optional batch form: (start, ends, head index) -> how many of the segments are blocked.
RaycastFrom = Callable[[Vector, Sequence[Vector], int], int]
ELECTRON = 1.602176634e-19


def _sub(a: Vector, b: Vector) -> list[float]:
    return [a[0] - b[0], a[1] - b[1], a[2] - b[2]]


def _dot(a: Vector, b: Vector) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _norm(a: Vector) -> float:
    return math.sqrt(_dot(a, a))


def _unit(a: Vector) -> list[float]:
    n = _norm(a)
    return [a[0] / n, a[1] / n, a[2] / n] if n > 0 else [0.0, 0.0, 1.0]


def _cross(a: Vector, b: Vector) -> list[float]:
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def lambert_order(half_angle_deg: float) -> float:
    """Exponent m of cos^m with half power at `half_angle_deg`."""
    c = math.cos(math.radians(half_angle_deg))
    if not 0.0 < c < 1.0:
        raise ValueError("half angle must be within 0..90 degrees")
    return -math.log(2.0) / math.log(c)


def disc_points(center: Vector, axis: Vector, diameter: float, rings: int = 1, per_ring: int = 6) -> list[list[float]]:
    """Sample points on a disc (centre plus `rings` rings) perpendicular to `axis`."""
    axis = _unit(axis)
    helper = [1.0, 0.0, 0.0] if abs(axis[0]) < 0.9 else [0.0, 1.0, 0.0]
    u = _unit(_cross(axis, helper))
    v = _cross(axis, u)
    points = [list(center)]
    for ring in range(1, rings + 1):
        radius = diameter / 2 * ring / (rings + 0.5)
        for k in range(per_ring):
            angle = 2 * math.pi * (k + 0.5 * (ring % 2)) / per_ring
            c, s = math.cos(angle) * radius, math.sin(angle) * radius
            points.append([center[i] + c * u[i] + s * v[i] for i in range(3)])
    return points


@dataclass
class CurtainOptics:
    """Emitter, receiver and amplifier parameters (SI units)."""
    intensity: float = 10e-3            # emitter radiant intensity on axis, W/sr
    emitter_half_angle: float = 10.0    # deg, half power
    emitter_aperture: float = 3e-3      # lens diameter, m
    receiver_area: float = 7e-6         # photodiode area, m^2 (BPW34-class)
    receiver_half_angle: float = 20.0   # deg, half sensitivity
    receiver_aperture: float = 3e-3     # m
    responsivity: float = 0.6           # A/W at 940 nm
    gain: float = 1e7                   # transimpedance + post gain, V/A
    offset: float = 0.05                # output offset, V
    rail: float = 3.2                   # output saturation, V
    tau: float = 20e-6                  # multiplexer/amplifier settling time constant, s
    ambient: float = 1.0                # ambient IR irradiance on the receivers, W/m^2
    ambient_flicker: float = 0.3        # relative 100 Hz flicker of the ambient light
    flicker_hz: float = 100.0
    ambient_rejection: float = 1e-4     # synchronous demodulation leakage (80 dB)
    bandwidth: float = 20e3             # noise bandwidth, Hz
    noise_floor: float = 2e-3           # amplifier output noise, V rms
    threshold_on: float = 0.6           # comparator: light detected above, V
    threshold_off: float = 0.4          # comparator: dark below, V
    rays: int = 1                       # rings of sample points per disc (1 -> 7 points, 49 rays)
    seed: int = 0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "CurtainOptics":
        from .quantities import Quantity
        units = {"intensity": "W/sr", "emitter_aperture": "m", "receiver_area": "m^2", "receiver_aperture": "m",
                 "responsivity": "A/W", "gain": "V/A", "offset": "V", "rail": "V", "tau": "s", "ambient": "W/m^2",
                 "flicker_hz": "Hz", "bandwidth": "Hz", "noise_floor": "V", "threshold_on": "V", "threshold_off": "V",
                 "emitter_half_angle": "deg", "receiver_half_angle": "deg"}
        values: dict[str, Any] = {}
        known = set(cls.__dataclass_fields__)
        for key, raw_value in dict(raw or {}).items():
            if key not in known:
                raise ValueError(f"light curtain: unknown optics parameter {key!r}")
            if key in ("rays", "seed"):
                values[key] = int(raw_value)
            else:
                unit = units.get(key, "")
                try:
                    quantity = Quantity.parse(raw_value, unit)
                except (TypeError, ValueError) as error:
                    raise ValueError(f"light curtain optics {key}: {error}") from error
                if unit and quantity.unit != unit:      # an unknown unit is kept as written: refuse it
                    raise ValueError(f"light curtain optics {key}: {raw_value!r} is not a quantity in {unit}")
                values[key] = quantity.value
        optics = cls(**values)
        if optics.threshold_off > optics.threshold_on:
            raise ValueError("light curtain: threshold_off must not exceed threshold_on")
        if optics.tau < 0 or optics.rays < 0:
            raise ValueError("light curtain: tau and rays must not be negative")
        lambert_order(optics.emitter_half_angle)
        lambert_order(optics.receiver_half_angle)
        return optics


@dataclass
class Beam:
    emitter: list[float]
    receiver: list[float]
    head: int = 0
    emitter_axis: list[float] | None = None     # optical axes (default: aimed at each other)
    receiver_axis: list[float] | None = None


@dataclass
class BeamReading:
    transmittance: float
    power: float            # W on the photodiode from the emitter
    current: float          # A, signal photocurrent


@dataclass
class LightCurtain:
    """Scanning curtain controller: address bus in, multiplexed receiver voltage and comparator out."""
    optics: CurtainOptics = field(default_factory=CurtainOptics)
    voltage: float = 0.0
    detect: bool = False
    selected: int | None = None

    def __post_init__(self) -> None:
        self._me = lambert_order(self.optics.emitter_half_angle)
        self._mr = lambert_order(self.optics.receiver_half_angle)
        self._rng = random.Random(self.optics.seed)
        self.voltage = self.optics.offset

    # -- physics --------------------------------------------------------------------
    def read_beam(self, beam: Beam, raycast: Raycast, cast_from: RaycastFrom | None = None,
                  transmittance: float | None = None) -> BeamReading:
        o = self.optics
        axis = _sub(beam.receiver, beam.emitter)
        distance = _norm(axis)
        if distance <= 0:
            return BeamReading(0.0, 0.0, 0.0)
        direction = [x / distance for x in axis]
        if transmittance is None:                     # else: reuse a recent ray result
            sources = disc_points(beam.emitter, direction, o.emitter_aperture, o.rays)
            targets = disc_points(beam.receiver, direction, o.receiver_aperture, o.rays)
            if cast_from is not None:
                blocked = sum(cast_from(a, targets, beam.head) for a in sources)
            else:
                blocked = sum(raycast(a, b, beam.head) for a in sources for b in targets)
            transmittance = 1.0 - blocked / (len(sources) * len(targets))
        # Off-axis angles: a tilted emitter or receiver (mounting error, a bent rail) loses signal.
        cos_e = 1.0 if beam.emitter_axis is None else max(0.0, _dot(_unit(beam.emitter_axis), direction))
        cos_r = 1.0 if beam.receiver_axis is None else max(0.0, -_dot(_unit(beam.receiver_axis), direction))
        power = transmittance * o.intensity * cos_e ** self._me * o.receiver_area * cos_r ** self._mr / distance ** 2
        return BeamReading(transmittance, power, o.responsivity * power)

    def target_voltage(self, current: float, time: float) -> float:
        o = self.optics
        flicker = 1.0 + o.ambient_flicker * math.sin(2 * math.pi * o.flicker_hz * time)
        ambient = o.responsivity * o.ambient * flicker * o.receiver_area
        shot = math.sqrt(2 * ELECTRON * (current + ambient) * o.bandwidth)
        noise = math.hypot(shot * o.gain, o.noise_floor)
        return o.offset + o.gain * (current + ambient * o.ambient_rejection) + self._rng.gauss(0.0, noise)

    def update(self, beam: Beam | None, raycast: Raycast, time: float, dt: float,
               cast_from: RaycastFrom | None = None, transmittance: float | None = None) -> BeamReading | None:
        """Advance the multiplexer output by `dt` with `beam` selected (None: emitters off)."""
        reading = self.read_beam(beam, raycast, cast_from, transmittance) if beam is not None else None
        target = self.target_voltage(reading.current if reading else 0.0, time)
        alpha = 1.0 if self.optics.tau <= 0 else 1.0 - math.exp(-dt / self.optics.tau)
        self.voltage += (target - self.voltage) * alpha
        self.voltage = min(max(self.voltage, 0.0), self.optics.rail)
        if self.voltage >= self.optics.threshold_on:
            self.detect = True
        elif self.voltage <= self.optics.threshold_off:
            self.detect = False
        return reading

    def snapshot(self) -> dict[str, Any]:
        state = self._rng.getstate()
        return {"voltage": self.voltage, "detect": self.detect,
                "rng": [state[0], list(state[1]), state[2]]}

    def restore(self, state: Mapping[str, Any]) -> None:
        self.voltage = float(state["voltage"])
        self.detect = bool(state["detect"])
        version, internal, gauss = state["rng"]
        self._rng.setstate((version, tuple(internal), gauss))
