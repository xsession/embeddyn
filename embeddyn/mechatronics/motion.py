"""Motion sources for boards with inertial and pressure sensors.

``PendulumMount`` is a board fixed at ``length`` from a horizontal pivot and swinging under gravity (a simple
pendulum with viscous damping and an optional drive acceleration). It gives what an IMU and a barometer on the
board measure, in the board frame: X along the swing (tangential), Y along the pivot axis, Z towards the pivot
(so a board hanging at rest reads +1 g on Z, like a board lying flat):

* ``acc_x/acc_y/acc_z`` specific force (m/s^2): the acceleration minus gravity
* ``gyro_x/gyro_y/gyro_z`` angular rate (rad/s)
* ``pressure`` (Pa) at the board's height (ISA troposphere from ``altitude`` at the pivot), ``temperature`` (degC)

``axes`` maps board axes to the sensor's: e.g. "y,-x,z" when the sensor is rotated by 90 degrees on the PCB.
"""
from __future__ import annotations

import math
from typing import Mapping, Sequence


def isa_pressure(height_m: float, sea_level_pa: float = 101325.0) -> float:
    return sea_level_pa * (1.0 - 2.25577e-5 * height_m) ** 5.25588


def _axis_map(spec: str) -> list[tuple[int, float]]:
    mapping = []
    for item in spec.replace(" ", "").split(","):
        sign = -1.0 if item.startswith("-") else 1.0
        mapping.append(("xyz".index(item.lstrip("+-").lower()), sign))
    if sorted(index for index, _ in mapping) != [0, 1, 2]:
        raise ValueError(f"axes must be a permutation of x,y,z with signs, got {spec!r}")
    return mapping


class PendulumMount:
    inputs = ("drive",)
    outputs = ("angle", "angle_deg", "rate", "acc_x", "acc_y", "acc_z", "gyro_x", "gyro_y", "gyro_z", "pressure",
               "temperature", "height")
    states = ("angle", "rate")

    def __init__(self, length: float = 0.5, damping: float = 0.05, angle: float = 30.0, rate: float = 0.0,
                 gravity: float = 9.80665, altitude: float = 120.0, temperature: float = 22.0,
                 sea_level_pressure: float = 101325.0, axes: str = "x,y,z") -> None:
        if length <= 0:
            raise ValueError("length must be positive")
        self.length, self.damping, self.g = length, damping, gravity
        self.initial = (math.radians(angle), rate)
        self.altitude, self.temperature, self.sea_level = altitude, temperature, sea_level_pressure
        self.axes = _axis_map(axes)

    def initial_state(self) -> list[float]:
        return list(self.initial)

    def _alpha(self, x: Sequence[float], u: Mapping[str, float]) -> float:
        return -(self.g / self.length) * math.sin(x[0]) - self.damping * x[1] + u.get("drive", 0.0)

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        return [x[1], self._alpha(x, u)]

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        theta, omega = x[0], x[1]
        alpha = self._alpha(x, u)
        # specific force in the board frame (X tangential, Y pivot axis, Z towards the pivot)
        board = (self.length * alpha + self.g * math.sin(theta), 0.0,
                 self.length * omega * omega + self.g * math.cos(theta))
        rate = (0.0, -omega, 0.0)
        acc = [board[index] * sign for index, sign in self.axes]
        gyro = [rate[index] * sign for index, sign in self.axes]
        height = self.altitude - self.length * math.cos(theta)
        return {"angle": theta, "angle_deg": math.degrees(theta), "rate": omega,
                "acc_x": acc[0], "acc_y": acc[1], "acc_z": acc[2],
                "gyro_x": gyro[0], "gyro_y": gyro[1], "gyro_z": gyro[2],
                "pressure": isa_pressure(height, self.sea_level), "temperature": self.temperature, "height": height}
