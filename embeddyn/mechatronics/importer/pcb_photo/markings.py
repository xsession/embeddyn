"""Markings on part bodies, read text-first.

``MarkingReader(rgb, dark)`` finds the bodies that may carry text (dark regions; packed parts may form one
region, every word keeps its own position), turns each to its long axis in every orientation it can have, removes
the body's own texture (a white top-hat about a third of the body across) and reads the text two ways:

* tesseract, on the cleaned grey image and a binarised copy;
* a character-template reader (``glyphs``): characters split by shape, matched against characters rendered in
  sans fonts and, above all, characters learned from markings someone confirmed on this board's parts.

The orientation and reading whose words decode best win. The reader keeps its crops, so ``learn`` (a marking a
person typed for one body) followed by ``reread`` updates every body in seconds, without new OCR.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from ...smd import decode
from . import glyphs

WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


@dataclass
class Word:
    text: str
    conf: float              # 0..1
    x: float                 # centre in the photo (px)
    y: float
    up: tuple[float, float]  # the letters' up direction in the photo (unit vector, y down)
    body: int
    score: float = 0.0       # how well it decodes (0..1)
    source: str = ""         # tesseract | glyphs


@dataclass
class Body:
    id: int
    cx: float
    cy: float
    angle: float             # long axis, radians
    length: float            # px
    width: float
    words: list[Word] = field(default_factory=list)
    taught: str = ""         # a marking a person gave for this body
    word: bool = False       # a line of text found by itself, not a body
    chars: int = 0           # characters seen in it (words only)

    @property
    def chip(self) -> bool:
        """A resistor's or capacitor's code: digits and R only (a long narrow body, or a short word on one)."""
        if self.word:
            return self.chars <= 4 and self.length < 4.5 * self.width
        return self.length > 1.7 * self.width

    @property
    def text(self) -> str:
        """The marking: words in reading order, lines joined by newlines."""
        if not self.words:
            return ""
        ux, uy = self.words[0].up
        rx, ry = -uy, ux
        rows: list[list[Word]] = []
        for w in sorted(self.words, key=lambda w: -(w.x * ux + w.y * uy)):
            for row in rows:
                if abs((row[0].x - w.x) * ux + (row[0].y - w.y) * uy) < 0.25 * self.width:
                    row.append(w)
                    break
            else:
                rows.append([w])
        return "\n".join(" ".join(w.text for w in sorted(r, key=lambda w: w.x * rx + w.y * ry)) for r in rows)

    @property
    def conf(self) -> float:
        return float(np.mean([w.conf for w in self.words])) if self.words else 0.0


@dataclass
class Crop:
    body: Body
    rotation: int
    grey: np.ndarray          # cleaned, dark text on white, scaled for reading (with a 20 px frame)
    binary: np.ndarray
    scale: float
    box: tuple[int, int]
    window: tuple[int, int]
    angle: float
    half: int

    @property
    def up(self) -> tuple[float, float]:
        a = math.radians(self.angle)
        return (-math.sin(a), -math.cos(a))

    def back(self, u: float, v: float) -> tuple[float, float]:
        """A point of the reading image back in photo coordinates."""
        px = (u - 20) / self.scale + self.box[0] - self.half
        py = (v - 20) / self.scale + self.box[1] - self.half
        a = math.radians(self.angle)            # PIL's rotate(a) turns counter-clockwise on screen: undo it
        return (self.window[0] + self.half + px * math.cos(a) - py * math.sin(a),
                self.window[1] + self.half + px * math.sin(a) + py * math.cos(a))


def word_score(text: str, chip: bool = False) -> float:
    """How plausible a word is as a marking: decodes to a value, a code or a part number."""
    if len(text) < 2:
        return 0.0
    best = decode(text, None, "resistor" if chip and text.isdigit() else None).best
    if best is None:
        return 0.0
    if best.kind == "part-number" and best.source == "printed text":
        return 0.25 if len(text) >= 4 else 0.0
    return best.score


def find_bodies(dark: np.ndarray, min_side: float = 12.0) -> list[Body]:
    """Dark body regions (holes filled) that may carry a marking."""
    from .parts import _oriented
    labels, _ = ndimage.label(ndimage.binary_fill_holes(ndimage.binary_closing(dark, iterations=2)))
    bodies = []
    for i, sl in enumerate(ndimage.find_objects(labels), 1):
        if sl is None:
            continue
        region = labels[sl] == i
        if region.sum() < min_side * min_side * 1.5:
            continue
        # a body is mostly dark itself; a via or solder ring is a dark outline filled with bright metal
        if dark[sl][region].mean() < 0.55:
            continue
        # leads in shade make an IC look wider: open them away when most of the body survives
        r = max(2, int(0.07 * math.sqrt(region.sum())))
        y, x = np.ogrid[-r:r + 1, -r:r + 1]
        opened = ndimage.binary_opening(region, structure=x * x + y * y <= r * r)
        if opened.sum() > 0.6 * region.sum():
            region = opened
        ys, xs = np.nonzero(region)
        if len(xs) < 10:
            continue
        cx, cy, angle, length, width = _oriented(ys + sl[0].start, xs + sl[1].start)
        if width < min_side or region.sum() / max(length * width, 1) < 0.45:
            continue
        bodies.append(Body(len(bodies), cx, cy, angle, length, width))
    return bodies


def find_words(grey: np.ndarray, dark: np.ndarray, char: float | None = None) -> list[Body]:
    """Lines of text found by their strokes: bright, character-sized marks on dark plastic, linked to their
    neighbours of similar height. Each becomes a pseudo-body oriented along its line (either reading direction)."""
    from .parts import _oriented
    size = int(char or max(grey.shape) / 60) | 1          # about one character height in a close-up
    # inside dark bodies (their text shows as holes in the dark mask; solder and pads stay outside)
    roi = ndimage.binary_fill_holes(ndimage.binary_closing(dark, iterations=2))
    roi = ndimage.binary_erosion(roi, iterations=1)
    smooth = ndimage.gaussian_filter(grey, 1.0)
    tophat = smooth - ndimage.maximum_filter(ndimage.minimum_filter(smooth, size=size), size=size)
    values = tophat[roi]
    if values.size < 100:
        return []
    strokes = roi & (tophat > max(18.0, float(np.percentile(values, 99)) * 0.4))
    labels, n = ndimage.label(strokes, structure=np.ones((3, 3)))
    chars = []
    for i, sl in enumerate(ndimage.find_objects(labels), 1):
        if sl is None:
            continue
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        big, small = max(h, w), min(h, w)
        if not (0.3 * size <= big <= 1.6 * size) or small < 0.08 * size:
            continue
        area = int((labels[sl] == i).sum())
        if area < 0.08 * h * w:
            continue
        chars.append(((sl[0].start + sl[0].stop) / 2, (sl[1].start + sl[1].stop) / 2, big, sl))
    if len(chars) < 2:
        return []
    # link characters: close (1.25 x their size) and of similar size
    parent = list(range(len(chars)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    from scipy.spatial import cKDTree
    tree = cKDTree([(c[1], c[0]) for c in chars])
    for a, b in tree.query_pairs(1.3 * size):
        ca, cb = chars[a], chars[b]
        if max(ca[2], cb[2]) > 1.45 * min(ca[2], cb[2]):
            continue
        if math.hypot(ca[0] - cb[0], ca[1] - cb[1]) <= 1.25 * max(ca[2], cb[2]):
            parent[find(a)] = find(b)
    groups: dict[int, list] = {}
    for k in range(len(chars)):
        groups.setdefault(find(k), []).append(chars[k])
    words = []
    for members in groups.values():
        if len(members) < 2:
            continue
        mask = np.zeros(grey.shape, bool)
        for _, _, _, sl in members:
            mask[sl] = True
        ys, xs = np.nonzero(mask)
        cx, cy, angle, length, width = _oriented(ys, xs)
        height = float(np.median([m[2] for m in members]))
        if length < 1.2 * height:
            continue
        words.append(Body(0, cx, cy, angle, length + 1.2 * height, max(width, height) + 1.0 * height,
                          word=True, chars=len(members)))
    return words


def make_crop(grey: np.ndarray, body: Body, rotation: int) -> Crop | None:
    half = int(math.hypot(body.length, body.width) / 2) + 4
    x0, y0 = int(round(body.cx)) - half, int(round(body.cy)) - half
    pad = ((max(0, -y0), max(0, y0 + 2 * half - grey.shape[0])), (max(0, -x0), max(0, x0 + 2 * half - grey.shape[1])))
    window = np.pad(grey, pad, mode="edge")[y0 + pad[0][0]:y0 + pad[0][0] + 2 * half,
                                            x0 + pad[1][0]:x0 + pad[1][0] + 2 * half]
    angle = math.degrees(body.angle) + rotation
    turned = np.asarray(Image.fromarray(window).rotate(angle, resample=Image.BICUBIC), dtype=np.float32)
    long_frac = 0.68 if body.chip and not body.word else 0.92      # chip parts: not the metal end caps
    length, width = body.length * long_frac, body.width * 0.92
    w, h = (length, width) if rotation % 180 == 0 else (width, length)
    c = turned.shape[0] / 2
    bx, by = int(c - w / 2), int(c - h / 2)
    crop = turned[by:int(c + h / 2), bx:int(c + w / 2)]
    if crop.size == 0 or min(crop.shape) < 4:
        return None
    shrink = min(1.0, 120.0 / max(1, min(crop.shape)))     # ~120 px across is enough, and keeps filters fast
    if shrink < 1.0:
        crop = np.asarray(Image.fromarray(crop).resize((max(4, int(crop.shape[1] * shrink)),
                                                        max(4, int(crop.shape[0] * shrink))), Image.BILINEAR),
                          dtype=np.float32)
    short = min(crop.shape)
    smooth = ndimage.gaussian_filter(crop, max(0.5, short / 90))
    size = max(5, int(short / 3)) | 1
    background = ndimage.maximum_filter(ndimage.minimum_filter(smooth, size=size), size=size)
    text = smooth - background
    lo, hi = np.percentile(text, [55, 99.6])
    if hi - lo < 6:                                          # no bright strokes worth reading
        return None
    text = np.clip((text - lo) / max(hi - lo, 1e-3), 0, 1)
    if not 0.004 < (text > 0.5).mean() < 0.35:
        return None
    target = 110.0 if body.chip or body.word or min(body.length, body.width) < 80 else 300.0
    scale = float(np.clip(target / short, 0.4, 6.0))
    image = Image.fromarray((255 - text * 255).astype(np.uint8))
    image = image.resize((max(8, int(image.width * scale)), max(8, int(image.height * scale))), Image.LANCZOS)
    framed = Image.new("L", (image.width + 40, image.height + 40), 255)
    framed.paste(image, (20, 20))
    grey_img = np.asarray(framed, dtype=np.float32)
    binary = np.where(grey_img < glyphs._otsu(grey_img), 0, 255).astype(np.uint8)
    return Crop(body, rotation, grey_img, binary, scale * shrink, (bx, by), (x0, y0), angle, half)


def _tesseract(images: list[np.ndarray], psm: str, tessdata: str | None) -> dict[int, list]:
    """Words per image: (text, conf, centre x, centre y)."""
    if not images:
        return {}
    with tempfile.TemporaryDirectory(prefix="markings") as tmp:
        paths = []
        for k, image in enumerate(images):
            path = Path(tmp) / f"{k:05d}.png"
            Image.fromarray(image.astype(np.uint8)).save(path)
            paths.append(path)
        listing = Path(tmp) / "list.txt"
        listing.write_text("\n".join(str(p) for p in paths) + "\n")
        cmd = ["tesseract", str(listing), "-"]
        if tessdata:
            cmd += ["--tessdata-dir", tessdata]
        cmd += ["--psm", psm, "-c", f"tessedit_char_whitelist={WHITELIST}", "tsv"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=1200).stdout
    pages: dict[int, list] = {}
    for line in out.splitlines()[1:]:
        c = line.split("\t")
        if len(c) < 12 or c[0] != "5" or not c[11].strip():
            continue
        left, top, width, height = (float(v) for v in c[6:10])
        pages.setdefault(int(c[1]) - 1, []).append((c[11].strip(), float(c[10]) / 100, left + width / 2,
                                                    top + height / 2))
    return pages


def tessdata_dir() -> str | None:
    """A better tesseract model when one is installed ($EMBEDDYN_TESSDATA, e.g. tessdata_best)."""
    import os
    path = os.environ.get("EMBEDDYN_TESSDATA")
    return path if path and (Path(path) / "eng.traineddata").is_file() else None


class MarkingReader:
    """The markings in one photo (a straightened board side or a close-up)."""

    def __init__(self, rgb: np.ndarray, dark: np.ndarray, source: str = "", min_side: float = 12.0,
                 words: bool = True, char: float | None = None):
        self.source = source
        self.grey = np.asarray(Image.fromarray(rgb).convert("L"), dtype=np.float32)
        self.bodies = find_bodies(dark, min_side)
        if words:
            self.bodies += find_words(self.grey, dark, char)
        for k, body in enumerate(self.bodies):
            body.id = k
        self.crops: dict[int, list[Crop]] = {}
        for body in self.bodies:
            rotations = (0, 180) if body.word or body.chip or body.length > 1.45 * body.width else (0, 90, 180, 270)
            made = [make_crop(self.grey, body, r) for r in rotations]
            self.crops[body.id] = [c for c in made if c is not None]
        self.tess: dict[tuple[int, int], list[Word]] = {}

    def read(self, use_tesseract: bool = True) -> "MarkingReader":
        if use_tesseract and shutil.which("tesseract"):
            jobs = [(crop, variant) for crops in self.crops.values() for crop in crops for variant in (0, 1)]
            by_psm: dict[str, list[int]] = {"7": [], "6": []}
            for k, (crop, _) in enumerate(jobs):
                one_line = crop.body.chip or min(crop.body.length, crop.body.width) < 60
                by_psm["7" if one_line else "6"].append(k)
            for psm, ks in by_psm.items():
                results = _tesseract([jobs[k][0].grey if jobs[k][1] == 0 else jobs[k][0].binary for k in ks], psm,
                                     tessdata_dir())
                for n, k in enumerate(ks):
                    crop = jobs[k][0]
                    words = []
                    for text, conf, u, v in results.get(n, []):
                        if conf < 0.2:
                            continue
                        x, y = crop.back(u, v)
                        words.append(Word(text, conf, x, y, crop.up, crop.body.id,
                                          word_score(text, crop.body.chip), "tesseract"))
                    key = (crop.body.id, crop.rotation)
                    # of the two image variants, keep the one whose words decode better
                    old = self.tess.get(key, [])
                    if sum(w.conf * (0.15 + w.score) for w in words) > sum(w.conf * (0.15 + w.score) for w in old):
                        self.tess[key] = words
        self.reread()
        return self

    def _glyph_words(self, crop: Crop) -> list[Word]:
        charset = glyphs.VALUE_CHARS if crop.body.chip else None
        words = []
        for line in glyphs.read_gray(crop.grey, charset):
            text = "".join(g.char for g in line)
            if len(text) < 2:
                continue
            u = (min(g.x0 for g in line) + max(g.x1 for g in line)) / 2
            v = (min(g.y0 for g in line) + max(g.y1 for g in line)) / 2
            x, y = crop.back(u, v)
            conf = float(np.mean([g.score for g in line]))
            score = word_score(text, crop.body.chip)
            if crop.body.chip and len(text) == 4:
                # solder or the body's dark end at a code's side reads as one more character: try without it
                for trimmed in (text[1:], text[:-1]):
                    trimmed_score = word_score(trimmed, True) - 0.1
                    if trimmed_score > score:
                        text, score = trimmed, trimmed_score
            words.append(Word(text, conf, x, y, crop.up, crop.body.id, score, "glyphs"))
        return words

    def reread(self) -> "MarkingReader":
        """Choose every body's reading again (after new characters were learned): no new OCR."""
        for body in self.bodies:
            best: tuple[float, list[Word]] = (0.0, [])
            for crop in self.crops.get(body.id, []):
                for words in (self.tess.get((body.id, crop.rotation), []), self._glyph_words(crop)):
                    total = sum(w.conf * (0.15 + w.score) * min(len(w.text), 8) for w in words)
                    if total > best[0]:
                        best = (total, words)
            body.words = [w for w in best[1]
                          if (w.score >= 0.6 and w.conf >= 0.4) or (w.score >= 0.4 and w.conf >= 0.5)
                          or (w.conf >= 0.8 and len(w.text) >= 3)]
        return self

    def body_at(self, x: float, y: float) -> Body | None:
        """The body under a point, or the nearest within its own size."""
        best = None
        for body in self.bodies:
            d = math.hypot(body.cx - x, body.cy - y)
            if d < 0.6 * body.length and (best is None or d < best[0]):
                best = (d, body)
        return best[1] if best else None

    def learn(self, body: Body, text: str) -> tuple[int, tuple[float, float] | None]:
        """Learn the characters of ``text`` from ``body`` (the orientation that fits it best); returns how many
        characters were learned and the marking's up direction in the photo."""
        best: tuple[float, list, Crop | None] = (0.0, [], None)
        for crop in self.crops.get(body.id, []):
            score, samples = glyphs.fit(crop.grey, text)
            if score > best[0]:
                best = (score, samples, crop)
        score, samples, crop = best
        if crop is None or score < 0.2 or not samples:
            return 0, None
        glyphs.remember(samples)
        body.taught = text
        return len(samples), crop.up

    @property
    def marked(self) -> list[Body]:
        return [b for b in self.bodies if b.words]


def read_markings(rgb: np.ndarray, dark: np.ndarray, min_side: float = 12.0) -> list[Body]:
    """Every dark body's marking in a photo."""
    return MarkingReader(rgb, dark, min_side=min_side).read().marked
