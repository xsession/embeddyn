"""Emitter and receiver rows of an infrared light curtain and their optical coupling.

A curtain head is a rail of `count` IR LEDs facing a rail of `count`
photodiodes across `span`, `pitch` apart. In the curtain's own frame the rails
run along x, light crosses along +y (emitters at y = -span/2, receivers at
+span/2) and the curtain plane is z = 0. A head pose (`origin`, `rotation`)
puts the curtain in the world, e.g. 12 mm under a SPECT detector face.

`coupling()` returns, for every emitter e and receiver r, the optical power
that reaches r from e when e is on:

    P[e, r] = T[e, r] * I_e * cos^m_e(theta_e) * A * cos^m_r(theta_r) / d^2

with T the unblocked fraction of the pencil between the two apertures (rays
between sample points on both discs, cast against the `Scene`), I_e the
emitter's on-axis radiant intensity, theta_e the angle off the emitter's
optical axis, theta_r the angle off the receiver's axis, m_e / m_r the
generalised Lambert orders from the half angles. The diagonal (e == r) holds
the straight beams; off-diagonal terms are what a receiver sees from other
emitters: crossed beams for algorithms that use them, crosstalk for those
that light several LEDs at once.

Emitter description: either the on-axis `intensity` (W/sr) or the total
radiant `power` (W) is given. With a fixed total power a narrower LED puts
more intensity on axis: I0 = power * (m + 1) / (2 pi). That is the physical
trade-off when choosing an LED angle: narrow beams give signal margin, wide
beams tolerate misalignment and reach diagonal receivers.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

import numpy as np

from ..optics import CurtainOptics, disc_points, lambert_order
from .scene import Scene, rotation_matrix


def _q(raw: Any, unit: str) -> float:
    from ..quantities import Quantity
    quantity = Quantity.parse(raw, unit)
    if unit and quantity.unit != unit:
        raise ValueError(f"{raw!r} is not a quantity in {unit}")
    return quantity.value


@dataclass
class EmitterSpec:
    """IR LED: angular profile, intensity, mounting."""
    intensity: float | None = 10e-3     # W/sr on axis (or give `power`)
    power: float | None = None          # W total radiant flux; sets intensity from the half angle
    half_angle: float = 10.0            # deg, half intensity
    aperture: float = 3e-3              # lens diameter, m
    drive: float = 1.0                  # relative drive current (intensity scales linearly)
    spread: float = 0.0                 # relative sigma of intensity between LEDs (binning, ageing)
    tilt: float = 0.0                   # deg, all LEDs pitched about the rail axis (towards +z)
    yaw: float = 0.0                    # deg, all LEDs turned along the rail (towards +x)
    tilt_sigma: float = 0.0             # deg, random mounting error per LED (both axes)

    FIELDS = {"intensity": "W/sr", "power": "W", "half_angle": "deg", "aperture": "m", "drive": "",
              "spread": "", "tilt": "deg", "yaw": "deg", "tilt_sigma": "deg"}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "EmitterSpec":
        values: dict[str, Any] = {}
        for key, value in dict(raw or {}).items():
            if key not in cls.FIELDS:
                raise ValueError(f"light curtain emitter: unknown parameter {key!r}")
            values[key] = None if value is None else _q(value, cls.FIELDS[key])
        if "power" in values and values["power"] is not None and "intensity" not in values:
            values["intensity"] = None
        spec = cls(**values)
        lambert_order(spec.half_angle)
        if spec.intensity is None and spec.power is None:
            raise ValueError("light curtain emitter: give intensity (W/sr) or power (W)")
        return spec

    @property
    def order(self) -> float:
        return lambert_order(self.half_angle)

    @property
    def on_axis(self) -> float:
        """On-axis radiant intensity in W/sr at the configured drive."""
        base = self.intensity if self.power is None else self.power * (self.order + 1) / (2 * math.pi)
        return float(base) * self.drive


@dataclass
class ReceiverSpec:
    half_angle: float = 20.0            # deg, half sensitivity
    area: float = 7e-6                  # m^2
    aperture: float = 3e-3              # m
    tilt: float = 0.0
    yaw: float = 0.0
    tilt_sigma: float = 0.0

    FIELDS = {"half_angle": "deg", "area": "m^2", "aperture": "m", "tilt": "deg", "yaw": "deg", "tilt_sigma": "deg"}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "ReceiverSpec":
        values = {}
        for key, value in dict(raw or {}).items():
            if key not in cls.FIELDS:
                raise ValueError(f"light curtain receiver: unknown parameter {key!r}")
            values[key] = _q(value, cls.FIELDS[key])
        spec = cls(**values)
        lambert_order(spec.half_angle)
        return spec

    @property
    def order(self) -> float:
        return lambert_order(self.half_angle)


@dataclass
class CurtainLayout:
    count: int = 16
    pitch: float = 0.024
    span: float = 0.58
    receivers: int | None = None        # default: same as count
    receiver_offset: float = 0.0        # m, receiver row shifted along the rail (staggered rows)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CurtainLayout":
        layout = cls(count=int(raw.get("count", 16)), pitch=_q(raw.get("pitch", 0.024), "m"),
                     span=_q(raw.get("span", 0.58), "m"),
                     receivers=None if raw.get("receivers") is None else int(raw["receivers"]),
                     receiver_offset=_q(raw.get("receiver_offset", 0.0), "m"))
        if layout.count < 1 or layout.pitch <= 0 or layout.span <= 0:
            raise ValueError("light curtain: count >= 1, pitch > 0 and span > 0 required")
        return layout

    @property
    def receiver_count(self) -> int:
        return self.count if self.receivers is None else self.receivers

    def local_points(self) -> tuple[np.ndarray, np.ndarray]:
        """Emitter and receiver centres in the curtain frame."""
        n, m = self.count, self.receiver_count
        ex = (np.arange(n) - (n - 1) / 2) * self.pitch
        rx = (np.arange(m) - (m - 1) / 2) * self.pitch + self.receiver_offset
        emitters = np.column_stack([ex, np.full(n, -self.span / 2), np.zeros(n)])
        receivers = np.column_stack([rx, np.full(m, self.span / 2), np.zeros(m)])
        return emitters, receivers


def _aim(base: Sequence[float], tilt_deg: float, yaw_deg: float) -> np.ndarray:
    """Turn a rail-normal axis by `tilt` about x (towards +z) and `yaw` about z (towards +x)."""
    axis = np.asarray(base, dtype=float)
    sign = 1.0 if axis[1] > 0 else -1.0
    axis = rotation_matrix([1, 0, 0], sign * math.radians(tilt_deg)) @ axis
    return rotation_matrix([0, 0, 1], -sign * math.radians(yaw_deg)) @ axis


@dataclass
class CurtainHead:
    """One curtain in the world: positions, optical axes and per-LED intensities."""
    name: str
    emitters: np.ndarray            # (E, 3) world positions
    emitter_axes: np.ndarray        # (E, 3) unit
    receivers: np.ndarray           # (R, 3)
    receiver_axes: np.ndarray       # (R, 3)
    intensity: np.ndarray           # (E,) W/sr on axis
    emitter: EmitterSpec = field(default_factory=EmitterSpec)
    receiver: ReceiverSpec = field(default_factory=ReceiverSpec)
    origin: np.ndarray = field(default_factory=lambda: np.zeros(3))
    rotation: np.ndarray = field(default_factory=lambda: np.eye(3))

    @classmethod
    def build(cls, name: str, layout: CurtainLayout, emitter: EmitterSpec, receiver: ReceiverSpec,
              origin: Sequence[float] = (0, 0, 0), rotation: np.ndarray | None = None, seed: int = 0) -> "CurtainHead":
        rot = np.eye(3) if rotation is None else np.asarray(rotation, dtype=float)
        org = np.asarray(origin, dtype=float)
        rng = random.Random(f"{seed}:{name}:aim")           # separate streams: changing the tilt
        gain_rng = random.Random(f"{seed}:{name}:gain")     # error does not reshuffle the spread
        e_local, r_local = layout.local_points()

        def axes(count: int, base, spec) -> np.ndarray:
            out = []
            for _ in range(count):
                tilt = spec.tilt + (rng.gauss(0, spec.tilt_sigma) if spec.tilt_sigma else 0.0)
                yaw = spec.yaw + (rng.gauss(0, spec.tilt_sigma) if spec.tilt_sigma else 0.0)
                out.append(_aim(base, tilt, yaw))
            return np.array(out)

        e_axes = axes(layout.count, [0, 1, 0], emitter)
        r_axes = axes(layout.receiver_count, [0, -1, 0], receiver)
        gains = np.array([max(0.0, 1.0 + gain_rng.gauss(0, emitter.spread)) if emitter.spread else 1.0
                          for _ in range(layout.count)])
        return cls(name, org + e_local @ rot.T, e_axes @ rot.T, org + r_local @ rot.T, r_axes @ rot.T,
                   emitter.on_axis * gains, emitter, receiver, org, rot)

    def moved(self, origin: Sequence[float], rotation: np.ndarray) -> "CurtainHead":
        """The same curtain (same per-LED errors) at another pose."""
        rot = np.asarray(rotation, dtype=float)
        org = np.asarray(origin, dtype=float)
        to_local = lambda pts: (pts - self.origin) @ self.rotation      # noqa: E731
        return replace(self, emitters=org + to_local(self.emitters) @ rot.T,
                       emitter_axes=(self.emitter_axes @ self.rotation) @ rot.T,
                       receivers=org + to_local(self.receivers) @ rot.T,
                       receiver_axes=(self.receiver_axes @ self.rotation) @ rot.T,
                       origin=org, rotation=rot)

    @property
    def counts(self) -> tuple[int, int]:
        return len(self.emitters), len(self.receivers)


@dataclass
class Coupling:
    """Optical coupling of one head at one instant."""
    power: np.ndarray           # (E, R) W on receiver r from emitter e (blocking included)
    clear: np.ndarray           # (E, R) W with nothing in the way
    transmittance: np.ndarray   # (E, R) unblocked fraction of the pencil (1 where not cast)
    cast: np.ndarray            # (E, R) bool: pair was ray-cast

    def truth_dark(self, pairs: Sequence[tuple[int, int]], level: float = 0.5) -> list[bool]:
        """Ground truth: a beam is interrupted when less than `level` of its pencil gets through."""
        return [bool(self.transmittance[e, r] < level) for e, r in pairs]


def geometric_gain(head: CurtainHead) -> np.ndarray:
    """Unblocked power matrix (E, R) in W."""
    v = head.receivers[None, :, :] - head.emitters[:, None, :]
    d = np.linalg.norm(v, axis=2)
    u = v / np.where(d > 0, d, 1.0)[..., None]
    cos_e = np.clip(np.einsum("ek,erk->er", head.emitter_axes, u), 0.0, 1.0)
    cos_r = np.clip(-np.einsum("rk,erk->er", head.receiver_axes, u), 0.0, 1.0)
    me, mr = head.emitter.order, head.receiver.order
    return head.intensity[:, None] * cos_e ** me * head.receiver.area * cos_r ** mr / np.where(d > 0, d * d, np.inf)


def cast_pairs(head: CurtainHead, scene: Scene, pairs: Sequence[tuple[int, int]], rays: int = 1,
               groups=None) -> np.ndarray:
    """Unblocked fraction of the pencil for each (emitter, receiver) pair.

    `rays` is the number of sample rings per aperture disc: 0 -> 1 ray per pair,
    1 -> 7 x 7 = 49 rays, 2 -> 13 x 13 = 169 rays.
    """
    if not len(pairs):
        return np.zeros(0)
    starts, ends, owners = [], [], []
    for e, r in pairs:
        direction = (head.receivers[r] - head.emitters[e]).tolist()
        src = disc_points(head.emitters[e].tolist(), direction, head.emitter.aperture, rays)
        dst = disc_points(head.receivers[r].tolist(), direction, head.receiver.aperture, rays)
        starts.extend(a for a in src for _ in dst)
        ends.extend(b for _ in src for b in dst)
        owners.append(len(src) * len(dst))
    blocked = scene.blocked(np.array(starts), np.array(ends), groups)
    counts = np.add.reduceat(blocked.astype(float), np.concatenate([[0], np.cumsum(owners)[:-1]]).astype(int))
    return 1.0 - counts / np.array(owners)


def coupling(head: CurtainHead, scene: Scene, rays: int = 1, pairs: Sequence[tuple[int, int]] | None = None,
             relative_floor: float = 1e-4, groups=None) -> Coupling:
    """Cast pencils and return the power matrix.

    With `pairs` only those pairs are cast (the rest keep transmittance 1 and
    `cast` False). Without, every pair whose unblocked power is at least
    `relative_floor` of the strongest pair is cast.
    """
    gain = geometric_gain(head)
    if pairs is None:
        cast = gain >= relative_floor * float(gain.max() if gain.size else 0.0)
        todo = [tuple(map(int, p)) for p in np.argwhere(cast)]
    else:
        cast = np.zeros_like(gain, dtype=bool)
        todo = sorted({(int(e), int(r)) for e, r in pairs})
        for e, r in todo:
            cast[e, r] = True
    transmittance = np.ones_like(gain)
    if todo:
        index = np.array(todo)
        transmittance[index[:, 0], index[:, 1]] = cast_pairs(head, scene, todo, rays, groups)
    return Coupling(gain * transmittance, gain, transmittance, cast)


class LazyCoupling:
    """Coupling of one head at one instant, ray-cast pair by pair as a scan asks for them.

    With a `ReflectionModel` the stray (one-bounce) light of each lit LED is added;
    `stray` may carry rows from an earlier frame when the head has hardly moved.
    """

    def __init__(self, head: CurtainHead, scene: Scene, rays: int = 1, groups=None,
                 warm: Sequence[tuple[int, int]] = (), reflections: "ReflectionModel | None" = None,
                 stray: dict[int, np.ndarray] | None = None) -> None:
        self.head, self.scene, self.rays, self.groups = head, scene, rays, groups
        self.gain = geometric_gain(head)
        self.transmittance: dict[tuple[int, int], float] = {}
        self.reflections = reflections if reflections is not None and reflections.enabled else None
        self.stray: dict[int, np.ndarray] = {} if stray is None else stray
        self.ensure(warm)

    def ensure(self, pairs) -> None:
        todo = sorted({(int(e), int(r)) for e, r in pairs} - self.transmittance.keys())
        if todo:
            for pair, value in zip(todo, cast_pairs(self.head, self.scene, todo, self.rays, self.groups)):
                self.transmittance[pair] = float(value)

    def stray_row(self, emitter: int) -> np.ndarray | None:
        if self.reflections is None:
            return None
        row = self.stray.get(emitter)
        if row is None:
            row = stray_power(self.head, self.scene, self.reflections, [emitter])[0]
            self.stray[emitter] = row
        return row

    def power(self, lit: Sequence[int], read: Sequence[int]) -> float:
        """Optical power summed over the enabled receivers from the lit emitters (W)."""
        pairs = [(e, r) for e in lit for r in read]
        self.ensure(pairs)
        total = float(sum(self.gain[e, r] * self.transmittance[(e, r)] for e, r in pairs))
        if self.reflections is not None:
            for e in lit:
                row = self.stray_row(e)
                total += float(sum(row[r] for r in read))
        return total

    def truth_dark(self, pairs: Sequence[tuple[int, int]], level: float = 0.5) -> list[bool]:
        self.ensure(pairs)
        return [self.transmittance[(int(e), int(r))] < level for e, r in pairs]


def chain_optics(raw: Mapping[str, Any] | None, emitter: EmitterSpec, receiver: ReceiverSpec) -> CurtainOptics:
    """The receiver chain (amplifier, ambient, noise, comparator) as `CurtainOptics`."""
    chain = dict(raw or {})
    for key in ("intensity", "emitter_half_angle", "emitter_aperture", "receiver_area",
                "receiver_half_angle", "receiver_aperture", "rays"):
        if key in chain:
            raise ValueError(f"light curtain chain: {key!r} belongs under emitter/receiver/scan")
    optics = CurtainOptics.from_mapping(chain)
    return replace(optics, intensity=emitter.on_axis, emitter_half_angle=emitter.half_angle,
                   emitter_aperture=emitter.aperture, receiver_area=receiver.area,
                   receiver_half_angle=receiver.half_angle, receiver_aperture=receiver.aperture)


# -- stray light: one diffuse bounce ------------------------------------------------------------
@dataclass
class ReflectionModel:
    """First-order diffuse reflection of LED light off the scene into the receivers.

    Each lit LED shoots `rays` rays distributed like its cos^m lobe (every ray
    carries the same share of the LED's total flux 2 pi I0 / (m + 1)). Where a
    ray lands on a primitive, the patch reflects rho * flux * cos(theta_out) / pi
    W/sr (Lambertian, rho = the primitive's albedo); every receiver that sees
    the patch unobstructed gets that intensity * A cos^m_r(theta_r) / d^2.
    This is the light that walks around an obstacle: a wide LED lighting the
    patient's skin, the couch or the detector face can keep a blocked beam's
    receiver above threshold. Off by default (`enabled: false`).
    """
    enabled: bool = False
    albedo: float = 0.5                 # default reflectance (skin at 940 nm is roughly 0.5..0.6)
    face_albedo: float = 0.0            # reflective detector face above the curtain (0: not modelled)
    face_gap: float = 0.012             # m, curtain plane to face
    rays: int = 256                     # per lit LED
    reuse: float = 0.001                # m, recompute after the head moved this far

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "ReflectionModel":
        raw = dict(raw or {})
        known = {"enabled", "albedo", "face_albedo", "face_gap", "rays", "reuse"}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"light curtain reflections: unknown keys {sorted(unknown)}")
        model = cls(enabled=bool(raw.get("enabled", bool(raw))), albedo=float(raw.get("albedo", 0.5)),
                    face_albedo=float(raw.get("face_albedo", 0.0)), face_gap=_q(raw.get("face_gap", 0.012), "m"),
                    rays=int(raw.get("rays", 256)), reuse=_q(raw.get("reuse", 0.001), "m"))
        if not 0 <= model.albedo <= 1 or not 0 <= model.face_albedo <= 1:
            raise ValueError("light curtain reflections: albedo must be within 0..1")
        return model


def _lobe_directions(axis: np.ndarray, order: float, count: int) -> np.ndarray:
    """`count` stratified unit vectors distributed like cos^order about `axis`."""
    i = np.arange(count) + 0.5
    cos_t = (i / count) ** (1.0 / (order + 1.0))
    sin_t = np.sqrt(np.maximum(0.0, 1 - cos_t ** 2))
    phi = i * math.pi * (3 - math.sqrt(5))             # golden angle
    z = axis / np.linalg.norm(axis)
    helper = np.array([1.0, 0, 0]) if abs(z[0]) < 0.9 else np.array([0, 1.0, 0])
    x = np.cross(helper, z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return (np.outer(sin_t * np.cos(phi), x) + np.outer(sin_t * np.sin(phi), y) + np.outer(cos_t, z))


def face_primitive(head: CurtainHead, model: ReflectionModel):
    """The detector face above the curtain plane as a reflecting box (curtain frame +z)."""
    from .scene import Box
    e_local = (head.emitters - head.origin) @ head.rotation
    r_local = (head.receivers - head.origin) @ head.rotation
    xs = np.concatenate([e_local[:, 0], r_local[:, 0]])
    ys = np.concatenate([e_local[:, 1], r_local[:, 1]])
    half = np.array([(xs.max() - xs.min()) / 2 + 0.02, (ys.max() - ys.min()) / 2 - 0.02, 0.05])
    centre = head.origin + head.rotation @ np.array([(xs.max() + xs.min()) / 2, 0.0, model.face_gap + half[2]])
    return Box("detector-face", centre, head.rotation, "face", albedo=model.face_albedo, half=half)


def stray_power(head: CurtainHead, scene: Scene, model: ReflectionModel, emitters: Sequence[int]) -> np.ndarray:
    """(len(emitters), R) power in W reaching each receiver after one diffuse bounce."""
    out = np.zeros((len(emitters), len(head.receivers)))
    if not model.enabled or not len(emitters):
        return out
    prims = list(scene.primitives)
    if model.face_albedo > 0:
        prims.append(face_primitive(head, model))
    world = Scene(prims)
    albedos = np.array([model.albedo if p.albedo is None else p.albedo for p in prims] + [0.0])
    order_e, order_r = head.emitter.order, head.receiver.order
    area = head.receiver.area
    for row, e in enumerate(emitters):
        flux = 2 * math.pi * head.intensity[e] / (order_e + 1) / model.rays
        dirs = _lobe_directions(head.emitter_axes[e], order_e, model.rays)
        starts = np.repeat(head.emitters[e][None, :], model.rays, axis=0)
        t, normal, owner = world.first_hit(starts, dirs)
        hit = np.isfinite(t) & (owner >= 0)
        if not hit.any():
            continue
        points = starts[hit] + dirs[hit] * t[hit][:, None]
        normals = normal[hit]
        flip = np.einsum("ij,ij->i", normals, dirs[hit]) > 0
        normals[flip] *= -1                               # face the incoming light
        rho = albedos[owner[hit]]
        # Every hit patch towards every receiver.
        v = head.receivers[None, :, :] - points[:, None, :]
        dist = np.linalg.norm(v, axis=2)
        w = v / dist[..., None]
        cos_out = np.clip(np.einsum("hk,hrk->hr", normals, w), 0.0, 1.0)
        cos_r = np.clip(-np.einsum("rk,hrk->hr", head.receiver_axes, w), 0.0, 1.0)
        weight = (rho * flux / math.pi)[:, None] * cos_out * area * cos_r ** order_r / dist ** 2
        live = weight > 0
        if not live.any():
            continue
        hp, rr = np.nonzero(live)
        lift = points[hp] + normals[hp] * 1e-6
        blocked = world.blocked(lift, head.receivers[rr])
        weight[hp[blocked], rr[blocked]] = 0.0
        out[row] = weight.sum(axis=0)
    return out
