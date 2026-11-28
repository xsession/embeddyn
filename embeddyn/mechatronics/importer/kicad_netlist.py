"""Read a KiCad netlist: components (reference, value, footprint, part, fields) and nets (component pins).

Formats: the s-expression netlist of KiCad 5 (``.net``, ``(export (version D) ...)``) and KiCad 6+
(``(export (version "E") ...)``, nodes carry ``pinfunction``), and the XML netlist (``.xml``). For a KiCad 6+
project without an exported netlist, ``export_netlist()`` runs ``kicad-cli sch export netlist``. KiCad 5 nodes
have no pin names: they come from the ``libparts`` section (part pins by number).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Component:
    ref: str
    value: str = ""
    footprint: str = ""
    lib: str = ""
    part: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    sheet: str = ""

    @property
    def mpn(self) -> str:
        """The best manufacturer part number guess: an MPN-like field, else the value."""
        for key in ("MPN", "Mfr. Part #", "Manufacturer_Part_Number", "PartNumber", "Part Number", "MFN"):
            if self.fields.get(key):
                return self.fields[key]
        return self.value


@dataclass
class Node:
    ref: str
    pin: str
    pin_name: str = ""


@dataclass
class Net:
    name: str
    nodes: list[Node] = field(default_factory=list)

    @property
    def short_name(self) -> str:
        return self.name.rsplit("/", 1)[-1] if self.name else ""


@dataclass
class Netlist:
    source: Path
    components: dict[str, Component] = field(default_factory=dict)
    nets: list[Net] = field(default_factory=list)

    def nets_of(self, ref: str) -> dict[str, Net]:
        """Pin number -> net for one component."""
        result: dict[str, Net] = {}
        for net in self.nets:
            for node in net.nodes:
                if node.ref == ref:
                    result[node.pin] = net
        return result

    def pin_names(self, ref: str) -> dict[str, str]:
        return {node.pin: node.pin_name for net in self.nets for node in net.nodes if node.ref == ref}

    def net_of(self, ref: str, pin: str) -> Net | None:
        return self.nets_of(ref).get(pin)


# --- s-expressions ---------------------------------------------------------------------------------------------

_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')


def parse_sexpr(text: str):
    stack: list[list] = [[]]
    position = 0
    while position < len(text):
        m = _TOKEN.match(text, position)
        if not m or m.end() == position:
            break
        position = m.end()
        if m.group(1):
            stack.append([])
        elif m.group(2):
            done = stack.pop()
            stack[-1].append(done)
        elif m.group(3) is not None:
            stack[-1].append(m.group(3).replace('\\"', '"'))
        elif m.group(4) is not None:
            stack[-1].append(m.group(4))
    return stack[0][0] if stack[0] else []


def _children(node, name: str) -> list:
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == name]


def _child(node, name: str):
    found = _children(node, name)
    return found[0] if found else None


def _value(node, name: str, default: str = "") -> str:
    child = _child(node, name)
    if child is None or len(child) < 2:
        return default
    return child[1] if isinstance(child[1], str) else default


def _from_sexpr(tree, source: Path) -> Netlist:
    netlist = Netlist(source=source)
    libpart_pins: dict[tuple[str, str], dict[str, str]] = {}
    libparts = _child(tree, "libparts")
    for libpart in _children(libparts, "libpart") if libparts else []:
        key = (_value(libpart, "lib"), _value(libpart, "part"))
        pins = _child(libpart, "pins")
        libpart_pins[key] = {_value(p, "num"): _value(p, "name") for p in _children(pins, "pin")} if pins else {}
    components = _child(tree, "components")
    for comp in _children(components, "comp") if components else []:
        libsource = _child(comp, "libsource")
        component = Component(
            ref=_value(comp, "ref"), value=_value(comp, "value"), footprint=_value(comp, "footprint"),
            lib=_value(libsource, "lib") if libsource else "", part=_value(libsource, "part") if libsource else "",
        )
        fields = _child(comp, "fields")
        for f in _children(fields, "field") if fields else []:
            name = _value(f, "name")
            text = next((x for x in f[2:] if isinstance(x, str)), "")
            if name:
                component.fields[name] = text
        for prop in _children(comp, "property"):
            name, text = _value(prop, "name"), _value(prop, "value")
            if name and name not in component.fields:
                component.fields[name] = text
        sheet = _child(comp, "sheetpath")
        component.sheet = _value(sheet, "names") if sheet else ""
        netlist.components[component.ref] = component
    nets = _child(tree, "nets")
    for net in _children(nets, "net") if nets else []:
        entry = Net(name=_value(net, "name"))
        for node in _children(net, "node"):
            ref, pin = _value(node, "ref"), _value(node, "pin")
            name = _value(node, "pinfunction")
            if not name:
                comp = netlist.components.get(ref)
                if comp:
                    name = libpart_pins.get((comp.lib, comp.part), {}).get(pin, "")
            entry.nodes.append(Node(ref=ref, pin=pin, pin_name=name))
        netlist.nets.append(entry)
    return netlist


def _from_xml(path: Path) -> Netlist:
    root = ET.parse(path).getroot()
    netlist = Netlist(source=path)
    libpart_pins: dict[tuple[str, str], dict[str, str]] = {}
    for libpart in root.iter("libpart"):
        key = (libpart.get("lib") or "", libpart.get("part") or "")
        libpart_pins[key] = {p.get("num") or "": p.get("name") or "" for p in libpart.iter("pin")}
    for comp in root.iter("comp"):
        libsource = comp.find("libsource")
        component = Component(
            ref=comp.get("ref") or "", value=(comp.findtext("value") or "").strip(),
            footprint=(comp.findtext("footprint") or "").strip(),
            lib=libsource.get("lib") if libsource is not None else "",
            part=libsource.get("part") if libsource is not None else "",
        )
        for f in comp.iter("field"):
            if f.get("name"):
                component.fields[f.get("name")] = (f.text or "").strip()
        netlist.components[component.ref] = component
    for net in root.iter("net"):
        entry = Net(name=net.get("name") or "")
        for node in net.iter("node"):
            ref, pin = node.get("ref") or "", node.get("pin") or ""
            name = node.get("pinfunction") or ""
            if not name and ref in netlist.components:
                comp = netlist.components[ref]
                name = libpart_pins.get((comp.lib, comp.part), {}).get(pin, "")
            entry.nodes.append(Node(ref=ref, pin=pin, pin_name=name))
        netlist.nets.append(entry)
    return netlist


def read_netlist(path: Path) -> Netlist:
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.lstrip().startswith("<"):
        return _from_xml(path)
    tree = parse_sexpr(text)
    if not tree or tree[0] != "export":
        raise ValueError(f"{path}: not a KiCad netlist")
    return _from_sexpr(tree, path)


def export_netlist(schematic: Path, output: Path, kicad_cli: str | None = None) -> Path:
    """Export the netlist of a KiCad 6+ schematic with ``kicad-cli``."""
    executable = kicad_cli or shutil.which("kicad-cli")
    if not executable:
        raise FileNotFoundError("kicad-cli not found: export the netlist from KiCad (File > Export > Netlist)")
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([executable, "sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(output), str(schematic)],
                   check=True, capture_output=True, text=True)
    return output


def find_netlist(root: Path, work: Path | None = None) -> Path | None:
    """The newest exported netlist under ``root``; else export one from a KiCad 6+ root schematic."""
    candidates = [p for p in root.rglob("*") if p.suffix in (".net", ".xml") and p.is_file()]
    netlists = []
    for p in candidates:
        head = p.read_text(encoding="utf-8", errors="replace")[:400]
        if "(export" in head or "<export" in head:
            netlists.append(p)
    if netlists:
        return max(netlists, key=lambda p: (_netlist_date(p), p.stat().st_mtime))
    for project in sorted(root.rglob("*.kicad_pro")):
        schematic = project.with_suffix(".kicad_sch")
        if schematic.exists() and work is not None:
            try:
                return export_netlist(schematic, work / (project.stem + ".net"))
            except (FileNotFoundError, subprocess.CalledProcessError):
                return None
    return None


def _netlist_date(path: Path) -> tuple:
    """The export date written in the netlist (day/month/year or month/day/year guessed), as a sortable tuple."""
    head = path.read_text(encoding="utf-8", errors="replace")[:2000]
    m = re.search(r'(?:\(date "|<date>)(\d+)/(\d+)/(\d{4})', head)
    if not m:
        return (0, 0, 0)
    a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    month, day = (a, b) if a <= 12 and b > 12 else (b, a) if b <= 12 and a > 12 else (a, b)
    return (year, month, day)
