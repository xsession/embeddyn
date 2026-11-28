"""Board photos: load (EXIF rotation applied), find the board by its solder-mask colour, and straighten it.

A handheld photo shows the board in perspective. The board outline (the largest region of the dominant saturated
hue, holes filled) gives four corners; a homography maps them onto a rectangle, so every later step works in
board millimetres. Numpy and scipy only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage


@dataclass
class BoardImage:
    """A straightened view of one side of the board."""
    rgb: np.ndarray                 # H x W x 3 uint8, board coordinates (x right, y down), top side seen from above
    px_per_mm: float
    source: Path
    side: str                       # "top" | "bottom"
    corners: np.ndarray             # 4 x 2 corners in the original photo (tl, tr, br, bl)
    homography: np.ndarray          # 3x3, board pixel -> photo pixel
    mask_hue: float                 # 0..1
    mirrored: bool = False          # bottom photos are mirrored so that they line up with the top
    notes: list[str] = field(default_factory=list)

    @property
    def size_mm(self) -> tuple[float, float]:
        return self.rgb.shape[1] / self.px_per_mm, self.rgb.shape[0] / self.px_per_mm


def load(path: Path, max_side: int = 0) -> np.ndarray:
    image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    if max_side and max(image.size) > max_side:
        scale = max_side / max(image.size)
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    return np.asarray(image)


def hsv(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Hue, saturation, value in 0..1 (float32)."""
    a = rgb.astype(np.float32) / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    d = mx - mn
    s = np.where(mx > 0, d / np.maximum(mx, 1e-6), 0.0)
    h = np.zeros_like(mx)
    nz = d > 1e-6
    rm = nz & (mx == r)
    gm = nz & (mx == g) & ~rm
    bm = nz & ~rm & ~gm
    h[rm] = ((g - b)[rm] / d[rm]) % 6
    h[gm] = (b - r)[gm] / d[gm] + 2
    h[bm] = (r - g)[bm] / d[bm] + 4
    return (h / 6.0).astype(np.float32), s.astype(np.float32), mx.astype(np.float32)


def hue_distance(h: np.ndarray, centre: float) -> np.ndarray:
    d = np.abs(h - centre)
    return np.minimum(d, 1.0 - d)


def mask_hue(h: np.ndarray, s: np.ndarray, v: np.ndarray) -> float:
    """The solder-mask hue: the most common hue among saturated pixels (green, blue, red, black boards...)."""
    sel = (s > 0.25) & (v > 0.2)
    if sel.sum() < 100:
        return 0.45
    hist, edges = np.histogram(h[sel], bins=180, range=(0, 1))
    hist = ndimage.uniform_filter1d(hist.astype(float), 5, mode="wrap")
    return float((edges[np.argmax(hist)] + edges[np.argmax(hist) + 1]) / 2)


def board_region(rgb: np.ndarray) -> tuple[np.ndarray, float]:
    """Pixels of the board (mask colour, holes filled) and the mask hue."""
    h, s, v = hsv(rgb)
    hue = mask_hue(h, s, v)
    green = (hue_distance(h, hue) < 0.06) & (s > 0.2) & (v > 0.15)
    green = ndimage.binary_opening(green, iterations=2)
    labels, n = ndimage.label(green)
    if n == 0:
        raise ValueError("no solder-mask coloured region found: is the board in the picture?")
    sizes = ndimage.sum(green, labels, range(1, n + 1))
    board = labels == (1 + int(np.argmax(sizes)))
    board = ndimage.binary_closing(board, iterations=max(3, rgb.shape[0] // 150))
    board = ndimage.binary_fill_holes(board)
    return board, hue


def _hull(points: np.ndarray) -> np.ndarray:
    pts = sorted(map(tuple, points))
    if len(pts) <= 2:
        return np.array(pts, dtype=float)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.array(lower[:-1] + upper[:-1], dtype=float)


def quad_corners(region: np.ndarray) -> np.ndarray:
    """Four corners (tl, tr, br, bl, as x, y) of a roughly rectangular region seen in perspective.

    The quadrilateral with the largest area whose corners are on the convex hull, found by refining the four
    extreme points along the diagonals."""
    edge = region & ~ndimage.binary_erosion(region)
    ys, xs = np.nonzero(edge)
    step = max(1, len(xs) // 6000)
    hull = _hull(np.column_stack([xs[::step], ys[::step]]))
    s, d = hull.sum(axis=1), hull[:, 0] - hull[:, 1]
    corners = [int(np.argmin(s)), int(np.argmax(d)), int(np.argmax(s)), int(np.argmin(d))]

    def area(idx):
        p = hull[idx]
        return 0.5 * abs(np.dot(p[:, 0], np.roll(p[:, 1], -1)) - np.dot(p[:, 1], np.roll(p[:, 0], -1)))
    best = area(corners)
    improved = True
    while improved:
        improved = False
        for k in range(4):
            for cand in range(len(hull)):
                trial = corners.copy()
                trial[k] = cand
                a = area(trial)
                if a > best + 1e-6 and len(set(trial)) == 4:
                    best, corners, improved = a, trial, True
    quad = hull[corners]
    # order: tl, tr, br, bl
    c = quad.mean(axis=0)
    ang = np.arctan2(quad[:, 1] - c[1], quad[:, 0] - c[0])
    quad = quad[np.argsort(ang)]          # clockwise on screen starting near -pi (left)
    start = int(np.argmin(quad.sum(axis=1)))
    return np.roll(quad, -start, axis=0)


def homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """3x3 H with dst ~ H @ src (four or more point pairs)."""
    rows = []
    for (x, y), (u, v) in zip(src, dst):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    _, _, vt = np.linalg.svd(np.asarray(rows, dtype=float))
    H = vt[-1].reshape(3, 3)
    return H / H[2, 2]


def warp(rgb: np.ndarray, H_dst_to_src: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    hgt, wid = shape
    yy, xx = np.mgrid[0:hgt, 0:wid].astype(np.float64)
    pts = np.stack([xx.ravel(), yy.ravel(), np.ones(xx.size)])
    src = H_dst_to_src @ pts
    sx, sy = src[0] / src[2], src[1] / src[2]
    out = np.empty((hgt, wid, rgb.shape[2]), dtype=np.uint8)
    for c in range(rgb.shape[2]):
        out[..., c] = ndimage.map_coordinates(rgb[..., c], [sy.reshape(hgt, wid), sx.reshape(hgt, wid)],
                                              order=1, mode="constant", cval=0).astype(np.uint8)
    return out


def straighten(path: Path, side: str = "top", board_mm: tuple[float, float] | None = None,
               px_per_mm: float = 20.0, mirror: bool | None = None) -> BoardImage:
    """The board in ``path`` as a straight, top-view image at ``px_per_mm``.

    ``board_mm`` (width, height) fixes the scale; without it the board's pixel size in the photo is kept and the
    scale is estimated later (from parts of known size). Bottom photos are mirrored left-right so that they line
    up with the top (as if seen through the board)."""
    rgb = load(path)
    work_scale = 1600 / max(rgb.shape[:2])
    small = load(path, 1600) if work_scale < 1 else rgb
    region, hue = board_region(small)
    corners = quad_corners(region) / (work_scale if work_scale < 1 else 1.0)
    top_w = np.linalg.norm(corners[1] - corners[0])
    bot_w = np.linalg.norm(corners[2] - corners[3])
    left_h = np.linalg.norm(corners[3] - corners[0])
    right_h = np.linalg.norm(corners[2] - corners[1])
    w_px, h_px = max(top_w, bot_w), max(left_h, right_h)
    notes = []
    if board_mm:
        width_mm, height_mm = board_mm
        if (w_px > h_px) != (width_mm > height_mm) and abs(width_mm - height_mm) > 1:
            width_mm, height_mm = height_mm, width_mm
            notes.append("board size given as width x height was turned to match the photo")
        scale = px_per_mm
    else:
        # no size: keep about the photo's resolution, assume 1 px = 1/px_per_mm mm for now
        width_mm, height_mm = w_px / px_per_mm, h_px / px_per_mm
        scale = px_per_mm
        notes.append("board size unknown: scale estimated later from parts of known size")
    out_w, out_h = int(round(width_mm * scale)), int(round(height_mm * scale))
    dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype=float)
    H = homography(dst, corners)
    image = warp(rgb, H, (out_h, out_w))
    mirrored = side == "bottom" if mirror is None else mirror
    if mirrored:
        image = image[:, ::-1].copy()
    return BoardImage(image, scale, path, side, corners, H, hue, mirrored, notes)


def save(image: np.ndarray, path: Path) -> Path:
    Image.fromarray(image).save(path)
    return path


def angle_of(points: np.ndarray) -> float:
    """Main axis angle (radians) of a point cloud."""
    c = points - points.mean(axis=0)
    cov = c.T @ c / max(1, len(c))
    w, v = np.linalg.eigh(cov)
    axis = v[:, int(np.argmax(w))]
    return math.atan2(axis[1], axis[0])
