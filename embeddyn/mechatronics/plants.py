"""Built-in plant models for the `python` backend (see adapters/python_model.py)."""
from __future__ import annotations

import math
from typing import Mapping, Sequence


class GearedDrivetrain:
    """Motor rotor -> gearbox with backlash -> compliant shaft -> load with friction.

    States: motor angle/speed (motor side) and load angle/speed (output side).
    The gear couples the motor angle divided by `gear_ratio` to the load through
    a spring-damper that only transmits torque outside the backlash gap.
    Load friction is viscous + Coulomb + Stribeck, regularised with tanh so the
    ODE stays smooth (`friction_speed` sets the regularisation width).
    An incremental encoder on the load counts `encoder_cpr` per revolution.
    """

    inputs = ("motor_torque", "load_torque")
    outputs = ("motor_angle", "motor_speed", "motor_acceleration", "load_angle", "load_speed",
               "shaft_torque", "friction_torque", "encoder_count", "backlash_contact",
               "dissipated_power", "stored_energy")
    states = ("motor_angle", "motor_speed", "load_angle", "load_speed")

    def __init__(self, motor_inertia: float = 0.002, motor_damping: float = 1e-4, gear_ratio: float = 10.0,
                 gear_efficiency: float = 1.0, backlash: float = 0.0, shaft_stiffness: float = 2000.0,
                 shaft_damping: float = 2.0, load_inertia: float = 0.5, load_damping: float = 0.05,
                 coulomb_friction: float = 0.0, static_friction: float | None = None, stribeck_speed: float = 0.05,
                 friction_speed: float = 1e-3, encoder_cpr: int = 0, motor_angle: float = 0.0, motor_speed: float = 0.0,
                 load_angle: float = 0.0, load_speed: float = 0.0) -> None:
        if motor_inertia <= 0 or load_inertia <= 0 or gear_ratio == 0:
            raise ValueError("inertias must be positive and gear_ratio non-zero")
        if backlash < 0 or not 0 < gear_efficiency <= 1:
            raise ValueError("backlash must be >= 0 and 0 < gear_efficiency <= 1")
        self.jm, self.bm, self.n, self.eta = motor_inertia, motor_damping, gear_ratio, gear_efficiency
        self.backlash, self.k, self.c = backlash, shaft_stiffness, shaft_damping
        self.jl, self.bl = load_inertia, load_damping
        self.tc = coulomb_friction
        self.ts = coulomb_friction if static_friction is None else static_friction
        self.ws, self.eps, self.cpr = stribeck_speed, friction_speed, int(encoder_cpr)
        self.x0 = [motor_angle, motor_speed, load_angle, load_speed]

    def initial_state(self) -> list[float]:
        return list(self.x0)

    def _shaft(self, x: Sequence[float]) -> tuple[float, bool]:
        twist = x[0] / self.n - x[2]
        half_gap = self.backlash / 2
        if abs(twist) <= half_gap:
            return 0.0, False
        rate = x[1] / self.n - x[3]
        torque = self.k * (twist - math.copysign(half_gap, twist)) + self.c * rate
        # A damper can't pull the teeth apart: no tension through an opening contact.
        if torque * twist < 0:
            torque = 0.0
        return torque, True

    def _friction(self, speed: float) -> float:
        level = self.tc + (self.ts - self.tc) * math.exp(-(speed / self.ws) ** 2) if self.ws > 0 else self.tc
        return level * math.tanh(speed / self.eps) + self.bl * speed

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        shaft, _ = self._shaft(x)
        motor_side = shaft / self.n / (self.eta if shaft * x[1] > 0 else 1.0)
        motor_acc = (u.get("motor_torque", 0.0) - self.bm * x[1] - motor_side) / self.jm
        load_acc = (shaft - self._friction(x[3]) - u.get("load_torque", 0.0)) / self.jl
        return [x[1], motor_acc, x[3], load_acc]

    def _energy(self, x: Sequence[float]) -> tuple[float, float]:
        """(dissipated power, stored energy) for energy-balance checks."""
        shaft, contact = self._shaft(x)
        rate = x[1] / self.n - x[3]
        twist = x[0] / self.n - x[2]
        compression = twist - math.copysign(self.backlash / 2, twist) if contact else 0.0
        damper = self.c * rate * rate if contact and shaft != 0.0 else 0.0
        gear_loss = (1 / self.eta - 1) * shaft * x[1] / self.n if shaft * x[1] > 0 else 0.0
        dissipated = self.bm * x[1] ** 2 + self._friction(x[3]) * x[3] + damper + gear_loss
        stored = 0.5 * self.jm * x[1] ** 2 + 0.5 * self.jl * x[3] ** 2 + 0.5 * self.k * compression ** 2
        return dissipated, stored

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        shaft, contact = self._shaft(x)
        derivative = self.derivatives(t, x, u)
        counts = math.floor(x[2] / (2 * math.pi) * self.cpr) if self.cpr else 0
        dissipated, stored = self._energy(x)
        return {
            "dissipated_power": dissipated, "stored_energy": stored,
            "motor_angle": x[0], "motor_speed": x[1], "motor_acceleration": derivative[1],
            "load_angle": x[2], "load_speed": x[3], "shaft_torque": shaft,
            "friction_torque": self._friction(x[3]), "encoder_count": float(counts),
            "backlash_contact": 1.0 if contact else 0.0,
        }


class ThermalRC:
    """First-order winding thermal model with copper loss from the current:

        C dT/dt = I^2 R(T) - (T - T_ambient) / R_th,   R(T) = R20 (1 + alpha (T - 20))

    `resistance` is fed back to the electrical model, closing the
    current -> heating -> resistance -> current loop.
    """

    inputs = ("current", "ambient")
    outputs = ("temperature", "resistance", "copper_loss")
    states = ("temperature",)

    def __init__(self, thermal_resistance: float = 2.0, thermal_capacitance: float = 50.0,
                 resistance_20c: float = 2.0, alpha: float = 0.00393, initial_temperature: float = 25.0) -> None:
        if thermal_resistance <= 0 or thermal_capacitance <= 0 or resistance_20c <= 0:
            raise ValueError("thermal resistance, capacitance and R20 must be positive")
        self.rth, self.cth, self.r20, self.alpha, self.t0 = (
            thermal_resistance, thermal_capacitance, resistance_20c, alpha, initial_temperature)

    def initial_state(self) -> list[float]:
        return [self.t0]

    def _resistance(self, temperature: float) -> float:
        return self.r20 * (1 + self.alpha * (temperature - 20.0))

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        loss = u.get("current", 0.0) ** 2 * self._resistance(x[0])
        return [(loss - (x[0] - u.get("ambient", 25.0)) / self.rth) / self.cth]

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        resistance = self._resistance(x[0])
        return {"temperature": x[0], "resistance": resistance, "copper_loss": u.get("current", 0.0) ** 2 * resistance}


class PIController:
    """Continuous PI speed controller with output clamping and anti-windup.

    Mappable inputs (`target`, `measured`), so several controllers can run as
    separate firmware-domain instances (the mock firmware is tied to fixed
    signal names).
    """

    inputs = ("target", "measured")
    outputs = ("command", "error")
    states = ("integral",)

    def __init__(self, kp: float = 0.12, ki: float = 0.8, minimum: float = -1.0, maximum: float = 1.0) -> None:
        if minimum >= maximum:
            raise ValueError("minimum must be below maximum")
        self.kp, self.ki, self.lo, self.hi = kp, ki, minimum, maximum

    def initial_state(self) -> list[float]:
        return [0.0]

    def _raw(self, x: Sequence[float], u: Mapping[str, float]) -> tuple[float, float]:
        error = u.get("target", 0.0) - u.get("measured", 0.0)
        return error, self.kp * error + self.ki * x[0]

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        error, raw = self._raw(x, u)
        # Conditional integration: stop integrating while saturated in the direction of the error.
        if (raw >= self.hi and error > 0) or (raw <= self.lo and error < 0):
            return [0.0]
        return [error]

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        error, raw = self._raw(x, u)
        return {"command": min(self.hi, max(self.lo, raw)), "error": error}


class DCMotorRL:
    """Armature circuit of a PWM-driven DC motor: L di/dt = duty*V - R i - Ke w.

    Outputs current, terminal voltage, electrical input power and torque Kt*i.
    """

    inputs = ("duty", "supply", "speed")
    outputs = ("current", "voltage", "power", "torque", "back_emf")
    states = ("current",)

    def __init__(self, resistance: float = 2.0, inductance: float = 0.003, ke: float = 0.08, kt: float = 0.08,
                 current: float = 0.0) -> None:
        if resistance <= 0 or inductance <= 0:
            raise ValueError("resistance and inductance must be positive")
        self.r, self.l, self.ke, self.kt, self.i0 = resistance, inductance, ke, kt, current

    def initial_state(self) -> list[float]:
        return [self.i0]

    def _voltage(self, u: Mapping[str, float]) -> float:
        return max(-1.0, min(1.0, u.get("duty", 0.0))) * u.get("supply", 24.0)

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        return [(self._voltage(u) - self.r * x[0] - self.ke * u.get("speed", 0.0)) / self.l]

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        voltage = self._voltage(u)
        return {"current": x[0], "voltage": voltage, "power": voltage * x[0], "torque": self.kt * x[0],
                "back_emf": self.ke * u.get("speed", 0.0)}


class RCServo:
    """Hobby RC servo: pulse decoder, servo amplifier, DC motor, gearbox, horn.

    The input is the width (seconds) of the last servo pulse, as measured from
    the controller's PWM output. `pulse_center` maps to 0 rad and each
    `pulse_per_rad` seconds of deviation to one radian (1.5 ms +- 1 ms ->
    +-90 deg by default). A width outside `pulse_valid` means no signal: the
    amplifier switches off and the output coasts.

    Inside the servo a proportional amplifier with a dead band (`deadband`,
    rad) and speed feedback (`amp_damping`) drives the motor, saturating at
    the supply. The armature inductance is neglected (its time constant is
    well below the mechanical one), so the current is algebraic. The gearbox
    has a ratio and efficiency; the output carries the horn/arm inertia,
    viscous and Coulomb friction (tanh-regularised), an external load torque
    and soft end stops at +-`travel_limit`.

    The feedback potentiometer gives `pot_volts_at_zero` + `pot_volts_per_rad`
    * angle, as on analog-feedback servos.
    """

    inputs = ("pulse_width", "load_torque", "supply")
    outputs = ("angle", "angle_deg", "speed", "target_angle", "current", "motor_voltage", "power",
               "feedback_voltage", "output_torque", "signal_valid")
    states = ("angle", "speed")

    def __init__(self, supply: float = 6.0, resistance: float = 4.0, kt: float = 0.0023, gear_ratio: float = 200.0,
                 gear_efficiency: float = 0.7, motor_inertia: float = 1e-7, load_inertia: float = 2e-4,
                 viscous_friction: float = 1e-3, coulomb_friction: float = 0.01, friction_speed: float = 0.05,
                 amp_gain: float = 150.0, amp_damping: float = 3.5, deadband: float = 0.008,
                 pulse_center: float = 1.5e-3, pulse_per_rad: float = 1.0e-3 / (math.pi / 2),
                 pulse_valid: Sequence[float] = (4.0e-4, 2.7e-3), travel_limit: float = math.radians(100),
                 stop_stiffness: float = 50.0, stop_damping: float = 0.5,
                 pot_volts_at_zero: float = 1.65, pot_volts_per_rad: float = 2.7 / math.pi,
                 angle: float = 0.0, speed: float = 0.0) -> None:
        if resistance <= 0 or gear_ratio <= 0 or pulse_per_rad <= 0:
            raise ValueError("resistance, gear_ratio and pulse_per_rad must be positive")
        self.supply, self.r, self.kt, self.n, self.eta = supply, resistance, kt, gear_ratio, gear_efficiency
        self.inertia = motor_inertia * gear_ratio ** 2 + load_inertia
        if self.inertia <= 0:
            raise ValueError("inertia must be positive")
        self.viscous, self.coulomb, self.friction_speed = viscous_friction, coulomb_friction, friction_speed
        self.kp, self.kd, self.deadband = amp_gain, amp_damping, deadband
        self.center, self.per_rad = pulse_center, pulse_per_rad
        self.valid = (float(pulse_valid[0]), float(pulse_valid[1]))
        self.limit, self.k_stop, self.d_stop = travel_limit, stop_stiffness, stop_damping
        self.pot0, self.pot_slope = pot_volts_at_zero, pot_volts_per_rad
        self.x0 = [angle, speed]

    def initial_state(self) -> list[float]:
        return list(self.x0)

    def _drive(self, x: Sequence[float], u: Mapping[str, float]) -> tuple[float, float, float, bool]:
        """Target angle, motor voltage, current and whether the pulse is valid."""
        width = u.get("pulse_width", 0.0)
        supply = u.get("supply", self.supply)
        if not self.valid[0] <= width <= self.valid[1]:
            return 0.0, 0.0, 0.0, False
        target = (width - self.center) / self.per_rad
        error = target - x[0]
        error = math.copysign(max(abs(error) - self.deadband, 0.0), error)
        voltage = max(-supply, min(supply, self.kp * error - self.kd * x[1]))
        current = (voltage - self.kt * self.n * x[1]) / self.r
        return target, voltage, current, True

    def _torques(self, x: Sequence[float], current: float, u: Mapping[str, float]) -> tuple[float, float]:
        angle, speed = x
        drive = self.eta * self.n * self.kt * current
        friction = self.viscous * speed + self.coulomb * math.tanh(speed / self.friction_speed)
        overshoot = abs(angle) - self.limit
        stop = 0.0
        if overshoot > 0:
            stop = -math.copysign(self.k_stop * overshoot, angle) - self.d_stop * speed
        return drive, drive - friction + stop - u.get("load_torque", 0.0)

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        _, _, current, _ = self._drive(x, u)
        return [x[1], self._torques(x, current, u)[1] / self.inertia]

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        target, voltage, current, valid = self._drive(x, u)
        drive, _ = self._torques(x, current, u)
        return {"angle": x[0], "angle_deg": math.degrees(x[0]), "speed": x[1], "target_angle": target,
                "current": current, "motor_voltage": voltage, "power": voltage * current,
                "feedback_voltage": self.pot0 + self.pot_slope * x[0], "output_torque": drive,
                "signal_valid": float(valid)}


class HBridgePWM:
    """Sign-magnitude H-bridge seen from its two PWM inputs: duty = in1 - in2.

    Algebraic (no states). Feeds `DCMotorRL.duty` when a controller drives the
    bridge's IN1/IN2 with two PWM channels, as on a real board.
    """

    inputs = ("in1", "in2")
    outputs = ("duty",)
    states = ()

    def __init__(self, shoot_through_is_brake: bool = True) -> None:
        self.brake = bool(shoot_through_is_brake)

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t, x, u):
        return []

    def output(self, t, x, u):
        in1 = min(1.0, max(0.0, u.get("in1", 0.0)))
        in2 = min(1.0, max(0.0, u.get("in2", 0.0)))
        return {"duty": in1 - in2}
