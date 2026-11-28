"""Decode the marking on an SMD part: value codes, marking codes and printed part numbers.

``decode("103")`` -> a 10 kΩ resistor (or a 10 nF capacitor, if it is a capacitor);
``decode("A7", package="SOT-23")`` -> BAV99; ``decode("74HC595D\\nCRE79805")`` -> an 8-bit shift register in SO-16;
``decode("F051K6U6")`` -> STM32F051K6 in UFQFPN-32. Lines after the first are usually date and lot codes.
"""
from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path

import yaml

from .packages import package as find_package
from .values import Value, decode_value

USER_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "embeddyn" / "smd-codes"
NEXPERIA_SITE = re.compile(r"^(.{1,3}?)([ptW%*])$")


@dataclass
class Reading:
    """One interpretation of a marking."""
    kind: str               # value | code | part-number
    part: str               # what it is: "BAV99", "10k resistor", "74HC595 8-bit shift register ..."
    detail: str = ""        # function, maker, package
    package: str = ""
    score: float = 0.5      # 0..1, how likely
    source: str = ""        # built-in table, a user table, a part-number rule
    confidence: str = ""    # "" or "check"
    value: Value | None = None
    pins: str = ""
    matched: str = ""       # the text that matched

    def as_dict(self) -> dict:
        data = {k: v for k, v in self.__dict__.items() if k != "value"}
        if self.value:
            data["value"] = self.value.as_dict()
        data["score"] = round(self.score, 3)
        return data


@dataclass
class Decoded:
    text: str
    package: str = ""
    readings: list[Reading] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def best(self) -> Reading | None:
        return self.readings[0] if self.readings else None

    def as_dict(self) -> dict:
        return {"text": self.text, "package": self.package, "readings": [r.as_dict() for r in self.readings],
                "notes": self.notes}


# --- code tables ----------------------------------------------------------------------------------------------

def _rows_from_file(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8", newline="") as handle:
            return [{k.strip().lower(): (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(handle)]
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    if isinstance(data, dict):
        data = data.get("codes") or []
    return [dict(row) for row in data or [] if isinstance(row, dict)]


def code_files() -> list[Path]:
    files = []
    if USER_DIR.is_dir():
        files += sorted(p for p in USER_DIR.iterdir() if p.suffix.lower() in (".csv", ".yaml", ".yml", ".json"))
    for item in os.environ.get("EMBEDDYN_SMD_CODES", "").split(os.pathsep):
        if item and Path(item).is_file():
            files.append(Path(item))
    return files


@lru_cache(maxsize=8)
def _load(extra: tuple[str, ...] = ()) -> list[dict]:
    text = resources.files("embeddyn.mechatronics.smd").joinpath("codes.yaml").read_text(encoding="utf-8")
    rows = [dict(r, source="built-in") for r in yaml.safe_load(text)["codes"]]
    for path in [*code_files(), *(Path(p) for p in extra)]:
        try:
            rows += [dict(r, source=path.name) for r in _rows_from_file(path)]
        except (OSError, ValueError, yaml.YAMLError, csv.Error) as error:
            rows.append({"code": "", "error": f"{path}: {error}"})
    for row in rows:
        row["code"] = str(row.get("code") or "").strip()
        row["prefix"] = str(row.get("prefix", "")).lower() in ("1", "true", "yes")
    return [r for r in rows if r["code"]]


def code_table(extra: list[Path] | None = None) -> list[dict]:
    return _load(tuple(str(p) for p in extra or []))


# --- part numbers printed in full ---------------------------------------------------------------------------

LOGIC_FUNCTIONS = {
    "00": "quad 2-input NAND", "02": "quad 2-input NOR", "04": "hex inverter", "05": "hex inverter, open drain",
    "07": "hex buffer, open drain", "08": "quad 2-input AND", "10": "triple 3-input NAND", "14": "hex Schmitt inverter",
    "17": "hex Schmitt buffer", "21": "dual 4-input AND", "30": "8-input NAND", "32": "quad 2-input OR",
    "74": "dual D flip-flop", "86": "quad 2-input XOR", "107": "dual JK flip-flop", "123": "dual monostable",
    "125": "quad buffer, 3-state", "126": "quad buffer, 3-state (active-high enable)", "132": "quad Schmitt NAND",
    "138": "3-to-8 decoder", "139": "dual 2-to-4 decoder", "151": "8-to-1 multiplexer", "153": "dual 4-to-1 mux",
    "157": "quad 2-to-1 mux", "161": "4-bit binary counter", "163": "4-bit binary counter (sync reset)",
    "164": "8-bit serial-in parallel-out shift register", "165": "8-bit parallel-in serial-out shift register",
    "166": "8-bit PISO shift register", "174": "hex D flip-flop", "175": "quad D flip-flop",
    "191": "4-bit up/down counter", "240": "octal inverting buffer, 3-state", "244": "octal buffer, 3-state",
    "245": "octal bus transceiver, 3-state", "273": "octal D flip-flop with clear", "374": "octal D flip-flop, 3-state",
    "393": "dual 4-bit counter", "541": "octal buffer, 3-state", "573": "octal D latch, 3-state",
    "574": "octal D flip-flop, 3-state", "595": "8-bit shift register with output latches, 3-state (SPI-like)",
    "597": "8-bit shift register with input latches", "4017": "decade counter / divider",
    "4040": "12-stage binary counter", "4051": "8-channel analog multiplexer", "4052": "dual 4-channel analog mux",
    "4053": "triple 2-channel analog switch", "4060": "14-stage counter with oscillator", "4066": "quad analog switch",
    "4094": "8-bit shift register with latches", "1G00": "single 2-input NAND", "1G04": "single inverter",
    "1G08": "single 2-input AND", "1G14": "single Schmitt inverter", "1G125": "single buffer, 3-state",
    "1G32": "single 2-input OR", "1G86": "single XOR", "2G04": "dual inverter", "2G14": "dual Schmitt inverter",
    "1T45": "single bus transceiver, dual supply", "2T45": "dual bus transceiver, dual supply",
}
LOGIC_FAMILIES = {
    "HC": "CMOS 2-6 V", "HCT": "CMOS, TTL inputs, 5 V", "AHC": "advanced CMOS 2-5.5 V", "AHCT": "advanced CMOS, TTL inputs",
    "LV": "CMOS 1-5.5 V", "LVC": "low-voltage CMOS 1.65-3.6 V (5 V tolerant inputs)", "LVT": "BiCMOS 3.3 V",
    "ALVC": "advanced low-voltage CMOS", "AUP": "ultra-low-power CMOS 0.8-3.6 V", "AC": "advanced CMOS",
    "ACT": "advanced CMOS, TTL inputs", "LS": "low-power Schottky TTL", "ALS": "advanced LS TTL", "F": "fast TTL",
    "ABT": "advanced BiCMOS", "HEF": "CMOS 4000 series 3-15 V", "VHC": "very-high-speed CMOS", "VHCT": "VHC, TTL inputs",
    "LCX": "low-voltage CMOS 3.3 V (5 V tolerant)", "AXP": "extremely low-power CMOS",
}
LOGIC_PACKAGES = {  # suffix (NXP / TI / onsemi) -> package
    "D": "SOIC", "DR": "SOIC", "DW": "SOIC wide", "PW": "TSSOP", "PWR": "TSSOP", "DB": "SSOP", "DGG": "TSSOP",
    "N": "DIP", "BQ": "DHVQFN", "GW": "SOT-353", "GV": "SOT-753", "DBV": "SOT-23-5", "DCK": "SC-70-5",
    "GM": "XQFN", "DTR2G": "SOIC", "DR2G": "SOIC", "FN": "PLCC",
}
STM32_PINS = {"F": 20, "G": 28, "K": 32, "T": 36, "S": 44, "C": 48, "U": 63, "R": 64, "O": 90, "V": 100, "Q": 132,
              "Z": 144, "A": 169, "I": 176, "B": 208, "N": 216, "X": 256, "E": 25, "J": 72}
STM32_FLASH = {"3": "8 KB", "4": "16 KB", "6": "32 KB", "8": "64 KB", "B": "128 KB", "Z": "192 KB", "C": "256 KB",
               "D": "384 KB", "E": "512 KB", "F": "768 KB", "G": "1 MB", "H": "1.5 MB", "I": "2 MB", "K": "3 MB"}
STM32_PACKAGE = {"T": "LQFP", "U": "UFQFPN", "H": "BGA", "Y": "WLCSP", "P": "TSSOP", "M": "SO", "I": "UFBGA",
                 "K": "UFBGA", "J": "UFBGA", "Q": "UFBGA", "V": "VFQFPN", "G": "UFQFPN", "D": "TSSOP"}
STM32_TEMP = {"6": "-40..85 °C", "7": "-40..105 °C", "3": "-40..125 °C"}

_LOGIC = re.compile(r"^(SN|MC|M|NC7S|NC7SZ|NL|HEF|CD|TC|NLV|NLX)?(?:74|54)(" + "|".join(sorted(LOGIC_FAMILIES, key=len, reverse=True))
                    + r")?(1G\d{2,3}|2G\d{2}|[12]T45|\d{2,4})([A-Z0-9]*)$")
_STM32 = re.compile(r"^(?:STM32)?([FGLHUC][0-9][0-9A-Z][0-9]|W[BL][0-9]{2}|WBA[0-9]{2})()([FGKTSCURVZAIBNXEJOQ])"
                    r"([3468BZCDEFGHIK])([THYPMIKJQVGDU])?([367])?$")


def _logic(token: str) -> Reading | None:
    m = _LOGIC.match(token)
    if not m:
        return None
    family, number, suffix = m.group(2) or "", m.group(3), m.group(4)
    function = LOGIC_FUNCTIONS.get(number)
    if not function and not family:
        return None
    pkg = next((LOGIC_PACKAGES[s] for s in sorted(LOGIC_PACKAGES, key=len, reverse=True) if suffix.startswith(s)), "")
    name = f"74{family}{number}"
    detail = ", ".join(x for x in (function or "logic", LOGIC_FAMILIES.get(family, ""),
                                   f"package {pkg}" if pkg else "") if x)
    return Reading("part-number", name, detail, pkg, 0.9 if function else 0.6, "74-series rule", matched=token)


def _stm32(token: str) -> Reading | None:
    m = _STM32.match(token)
    if not m or not (token.startswith("STM32") or m.group(5) or len(token) >= 6):
        return None
    line, _variant, pins, flash, pkg, temp = m.groups()
    name = f"STM32{line}{_variant or ''}{pins}{flash}"
    pin_count = STM32_PINS.get(pins)
    package = f"{STM32_PACKAGE.get(pkg, '')}-{pin_count}" if pkg and pin_count else ""
    detail = ", ".join(x for x in (f"{pin_count} pins" if pin_count else "", f"flash {STM32_FLASH.get(flash, '?')}",
                                   package, STM32_TEMP.get(temp or "", "")) if x)
    return Reading("part-number", name, detail, package, 0.9 if token.startswith("STM32") or pkg else 0.7,
                   "STM32 part-number rule", matched=token)


# --- fuzzy part numbers: OCR of etched markings confuses similar glyphs -------------------------------------------

_PAIRS = ["S5", "S9", "59", "O0", "D0", "Q0", "U0", "I1", "L1", "T1", "J1", "71", "7T", "7P", "7Z", "B8", "Z2",
          "G6", "A4", "NH", "HM", "EF", "CG", "UV", "E6", "38", "B3", "2Z"]
_CONFUSE = {frozenset(p) for p in _PAIRS}


def _classes() -> dict[str, str]:
    parent = {}

    def find(c):
        parent.setdefault(c, c)
        while parent[c] != c:
            c = parent[c]
        return c
    for a, b in _PAIRS:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    return {c: find(c) for c in parent}


_CLASS = _classes()


def _bag(text: str) -> Counter:
    return Counter(_CLASS.get(c, c) for c in text)


def _cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    return 0.3 if frozenset((a, b)) in _CONFUSE else 1.0


def _distance(a: str, b: str) -> float:
    """Edit distance where look-alike characters (S/5, O/0, 7/P ...) cost 0.3."""
    prev = [float(j) for j in range(len(b) + 1)]
    for i, ca in enumerate(a, 1):
        cur = [float(i)]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + _cost(ca, cb)))
        prev = cur
    return prev[-1]


@lru_cache(maxsize=1)
def _fuzzy_index() -> list[tuple[str, Counter]]:
    return [(pn, _bag(pn)) for pn in _fuzzy_dictionary()]


@lru_cache(maxsize=1)
def _fuzzy_dictionary() -> list[str]:
    """Part numbers worth recognising from a poor reading: 74-series logic in the common families/packages."""
    entries = []
    for number in LOGIC_FUNCTIONS:
        if not number[0].isdigit():
            continue
        for family in ("HC", "HCT", "LVC", "AHC", "AHCT", "LV", "AC", "ACT", "LS", "VHC"):
            for suffix in ("D", "PW", "N", "DB", "BQ", ""):
                entries.append(f"74{family}{number}{suffix}")
    return entries


def fuzzy_part_number(token: str, threshold: float = 0.72) -> Reading | None:
    """The known part number a garbled reading most likely is (74-series logic), or None."""
    token = re.sub(r"[^A-Z0-9]", "", token.upper())
    if len(token) < 6:
        return None
    match = _fuzzy_best(token)
    if match is None or match[0] < threshold:
        return None
    best = match
    reading = _logic(best[1])
    if reading is None:
        return None
    reading.part = f"{reading.part}?"
    reading.detail = f"{reading.detail} (read as '{token}', {best[0]:.0%} alike: check)"
    reading.score = 0.6 * best[0]
    reading.confidence = "check"
    reading.source = "fuzzy match"
    reading.matched = token
    return reading


@lru_cache(maxsize=4096)
def _fuzzy_best(token: str) -> tuple[float, str] | None:
    bag = _bag(token)
    best = (0.0, "")
    for pn, pbag in _fuzzy_index():
        if abs(len(pn) - len(token)) > 2:
            continue
        if sum((bag & pbag).values()) < 0.6 * max(len(pn), len(token)):
            continue
        score = 1.0 - _distance(token, pn) / max(len(pn), len(token))
        if score > best[0]:
            best = (score, pn)
    return best if best[1] else None


_GENERIC_PN = re.compile(r"^(?=.*[A-Z])(?=.*\d)[A-Z0-9][A-Z0-9\-./]{4,}$")


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s,;]+", text.upper().replace("Ø", "0")) if t]


# --- the decoder ------------------------------------------------------------------------------------------------

def decode(text: str, package: str | None = None, kind: str | None = None,
           extra_tables: list[Path] | None = None) -> Decoded:
    """All readings of a marking, best first. ``package`` (e.g. "SOT-23", "0603") and ``kind`` (resistor,
    capacitor, inductor, diode, transistor, ic ...) narrow and rank them."""
    result = Decoded(text=text, package=package or "")
    lines = [line.strip() for line in str(text).splitlines() if line.strip()] or [str(text).strip()]
    pkg = find_package(package) if package else None
    two_terminal = pkg is not None and pkg.layout == "two-terminal"
    passive = kind in ("resistor", "capacitor", "inductor") or (two_terminal and not set(pkg.kinds) & {"diode"})
    first = lines[0]

    # value codes: passives carry a single short code
    if len(lines) == 1 and len(first.replace(" ", "")) <= 5 and (passive or kind is None or two_terminal):
        for value in decode_value(first, kind if kind in ("resistor", "capacitor", "inductor") else None):
            score = 0.85 if value.kind == "resistor" else 0.55 if value.kind == "capacitor" else 0.45
            if kind == value.kind:
                score = 0.95
            elif pkg is not None and value.kind not in pkg.kinds:
                score *= 0.3
            if not passive and pkg is not None and not two_terminal:
                score *= 0.2                 # a 3-digit code on a SOT-23 is a marking code, not a value
            digits = first.strip()
            if len(digits) == 3 and digits[0] == "0" and digits != "000":
                score *= 0.3                 # "089" is "680" upside down: codes do not start with 0
            if value.kind == "resistor" and not (0.01 <= value.value <= 22e6):
                score *= 0.2
            if value.system in ("3-digit", "3-digit pF", "4-digit", "3-digit µH") and digits.isdigit():
                from .values import standard_series
                figures = digits[:-1]
                if not standard_series(int(figures), len(figures)):
                    score *= 0.35            # not a standard value: more likely a misreading
            if value.kind == "capacitor" and not (0.5e-12 <= value.value <= 10e-3):
                score *= 0.2
            result.readings.append(Reading("value", f"{value.text} {value.kind}", value.system, package or "",
                                           score, "value code", value=value, matched=first))
            if value.note:
                result.readings[-1].detail += f" ({value.note})"

    # marking codes (short top-marks)
    table = code_table(extra_tables)
    for raw in [first.replace(" ", ""), *(_tokens(first))]:
        candidates = {raw, raw.upper()}
        m = NEXPERIA_SITE.match(raw)
        if m and len(raw) >= 2:
            candidates.add(m.group(1))
            candidates.add(m.group(1).upper())
        for row in table:
            code = row["code"]
            hit = any(c == code or c.upper() == code.upper() for c in candidates)
            if not hit and row["prefix"]:
                hit = any(c.upper().startswith(code.upper()) and len(c) <= len(code) + 3 for c in candidates)
            if not hit:
                continue
            score = 0.6
            row_pkg = str(row.get("package") or "")
            if pkg is not None and row_pkg:
                same = find_package(row_pkg)
                score = 0.85 if same is pkg else 0.75 if same and same.leads == pkg.leads else 0.25
            if str(row.get("confidence", "")).lower() == "check":
                score *= 0.85
            if any(r.kind == "code" and r.part == row.get("part") and r.package == row_pkg for r in result.readings):
                continue
            result.readings.append(Reading(
                "code", str(row.get("part") or "?"),
                ", ".join(x for x in (str(row.get("function") or ""), str(row.get("maker") or "")) if x),
                row_pkg, score, row.get("source", ""), str(row.get("confidence") or ""), pins=str(row.get("pins") or ""),
                matched=raw))

    # part numbers printed in full (any line)
    for line in lines:
        for token in _tokens(line):
            for rule in (_logic, _stm32):
                reading = rule(token)
                if reading and not any(r.part == reading.part for r in result.readings):
                    result.readings.append(reading)
    if not any(r.kind == "part-number" for r in result.readings) and not passive:
        for line in lines[:2]:
            for token in _tokens(line):
                reading = fuzzy_part_number(token)
                if reading and not any(r.part == reading.part for r in result.readings):
                    result.readings.append(reading)
    if not any(r.kind == "part-number" for r in result.readings) and not passive:
        token = next((t for t in _tokens(first) if _GENERIC_PN.match(t)), None)
        if token and not any(r.kind == "code" for r in result.readings):
            result.readings.append(Reading("part-number", token, "printed part number (look it up)", package or "",
                                           0.4, "printed text", matched=token))
    if len(lines) > 1:
        result.notes.append("lines after the first are usually date, lot and plant codes: "
                            + " / ".join(lines[1:]))
    result.readings.sort(key=lambda r: -r.score)
    if not result.readings:
        result.notes.append("no known code: add it to a table in " + str(USER_DIR))
    return result
