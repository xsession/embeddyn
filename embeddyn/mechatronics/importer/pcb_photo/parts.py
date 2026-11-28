"""Parts on a straightened board photo: bodies, their pads, package, marking and pin numbers.

A part is a dark or light body region (rectangle-like, 0.3 mm² to 4 cm²) with the metal blobs around it as its
pads. The body's main axis and extent give its size in mm, the pads' layout (how many, on which sides) and the
size give the package (``embeddyn.mechatronics.smd.classify``); the marking is read by OCR on a crop turned to the
part's axis, and decoded (``embeddyn.mechatronics.smd.decode``); the reading direction of the marking orients the
pin numbering.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage

from ...smd import classify as classify_package, decode, number_pins
from ...smd.packages import Package, package as find_package
from .segment import Classes


@dataclass
class Pad:
    id: int
    x: float                  # board pixels
    y: float
    area: float               # px²
    part: int | None = None   # index of the part it belongs to
    pin: str = ""
    guessed: bool = True
    hole: bool = False
    side: str = "top"


@dataclass
class Part:
    id: int
    ref: str
    tone: str                         # dark | light
    cx: float
    cy: float
    angle: float                      # radians, long axis
    length: float                     # px
    width: float                      # px
    pads: list[int] = field(default_factory=list)
    package: str = ""
    package_score: float = 0.0
    candidates: list[str] = field(default_factory=list)
    kind: str = ""
    marking: str = ""
    marking_conf: float = 0.0
    up: tuple[float, float] = (0.0, -1.0)     # reading direction of the marking (unit vector, image coords)
    value: str = ""
    part_number: str = ""
    decoded: list[dict] = field(default_factory=list)
    side: str = "top"
    notes: list[str] = field(default_factory=list)

    def corners(self) -> list[tuple[float, float]]:
        ux, uy = math.cos(self.angle), math.sin(self.angle)
        vx, vy = -uy, ux
        hl, hw = self.length / 2, self.width / 2
        return [(self.cx + sx * hl * ux + sy * hw * vx, self.cy + sx * hl * uy + sy * hw * vy)
                for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def _labels(mask: np.ndarray, px_per_mm: float, fill: bool = False) -> tuple[np.ndarray, int]:
    r = max(1, int(round(0.08 * px_per_mm)))
    m = ndimage.binary_closing(mask, structure=np.ones((2 * r + 1, 2 * r + 1)))
    if fill:                                  # marking text inside a body would otherwise cut it apart
        m = ndimage.binary_fill_holes(m)
    m = ndimage.binary_opening(m, structure=np.ones((2 * r + 1, 2 * r + 1)))
    return ndimage.label(m)


def _dark_with_text(classes: Classes, px_per_mm: float) -> np.ndarray:
    """Dark bodies including the light marking text printed on them."""
    near = ndimage.binary_dilation(classes.dark, iterations=max(1, int(0.12 * px_per_mm)))
    return classes.dark | ((classes.silk | classes.metal) & near & ~ndimage.binary_opening(
        classes.metal, iterations=max(1, int(0.2 * px_per_mm))))


def body_mask(classes: Classes, px_per_mm: float) -> np.ndarray:
    """Pixels covered by part bodies (holes filled): metal glints on a body are not pads."""
    dark, _ = _labels(_dark_with_text(classes, px_per_mm), px_per_mm, fill=True)
    light, _ = _labels(classes.light, px_per_mm, fill=True)
    return (dark > 0) | (light > 0)


def _strip_leads(region: np.ndarray, px_per_mm: float) -> np.ndarray:
    """Remove thin protrusions (gull-wing leads in shade) from a body region when most of it survives."""
    r = int(round(0.3 * px_per_mm))
    if r < 2 or min(region.shape) < 4 * r:
        return region
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    opened = ndimage.binary_opening(region, structure=x * x + y * y <= r * r)
    return opened if opened.sum() > 0.6 * region.sum() else region


def _oriented(ys: np.ndarray, xs: np.ndarray) -> tuple[float, float, float, float, float]:
    """Centre, angle and robust extents (length, width) of a pixel set."""
    cx, cy = xs.mean(), ys.mean()
    dx, dy = xs - cx, ys - cy
    # the rotation with the smallest bounding box (a principal axis is unstable for square bodies); parts are
    # mostly placed square to the board, so 0°/90° wins unless another angle is clearly tighter
    step = max(1, len(xs) // 4000)
    sx, sy = dx[::step], dy[::step]
    best = None
    for deg in range(0, 90, 2):
        a = math.radians(deg)
        u = sx * math.cos(a) + sy * math.sin(a)
        t = -sx * math.sin(a) + sy * math.cos(a)
        area = (np.percentile(u, 98.5) - np.percentile(u, 1.5)) * (np.percentile(t, 98.5) - np.percentile(t, 1.5))
        if deg == 0:
            area *= 0.93
        if best is None or area < best[0]:
            best = (area, a)
    angle = best[1]
    u = dx * math.cos(angle) + dy * math.sin(angle)
    t = -dx * math.sin(angle) + dy * math.cos(angle)
    lo_u, hi_u = np.percentile(u, [1.5, 98.5])
    lo_t, hi_t = np.percentile(t, [1.5, 98.5])
    ccx = cx + (lo_u + hi_u) / 2 * math.cos(angle) - (lo_t + hi_t) / 2 * math.sin(angle)
    ccy = cy + (lo_u + hi_u) / 2 * math.sin(angle) + (lo_t + hi_t) / 2 * math.cos(angle)
    length, width = hi_u - lo_u + 1, hi_t - lo_t + 1
    if width > length:
        length, width = width, length
        angle += math.pi / 2
    angle = (angle + math.pi / 2) % math.pi - math.pi / 2
    return ccx, ccy, angle, length, width


def find_pads(classes: Classes, px_per_mm: float, side: str = "top",
              bodies: np.ndarray | None = None) -> tuple[list[Pad], np.ndarray]:
    metal = classes.metal | classes.hole
    if bodies is not None:
        metal = metal & ~ndimage.binary_erosion(bodies, iterations=max(1, int(0.1 * px_per_mm)))
    labels, n = _labels(metal, px_per_mm)
    pads = []
    if n == 0:
        return pads, labels
    idx = np.arange(1, n + 1)
    areas = ndimage.sum(np.ones_like(labels), labels, idx)
    centres = ndimage.center_of_mass(np.ones_like(labels), labels, idx)
    holes = ndimage.maximum(classes.hole, labels, idx)
    min_area = (0.15 * px_per_mm) ** 2
    keep = np.zeros(n + 1, bool)
    for i, (a, (cy, cx), hole) in enumerate(zip(areas, centres, holes), 1):
        if a < min_area:
            continue
        keep[i] = True
        pads.append(Pad(i, float(cx), float(cy), float(a), hole=bool(hole), side=side))
    labels = np.where(keep[labels], labels, 0)
    return pads, labels


def find_parts(rgb: np.ndarray, classes: Classes, px_per_mm: float, pads: list[Pad], pad_labels: np.ndarray,
               side: str = "top") -> list[Part]:
    parts: list[Part] = []
    pad_by_label = {p.id: p for p in pads}
    reach = max(2, int(round(1.0 * px_per_mm)))
    for tone, mask in (("dark", _dark_with_text(classes, px_per_mm)), ("light", classes.light)):
        labels, n = _labels(mask, px_per_mm, fill=True)
        if n == 0:
            continue
        objs = ndimage.find_objects(labels)
        for i, sl in enumerate(objs, 1):
            if sl is None:
                continue
            region = _strip_leads(labels[sl] == i, px_per_mm)
            if not region.any():
                continue
            area_mm2 = region.sum() / px_per_mm ** 2
            if not (0.3 <= area_mm2 <= 400):
                continue
            ys, xs = np.nonzero(region)
            cx, cy, angle, length, width = _oriented(ys + sl[0].start, xs + sl[1].start)
            if width < 0.35 * px_per_mm or length / max(width, 1) > 8:
                continue
            fill = region.sum() / max(length * width, 1)
            if fill < 0.5:
                continue
            # pads touching the body (dilated by ~0.45 mm)
            y0, y1 = max(0, sl[0].start - reach), min(labels.shape[0], sl[0].stop + reach)
            x0, x1 = max(0, sl[1].start - reach), min(labels.shape[1], sl[1].stop + reach)
            body = np.zeros((y1 - y0, x1 - x0), bool)
            body[sl[0].start - y0:sl[0].stop - y0, sl[1].start - x0:sl[1].stop - x0] = region
            near = ndimage.binary_dilation(body, iterations=reach)
            touching = np.unique(pad_labels[y0:y1, x0:x1][near])
            part = Part(len(parts), "", tone, cx, cy, angle, length, width,
                        pads=[int(t) for t in touching if t and t in pad_by_label], side=side)
            parts.append(part)
    _resolve_shared_pads(parts, pad_by_label)
    # a body without pads is a logo, a label or silkscreen, unless it is large (clips, connectors, shields)
    return [p for p in parts if (len(p.pads) >= 2 or p.length * p.width / px_per_mm ** 2 > 6.0)]


def _resolve_shared_pads(parts: list[Part], pad_by_label: dict[int, Pad]) -> None:
    """A pad near two bodies belongs to the one whose outline is nearer."""
    owner: dict[int, tuple[float, int]] = {}
    for k, part in enumerate(parts):
        for pid in part.pads:
            pad = pad_by_label[pid]
            d = _outline_distance(part, pad.x, pad.y)
            if pid not in owner or d < owner[pid][0]:
                owner[pid] = (d, k)
    for k, part in enumerate(parts):
        part.pads = [pid for pid in part.pads if owner[pid][1] == k]
        for pid in part.pads:
            pad_by_label[pid].part = part.id


def _outline_distance(part: Part, x: float, y: float) -> float:
    dx, dy = x - part.cx, y - part.cy
    u = abs(dx * math.cos(part.angle) + dy * math.sin(part.angle)) - part.length / 2
    t = abs(-dx * math.sin(part.angle) + dy * math.cos(part.angle)) - part.width / 2
    return math.hypot(max(u, 0), max(t, 0))


def pad_sides(part: Part, pads: dict[int, Pad]) -> int:
    sides = set()
    for pid in part.pads:
        p = pads[pid]
        dx, dy = p.x - part.cx, p.y - part.cy
        u = (dx * math.cos(part.angle) + dy * math.sin(part.angle)) / (part.length / 2)
        t = (-dx * math.sin(part.angle) + dy * math.cos(part.angle)) / (part.width / 2)
        if abs(u) >= abs(t):
            sides.add("u+" if u > 0 else "u-")
        else:
            sides.add("t+" if t > 0 else "t-")
    return len(sides)


def identify(part: Part, pads: dict[int, Pad], px_per_mm: float) -> None:
    length, width = part.length / px_per_mm, part.width / px_per_mm
    leads = len(part.pads)
    sides = pad_sides(part, pads)
    kind = None
    if leads == 2:
        # a chip part's visible body is shorter than the package (the metal end caps count): the two metal ends'
        # centres are ~0.93 of the package length apart
        a, b = (pads[p] for p in part.pads)
        along = abs((a.x - b.x) * math.cos(part.angle) + (a.y - b.y) * math.sin(part.angle)) / px_per_mm
        length = max(length, along / 0.93)
        kind = "capacitor" if part.tone == "light" else "resistor"
    matches = classify_package(length, width, leads or None, sides or None, kind)
    if matches:
        part.package = matches[0].package.name
        part.package_score = matches[0].score
        part.candidates = [m.package.name for m in matches[:4]]
        kinds = matches[0].package.kinds
        part.kind = (kind if kind and kind in kinds else
                     "capacitor" if part.tone == "light" and "capacitor" in kinds else
                     kinds[0] if kinds else "")


# --- OCR of markings ---------------------------------------------------------------------------------------------

def _crop(rgb: np.ndarray, part: Part, px_per_mm: float, rotation: int) -> Image.Image:
    """The part's marking area, turned by ``rotation`` degrees, as dark text on white for OCR."""
    margin = 0.15 * px_per_mm
    half = int(math.hypot(part.length, part.width) / 2 + margin + 2)
    x0, y0 = int(part.cx) - half, int(part.cy) - half
    pad = ((max(0, -y0), max(0, y0 + 2 * half - rgb.shape[0])), (max(0, -x0), max(0, x0 + 2 * half - rgb.shape[1])),
           (0, 0))
    window = np.pad(rgb, pad, mode="edge")[y0 + pad[0][0]:y0 + pad[0][0] + 2 * half,
                                           x0 + pad[1][0]:x0 + pad[1][0] + 2 * half]
    image = Image.fromarray(window).convert("L").rotate(math.degrees(part.angle) + rotation,
                                                        resample=Image.BICUBIC)
    long_frac = 0.72 if part.length > 1.6 * part.width else 0.92     # passives: skip the end caps
    length, width = part.length * long_frac, part.width * 0.9
    w, h = (length, width) if rotation % 180 == 0 else (width, length)
    c = image.width / 2
    image = image.crop((int(c - w / 2), int(c - h / 2), int(c + w / 2), int(c + h / 2)))
    gray = np.asarray(image, dtype=np.float32)
    if part.tone == "light":
        gray = 255 - gray                   # dark text on a light body: make the text the bright part
    # text = brighter than the body around it: remove the body's own shading and texture
    size = max(5, int(0.6 * px_per_mm)) | 1
    background = ndimage.median_filter(gray, size=size)
    text = ndimage.gaussian_filter(gray - background, sigma=max(0.6, 0.025 * px_per_mm))
    lo, hi = np.percentile(text, [50, 99.7])
    text = np.clip((text - lo) / max(hi - lo, 1e-3), 0, 1)
    image = Image.fromarray((255 - text * 255).astype(np.uint8))
    target = 36.0                            # px per mm of text height is what tesseract likes (~30 px glyphs)
    scale = max(0.5, min(6.0, 1.1 * target / px_per_mm * 1.0))
    if scale * min(image.size) < 40:
        scale = 40 / max(1, min(image.size))
    image = image.resize((max(8, int(image.width * scale)), max(8, int(image.height * scale))), Image.LANCZOS)
    border = Image.new("L", (image.width + 40, image.height + 40), 255)
    border.paste(image, (20, 20))
    return border


def _otsu(arr: np.ndarray) -> float:
    hist, edges = np.histogram(arr, bins=64, range=(0, 256))
    w = np.cumsum(hist)
    m = np.cumsum(hist * (edges[:-1] + 2))
    total, mt = w[-1], m[-1]
    between = (mt * w - m * total) ** 2 / np.maximum(w * (total - w), 1)
    return float(edges[int(np.argmax(between))] + 2)


def read_markings(rgb: np.ndarray, parts: list[Part], pads: dict[int, Pad], px_per_mm: float,
                  only_dark: bool = True) -> None:
    """OCR each part's marking in the orientations it can have; keep the reading that decodes best."""
    if not shutil.which("tesseract"):
        return
    jobs = []
    for part in parts:
        if only_dark and part.tone != "dark":
            continue
        if part.width / px_per_mm < 0.6:
            continue
        rotations = (0, 180) if part.length > 1.6 * part.width else (0, 90, 180, 270)
        for rot in rotations:
            jobs.append((part, rot))
    if not jobs:
        return
    pages: dict[int, list[tuple[int, int, str, float]]] = {}
    with tempfile.TemporaryDirectory(prefix="pcbocr") as tmp:
        batches: dict[str, list[int]] = {"7": [], "6": []}
        for k, (part, rot) in enumerate(jobs):
            _crop(rgb, part, px_per_mm, rot).save(Path(tmp) / f"{k:05d}.png")
            small = part.length / px_per_mm < 3.5 or len(part.pads) <= 3
            batches["7" if small else "6"].append(k)
        for psm, ks in batches.items():
            if not ks:
                continue
            listing = Path(tmp) / f"list{psm}.txt"
            listing.write_text("\n".join(str(Path(tmp) / f"{k:05d}.png") for k in ks) + "\n")
            out = subprocess.run(["tesseract", str(listing), "-", "--psm", psm, "-c",
                                  "tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
                                  "tsv"], capture_output=True, text=True, timeout=900).stdout
            for line in out.splitlines()[1:]:
                cols = line.split("\t")
                if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
                    continue
                page, block_line = ks[int(cols[1]) - 1], (int(cols[2]), int(cols[4]))
                pages.setdefault(page, []).append((*block_line, cols[11].strip(), float(cols[10])))
    best: dict[int, tuple[float, str, float, int]] = {}
    for k, (part, rot) in enumerate(jobs):
        words = pages.get(k, [])
        if not words:
            continue
        lines: dict[tuple[int, int], list[str]] = {}
        confs = []
        for blk, ln, word, conf in words:
            lines.setdefault((blk, ln), []).append(word)
            confs.append(max(conf, 0))
        text = "\n".join(" ".join(ws) for _, ws in sorted(lines.items()))
        conf = float(np.mean(confs)) / 100 if confs else 0.0
        decoded = decode(text, part.package or None)
        top = decoded.best.score if decoded.best else 0.0
        score = conf * (0.5 + top)
        if part.id not in best or score > best[part.id][0]:
            best[part.id] = (score, text, conf, rot)
    for part in parts:
        if part.id not in best:
            continue
        score, text, conf, rot = best[part.id]
        compact = text.replace("\n", "").replace(" ", "")
        decoded_check = decode(text, part.package or None)
        useful = decoded_check.best is not None and decoded_check.best.score >= 0.5
        if not compact or conf < 0.5 or not (useful or (conf >= 0.8 and len(compact) >= 3
                                                         and any(c.isdigit() for c in compact))):
            continue
        part.marking, part.marking_conf = text, round(conf, 2)
        # the crop was turned by (angle + rot) counter-clockwise; image "up" of the crop is the marking's up
        a = part.angle + math.radians(rot)
        part.up = (math.sin(a), -math.cos(a))
        decoded = decode(text, part.package or None, part.kind or None)
        part.decoded = [r.as_dict() for r in decoded.readings[:4]]
        if decoded.best:
            if decoded.best.kind == "value":
                part.value = decoded.best.value.text if decoded.best.value else decoded.best.part
                part.kind = decoded.best.value.kind if decoded.best.value else part.kind
            else:
                part.part_number = decoded.best.part
                part.value = decoded.best.part
                if decoded.best.package and find_package(decoded.best.package.split("-")[0] + "-"
                                                         + decoded.best.package.split("-")[-1] if "-" in decoded.best.package else decoded.best.package):
                    pass


def number(part: Part, pads: dict[int, Pad], layout: str | None = None) -> None:
    pts = [(pads[p].x, pads[p].y) for p in part.pads]
    if not pts:
        return
    pkg = find_package(part.package) if part.package else None
    layout = layout or (pkg.layout if pkg else "dual")
    if len(pts) == 2:
        layout = "two-terminal"
    # without a read marking, "up" is unknown: number from the body's own axis and call the numbers guessed
    up = part.up
    known = bool(part.marking)
    if not known:
        up = (math.sin(part.angle), -math.cos(part.angle)) if layout != "two-terminal" else (0.0, -1.0)
    numbers = number_pins(pts, layout, up, (part.cx, part.cy))
    for pid, n in zip(part.pads, numbers):
        pads[pid].pin = str(n)
        pads[pid].guessed = not known or layout == "two-terminal"
