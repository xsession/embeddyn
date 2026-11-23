# MuJoCo multibody physics (`backend: mujoco`)

MuJoCo (Apache-2.0) runs inside the embeddyn process as an ordinary simulator
instance. It brings rigid-body dynamics, gravity, joint limits, contacts and
friction to the mechanical domain. Its full integration state is saved and
restored natively, so runs checkpoint, resume and repeat bit for bit. The Webots
bridge cannot do this: it integrates its own ODE and has no state snapshot.

```bash
pip install mujoco            # 4.x needs Python >= 3.11
embeddyn mechatronics run examples/mechatronics/mujoco-arm.yaml -o arm.csv
embeddyn mechatronics gui examples/mechatronics/mujoco-arm.yaml
```

## Configuration

```yaml
arm:
  domain: mechanical
  model: mujoco/arm-joint.xml          # MJCF, relative to the project file
  options:
    backend: mujoco
    timestep: 0.0005                   # optional override of <option timestep>
    inputs:                            # model address -> signal
      "ctrl:shoulder_motor": motor.torque
      "qfrc:shoulder": load.value
    outputs:                           # model address -> signal
      "qpos:shoulder": arm.angle
      "qvel:shoulder": arm.speed
      "qfrc_act:shoulder": arm.joint_torque
      "sensor:tip_acc[2]": arm.tip_acc_z
      "xpos:tip[2]": arm.tip_height
```

| Address | Direction | Meaning |
|---|---|---|
| `ctrl:<actuator>` | in / out | actuator control; for a `<motor>` the shaft torque (the joint gets `gear` × ctrl) |
| `qfrc:<joint>` | in | generalised force applied to a joint (disturbance, load torque) |
| `qpos:<joint>`, `qvel:<joint>` | out | joint position / velocity (`[i]` for ball and free joints) |
| `qfrc_act:<joint>` | out | joint-side torque from all actuators (after the gear) |
| `act:<actuator>` | out | actuator-side force (before the gear) |
| `sensor:<name>` | out | any MJCF sensor; `[i]` selects a component of vector sensors |
| `xpos:<body>[i]` | out | world position of a body |

The co-simulation macro step must be a whole multiple of the MuJoCo timestep.
Each step writes the inputs, runs that many `mj_step` calls, then reads the
outputs, and the adapter refuses steps that don't fit. In the Lab, a "MuJoCo
model" part offers every actuator, joint and sensor of its MJCF file as a pin.

## Modelling the drive

When another instance owns the motor winding (`DCMotorRL`, ngspice or an FMU),
drive a plain `<motor>` actuator with its torque. Put the rotor inertia in the
joint's `armature` (rotor inertia × gear²), and couple the joint speed back
to the motor's back-EMF with the gear ratio. MuJoCo's own `dcmotor` actuator
also models the winding, so using it as well would count the electrical
dynamics twice. Use it only when MuJoCo is the whole drive.

## Example: one robot-arm joint

[`examples/mechatronics/mujoco-arm.yaml`](../../examples/mechatronics/mujoco-arm.yaml):
a square-wave angle target (0 ↔ 1.2 rad) drives a PI position controller,
which drives a DC motor armature (python, 24 V, 2 Ω, kt = 0.08 N·m/A). The
motor turns a MuJoCo 0.25 m link with a 0.2 kg payload under gravity through a
20:1 gear. A Lab slider applies a disturbance torque at the joint.

| Result (kp 6, ki 3) | Value |
|---|---|
| 0 → 1.2 rad | rise 0.52 s (duty-saturated, about 2 rad/s), settles in 0.65 s, 0.3 % overshoot |
| Holding horizontal | joint torque 0.85 N·m = gravity (0.3·0.125 + 0.2·0.25)·9.81 |
| −1 N·m disturbance | rejected by the integrator; the motor supplies the extra 1 N·m |
| Peak current | 11.5 A, under the 12 A stall current |

## With firmware in the loop

[`examples/mechatronics/mujoco-arm-firmware.yaml`](../../examples/mechatronics/mujoco-arm-firmware.yaml)
closes the same joint through real STM32L476 firmware
(`firmware/arm-joint-l476`, Renode ≥ 1.17):

| Path | How |
|---|---|
| Encoder | MuJoCo angle × 13 038 counts/rad (4096 cpr × 20 / 2π) is written into `TIM3_CNT` every step. The timer is in encoder mode, so it holds the value, and the firmware unwraps the 16-bit counter. |
| H-bridge | TIM2 CH1/CH2 at 20 kHz, sign-magnitude. Each pin's duty is measured from GPIO edges, and `HBridgePWM` gives duty = IN1 − IN2 for `DCMotorRL`. |
| Command | ADC1 IN1 (PC0), 0–3.3 V = ±1.5 rad |
| Control | 1 kHz PID with anti-windup, run from TIM2's update interrupt. Gains in `arm_cal` at 0x20000000 can be tuned over the bus. |

Result: 0 → 1.2 rad settles in 0.68 s with 0.35 % overshoot and a 3 mrad
error. The firmware's encoder angle stays within 2 mrad of MuJoCo, and the
duty measured on the pins equals the firmware's duty one step later. 4 s of
simulation take about 30 s (`tests/mechatronics/test_arm_firmware.py`).

## Determinism

`tests/mechatronics/test_mujoco.py` runs the example twice and requires every
row to be identical. It then resumes from the 2 s checkpoint and requires the
remaining rows to match the uninterrupted run bit for bit. That covers the MuJoCo
state (`mjSTATE_INTEGRATION`) and the python plants' states together. This exactness
needs the orchestrator's integer step index: step *k* always ends at
*k* × macro step, so a resumed run lands on the same times.
