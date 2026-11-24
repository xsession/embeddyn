"""Numbers with units, as written in project files.

`Quantity.parse("0.5 ms", "s")` gives `Quantity(0.0005, "s")`: a value with a
unit is converted to the unit the caller expects (a signal's declared unit,
seconds for steps). Without an expected unit, SI prefixes are normalised away
(`"2 kohm"` -> 2000 ohm, `"500 g"` -> 0.5 kg) while units chosen on purpose
(deg, rpm, degC, ...) are kept. Converting between incompatible dimensions
(`"3 V"` where seconds are expected) is an error.

Unit expressions are products and quotients of symbols with optional integer
powers: `N*m`, `kg*m^2`, `rad/s^2`, `N*m*s/rad`, `mN*m`. Symbols take the SI
prefixes p n u µ m c k M G. A unit embeddyn cannot parse (`counts`, `duty`)
is kept as written and the number is used unchanged.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

_QUANTITY = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(.*?)\s*$")

# Dimension vector: metre, kilogram, second, ampere, kelvin, radian. Angle is kept as its own
# dimension so deg <-> rad and rpm <-> rad/s convert, and rad/s is not mistaken for Hz.
_Dims = tuple[int, int, int, int, int, int]
_NONE: _Dims = (0, 0, 0, 0, 0, 0)


def _d(m=0, kg=0, s=0, a=0, k=0, rad=0) -> _Dims:
    return (m, kg, s, a, k, rad)


# symbol -> (factor to coherent SI, dimensions, canonical symbol when normalising, takes a prefix)
_SYMBOLS: dict[str, tuple[float, _Dims, str, bool]] = {
    "s": (1.0, _d(s=1), "s", True),
    "min": (60.0, _d(s=1), "s", False),
    "h": (3600.0, _d(s=1), "s", False),
    "Hz": (1.0, _d(s=-1), "Hz", True),
    "m": (1.0, _d(m=1), "m", True),
    "g": (1e-3, _d(kg=1), "kg", True),
    "A": (1.0, _d(a=1), "A", True),
    "V": (1.0, _d(m=2, kg=1, s=-3, a=-1), "V", True),
    "ohm": (1.0, _d(m=2, kg=1, s=-3, a=-2), "ohm", True),
    "Ω": (1.0, _d(m=2, kg=1, s=-3, a=-2), "ohm", True),
    "S": (1.0, _d(m=-2, kg=-1, s=3, a=2), "S", True),
    "H": (1.0, _d(m=2, kg=1, s=-2, a=-2), "H", True),
    "F": (1.0, _d(m=-2, kg=-1, s=4, a=2), "F", True),
    "C": (1.0, _d(s=1, a=1), "C", True),
    "W": (1.0, _d(m=2, kg=1, s=-3), "W", True),
    "J": (1.0, _d(m=2, kg=1, s=-2), "J", True),
    "N": (1.0, _d(m=1, kg=1, s=-2), "N", True),
    "Nm": (1.0, _d(m=2, kg=1, s=-2), "N*m", False),
    "Pa": (1.0, _d(m=-1, kg=1, s=-2), "Pa", True),
    "bar": (1e5, _d(m=-1, kg=1, s=-2), "bar", False),
    "T": (1.0, _d(kg=1, s=-2, a=-1), "T", True),
    "Wb": (1.0, _d(m=2, kg=1, s=-2, a=-1), "Wb", True),
    "K": (1.0, _d(k=1), "K", True),
    "degC": (1.0, _d(k=1), "degC", False),       # a lone degC also has an offset (below)
    "rad": (1.0, _d(rad=1), "rad", True),
    "deg": (math.pi / 180, _d(rad=1), "deg", False),
    "rev": (2 * math.pi, _d(rad=1), "rev", False),
    "rpm": (2 * math.pi / 60, _d(s=-1, rad=1), "rpm", False),
    "sr": (1.0, _NONE, "sr", False),             # steradian (radiant intensity W/sr)
}
_PREFIXES = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "c": 1e-2,
             "k": 1e3, "M": 1e6, "G": 1e9}
_OFFSETS = {"degC": 273.15}                     # absolute temperature: K = degC + 273.15
_TOKEN = re.compile(r"\s*([*/·])?\s*([^\s*/·^]+)(?:\^([+-]?\d+))?")


@dataclass(frozen=True)
class Unit:
    factor: float              # SI value = factor * value + offset
    dims: _Dims
    offset: float = 0.0
    canonical: str = ""        # the same unit with SI prefixes removed
    canonical_factor: float = 1.0


def _symbol(token: str) -> tuple[float, _Dims, str] | None:
    if token in _SYMBOLS:
        factor, dims, canonical, _ = _SYMBOLS[token]
        return factor, dims, canonical
    for prefix, scale in _PREFIXES.items():
        rest = token[len(prefix):]
        if token.startswith(prefix) and rest in _SYMBOLS and _SYMBOLS[rest][3]:
            factor, dims, canonical, _ = _SYMBOLS[rest]
            return scale * factor, dims, canonical
    return None


def parse_unit(expression: str) -> Unit | None:
    """The unit an expression denotes, or None when embeddyn does not know it."""
    text = (expression or "").strip()
    if not text:
        return Unit(1.0, _NONE)
    if text in _OFFSETS:
        factor, dims, canonical = _symbol(text)
        return Unit(factor, dims, _OFFSETS[text], canonical, factor)
    factor, canonical_factor, dims = 1.0, 1.0, [0] * 6
    parts: list[str] = []
    position = 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if not match or match.end() == position or bool(match.group(1)) != bool(parts):
            return None
        operator, token, power = match.group(1), match.group(2), int(match.group(3) or 1)
        symbol = _symbol(token)
        if symbol is None:
            return None
        sign = -1 if operator == "/" else 1
        token_factor, token_dims, canonical = symbol
        factor *= token_factor ** (sign * power)
        canonical_factor *= _SYMBOLS[canonical][0] ** (sign * power) if canonical in _SYMBOLS else 1.0
        dims = [d + sign * power * t for d, t in zip(dims, token_dims)]
        parts.append(("/" if operator == "/" else ("*" if parts else "")) + canonical + (f"^{power}" if power != 1 else ""))
        position = match.end()
    return Unit(factor, tuple(dims), 0.0, "".join(parts), canonical_factor)


def convert(value: float, source: str, target: str) -> float:
    """`value` in unit `source` expressed in unit `target`; ValueError if incompatible or unknown."""
    if source == target:
        return float(value)
    a, b = parse_unit(source), parse_unit(target)
    if a is None or b is None:
        raise ValueError(f"cannot convert {source!r} to {target!r}: unknown unit")
    if a.dims != b.dims:
        raise ValueError(f"cannot convert {source!r} to {target!r}: different dimensions")
    return ((float(value) * a.factor + a.offset) - b.offset) / b.factor


@dataclass(frozen=True)
class Quantity:
    value: float
    unit: str = ""

    @classmethod
    def parse(cls, raw: Any, default_unit: str = "") -> "Quantity":
        """A number, `"<number> <unit>"` or `{value, unit}`; converted to `default_unit` when one is given."""
        if isinstance(raw, Quantity):
            value, unit = raw.value, raw.unit
        elif isinstance(raw, (int, float)):
            return cls(float(raw), default_unit)
        elif isinstance(raw, dict):
            value, unit = float(raw.get("value", raw.get("value_si", 0.0))), str(raw.get("unit", "") or "")
        elif isinstance(raw, str):
            match = _QUANTITY.match(raw)
            if not match:
                raise ValueError(f"invalid quantity: {raw!r}")
            value, unit = float(match.group(1)), match.group(2)
        else:
            raise TypeError(f"unsupported quantity value: {type(raw).__name__}")
        return cls._resolve(value, unit, default_unit)

    @classmethod
    def _resolve(cls, value: float, unit: str, default_unit: str) -> "Quantity":
        if not unit or unit == default_unit:
            return cls(value, default_unit or unit)
        if default_unit:
            source, target = parse_unit(unit), parse_unit(default_unit)
            if source is None or target is None:
                return cls(value, unit)                  # unknown unit: kept as written, number unchanged
            if source.dims != target.dims:
                raise ValueError(f"'{value:g} {unit}' is not a quantity in {default_unit}")
            return cls(convert(value, unit, default_unit), default_unit)
        parsed = parse_unit(unit)
        if parsed is None or parsed.offset or not parsed.canonical or parsed.canonical == unit:
            return cls(value, unit)
        return cls(value * parsed.factor / parsed.canonical_factor, parsed.canonical)

    def to(self, unit: str) -> float:
        """The value expressed in `unit`."""
        return convert(self.value, self.unit, unit) if self.unit else float(self.value)

    def to_dict(self) -> dict[str, object]:
        return {"value": self.value, "unit": self.unit}
