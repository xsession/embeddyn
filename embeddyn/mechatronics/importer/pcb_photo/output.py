"""Results of a board read: KiCad netlist, JSON report, overlay pictures and a draft schematic (SVG).

The draft schematic draws every part as a box with its pins and writes the net name at each pin end (a
net-label schematic): a starting point to redraw in an EDA tool, not a finished drawing.
"""
from __future__ import annotations

import colorsys
import html
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from ..kicad_netlist import Component, Net, Netlist, Node
from ..schematic_scan.assemble import write_kicad_netlist
from .nets import net_ids
from .reader import BoardResult


def netlist(result: BoardResult, source: Path) -> Netlist:
    out = Netlist(source=source)
    pin_names: dict[tuple[str, str], str] = {}
    for part in result.parts:
        out.components[part.ref] = Component(ref=part.ref, value=part.value or part.part_number or "",
                                             footprint=part.package or "",
                                             fields={k: v for k, v in (("Marking", part.marking.replace("\n", " ")),
                                                                       ("Kind", part.kind)) if v})
    for name, nodes in result.nets.items():
        out.nets.append(Net(name=name, nodes=[Node(ref=r, pin=p, pin_name=pin_names.get((r, p), ""))
                                              for r, p in nodes]))
    return out


def report(result: BoardResult) -> dict:
    sides = [result.top] + ([result.bottom] if result.bottom else [])
    parts = []
    for side in sides:
        for part in side.parts:
            pads = [side.pads[p] for p in part.pads]
            parts.append({
                "ref": part.ref, "side": part.side, "kind": part.kind, "tone": part.tone,
                "package": part.package, "package_candidates": part.candidates,
                "size_mm": [round(part.length / result.px_per_mm, 2), round(part.width / result.px_per_mm, 2)],
                "centre_mm": [round(part.cx / result.px_per_mm, 2), round(part.cy / result.px_per_mm, 2)],
                "angle_deg": round(math.degrees(part.angle), 1), "marking": part.marking,
                "marking_confidence": part.marking_conf, "value": part.value, "part_number": part.part_number,
                "readings": part.decoded,
                "pins": [{"pin": p.pin, "guessed": p.guessed, "at_mm": [round(p.x / result.px_per_mm, 2),
                                                                         round(p.y / result.px_per_mm, 2)]}
                         for p in pads],
                "notes": part.notes,
            })
    return {"px_per_mm": round(result.px_per_mm, 3),
            "board_mm": [round(v, 1) for v in result.top.image.size_mm],
            "parts": parts, "nets": {k: [f"{r}.{p}" for r, p in v] for k, v in result.nets.items()},
            "vias": {"top": len(result.connectivity.top.vias),
                     "bottom": len(result.connectivity.bottom.vias) if result.connectivity.bottom else 0,
                     "joins": len(result.connectivity.joins)},
            "closeups": result.closeups, "notes": result.notes + result.connectivity.notes}


def _colour(k: int) -> tuple[int, int, int]:
    h = (k * 0.6180339887) % 1.0
    r, g, b = colorsys.hsv_to_rgb(h, 0.85, 1.0)
    return int(r * 255), int(g * 255), int(b * 255)


def overlay(result: BoardResult, side: str, path: Path, scale: float = 0.5) -> Path:
    res = result.top if side == "top" else result.bottom
    conn_side = result.connectivity.top if side == "top" else result.connectivity.bottom
    if res is None or conn_side is None:
        raise ValueError(f"no {side} side")
    ids = net_ids(result.connectivity)
    # colour copper by net; nets with part pins get colours, the rest grey
    used = set()
    pad_net: dict[int, int] = {}
    for pad in res.pads.values():
        if pad.part is None:
            continue
        x, y = int(round(pad.x)), int(round(pad.y))
        if 0 <= y < conn_side.labels.shape[0] and 0 <= x < conn_side.labels.shape[1]:
            region = conn_side.labels[y, x]
            if region:
                used.add(ids.get((side, int(region)), 0))
    lut = np.zeros((conn_side.count + 1, 3), np.uint8)
    for region in range(1, conn_side.count + 1):
        net = ids.get((side, region), 0)
        lut[region] = _colour(net) if net in used else (90, 90, 90)
    base = (res.balanced.astype(np.float32) * 0.45).astype(np.uint8)
    painted = lut[conn_side.labels]
    sel = conn_side.labels > 0
    base[sel] = (0.35 * base[sel] + 0.65 * painted[sel]).astype(np.uint8)
    image = Image.fromarray(base)
    if scale != 1.0:
        image = image.resize((int(image.width * scale), int(image.height * scale)), Image.BILINEAR)
    d = ImageDraw.Draw(image)
    for part in res.parts:
        pts = [(x * scale, y * scale) for x, y in part.corners()]
        d.polygon(pts, outline=(255, 255, 255) if part.marking else (200, 200, 200), width=2)
        label = part.ref + (f" {part.value}" if part.value else "")
        d.text((pts[0][0], pts[0][1] - 11), label, fill=(255, 255, 0))
        for pid in part.pads:
            pad = res.pads[pid]
            r = 3
            colour = (255, 80, 80) if pad.guessed else (80, 255, 80)
            d.ellipse((pad.x * scale - r, pad.y * scale - r, pad.x * scale + r, pad.y * scale + r), fill=colour)
    for x, y in conn_side.vias:
        d.ellipse((x * scale - 2, y * scale - 2, x * scale + 2, y * scale + 2), outline=(0, 255, 255))
    image.save(path)
    return path


def schematic_svg(result: BoardResult, path: Path) -> Path:
    """A net-label schematic: parts in rows (ICs first), pins on the box sides, the net name at each pin."""
    pin_nets: dict[tuple[str, str], str] = {}
    for name, nodes in result.nets.items():
        for ref, pin in nodes:
            pin_nets[(ref, pin)] = name
    order = {"U": 0, "Q": 1, "D": 2, "LED": 2, "Y": 3, "L": 4, "R": 5, "C": 6}
    parts = sorted([p for p in result.parts if p.pads],
                   key=lambda p: (order.get("".join(ch for ch in p.ref if ch.isalpha()), 9), _num(p.ref)))
    boxes = []
    x, y, row_h = 40, 60, 0
    width = 1600
    for part in parts:
        side = result.top if part.side == "top" else result.bottom
        pins = sorted([side.pads[p] for p in part.pads], key=lambda p: _num(p.pin))
        n = len(pins)
        left = pins[: (n + 1) // 2] if n > 2 else pins[:1]
        right = pins[(n + 1) // 2:] if n > 2 else pins[1:]
        h = 30 + 20 * max(len(left), len(right))
        w = 120
        label_w = 150
        if x + w + 2 * label_w > width:
            x, y, row_h = 40, y + row_h + 50, 0
        boxes.append((part, x + label_w, y, w, h, left, right))
        x += w + 2 * label_w + 20
        row_h = max(row_h, h)
    height = y + row_h + 60
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}" font-family="monospace" font-size="11">',
           '<rect width="100%" height="100%" fill="#fbfbf7"/>',
           '<text x="20" y="28" font-size="15" fill="#1d2b3a">Draft schematic from board photos: net labels at each '
           'pin (red: pin number guessed)</text>']
    for part, bx, by, w, h, left, right in boxes:
        out.append(f'<rect x="{bx}" y="{by}" width="{w}" height="{h}" fill="#fff" stroke="#1d2b3a"/>')
        out.append(f'<text x="{bx + 4}" y="{by + 14}" font-weight="bold">{html.escape(part.ref)}</text>')
        sub = part.value or part.package
        if sub:
            out.append(f'<text x="{bx + 4}" y="{by + h - 6}" fill="#555">{html.escape(sub[:16])}</text>')
        for k, pad in enumerate(left):
            py = by + 30 + 20 * k
            net = pin_nets.get((part.ref, pad.pin), "")
            colour = "#c0392b" if pad.guessed else "#1d2b3a"
            out.append(f'<line x1="{bx - 30}" y1="{py}" x2="{bx}" y2="{py}" stroke="#1d2b3a"/>')
            out.append(f'<text x="{bx - 28}" y="{py - 3}" fill="{colour}">{html.escape(pad.pin)}</text>')
            if net:
                out.append(f'<text x="{bx - 34}" y="{py + 4}" text-anchor="end" fill="#1f6fb2">'
                           f'{html.escape(net[:22])}</text>')
        for k, pad in enumerate(right):
            py = by + 30 + 20 * k
            net = pin_nets.get((part.ref, pad.pin), "")
            colour = "#c0392b" if pad.guessed else "#1d2b3a"
            out.append(f'<line x1="{bx + w}" y1="{py}" x2="{bx + w + 30}" y2="{py}" stroke="#1d2b3a"/>')
            out.append(f'<text x="{bx + w + 4}" y="{py - 3}" fill="{colour}">{html.escape(pad.pin)}</text>')
            if net:
                out.append(f'<text x="{bx + w + 34}" y="{py + 4}" fill="#1f6fb2">{html.escape(net[:22])}</text>')
    out.append("</svg>")
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def _num(text: str) -> tuple:
    digits = "".join(ch for ch in text if ch.isdigit())
    return (int(digits) if digits else 0, text)


def write_all(result: BoardResult, out: Path, source: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    files = {"netlist": write_kicad_netlist(netlist(result, source), out / "board.net")}
    (out / "board.json").write_text(json.dumps(report(result), indent=2) + "\n", encoding="utf-8")
    files["report"] = out / "board.json"
    files["overlay_top"] = overlay(result, "top", out / "overlay-top.jpg")
    if result.bottom is not None:
        files["overlay_bottom"] = overlay(result, "bottom", out / "overlay-bottom.jpg")
    files["schematic"] = schematic_svg(result, out / "schematic.svg")
    Image.fromarray(result.top.balanced).save(out / "top.jpg", quality=90)
    files["top"] = out / "top.jpg"
    if result.bottom is not None:
        Image.fromarray(result.bottom.balanced).save(out / "bottom.jpg", quality=90)
        files["bottom"] = out / "bottom.jpg"
    return files
