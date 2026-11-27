"""Breadboard view of the Lab: a SPICE netlist laid out as parts on a solderless breadboard.

The Lab's breadboard is a prototype drawer in the spirit of Fritzing. Every
element of an ngspice circuit becomes a part (resistor with colour bands,
capacitor, LED, TO-92/TO-220 transistor, DIP chip for behavioural sources and
subcircuits); its legs sit in breadboard holes. Sources sit next to the board
as modules (bench supply, function generator, MCU pins, signal inputs) and
reach the board through jumper wires.

Connectivity follows the real board: the five holes of a column half (a-e,
f-j) form one strip, each power rail is one conductor, jumper wires join their
two ends. :func:`board_nets` derives the nets from that, :func:`check_board`
compares them with the netlist (opens, shorts, unplaced parts), and
:func:`export_netlist` writes the netlist back with the breadboard's
connectivity and part values, keeping every other line of the file.

The layout is saved next to the netlist as ``<netlist>.breadboard.yaml``::

    version: 1
    netlist: motor-hbridge.cir
    board: {columns: 63}
    parts:
      - {id: R1, type: resistor, ref: R1, value: 10k, pins: [{name: "1", hole: a12}, ...],
         spice: {letter: R, nodes: [in, out], model: null, extra: ""}}
      - {id: Vbus, type: supply, ..., pos: [-14, 2]}          # modules: position left of the board
    wires: [{id: w1, a: a12, b: tp13, color: "#d22"}]

Hole names: ``a1``..``j63`` on the main field; ``tp``/``tn`` (top +/-) and
``bp``/``bn`` (bottom) plus a column for the rails; ``m:<part>:<pin>`` for a
module terminal.
"""
from __future__ import annotations

import copy
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

ROWS_TOP = "abcde"
ROWS_BOTTOM = "fghij"
RAILS = ("tn", "tp", "bp", "bn")
GROUND_NAMES = {"0", "gnd", "gnd!", "ground"}
SUPPLY_NAME = re.compile(r"^(v?cc|v?dd|vbus|vsupply|vsup|supply|vin|vbat|vpwr|pwr|v\+|\+?\d+v\d*|\d+v)$", re.I)
WIRE_COLORS = ["#1f6fd1", "#e3b505", "#1e9e4a", "#e67e22", "#8e44ad", "#16a3b8", "#c0392b", "#7f8c8d",
               "#d35400", "#2c3e50", "#27ae60", "#f39c12"]
RED, BLACK = "#d12b2b", "#222222"
SIDECAR_SUFFIX = ".breadboard.yaml"
_HOLE = re.compile(r"^([a-j])(\d+)$")
_RAIL = re.compile(r"^(tn|tp|bp|bn)(\d+)$")
_SI = {"t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3, "m": 1e-3, "mil": 25.4e-6, "u": 1e-6, "µ": 1e-6,
       "n": 1e-9, "p": 1e-12, "f": 1e-15}


# ---------------------------------------------------------------------------
# Netlist parsing

@dataclass
class Element:
    """One element card of a SPICE netlist."""
    ref: str
    letter: str
    nodes: list[str]
    value: str = ""             # value / source spec / expression (everything after nodes and model)
    model: str | None = None    # model or subcircuit name
    line: int = 0               # first line (0-based) in the file
    span: int = 1               # number of physical lines (continuations)

    @property
    def kind(self) -> str:
        return self.letter


@dataclass
class Netlist:
    title: str
    elements: list[Element]
    models: dict[str, tuple[str, str]] = field(default_factory=dict)   # name -> (type, parameters)
    subckts: dict[str, list[str]] = field(default_factory=dict)        # name -> pin names
    lines: list[str] = field(default_factory=list)

    def element(self, ref: str) -> Element | None:
        return next((e for e in self.elements if e.ref.lower() == ref.lower()), None)

    def nets(self) -> dict[str, list[tuple[str, int]]]:
        """net -> [(element ref, pin index)], ground merged into "0"."""
        result: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for element in self.elements:
            for index, node in enumerate(element.nodes):
                result[canonical_net(node)].append((element.ref, index))
        return dict(result)


def canonical_net(name: str) -> str:
    return "0" if name.lower() in GROUND_NAMES else name


def _cards(text: str) -> list[tuple[int, int, str]]:
    """(first line, line count, joined card) with continuations merged and comments dropped."""
    cards: list[tuple[int, int, str]] = []
    for number, raw in enumerate(text.splitlines()):
        line = raw.rstrip()
        stripped = line.lstrip()
        if not stripped or stripped.startswith("*"):
            continue
        for marker in (" ;", "\t;", " $ ", "\t$ "):
            if marker in line:
                line = line[:line.index(marker)]
        if stripped.startswith(";"):
            continue
        if stripped.startswith("+") and cards:
            first, count, body = cards[-1]
            cards[-1] = (first, number - first + 1, body + " " + stripped[1:].strip())
            continue
        cards.append((number, 1, line.strip()))
    return cards


def _tokens(card: str) -> list[str]:
    """Split a card on whitespace, keeping (...) and {...} groups and `a = b` together."""
    tokens: list[str] = []
    current, depth = "", 0
    for char in card:
        if char in "({":
            depth += 1
        elif char in ")}":
            depth = max(0, depth - 1)
        if char.isspace() and depth == 0:
            if current:
                tokens.append(current)
                current = ""
            continue
        current += char
    if current:
        tokens.append(current)
    return tokens


_NODE_COUNTS = {"R": 2, "C": 2, "L": 2, "D": 2, "V": 2, "I": 2, "B": 2, "E": 4, "G": 4, "F": 2, "H": 2,
                "S": 4, "W": 2, "T": 4, "O": 4, "U": 3, "Z": 3}


def parse_netlist(text: str) -> Netlist:
    lines = text.splitlines()
    cards = _cards(text)
    title = lines[0].lstrip("* ").strip() if lines else ""
    models: dict[str, tuple[str, str]] = {}
    subckts: dict[str, list[str]] = {}
    in_subckt = 0
    in_control = False
    for _, _, card in cards:          # models and subcircuit pins first: Q/M/X node counts depend on them
        low = card.lower()
        if low.startswith(".model"):
            parts = card.split(None, 3)
            if len(parts) >= 3:
                kind = re.split(r"[\s(]", parts[2], maxsplit=1)[0]
                rest = card.split(None, 2)[2][len(kind):].strip() if len(parts) > 2 else ""
                models[parts[1].lower()] = (kind.lower(), rest)
        elif low.startswith(".subckt"):
            parts = card.split()
            if len(parts) >= 2:
                subckts[parts[1].lower()] = [p for p in parts[2:] if "=" not in p and not p.lower().startswith("params:")]
    elements: list[Element] = []
    for first, span, card in cards:
        low = card.lower()
        if low.startswith(".control"):
            in_control = True
            continue
        if in_control:
            in_control = not low.startswith(".endc")
            continue
        if low.startswith(".subckt"):
            in_subckt += 1
            continue
        if low.startswith(".ends"):
            in_subckt = max(0, in_subckt - 1)
            continue
        if in_subckt or card.startswith(".") or card.startswith("{") or not card[0].isalpha():
            continue
        tokens = _tokens(card)
        ref, letter = tokens[0], tokens[0][0].upper()
        args = tokens[1:]
        model = None
        if letter in {"Q", "M", "J", "D", "Z"}:
            limit, default = {"Q": (5, 3), "M": (5, 4), "J": (3, 3), "D": (2, 2), "Z": (3, 3)}[letter]
            index = next((i for i, token in enumerate(args[:limit + 1]) if token.lower() in models), None)
            count = index if index is not None and index >= 2 else min(default, len(args))
            nodes = args[:count]
            rest = args[count:]
            if rest:
                model, rest = rest[0], rest[1:]
            value = " ".join(rest)
        elif letter == "X":
            names = [t for t in args if "=" not in t and not t.lower().startswith("params:")]
            if not names:
                continue
            model = names[-1]
            pins = subckts.get(model.lower())
            count = len(pins) if pins else len(names) - 1
            nodes = args[:count]
            value = " ".join(args[count + 1:])
        elif letter == "K":
            nodes, value = [], " ".join(args)
        else:
            count = _NODE_COUNTS.get(letter, 2)
            nodes = args[:count]
            rest = args[count:]
            if letter in {"F", "H", "W", "S"} and rest:     # controlling source / switch model
                model, rest = rest[0], rest[1:]
            value = " ".join(rest)
        elements.append(Element(ref=ref, letter=letter, nodes=nodes, value=value, model=model,
                                line=first, span=span))
    return Netlist(title=title, elements=elements, models=models, subckts=subckts, lines=lines)


def parse_value(text: str) -> float | None:
    """SPICE number with SI suffix (10k, 4.7u, 1meg); None for expressions."""
    match = re.match(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(meg|mil|[tgkmunpfµ])?", text or "", re.I)
    if not match:
        return None
    rest = text.strip()[match.end():]
    if rest and not re.match(r"^[a-zA-Z]*$", rest):
        return None
    value = float(match.group(1))
    suffix = (match.group(2) or "").lower()
    return value * _SI.get(suffix, 1.0)


# ---------------------------------------------------------------------------
# Part types

def part_type(element: Element, netlist: Netlist) -> str:
    """Breadboard part type of an element (drives the drawing and the palette)."""
    letter, ref = element.letter, element.ref.lower()
    model = (element.model or "").lower()
    mtype, params = netlist.models.get(model, ("", ""))
    value = element.value.lower()
    if letter == "R":
        return "resistor"
    if letter == "C":
        number = parse_value(element.value.split()[0] if element.value else "")
        return "electrolytic" if number is not None and number >= 1e-6 else "capacitor"
    if letter == "L":
        return "inductor"
    if letter == "D":
        if "led" in model or "led" in ref or "ir" == model[:2]:
            return "led"
        if "photo" in model or ref.startswith("dph"):
            return "photodiode"
        return "diode"
    if letter == "Q":
        if "photo" in model or ref.startswith("qph") or "pt" == model[:2]:
            return "phototransistor"
        return "pnp" if mtype == "pnp" else "npn"
    if letter == "M":
        if mtype == "vdmos":
            return "pmos_power" if "pchan" in params.lower() else "nmos_power"
        return "pmos" if mtype == "pmos" else "nmos"
    if letter == "J":
        return "jfet"
    if letter in {"S", "W"}:
        return "switch"
    if letter in {"E", "G", "F", "H"}:
        return "dependent"
    if letter == "B":
        return "behavioral"
    if letter == "X":
        return "ic"
    if letter == "V":
        if "external" in value:
            return "signal"
        if _is_zero_source(value):
            return "ammeter"
        if re.search(r"\b(pulse|sin|pwl|exp|sffm|am|trnoise|trrandom)\s*\(", value):
            return "generator"
        return "supply"
    if letter == "I":
        return "current_signal" if "external" in value else "current_source"
    return "generic"


def _is_zero_source(value: str) -> bool:
    text = value.lower().replace("dc", " ").strip()
    if not text:
        return True
    number = parse_value(text.split()[0])
    return number == 0.0 and len(text.split()) == 1


MODULE_TYPES = {"signal", "generator", "supply", "current_source", "current_signal", "mcu"}
DIP_TYPES = {"dependent", "ic", "switch"}
TRANSISTOR_TYPES = {"npn", "pnp", "nmos", "pmos", "nmos_power", "pmos_power", "jfet", "phototransistor"}
PIN_NAMES = {
    "resistor": ["1", "2"], "capacitor": ["1", "2"], "electrolytic": ["+", "-"], "inductor": ["1", "2"],
    "diode": ["A", "K"], "led": ["A", "K"], "photodiode": ["A", "K"], "ammeter": ["+", "-"],
    "behavioral": ["+", "-"], "npn": ["C", "B", "E", "S"], "pnp": ["C", "B", "E", "S"],
    "phototransistor": ["C", "B", "E", "S"], "nmos": ["D", "G", "S", "B"], "pmos": ["D", "G", "S", "B"],
    "nmos_power": ["D", "G", "S"], "pmos_power": ["D", "G", "S"], "jfet": ["D", "G", "S"],
    "switch": ["1", "2", "C+", "C-"], "dependent": ["+", "-", "C+", "C-"],
    "supply": ["+", "-"], "generator": ["+", "-"], "signal": ["+", "-"],
    "current_source": ["+", "-"], "current_signal": ["+", "-"],
}

# Real leg order of the packages, left to right with the flat (TO-92) or labelled (TO-220) face toward you.
# The breadboard puts the legs in that order so a real part plugged in as drawn is wired as simulated.
PACKAGE_ORDER: dict[str, list[str]] = {
    "npn": ["E", "B", "C"], "pnp": ["E", "B", "C"],          # 2N3904, 2N3906, 2N2222A, PN2222 (TO-92)
    "nmos": ["D", "G", "S"], "pmos": ["D", "G", "S"],        # BS170, BS250 (TO-92)
    "nmos_power": ["G", "D", "S"], "pmos_power": ["G", "D", "S"],   # TO-220 MOSFETs (IRF540, IRF9540...)
    "jfet": ["D", "S", "G"],                                 # J201, 2N5457
}
MODEL_PACKAGE_ORDER: dict[str, list[str]] = {             # parts whose pinout differs from their family's
    "2n7000": ["S", "G", "D"], "n2n7000": ["S", "G", "D"], "2n7002": ["G", "S", "D"],
    "bc547": ["C", "B", "E"], "bc548": ["C", "B", "E"], "bc549": ["C", "B", "E"], "bc557": ["C", "B", "E"],
    "bc558": ["C", "B", "E"], "bc337": ["C", "B", "E"], "bc327": ["C", "B", "E"], "j310": ["G", "S", "D"],
}


def package_order(kind: str, model: str | None, names: list[str]) -> list[str]:
    """The pin names in the package's physical order (pins the package does not have, e.g. a substrate, last)."""
    key = (model or "").lower()
    order = next((v for k, v in MODEL_PACKAGE_ORDER.items() if key == k or key.startswith(k)), None) \
        or PACKAGE_ORDER.get(kind) or names
    return [n for n in order if n in names] + [n for n in names if n not in order]


# Parts the palette can add: type -> (letter, default value, model, .model card or None)
PALETTE: dict[str, dict[str, Any]] = {
    "resistor": {"letter": "R", "value": "10k", "label": "Resistor"},
    "capacitor": {"letter": "C", "value": "100n", "label": "Ceramic capacitor"},
    "electrolytic": {"letter": "C", "value": "100u", "label": "Electrolytic capacitor"},
    "inductor": {"letter": "L", "value": "100u", "label": "Inductor"},
    "diode": {"letter": "D", "model": "D1N4148", "label": "Diode 1N4148",
              "card": ".model D1N4148 D(Is=2.52n Rs=0.568 N=1.752 Cjo=4p M=0.4 tt=20n Bv=100)"},
    "led": {"letter": "D", "model": "LED_RED", "label": "LED (red)",
            "card": ".model LED_RED D(Is=2.3e-18 N=2 Rs=5 Cjo=20p)"},
    "led_ir": {"type": "led", "letter": "D", "model": "LED_IR940", "label": "IR LED 940 nm",
               "card": ".model LED_IR940 D(Is=5.7e-17 N=1.5 Rs=1.5 Cjo=30p)"},
    "photodiode": {"letter": "D", "model": "PHOTO_D", "label": "Photodiode",
                   "card": ".model PHOTO_D D(Is=1e-12 N=1.05 Cjo=10p)"},
    "npn": {"letter": "Q", "model": "Q2N3904", "label": "NPN 2N3904",
            "card": ".model Q2N3904 NPN(Is=6.734f Bf=416.4 Vaf=74.03 Ikf=66.78m Rb=10 Rc=1 Cjc=3.638p Cje=4.493p Tf=301.2p)"},
    "pnp": {"letter": "Q", "model": "Q2N3906", "label": "PNP 2N3906",
            "card": ".model Q2N3906 PNP(Is=1.41f Bf=180.7 Vaf=18.7 Ikf=80m Rb=10 Rc=2.5 Cjc=9.728p Cje=8.063p Tf=411.1p)"},
    "phototransistor": {"letter": "Q", "model": "PT_NPN", "label": "Phototransistor",
                        "card": ".model PT_NPN NPN(Is=1e-14 Bf=500 Vaf=60 Cjc=12p Cje=20p)"},
    "nmos": {"letter": "M", "model": "N2N7000", "label": "N-MOSFET 2N7000",
             "card": ".model N2N7000 VDMOS(Vto=2.1 Kp=0.3 Rd=1 Rs=0.5 Cgdmax=60p Cgdmin=5p Cgs=50p Cjo=20p)"},
    "nmos_power": {"letter": "M", "model": "NPOWER", "label": "Power N-MOSFET (TO-220)",
                   "card": ".model NPOWER VDMOS(Vto=3 Kp=60 Rd=4m Rs=2m Rg=1 Cgs=2n Cgdmax=1.2n Cgdmin=80p Cjo=1n)"},
    "pmos_power": {"letter": "M", "model": "PPOWER", "label": "Power P-MOSFET (TO-220)",
                   "card": ".model PPOWER VDMOS(pchan Vto=-3 Kp=30 Rd=8m Rs=4m Cgs=2n Cgdmax=1.2n Cgdmin=80p Cjo=1n)"},
    "supply": {"letter": "V", "value": "dc 5", "label": "Bench supply"},
    "generator": {"letter": "V", "value": "PULSE(0 3.3 0 10n 10n 0.5m 1m)", "label": "Function generator"},
    "ammeter": {"letter": "V", "value": "dc 0", "label": "Ammeter (0 V sense)"},
}


# ---------------------------------------------------------------------------
# Board geometry and connectivity

def rail_hole_ok(column: int, columns: int) -> bool:
    return 1 <= column <= columns and column % 6 != 0


def hole_bus(hole: str) -> str:
    """The conductor a hole belongs to."""
    match = _HOLE.match(hole)
    if match:
        return f"S{match.group(2)}{'t' if match.group(1) in ROWS_TOP else 'b'}"
    match = _RAIL.match(hole)
    if match:
        return f"R{match.group(1)}"
    return hole


def valid_hole(hole: str, columns: int) -> bool:
    match = _HOLE.match(hole)
    if match:
        return 1 <= int(match.group(2)) <= columns
    match = _RAIL.match(hole)
    if match:
        return rail_hole_ok(int(match.group(2)), columns)
    return hole.startswith("m:")


class _Union:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def join(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def board_nets(board: dict[str, Any]) -> dict[tuple[str, str], str]:
    """(part id, pin name) -> connection group id, from holes, rails and wires."""
    union = _Union()
    columns = int((board.get("board") or {}).get("columns", 63))
    for wire in board.get("wires") or []:
        a, b = str(wire.get("a", "")), str(wire.get("b", ""))
        if valid_hole(a, columns) and valid_hole(b, columns):
            union.join(hole_bus(a), hole_bus(b))
    result: dict[tuple[str, str], str] = {}
    for part in board.get("parts") or []:
        for pin in part.get("pins") or []:
            hole = pin.get("hole")
            if part.get("module"):
                hole = f"m:{part['id']}:{pin['name']}"
            if not hole or not valid_hole(str(hole), columns):
                result[(part["id"], pin["name"])] = f"open:{part['id']}:{pin['name']}"
                continue
            result[(part["id"], pin["name"])] = union.find(hole_bus(str(hole)))
    return result


def name_nets(board: dict[str, Any], groups: dict[tuple[str, str], str] | None = None) -> dict[str, str]:
    """Connection group -> net name: the netlist node most of its pins had, "0" for ground."""
    groups = groups if groups is not None else board_nets(board)
    votes: dict[str, Counter] = defaultdict(Counter)
    for part in board.get("parts") or []:
        nodes = list((part.get("spice") or {}).get("nodes") or [])
        for index, pin in enumerate(part.get("pins") or []):
            group = groups.get((part["id"], pin["name"]))
            if group is None or group.startswith("open:"):
                continue
            if index < len(nodes) and nodes[index]:
                votes[group][canonical_net(str(nodes[index]))] += 1
            else:
                votes[group]  # noqa: B018 - make the group known
    names: dict[str, str] = {}
    used: set[str] = set()
    for group, counter in sorted(votes.items(), key=lambda item: -sum(item[1].values())):
        if "0" in counter:
            name = "0"
        else:
            name = next((n for n, _ in counter.most_common() if n not in used), "")
        if not name:
            index = 1
            while f"N{index:03d}" in used:
                index += 1
            name = f"N{index:03d}"
        names[group] = name
        used.add(name)
    for group in set(groups.values()):
        if group not in names:
            names[group] = group
    return names


def check_board(board: dict[str, Any]) -> dict[str, Any]:
    """Compare the breadboard's connectivity with each part's netlist nodes.

    * ``open``: a pin's netlist node is split over several groups, or the pin is in no hole.
    * ``short``: one group joins pins of different netlist nodes.
    """
    groups = board_nets(board)
    names = name_nets(board, groups)
    expected: dict[str, set[str]] = defaultdict(set)        # netlist node -> groups
    members: dict[str, set[str]] = defaultdict(set)         # group -> netlist nodes
    issues: list[dict[str, Any]] = []
    for part in board.get("parts") or []:
        nodes = list((part.get("spice") or {}).get("nodes") or [])
        for index, pin in enumerate(part.get("pins") or []):
            group = groups[(part["id"], pin["name"])]
            if group.startswith("open:"):
                issues.append({"kind": "unplaced", "part": part["id"], "pin": pin["name"],
                               "text": f"{part['id']}.{pin['name']} is not in a hole"})
                continue
            if index < len(nodes) and nodes[index] and not part.get("added"):
                node = canonical_net(str(nodes[index]))
                expected[node].add(group)
                members[group].add(node)
    for node, found in sorted(expected.items()):
        if len(found) > 1:
            issues.append({"kind": "open", "net": node,
                           "text": f"net {node} is split into {len(found)} unconnected pieces"})
    for group, nodes in sorted(members.items()):
        if len(nodes) > 1:
            issues.append({"kind": "short", "nets": sorted(nodes), "group": group,
                           "text": f"short: {' + '.join(sorted(nodes))} are connected"})
    return {"ok": not any(i["kind"] in {"open", "short"} for i in issues), "issues": issues,
            "nets": {f"{part_id}.{pin}": names[group] for (part_id, pin), group in groups.items()}}


# ---------------------------------------------------------------------------
# Automatic layout

class _Layout:
    def __init__(self, netlist: Netlist, supply: str | None) -> None:
        self.netlist = netlist
        self.supply = supply
        self.strips: dict[tuple[int, str], str] = {}                  # (column, half) -> net
        self.used: set[str] = set()                                   # holes taken
        self.net_strips: dict[str, list[tuple[int, str]]] = defaultdict(list)
        self.wires: list[dict[str, Any]] = []
        self.color: dict[str, str] = {}
        self.cursor = 2
        self.tall: set[int] = set()                                   # top-half columns under a TO-92/TO-220 body

    # -- nets ----------------------------------------------------------------
    def rail(self, net: str) -> str | None:
        if net == "0":
            return "n"
        if self.supply and net == self.supply:
            return "p"
        return None

    def net_color(self, net: str) -> str:
        if net == "0":
            return BLACK
        if net == self.supply:
            return RED
        if net not in self.color:
            self.color[net] = WIRE_COLORS[len(self.color) % len(WIRE_COLORS)]
        return self.color[net]

    def wire(self, a: str, b: str, net: str) -> None:
        self.used.update({a, b} - {h for h in (a, b) if h.startswith("m:")})
        self.wires.append({"id": f"w{len(self.wires) + 1}", "a": a, "b": b, "color": self.net_color(net)})

    # -- strips ----------------------------------------------------------------
    def free_rows(self, column: int, half: str) -> list[str]:
        rows = ROWS_TOP[::-1] if half == "t" else ROWS_BOTTOM    # nearest the trench first
        return [r for r in rows if f"{r}{column}" not in self.used]

    def new_strip(self, net: str, half: str, start: int | None = None) -> tuple[int, str]:
        column = max(2, start if start is not None else self.cursor)
        while (column, half) in self.strips:
            column += 1
        self.strips[(column, half)] = net
        self.net_strips[net].append((column, half))
        if start is None or column >= self.cursor:
            self.cursor = max(self.cursor, column + 2)          # a free column between nets
        self.connect_strip(net, (column, half))
        return column, half

    def connect_strip(self, net: str, strip: tuple[int, str]) -> None:
        """A new strip of a net is joined to the rail or to the net's first strip."""
        column, half = strip
        rail = self.rail(net)
        if rail:
            rail_name = ("t" if half == "t" else "b") + rail
            outer = "a" if half == "t" else "j"
            # strip holes nearest the rail: a (top) / j (bottom)
            hole = f"{outer}{column}" if f"{outer}{column}" not in self.used else \
                f"{self.free_rows(column, half)[-1]}{column}"
            self.wire(hole, self.rail_hole(rail_name, column), net)
            return
        others = [s for s in self.net_strips[net] if s != strip and self.free_rows(*s)]
        if len(self.net_strips[net]) > 1 and others:
            self.wire(self.take(others[0], outer_first=True), self.take(strip, outer_first=True), net)

    def take(self, strip: tuple[int, str], outer_first: bool = False, row: str | None = None) -> str:
        column, half = strip
        rows = self.free_rows(column, half)
        if row and row in rows:
            choice = row
        elif not rows:
            raise _StripFull(strip)
        else:
            choice = rows[-1] if outer_first else rows[0]
        hole = f"{choice}{column}"
        self.used.add(hole)
        return hole

    def rail_hole(self, rail: str, column: int, columns: int = 10_000) -> str:
        for offset in range(0, 200):
            for candidate in (column + offset, column - offset):
                hole = f"{rail}{candidate}"
                if candidate >= 1 and rail_hole_ok(candidate, columns) and hole not in self.used:
                    self.used.add(hole)
                    return hole
        raise RuntimeError("no free rail hole")

    def hole_for(self, net: str, half: str, near: int, row: str | None = None) -> str:
        """A free hole on `net` close to column `near` in `half` (rail hole for rail nets)."""
        rail = self.rail(net)
        if rail:
            return self.rail_hole(("t" if half == "t" else "b") + rail, near)
        candidates = [s for s in self.net_strips.get(net, []) if s[1] == half and self.free_rows(*s)
                      and abs(s[0] - near) <= 6]
        candidates.sort(key=lambda s: abs(s[0] - near))
        for strip in candidates:
            try:
                return self.take(strip, row=row)
            except _StripFull:
                continue
        strip = self.new_strip(net, half, start=near)
        return self.take(strip, row=row)


class _StripFull(Exception):
    pass


def choose_supply(netlist: Netlist) -> str | None:
    nets = netlist.nets()
    named = [n for n in nets if SUPPLY_NAME.match(n)]
    if named:
        return max(named, key=lambda n: len(nets[n]))
    candidates = []
    for element in netlist.elements:
        if element.letter == "V" and len(element.nodes) == 2 and canonical_net(element.nodes[1]) == "0":
            kind = part_type(element, netlist)
            if kind == "supply":
                candidates.append(canonical_net(element.nodes[0]))
    candidates = [c for c in candidates if c != "0"]
    return max(candidates, key=lambda n: len(nets.get(n, [])), default=None)


def _order(netlist: Netlist, modules: list[Element]) -> list[Element]:
    """On-board elements in signal-flow order: breadth first from the nets the modules drive."""
    on_board = [e for e in netlist.elements if e not in modules and e.nodes]
    by_net: dict[str, list[Element]] = defaultdict(list)
    for element in on_board:
        for node in element.nodes:
            by_net[canonical_net(node)].append(element)
    seen: list[Element] = []
    queue = [canonical_net(n) for m in modules for n in m.nodes if canonical_net(n) != "0"]
    visited_nets: set[str] = set()
    while queue or len(seen) < len(on_board):
        if not queue:
            rest = next(e for e in on_board if e not in seen)
            queue = [canonical_net(n) for n in rest.nodes]
        net = queue.pop(0)
        if net in visited_nets:
            continue
        visited_nets.add(net)
        for element in by_net.get(net, []):
            if element in seen:
                continue
            seen.append(element)
            queue.extend(canonical_net(n) for n in element.nodes if canonical_net(n) not in visited_nets
                         and canonical_net(n) != "0")
    return seen


def auto_layout(netlist: Netlist, name: str = "", bindings: dict[str, Any] | None = None) -> dict[str, Any]:
    """A breadboard document for a netlist: parts in holes, modules beside the board, jumper wires."""
    supply = choose_supply(netlist)
    layout = _Layout(netlist, supply)
    module_elements = [e for e in netlist.elements if part_type(e, netlist) in MODULE_TYPES]
    parts: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []

    def base_part(element: Element, kind: str) -> dict[str, Any]:
        return {"id": element.ref, "ref": element.ref, "type": kind, "value": element.value,
                "spice": {"letter": element.letter, "nodes": list(element.nodes), "model": element.model,
                          "value": element.value}}

    for element in _order(netlist, module_elements):
        kind = part_type(element, netlist)
        nets = [canonical_net(n) for n in element.nodes]
        names = _pin_names(kind, len(nets))
        part = base_part(element, kind)
        if element.letter == "K" or kind == "generic" or not nets:
            part["pins"] = []
            unplaced.append(part)
            continue
        if kind in TRANSISTOR_TYPES:
            half = "t"
            start = layout.cursor
            holes = []
            physical = package_order(kind, element.model, names[:len(nets)])
            part["pinout"] = " ".join(physical)
            spice_nets = dict(zip(names, nets))
            nets = [spice_nets[n] for n in physical]
            names = physical
            for offset, net in enumerate(nets):
                strip = (start + offset, half)
                while strip in layout.strips:
                    start += 1
                    strip = (start + offset, half)
                holes.append(strip)
            for (column, _), net in zip(holes, nets):
                layout.strips[(column, half)] = net
                layout.net_strips[net].append((column, half))
            layout.cursor = max(layout.cursor, holes[-1][0] + 2)
            layout.tall.update(range(holes[0][0] - 1, holes[-1][0] + 2))
            placed = {}
            for (column, _), net, pin in zip(holes, nets, names):
                placed[pin] = layout.take((column, half), row="e")
                layout.connect_strip(net, (column, half))
            spice_order = _pin_names(kind, len(nets))
            part["pins"] = [{"name": pin, "hole": placed[pin]} for pin in spice_order]   # SPICE order: pin i = node i
        elif kind in DIP_TYPES:
            count = len(nets) + (len(nets) % 2)
            width = count // 2
            start = layout.cursor
            while any((start + i, h) in layout.strips for i in range(width) for h in "tb"):
                start += 1
            layout.cursor = start + width + 1
            pins = []
            for index, (net, pin) in enumerate(zip(nets, names)):
                # DIP numbering: pins 1..n/2 along the bottom row (f), then back along the top row (e)
                if index < width:
                    column, half, row = start + index, "b", "f"
                else:
                    column, half, row = start + (count - 1 - index), "t", "e"
                layout.strips[(column, half)] = net
                layout.net_strips[net].append((column, half))
                pins.append({"name": pin, "hole": layout.take((column, half), row=row)})
                layout.connect_strip(net, (column, half))
            part["pins"] = pins
            part["dip"] = count
        else:                                                 # two-terminal parts
            hole_a, hole_b = _place_two(layout, nets[0], nets[1])
            part["pins"] = [{"name": names[0], "hole": hole_a}, {"name": names[1], "hole": hole_b}]
        parts.append(part)

    columns = max(30, layout.cursor + 3)
    columns = 30 if columns <= 30 else 63 if columns <= 63 else ((columns + 9) // 10) * 10
    # Modules: stacked left of the board, terminals wired to their nets.
    y = 0.0
    for element in module_elements:
        kind = part_type(element, netlist)
        nets = [canonical_net(n) for n in element.nodes]
        names = _pin_names(kind, len(nets))
        part = base_part(element, kind)
        part["module"] = True
        part["pins"] = [{"name": pin} for pin in names]
        part["pos"] = [-15.0, y]
        y += 5.5
        for net, pin in zip(nets, names):
            near = layout.net_strips[net][0][0] if layout.net_strips.get(net) else 2
            half = layout.net_strips[net][0][1] if layout.net_strips.get(net) else "t"
            if layout.rail(net):
                hole = layout.rail_hole("t" + layout.rail(net), 1, columns)
            else:
                try:
                    hole = layout.hole_for(net, half, near)
                except _StripFull:
                    hole = layout.take(layout.new_strip(net, half))
            layout.wire(f"m:{part['id']}:{pin}", hole, net)
        parts.append(part)
    columns = max(columns, layout.cursor + 1)
    columns = 30 if columns <= 30 else 63 if columns <= 63 else ((columns + 9) // 10) * 10
    # Bridge the top and bottom rails at the right end, as on a real board.
    end = max(c for c in range(1, columns + 1) if rail_hole_ok(c, columns))
    for rail in ("p", "n"):
        nets_on_rail = [n for n in (supply, "0") if n and layout.rail(n) == rail]
        if nets_on_rail and any(h.startswith("b" + rail) for h in layout.used):
            layout.wire(layout.rail_hole("t" + rail, end, columns), layout.rail_hole("b" + rail, end, columns),
                        nets_on_rail[0])
    return {"version": 1, "name": name or netlist.title, "board": {"columns": columns},
            "supply": supply, "parts": parts + unplaced, "wires": layout.wires}


def _place_two(layout: _Layout, a: str, b: str) -> tuple[str, str]:
    """Holes for a two-terminal part between nets a and b."""
    rail_a, rail_b = layout.rail(a), layout.rail(b)
    if a == b or (rail_a and rail_b):
        column = layout.cursor
        layout.cursor += 2
        return layout.hole_for(a, "t", column), layout.hole_for(b, "t", column + 1)
    if rail_a or rail_b:                                     # strip -> rail: a short vertical part
        net, rail_net = (b, a) if rail_a else (a, b)
        strips = [s for s in layout.net_strips.get(net, []) if layout.free_rows(*s)]
        strip = strips[0] if strips else layout.new_strip(net, "t")
        column, half = strip
        rows = layout.free_rows(column, half)
        hole = layout.take(strip, row=rows[-1])            # the row nearest the rail
        rail_hole = layout.hole_for(rail_net, half, column)
        return (rail_hole, hole) if rail_a else (hole, rail_hole)
    strips_a = [s for s in layout.net_strips.get(a, []) if layout.free_rows(*s)]
    strips_b = [s for s in layout.net_strips.get(b, []) if layout.free_rows(*s)]
    if not strips_a and not strips_b:
        sa = layout.new_strip(a, "t")
        sb = layout.new_strip(b, "t", start=sa[0] + 3)
        row = _common_row(layout, sa, sb)
        return layout.take(sa, row=row), layout.take(sb, row=row)
    if strips_a and strips_b:                               # both exist: bridge the closest pair
        sa, sb = min(((x, y) for x in strips_a for y in strips_b if x[1] == y[1] and x[0] != y[0]),
                     key=lambda pair: abs(pair[0][0] - pair[1][0]), default=(strips_a[0], None))
        if sb is not None and abs(sa[0] - sb[0]) <= 4:
            row = _common_row(layout, sa, sb)
            return layout.take(sa, row=row), layout.take(sb, row=row)
        sb = layout.new_strip(b, sa[1], start=sa[0] + 2)
        row = _common_row(layout, sa, sb)
        return layout.take(sa, row=row), layout.take(sb, row=row)
    anchor, other = (a, b) if strips_a else (b, a)
    sa = (strips_a or strips_b)[0]
    sb = layout.new_strip(other, sa[1], start=sa[0] + 3)
    row = _common_row(layout, sa, sb)
    ha, hb = layout.take(sa, row=row), layout.take(sb, row=row)
    return (ha, hb) if anchor == a else (hb, ha)


def _pin_names(kind: str, count: int) -> list[str]:
    names = list(PIN_NAMES.get(kind, []))[:count]
    while len(names) < count:
        names.append(str(len(names) + 1))
    return names


def _common_row(layout: _Layout, a: tuple[int, str], b: tuple[int, str]) -> str | None:
    """A row free in both strips; outer rows when a transistor body stands between them."""
    rows_b = set(layout.free_rows(*b))
    rows = layout.free_rows(*a)
    low, high = sorted((a[0], b[0]))
    if a[1] == "t" and any(c in layout.tall for c in range(low, high + 1)):
        rows = [r for r in "ba" if r in rows] + [r for r in rows if r not in "ba"]
    return next((r for r in rows if r in rows_b), None)


# ---------------------------------------------------------------------------
# Export

def _format_card(part: dict[str, Any], nodes: list[str]) -> str:
    spice = part.get("spice") or {}
    letter = spice.get("letter") or PALETTE.get(part["type"], {}).get("letter", "X")
    ref = part.get("ref") or part["id"]
    if not ref.upper().startswith(letter.upper()):
        ref = letter + ref
    model = spice.get("model")
    value = str(part.get("value", spice.get("value", "")) or "")
    tokens = [ref, *nodes]
    if model:
        tokens.append(str(model))
    if value:
        tokens.append(value)
    return " ".join(tokens)


def export_netlist(board: dict[str, Any], original: str | None = None) -> str:
    """The netlist with the breadboard's connectivity and values.

    Element cards of parts on the board are rewritten (continuation lines
    folded into one); added parts and the model cards they need are appended
    before ``.end``; removed parts are commented out. Every other line of the
    original file is kept.
    """
    groups = board_nets(board)
    names = name_nets(board, groups)
    cards: dict[str, str] = {}
    for part in board.get("parts") or []:
        if not part.get("pins"):
            continue
        nodes = []
        for pin in part["pins"]:
            group = groups[(part["id"], pin["name"])]
            nodes.append(names.get(group, group) if not group.startswith("open:")
                         else f"nc_{part['id']}_{pin['name']}".replace("+", "p").replace("-", "n"))
        cards[(part.get("ref") or part["id"]).lower()] = _format_card(part, nodes)
    original = original or f"* {board.get('name', 'breadboard')}\n.end\n"
    netlist = parse_netlist(original)
    lines = list(netlist.lines)
    known = {e.ref.lower() for e in netlist.elements}
    for element in sorted(netlist.elements, key=lambda e: -e.line):
        key = element.ref.lower()
        if element.letter == "K" or (element.nodes and key not in cards and
                                     not any((p.get("ref") or p["id"]).lower() == key for p in board.get("parts") or [])):
            if element.letter != "K":
                lines[element.line:element.line + element.span] = [
                    "* removed on the breadboard: " + " ".join(lines[element.line:element.line + element.span])]
            continue
        if key in cards:
            lines[element.line:element.line + element.span] = [cards[key]]
    added = [card for key, card in cards.items() if key not in known]
    models = {m.lower() for m in netlist.models}
    model_cards = []
    for part in board.get("parts") or []:
        if (part.get("ref") or part["id"]).lower() in known:
            continue
        palette = PALETTE.get(part.get("palette", part["type"]), {})
        model = (part.get("spice") or {}).get("model")
        if palette.get("card") and model and model.lower() not in models:
            model_cards.append(palette["card"])
            models.add(model.lower())
    if added or model_cards:
        end = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip().lower() == ".end"), len(lines))
        lines[end:end] = ["", "* added on the breadboard", *added, *model_cards]
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Project integration

def sidecar_path(netlist: Path) -> Path:
    return netlist.with_name(netlist.name + SIDECAR_SUFFIX)


def load_board(netlist_path: Path) -> dict[str, Any]:
    """The saved breadboard of a netlist, or a fresh automatic layout."""
    netlist_path = Path(netlist_path)
    text = netlist_path.read_text(encoding="utf-8")
    netlist = parse_netlist(text)
    sidecar = sidecar_path(netlist_path)
    if sidecar.exists():
        board = yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {}
        board.setdefault("board", {"columns": 63})
        board.setdefault("wires", [])
        board.setdefault("parts", [])
        board["saved"] = True
        return board
    board = auto_layout(netlist, name=netlist_path.stem)
    board["saved"] = False
    return board


def save_board(netlist_path: Path, board: dict[str, Any]) -> Path:
    sidecar = sidecar_path(Path(netlist_path))
    clean = {k: copy.deepcopy(v) for k, v in board.items() if k not in {"saved", "check", "path", "sidecar"}}
    clean["netlist"] = Path(netlist_path).name
    sidecar.write_text(yaml.safe_dump(clean, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")
    return sidecar


def circuit_instances(graph: dict[str, Any]) -> list[dict[str, Any]]:
    """ngspice instances of a Lab graph with their netlist, bindings and where each bound signal goes."""
    root = Path(graph.get("root") or ".")
    producers: dict[str, str] = {}
    consumers: dict[str, list[str]] = defaultdict(list)
    for node in graph.get("nodes") or []:
        for port in node.get("ports") or []:
            if port.get("signal"):
                if port["direction"] == "out":
                    producers.setdefault(port["signal"], node["id"])
                else:
                    consumers[port["signal"]].append(node["id"])
    couplings = {w.get("target_signal"): w.get("signal") for w in graph.get("wires") or [] if w.get("kind") == "coupling"}
    downstream: dict[str, list[str]] = defaultdict(list)
    for w in graph.get("wires") or []:
        if w.get("kind") == "coupling":
            downstream[w.get("signal")].append(w.get("target_signal"))
    domains = {n["id"]: n.get("domain") or n.get("kind") for n in graph.get("nodes") or []}
    result = []
    for node in graph.get("nodes") or []:
        spec = node.get("spec") or {}
        options = spec.get("options") or {}
        if node.get("kind") != "instance" or options.get("backend") != "ngspice-shared" or not spec.get("model"):
            continue
        netlist_path = (root / str(spec["model"])).resolve()
        inputs, outputs = [], []
        bindings_file = options.get("bindings")
        try:
            from ..adapters.ngspice_shared import load_ngspice_bindings
            file = (root / str(bindings_file)).resolve() if bindings_file else None
            bindings = load_ngspice_bindings(file if file and file.exists() else None,
                                             list(options.get("inline_bindings") or []))
            for b in bindings.inputs:
                origin_signal = couplings.get(b.signal)
                origin = producers.get(origin_signal or "") if origin_signal else producers.get(b.signal)
                inputs.append({"signal": b.signal, "kind": b.kind, "target": b.target, "scale": b.scale,
                               "offset": b.offset, "from_signal": origin_signal, "from_node": origin,
                               "from_domain": domains.get(origin) if origin else None})
            for b in bindings.outputs:
                targets = downstream.get(b.signal, [])
                readers = sorted({c for t in targets for c in consumers.get(t, [])} |
                                 {c for c in consumers.get(b.signal, []) if c != node["id"]})
                outputs.append({"signal": b.signal, "vector": b.vector, "mode": b.mode, "scale": b.scale,
                                "offset": b.offset, "to_signals": targets, "to_nodes": readers})
        except (OSError, ValueError):
            pass
        result.append({"instance": node["id"], "label": node.get("label", node["id"]),
                       "netlist": str(netlist_path), "relative": str(spec["model"]),
                       "exists": netlist_path.exists(), "inputs": inputs, "outputs": outputs})
    return result


def open_board(netlist_path: Path) -> dict[str, Any]:
    """Board, check and netlist text for the Lab."""
    netlist_path = Path(netlist_path).resolve()
    text = netlist_path.read_text(encoding="utf-8")
    board = load_board(netlist_path)
    netlist = parse_netlist(text)
    return {"board": board, "check": check_board(board), "netlist": text,
            "models": {k: v[0] for k, v in netlist.models.items()},
            "path": str(netlist_path), "sidecar": str(sidecar_path(netlist_path))}


def palette() -> list[dict[str, Any]]:
    items = []
    for key, spec in PALETTE.items():
        kind = spec.get("type", key)
        item = {"id": key, "type": kind, "label": spec["label"], "letter": spec["letter"],
                "value": spec.get("value", ""), "model": spec.get("model")}
        if kind in TRANSISTOR_TYPES:
            item["pinout"] = package_order(kind, spec.get("model"), PIN_NAMES[kind][:3])
        items.append(item)
    return items


def iter_holes(columns: int) -> Iterable[str]:
    for column in range(1, columns + 1):
        for row in ROWS_TOP + ROWS_BOTTOM:
            yield f"{row}{column}"
        for rail in RAILS:
            if rail_hole_ok(column, columns):
                yield f"{rail}{column}"
