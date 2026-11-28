"""Read the pages of a PDF schematic as drawn: strokes, closed shapes, junction dots and positioned text.

MuPDF's ``mutool`` does the PDF work (no Python PDF package needed): ``mutool draw -F trace`` lists every path
with its transform, ``mutool draw -F stext`` every character with its quad. Text drawn as strokes (KiCad's stroke
font) comes with an invisible copy of the same text in KiCad PDFs; the strokes under any text box are dropped.
A page that is a scanned picture (an image and almost no paths) is rendered and read as an image instead.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .primitives import Dot, Page, Segment, Text, polygon_area

MUTOOL = "mutool"


class PdfToolMissing(RuntimeError):
    pass


def _mutool() -> str:
    tool = shutil.which(MUTOOL)
    if not tool:
        raise PdfToolMissing("mutool (MuPDF tools) is needed to read PDF schematics: install mupdf-tools")
    return tool


def _run(args: list[str]) -> None:
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])} ...: {r.stderr.strip()[:400]}")


def page_count(pdf: Path) -> int:
    r = subprocess.run([_mutool(), "info", str(pdf)], capture_output=True, text=True)
    m = re.search(r"^Pages:\s*(\d+)", r.stdout, re.M)
    if not m:
        raise RuntimeError(f"{pdf}: not a readable PDF ({r.stderr.strip()[:200]})")
    return int(m.group(1))


def _transform(attrib: dict) -> tuple[float, ...]:
    values = [float(v) for v in attrib.get("transform", "1 0 0 1 0 0").split()]
    return tuple(values) if len(values) == 6 else (1, 0, 0, 1, 0, 0)


def _apply(m: tuple[float, ...], x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def _cubic(p0, p1, p2, p3, steps: int = 6):
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        yield (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
               u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])


def _subpaths(element, m):
    """(points, closed, curved) for each subpath of a trace path element, in page coordinates."""
    points: list[tuple[float, float]] = []
    closed = curved = False
    for op in element:
        tag = op.tag
        if tag == "moveto":
            if len(points) > 1:
                yield points, closed, curved
            points, closed, curved = [_apply(m, float(op.get("x")), float(op.get("y")))], False, False
        elif tag == "lineto":
            points.append(_apply(m, float(op.get("x")), float(op.get("y"))))
        elif tag == "curveto":
            if not points:
                continue
            c1 = _apply(m, float(op.get("x1")), float(op.get("y1")))
            c2 = _apply(m, float(op.get("x2")), float(op.get("y2")))
            end = _apply(m, float(op.get("x3")), float(op.get("y3")))
            points.extend(_cubic(points[-1], c1, c2, end))
            curved = True
        elif tag == "closepath":
            closed = True
    if len(points) > 1:
        yield points, closed, curved


def _texts(stext_page) -> list[Text]:
    """Words of the page: the characters of each stext line, split where the gap is wider than a space."""
    texts: list[Text] = []
    for line in stext_page.iter("line"):
        direction = [float(v) for v in line.get("dir", "1 0").split()]
        vertical = abs(direction[1]) > abs(direction[0])
        chars = []
        for char in line.iter("char"):
            q = [float(v) for v in char.get("quad", "").split()]
            if len(q) != 8:
                continue
            xs, ys = q[0::2], q[1::2]
            chars.append((char.get("c", ""), min(xs), min(ys), max(xs), max(ys)))
        if not chars:
            continue
        height = max((c[3] - c[1]) if vertical else (c[4] - c[2]) for c in chars) or 1.0
        word: list = []

        def flush():
            text = "".join(c[0] for c in word).strip()
            if text:
                texts.append(Text(text, min(c[1] for c in word), min(c[2] for c in word),
                                  max(c[3] for c in word), max(c[4] for c in word), vertical=vertical))

        for char in chars:
            if not char[0].strip():
                # a space ends the word (MuPDF joins separate texts on one line with a space: "VDD 19")
                if word:
                    flush()
                word = []
                continue
            if word:
                previous = word[-1]
                gap = (min(abs(char[2] - previous[4]), abs(previous[2] - char[4])) if vertical
                       else min(abs(char[1] - previous[3]), abs(previous[1] - char[3])))
                if gap > 0.8 * height:
                    flush()
                    word = []
            word.append(char)
        flush()
    # "R1 10k" in one word: split when it starts with a reference designator
    result: list[Text] = []
    for t in texts:
        parts = t.text.split()
        if len(parts) > 1 and re.fullmatch(r"[A-Z]{1,4}\d{1,4}[A-Z]?", parts[0]):
            n = len(t.text)
            pos = 0
            for part in parts:
                start = t.text.index(part, pos)
                pos = start + len(part)
                if t.vertical:
                    span = t.y1 - t.y0
                    result.append(Text(part, t.x0, t.y0 + span * start / n, t.x1, t.y0 + span * pos / n, True))
                else:
                    span = t.x1 - t.x0
                    result.append(Text(part, t.x0 + span * start / n, t.y0, t.x0 + span * pos / n, t.y1))
        else:
            result.append(t)
    return result


def read_pdf(pdf: Path, pages: list[int] | None = None, dpi: int = 300) -> list[Page]:
    """The pages (1-based numbers; all by default) of a PDF. Scanned pages are rendered at ``dpi`` and read by
    ``raster.read_image``."""
    tool = _mutool()
    count = page_count(pdf)
    numbers = pages or list(range(1, count + 1))
    result: list[Page] = []
    with tempfile.TemporaryDirectory(prefix="schscan") as work:
        work_dir = Path(work)
        page_list = ",".join(str(n) for n in numbers)
        trace = work_dir / "trace.xml"
        stext = work_dir / "stext.xml"
        _run([tool, "draw", "-q", "-F", "trace", "-o", str(trace), str(pdf), page_list])
        _run([tool, "draw", "-q", "-F", "stext", "-o", str(stext), str(pdf), page_list])
        trace_pages = ET.parse(trace).getroot().findall("page")
        stext_pages = ET.parse(stext).getroot().findall("page")
        for number, tpage, spage in zip(numbers, trace_pages, stext_pages):
            box = [float(v) for v in tpage.get("mediabox", "0 0 612 792").split()]
            page = Page(index=number, width=box[2] - box[0], height=box[3] - box[1], source=pdf, source_page=number)
            images = sum(1 for _ in tpage.iter("fill_image"))
            paths = sum(1 for e in tpage.iter() if e.tag in ("stroke_path", "fill_path"))
            if images and paths < 30:
                from .raster import read_image
                png = work_dir / f"page{number}.png"
                _run([tool, "draw", "-q", "-r", str(dpi), "-o", str(png), str(pdf), str(number)])
                scanned = read_image(png, dpi=dpi)
                scanned.index = number
                scanned.source, scanned.source_page = pdf, number
                result.append(scanned)
                continue
            _read_paths(tpage, page)
            page.texts = _texts(spage)
            _drop_glyph_strokes(page)
            result.append(page)
    return result


def _read_paths(tpage, page: Page) -> None:
    widths: list[float] = []
    path_id = 0
    for element in tpage.iter():
        if element.tag not in ("stroke_path", "fill_path"):
            continue
        m = _transform(element.attrib)
        scale = (abs(m[0] * m[3] - m[1] * m[2])) ** 0.5
        stroke = element.tag == "stroke_path"
        width = float(element.get("linewidth", "0")) * scale if stroke else 0.0
        for points, closed, curved in _subpaths(element, m):
            path_id += 1
            if not closed and len(points) > 3 and abs(points[0][0] - points[-1][0]) < 0.05 and \
                    abs(points[0][1] - points[-1][1]) < 0.05:
                closed = True       # drawn back to its start without a closepath (KiCad's diode triangle)
                points = points[:-1]
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            if not stroke:
                # a small filled round shape is a junction dot; anything else filled is symbol graphics
                if curved and max(w, h) < 6.0 and 0.6 < (w + 1e-6) / (h + 1e-6) < 1.6:
                    page.dots.append(Dot((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, max(w, h) / 2))
                    continue
                closed = True
            pieces = list(zip(points, points[1:] + ([points[0]] if closed else [])))
            straight_manhattan = not curved and not closed and all(
                Segment(a[0], a[1], b[0], b[1]).manhattan for a, b in pieces)
            kind = "wire" if straight_manhattan else "body"
            for a, b in pieces:
                if a != b:
                    page.segments.append(Segment(a[0], a[1], b[0], b[1], width, kind, path_id))
            if closed and len(points) >= 3 and polygon_area(points) > 0.5:
                page.polygons.append(points)
            if stroke and width > 0:
                widths.append(width)
    widths.sort()
    page.line_width = widths[len(widths) // 2] if widths else 0.5
    # a dot's outline (KiCad strokes the filled circle too) is part of the dot, not symbol graphics
    if page.dots:
        extent: dict[int, list[float]] = {}
        for s in page.segments:
            if s.kind == "body":
                e = extent.setdefault(s.path, [1e9, 1e9, -1e9, -1e9])
                e[0], e[1] = min(e[0], s.x1, s.x2), min(e[1], s.y1, s.y2)
                e[2], e[3] = max(e[2], s.x1, s.x2), max(e[3], s.y1, s.y2)
        outline = {path for path, (x0, y0, x1, y1) in extent.items()
                   if any(abs((x0 + x1) / 2 - d.x) < d.r and abs((y0 + y1) / 2 - d.y) < d.r
                          and max(x1 - x0, y1 - y0) < 3 * d.r for d in page.dots)}
        page.segments = [s for s in page.segments if s.path not in outline]
        page.polygons = [p for p in page.polygons
                         if not any(abs(sum(x for x, _ in p) / len(p) - d.x) < d.r and
                                    abs(sum(y for _, y in p) / len(p) - d.y) < d.r and polygon_area(p) < 9 * d.r * d.r
                                    for d in page.dots)]


def _drop_glyph_strokes(page: Page) -> None:
    """Remove strokes that draw text (KiCad's stroke font, under its invisible text): paths inside a text box
    (widened a little), and lone straight strokes that stay within the box itself (a pin line under its pin number
    or an overbar reaches past the text and stays)."""
    if not page.texts:
        return
    tight = [(t.x0 - 0.3, t.y0 - 0.3, t.x1 + 0.3, t.y1 + 0.3) for t in page.texts if t.height > 0]
    wide = [(t.x0 - 0.25 * t.height, t.y0 - 0.25 * t.height, t.x1 + 0.25 * t.height, t.y1 + 0.25 * t.height)
            for t in page.texts if t.height > 0]
    by_path: dict[int, list[Segment]] = {}
    for s in page.segments:
        by_path.setdefault(s.path, []).append(s)

    def inside(boxes, x, y):
        return any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in boxes)

    texts = [t for t in page.texts if t.height > 0]
    drop = set()
    for path, segs in by_path.items():
        boxes = tight if len(segs) == 1 else wide
        if not all(inside(boxes, s.x1, s.y1) and inside(boxes, s.x2, s.y2) for s in segs):
            continue
        # an outline around the whole text (a label flag) is not a glyph
        x0, y0 = min(min(s.x1, s.x2) for s in segs), min(min(s.y1, s.y2) for s in segs)
        x1, y1 = max(max(s.x1, s.x2) for s in segs), max(max(s.y1, s.y2) for s in segs)
        if len(segs) >= 3 and any(x0 <= t.x0 + 0.1 * (t.x1 - t.x0) and x1 >= t.x1 - 0.1 * (t.x1 - t.x0) and
                                  y0 <= t.y0 + 0.1 * (t.y1 - t.y0) and y1 >= t.y1 - 0.1 * (t.y1 - t.y0)
                                  and len(t.text) > 1 for t in texts):
            continue
        drop.add(path)
    page.segments = [s for s in page.segments if s.path not in drop]
