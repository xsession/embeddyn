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
    initial:                   # optional start state, set before the first step
      qpos:hinge: 0.6
```

Addresses: ``ctrl:<actuator>``, ``qfrc:<joint>`` (inputs);
``qpos:<joint>``, ``qvel:<joint>``, ``qfrc_act:<joint>`` (joint-side torque from
all actuators, i.e. after the gear), ``sensor:<name>``, ``act:<actuator>``
(actuator-side force, before the gear), ``xpos:<body>`` (world position) and
``ctrl:<actuator>`` (outputs). ``[i]`` picks a component of a vector quantity (default 0).
``initial`` accepts ``qpos:<joint>`` and ``qvel:<joint>``. Give the matching
signals the same ``initial`` value, because other instances read the signal
before MuJoCo's first step.
When ngspice or a python plant already models the motor winding, drive a plain
``<motor>`` actuator with torque; MuJoCo's ``dcmotor`` would count the
electrical dynamics twice.

Optical sensing (``light_curtain``, see ``optics.py``): IR light curtains whose
emitters and receivers are MJCF sites. The firmware selects one beam at a time
through an address bus (an integer signal, e.g. a GPIO port's output register);
the adapter casts the beam against the scene with ``mj_ray`` each step and
writes the multiplexed receiver voltage and comparator level::

    light_curtain:
      heads:                               # beams are numbered head by head
        - {emitters: a_em, receivers: a_rx, count: 16, body: head_a}   # sites a_em0..15, a_rx0..15
        - {emitters: b_em, receivers: b_rx, count: 16, body: head_b}
      address: {signal: mcu.curtain_port, shift: 0, bits: 5}          # beam = (port >> shift) & 31
      enable: {signal: mcu.curtain_port, bit: 5}                       # emitters off while 0
      voltage: curtain.rx_voltage          # receiver voltage after the multiplexer (to an ADC)
      detect: curtain.rx_detect            # comparator: 1 = light received (to a GPIO input)
      blocked: [curtain.blocked_a, curtain.blocked_b]   # optional: blocked beams per head (centre rays)
      blocked_every: 1 ms                  # how often `blocked` is recomputed (default 1 ms)
      ray_refresh: 200 us                  # reuse a beam's ray result this long (default 0: every step)
      geomgroup: [1, 1, 1, 0, 0, 0]        # geom groups that block light (default all)
      optics: {intensity: 10 mW/sr, emitter_half_angle: 10 deg, ambient: 1 W/m^2, tau: 20 us}

Emitter sites point their z axis at the receiver row and receiver sites at the
emitters; a tilted site loses signal like a misaligned part. ``body`` (the
head carrying the curtain) is excluded from its rays.

``clearance`` writes the smallest distance between two groups of geoms (signed:
negative means penetration), for example a detector head and the patient::

    clearance:
      - {signal: gantry.gap_a, geoms: [a_face, a_rail_e, a_rail_r], to: [torso, arm_l, arm_r, couch], max: 0.5}
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from .base import SimulatorAdapter, StepResult

_ADDRESS = re.compile(r"^(?P<kind>ctrl|qfrc_act|qfrc|qpos|qvel|sensor|act|xpos):(?P<name>[^\[\]]+)(?:\[(?P<index>\d+)\])?$")


def _seconds(raw: Any) -> float:
    from ..quantities import Quantity
    return Quantity.parse(raw, "s").value


def _mujoco():
    try:
        import mujoco
    except ImportError as error:              # pragma: no cover - depends on the environment
        raise RuntimeError("backend: mujoco needs the `mujoco` package (pip install mujoco)") from error
    return mujoco


class MujocoAdapter(SimulatorAdapter):
    name = "mujoco"

    def __init__(self, model: str, inputs: Mapping[str, str] | None = None,
                 outputs: Mapping[str, str] | None = None, timestep: float | None = None,
                 initial: Mapping[str, float] | None = None, light_curtain: Mapping[str, Any] | None = None,
                 clearance: list[Mapping[str, Any]] | None = None) -> None:
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
        self._initial = []
        for address, value in dict(initial or {}).items():
            kind, name, index = self._parse(address)
            if kind not in ("qpos", "qvel"):
                raise ValueError(f"MuJoCo initial {address!r}: only qpos:<joint> and qvel:<joint> can be set")
            self._initial.append((kind, self._joint_slot(name, index, velocity=kind == "qvel"), float(value)))
        self._curtain_spec = self._parse_curtain(light_curtain) if light_curtain else None
        self.curtain = None
        self._clearance = [self._parse_clearance(item) for item in clearance or []]

    # -- optics ---------------------------------------------------------------------------
    def _parse_curtain(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        from ..optics import CurtainOptics
        objects = self.mujoco.mjtObj
        heads = []
        for index, head in enumerate(raw.get("heads") or []):
            count = int(head.get("count", 0))
            if count < 1:
                raise ValueError(f"light curtain head {index}: needs count >= 1")
            emitters = [self._id(objects.mjOBJ_SITE, f"{head['emitters']}{k}", "site") for k in range(count)]
            receivers = [self._id(objects.mjOBJ_SITE, f"{head['receivers']}{k}", "site") for k in range(count)]
            body = self._id(objects.mjOBJ_BODY, str(head["body"]), "body") if head.get("body") else -1
            heads.append({"emitters": emitters, "receivers": receivers, "body": body})
        if not heads:
            raise ValueError("light_curtain needs at least one head")
        address = dict(raw.get("address") or {})
        if "signal" not in address:
            raise ValueError("light_curtain needs address: {signal: ..., bits: n}")
        beams = sum(len(head["emitters"]) for head in heads)
        bits = int(address.get("bits", max(1, (beams - 1).bit_length())))
        if (1 << bits) < beams:
            raise ValueError(f"light_curtain: {bits} address bits cannot select {beams} beams")
        if not raw.get("voltage") and not raw.get("detect"):
            raise ValueError("light_curtain needs a voltage and/or detect output signal")
        blocked = list(raw.get("blocked") or [])
        if blocked and len(blocked) != len(heads):
            raise ValueError("light_curtain: blocked needs one signal per head")
        group = raw.get("geomgroup")
        import numpy as np
        return {
            "heads": heads, "address": str(address["signal"]), "shift": int(address.get("shift", 0)),
            "mask": (1 << bits) - 1, "enable": dict(raw["enable"]) if raw.get("enable") else None,
            "voltage": raw.get("voltage"), "detect": raw.get("detect"), "blocked": blocked,
            "geomgroup": None if group is None else np.array([1 if g else 0 for g in group], dtype=np.uint8),
            "optics": CurtainOptics.from_mapping(raw.get("optics")),
            "beam_head": [h for h, head in enumerate(heads) for _ in head["emitters"]],
            "beam_index": [k for head in heads for k in range(len(head["emitters"]))],
            "blocked_every": _seconds(raw.get("blocked_every", 1e-3)),
            "ray_refresh": _seconds(raw.get("ray_refresh", 0.0)),
        }

    def _parse_clearance(self, raw: Mapping[str, Any]) -> tuple[str, list[int], list[int], float]:
        geom = self.mujoco.mjtObj.mjOBJ_GEOM
        if not raw.get("signal") or not raw.get("geoms") or not raw.get("to"):
            raise ValueError("clearance entries need signal, geoms and to")
        return (str(raw["signal"]), [self._id(geom, str(g), "geom") for g in raw["geoms"]],
                [self._id(geom, str(g), "geom") for g in raw["to"]], float(raw.get("max", 1.0)))

    def _raycast(self, start, end, head: int) -> bool:
        import numpy as np
        spec = self._curtain_spec
        origin = np.asarray(start, dtype=float)
        vec = np.asarray(end, dtype=float) - origin
        hit = self.mujoco.mj_ray(self.model, self.data, origin, vec, spec["geomgroup"], 1,
                                 spec["heads"][head]["body"], self._geomid)
        return 0.0 <= hit < 1.0

    def _cast_from(self, start, ends, head: int) -> int:
        """How many segments from `start` to each of `ends` are blocked (one mj_multiRay call)."""
        import numpy as np
        spec = self._curtain_spec
        origin = np.asarray(start, dtype=float)
        vectors = np.asarray(ends, dtype=float) - origin
        lengths = np.linalg.norm(vectors, axis=1)
        count = len(lengths)
        if self._multi_dist.shape[0] != count:
            self._multi_dist = np.zeros(count)
            self._multi_geom = np.zeros(count, dtype=np.int32)
        self.mujoco.mj_multiRay(self.model, self.data, origin, (vectors / lengths[:, None]).ravel(),
                                spec["geomgroup"], 1, spec["heads"][head]["body"], self._multi_geom,
                                self._multi_dist, None, count, float(lengths.max()))
        return int(np.count_nonzero((self._multi_dist >= 0) & (self._multi_dist < lengths)))

    def _beam(self, number: int):
        from ..optics import Beam
        spec = self._curtain_spec
        head_index, k = spec["beam_head"][number], spec["beam_index"][number]
        head = spec["heads"][head_index]
        e, r = head["emitters"][k], head["receivers"][k]
        def z_axis(site: int) -> list[float]:           # third column of the row-major frame
            xmat = self.data.site_xmat[site]
            return [float(xmat[2]), float(xmat[5]), float(xmat[8])]
        return Beam([float(x) for x in self.data.site_xpos[e]], [float(x) for x in self.data.site_xpos[r]],
                    head_index, z_axis(e), z_axis(r))

    def _selected_beam(self, inputs: Mapping[str, float]) -> int | None:
        spec = self._curtain_spec
        port = int(round(float(inputs.get(spec["address"], 0.0))))
        enable = spec["enable"]
        if enable is not None:
            level = int(round(float(inputs.get(str(enable["signal"]), 0.0))))
            if not (level >> int(enable.get("bit", 0))) & 1:
                return None
        number = (port >> spec["shift"]) & spec["mask"]
        return number if number < len(spec["beam_head"]) else None

    def _curtain_outputs(self, inputs: Mapping[str, float], dt: float) -> dict[str, float]:
        spec = self._curtain_spec
        number = self._selected_beam(inputs)
        self.curtain.selected = number
        beam = self._beam(number) if number is not None else None
        now = float(self.data.time)
        cached = self._ray_cache.get(number)
        if beam is not None and cached is not None and now - cached[0] < spec["ray_refresh"] - 1e-12:
            self.curtain.update(beam, self._raycast, now, dt, transmittance=cached[1])
        else:
            reading = self.curtain.update(beam, self._raycast, now, dt, self._cast_from)
            if reading is not None and spec["ray_refresh"] > 0:
                self._ray_cache[number] = (now, reading.transmittance)
        out: dict[str, float] = {}
        if spec["voltage"]:
            out[spec["voltage"]] = self.curtain.voltage
        if spec["detect"]:
            out[spec["detect"]] = 1.0 if self.curtain.detect else 0.0
        if spec["blocked"]:
            now = float(self.data.time)
            if self._blocked is None or now + 1e-12 >= self._blocked_at + spec["blocked_every"]:
                counts = [0] * len(spec["heads"])
                for h, head in enumerate(spec["heads"]):
                    for e, r in zip(head["emitters"], head["receivers"]):
                        counts[h] += self._raycast(self.data.site_xpos[e], self.data.site_xpos[r], h)
                self._blocked, self._blocked_at = counts, now
            out.update({signal: float(count) for signal, count in zip(spec["blocked"], self._blocked)})
        return out

    def _clearance_outputs(self) -> dict[str, float]:
        out = {}
        for signal, geoms, others, distmax in self._clearance:
            out[signal] = min(self.mujoco.mj_geomDistance(self.model, self.data, a, b, distmax, None)
                              for a in geoms for b in others)
        return out

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
        for kind, slot, value in self._initial:
            (self.data.qpos if kind == "qpos" else self.data.qvel)[slot] = value
        self.mujoco.mj_forward(self.model, self.data)
        if self._curtain_spec is not None:
            import numpy as np
            from ..optics import LightCurtain
            self._geomid = np.zeros(1, dtype=np.int32)
            self._multi_dist, self._multi_geom = np.zeros(0), np.zeros(0, dtype=np.int32)
            self._blocked, self._blocked_at = None, 0.0
            self._ray_cache: dict[int, tuple[float, float]] = {}
            self.curtain = LightCurtain(self._curtain_spec["optics"])

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
        if self.curtain is not None:
            outputs.update(self._curtain_outputs(inputs, max(0.0, span)))
        if self._clearance:
            outputs.update(self._clearance_outputs())
        if any(not math.isfinite(v) for v in outputs.values()):
            raise FloatingPointError(f"MuJoCo diverged at t={self.data.time:.6g}; reduce the timestep")
        return StepResult(outputs, {"substeps": max(0, steps)})

    def checkpoint(self) -> object:
        size = self.mujoco.mj_stateSize(self.model, self.state_spec)
        import numpy as np
        state = np.empty(size)
        self.mujoco.mj_getState(self.model, self.data, state, self.state_spec)
        saved = {"spec": "integration", "state": [float(x) for x in state]}
        if self.curtain is not None:
            saved["curtain"] = self.curtain.snapshot()
        return saved

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
        if self._curtain_spec is not None and data.get("curtain"):
            if self.curtain is None:
                self.start({})
                self.mujoco.mj_setState(self.model, self.data, state, self.state_spec)
                self.mujoco.mj_forward(self.model, self.data)
            self.curtain.restore(data["curtain"])

    def capabilities(self) -> dict[str, bool]:
        return {**super().capabilities(), "checkpoint": True, "variable_step": False, "mechanical_contacts": True}

    def stop(self) -> None:
        self.data = None
