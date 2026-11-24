# Theory of operation

This part explains **how** embeddyn works: what happens in one co-simulation step, how each simulator is driven, and the mathematics behind the analysis tools. Read it when you need to reason about accuracy, determinism or performance, or before writing an adapter of your own.

## The layers

```mermaid
flowchart TB
  A["Authoring<br/>project.yaml · Lab schematic · KiCad · MJCF · netlists · ELF"] --> B
  B["Engineering model<br/>MechatronicProject: signals, couplings, instances, parameters, metrics"] --> C
  C["Execution<br/>CoSimulationOrchestrator · scheduler · AdapterManager · checkpoints"] --> D
  D["Adapters<br/>renode · ngspice-shared · mujoco · python · fmu · webots · replay · mock"] --> E
  E["Simulators<br/>Renode (process, TCP) · libngspice (in-process) · MuJoCo · FMU binaries · Webots"]
  C --> F["Run outputs<br/>rows · CSV · telemetry · events · manifest"]
  F --> G["Analysis<br/>metrics → tuning · tolerance · estimation · verification"]
```

| Layer | Code | Page |
|---|---|---|
| Engineering model | `mechatronics/model.py` | [System model](system-model.md) |
| Execution | `orchestrator.py`, `runtime/scheduler.py`, `runtime/manager.py`, `runtime/checkpoints.py` | [Master algorithm](master-algorithm.md) |
| Firmware adapter | `adapters/renode.py`, `renode_api*.py`, `spi_devices.py` | [Firmware emulation](firmware.md) |
| Electrical adapters | `adapters/ngspice_shared.py`, `python_model.py` | [Electronics](electronics.md) |
| Mechanical adapters | `adapters/mujoco_adapter.py`, `python_model.py`, `plants.py`, `webots.py` | [Mechanics and plants](mechanics.md) |
| FMI | `adapters/fmu.py`, `fmi_export.py` | [FMI co-simulation](fmi.md) |
| Metrics | `metrics.py` | [Metrics](metrics.md) |
| Tuning | `tuning.py` | [Optimisation](optimisation.md) |
| Tolerance | `tolerance.py` | [Uncertainty and sensitivity](uncertainty.md) |
| Estimation | `estimation.py` | [Parameter estimation](estimation.md) |
| Verification | `verification.py` | [Model verification](verification.md) |
| Lab | `gui/` | [The Lab](lab.md) |

## Design principles

**Signal-oriented, not simulator-oriented.** A project talks about `motor.current` and `arm.angle`, not about SPICE vectors or Renode registers. Adapters translate at the edge, so you can replace the averaged H-bridge model with a switching ngspice netlist, or a Python rotor with a MuJoCo arm, without touching the rest of the project.

**Deterministic by construction.** Time is an integer step index; instances run in a fixed order; Renode's virtual time only moves when the master sends `RunFor`; random sampling is seeded. The same project and parameters give the same rows, bit for bit, which is what makes golden-waveform tests, checkpoint/resume and optimisation over full runs work.

**Real firmware, unmodified.** The firmware domain runs the ELF you would flash. It reads sensors through its own SPI, I²C, ADC and timer drivers, and the adapter only touches what hardware would touch: pins, peripheral registers and RAM where you choose to inject calibration values.

**Every run is an experiment.** Tuning, tolerance analysis, estimation and verification are all *outer loops* over the same `run`. Anything you can run, you can score, optimise and test.

## One step at a glance

```mermaid
sequenceDiagram
  participant O as Orchestrator
  participant R as Renode (firmware)
  participant N as ngspice (electrical)
  participant M as MuJoCo (mechanical)
  Note over O: macro step k: t_{k-1} → t_k
  O->>O: apply couplings into firmware (delayed feedback from step k−1)
  O->>R: write ADC / GPIO / RAM inputs, RunFor(h)
  R-->>O: PWM duty, pin levels, RAM telemetry, SPI/CAN/UART traffic
  O->>O: apply couplings into electrical
  O->>N: set external sources, resume transient to t_k
  N-->>O: mean current, voltages over the step
  O->>O: apply couplings into mechanical
  O->>M: ctrl/qfrc inputs, n × mj_step
  M-->>O: qpos, qvel, sensors
  O->>O: commit history, telemetry row, checkpoint if due
```

The rest of this section takes each arrow apart.
