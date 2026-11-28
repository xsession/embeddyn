"""Value codes printed on SMD passives: resistors (3- and 4-digit, R/m notation, EIA-96), capacitors (pF digit
codes, EIA-198 letter+digit, tantalum voltage letters) and inductors (µH digit codes).

Every decoder returns ``None`` when the text is not that kind of code, else a :class:`Value`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# EIA-96 (E96 series, 1 %): code 01..96 -> significant figures
E96 = [100, 102, 105, 107, 110, 113, 115, 118, 121, 124, 127, 130, 133, 137, 140, 143, 147, 150, 154, 158, 162,
       165, 169, 174, 178, 182, 187, 191, 196, 200, 205, 210, 215, 221, 226, 232, 237, 243, 249, 255, 261, 267,
       274, 280, 287, 294, 301, 309, 316, 324, 332, 340, 348, 357, 365, 374, 383, 392, 402, 412, 422, 432, 442,
       453, 464, 475, 487, 499, 511, 523, 536, 549, 562, 576, 590, 604, 619, 634, 649, 665, 681, 698, 715, 732,
       750, 768, 787, 806, 825, 845, 866, 887, 909, 931, 953, 976]
EIA96_MULTIPLIER = {"Z": 0.001, "Y": 0.01, "R": 0.01, "X": 0.1, "S": 0.1, "A": 1, "B": 10, "H": 10, "C": 100,
                    "D": 1e3, "E": 1e4, "F": 1e5}
# EIA-198 capacitor code: letter = significant figures, digit = power of ten (pF); 9 means 0.1
EIA198_LETTER = {"A": 1.0, "B": 1.1, "C": 1.2, "D": 1.3, "E": 1.5, "F": 1.6, "G": 1.8, "H": 2.0, "J": 2.2,
                 "K": 2.4, "a": 2.5, "L": 2.7, "M": 3.0, "N": 3.3, "b": 3.5, "P": 3.6, "Q": 3.9, "d": 4.0,
                 "R": 4.3, "e": 4.5, "S": 4.7, "f": 5.0, "T": 5.1, "U": 5.6, "m": 6.0, "V": 6.2, "W": 6.8,
                 "n": 7.0, "X": 7.5, "t": 8.0, "Y": 8.2, "y": 9.0, "Z": 9.1}
# tantalum and polymer capacitors: rated voltage letter
TANTALUM_VOLTAGE = {"e": 2.5, "G": 4.0, "J": 6.3, "A": 10.0, "C": 16.0, "D": 20.0, "E": 25.0, "V": 35.0,
                    "H": 50.0, "T": 50.0}


@dataclass
class Value:
    kind: str            # resistor | capacitor | inductor
    value: float         # ohm, farad, henry
    text: str            # the value written the usual way, e.g. "4.7k", "100n", "10u"
    code: str            # the code that was decoded
    system: str          # which code system, e.g. "3-digit", "EIA-96"
    tolerance: str = ""  # what the code system implies, e.g. "1 %"
    note: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value, "text": self.text, "code": self.code,
                "system": self.system, "tolerance": self.tolerance, "note": self.note}


def _format(value: float, unit: str) -> str:
    prefixes = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p")]
    if value == 0:
        return "0" + unit
    for scale, prefix in prefixes:
        if abs(value) >= scale * 0.9999:
            number = value / scale
            text = f"{number:.3g}"
            if unit == "Ω" and prefix:            # resistors: 4k7 style is common, 4.7k is clearer
                return f"{text}{prefix}"
            return f"{text}{prefix}{unit}"
    return f"{value:.3g}{unit}"


def _clean(code: str) -> str:
    return code.strip().replace(" ", "").replace("_", "").replace("-", "")


def decode_resistor(code: str) -> Value | None:
    c = re.sub(r"[Oo]", "0", _clean(code))          # OCR reads 0 as O; no resistor code uses the letter O
    if not c:
        return None
    if re.fullmatch(r"0+", c):
        return Value("resistor", 0.0, "0Ω (jumper)", code, "zero-ohm")
    m = re.fullmatch(r"(\d{2})([ZYRXSABHCDEF])", c.upper())
    if m and 1 <= int(m.group(1)) <= 96:
        value = E96[int(m.group(1)) - 1] * EIA96_MULTIPLIER[m.group(2)]
        return Value("resistor", value, _format(value, "Ω"), code, "EIA-96", "1 %")
    m = re.fullmatch(r"(\d)(\d)(\d)", c)
    if m:
        value = int(m.group(1) + m.group(2)) * 10 ** int(m.group(3))
        return Value("resistor", float(value), _format(value, "Ω"), code, "3-digit", "5 %")
    m = re.fullmatch(r"(\d{3})(\d)", c)
    if m:
        value = int(m.group(1)) * 10 ** int(m.group(2))
        return Value("resistor", float(value), _format(value, "Ω"), code, "4-digit", "1 %")
    m = re.fullmatch(r"(\d*)([RrMmKk])(\d+)", c)
    if m and (m.group(1) or m.group(3)) and len(c) <= 5:
        scale = {"R": 1.0, "M": 1e-3, "K": 1e3}[m.group(2).upper()]
        if m.group(2) == "M":                     # capital M in a resistor code: megaohm (e.g. 1M0)
            scale = 1e6
        value = float(f"{m.group(1) or 0}.{m.group(3)}") * scale
        return Value("resistor", value, _format(value, "Ω"), code, "R notation",
                     "1 %" if len(m.group(1) + m.group(3)) >= 3 else "5 %",
                     "current-sense value" if value < 1 else "")
    m = re.fullmatch(r"(\d+)([Rr])", c)
    if m:
        value = float(m.group(1))
        return Value("resistor", value, _format(value, "Ω"), code, "R notation")
    return None


def decode_capacitor(code: str) -> Value | None:
    c = _clean(code)
    if not c:
        return None
    # tantalum/polymer: optional voltage letter before or after a 3-digit pF code ("A107", "107A", "107 16V")
    voltage = None
    m = re.fullmatch(r"(?:([eGJACDEVHT]))?(\d{3})(?:([eGJACDEVHT]))?", c)
    if m and (m.group(1) or m.group(3)):
        letter = m.group(1) or m.group(3)
        voltage = TANTALUM_VOLTAGE.get(letter)
        c = m.group(2)
    m = re.fullmatch(r"(\d{3})(\d+(?:\.\d)?)V", code.replace(" ", "")) if voltage is None else None
    if m:
        voltage = float(m.group(2))
        c = m.group(1)
    m = re.fullmatch(r"(\d)(\d)(\d)", c)
    if m and int(m.group(3)) <= 7:
        pf = int(m.group(1) + m.group(2)) * 10 ** int(m.group(3))
        value = pf * 1e-12
        return Value("capacitor", value, _format(value, "F"), code, "3-digit pF",
                     note=f"rated {voltage:g} V" if voltage else "")
    m = re.fullmatch(r"(\d*)[Rr](\d+)", c)
    if m:
        value = float(f"{m.group(1) or 0}.{m.group(2)}") * 1e-12
        return Value("capacitor", value, _format(value, "F"), code, "R notation pF")
    m = re.fullmatch(r"([A-Za-z])(\d)", c)
    if m and m.group(1) in EIA198_LETTER:
        exponent = -1 if m.group(2) == "9" else int(m.group(2))
        value = EIA198_LETTER[m.group(1)] * 10 ** exponent * 1e-12
        return Value("capacitor", value, _format(value, "F"), code, "EIA-198",
                     note="letter+digit capacitor code (rare on ceramics; same text can be a semiconductor code)")
    return None


def decode_inductor(code: str) -> Value | None:
    c = _clean(code)
    m = re.fullmatch(r"(\d)(\d)(\d)", c)
    if m:
        value = int(m.group(1) + m.group(2)) * 10 ** int(m.group(3)) * 1e-6
        return Value("inductor", value, _format(value, "H"), code, "3-digit µH")
    m = re.fullmatch(r"(\d*)[Rr](\d+)", c)
    if m:
        value = float(f"{m.group(1) or 0}.{m.group(2)}") * 1e-6
        return Value("inductor", value, _format(value, "H"), code, "R notation µH")
    m = re.fullmatch(r"(\d*)[Nn](\d+)", c)
    if m:
        value = float(f"{m.group(1) or 0}.{m.group(2)}") * 1e-9
        return Value("inductor", value, _format(value, "H"), code, "N notation nH")
    return None


def decode_value(code: str, kind: str | None = None) -> list[Value]:
    """Every reading of a value code; ``kind`` (resistor/capacitor/inductor) narrows it."""
    found = []
    for name, decoder in (("resistor", decode_resistor), ("capacitor", decode_capacitor),
                          ("inductor", decode_inductor)):
        if kind and kind != name:
            continue
        value = decoder(code)
        if value:
            found.append(value)
    return found
