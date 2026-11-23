"""MuJoCo multibody physics as a co-simulation instance (``backend: mujoco``).

The model is an MJCF file. MuJoCo runs in-process: each co-simulation step
advances ``mj_step`` by whole MuJoCo timesteps (the macro step must be a
multiple of ``<option timestep>``), and the full integration state is saved
and restored through ``mj_getState``/``mj_setState``, so runs can be
checkpointed and replayed bit for bit.

```yaml
arm:
  domain: mechanical
  model: mujoco/servo-arm.xml
  options:
    backend: mujoco
    inputs:                    # model address -> project signal (read each step)
      ctrl:shaft: servo.output_torque      # actuator control (a torque for <motor>)
      qfrc:hinge: arm.disturbance          # generalised force applied to a joint
    outputs:                   # model address -> project signal (written each step)
      qpos:hinge: arm.angle
      qvel:hinge: arm.speed
      qfrc_act:hinge: arm.joint_torque
      sensor:tip_acc[2]: arm.tip_acc_z
      xpos:tip[0]: arm.tip_x
```

Addresses: ``ctrl:<actuator>``, ``qfrc:<joint>`` (inputs);
``qpos:<joint>``, ``qvel:<joint>``, ``qfrc_act:<joint>`` (joint-side torque from
all actuators, i.e. after the gear), ``sensor:<name>``, ``act:<actuator>``
(actuator-side force, before the gear), ``xpos:<body>`` (world position) and
``ctrl:<actuator>`` (outputs). ``[i]`` picks a component of a vector quantity (default 0).
When ngspice or a python plant already models the motor winding, drive a plain
``<motor>`` actuator with torque; MuJoCo's ``dcmotor`` would count the
electrical dynamics twice.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from .base import SimulatorAdapter, StepResult

_ADDRESS = re.compile(r"^(?P<kind>ctrl|qfrc_act|qfrc|qpos|qvel|sensor|act|xpos):(?P<name>[^\[\]]+)(?:\[(?P<index>\d+)\])?$")


def _mujoco():
    try:
        import mujoco
    except ImportError as error:              # pragma: no cover - depends on the environment
        raise RuntimeError("backend: mujoco needs the `mujoco` package (pip install mujoco)") from error
    return mujoco


class MujocoAdapter(SimulatorAdapter):
    name = "mujoco"

    def __init__(self, model: str, inputs: Mapping[str, str] | None = None,
                 outputs: Mapping[str, str] | None = None, timestep: float | None = None) -> None:
        mujoco = _mujoco()
        self.mujoco = mujoco
        self.model_path = Path(model)
        if not self.model_path.is_file():
            raise FileNotFoundError(f"MuJoCo model not found: {self.model_path}")
        self.model = mujoco.MjModel.from_xml_path(str(self.model_path))
        if timestep is not None:
            if not timestep > 0:
                raise ValueError("timestep must be positive")
            self.model.opt.timestep = float(timestep)
        self.data = None
        self.input_map = dict(inputs or {})
        self.output_map = dict(outputs or {})
        self._writers = [(self._writer(address), signal) for address, signal in self.input_map.items()]
        self._readers = [(self._reader(address), signal) for address, signal in self.output_map.items()]
        self.state_spec = mujoco.mjtState.mjSTATE_INTEGRATION

    # -- address resolution --------------------------------------------------------
    def _parse(self, address: str) -> tuple[str, str, int]:
        match = _ADDRESS.match(address)
        if not match:
            raise ValueError(f"MuJoCo address {address!r} must look like kind:name or kind:name[i] "
                             "(kinds: ctrl, qfrc, qpos, qvel, qfrc_act, sensor, act, xpos)")
        return match["kind"], match["name"], int(match["index"] or 0)

    def _id(self, kind: Any, name: str, what: str) -> int:
        index = self.mujoco.mj_name2id(self.model, kind, name)
        if index < 0:
            raise ValueError(f"{self.model_path.name} has no {what} named {name!r}")
        return index

    def _joint_slot(self, name: str, index: int, velocity: bool) -> int:
        joint = self._id(self.mujoco.mjtObj.mjOBJ_JOINT, name, "joint")
        start = (self.model.jnt_dofadr if velocity else self.model.jnt_qposadr)[joint]
        kinds = self.mujoco.mjtJoint
        jtype = self.model.jnt_type[joint]
        width = {kinds.mjJNT_FREE: 6 if velocity else 7, kinds.mjJNT_BALL: 3 if velocity else 4}.get(jtype, 1)
        if index >= width:
            raise ValueError(f"joint {name} has {width} {'velocity' if velocity else 'position'} components")
        return int(start + index)

    def _writer(self, address: str) -> Callable[[float], None]:
        kind, name, index = self._parse(address)
        if kind == "ctrl":
            actuator = self._id(self.mujoco.mjtObj.mjOBJ_ACTUATOR, name, "actuator")
            return lambda value: self.data.ctrl.__setitem__(actuator, value)
        if kind == "qfrc":
            dof = self._joint_slot(name, index, velocity=True)
            return lambda value: self.data.qfrc_applied.__setitem__(dof, value)
        raise ValueError(f"MuJoCo input {address!r}: inputs must be ctrl:<actuator> or qfrc:<joint>")

    def _reader(self, address: str) -> Callable[[], float]:
        kind, name, index = self._parse(address)
        objects = self.mujoco.mjtObj
        if kind in ("qpos", "qvel"):
            slot = self._joint_slot(name, index, velocity=kind == "qvel")
            return (lambda: self.data.qpos[slot]) if kind == "qpos" else (lambda: self.data.qvel[slot])
        if kind == "qfrc_act":
            dof = self._joint_slot(name, index, velocity=True)
            return lambda: self.data.qfrc_actuator[dof]
        if kind == "qfrc":
            raise ValueError(f"MuJoCo output {address!r}: qfrc is an input; read qfrc_act:<joint> for actuator torque")
        if kind == "sensor":
            sensor = self._id(objects.mjOBJ_SENSOR, name, "sensor")
            if index >= self.model.sensor_dim[sensor]:
                raise ValueError(f"sensor {name} has {self.model.sensor_dim[sensor]} components")
            slot = int(self.model.sensor_adr[sensor] + index)
            return lambda: self.data.sensordata[slot]
        if kind in ("act", "ctrl"):
            actuator = self._id(objects.mjOBJ_ACTUATOR, name, "actuator")
            return (lambda: self.data.actuator_force[actuator]) if kind == "act" else (lambda: self.data.ctrl[actuator])
        body = self._id(objects.mjOBJ_BODY, name, "body")
        if index > 2:
            raise ValueError("xpos has 3 components")
        return lambda: self.data.xpos[body][index]

    # -- lifecycle ----------------------------------------------------------------------
    def start(self, initial_signals: Mapping[str, float]) -> None:
        self.data = self.mujoco.MjData(self.model)
        self.mujoco.mj_forward(self.model, self.data)

    def _apply(self, inputs: Mapping[str, float]) -> None:
        for write, signal in self._writers:
            value = float(inputs.get(signal, 0.0))
            if not math.isfinite(value):
                raise FloatingPointError(f"MuJoCo input {signal} is not finite")
            write(value)

    def step(self, time_seconds: float, step_seconds: float, inputs: Mapping[str, float]) -> StepResult:
        if self.data is None:
            raise RuntimeError("MuJoCo adapter is not started")
        self._apply(inputs)
        dt = self.model.opt.timestep
        span = float(time_seconds) - self.data.time
        steps = round(span / dt)
        if span > 0 and abs(steps * dt - span) > 1e-9 * max(1.0, span) + 1e-12:
            raise ValueError(f"co-simulation step {span:g} s is not a multiple of the MuJoCo timestep {dt:g} s")
        for _ in range(max(0, steps)):
            self.mujoco.mj_step(self.model, self.data)
        if steps > 0:
            self.data.time = float(time_seconds)          # no accumulated float drift against the orchestrator
        self.mujoco.mj_forward(self.model, self.data)     # sensors/positions consistent with the new state
        outputs = {signal: float(read()) for read, signal in self._readers}
        if any(not math.isfinite(v) for v in outputs.values()):
            raise FloatingPointError(f"MuJoCo diverged at t={self.data.time:.6g}; reduce the timestep")
        return StepResult(outputs, {"substeps": max(0, steps)})

    def checkpoint(self) -> object:
        size = self.mujoco.mj_stateSize(self.model, self.state_spec)
        import numpy as np
        state = np.empty(size)
        self.mujoco.mj_getState(self.model, self.data, state, self.state_spec)
        return {"spec": "integration", "state": [float(x) for x in state]}

    def restore(self, checkpoint: object) -> None:
        import numpy as np
        data = dict(checkpoint or {})
        if self.data is None:
            self.data = self.mujoco.MjData(self.model)
        state = np.asarray(data["state"], dtype=float)
        if state.size != self.mujoco.mj_stateSize(self.model, self.state_spec):
            raise ValueError("MuJoCo checkpoint does not match this model")
        self.mujoco.mj_setState(self.model, self.data, state, self.state_spec)
        self.mujoco.mj_forward(self.model, self.data)

    def capabilities(self) -> dict[str, bool]:
        return {**super().capabilities(), "checkpoint": True, "variable_step": False, "mechanical_contacts": True}

    def stop(self) -> None:
        self.data = None
