# Roadmap: whole-system emulation and tuning

**Goal:** emulate, test and fine-tune a complete embedded product (firmware,
electronics and mechanics) as one deterministic, repeatable simulation, and
close the loop with bench measurements.

*Written 2026-11-21 from a review of the repository at that date.*

## Where we are

| Area | State | Notes |
|------|-------|-------|
| Project model (signals, units, couplings, delays) | ✅ solid | one YAML describes the system |
| Orchestrator / scheduler | ✅ solid | deterministic fixed-step and multi-rate, iterative coupling, checkpoints |
| Storage, telemetry, distributed workers, HIL safety, twin analytics | ✅ baseline | good foundations, mostly exercised with mocks |
| **Firmware: Renode** | 🟢 works end to end on real Renode | custom-cores build, reference STM32F4 motor firmware and an STM32L476 RC-servo controller, closed loop validated ([RENODE.md](RENODE.md), [RC_SERVO_L476.md](RC_SERVO_L476.md)) |
| **Electrical: ngspice (libngspice)** | 🟢 works with carried state | `ngspice-shared` backend, switching H-bridge example, closed loop with Renode firmware ([NGSPICE.md](NGSPICE.md)); LTspice and the per-step `ngspice` backend remain batch-only |
| Mechanical: MuJoCo | 🟢 works | `backend: mujoco`: in-process multibody physics, exact checkpoint/resume, robot-arm joint example ([MUJOCO.md](MUJOCO.md)) |
| Mechanical: Webots | 🔴 Webots physics unused | reference controller computes the plant in Python; 0.5 ms step is rounded to 1 ms of Webots time |
| Python plant models, thermal domain | 🟢 works | `backend: python` for any domain; geared drivetrain and winding thermal models ([TUNING.md](TUNING.md)) |
| Named simulator instances, FMI | 🟢 works | any number of instances per domain with own rates; FMI 2.0/3.0 co-simulation FMUs ([INSTANCES_AND_FMI.md](INSTANCES_AND_FMI.md)) |
| Verification (energy, convergence, golden, scenarios) | 🟢 works | `mechatronics verify`, gating CI ([VERIFICATION.md](VERIFICATION.md)) |
| Tuning (metrics, optimiser, parameter estimation) | 🟢 works | `mechatronics tune` (bounded Nelder–Mead over full co-simulations); parameter estimation with confidence intervals ([ESTIMATION.md](ESTIMATION.md)) |
| Lab GUI for co-simulation | 🟢 works | `mechatronics gui`: schematic editor, parts library, live run with scopes/meters/logic analyser, sliders, 3-D view, replay ([GUI.md](GUI.md)) |

Firmware runs for real in Renode and electronics in ngspice; the mechanical domain still runs only as a built-in model.

## Phase R1: real firmware in the loop (Renode)

*Status: in progress.*

Done:
- [x] Pin `renode-source` to the `custom-cores` branch of `xsession/renode`
  (`embeddyn.yml`, `embeddyn.lock.json`, `.gitmodules`).
- [x] Pure-Python External Control client covering RunFor, GetTime,
  GetMachine, ADC, GPIOPort (with edge events) and SystemBus.
- [x] `RenodeAdapter`: integer-µs virtual time aligned to co-simulation time,
  optional Renode launch, binding kinds `gpio_duty`, `gpio_state`, `sysbus`,
  `register_ratio`, `adc`, `gpio`.
- [x] `IADC` for `STM32_ADC` (patch in `externals/patches/renode-infrastructure`).
- [x] Example project `dc-motor-renode.yaml`, `.resc`, bindings;
  `integration-validate` checks for Renode.
- [x] Byte-compatible fake server and closed-loop tests.

- [x] Build the branch on the host (`tools/renode/build-custom-cores.sh`,
  user-local .NET 8, no Docker) and validate on real Renode: protocol, ADC
  patch, timer PWM edge events, closed loop (`test_renode_real.py`), CLI run.
- [x] Reference firmware `examples/mechatronics/firmware/motor-speed-control`
  (bare-metal, `arm-none-eabi-gcc`), with run-time calibration in RAM.
  Interrupt + `WFI` paced by default (about 10× less CPU than polling);
  the earlier "interrupt never taken" problem was a missing `DSB; ISB`,
  reproduced identically on upstream Renode.
- [x] CI job `.github/workflows/renode-cosim.yml`: builds Renode (cached),
  builds the firmware, runs `test_renode_real.py` with a golden-trace check.
  Not yet run on GitHub.
- [x] `IADC` for the `STM32_ADC_Common` family (F0/F3/G0/WBA/H7), patch 0002,
  verified on a real build. Still open: the custom cores' own ADCs.
- [x] `STM32_Timer` PWM dropout at `CCR = ARR` fixed (patch 0003); the real
  firmware now matches the mock controller's speed to within 0.04 rad/s.
- [x] `float32` sysbus bindings; `RENODE_BIN`; `variables` (e.g. quantum);
  fail fast on `.resc` errors.

Next:
- [ ] **Fix the fork so it builds as-is**: align `src/Infrastructure` and `lib/*`
  pins with the top-level `build.sh`/`Renode.sln`, and commit the small
  source fixes (see the table in [RENODE.md](RENODE.md#building-the-custom-cores-branch)).
  Then the build script reduces to clone + `./build.sh`.
- [ ] Commit the patches in `externals/patches/renode-infrastructure` (ADC 0001,
  0002; timer 0003) to `xsession/renode-infrastructure`.
- [ ] Make the branch's STM32 platform files load: `stm32f4.repl` needs
  `STM32_RNG`, `stm32g0.repl` an `STM32SPI` constructor that matches.
- [ ] Replace the bundled `externals/renode` copy with the managed checkout,
  `python bootstrap.py update renode-source --adopt`, or make it a real
  submodule. Push the patches to the fork first.
- [ ] Checkpoints via Renode `Save`/`Load`. Needs a fork change: External
  Control can't trigger them, and `Load` replaces the server the adapter talks
  to (see Limits in [RENODE.md](RENODE.md#limits)).
- [ ] Bus-level signals (UART/SPI/I²C/CAN) through a Renode-side plugin, for
  sensor ICs and comms stacks.

## Phase R2: electronics with state (ngspice shared library)

*Status: core done.*

- [x] `NgspiceSharedAdapter` (`backend: ngspice-shared`) on libngspice via
  ctypes: one transient for the whole run, `stop when` + `resume` per step,
  `GetSyncData` lands each step exactly on co-simulation time.
- [x] Inputs through `external` sources (zero-order hold) and `alter`;
  outputs as `V()`/`I()` vectors, per step `mean`/`rms`/`min`/`max`/`last`.
- [x] Switching H-bridge example (`circuits/motor-hbridge.cir`, 20 kHz PWM,
  DC link, current sense) and `dc-motor-full.yaml`: real firmware in Renode +
  H-bridge in ngspice.
- [ ] Gate drive, dead time and MOSFET/diode models in the bridge.
- [ ] `.save`-based vector filtering so long runs don't keep every point.
- [ ] KiCad-to-SPICE round trip using the existing `KiCadBridge`.
- [ ] Keep LTspice for offline validation runs only (it is batch-only).

## Phase R3: flexible system topology and FMI

- [x] Simulators as **named instances** (`instances:`), each with a domain,
  backend and optional own step; legacy `simulators:` still works.
- [x] `FmuAdapter`: FMI 2.0 and 3.0 co-simulation via ctypes (no `fmpy`);
  example FMU built from C; two-wheel-drive example with six instances.
- [ ] FMU state in checkpoints (`fmi*GetFMUState`/`SetFMUState`); FMU export
  of Embeddyn models.
- [x] Python model adapter (`backend: python`, RK4 sub-stepping, checkpoints) with
  built-in `GearedDrivetrain` (backlash, compliance, Stribeck friction,
  encoder) and `ThermalRC`; `thermal`/`environment` domains built by the factory.
- [ ] Fix the Webots bridge: drive real motors and sensors through the
  Supervisor API, require `basicTimeStep` to divide the co-simulation step,
  and use Webots only where contacts or 3D matter.
- [ ] Shared physical constants: declare `Kt`/`Ke` once instead of copying
  them into coupling gains.
- [x] Integer-tick time base in the orchestrator: step k ends at k × macro step, so resumed runs match exactly.
- [x] MuJoCo multibody backend with native state checkpoints ([MUJOCO.md](MUJOCO.md)).

## Phase R4: fine-tuning loop

- [x] Response metrics (settling/rise/peak time, overshoot, steady-state
  error, IAE/ISE/ITAE, peak/RMS/mean, integrals) declared per project,
  computed after every run and recorded on sweep jobs.
- [x] `mechatronics tune`: deterministic bounded Nelder–Mead over full
  co-simulations with weighted objectives and constraints.
- [ ] Optimiser jobs on the distributed-worker system (parallel evaluations),
  CMA-ES / Bayesian methods.
- [x] Physical parameter estimation from bench traces (`mechatronics estimate`):
  Levenberg–Marquardt over full co-simulations, replayed inputs, 95 %
  confidence intervals and parameter correlations.
- [x] Monte Carlo tolerance runs over component spread (`mechatronics tolerance`):
  Latin hypercube, yield with Wilson intervals, Sobol first-order/total
  indices with bootstrap intervals, parallel process pool.
- [ ] Run tolerance and tuning evaluations on the distributed worker queue.
- [x] Firmware parameter injection at run time (`float32` sysbus binding into
  the firmware's `motor_cal` struct), so tuning doesn't need a rebuild.
  Optimiser jobs can now vary `kp`/`ki` in the real firmware.

## Phase R5: usability

- [x] Lab GUI: build, run, plot and replay mechatronics runs; edit parts, wires and couplings ([GUI.md](GUI.md)).
- [ ] Lab: compare several runs; edit binding files from the GUI; tuning/tolerance jobs from the GUI.
- [ ] `embeddyn mechatronics doctor` checks the Renode build, ngspice library
  and Webots versions.
- [ ] Docker image with Renode (custom-cores), ngspice and Webots for
  reproducible CI and offline use.
- [ ] HIL step: replace an emulated domain with real hardware through the
  existing HIL safety layer.

## Remaining features, consolidated

*Collected 2026-11-21 from every plan in the repository: this roadmap,
`INTEGRATION_PLAN.md`, `ARCHITECTURE.md`, `ARCHITECTURE_AND_PARAMETER_STORAGE.md`
and the "remaining work" sections of the phase changelogs/completion notes,
checked against the code.* Order is the build order: each group unblocks the next.

| # | Feature | Source | State |
|---|---------|--------|-------|
| 1 | ✅ Response metrics (settling/rise time, overshoot, steady-state error, IAE/ITAE, peak/RMS current, energy) computed after every run and per sweep job | R4; `pid-sweep.yaml` declares them | done: `metrics:` in projects, `metrics.json` per run |
| 2 | ✅ Optimiser over parameters (firmware gains, design values) against weighted metrics and constraints | R4 | done: `mechatronics tune` (serial evaluations) |
| 3 | ✅ Python plant models with sub-stepping; a drivetrain library (rotor, gearbox with backlash and compliance, Coulomb/Stribeck friction, load, encoder) | R3; ARCHITECTURE "mechanical domain" | done: `backend: python`, `plants.py` |
| 4 | ✅ Named simulator instances (several per domain; thermal/environment domains, which the enum already has but the factory ignores) | R3; spec §3.5, §4.3 | done: `instances:` with per-instance rates |
| 5 | ✅ FMI 2/3 co-simulation import (export still open) | R3; INTEGRATION_PLAN 5 | done: `backend: fmu` (ctypes) |
| 6 | ✅ Verification checks: energy balance, step-size convergence, golden waveforms, scenario matrices | INTEGRATION_PLAN 7 | done: `mechatronics verify` ([VERIFICATION.md](VERIFICATION.md)), CI workflow |
| 7 | ✅ Physical parameter estimation from bench traces (R, L, Kt, J, friction) with confidence intervals | R4; PHASE6 next steps | done: `mechatronics estimate` ([ESTIMATION.md](ESTIMATION.md)) |
| 8 | ✅ Monte Carlo tolerance runs with yield; Sobol sensitivity | R4; PHASE6 | done: `mechatronics tolerance` ([TOLERANCE.md](TOLERANCE.md)), local process pool; experiment-queue execution still open |
| 9 | Renode sensor/bus signals: quadrature encoder, UART/SPI/I²C/CAN frames, fault injection | R1; INTEGRATION_PLAN 4 | missing |
| 10 | Renode fork fixes upstreamed (pins, patches 0001–0003, platform files), checkpoints via Save/Load | R1 | patches local only |
| 11 | ngspice: gate drive/dead time/MOSFET models, `.save` filtering, KiCad round trip | R2; INTEGRATION_PLAN 6 | missing |
| 12 | Webots bridge using real physics (Supervisor motors/sensors, timestep alignment) | R3 | MuJoCo backend now provides multibody physics ([MUJOCO.md](MUJOCO.md)); the Webots bridge still computes the plant itself |
| 13 | Shared physical constants (`Kt`/`Ke` once); integer-tick time base | R3 | integer-tick time base done; shared constants missing |
| 14 | Studio UI for co-simulation runs, plots, binding editor | R5; INTEGRATION_PLAN 2 | Lab GUI done (GUI.md); run comparison and binding-file editor missing |
| 15 | `mechatronics doctor`, Docker image, HIL step with real hardware | R5; INTEGRATION_PLAN 8 | missing (HIL safety layer exists) |
| 16 | Storage: override layers, PostgreSQL, telemetry range queries, retention/GC, bundle import/export, run recovery, backups | PHASE2 remaining | missing |
| 17 | Twin: live streaming sync, clock-drift estimation, HTML/PDF and signed evidence reports | PHASE6 next steps | missing |

## Housekeeping

- Fix git's "dubious ownership" error on the repository directory:
  `git config --global --add safe.directory <repo>`.
- `externals/` holds about 16k vendored files; prefer real submodules or
  bootstrap-managed checkouts.
