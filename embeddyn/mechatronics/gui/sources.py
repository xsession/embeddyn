"""Signal sources for the Lab's parts library, written as ordinary python plants.

They run inside the co-simulation (backend: python), so their output is exact at
every macro step and a saved project runs the same from the command line.
"""
from __future__ import annotations

import math
from typing import Mapping, Sequence

WAVEFORMS = ("sine", "square", "triangle", "sawtooth", "step", "ramp", "pulse", "dc")


class FunctionGenerator:
    """Function generator: `offset + amplitude * waveform(frequency * (t - delay) + phase)`.

    `step` and `ramp` start at `delay`; `pulse` is high for `duty` of each
    period. The optional `gate` input multiplies the AC part (0 = off).
    """

    inputs = ("gate",)
    outputs = ("value",)
    states = ()

    def __init__(self, waveform: str = "sine", amplitude: float = 1.0, offset: float = 0.0,
                 frequency: float = 1.0, phase: float = 0.0, delay: float = 0.0, duty: float = 0.5) -> None:
        if waveform not in WAVEFORMS:
            raise ValueError(f"waveform must be one of {', '.join(WAVEFORMS)}")
        if frequency < 0 or not 0 <= duty <= 1:
            raise ValueError("frequency must be >= 0 and duty within 0..1")
        self.waveform, self.amplitude, self.offset = waveform, float(amplitude), float(offset)
        self.frequency, self.phase, self.delay, self.duty = float(frequency), float(phase), float(delay), float(duty)

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        return []

    def shape(self, t: float) -> float:
        tau = t - self.delay
        if self.waveform == "dc":
            return 1.0
        if self.waveform == "step":
            return 1.0 if tau >= 0 else 0.0
        if self.waveform == "ramp":
            return max(tau, 0.0) * (self.frequency or 1.0)
        cycle = (self.frequency * tau + self.phase) % 1.0
        if self.waveform == "sine":
            return math.sin(2 * math.pi * cycle)
        if self.waveform == "square":
            return 1.0 if cycle < 0.5 else -1.0
        if self.waveform == "pulse":
            return 1.0 if cycle < self.duty and tau >= 0 else 0.0
        if self.waveform == "triangle":
            return 4 * cycle - 1 if cycle < 0.5 else 3 - 4 * cycle
        return 2 * cycle - 1                                         # sawtooth

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        gate = u.get("gate", 1.0)
        return {"value": self.offset + gate * self.amplitude * self.shape(t)}


class ServoPulseGenerator:
    """Hobby-RC transmitter channel: angle command (deg) -> servo pulse width (s).

    A software stand-in for a servo controller when no firmware is in the loop.
    """

    inputs = ("angle_deg",)
    outputs = ("pulse_width",)
    states = ()

    def __init__(self, center: float = 1.5e-3, per_degree: float = 1.0e-3 / 90, minimum: float = 0.5e-3,
                 maximum: float = 2.5e-3) -> None:
        self.center, self.per_degree, self.minimum, self.maximum = center, per_degree, minimum, maximum

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t, x, u):
        return []

    def output(self, t, x, u):
        width = self.center + u.get("angle_deg", 0.0) * self.per_degree
        return {"pulse_width": min(self.maximum, max(self.minimum, width))}
