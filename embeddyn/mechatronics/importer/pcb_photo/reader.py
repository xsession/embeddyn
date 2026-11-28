"""Read a board from photos: parts, packages, markings, pads and nets.

``read_board(top, bottom, closeups, board_mm)``:

1. straighten the top (and bottom, mirrored) photo by the board outline;
2. classify pixels (mask, copper, metal, bodies, holes) and find parts with their pads;
3. read markings: on the board photo for parts large enough, and from close-up photos placed onto the board by
   matching their parts;
4. fix the scale from parts whose package became known (a read part number), then name packages from sizes;
5. trace copper into nets on each side and join the sides through vias;
6. number pins (from the marking's reading direction when it was read) and name the parts.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from ...smd.packages import package as find_package
from .image import BoardImage, straighten
from .nets import Connectivity, align, conductor_regions, join_sides, net_ids
from .parts import Pad, Part, _dark_with_text, body_mask, find_pads, find_parts, identify, number
from .segment import Classes, classify, white_balance

PREFIX = {"resistor": "R", "capacitor": "C", "inductor": "L", "diode": "D", "transistor": "Q", "mosfet": "Q",
          "regulator": "U", "ic": "U", "logic": "U", "optocoupler": "U", "sensor": "U", "led": "LED",
          "crystal": "Y", "connector": "J", "battery": "BT", "mechanical": "MP"}


@dataclass
class SideResult:
    image: BoardImage
    balanced: np.ndarray
    classes: Classes
    pads: dict[int, Pad]
    pad_labels: np.ndarray
    parts: list[Part]
    bodies: np.ndarray


@dataclass
class BoardResult:
    top: SideResult
    bottom: SideResult | None
    connectivity: Connectivity
    px_per_mm: float
    nets: dict[str, list[tuple[str, str]]] = field(default_factory=dict)     # net -> [(ref, pin)]
    parts: list[Part] = field(default_factory=list)
    closeups: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # marking readers: the board photo's (transform None) and every placed close-up's (close-up px -> board px)
    readers: list = field(default_factory=list)

    def part(self, ref: str) -> Part | None:
        return next((p for p in self.parts if p.ref == ref), None)


def _side(image: BoardImage, px_per_mm: float, side: str) -> SideResult:
    balanced = white_balance(image.rgb)
    classes = classify(image.rgb, px_per_mm, image.mask_hue)
    bodies = body_mask(classes, px_per_mm)
    pads, pad_labels = find_pads(classes, px_per_mm, side, bodies)
    pad_by_id = {p.id: p for p in pads}
    parts = find_parts(balanced, classes, px_per_mm, pads, pad_labels, side)
    return SideResult(image, balanced, classes, pad_by_id, pad_labels, parts, bodies)


def _mechanical(part: Part, px_per_mm: float) -> bool:
    return part.length / px_per_mm > 12 and part.width / px_per_mm > 8 and len(part.pads) < 8


def _scale_from_anchors(parts: list[Part], px_per_mm: float) -> tuple[float, list[str]]:
    """Scale from parts whose package is known from their part number (e.g. 74HC595D -> SOIC-16)."""
    estimates, used = [], []
    for part in parts:
        for reading in part.decoded[:1]:
            name = reading.get("package") or ""
            if reading.get("kind") != "part-number" or not name:
                continue
            pkg = find_package(name) or _family_package(name, len(part.pads))
            if pkg is None:
                continue
            estimates.append(part.length / max(pkg.length, pkg.width))
            used.append(f"{part.ref or part.id} {reading.get('part')} ({pkg.name})")
    if not estimates:
        return px_per_mm, []
    return float(np.median(estimates)), used


def _family_package(name: str, leads: int):
    family = name.split("-")[0].strip().upper()
    if family == "SOIC":
        for n in (8, 14, 16):
            if abs(leads - n) <= 3:
                return find_package(f"SOIC-{n}")
        return find_package("SOIC-16")
    if family == "TSSOP":
        return find_package(f"TSSOP-{leads}") or find_package("TSSOP-16")
    return None


def read_board(top: Path, bottom: Path | None = None, closeups: list[Path] | None = None,
               board_mm: tuple[float, float] | None = None, px_per_mm: float | None = None) -> BoardResult:
    notes: list[str] = []
    if board_mm:
        top_img = straighten(top, "top", board_mm, px_per_mm=px_per_mm or 20.0)
        scale = top_img.px_per_mm
    else:
        top_img = straighten(top, "top", None, px_per_mm=1.0)
        # assume a 100 mm board until parts of known size say otherwise
        scale = px_per_mm or max(top_img.rgb.shape[:2]) / 100.0
        top_img.px_per_mm = scale
        notes.append(f"board size not given: scale first assumed {scale:.1f} px/mm (longest side 100 mm)")
    top_res = _side(top_img, scale, "top")
    for part in top_res.parts:
        identify(part, top_res.pads, scale)
    from .markings import MarkingReader
    readers = [(MarkingReader(top_res.balanced, _dark_with_text(top_res.classes, scale), str(top),
                              min_side=max(6.0, 0.5 * scale), char=0.65 * scale).read(), None)]
    closeup_info: list[dict] = []
    if closeups:
        from .closeup import apply_closeups
        closeup_info, more = apply_closeups(closeups, top_res, scale)
        readers += more
    assign_markings(top_res.parts, readers, scale)

    # the scale from parts identified by their part number
    if not board_mm and not px_per_mm:
        estimate, used = _scale_from_anchors(top_res.parts, scale)
        if used and abs(estimate / scale - 1) > 0.03:
            notes.append(f"scale {estimate:.2f} px/mm from {', '.join(used)} (was {scale:.2f})")
            scale = estimate
            top_img.px_per_mm = scale
            for part in top_res.parts:
                identify(part, top_res.pads, scale)

    bottom_res = None
    b2t = None
    if bottom is not None:
        bottom_img = straighten(bottom, "bottom", board_mm, px_per_mm=scale if board_mm else 1.0)
        if bottom_img.rgb.shape[:2] != top_img.rgb.shape[:2]:
            resized = Image.fromarray(bottom_img.rgb).resize((top_img.rgb.shape[1], top_img.rgb.shape[0]),
                                                             Image.BILINEAR)
            bottom_img.rgb = np.asarray(resized)
        bottom_img.px_per_mm = scale
        bottom_res = _side(bottom_img, scale, "bottom")
        for part in bottom_res.parts:
            identify(part, bottom_res.pads, scale)
        init = np.array([[1.0, 0, 0], [0, 1.0, 0]])
        top_side = conductor_regions(top_res.classes, top_res.bodies, scale, "top")
        bottom_side = conductor_regions(bottom_res.classes, bottom_res.bodies, scale, "bottom")
        b2t, residual, matched = align(bottom_side.vias, top_side.vias, init, scale)
        notes.append(f"bottom photo fitted onto the top by {matched} holes, median error "
                     f"{residual / scale:.2f} mm" if matched else "bottom photo placed by its outline only "
                     "(too few holes matched)")
        connectivity = join_sides(top_side, bottom_side, b2t, scale)
    else:
        connectivity = join_sides(conductor_regions(top_res.classes, top_res.bodies, scale, "top"), None, None,
                                  scale)

    result = BoardResult(top_res, bottom_res, connectivity, scale, closeups=closeup_info, notes=notes,
                         readers=readers)
    _finish(result)
    return result


def _finish(result: BoardResult) -> None:
    sides = [result.top] + ([result.bottom] if result.bottom else [])
    all_parts: list[Part] = []
    for side in sides:
        for part in side.parts:
            if _mechanical(part, result.px_per_mm):
                part.kind = "battery" if part.tone == "dark" else "mechanical"
                part.package = part.package or "mechanical"
            number(part, side.pads)
            all_parts.append(part)
    # reference designators, in reading order (rows of ~5 mm, left to right), per prefix
    counters: Counter = Counter()
    row = 5.0 * result.px_per_mm
    for part in sorted(all_parts, key=lambda p: (p.side != "top", round(p.cy / row), p.cx)):
        prefix = PREFIX.get(part.kind, "X")
        counters[prefix] += 1
        part.ref = f"{prefix}{counters[prefix]}"
    result.parts = all_parts
    _rebuild_nets(result)


def _to_board(m, x: float, y: float) -> tuple[float, float]:
    if m is None:
        return x, y
    return float(m[0, 0] * x + m[0, 1] * y + m[0, 2]), float(m[1, 0] * x + m[1, 1] * y + m[1, 2])


def _inside(part: Part, x: float, y: float, margin: float) -> bool:
    dx, dy = x - part.cx, y - part.cy
    u = abs(dx * math.cos(part.angle) + dy * math.sin(part.angle))
    t = abs(-dx * math.sin(part.angle) + dy * math.cos(part.angle))
    return u <= part.length / 2 + margin and t <= part.width / 2 + margin


def _bodies_on(part: Part, readers: list, px_per_mm: float):
    """(reader, body, its centre on the board) of every marking body that lies on ``part``."""
    for reader, m in readers:
        for body in reader.bodies:
            x, y = _to_board(m, body.cx, body.cy)
            if _inside(part, x, y, 0.3 * px_per_mm):
                yield reader, m, body


def assign_markings(parts: list[Part], readers: list, px_per_mm: float) -> int:
    """Give every part the best marking read on it (close-ups count more: they are sharper). Parts whose marking
    a person entered keep it. Returns how many parts have a read marking."""
    from ...smd import decode
    count = 0
    for part in parts:
        if any("marking entered by hand" == n for n in part.notes):
            continue
        best = None
        for reader, m, body in _bodies_on(part, readers, px_per_mm):
            # only confident readings that decode well: a wrong value that looks plausible is worse than none
            words = [w for w in body.words if (w.conf >= 0.55 and w.score >= 0.6) or
                     (w.conf >= 0.75 and len(w.text) >= 4 and any(c.isdigit() for c in w.text)
                      and any(c.isalpha() for c in w.text))]
            if not words:
                continue
            quality = float(np.mean([w.conf * (0.3 + w.score) for w in words])) * (1.2 if m is not None else 1.0)
            if best is None or quality > best[0]:
                best = (quality, reader, m, body)
        if best is None:
            continue
        quality, reader, m, body = best
        text = body.text
        up = body.words[0].up
        if m is not None:
            v = m[:, :2] @ np.array(up)
            n = float(np.linalg.norm(v)) or 1.0
            up = (float(v[0] / n), float(v[1] / n))
        decoded = decode(text, part.package or None, part.kind if part.kind in ("resistor", "capacitor") else None)
        part.marking, part.marking_conf, part.up = text, round(min(1.0, quality), 2), up
        part.decoded = [r.as_dict() for r in decoded.readings[:4]]
        part.value = part.part_number = ""
        if decoded.best:
            if decoded.best.kind == "value" and decoded.best.value:
                part.value = decoded.best.value.text
                part.kind = decoded.best.value.kind if part.kind in ("", "resistor", "capacitor", "inductor") \
                    else part.kind
            else:
                part.part_number = part.value = decoded.best.part
        part.notes = [n for n in part.notes if not n.startswith("marking read on")]
        part.notes.append(f"marking read on {Path(reader.source).name or 'the board photo'} by OCR: check it "
                          "(typing the right one fixes it and teaches the reader)")
        count += 1
    return count


def set_marking(result: BoardResult, ref: str, text: str, package: str | None = None,
                pin1: str | None = None) -> Part:
    """A marking typed in by hand (or corrected): decode it, update the part, renumber its pins.

    ``pin1`` says how the marking sits in the (straightened) photo, which fixes pin 1: "upright", "upside_down",
    "reads_up" (turned a quarter left: read from bottom to top) or "reads_down"; "auto" keeps the direction OCR found,
    or the body axis."""
    from ...smd import decode
    part = result.part(ref)
    if part is None:
        raise LookupError(f"no part {ref}")
    side = result.top if part.side == "top" else result.bottom
    if package:
        part.package = package
    decoded = decode(text, part.package or None, None)
    part.marking, part.marking_conf = text.strip(), 1.0
    part.decoded = [r.as_dict() for r in decoded.readings[:4]]
    part.value = part.part_number = ""
    if decoded.best:
        best = decoded.best
        if best.kind == "value" and best.value:
            part.value, part.kind = best.value.text, best.value.kind
        else:
            part.part_number = part.value = best.part
            if best.package and not package:
                if find_package(best.package):
                    part.package = best.package
                else:                       # a family only ("SOIC"): keep the measured size's package
                    part.notes.append(f"the part number says {best.package}")
    part.notes = [n for n in part.notes if not n.startswith("marking read on")]
    part.notes.append("marking entered by hand")
    # the "up" of the letters, in image coordinates (y down)
    directions = {"upright": (0.0, -1.0), "upside_down": (0.0, 1.0), "reads_up": (-1.0, 0.0), "reads_down": (1.0, 0.0)}
    if pin1 in directions:
        part.up = directions[pin1]
    # teach the marking readers this board's font: the characters of the typed marking, from the sharpest photo
    # of the part, then read every part again with them
    learned, orientation = 0, None
    for reader, m, body in sorted(_bodies_on(part, result.readers, result.px_per_mm),
                                  key=lambda item: item[1] is None):
        n, up = reader.learn(body, text)
        if n:
            learned += n
            if up is not None and orientation is None:
                orientation = _to_board(m, *up) if m is None else tuple(m[:, :2] @ np.array(up))
            break
    if learned:
        part.notes.append(f"learned {learned} characters of this board's marking font")
        if pin1 in (None, "", "auto") and orientation is not None:
            n_ = math.hypot(*orientation) or 1.0
            part.up = (orientation[0] / n_, orientation[1] / n_)
        for reader, _ in result.readers:
            reader.reread()
        changed = assign_markings([p for p in result.parts if p.side == "top"], result.readers, result.px_per_mm)
        result.notes = [n for n in result.notes if not n.startswith("markings re-read")] + \
            [f"markings re-read with the learned characters: {changed} parts"]
        for other in result.parts:
            if other is not part and other.marking:
                number(other, (result.top if other.side == "top" else result.bottom).pads)
    number(part, side.pads)
    _rebuild_nets(result)
    return part


def _rebuild_nets(result: BoardResult) -> None:
    """Pin numbers changed: rebuild the net lists from the stored pad -> net assignment."""
    from scipy import ndimage
    ids = net_ids(result.connectivity)
    sides = [result.top] + ([result.bottom] if result.bottom else [])
    members: dict[int, list[tuple[str, str]]] = {}
    for side in sides:
        labels = (result.connectivity.top if side.image.side == "top" else result.connectivity.bottom).labels
        parts_by_id = {p.id: p for p in side.parts}
        objects = ndimage.find_objects(side.pad_labels)
        for pad in side.pads.values():
            if pad.part is None or pad.part not in parts_by_id:
                continue
            region = _label_at(labels, pad, side.pad_labels, objects)
            net = ids.get((side.image.side, region)) if region else None
            if net is not None:
                members.setdefault(net, []).append((parts_by_id[pad.part].ref, pad.pin or "?"))
    named = {}
    for net, nodes in sorted(members.items(), key=lambda kv: -len(kv[1])):
        nodes = sorted(set(nodes))
        if len(nodes) >= 2:
            named[f"Net-({nodes[0][0]}-Pad{nodes[0][1]})"] = nodes
    result.nets = named


def _label_at(labels: np.ndarray, pad: Pad, pad_labels: np.ndarray, objects: list) -> int:
    sl = objects[pad.id - 1] if pad.id - 1 < len(objects) else None
    if sl is None:
        return 0
    vals = labels[sl][pad_labels[sl] == pad.id]
    vals = vals[vals > 0]
    return int(np.bincount(vals).argmax()) if vals.size else 0
