# Examples catalogue

All examples live in `examples/mechatronics/`. Run any of them with `embeddyn mechatronics run <file>` (add `--mock` to replace real simulators with built-in models) or open them in the Lab.

## Projects

| Project | Domains and backends | Needs | Shown in |
|---|---|---|---|
| `dc-motor.yaml` | firmware, electrical, mechanical: all mock | — | [Tutorial 1](../tutorials/dc-motor.md), [10](../tutorials/verification.md) |
| `dc-motor-ngspice.yaml` | mock firmware · **ngspice** 20 kHz H-bridge · mock mechanics | libngspice | [Tutorial 2](../tutorials/ngspice.md) |
| `dc-motor-renode.yaml` | **Renode** STM32 speed controller · mock electrical/mechanical | Renode | |
| `dc-motor-full.yaml` | **Renode** firmware · **ngspice** H-bridge · mock mechanics | Renode, libngspice | [Tutorial 2](../tutorials/ngspice.md#step-5-with-real-firmware-too) |
| `servo-drivetrain.yaml` | PI · DCMotorRL · **GearedDrivetrain** with backlash · **ThermalRC** | — | [Tutorials 7](../tutorials/tuning.md), [8](../tutorials/tolerance.md) |
| `mujoco-arm.yaml` | function generator · PI · DCMotorRL · **MuJoCo** arm | `mujoco` | [Tutorial 3](../tutorials/mujoco-arm.md) |
| `rc-servo-lab.yaml` | function generator · pulse generator · **RCServo** · slider; Lab layout | — | [Tutorial 11](../tutorials/lab.md) |
| `rc-servo-l476.yaml` | **Renode** STM32L476 · RCServo · replayed operator; UART console | Renode | [Tutorial 4](../tutorials/rc-servo-firmware.md) |
| `rc-servo-l476-can.yaml` | as above with **CAN** status/command frames | Renode ≥ 1.17 | [Renode adapter](renode.md#can-renode-117) |
| `mujoco-arm-firmware.yaml` | **Renode** STM32L476 · H-bridge · DCMotorRL · ThermalRC · **MuJoCo**; encoder, **SPI** AS5047P, **I²C** AS6221 | Renode ≥ 1.17, `mujoco` | [Tutorial 5](../tutorials/arm-joint-sensors.md) |
| `two-wheel-drive.yaml` | 2 × PI · 2 × DCMotorRL · **FMU** wheel · GearedDrivetrain wheel; multi-rate | built FMU | [Tutorial 6](../tutorials/instances-and-fmus.md) |

## Analysis specs

| Spec | Command | Project |
|---|---|---|
| `tuning/servo-pi.yaml` | `tune` | servo-drivetrain |
| `tuning/pi-mock.yaml` | `tune` | dc-motor (mock) |
| `tuning/two-wheel.yaml` | `tune` | two-wheel-drive |
| `tuning/arm-firmware.yaml` | `tune` (Nelder–Mead) | mujoco-arm-firmware |
| `tuning/arm-firmware-cmaes.yaml` | `tune` (CMA-ES, 4 workers) | mujoco-arm-firmware |
| `tolerance/servo.yaml` | `tolerance` | servo-drivetrain |
| `estimation/dc-motor.yaml` | `estimate` | dc-motor + `bench/dc-motor-step-test.csv` |

## Supporting files

| Directory | Contents |
|---|---|
| `firmware/` | C sources and Makefiles for the STM32L476 examples (`rc-servo-l476`, `arm-joint-l476`, `motor-speed-control`) |
| `renode/` | `.resc` scripts and `.repl` platform descriptions (incl. the SPI/I²C sensor platform) |
| `bindings/` | Renode, ngspice and Webots binding files |
| `circuits/` | SPICE netlists (`motor-hbridge.cir`) |
| `mujoco/` | MJCF models (`arm-joint.xml`) |
| `fmus/` | the rotational-load FMU (FMI 2 and 3) and its build script |
| `golden/` | golden waveforms for verification |
| `rc-servo/` | operator command profile (replay CSV) |
| `controllers/`, `worlds/` | Webots controller bridge and worlds |
| `experiments/`, `hil/`, `digital-twin/` | platform features: sweeps, HIL safety policies, twin evidence |
