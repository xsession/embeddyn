"""Read a schematic from pixels (PNG, JPEG, a scanned PDF page) into the same strokes, shapes, dots and text as a
vector PDF, so the same analysis applies.

1. The image is made black and white (Otsu threshold on grey; dark ink on light paper, inverted if needed).
2. Text: ``tesseract`` (TSV output, sparse text mode) on the page and on the page turned 90 degrees for vertical
   text. Ink blobs lying inside a word's box are the word's glyphs and are erased.
3. Horizontal and vertical strokes: morphological openings with long thin lines keep only straight runs; each run
   becomes a segment along its centre line.
4. Rectangles made of four such strokes are symbol bodies (closed shapes); round blobs a few stroke widths wide
   where lines meet are junction dots; whatever ink is left (curves, diagonals, arrows) is symbol ink.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from scipy import ndimage

from .primitives import Dot, Page, Segment, Text

TESSERACT = "tesseract"


class OcrMissing(RuntimeError):
    pass


def _otsu(gray: np.ndarray) -> int:
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = gray.size
    cumulative = np.cumsum(hist)
    mean = np.cumsum(hist * np.arange(256))
    best, threshold = -1.0, 128
    for t in range(1, 255):
        w0, w1 = cumulative[t], total - cumulative[t]
        if w0 == 0 or w1 == 0:
            continue
        m0 = mean[t] / w0
        m1 = (mean[-1] - mean[t]) / w1
        between = w0 * w1 * (m0 - m1) ** 2
        if between > best:
            best, threshold = between, t
    return threshold


def _stroke_width(ink: np.ndarray) -> float:
    """The commonest short run length across strokes: the line width in pixels."""
    runs = []
    for axis_ink in (ink, ink.T):
        sample = axis_ink[:: max(1, axis_ink.shape[0] // 400)]
        for row in sample:
            edges = np.flatnonzero(np.diff(np.concatenate(([0], row.astype(np.int8), [0]))))
            lengths = edges[1::2] - edges[0::2]
            runs.extend(lengths[lengths <= 12].tolist())
    if not runs:
        return 1.0
    counts = np.bincount(runs)
    return float(max(1, int(np.argmax(counts[1:]) + 1)))


def ocr_words(image_path: Path, min_confidence: float = 55.0) -> list[tuple[str, int, int, int, int, float]]:
    """(text, left, top, width, height, confidence) of the words tesseract finds."""
    tool = shutil.which(TESSERACT)
    if not tool:
        raise OcrMissing("tesseract is needed to read text from images: install tesseract-ocr")
    r = subprocess.run([tool, str(image_path), "stdout", "--psm", "11", "tsv"], capture_output=True, text=True)
    words = []
    for line in r.stdout.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 12 or not cols[11].strip():
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        text = cols[11].strip()
        if conf < min_confidence or not any(ch.isalnum() for ch in text):
            continue
        words.append((text, int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9]), conf / 100.0))
    return words


def _runs(mask: np.ndarray, horizontal: bool, width: float, min_length: int) -> list[tuple[float, float, float, float]]:
    """Straight strokes of the mask as (x0, y0, x1, y1) centre lines in pixels: the runs of each row (column) that
    survive an opening with a line ``min_length`` long, joined with the runs of the next rows when they cover the
    same span (one stroke a few pixels thick). Strokes that touch (a wire ending on a plate) stay separate."""
    structure = np.ones((1, min_length), bool) if horizontal else np.ones((min_length, 1), bool)
    opened = ndimage.binary_opening(mask, structure=structure)
    if not horizontal:
        opened = opened.T
    slack = max(2.0, width)
    open_strokes: list[list[float]] = []     # [start, end, first_row, last_row]
    done: list[list[float]] = []
    for row in range(opened.shape[0]):
        line = opened[row]
        if not line.any():
            done.extend(open_strokes)
            open_strokes = []
            continue
        edges = np.flatnonzero(np.diff(np.concatenate(([0], line.astype(np.int8), [0]))))
        runs = list(zip(edges[0::2], edges[1::2] - 1))
        still = []
        for a, b in runs:
            match = None
            for stroke in open_strokes:
                if stroke[3] == row - 1 and abs(stroke[0] - a) <= slack and abs(stroke[1] - b) <= slack:
                    match = stroke
                    break
            if match is not None and row - match[2] < 3 * width + 2:
                match[0], match[1] = min(match[0], a), max(match[1], b)
                match[3] = row
                open_strokes.remove(match)
                still.append(match)
            else:
                still.append([a, b, row, row])
        done.extend(open_strokes)
        open_strokes = still
    done.extend(open_strokes)
    result = []
    for a, b, r0, r1 in done:
        centre = (r0 + r1) / 2
        result.append((a, centre, b, centre) if horizontal else (centre, a, centre, b))
    return result


def _rectangles(hs, vs, tol: float, dots=()):
    """Indices (top, bottom, left, right) of horizontal / vertical strokes that close a rectangle: the four meet
    at the corners and none runs on past them (wires that happen to enclose an area do), and no junction dot sits
    on a side."""
    found = []
    by_y = sorted(range(len(hs)), key=lambda i: hs[i][1])
    for a_pos, a in enumerate(by_y):
        ax0, ay, ax1, _ = hs[a]
        for b in by_y[a_pos + 1:]:
            bx0, by, bx1, _ = hs[b]
            if by - ay < 2 * tol:
                continue
            if abs(ax0 - bx0) > tol or abs(ax1 - bx1) > tol:
                continue
            left = right = None
            for v, (vx, vy0, _, vy1) in enumerate(vs):
                if abs(vy0 - ay) <= tol and abs(vy1 - by) <= tol:
                    if abs(vx - ax0) <= tol:
                        left = v
                    elif abs(vx - ax1) <= tol:
                        right = v
            if left is None or right is None:
                continue
            on_side = any((abs(dy - ay) <= tol or abs(dy - by) <= tol) and ax0 - tol <= dx <= ax1 + tol or
                          (abs(dx - ax0) <= tol or abs(dx - ax1) <= tol) and ay - tol <= dy <= by + tol
                          for dx, dy in dots)
            dot_inside = any(ax0 + tol < dx < ax1 - tol and ay + tol < dy < by - tol for dx, dy in dots)

            def inside(x0, y0, x1, y1):
                return ax0 + tol < min(x0, x1) and max(x0, x1) < ax1 - tol and ay + tol < min(y0, y1) and \
                    max(y0, y1) < by - tol
            # a body holds text and small marks; a wire loop holds whole wires and symbols
            limit = 0.4 * min(ax1 - ax0, by - ay)
            long_inside = any(inside(*r) and max(abs(r[2] - r[0]), abs(r[3] - r[1])) > max(limit, 6 * tol)
                              for r in hs + vs)
            if not on_side and not dot_inside and not long_inside:
                found.append((a, b, left, right))
                break
    return found


def _wire_colour_mask(rgb: np.ndarray, ink: np.ndarray, dots: list, min_run: int) -> np.ndarray | None:
    """For a coloured drawing (KiCad / Eagle exports draw wires, symbols and text in their own colours): the ink of
    the wire colour, or None when the ink is not coloured. The wire colour is the colour of the junction dots, or
    else the colour with the most long straight runs."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    mx, mn = np.maximum(np.maximum(r, g), b), np.minimum(np.minimum(r, g), b)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0)
    coloured = ink & (sat > 0.3) & (mx > 0.15)
    if coloured.sum() < 0.3 * ink.sum():
        return None
    d = np.maximum(mx - mn, 1e-6)
    hue = np.where(mx == r, ((g - b) / d) % 6, np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4)) * 60.0
    bins = (hue // 15).astype(int) % 24
    counts = np.bincount(bins[coloured], minlength=24)
    total = counts.sum()
    # colour classes: runs of neighbouring hue bins holding 5 % of the coloured ink or more
    classes = []
    for i in np.argsort(-counts):
        if counts[i] < 0.05 * total:
            break
        if any(min(abs(i - c), 24 - abs(i - c)) <= 1 for cls in classes for c in cls):
            for cls in classes:
                if any(min(abs(i - c), 24 - abs(i - c)) <= 1 for c in cls):
                    cls.add(i)
            continue
        classes.append({i})
    if len(classes) < 2:
        return None
    masks = [coloured & np.isin(bins, list(cls)) for cls in classes]
    votes = [0] * len(masks)
    for x, y in dots:
        yi, xi = int(round(y)), int(round(x))
        for n, m in enumerate(masks):
            if m[max(0, yi - 1):yi + 2, max(0, xi - 1):xi + 2].any():
                votes[n] += 1
    if sum(votes) >= 3:
        best = int(np.argmax(votes))
    else:
        scores = []
        for m in masks:
            hl = ndimage.binary_opening(m, structure=np.ones((1, min_run), bool)).sum()
            vl = ndimage.binary_opening(m, structure=np.ones((min_run, 1), bool)).sum()
            scores.append(hl + vl)
        best = int(np.argmax(scores))
    # the wire mask, a pixel wider so anti-aliased edges belong to it
    return ndimage.binary_dilation(masks[best], iterations=1) & ink


def read_image(path: Path, dpi: float | None = None) -> Page:
    from PIL import Image
    image = Image.open(path)
    if dpi is None:
        info = image.info.get("dpi")
        dpi = float(info[0]) if info and info[0] and info[0] > 30 else 200.0
    colour = np.asarray(image.convert("RGB"), dtype=np.int16)
    image = image.convert("L")
    gray = np.asarray(image, dtype=np.uint8)
    threshold = _otsu(gray)
    ink = gray < threshold
    if ink.mean() > 0.5:
        ink = ~ink              # light lines on a dark background
    else:
        # coloured ink lighter than the threshold (teal text, light green wires) is ink too
        spread = colour.max(axis=2) - colour.min(axis=2)
        ink |= (spread > 60) & (gray < 235)
    k = 72.0 / dpi              # points per pixel
    page = Page(index=1, width=gray.shape[1] * k, height=gray.shape[0] * k, raster=True,
                image=Image.open(path), scale=1 / k, source=path, source_page=1)
    width = _stroke_width(ink)
    page.line_width = width * k

    # --- text ---
    texts: list[Text] = []
    # OCR a text-only copy: long lines (wires, symbol outlines) removed, enlarged to ~600 dpi, and thinned when the
    # strokes are heavy (tesseract misreads KiCad's bold stroke font and text inside boxes)
    long_run = max(int(10 * width), int(14 / k))      # longer than a letter's stem: 14 pt
    lines = ndimage.binary_opening(ink, structure=np.ones((1, long_run), bool)) | \
        ndimage.binary_opening(ink, structure=np.ones((long_run, 1), bool))
    text_ink = ink & ~ndimage.binary_dilation(lines, iterations=1)
    up = min(3.0, max(1.0, 600.0 / dpi))
    plain = Image.fromarray(np.where(text_ink, 0, 255).astype(np.uint8))
    if up > 1.0:
        plain = plain.resize((int(plain.width * up), int(plain.height * up)), Image.LANCZOS)
    # the whole page in grey too: some text reads best with its surroundings (thin fonts, text on lines)
    whole = Image.fromarray(np.where(ink, np.minimum(gray, 128), 255).astype(np.uint8))
    if up > 1.0:
        whole = whole.resize(plain.size, Image.LANCZOS)
    variants = [whole, plain]
    if width * up >= 4:
        # also a thinned copy: bold stroke fonts (KiCad's) read better thinner, thin fonts read better as they are
        from PIL import ImageFilter
        variants.append(plain.filter(ImageFilter.MaxFilter(3)))
    f = k / up
    H = plain.height
    found: list[Text] = []
    with tempfile.TemporaryDirectory(prefix="schscan") as work:
        for n, variant in enumerate(variants):
            upright = Path(work) / f"page{n}.png"
            variant.save(upright)
            for text, left, top, w, h, conf in ocr_words(upright):
                found.append(Text(_fix_ocr(text), left * f, top * f, (left + w) * f, (top + h) * f,
                                  confidence=conf))
            rotated = Path(work) / f"rotated{n}.png"
            variant.transpose(Image.ROTATE_270).save(rotated)
            for text, left, top, w, h, conf in ocr_words(rotated):
                # ROTATE_270 (clockwise): rotated (x, y) is original (y, H - 1 - x)
                x0, x1 = top, top + h
                y0, y1 = H - (left + w), H - left
                found.append(Text(_fix_ocr(text), x0 * f, y0 * f, x1 * f, y1 * f, vertical=True, confidence=conf))
    # where readings overlap keep the most confident (upright before turned on a tie)
    for t in sorted(found, key=lambda t: (-t.confidence, t.vertical)):
        if not any(_overlap(t, u) > 0.3 or _overlap(u, t) > 0.3 for u in texts):
            texts.append(t)
    page.texts = texts
    page.text_image = Image.fromarray(np.where(text_ink, 0, 255).astype(np.uint8))

    # erase glyphs: ink blobs inside a word's box
    labels, count = ndimage.label(ink, structure=np.ones((3, 3), bool))
    objects = ndimage.find_objects(labels)
    erase = np.zeros(count + 1, bool)
    boxes = [(int(t.x0 / k) - 1, int(t.y0 / k) - 1, int(t.x1 / k) + 1, int(t.y1 / k) + 1) for t in texts]
    for n, sl in enumerate(objects, 1):
        if sl is None:
            continue
        ys, xs = sl
        for x0, y0, x1, y1 in boxes:
            if xs.start >= x0 and xs.stop - 1 <= x1 and ys.start >= y0 and ys.stop - 1 <= y1:
                erase[n] = True
                break
    ink = ink & ~erase[labels]

    # --- dots: thick round blobs ---
    radius = max(1, int(round(width * 0.9)))
    yy, xx = np.mgrid[-radius:radius + 1, -radius:radius + 1]
    disk = xx * xx + yy * yy <= radius * radius
    thick = ndimage.binary_opening(ink, structure=disk)
    tl, tn = ndimage.label(thick)
    filled = np.zeros_like(ink)
    for n, sl in enumerate(ndimage.find_objects(tl), 1):
        if sl is None:
            continue
        ys, xs = sl
        h, w = ys.stop - ys.start, xs.stop - xs.start
        # a junction dot: round, solid and clearly thicker than a line (the bowl of a glyph is not)
        solid = ink[sl].mean() if h and w else 0.0
        if 3.6 * width <= max(h, w) <= 7 * width + 3 and 0.7 < (w + 1) / (h + 1) < 1.4 and solid >= 0.7:
            cy, cx = (ys.start + ys.stop - 1) / 2, (xs.start + xs.stop - 1) / 2
            page.dots.append(Dot(cx * k, cy * k, max(h, w) / 2 * k))
        else:
            filled[sl] |= tl[sl] == n

    # --- straight strokes ---
    min_length = max(5, int(round(4 * width)))
    rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32) / 255.0
    wire_ink = _wire_colour_mask(rgb, ink, [(d.x / k, d.y / k) for d in page.dots], max(20, int(12 * width)))
    if wire_ink is not None:
        # coloured drawing: wires are the wire colour, everything else is symbol ink
        page.dots = [d for d in page.dots if wire_ink[min(wire_ink.shape[0] - 1, int(d.y / k)),
                                                      min(wire_ink.shape[1] - 1, int(d.x / k))]]
        symbol_ink = ink & ~wire_ink
        hs_wire = _runs(wire_ink, True, width, min_length)
        vs_wire = _runs(wire_ink, False, width, min_length)
        hs = _runs(symbol_ink, True, width, min_length)
        vs = _runs(symbol_ink, False, width, min_length)
    else:
        symbol_ink = ink
        hs_wire = vs_wire = []
        hs = _runs(ink, True, width, min_length)
        vs = _runs(ink, False, width, min_length)
    tol_px = max(2.0, 1.5 * width)
    rects = _rectangles(hs, vs, tol_px, [(d.x / k, d.y / k) for d in page.dots])
    in_rect = set()
    path = 0
    for top, bottom, left, right in rects:
        path += 1
        ax0, ay, ax1, _ = hs[top]
        _, by, _, _ = hs[bottom]
        page.polygons.append([(ax0 * k, ay * k), (ax1 * k, ay * k), (ax1 * k, by * k), (ax0 * k, by * k)])
        for (x0, y0, x1, y1) in (hs[top], hs[bottom], vs[left], vs[right]):
            page.segments.append(Segment(x0 * k, y0 * k, x1 * k, y1 * k, width * k, "body", path))
        in_rect.update({("h", top), ("h", bottom), ("v", left), ("v", right)})
    other = "body" if wire_ink is not None else "wire"
    for n, (x0, y0, x1, y1) in enumerate(hs):
        if ("h", n) not in in_rect:
            path += 1
            page.segments.append(Segment(x0 * k, y0 * k, x1 * k, y1 * k, width * k, other, path))
    for n, (x0, y0, x1, y1) in enumerate(vs):
        if ("v", n) not in in_rect:
            path += 1
            page.segments.append(Segment(x0 * k, y0 * k, x1 * k, y1 * k, width * k, other, path))
    for x0, y0, x1, y1 in hs_wire + vs_wire:
        path += 1
        page.segments.append(Segment(x0 * k, y0 * k, x1 * k, y1 * k, width * k, "wire", path))

    # --- what is left: curves, diagonals, arrows, filled shapes -> symbol ink ---
    straight = np.zeros_like(ink) if wire_ink is None else wire_ink.copy()
    for x0, y0, x1, y1 in hs + vs:
        r = int(width / 2) + 1
        straight[max(0, int(min(y0, y1)) - r):int(max(y0, y1)) + r + 1,
                 max(0, int(min(x0, x1)) - r):int(max(x0, x1)) + r + 1] = True
    rest = ink & ~straight & ~thick | filled
    rl, rn = ndimage.label(rest, structure=np.ones((3, 3), bool))
    step = max(1, int(width))
    for n, sl in enumerate(ndimage.find_objects(rl), 1):
        if sl is None:
            continue
        ys, xs = sl
        if (ys.stop - ys.start) * (xs.stop - xs.start) < 4 * width * width:
            continue        # specks
        path += 1
        piece = rl[sl] == n
        for r in range(0, piece.shape[0], step):
            cols = np.flatnonzero(piece[r])
            if cols.size == 0:
                continue
            breaks = np.flatnonzero(np.diff(cols) > 1)
            starts = np.concatenate(([cols[0]], cols[breaks + 1]))
            ends = np.concatenate((cols[breaks], [cols[-1]]))
            y = (ys.start + r) * k
            for a, b in zip(starts, ends):
                page.segments.append(Segment((xs.start + a) * k, y, (xs.start + b) * k + 0.01, y + 0.01,
                                             width * k, "body", path))
    return page


def _fix_ocr(text: str) -> str:
    """Common OCR slips in reference designators: lower-case prefixes (c100) and O / I / l among the digits."""
    import re
    if text[:1] in "€(£" and len(text) > 1 and text[1:2].isdigit():
        text = "C" + text[1:]
    m = re.fullmatch(r"([A-Za-z]{1,3})([0-9OoIil|]{1,4})([A-Za-z]?)", text)
    if m and (any(ch.isdigit() for ch in m.group(2)) or
              (m.group(1).upper() in {"R", "C", "L", "D", "U", "Q", "J", "P", "F", "X", "Y", "K"} and
               m.group(1).isupper())):
        digits = m.group(2).translate(str.maketrans("OoIil|", "001111"))
        return m.group(1).upper() + digits + m.group(3).upper()
    return text


def _overlap(a: Text, b: Text) -> float:
    ix = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    iy = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
    area = (a.x1 - a.x0) * (a.y1 - a.y0) or 1.0
    return ix * iy / area


def reread_near(page: Page, boxes: list[tuple[float, float, float, float]], margin_pt: float = 22.0) -> int:
    """OCR again, one small crop at a time, around symbols that came out without a reference designator: sparse
    page OCR skips short words ("C1", "1k") that a block read of a crop finds. Adds the new words to the page and
    returns how many."""
    image = getattr(page, "text_image", None)
    if image is None or not boxes:
        return 0
    from PIL import Image
    k = 1.0 / page.scale
    added = 0
    new: list[Text] = []
    with tempfile.TemporaryDirectory(prefix="schscan") as work:
        crop_path = Path(work) / "crop.png"
        for x0, y0, x1, y1 in boxes:
            left = max(0, int((x0 - margin_pt) / k))
            top = max(0, int((y0 - margin_pt) / k))
            right = min(image.width, int((x1 + margin_pt) / k))
            bottom = min(image.height, int((y1 + margin_pt) / k))
            if right - left < 4 or bottom - top < 4:
                continue
            up = 3
            crop = image.crop((left, top, right, bottom))
            crop = crop.resize((crop.width * up, crop.height * up), Image.LANCZOS)
            framed = Image.new("L", (crop.width + 40, crop.height + 40), 255)
            framed.paste(crop, (20, 20))
            framed.save(crop_path)
            tool = shutil.which(TESSERACT)
            r = subprocess.run([tool, str(crop_path), "stdout", "--psm", "6", "tsv"], capture_output=True, text=True)
            for line in r.stdout.splitlines()[1:]:
                cols = line.split("\t")
                if len(cols) < 12 or not cols[11].strip():
                    continue
                try:
                    conf = float(cols[10]) / 100.0
                except ValueError:
                    continue
                text = _fix_ocr(cols[11].strip())
                if conf < 0.6 or not any(ch.isalnum() for ch in text):
                    continue
                wx = (int(cols[6]) - 20) / up + left
                wy = (int(cols[7]) - 20) / up + top
                t = Text(text, wx * k, wy * k, (wx + int(cols[8]) / up) * k, (wy + int(cols[9]) / up) * k,
                         confidence=conf)
                if not any(_overlap(t, u) > 0.2 or _overlap(u, t) > 0.2 for u in page.texts):
                    page.texts.append(t)
                    new.append(t)
                    added += 1
    if new:
        # the strokes of the words now read are glyphs, not drawing
        pad = 1.5 * k

        def in_word(x, y):
            return any(t.x0 - pad <= x <= t.x1 + pad and t.y0 - pad <= y <= t.y1 + pad for t in new)
        page.segments = [g for g in page.segments if not (in_word(g.x1, g.y1) and in_word(g.x2, g.y2))]
    return added
