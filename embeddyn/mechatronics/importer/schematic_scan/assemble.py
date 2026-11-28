"""Join the pages into one netlist: same-named nets (labels, power ports) are one net across the document."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..kicad_netlist import Component, Net, Netlist, Node
from .connectivity import PageResult


@dataclass
class ScanReport:
    """What was read and how sure it is: per page counts, guessed pin numbers, symbols without a reference."""
    source: Path
    pages: list[dict] = field(default_factory=list)
    guessed_pins: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"source": str(self.source), "pages": self.pages, "guessed_pins": self.guessed_pins,
                "notes": self.notes}


class _Union:
    def __init__(self):
        self.parent: dict = {}

    def find(self, a):
        self.parent.setdefault(a, a)
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def join(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[b] = a


def assemble(results: list[PageResult], source: Path) -> tuple[Netlist, ScanReport]:
    netlist = Netlist(source=source)
    report = ScanReport(source=source)
    union = _Union()
    names: dict = defaultdict(list)          # net key -> [(priority, name)]
    nodes: dict = defaultdict(list)          # net key -> [(ref, pin, pin_name)]

    for r in results:
        p = r.page.index
        # a wire that two different power ports name (GND and +5V) is a misread, not a short: it joins neither
        powers: dict = defaultdict(set)
        for port in r.ports:
            if port.power:
                for c in port.contacts:
                    powers[c.net].add(port.name)
        conflicts = {net for net, found in powers.items() if len(found) > 1}
        for net in conflicts:
            report.notes.append(f"page {p}: one wire carries the power names {', '.join(sorted(powers[net]))};"
                                " left unjoined, check the overlay")
            names[(p, net)].append((-1, "CONFLICT(" + "/".join(sorted(powers[net])) + ")"))
        for port in r.ports:
            for c in port.contacts:
                key = (p, c.net)
                if c.net in conflicts:
                    union.find(key)
                    continue
                union.join(("name", port.name), key)
                names[key].append((0 if port.power else 1, port.name))
        for group, labels in r.labels.items():
            if group in conflicts:
                continue
            for label in labels:
                union.join(("name", label), (p, group))
                names[(p, group)].append((2, label))
        for symbol in r.symbols.values():
            component = netlist.components.get(symbol.ref)
            if component is None:
                component = netlist.components[symbol.ref] = Component(ref=symbol.ref, value=symbol.value,
                                                                       sheet=str(p))
            elif symbol.value and not component.value:
                component.value = symbol.value
            for c in symbol.contacts:
                key = (p, c.net)
                union.find(key)
                nodes[key].append((symbol.ref, c.pin, c.pin_name))
                if c.guessed:
                    report.guessed_pins.append(f"{symbol.ref}.{c.pin}")
        report.pages.append({
            "page": p, "raster": r.page.raster, "segments": len(r.page.segments), "dots": len(r.page.dots),
            "texts": len(r.page.texts), "symbols": len(r.symbols), "ports": len(r.ports),
            "labels": sum(len(v) for v in r.labels.values()),
            "unnamed_symbols": [{"box": [round(v, 1) for v in box], "pins": n} for _, box, n in r.unnamed_clusters],
        })
        report.notes.extend(r.notes)

    merged_nodes: dict = defaultdict(list)
    merged_names: dict = defaultdict(list)
    for key, items in nodes.items():
        merged_nodes[union.find(key)].extend(items)
    for key, items in names.items():
        merged_names[union.find(key)].extend(items)
    for key in list(union.parent):
        if isinstance(key, tuple) and key and key[0] == "name":
            root = union.find(key)
            if not merged_names[root]:
                merged_names[root].append((3, key[1]))

    used_names: set[str] = set()
    for root, items in merged_nodes.items():
        unique = []
        for item in items:
            if item not in unique:
                unique.append(item)
        if not unique:
            continue
        candidates = sorted(merged_names.get(root, []))
        if candidates:
            name = candidates[0][1]
        else:
            ref, pin, _ = sorted(unique)[0]
            name = f"Net-({ref}-Pad{pin})"
        base, n = name, 1
        while name in used_names:
            n += 1
            name = f"{base}_{n}"
        used_names.add(name)
        netlist.nets.append(Net(name=name, nodes=[Node(ref=r, pin=pin, pin_name=pn) for r, pin, pn in unique]))
    netlist.nets.sort(key=lambda net: net.name)
    return netlist, report


def _q(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_kicad_netlist(netlist: Netlist, path: Path, tool: str = "embeddyn schematic_scan") -> Path:
    """A KiCad 6 style s-expression netlist (``(export (version "E") ...)``), readable by ``read_netlist``."""
    lines = ['(export (version "E")', f'  (design (source {_q(str(netlist.source))}) (tool {_q(tool)}))',
             "  (components"]
    for ref in sorted(netlist.components, key=_natural):
        c = netlist.components[ref]
        lines.append(f"    (comp (ref {_q(c.ref)}) (value {_q(c.value or '~')}))")
    lines.append("  )")
    lines.append("  (nets")
    for code, net in enumerate(netlist.nets, 1):
        lines.append(f"    (net (code {_q(str(code))}) (name {_q(net.name)})")
        for node in net.nodes:
            function = f" (pinfunction {_q(node.pin_name)})" if node.pin_name else ""
            lines.append(f"      (node (ref {_q(node.ref)}) (pin {_q(node.pin)}){function})")
        lines.append("    )")
    lines.append("  )")
    lines.append(")")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _natural(ref: str):
    import re
    m = re.fullmatch(r"([A-Za-z#]*)(\d*)(.*)", ref)
    return (m.group(1), int(m.group(2) or 0), m.group(3)) if m else (ref, 0, "")
