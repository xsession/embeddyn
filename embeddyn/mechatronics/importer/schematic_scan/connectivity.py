"""From a page's strokes and text to symbols, pins and nets.

1. Wire ink is every open horizontal / vertical stroke; symbol ink is closed shapes, curves, diagonals, filled
   shapes, anything inside a closed body (a fuse's centre line) and "plates": short strokes whose two ends touch
   nothing while something touches their middle (capacitor plates, a diode's bar, ground bars).
2. Wire strokes joined end to end, end to side (a T) or across under a junction dot form one net each; crossings
   without a dot do not connect.
3. Symbol ink that touches forms a symbol cluster; a wire end on a cluster is a pin of it.
4. Text gives the names: a reference designator names the nearest cluster, the text just below or beside it is the
   value; a short number by a pin line is the pin number, the text inside the body in line with the pin its pin
   name; a power name (GND, +3.3V ...) or label next to a small cluster without a reference makes that cluster a
   port that names its net; text lying on a wire is a net label. Nets with the same name are one net.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .primitives import Page, Segment, Text, point_in_polygon, polygon_area

REFDES = re.compile(r"#?([A-Z]{1,4})(\d{1,4})([A-Z]?)")
REFDES_PREFIXES = {
    "R", "C", "L", "D", "U", "Q", "J", "P", "K", "F", "FB", "Y", "X", "SW", "S", "T", "TP", "IC", "LED", "BT", "M",
    "RN", "RV", "VR", "CN", "CON", "JP", "H", "MH", "FID", "TR", "Z", "ZD", "BZ", "LS", "MK", "RL", "XTAL", "OSC",
    "TH", "NTC", "PTC", "VD", "VT", "DS", "LD", "A", "FL", "SP", "RT", "CR", "PS", "MOV",
}
POWER = re.compile(r"(?i)(gnd[a-z0-9_]*|[ad]?gnd|earth|chassis|vss[a-z0-9_]*|vcc[a-z0-9_]*|vdd[a-z0-9_]*|vee|"
                   r"v(bus|bat|in|out|ref|sys)[a-z0-9_]*|[+-]\d+([.,v]\d*)?v?\d*[a-z0-9_]*|\d+v\d*)")
PIN_NUMBER = re.compile(r"[A-Z]{0,2}\d{1,3}")
NUMBER_ONLY = re.compile(r"\d{1,3}")
LABEL = re.compile(r"[A-Za-z0-9_+\-./\[\]~{}#!]+")
UNIT_PREFIXES = {"U", "IC", "Q", "K", "RN", "SW", "D", "J"}


@dataclass
class Contact:
    cluster: int
    net: int                 # wire group
    x: float
    y: float
    segment: int             # the wire segment ending here (the pin line)
    pin: str = ""
    pin_name: str = ""
    guessed: bool = False


@dataclass
class Symbol:
    ref: str
    value: str = ""
    clusters: list[int] = field(default_factory=list)
    contacts: list[Contact] = field(default_factory=list)
    box: tuple[float, float, float, float] = (0, 0, 0, 0)
    ref_text: Text | None = None


@dataclass
class Port:
    name: str
    power: bool
    cluster: int
    contacts: list[Contact] = field(default_factory=list)


@dataclass
class PageResult:
    page: Page
    wire_group: list[int]                       # per segment index: its wire group (-1 for symbol ink)
    symbols: dict[str, Symbol] = field(default_factory=dict)
    ports: list[Port] = field(default_factory=list)
    labels: dict[int, list[str]] = field(default_factory=dict)   # wire group -> label names
    notes: list[str] = field(default_factory=list)
    unnamed_clusters: list[tuple[int, tuple[float, float, float, float], int]] = field(default_factory=list)


class _Union:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def join(self, a: int, b: int) -> None:
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


class _Grid:
    """Segments bucketed by cell, for "what is near this point" queries."""

    def __init__(self, segments: list[Segment], indices, cell: float, margin: float):
        self.cell = cell
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.segments = segments
        for i in indices:
            x0, y0, x1, y1 = segments[i].bbox()
            for cx in range(int((x0 - margin) // cell), int((x1 + margin) // cell) + 1):
                for cy in range(int((y0 - margin) // cell), int((y1 + margin) // cell) + 1):
                    self.cells[(cx, cy)].append(i)

    def near(self, x: float, y: float) -> list[int]:
        return self.cells.get((int(x // self.cell), int(y // self.cell)), [])


def _bbox_union(boxes):
    boxes = list(boxes)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _box_distance(a, b) -> float:
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return math.hypot(dx, dy)


def is_refdes(text: str) -> bool:
    """R12, C3, U5A (a unit of a multi-part symbol), FB100 ... but not a part number such as B5819W."""
    m = REFDES.fullmatch(text)
    return bool(m) and m.group(1) in REFDES_PREFIXES and (not m.group(3) or m.group(1) in UNIT_PREFIXES)


def is_power(text: str) -> bool:
    return bool(POWER.fullmatch(text))


NET_NAME = re.compile(r"[A-Z0-9_+\-./!~\[\]]*[A-Z][A-Z0-9_+\-./!~\[\]]*")


def _usable_name(t: Text, raster: bool) -> bool:
    """From pixels only confident, net-like words name nets (OCR reads bits of symbols as words)."""
    if not raster:
        return True
    return t.confidence >= 0.75 and len(t.text) >= 2 and (is_power(t.text) or bool(NET_NAME.fullmatch(t.text)))


def analyse(page: Page, tolerance: float | None = None) -> PageResult:
    tol = tolerance if tolerance is not None else (
        max(0.6, 1.5 * page.line_width) if page.raster else max(0.3, 0.75 * page.line_width))
    segments = page.segments
    texts = page.texts
    heights = sorted(t.height for t in texts if t.height > 0 and len(t.text) <= 12)
    h = heights[len(heights) // 2] if heights else 7.0

    # --- 1. which ink is wire and which is symbol -----------------------------------------------------------
    page_area = page.width * page.height
    frame_paths = set()
    polygons = []
    for points in page.polygons:
        area = polygon_area(points)
        if area > 0.15 * page_area:
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            frame_paths.add((min(xs), min(ys), max(xs), max(ys)))
        else:
            polygons.append(points)

    def on_frame(s: Segment) -> bool:
        """A side of a page-sized rectangle (the drawing frame)."""
        for x0, y0, x1, y1 in frame_paths:
            vertical_side = abs(s.x1 - s.x2) < tol and min(abs(s.x1 - x0), abs(s.x1 - x1)) < tol
            horizontal_side = abs(s.y1 - s.y2) < tol and min(abs(s.y1 - y0), abs(s.y1 - y1)) < tol
            if vertical_side or horizontal_side:
                return True
        return False

    keep = [i for i, s in enumerate(segments) if not on_frame(s)]

    def box_of(points):
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        return min(xs), min(ys), max(xs), max(ys)

    def within(box, x, y, margin=0.0):
        return box[0] - margin <= x <= box[2] + margin and box[1] - margin <= y <= box[3] + margin

    # a closed shape around other shapes, junction dots or several wires is an annotation box (a frame drawn
    # round a part of the circuit), not a symbol body: drop its outline
    boxes = [box_of(p) for p in polygons]
    annotation = set()
    for n, box in enumerate(boxes):
        inner_shapes = sum(1 for m, other in enumerate(boxes) if m != n and other != box and
                           within(box, other[0], other[1]) and within(box, other[2], other[3]))
        dots = sum(1 for d in page.dots if within(box, d.x, d.y))
        wires_inside = sum(1 for i in keep if segments[i].kind == "wire" and segments[i].length >= 2 * h and
                           within(box, segments[i].x1, segments[i].y1) and within(box, segments[i].x2, segments[i].y2))
        large = min(box[2] - box[0], box[3] - box[1]) > 8 * h
        if large and (dots or wires_inside >= 2 or (inner_shapes and wires_inside)):
            annotation.add(n)
    if annotation:
        def on_outline(s: Segment) -> bool:
            for n in annotation:
                for (ax, ay), (bx, by) in zip(polygons[n], polygons[n][1:] + polygons[n][:1]):
                    edge = Segment(ax, ay, bx, by)
                    if edge.distance(s.x1, s.y1)[0] <= tol and edge.distance(s.x2, s.y2)[0] <= tol:
                        return True
            return False
        keep = [i for i in keep if segments[i].kind != "body" or not on_outline(segments[i])]
        polygons = [p for n, p in enumerate(polygons) if n not in annotation]
    kind = {i: segments[i].kind for i in keep}
    poly_boxes = [box_of(p) for p in polygons]
    for i in keep:
        s = segments[i]
        if kind[i] != "wire":
            continue
        mx, my = (s.x1 + s.x2) / 2, (s.y1 + s.y2) / 2
        for points, box in zip(polygons, poly_boxes):
            if box[0] + tol < mx < box[2] - tol and box[1] + tol < my < box[3] - tol and \
                    point_in_polygon(mx, my, points):
                # through a closed body (a fuse's centre line): part of the symbol
                kind[i] = "body"
                break

    cell = max(8 * tol, 2 * h)
    plate_max = 2.2 * h

    # a short straight piece whose two ends run into the same symbol drawing is part of it (the flat top of a
    # circle or arc read from pixels, a line inside a symbol)
    bodies0 = [i for i in keep if kind[i] == "body"]
    bgrid0 = _Grid(segments, bodies0, cell, tol)
    for i in keep:
        s = segments[i]
        if kind[i] != "wire" or s.length > 2 * h:
            continue
        ends = []
        for x, y in s.ends:
            ends.append({segments[j].path for j in bgrid0.near(x, y) if segments[j].distance(x, y)[0] <= tol})
        if ends[0] & ends[1]:
            kind[i] = "body"
    for _ in range(3):
        wires = [i for i in keep if kind[i] == "wire"]
        bodies = [i for i in keep if kind[i] == "body"]
        wgrid = _Grid(segments, wires, cell, tol)
        bgrid = _Grid(segments, bodies, cell, tol)
        changed = False
        for i in wires:
            s = segments[i]
            if s.length > plate_max:
                continue
            free_ends = 0
            for x, y in s.ends:
                touched = any(j != i and segments[j].distance(x, y)[0] <= tol for j in wgrid.near(x, y)) or \
                    any(segments[j].distance(x, y)[0] <= tol for j in bgrid.near(x, y))
                free_ends += not touched
            if free_ends < 2:
                continue
            # something ends on its middle
            middle = False
            for j in set(wgrid.near((s.x1 + s.x2) / 2, (s.y1 + s.y2) / 2)) | set(wgrid.near(s.x1, s.y1)) | \
                    set(wgrid.near(s.x2, s.y2)):
                if j == i:
                    continue
                for x, y in segments[j].ends:
                    d, t = s.distance(x, y)
                    if d <= tol and 0.05 < t < 0.95:
                        middle = True
            if middle:
                kind[i] = "body"
                changed = True
        if not changed:
            break

    wires = [i for i in keep if kind[i] == "wire"]
    bodies = [i for i in keep if kind[i] == "body"]
    wgrid = _Grid(segments, wires, cell, tol)
    bgrid = _Grid(segments, bodies, cell, tol)

    # --- 2. nets of wire ink -----------------------------------------------------------------------------------
    union = _Union(len(segments))
    dot_points = [(d.x, d.y, max(d.r, tol)) for d in page.dots]
    for i in wires:
        s = segments[i]
        for x, y in s.ends:
            for j in wgrid.near(x, y):
                if j != i and segments[j].distance(x, y)[0] <= tol:
                    union.join(i, j)
    # crossings joined by a dot
    for x, y, r in dot_points:
        near = [j for j in wgrid.near(x, y) if segments[j].distance(x, y)[0] <= r + tol]
        for j in near[1:]:
            union.join(near[0], j)
    wire_group = [-1] * len(segments)
    for i in wires:
        wire_group[i] = union.find(i)

    # --- 3. symbol clusters -------------------------------------------------------------------------------------
    bunion = _Union(len(segments))
    by_path: dict[int, list[int]] = defaultdict(list)
    for i in bodies:
        by_path[segments[i].path].append(i)
    for members in by_path.values():
        for j in members[1:]:
            bunion.join(members[0], j)
    for i in bodies:
        for x, y in segments[i].ends:
            for j in bgrid.near(x, y):
                if j != i and segments[j].distance(x, y)[0] <= 1.5 * tol:
                    bunion.join(i, j)
    # ink inside a closed body belongs to it
    for points, box in zip(polygons, poly_boxes):
        inside = [i for i in bodies
                  if box[0] - tol <= segments[i].x1 <= box[2] + tol and box[1] - tol <= segments[i].y1 <= box[3] + tol
                  and box[0] - tol <= segments[i].x2 <= box[2] + tol and box[1] - tol <= segments[i].y2 <= box[3] + tol]
        for j in inside[1:]:
            bunion.join(inside[0], j)
    cluster_of = {i: bunion.find(i) for i in bodies}
    # a no-connect marker (an X of two short diagonals) is not a symbol: drop it
    members: dict[int, list[int]] = defaultdict(list)
    for i, c in cluster_of.items():
        members[c].append(i)
    no_connect = set()
    for c, items in members.items():
        if len(items) == 2 and all(not segments[i].manhattan and segments[i].length < 2.5 * h for i in items):
            a, b = segments[items[0]], segments[items[1]]
            if math.hypot((a.x1 + a.x2 - b.x1 - b.x2) / 2, (a.y1 + a.y2 - b.y1 - b.y2) / 2) < tol + 0.2 * a.length:
                no_connect.add(c)
    if no_connect:
        bodies = [i for i in bodies if cluster_of[i] not in no_connect]
        cluster_of = {i: c for i, c in cluster_of.items() if c not in no_connect}
        bgrid = _Grid(segments, bodies, cell, tol)
    cluster_boxes: dict[int, tuple] = {}
    for i, c in cluster_of.items():
        b = segments[i].bbox()
        cluster_boxes[c] = _bbox_union([cluster_boxes[c], b]) if c in cluster_boxes else b

    # --- 4. contacts: wire ends on symbol ink (and symbol ends on wires) -------------------------------------
    contacts: list[Contact] = []
    seen = set()
    for i in wires:
        s = segments[i]
        for x, y in s.ends:
            hit = None
            for j in bgrid.near(x, y):
                if segments[j].distance(x, y)[0] <= tol:
                    hit = cluster_of[j]
                    break
            if hit is None:
                continue
            key = (hit, round(x / tol), round(y / tol))
            if key in seen:
                continue
            seen.add(key)
            contacts.append(Contact(hit, wire_group[i], x, y, i))
    for i in bodies:
        s = segments[i]
        for x, y in s.ends:
            for j in wgrid.near(x, y):
                d, t = segments[j].distance(x, y)
                if d <= tol and 0.0 < t < 1.0:
                    c = cluster_of[i]
                    key = (c, round(x / tol), round(y / tol))
                    if key not in seen and not any(k[0] == c and abs(k[1] * tol - x) < 2 * tol and
                                                   abs(k[2] * tol - y) < 2 * tol for k in seen):
                        seen.add(key)
                        contacts.append(Contact(c, wire_group[j], x, y, j))
                    break

    # bits of an unread word (a letter's stem touching its curve) are smaller than a letter on both sides: no pin
    group_box: dict[int, tuple] = {}
    for i in wires:
        b = segments[i].bbox()
        g = wire_group[i]
        group_box[g] = _bbox_union([group_box[g], b]) if g in group_box else b

    def tiny(box):
        return max(box[2] - box[0], box[3] - box[1]) < 1.2 * h
    if page.raster:
        contacts = [c for c in contacts if not (tiny(cluster_boxes[c.cluster]) and tiny(group_box[c.net]))]

    # one wire meeting one symbol at two points close together (a pin line ending on a line through the body)
    merged: list[Contact] = []
    for c in contacts:
        if not any(m.cluster == c.cluster and m.net == c.net and math.hypot(m.x - c.x, m.y - c.y) < 0.6 * h
                   for m in merged):
            merged.append(c)
    contacts = merged

    result = PageResult(page=page, wire_group=wire_group)
    contacts_by_cluster: dict[int, list[Contact]] = defaultdict(list)
    for c in contacts:
        contacts_by_cluster[c.cluster].append(c)

    # --- 5. text roles -------------------------------------------------------------------------------------------
    used: set[int] = set()
    refs = [k for k, t in enumerate(texts) if is_refdes(t.text)]

    # ports: a cluster joining a single net, named by the text inside it (a label flag) or, for a small one, the
    # text next to it (a power or ground symbol); a reference designator close by means a part instead
    port_clusters: dict[int, Port] = {}
    for c, box in cluster_boxes.items():
        if c not in contacts_by_cluster or len({k.net for k in contacts_by_cluster[c]}) != 1:
            continue
        small = max(box[2] - box[0], box[3] - box[1]) <= 4.5 * h
        best = None
        for k, t in enumerate(texts):
            if k in used or is_refdes(t.text) or not LABEL.fullmatch(t.text) or NUMBER_ONLY.fullmatch(t.text):
                continue
            if not _usable_name(t, page.raster) or (page.raster and not is_power(t.text) and not small):
                continue
            inside = box[0] - 0.3 * h <= t.cx <= box[2] + 0.3 * h and box[1] - 0.3 * h <= t.cy <= box[3] + 0.3 * h
            d = 0.0 if inside else t.box_distance(box)
            if not (inside or (small and d <= 1.6 * h)):
                continue
            if page.raster and not inside and not is_power(t.text):
                continue      # from pixels, a word next to a small drawing is too often a pin name or a value
            # rank outside names by how close they are to the symbol's centre (power symbols side by side)
            rank = 0.0 if inside else math.hypot(t.cx - (box[0] + box[2]) / 2, t.cy - (box[1] + box[3]) / 2)
            if best is None or rank < best[2]:
                best = (d, k, rank)
        if best is None:
            continue
        name = texts[best[1]].text
        ref_d = min((texts[k].box_distance(box) for k in refs), default=1e9)
        # (a name inside the shape is a label flag whatever is next to it)
        if best[0] > 0 and not is_power(name) and ref_d < max(2 * best[0], 3 * h):
            continue
        used.add(best[1])
        port_clusters[c] = Port(name=name, power=is_power(name), cluster=c, contacts=contacts_by_cluster[c])
    result.ports = list(port_clusters.values())

    # reference designators -> clusters: one-to-one first (the assignment with the least total distance, so a
    # reference that happens to touch a neighbour's body still goes to its own), then clusters left over (pieces of
    # a symbol drawn apart, e.g. a MOSFET's arrow) join the nearest reference
    symbol_clusters = [c for c in cluster_boxes if c not in port_clusters and c in contacts_by_cluster]
    assignment: dict[int, int] = {}
    reach = 4.5 * h
    cost = [[texts[k].box_distance(cluster_boxes[c]) for c in symbol_clusters] for k in refs]
    if refs and symbol_clusters:
        try:
            from scipy.optimize import linear_sum_assignment
            big = 1e6
            matrix = [[d if d <= reach else big for d in row] for row in cost]
            rows, cols = linear_sum_assignment(matrix)
            for r, c in zip(rows, cols):
                if matrix[r][c] < big:
                    assignment[symbol_clusters[c]] = refs[r]
        except ImportError:
            pairs = sorted((cost[r][c], r, c) for r in range(len(refs)) for c in range(len(symbol_clusters))
                           if cost[r][c] <= reach)
            done_r, done_c = set(), set()
            for d, r, c in pairs:
                if r not in done_r and c not in done_c:
                    done_r.add(r)
                    done_c.add(c)
                    assignment[symbol_clusters[c]] = refs[r]
    for n, c in enumerate(symbol_clusters):
        if c in assignment:
            continue
        best = min(((cost[r][n], refs[r]) for r in range(len(refs)) if cost[r][n] <= reach), default=None)
        if best:
            assignment[c] = best[1]
    for k in refs:
        used.add(k)
    for c, k in assignment.items():
        t = texts[k]
        m = REFDES.fullmatch(t.text)
        ref = t.text.lstrip("#")
        if m and m.group(3) and m.group(1) in UNIT_PREFIXES:
            ref = m.group(1) + m.group(2)
        symbol = result.symbols.get(ref)
        if symbol is None:
            symbol = result.symbols[ref] = Symbol(ref=ref, ref_text=t, box=cluster_boxes[c])
        symbol.clusters.append(c)
        symbol.box = _bbox_union([symbol.box, cluster_boxes[c]])
        symbol.contacts.extend(contacts_by_cluster[c])

    # values: just below, above or beside the reference
    # (a word inside the body is a pin name, not the value; a large symbol, e.g. an MCU, has its value at the far
    # edge of the body, below or above the pins, in line with the reference)
    for symbol in result.symbols.values():
        t = symbol.ref_text
        box = symbol.box
        best = None
        far = None
        for k, v in enumerate(texts):
            if k in used or is_refdes(v.text) or is_power(v.text):
                continue
            if box[0] + 0.5 * h < v.cx < box[2] - 0.5 * h and box[1] + 0.5 * h < v.cy < box[3] - 0.5 * h:
                continue
            if not t.vertical and not v.vertical and abs(v.x0 - t.x0) <= 2.5 * h:
                if t.cy < box[1] + h:                       # reference above the body: value at its bottom edge
                    edge, ok = box[3], -1.2 * h <= v.cy - box[3] <= 1.5 * h
                elif t.cy > box[3] - h:                     # reference below: value at the top edge
                    edge, ok = box[1], -1.5 * h <= v.cy - box[1] <= 1.2 * h
                else:
                    edge, ok = 0.0, False
                if ok:
                    d = abs(v.cy - edge) + abs(v.x0 - t.x0)
                    if far is None or d < far[0]:
                        far = (d, k)
            if t.vertical == v.vertical and not t.vertical:
                below = 0 <= v.y0 - t.y1 + 0.3 * h <= 1.2 * h and abs(v.x0 - t.x0) <= 2.5 * h
                above = 0 <= t.y0 - v.y1 + 0.3 * h <= 1.2 * h and abs(v.x0 - t.x0) <= 2.5 * h
                beside = abs(v.cy - t.cy) <= 0.4 * h and 0 <= v.x0 - t.x1 <= 1.5 * h
            else:
                below = 0 <= v.x0 - t.x1 + 0.3 * h <= 1.2 * h and abs(v.y1 - t.y1) <= 2.5 * h
                above = 0 <= t.x0 - v.x1 + 0.3 * h <= 1.2 * h and abs(v.y1 - t.y1) <= 2.5 * h
                beside = abs(v.cx - t.cx) <= 0.4 * h and 0 <= t.y0 - v.y1 <= 1.5 * h
            if below or above or beside:
                d = math.hypot(v.cx - t.cx, v.cy - t.cy) + (0 if below else 0.5 * h)
                if best is None or d < best[0]:
                    best = (d, k)
        if far and (best is None or len(symbol.contacts) > 4):    # a chip: the near words are pin numbers
            best = far
        if best:
            symbol.value = texts[best[1]].text
            used.add(best[1])

    # pin numbers: short numbers beside the pin line near the body
    all_contacts = [c for s in result.symbols.values() for c in s.contacts]
    candidates = []
    for ci, c in enumerate(all_contacts):
        s = segments[c.segment]
        (ax, ay), (bx, by) = s.ends
        far = (bx, by) if math.hypot(ax - c.x, ay - c.y) < math.hypot(bx - c.x, by - c.y) else (ax, ay)
        reach = min(s.length, 6 * h)
        length = math.hypot(far[0] - c.x, far[1] - c.y) or 1.0
        ex, ey = c.x + (far[0] - c.x) * reach / length, c.y + (far[1] - c.y) * reach / length
        pin_line = Segment(c.x, c.y, ex, ey)
        for k, t in enumerate(texts):
            if k in used or not PIN_NUMBER.fullmatch(t.text):
                continue
            d, foot = pin_line.distance(t.cx, t.cy)
            if d <= 1.25 * h and 0.0 < foot < 1.0:
                # numbers sit beside the middle of their own pin line: across-distance first, a neighbour's number
                # further along the line loses a tie
                along = abs(foot - 0.5) * pin_line.length
                candidates.append((d + 0.3 * along, ci, k))
    taken_c, taken_t = set(), set()
    for d, ci, k in sorted(candidates):
        if ci in taken_c or k in taken_t:
            continue
        taken_c.add(ci)
        taken_t.add(k)
        all_contacts[ci].pin = texts[k].text
        used.add(k)

    # pin names: inside the body, in line with the pin
    for symbol in result.symbols.values():
        box = symbol.box
        for c in symbol.contacts:
            s = segments[c.segment]
            horizontal = s.horizontal
            best = None
            for k, t in enumerate(texts):
                if k in used or not (box[0] - 0.3 * h <= t.cx <= box[2] + 0.3 * h and
                                     box[1] - 0.3 * h <= t.cy <= box[3] + 0.3 * h):
                    continue
                aligned = abs(t.cy - c.y) <= 0.6 * h if horizontal else abs(t.cx - c.x) <= 0.6 * h
                if not aligned:
                    continue
                d = abs(t.cx - c.x) if horizontal else abs(t.cy - c.y)
                if d <= 12 * h and (best is None or d < best[0]):
                    best = (d, k)
            if best:
                c.pin_name = texts[best[1]].text
        for c in symbol.contacts:
            if c.pin_name:
                used.update(k for k, t in enumerate(texts) if t.text == c.pin_name and
                            box[0] - h <= t.cx <= box[2] + h and box[1] - h <= t.cy <= box[3] + h)

    # pins without a number: two-pin parts are numbered left-to-right / top-to-bottom, others ?n
    for symbol in result.symbols.values():
        unnumbered = [c for c in symbol.contacts if not c.pin]
        if not unnumbered:
            continue
        ordered = sorted(unnumbered, key=lambda c: (round(c.x / h), c.y)
                         if (symbol.box[2] - symbol.box[0]) >= (symbol.box[3] - symbol.box[1])
                         else (round(c.y / h), c.x))
        numbered = {c.pin for c in symbol.contacts if c.pin}
        n = 1
        for c in ordered:
            while str(n) in numbered:
                n += 1
            c.pin, c.guessed = str(n), True
            n += 1

    # net labels: text standing on a wire that runs along it (KiCad, Altium, Eagle place the label's baseline on
    # the wire); a value or title merely near a wire does not count
    for k, t in enumerate(texts):
        if k in used or not LABEL.fullmatch(t.text) or NUMBER_ONLY.fullmatch(t.text) or is_refdes(t.text) or \
                not _usable_name(t, page.raster):
            continue
        best = None
        probes = [(t.cx, t.y1), (t.cx, t.cy)] if not t.vertical else [(t.x1, t.cy), (t.x0, t.cy), (t.cx, t.cy)]
        for x, y in probes:
            for j in wgrid.near(x, y):
                s = segments[j]
                if s.horizontal == t.vertical:
                    continue
                d, _ = s.distance(x, y)
                # the wire under the label covers most of it
                if t.vertical:
                    overlap = min(t.y1, max(s.y1, s.y2)) - max(t.y0, min(s.y1, s.y2))
                    span = t.y1 - t.y0
                else:
                    overlap = min(t.x1, max(s.x1, s.x2)) - max(t.x0, min(s.x1, s.x2))
                    span = t.x1 - t.x0
                if d <= 0.6 * h and overlap >= 0.5 * span and (best is None or d < best[0]):
                    best = (d, j)
        if best:
            result.labels.setdefault(wire_group[best[1]], []).append(t.text)
            used.add(k)

    for c, box in cluster_boxes.items():
        if c in contacts_by_cluster and c not in port_clusters and c not in assignment:
            result.unnamed_clusters.append((c, box, len(contacts_by_cluster[c])))
    if result.unnamed_clusters:
        result.notes.append(f"page {page.index}: {len(result.unnamed_clusters)} symbol(s) with wires but no "
                            "reference designator or name")
    return result
