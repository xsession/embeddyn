"""Indicator models: what an LED driven by a GPIO or PWM output looks like.

``LedBrightness`` turns the fraction of time a pin is high (the Renode ``gpio_duty`` binding, per step) into the
LED's perceived brightness 0..1: inverted for an active-low LED (anode on the supply), and averaged with a time
constant ``tau`` (default 20 ms, about what the eye integrates), so a 100 Hz PWM reads as its duty cycle instead of
flickering between 0 and 1 from step to step.
"""
from __future__ import annotations

from typing import Mapping, Sequence


class LedBrightness:
    inputs = ("duty",)
    outputs = ("brightness", "on")
    states = ("brightness",)

    def __init__(self, active_low: bool = False, tau: float = 0.02, threshold: float = 0.5, brightness: float = 0.0) -> None:
        if tau <= 0:
            raise ValueError("tau must be positive")
        self.active_low, self.tau, self.threshold, self.initial = active_low, tau, threshold, brightness

    def initial_state(self) -> list[float]:
        return [self.initial]

    def _target(self, u: Mapping[str, float]) -> float:
        duty = min(1.0, max(0.0, u.get("duty", 1.0 if self.active_low else 0.0)))
        return 1.0 - duty if self.active_low else duty

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        return [(self._target(u) - x[0]) / self.tau]

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        return {"brightness": x[0], "on": 1.0 if x[0] >= self.threshold else 0.0}
