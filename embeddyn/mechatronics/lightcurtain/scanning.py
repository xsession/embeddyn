"""Scanning algorithms for light curtains.

A scan algorithm decides, slot by slot, which LEDs are lit and which
receivers are switched onto the amplifier, and turns the sampled levels into
"beam interrupted" decisions. It is written as a Python generator: it yields
a `Slot`, gets back a `Sample` (the receiver node voltage and comparator level
at the sampling instant) and finally returns one boolean per entry of
`self.beams` (True: dark). One `scan()` call is one frame.

    class MyScan(ScanAlgorithm):
        name = "mine"
        def setup(self):
            self.beams = [(k, k) for k in range(self.emitters)]
        def scan(self):
            dark = []
            for k in range(self.emitters):
                sample = yield Slot(lit=(k,), read=(k,))
                dark.append(not sample.detect)
            return dark

Projects name an algorithm by its registered name or as "package.module:Class".
`beams` lists the (emitter, receiver) lines a frame reports on; the bench uses
them for the ground truth and Webots draws them.

Hardware model: one receiver node (multiplexer or a shared current bus as in
chained receiver ICs). Lighting several LEDs or enabling several receivers in
one slot sums their light on that node.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Generator, Mapping

Scan = Generator["Slot", "Sample", list]


@dataclass(frozen=True)
class Slot:
    lit: tuple[int, ...]                # emitters on during the slot
    read: tuple[int, ...]               # receivers connected to the amplifier node
    duration: float | None = None       # s, default: the algorithm's slot time
    settle: float | None = None         # s after the slot start when the ADC/comparator samples
    tag: Any = None                     # free for the algorithm (e.g. beam index)
    drive: float = 1.0                  # relative LED current in this slot (e.g. 1/g for a group of g)


@dataclass(frozen=True)
class Sample:
    slot: Slot
    time: float                         # s, sampling instant
    voltage: float                      # V on the receiver node
    detect: bool                        # comparator: light received
    power: float                        # W, optical signal power at the sample (ground truth, not for decisions)


class ScanAlgorithm:
    """Base class; subclasses set `beams` in `setup()` and implement `scan()`."""
    name = "base"

    def __init__(self, emitters: int, receivers: int, slot: float = 200e-6, settle: float | None = None,
                 threshold: float | None = None, **params: Any) -> None:
        self.emitters, self.receivers = emitters, receivers
        self.slot, self.settle = slot, settle
        self.threshold = threshold          # V, for algorithms that compare voltages themselves
        self.params = params
        self.beams: list[tuple[int, int]] = []
        self.setup()

    def setup(self) -> None:
        self.beams = [(k, k) for k in range(min(self.emitters, self.receivers))]

    def scan(self) -> Scan:  # pragma: no cover - abstract
        raise NotImplementedError
        yield

    def light(self, sample: Sample) -> bool:
        """Light received: the comparator, or the ADC voltage against `threshold`."""
        return sample.voltage >= self.threshold if self.threshold is not None else sample.detect

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "beams": len(self.beams), "slot": self.slot, "settle": self.settle, **self.params}


class Sequential(ScanAlgorithm):
    """One LED at a time with its opposite receiver: the classic scan."""
    name = "sequential"

    def setup(self):
        order = self.params.get("order", "linear")
        n = min(self.emitters, self.receivers)
        if order == "interleaved":          # 0, n/2, 1, n/2+1, ...: far neighbours follow each other
            half = (n + 1) // 2
            self.order = [k for i in range(half) for k in (i, i + half) if k < n]
        else:
            self.order = list(range(n))
        self.beams = [(k, k) for k in range(n)]

    def scan(self):
        dark = [False] * len(self.beams)
        for k in self.order:
            sample = yield Slot((k,), (k,))
            dark[k] = not self.light(sample)
        return dark


class Crossed(ScanAlgorithm):
    """Each LED is read by its own receiver and by `reach` neighbours on both sides.

    The diagonal beams cross between the straight ones, so an object smaller than
    the pitch is seen by a diagonal even where it slips between straight beams.
    Costs (2 reach + 1) slots per LED; needs LEDs and receivers wide enough to see
    each other at the diagonal angle atan(reach * pitch / span).
    """
    name = "crossed"

    def setup(self):
        reach = int(self.params.get("reach", 1))
        self.beams = [(e, r) for e in range(self.emitters) for r in range(e - reach, e + reach + 1)
                      if 0 <= r < self.receivers]

    def scan(self):
        dark = []
        for e, r in self.beams:
            sample = yield Slot((e,), (r,))
            dark.append(not self.light(sample))
        return dark


class Group(ScanAlgorithm):
    """Coarse-to-fine: light a group of LEDs and enable their receivers together.

    A clear group passes in one slot; a group whose summed signal drops below
    `group_threshold` times the clear level (learnt beam by beam in the first
    frame) is re-scanned beam by beam. Fast while the curtain is clear.

    On a shared receiver node every enabled photodiode also sees the other lit
    LEDs, so a group of g collects up to g^2 beams' worth of light and one dark
    straight beam removes only about 1/g^2 of it. Defaults follow from that:
    each LED runs at `drive` = 1/g^2 so the node stays near one beam's level,
    and the threshold sits half a beam below the clear level, 1 - 0.5/g^2.
    Whether the LEDs give enough margin over noise and ambient flicker for that
    is exactly what the bench measures.
    """
    name = "group"

    def setup(self):
        self.size = int(self.params.get("size", 4))
        self.fraction = float(self.params.get("group_threshold", 1.0 - 0.5 / self.size ** 2))
        self.drive = float(self.params.get("drive", 1.0 / self.size ** 2))
        n = min(self.emitters, self.receivers)
        self.beams = [(k, k) for k in range(n)]
        self.groups = [tuple(range(i, min(i + self.size, n))) for i in range(0, n, self.size)]
        self.reference: dict[int, float] = {}

    def scan(self):
        dark = [False] * len(self.beams)
        for g, members in enumerate(self.groups):
            sample = yield Slot(members, members, tag=("group", g), drive=self.drive)
            reference = self.reference.get(g)
            if reference is None:               # first frame: learn every member, then the group level
                clear = True
                for k in members:
                    single = yield Slot((k,), (k,), tag=("beam", k))
                    dark[k] = not self.light(single)
                    clear &= not dark[k]
                if clear:
                    self.reference[g] = sample.voltage
                continue
            if sample.voltage >= self.fraction * reference:
                continue
            for k in members:
                single = yield Slot((k,), (k,), tag=("beam", k))
                dark[k] = not self.light(single)
        return dark


ALGORITHMS: dict[str, type[ScanAlgorithm]] = {cls.name: cls for cls in (Sequential, Crossed, Group)}


def make_algorithm(spec: str | Mapping[str, Any], emitters: int, receivers: int, slot: float = 200e-6,
                   settle: float | None = None, threshold: float | None = None, **params: Any) -> ScanAlgorithm:
    """Instantiate an algorithm by registered name or "module:Class"."""
    if isinstance(spec, Mapping):
        spec = dict(spec)
        name = str(spec.pop("name"))
        params = {**spec, **params}
    else:
        name = str(spec)
    if name in ALGORITHMS:
        cls = ALGORITHMS[name]
    elif ":" in name:
        module, _, attr = name.partition(":")
        cls = getattr(importlib.import_module(module), attr)
        if not (isinstance(cls, type) and issubclass(cls, ScanAlgorithm)):
            raise ValueError(f"{name} is not a ScanAlgorithm subclass")
    else:
        raise ValueError(f"unknown scan algorithm {name!r}; built in: {', '.join(ALGORITHMS)}; or 'module:Class'")
    return cls(emitters, receivers, slot=slot, settle=settle, threshold=threshold, **params)
