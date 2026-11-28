"""Copper connectivity: conductor regions on each side, vias, and the two sides joined into nets.

Conductor is copper under the mask plus bare metal, without part bodies. Connected conductor regions are nets on
one side; a via (a hole in a metal ring) or a through-hole pad joins the regions it sits in on both sides. The
bottom photo is fitted onto the top by matching holes (vias and pads) with an affine transform (iterative closest
points, trimmed), starting from the board outline.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree

from .segment import Classes


@dataclass
class Side:
    name: str
    labels: np.ndarray                  # conductor region per pixel (0 = none)
    count: int
    vias: np.ndarray                    # N x 2 hole centres (x, y), this side's pixels


@dataclass
class Connectivity:
    top: Side
    bottom: Side | None
    bottom_to_top: np.ndarray | None    # 2x3 affine: bottom pixel -> top pixel
    joins: list[tuple[int, int]] = field(default_factory=list)   # (top region, bottom region) joined by a via
    notes: list[str] = field(default_factory=list)


def conductor_regions(classes: Classes, bodies: np.ndarray, px_per_mm: float, name: str) -> Side:
    conductor = (classes.copper | classes.metal | classes.hole) & ~ndimage.binary_erosion(
        bodies, iterations=max(1, int(0.15 * px_per_mm)))
    conductor = ndimage.binary_closing(conductor, structure=np.ones((3, 3)))
    # thin bright specks (glints, mask reflections) are not copper: keep regions of a useful size
    labels, n = ndimage.label(conductor, structure=np.ones((3, 3)))
    if n:
        sizes = ndimage.sum(conductor, labels, range(1, n + 1))
        keep = np.concatenate([[False], sizes >= (0.25 * px_per_mm) ** 2])
        labels = np.where(keep[labels], labels, 0)
        labels, n = ndimage.label(labels > 0, structure=np.ones((3, 3)))
    vias = hole_centres(classes, px_per_mm)
    return Side(name, labels, n, vias)


def hole_centres(classes: Classes, px_per_mm: float) -> np.ndarray:
    labels, n = ndimage.label(classes.hole)
    if n == 0:
        return np.zeros((0, 2))
    c = ndimage.center_of_mass(classes.hole, labels, range(1, n + 1))
    return np.array([(x, y) for y, x in c])


def _affine(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    a = np.hstack([src, np.ones((len(src), 1))])
    m, *_ = np.linalg.lstsq(a, dst, rcond=None)
    return m.T                                   # 2x3


def apply(m: np.ndarray, pts: np.ndarray) -> np.ndarray:
    return pts @ m[:, :2].T + m[:, 2]


def align(bottom_pts: np.ndarray, top_pts: np.ndarray, init: np.ndarray, px_per_mm: float,
          rounds: int = 30) -> tuple[np.ndarray, float, int]:
    """Affine transform bottom -> top refining ``init`` by trimmed ICP on hole positions.
    Returns the transform, the median residual (px) and the number of matched holes."""
    if len(bottom_pts) < 6 or len(top_pts) < 6:
        return init, float("nan"), 0
    tree = cKDTree(top_pts)
    m = init.copy()
    radius = 3.0 * px_per_mm
    matched = 0
    residual = float("nan")
    for k in range(rounds):
        moved = apply(m, bottom_pts)
        d, idx = tree.query(moved, distance_upper_bound=radius)
        ok = np.isfinite(d)
        if ok.sum() < 6:
            break
        keep = ok & (d <= np.percentile(d[ok], 80))
        m_new = _affine(bottom_pts[keep], top_pts[idx[keep]])
        residual, matched = float(np.median(d[ok])), int(ok.sum())
        if np.abs(m_new - m).max() < 1e-3:
            m = m_new
            break
        m = m_new
        radius = max(0.4 * px_per_mm, radius * 0.7)
    return m, residual, matched


def join_sides(top: Side, bottom: Side | None, bottom_to_top: np.ndarray | None,
               px_per_mm: float) -> Connectivity:
    conn = Connectivity(top, bottom, bottom_to_top)
    if bottom is None or bottom_to_top is None:
        conn.notes.append("top side only: nets that change sides through vias are split")
        return conn
    inv = np.linalg.inv(np.vstack([bottom_to_top, [0, 0, 1]]))[:2]
    r = max(1, int(0.25 * px_per_mm))
    via_points = list(top.vias) + list(apply(bottom_to_top, bottom.vias)) if len(bottom.vias) else list(top.vias)
    seen = set()
    for x, y in via_points:
        t = _region_near(top.labels, x, y, r)
        bx, by = apply(inv, np.array([[x, y]]))[0]
        b = _region_near(bottom.labels, bx, by, r)
        if t and b and (t, b) not in seen:
            seen.add((t, b))
            conn.joins.append((t, b))
    return conn


def _region_near(labels: np.ndarray, x: float, y: float, r: int) -> int:
    xi, yi = int(round(x)), int(round(y))
    if not (0 <= yi < labels.shape[0] and 0 <= xi < labels.shape[1]):
        return 0
    window = labels[max(0, yi - r):yi + r + 1, max(0, xi - r):xi + r + 1]
    vals = window[window > 0]
    if not vals.size:
        return 0
    return int(np.bincount(vals).argmax())


def net_ids(conn: Connectivity) -> dict[tuple[str, int], int]:
    """(side, region) -> net number, regions joined through vias sharing one."""
    parent: dict[tuple[str, int], tuple[str, int]] = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for t, b in conn.joins:
        ra, rb = find(("top", t)), find(("bottom", b))
        if ra != rb:
            parent[ra] = rb
    for k in range(1, conn.top.count + 1):
        find(("top", k))
    if conn.bottom is not None:
        for k in range(1, conn.bottom.count + 1):
            find(("bottom", k))
    roots: dict[tuple[str, int], int] = {}
    out = {}
    for key in parent:
        root = find(key)
        out[key] = roots.setdefault(root, len(roots) + 1)
    return out
