# Write a Python plant

A plant is any class with five members. Here is a linear actuator: a motor driving a lead screw that pushes a spring-loaded carriage with an end stop.

## 1. Write the class

```python title="plants/lead_screw.py"
import math

class LeadScrew:
    """Motor torque → lead screw (pitch p) → carriage of mass m against a spring k, with an end stop."""

    inputs = ("torque",)                                   # read each step, held constant
    outputs = ("position", "speed", "force", "at_stop")    # written each step
    states = ("position", "speed")                         # integrated with RK4

    def __init__(self, pitch=0.005, mass=0.5, rotor_inertia=2e-5, spring=200.0, damping=5.0,
                 stroke=0.05, stop_stiffness=1e6):
        self.ratio = 2 * math.pi / pitch                   # rad per metre
        self.m = mass + rotor_inertia * self.ratio ** 2    # rotor inertia reflected to the carriage
        self.k, self.c, self.stroke, self.ks = spring, damping, stroke, stop_stiffness

    def initial_state(self):
        return [0.0, 0.0]

    def _stop_force(self, x):
        return -self.ks * (x - self.stroke) if x > self.stroke else 0.0

    def derivatives(self, t, x, u):
        drive = u.get("torque", 0.0) * self.ratio          # torque → axial force
        force = drive - self.k * x[0] - self.c * x[1] + self._stop_force(x[0])
        return [x[1], force / self.m]

    def output(self, t, x, u):
        return {"position": x[0], "speed": x[1],
                "force": u.get("torque", 0.0) * self.ratio,
                "at_stop": 1.0 if x[0] > self.stroke else 0.0}
```

Rules:

* `derivatives` and `output` must be pure functions of `(t, x, u)`; keep no hidden state, or checkpoints and RK4 sub-steps break.
* Constructor arguments are the parameters; give them defaults.
* Use only the standard library if you want to [export it as an FMU](../theory/fmi.md#export-a-python-plant-as-an-fmu).
* Smooth discontinuities (`tanh` friction, stiff stops) rather than switching instantly; RK4 has no event detection.

## 2. Use it in a project

```yaml
instances:
  axis:
    domain: mechanical
    model: plants/lead_screw.py:LeadScrew        # file path relative to the project, or module:Class
    options:
      backend: python
      max_step: 1.0e-5                            # must resolve the stiffest mode (the end stop!)
      parameters: {pitch: 0.004, spring: "@axis.spring"}   # "@name" → project parameter
      inputs: {torque: motor.torque}
      outputs: {position: axis.position, speed: axis.speed, at_stop: axis.at_stop}
```

## 3. Choose `max_step`

The fastest mode sets the step. Here it is the end stop, and the effective mass matters: the rotor inertia reflected through the screw is \(J\,(2\pi/p)^2 = 2\times10^{-5} \cdot 1257^2 \approx 31.6\) kg, far more than the 0.5 kg carriage. So \(\omega = \sqrt{k_s/m_\text{eff}} = \sqrt{10^6/32.1} \approx 177\) rad/s, a period of 36 ms. RK4 wants ≥ 20 steps per period, so anything up to ~1.8 ms would do; `1e-5` is very safe. With a stiffer stop, a coarser pitch or a lighter rotor the mode gets faster, so recompute. If the state diverges anyway, the adapter stops the run with "state diverged at t=…; reduce max_step".

## 4. Test it alone

```python
from plants.lead_screw import LeadScrew
p = LeadScrew()
print(p.derivatives(0.0, [0.0, 0.0], {"torque": 0.01}))    # initial acceleration
```

Then run the project and check `verify` energy balance if the plant reports its losses.

## 5. Show it in the Lab

Python plants appear as parts automatically: their `inputs` and `outputs` become pins, constructor defaults appear as placeholders in the inspector.
