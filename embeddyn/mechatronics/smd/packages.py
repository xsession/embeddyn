"""SMD packages: nominal body size, leads and pin numbering, and a classifier from what a photo shows (body length
and width in mm, number of leads, on how many sides).

Sizes are the nominal body (not the land pattern) from the JEDEC / EIA outlines; photos measure the body plus a
little solder, so matching allows a tolerance. Pin numbering follows the universal rule: hold the part with its
marking readable (or the pin-1 mark at the lower left for dual-row parts, upper left for quad parts) and count
counter-clockwise.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Package:
    name: str
    length: float              # mm, along the long axis
    width: float               # mm
    leads: int
    layout: str                # two-terminal | sot | dual | quad | bottom (pads under the body only)
    kinds: tuple = ()          # what is usually in it: resistor, capacitor, diode, transistor, ic ...
    aliases: tuple = ()
    leads_per_side: tuple = ()  # (bottom, right, top, left) seen with pin 1 at the lower left
    pin1: str = ""             # how pin 1 is marked
    notes: str = ""
    pitch: float = 0.0

    def as_dict(self) -> dict:
        return {"name": self.name, "length": self.length, "width": self.width, "leads": self.leads,
                "layout": self.layout, "kinds": list(self.kinds), "aliases": list(self.aliases),
                "pitch": self.pitch, "pin1": self.pin1, "notes": self.notes}


def _two(name, l, w, kinds=("resistor", "capacitor"), aliases=(), notes=""):
    return Package(name, l, w, 2, "two-terminal", kinds, aliases, (0, 1, 0, 1),
                   "none for resistors and ceramic capacitors; a band or bar marks the cathode of diodes and the "
                   "positive end of tantalum capacitors", notes)


def _dual(name, l, w, leads, pitch, aliases=(), kinds=("ic",), notes=""):
    half = leads // 2
    return Package(name, l, w, leads, "dual", kinds, aliases, (half, 0, half, 0),
                   "dot or notch at pin 1, or a bevelled edge along the pin-1 side", notes, pitch)


def _quad(name, side, leads, pitch, aliases=(), layout="quad", notes=""):
    q = leads // 4
    return Package(name, side, side, leads, layout, ("ic",), aliases, (q, q, q, q),
                   "dot in the pin-1 corner (marking readable: upper left)", notes, pitch)


PACKAGES: list[Package] = [
    # chip resistors and capacitors (EIA inch code; metric code in aliases)
    _two("01005", 0.4, 0.2, aliases=("0402M",)),
    _two("0201", 0.6, 0.3, aliases=("0603M",)),
    _two("0402", 1.0, 0.5, aliases=("1005M",)),
    _two("0603", 1.6, 0.8, aliases=("1608M",)),
    _two("0805", 2.0, 1.25, aliases=("2012M",)),
    _two("1206", 3.2, 1.6, aliases=("3216M",)),
    _two("1210", 3.2, 2.5, aliases=("3225M",)),
    _two("1812", 4.5, 3.2, kinds=("capacitor", "resistor", "inductor"), aliases=("4532M",)),
    _two("2010", 5.0, 2.5, kinds=("resistor",), aliases=("5025M",)),
    _two("2512", 6.3, 3.2, kinds=("resistor",), aliases=("6332M",)),
    # diodes
    _two("SOD-923", 0.8, 0.6, kinds=("diode",)),
    _two("SOD-523", 1.2, 0.8, kinds=("diode",), aliases=("SC-79",)),
    _two("SOD-323", 1.7, 1.25, kinds=("diode",), aliases=("SC-76",)),
    _two("SOD-123", 2.7, 1.6, kinds=("diode",)),
    _two("SOD-123F", 2.7, 1.6, kinds=("diode",), notes="flat leads under the body"),
    _two("MiniMELF", 3.5, 1.5, kinds=("diode", "resistor"), aliases=("SOD-80",), notes="glass cylinder"),
    _two("MELF", 5.0, 2.5, kinds=("diode", "resistor"), aliases=("DO-213AB",)),
    _two("SMA", 4.3, 2.6, kinds=("diode",), aliases=("DO-214AC",)),
    _two("SMB", 4.3, 3.6, kinds=("diode",), aliases=("DO-214AA",)),
    _two("SMC", 6.9, 5.9, kinds=("diode",), aliases=("DO-214AB",)),
    _two("PowerDI-123", 2.8, 1.8, kinds=("diode",)),
    # tantalum and polymer capacitors (EIA case)
    _two("Tantalum A", 3.2, 1.6, kinds=("capacitor",), aliases=("EIA 3216-18",), notes="bar marks +"),
    _two("Tantalum B", 3.5, 2.8, kinds=("capacitor",), aliases=("EIA 3528-21",), notes="bar marks +"),
    _two("Tantalum C", 6.0, 3.2, kinds=("capacitor",), aliases=("EIA 6032-28",), notes="bar marks +"),
    _two("Tantalum D", 7.3, 4.3, kinds=("capacitor",), aliases=("EIA 7343-31",), notes="bar marks +"),
    # small-outline transistors
    Package("SOT-23", 2.9, 1.3, 3, "sot", ("transistor", "diode", "mosfet", "regulator"), ("TO-236", "SOT-23-3"),
            (2, 0, 1, 0), "pins 1, 2 on the two-pin side (1 left with the marking readable), 3 alone", pitch=0.95),
    Package("SOT-323", 2.0, 1.25, 3, "sot", ("transistor", "diode", "mosfet"), ("SC-70",), (2, 0, 1, 0),
            "pins 1, 2 on the two-pin side", pitch=0.65),
    Package("SOT-523", 1.6, 0.8, 3, "sot", ("transistor", "diode"), ("SC-89",), (2, 0, 1, 0), pitch=0.5),
    Package("SOT-416", 1.6, 0.8, 3, "sot", ("transistor", "diode"), ("SC-75",), (2, 0, 1, 0), pitch=0.5),
    Package("SOT-143", 2.9, 1.3, 4, "sot", ("transistor", "diode"), (), (2, 0, 2, 0),
            "pin 1 is the wide lead", pitch=1.9),
    Package("SOT-23-5", 2.9, 1.6, 5, "sot", ("ic", "regulator"), ("SOT-25", "SOT-753", "SC-74A"), (3, 0, 2, 0),
            "pins 1-3 on the three-pin side, left to right with the marking readable", pitch=0.95),
    Package("SOT-23-6", 2.9, 1.6, 6, "sot", ("ic", "regulator", "mosfet"), ("SOT-26", "SOT-457", "SC-74"),
            (3, 0, 3, 0), "dot or bevel at pin 1", pitch=0.95),
    Package("SOT-23-8", 2.9, 1.6, 8, "sot", ("ic",), ("SOT-28",), (4, 0, 4, 0), pitch=0.65),
    Package("SOT-353", 2.0, 1.25, 5, "sot", ("ic", "logic"), ("SC-70-5", "SC-88A"), (3, 0, 2, 0), pitch=0.65),
    Package("SOT-363", 2.0, 1.25, 6, "sot", ("transistor", "logic", "diode"), ("SC-70-6", "SC-88"), (3, 0, 3, 0),
            pitch=0.65),
    Package("SOT-89", 4.5, 2.5, 4, "sot", ("transistor", "regulator"), ("SC-62",), (3, 0, 1, 0),
            "pins 1-3 in a row; the wide tab (top) is pin 2 again, drawn as pin 4", pitch=1.5),
    Package("SOT-223", 6.5, 3.5, 4, "sot", ("regulator", "transistor"), ("TO-261",), (3, 0, 1, 0),
            "pins 1-3 in a row; the tab is pin 4 (= pin 2 on most regulators and transistors)", pitch=2.3),
    Package("DPAK", 6.6, 6.1, 3, "sot", ("regulator", "mosfet", "transistor"), ("TO-252",), (2, 0, 1, 0),
            "two leads, the tab is the middle pin", pitch=4.57),
    Package("D2PAK", 10.2, 9.0, 3, "sot", ("regulator", "mosfet"), ("TO-263",), (2, 0, 1, 0), pitch=5.08),
    # dual-row ICs
    _dual("SOIC-8", 4.9, 3.9, 8, 1.27, ("SO-8",)),
    _dual("SOIC-14", 8.65, 3.9, 14, 1.27, ("SO-14",)),
    _dual("SOIC-16", 9.9, 3.9, 16, 1.27, ("SO-16",)),
    _dual("SOIC-16W", 10.3, 7.5, 16, 1.27, ("SO-16 wide",)),
    _dual("SOIC-20W", 12.8, 7.5, 20, 1.27, ("SO-20",)),
    _dual("SOIC-28W", 17.9, 7.5, 28, 1.27, ("SO-28",)),
    _dual("MSOP-8", 3.0, 3.0, 8, 0.65, ("VSSOP-8", "µMAX-8")),
    _dual("MSOP-10", 3.0, 3.0, 10, 0.5, ("VSSOP-10",)),
    _dual("TSSOP-8", 3.0, 4.4, 8, 0.65),
    _dual("TSSOP-14", 5.0, 4.4, 14, 0.65),
    _dual("TSSOP-16", 5.0, 4.4, 16, 0.65),
    _dual("TSSOP-20", 6.5, 4.4, 20, 0.65),
    _dual("TSSOP-24", 7.8, 4.4, 24, 0.65),
    _dual("TSSOP-28", 9.7, 4.4, 28, 0.65),
    _dual("SSOP-16", 6.2, 5.3, 16, 0.65),
    _dual("SSOP-20", 7.2, 5.3, 20, 0.65),
    _dual("SSOP-28", 10.2, 5.3, 28, 0.65),
    _dual("SO-4 optocoupler", 4.4, 3.6, 4, 2.54, ("Mini-flat 4", "SOP-4"), kinds=("optocoupler", "sensor", "led"),
          notes="4-lead mini-flat: optocouplers, reflective sensors, bicolour LEDs"),
    _dual("SOP-6 optocoupler", 4.4, 4.6, 6, 1.27, ("Mini-flat 6",), kinds=("optocoupler",)),
    # no-lead dual and quad
    _dual("DFN-6 2x2", 2.0, 2.0, 6, 0.65, ("WSON-6",)),
    _dual("DFN-8 3x3", 3.0, 3.0, 8, 0.65, ("WSON-8", "VSON-8")),
    _dual("DFN-8 2x3", 3.0, 2.0, 8, 0.5, ("UDFN-8",)),
    _quad("QFN-16 3x3", 3.0, 16, 0.5, ("VQFN-16", "WQFN-16")),
    _quad("QFN-20 4x4", 4.0, 20, 0.5),
    _quad("QFN-24 4x4", 4.0, 24, 0.5),
    _quad("QFN-28 5x5", 5.0, 28, 0.5),
    _quad("QFN-32 5x5", 5.0, 32, 0.5, ("UFQFPN-32",)),
    _quad("QFN-48 7x7", 7.0, 48, 0.5, ("UFQFPN-48",)),
    _quad("QFN-64 9x9", 9.0, 64, 0.5),
    _quad("LGA-12 2x2", 2.0, 12, 0.5, ("LGA-12",), layout="bottom", notes="sensors: accelerometers, pressure"),
    _quad("LGA-14 2.5x3", 3.0, 14, 0.5, ("LGA-14",), layout="bottom", notes="sensors: IMUs"),
    _quad("LGA-16 3x3", 3.0, 16, 0.5, ("LGA-16",), layout="bottom", notes="sensors: IMUs, magnetometers"),
    _quad("LQFP-32", 7.0, 32, 0.8, ("TQFP-32",)),
    _quad("LQFP-44", 10.0, 44, 0.8, ("TQFP-44",)),
    _quad("LQFP-48", 7.0, 48, 0.5, ("TQFP-48",)),
    _quad("LQFP-64", 10.0, 64, 0.5, ("TQFP-64",)),
    _quad("LQFP-80", 12.0, 80, 0.5, ("TQFP-80",)),
    _quad("LQFP-100", 14.0, 100, 0.5, ("TQFP-100",)),
    _quad("LQFP-144", 20.0, 144, 0.5),
    _quad("LQFP-176", 24.0, 176, 0.5),
    # other common bodies
    Package("SMD electrolytic 4mm", 4.3, 4.3, 2, "two-terminal", ("capacitor",), ("case B",), (0, 1, 0, 1),
            "the black half of the top marks −"),
    Package("SMD electrolytic 5mm", 5.3, 5.3, 2, "two-terminal", ("capacitor",), ("case C",), (0, 1, 0, 1),
            "the black half of the top marks −"),
    Package("SMD electrolytic 6.3mm", 6.6, 6.6, 2, "two-terminal", ("capacitor",), ("case D",), (0, 1, 0, 1),
            "the black half of the top marks −"),
    Package("Power inductor 3x3", 3.0, 3.0, 2, "two-terminal", ("inductor",)),
    Package("Power inductor 4x4", 4.0, 4.0, 2, "two-terminal", ("inductor",)),
    Package("Power inductor 5x5", 5.0, 5.0, 2, "two-terminal", ("inductor",)),
    Package("Crystal 3225", 3.2, 2.5, 4, "bottom", ("crystal",), ("SMD3225",), (1, 1, 1, 1),
            "pins 1 and 3 are the crystal, 2 and 4 ground"),
    Package("Crystal 2520", 2.5, 2.0, 4, "bottom", ("crystal",), ("SMD2520",), (1, 1, 1, 1)),
    Package("Crystal 5032", 5.0, 3.2, 2, "two-terminal", ("crystal",), ("SMD5032",), (0, 1, 0, 1)),
    Package("HC-49/SMD", 11.4, 4.7, 2, "two-terminal", ("crystal",), ("HC-49US SMD",), (0, 1, 0, 1)),
    Package("PLCC-2 LED", 3.5, 2.8, 2, "two-terminal", ("led",), ("3528",), (0, 1, 0, 1), "cut corner: cathode"),
    Package("PLCC-4 LED", 3.5, 2.8, 4, "dual", ("led",), ("3528 RGB",), (2, 0, 2, 0)),
    Package("5050 LED", 5.0, 5.0, 6, "dual", ("led",), ("WS2812B-5050",), (3, 0, 3, 0), "cut corner: pin 1"),
]

BY_NAME = {p.name.lower(): p for p in PACKAGES}
for _p in PACKAGES:
    for _a in _p.aliases:
        BY_NAME.setdefault(_a.lower(), _p)


def package(name: str) -> Package | None:
    return BY_NAME.get(name.strip().lower())


@dataclass
class Match:
    package: Package
    score: float                # 0..1
    why: str = ""
    size_error: float = 0.0     # relative

    def as_dict(self) -> dict:
        return {"package": self.package.name, "score": round(self.score, 3), "why": self.why,
                "size_error": round(self.size_error, 3), **{k: v for k, v in self.package.as_dict().items()
                                                              if k in ("leads", "layout", "kinds", "pin1")}}


def classify(length: float, width: float, leads: int | None = None, sides: int | None = None,
             kind: str | None = None, top: int = 5) -> list[Match]:
    """Package candidates for a body of ``length`` x ``width`` mm (either order) with ``leads`` leads/pads on
    ``sides`` sides (2 for dual-row and two-terminal, 4 for quad)."""
    length, width = max(length, width), min(length, width)
    matches = []
    for p in PACKAGES:
        pl, pw = max(p.length, p.width), min(p.length, p.width)
        err = math.hypot(math.log(length / pl), math.log(width / pw)) if length > 0 and width > 0 else 9.0
        score = math.exp(-(err / 0.18) ** 2)
        why = [f"body {length:.2f}x{width:.2f} vs {pl:g}x{pw:g} mm"]
        if leads is not None:
            if leads == p.leads:
                why.append(f"{leads} leads")
            elif leads == 2 or p.leads == 2:
                score *= 0.05                # two terminals or not is seen reliably
            else:
                score *= 0.25 if abs(leads - p.leads) <= max(1, p.leads // 8) else 0.03
        if sides is not None:
            p_sides = 4 if p.layout in ("quad", "bottom") and p.leads_per_side.count(0) == 0 else 2
            if p.layout == "sot":
                p_sides = 2
            if sides != p_sides:
                score *= 0.3
        if kind and p.kinds and kind not in p.kinds:
            score *= 0.4
        matches.append(Match(p, score, "; ".join(why), err))
    matches.sort(key=lambda m: -m.score)
    return [m for m in matches[:top] if m.score > 0.01]


def number_pins(points: list[tuple[float, float]], layout: str, up: tuple[float, float] = (0.0, -1.0),
                centre: tuple[float, float] | None = None) -> list[int]:
    """Pin numbers for pad centres ``points`` (image coordinates, y down) of one part.

    ``up`` is the direction in which the marking reads upright (default: image up). Pins are counted
    counter-clockwise as seen from the top: dual-row and SOT parts from the lower left (bottom row left to right,
    then the top row right to left), quad parts from the upper left (down the left side first). Two-terminal parts:
    1 is the left (or upper) pad. Returns the number of each point, in input order."""
    if not points:
        return []
    cx, cy = centre or (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))
    ux, uy = up
    norm = math.hypot(ux, uy) or 1.0
    ux, uy = ux / norm, uy / norm
    rx, ry = -uy, ux                       # the reading direction (right), y down: rotate up by +90°
    local = [((x - cx) * rx + (y - cy) * ry, -((x - cx) * ux + (y - cy) * uy)) for x, y in points]  # (right, down)
    if layout == "two-terminal" or len(points) == 2:
        order = sorted(range(len(points)), key=lambda i: (local[i][0], local[i][1]))
        return _ranks(order)
    # angle of each pad around the centre; counter-clockwise on screen (y down) is decreasing atan2(down, right)
    if layout in ("quad", "bottom"):
        start = math.atan2(-1.0, -1.0)     # upper left
        # the first pin is the top of the left side: just past the corner going down the left side
        key = [(_ccw(math.atan2(d, r), start + 1e-6)) for r, d in local]
    else:
        start = math.pi                    # dual rows: from the left end of the bottom row
        key = [(_ccw(math.atan2(d, r), start)) for r, d in local]
    order = sorted(range(len(points)), key=lambda i: key[i])
    return _ranks(order)


def _ccw(angle: float, start: float) -> float:
    """Distance from ``start`` going counter-clockwise on screen (y down: decreasing atan2(down, right))."""
    return (start - angle) % (2 * math.pi)


def _ranks(order: list[int]) -> list[int]:
    numbers = [0] * len(order)
    for rank, index in enumerate(order, 1):
        numbers[index] = rank
    return numbers
