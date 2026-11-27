"""One IR beam as a python plant, for co-simulating a single curtain channel's electronics.

`IrBeamPath` turns the emitter LED current into phototransistor base current:
``photo_current = coupling * transmission * led_current + dark_current``. The
coupling (A of photo base current per A of LED current) lumps the LED
efficiency, the geometry of `geometric_gain` and the detector responsivity;
`transmission` (0 = blocked, 1 = clear) comes from an obstacle model or a
stimulus. It is the electrical counterpart of a single pencil of
`HeadScanner`, small enough to run with an ngspice front end
(examples/mechatronics/ir-curtain-channel.yaml).
"""
from __future__ import annotations

from typing import Mapping, Sequence


class IrBeamPath:
    inputs = ("led_current", "transmission")
    outputs = ("photo_current",)
    states = ()

    def __init__(self, coupling: float = 2.0e-4, dark_current: float = 1.0e-9) -> None:
        if coupling < 0 or dark_current < 0:
            raise ValueError("coupling and dark_current must be >= 0")
        self.coupling, self.dark_current = float(coupling), float(dark_current)

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        return []

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        transmission = min(1.0, max(0.0, float(u.get("transmission", 1.0))))
        led = max(0.0, float(u.get("led_current", 0.0)))
        return {"photo_current": self.coupling * transmission * led + self.dark_current}
