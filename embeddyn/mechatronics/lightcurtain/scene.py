"""Analytic ray casting against a scene of primitive solids.

The light-curtain bench needs one question answered many thousands of times
per frame: is the straight segment from a point on an emitter lens to a point
on a receiver aperture blocked? The patient phantom, the couch and test rods
are described by primitives (box, ellipsoid, sphere, cylinder, capsule), the
same shapes Webots uses for bounding objects, so the answer is exact and the
bench runs without a physics engine or a GPU. All segment tests are
vectorised with numpy: `Scene.blocked(starts, ends)` takes (N, 3) arrays.

Frames: every primitive has a centre `position` and a 3x3 `rotation` whose
columns are its local axes in world coordinates (world = position + R @ local).
Cylinders and capsules run along their local z axis, as in Webots R2022+.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

EPS = 1e-12


def rotation_matrix(axis: Sequence[float], angle: float) -> np.ndarray:
    """Rotation by `angle` (rad) about `axis` (Rodrigues)."""
    a = np.asarray(axis, dtype=float)
    n = float(np.linalg.norm(a))
    if n < EPS or abs(angle) < EPS:
        return np.eye(3)
    x, y, z = a / n
    c, s, t = math.cos(angle), math.sin(angle), 1.0 - math.cos(angle)
    return np.array([[t * x * x + c, t * x * y - s * z, t * x * z + s * y],
                     [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
                     [t * x * z - s * y, t * y * z + s * x, t * z * z + c]])


def frame_between(start: Sequence[float], end: Sequence[float]) -> tuple[np.ndarray, np.ndarray, float]:
    """Centre, rotation (local z along start->end) and length of a segment."""
    p0, p1 = np.asarray(start, dtype=float), np.asarray(end, dtype=float)
    axis = p1 - p0
    length = float(np.linalg.norm(axis))
    z = axis / length if length > EPS else np.array([0.0, 0.0, 1.0])
    helper = np.array([1.0, 0.0, 0.0]) if abs(z[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    x = np.cross(helper, z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return (p0 + p1) / 2, np.column_stack([x, y, z]), length


def _clip_interval(t0: np.ndarray, t1: np.ndarray) -> np.ndarray:
    """Whether [t0, t1] overlaps the open segment parameter range (0, 1)."""
    return (t1 > 0.0) & (t0 < 1.0) & (t1 >= t0)


@dataclass
class Primitive:
    """A solid with a pose. Subclasses implement `_hits` in local coordinates."""
    name: str = ""
    position: np.ndarray = field(default_factory=lambda: np.zeros(3))
    rotation: np.ndarray = field(default_factory=lambda: np.eye(3))
    group: str = "obstacle"
    albedo: float | None = None         # diffuse reflectance at the LED wavelength (None: scene default)

    kind = "primitive"

    def hits(self, starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
        q0 = (starts - self.position) @ self.rotation          # world -> local (R orthonormal)
        d = (ends - starts) @ self.rotation
        return self._hits(q0, d)

    def intersect(self, starts: np.ndarray, directions: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """First hit along rays (t > 0, inf on a miss) and the outward surface normal there (world)."""
        q0 = (starts - self.position) @ self.rotation
        d = directions @ self.rotation
        t, normal = self._intersect(q0, d)
        return t, normal @ self.rotation.T

    def _hits(self, q0: np.ndarray, d: np.ndarray) -> np.ndarray:  # pragma: no cover - abstract
        raise NotImplementedError

    def _intersect(self, q0: np.ndarray, d: np.ndarray):  # pragma: no cover - abstract
        raise NotImplementedError

    def moved(self, position: Sequence[float] | None = None, rotation: np.ndarray | None = None) -> "Primitive":
        import copy
        twin = copy.copy(self)
        if position is not None:
            twin.position = np.asarray(position, dtype=float)
        if rotation is not None:
            twin.rotation = np.asarray(rotation, dtype=float)
        return twin

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.kind, "position": self.position.tolist()}


@dataclass
class Ellipsoid(Primitive):
    radii: np.ndarray = field(default_factory=lambda: np.ones(3))
    kind = "ellipsoid"

    def _hits(self, q0, d):
        q, v = q0 / self.radii, d / self.radii
        a = np.einsum("ij,ij->i", v, v)
        b = 2.0 * np.einsum("ij,ij->i", q, v)
        c = np.einsum("ij,ij->i", q, q) - 1.0
        disc = b * b - 4 * a * c
        ok = (disc >= 0) & (a > EPS)
        root = np.sqrt(np.where(ok, disc, 0.0))
        safe = np.where(a > EPS, a, 1.0)
        t0, t1 = (-b - root) / (2 * safe), (-b + root) / (2 * safe)
        return (ok & _clip_interval(t0, t1)) | ((a <= EPS) & (c <= 0))

    def _intersect(self, q0, d):
        q, v = q0 / self.radii, d / self.radii
        a = np.einsum("ij,ij->i", v, v)
        b = 2.0 * np.einsum("ij,ij->i", q, v)
        c = np.einsum("ij,ij->i", q, q) - 1.0
        disc = b * b - 4 * a * c
        ok = (disc >= 0) & (a > EPS)
        root = np.sqrt(np.where(ok, disc, 0.0))
        safe = np.where(a > EPS, a, 1.0)
        t0, t1 = (-b - root) / (2 * safe), (-b + root) / (2 * safe)
        t = _first_positive(ok, t0, t1)
        point = q0 + d * np.where(np.isfinite(t), t, 0.0)[:, None]
        return t, _normalize(point / self.radii ** 2)


def _first_positive(ok: np.ndarray, t0: np.ndarray, t1: np.ndarray, eps: float = 1e-9) -> np.ndarray:
    return np.where(ok & (t0 > eps), t0, np.where(ok & (t1 > eps), t1, np.inf))


def _normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=1, keepdims=True)
    return v / np.where(n > 0, n, 1.0)


@dataclass
class Box(Primitive):
    half: np.ndarray = field(default_factory=lambda: np.full(3, 0.5))
    kind = "box"

    def _hits(self, q0, d):
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = 1.0 / d
            ta = (-self.half - q0) * inv
            tb = (self.half - q0) * inv
        flat = np.abs(d) < EPS
        inside = np.abs(q0) <= self.half
        lo = np.where(flat, np.where(inside, -np.inf, np.inf), np.minimum(ta, tb))
        hi = np.where(flat, np.where(inside, np.inf, -np.inf), np.maximum(ta, tb))
        return _clip_interval(lo.max(axis=1), hi.min(axis=1))

    def _intersect(self, q0, d):
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = 1.0 / d
            ta = (-self.half - q0) * inv
            tb = (self.half - q0) * inv
        flat = np.abs(d) < EPS
        inside = np.abs(q0) <= self.half
        lo = np.where(flat, np.where(inside, -np.inf, np.inf), np.minimum(ta, tb))
        hi = np.where(flat, np.where(inside, np.inf, -np.inf), np.maximum(ta, tb))
        enter, leave = lo.max(axis=1), hi.min(axis=1)
        ok = leave >= np.maximum(enter, 0.0)
        t = _first_positive(ok, enter, leave)
        rows = np.arange(len(q0))
        axis = np.where(enter > 1e-9, lo.argmax(axis=1), hi.argmin(axis=1))
        normal = np.zeros_like(q0)
        sign = np.where(enter > 1e-9, -np.sign(d[rows, axis]), np.sign(d[rows, axis]))
        normal[rows, axis] = np.where(sign == 0, 1.0, sign)
        return t, normal


@dataclass
class Cylinder(Primitive):
    radius: float = 0.1
    half_height: float = 0.1
    kind = "cylinder"

    def _hits(self, q0, d):
        a = d[:, 0] ** 2 + d[:, 1] ** 2
        b = 2 * (q0[:, 0] * d[:, 0] + q0[:, 1] * d[:, 1])
        c = q0[:, 0] ** 2 + q0[:, 1] ** 2 - self.radius ** 2
        disc = b * b - 4 * a * c
        radial = a > EPS
        root = np.sqrt(np.where(disc > 0, disc, 0.0))
        safe = np.where(radial, a, 1.0)
        r0 = np.where(radial, np.where(disc >= 0, (-b - root) / (2 * safe), np.inf), np.where(c <= 0, -np.inf, np.inf))
        r1 = np.where(radial, np.where(disc >= 0, (-b + root) / (2 * safe), -np.inf), np.where(c <= 0, np.inf, -np.inf))
        dz = d[:, 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            za = (-self.half_height - q0[:, 2]) / dz
            zb = (self.half_height - q0[:, 2]) / dz
        flat = np.abs(dz) < EPS
        inside = np.abs(q0[:, 2]) <= self.half_height
        z0 = np.where(flat, np.where(inside, -np.inf, np.inf), np.minimum(za, zb))
        z1 = np.where(flat, np.where(inside, np.inf, -np.inf), np.maximum(za, zb))
        return _clip_interval(np.maximum(r0, z0), np.minimum(r1, z1))

    def _intersect(self, q0, d):
        a = d[:, 0] ** 2 + d[:, 1] ** 2
        b = 2 * (q0[:, 0] * d[:, 0] + q0[:, 1] * d[:, 1])
        c = q0[:, 0] ** 2 + q0[:, 1] ** 2 - self.radius ** 2
        disc = b * b - 4 * a * c
        radial = a > EPS
        root = np.sqrt(np.where(disc > 0, disc, 0.0))
        safe = np.where(radial, a, 1.0)
        r0 = np.where(radial, np.where(disc >= 0, (-b - root) / (2 * safe), np.inf), np.where(c <= 0, -np.inf, np.inf))
        r1 = np.where(radial, np.where(disc >= 0, (-b + root) / (2 * safe), -np.inf), np.where(c <= 0, np.inf, -np.inf))
        dz = d[:, 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            za = (-self.half_height - q0[:, 2]) / dz
            zb = (self.half_height - q0[:, 2]) / dz
        flat = np.abs(dz) < EPS
        inside = np.abs(q0[:, 2]) <= self.half_height
        z0 = np.where(flat, np.where(inside, -np.inf, np.inf), np.minimum(za, zb))
        z1 = np.where(flat, np.where(inside, np.inf, -np.inf), np.maximum(za, zb))
        enter, leave = np.maximum(r0, z0), np.minimum(r1, z1)
        t = _first_positive(leave >= np.maximum(enter, 0.0), enter, leave)
        point = q0 + d * np.where(np.isfinite(t), t, 0.0)[:, None]
        on_cap = np.abs(np.abs(point[:, 2]) - self.half_height) < 1e-7 * max(1.0, self.half_height)
        normal = np.where(on_cap[:, None], np.column_stack([np.zeros(len(t)), np.zeros(len(t)), np.sign(point[:, 2])]),
                          np.column_stack([point[:, 0], point[:, 1], np.zeros(len(t))]))
        return t, _normalize(normal)


@dataclass
class Capsule(Primitive):
    """Cylinder of `half_height` (cap centres at +-half_height on local z) with hemispherical caps."""
    radius: float = 0.05
    half_height: float = 0.1
    kind = "capsule"

    def _hits(self, q0, d):
        # Distance between the segment q0 + s d (s in 0..1) and the axis segment from -h to +h.
        axis = np.array([0.0, 0.0, 2 * self.half_height])
        r = q0 - np.array([0.0, 0.0, -self.half_height])
        a = np.einsum("ij,ij->i", d, d)
        e = float(axis @ axis)
        f = r @ axis
        c = np.einsum("ij,ij->i", d, r)
        b = d @ axis
        safe_a = np.where(a > EPS, a, 1.0)
        if e <= EPS:                                       # a sphere
            s = np.clip(-c / safe_a, 0.0, 1.0)
            t = np.zeros_like(s)
        else:
            denom = a * e - b * b
            s = np.where(denom > EPS, np.clip((b * f - c * e) / np.where(denom > EPS, denom, 1.0), 0.0, 1.0), 0.0)
            t = (b * s + f) / e
            low, high = t < 0.0, t > 1.0
            s = np.where(low, np.clip(-c / safe_a, 0.0, 1.0), np.where(high, np.clip((b - c) / safe_a, 0.0, 1.0), s))
            t = np.clip(t, 0.0, 1.0)
        gap = r + d * s[:, None] - axis[None, :] * t[:, None]
        return np.einsum("ij,ij->i", gap, gap) <= self.radius ** 2

    def _intersect(self, q0, d):
        h, rad = self.half_height, self.radius
        candidates = []
        # Cylinder part: radial roots whose height lies within the caps' centres.
        a = d[:, 0] ** 2 + d[:, 1] ** 2
        b = 2 * (q0[:, 0] * d[:, 0] + q0[:, 1] * d[:, 1])
        c = q0[:, 0] ** 2 + q0[:, 1] ** 2 - rad ** 2
        disc = b * b - 4 * a * c
        ok = (a > EPS) & (disc >= 0)
        root = np.sqrt(np.where(ok, disc, 0.0))
        safe = np.where(a > EPS, a, 1.0)
        for t in ((-b - root) / (2 * safe), (-b + root) / (2 * safe)):
            z = q0[:, 2] + t * d[:, 2]
            candidates.append(np.where(ok & (t > 1e-9) & (np.abs(z) <= h), t, np.inf))
        # End spheres.
        aa = np.einsum("ij,ij->i", d, d)
        for zc in (-h, h):
            oc = q0 - np.array([0.0, 0.0, zc])
            bb = 2 * np.einsum("ij,ij->i", oc, d)
            cc = np.einsum("ij,ij->i", oc, oc) - rad ** 2
            disc2 = bb * bb - 4 * aa * cc
            ok2 = (disc2 >= 0) & (aa > EPS)
            root2 = np.sqrt(np.where(ok2, disc2, 0.0))
            safe2 = np.where(aa > EPS, aa, 1.0)
            for t in ((-bb - root2) / (2 * safe2), (-bb + root2) / (2 * safe2)):
                z = q0[:, 2] + t * d[:, 2]
                outer = np.ones(len(t), dtype=bool) if h == 0 else (z - zc) * np.sign(zc) >= 0   # the cap half
                candidates.append(np.where(ok2 & (t > 1e-9) & outer, t, np.inf))
        t = np.min(np.stack(candidates), axis=0)
        point = q0 + d * np.where(np.isfinite(t), t, 0.0)[:, None]
        axis_point = np.column_stack([np.zeros(len(t)), np.zeros(len(t)), np.clip(point[:, 2], -h, h)])
        return t, _normalize(point - axis_point)


def Sphere(name: str = "", position=None, radius: float = 0.1, group: str = "obstacle") -> Capsule:
    """A sphere is a capsule without a cylinder part."""
    return Capsule(name=name, position=np.zeros(3) if position is None else np.asarray(position, dtype=float),
                   radius=radius, half_height=0.0, group=group)


@dataclass
class Scene:
    """A set of primitives the light passes or not."""
    primitives: list[Primitive] = field(default_factory=list)

    def blocked(self, starts: np.ndarray, ends: np.ndarray, groups: Iterable[str] | None = None) -> np.ndarray:
        starts = np.atleast_2d(np.asarray(starts, dtype=float))
        ends = np.atleast_2d(np.asarray(ends, dtype=float))
        out = np.zeros(len(starts), dtype=bool)
        wanted = None if groups is None else set(groups)
        for prim in self.primitives:
            if wanted is not None and prim.group not in wanted:
                continue
            todo = ~out
            if not todo.any():
                break
            out[todo] = prim.hits(starts[todo], ends[todo])
        return out

    def first_hit(self, starts: np.ndarray, directions: np.ndarray, groups: Iterable[str] | None = None):
        """Nearest hit along rays: distance (inf on a miss), outward normal, primitive index (-1)."""
        starts = np.atleast_2d(np.asarray(starts, dtype=float))
        directions = np.atleast_2d(np.asarray(directions, dtype=float))
        best = np.full(len(starts), np.inf)
        normal = np.zeros_like(starts)
        owner = np.full(len(starts), -1)
        wanted = None if groups is None else set(groups)
        for index, prim in enumerate(self.primitives):
            if wanted is not None and prim.group not in wanted:
                continue
            t, n = prim.intersect(starts, directions)
            closer = t < best
            best[closer], normal[closer], owner[closer] = t[closer], n[closer], index
        return best, normal, owner

    def find(self, name: str) -> Primitive:
        for prim in self.primitives:
            if prim.name == name:
                return prim
        raise KeyError(f"scene has no primitive {name!r}")

    def replace(self, name: str, primitive: Primitive) -> None:
        for i, prim in enumerate(self.primitives):
            if prim.name == name:
                self.primitives[i] = primitive
                return
        self.primitives.append(primitive)


# -- YAML description ---------------------------------------------------------------------
def _length(raw: Any) -> float:
    from ..quantities import Quantity
    return Quantity.parse(raw, "m").to("m")


def _vector(raw: Any, default=(0.0, 0.0, 0.0)) -> np.ndarray:
    if raw is None:
        return np.asarray(default, dtype=float)
    if len(raw) != 3:
        raise ValueError(f"expected a 3-vector, got {raw!r}")
    return np.asarray([_length(v) for v in raw], dtype=float)


def _rotation(raw: Any) -> np.ndarray:
    """`rotation: [ax, ay, az, angle]` (angle in rad or with a unit, e.g. '90 deg')."""
    if raw is None:
        return np.eye(3)
    from ..quantities import Quantity
    if len(raw) != 4:
        raise ValueError(f"rotation must be [x, y, z, angle], got {raw!r}")
    return rotation_matrix([float(v) for v in raw[:3]], Quantity.parse(raw[3], "rad").to("rad"))


def primitive_from_mapping(raw: Mapping[str, Any]) -> Primitive:
    kind = str(raw.get("type", "")).lower()
    name = str(raw.get("name", kind))
    group = str(raw.get("group", "obstacle"))
    position = _vector(raw.get("position"))
    rotation = _rotation(raw.get("rotation"))
    if kind in ("from", "segment") or ("from" in raw and "to" in raw):
        position, rotation, length = frame_between(_vector(raw["from"]), _vector(raw["to"]))
        half = length / 2
        radius = _length(raw["radius"])
        if kind == "cylinder":
            return Cylinder(name, position, rotation, group, radius=radius, half_height=half)
        return Capsule(name, position, rotation, group, radius=radius, half_height=half)
    if kind == "box":
        return Box(name, position, rotation, group, half=_vector(raw["size"]) / 2)
    if kind == "sphere":
        return Capsule(name, position, rotation, group, radius=_length(raw["radius"]), half_height=0.0)
    if kind == "ellipsoid":
        return Ellipsoid(name, position, rotation, group, radii=_vector(raw["radii"]))
    if kind == "cylinder":
        return Cylinder(name, position, rotation, group, radius=_length(raw["radius"]),
                        half_height=_length(raw["height"]) / 2)
    if kind == "capsule":
        return Capsule(name, position, rotation, group, radius=_length(raw["radius"]),
                       half_height=_length(raw["height"]) / 2)
    raise ValueError(f"unknown primitive type {kind!r} (box, sphere, ellipsoid, cylinder, capsule)")


def scene_from_mapping(raw: Iterable[Mapping[str, Any]] | None) -> Scene:
    prims = []
    for item in raw or []:
        prim = primitive_from_mapping(item)
        if item.get("albedo") is not None:
            prim.albedo = float(item["albedo"])
        prims.append(prim)
    return Scene(prims)
