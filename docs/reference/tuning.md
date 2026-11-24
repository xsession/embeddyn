# Metrics, tuning and plant models

Three pieces turn the co-simulation into a tuning tool: metrics computed
from every run, an optimiser that varies parameters across runs, and Python
plant models for the physics that the built-in mock doesn't cover.

## Metrics

A project declares named metrics; `mechatronics run` computes them after the
run, prints them in its JSON summary and writes `metrics.json` into the run
directory. Distributed sweep jobs record them too (an experiment's
`metrics:` list selects which).

```yaml
metrics:
  speed: {type: step_response, signal: mechanical.velocity, target: firmware.target_velocity, settling_band: 0.02}
  current: {type: stats, signal: electrical.motor_current}
  energy: {type: integral, signal: electrical.input_power}
```

| Type | Results (`<name>.<result>`) |
|------|-----------------------------|
| `step_response` | `rise_time` (10–90 %), `overshoot` (%), `peak_time`, `settling_time` (last exit from the band), `final_value`, `steady_state_error`, `iae`, `ise`, `itae` |
| `stats` | `peak` (max \|x\|), `rms`, `mean` (time-weighted), `min`, `max` |
| `integral` | `integral` (trapezoidal; power gives energy in J) |
| `threshold` | `time` (first crossing of `threshold`, `direction` rising/falling/either), `delay` (from the window start), `crossings` |

`target` is a signal name or a number. `start`/`end` restrict the time
window. A response that never rises or settles reports `null`, never a made-up
number. `embeddyn mechatronics metrics <project> <run.csv>` recomputes the
metrics from an exported CSV.

The step-response figures are checked against the closed-form second-order
response (overshoot within 0.1 %, peak time within 2 ms).

## Tuning

```bash
embeddyn mechatronics tune examples/mechatronics/tuning/servo-pi.yaml -o result.json
```

```yaml
project: ../servo-drivetrain.yaml
mock: false                 # true forces the built-in simulators
duration: 1.0               # optional duration override
parameters:                 # project parameters; they reach every adapter as inputs
  controller.kp: {initial: 0.5, min: 0.02, max: 3.0, scale: log}
  controller.ki: {initial: 4.0, min: 0.1, max: 40.0, scale: log}
objective:                  # minimise sum(weight * metric)
  - {metric: speed.itae, weight: 1.0}
  - {metric: shaft.peak, weight: 0.002}
constraints:                # penalised; a missing metric counts as violated
  - {metric: speed.overshoot, max: 5.0}
  - {metric: speed.settling_time, max: 0.5}
max_evaluations: 30
```

* The optimiser is a bounded Nelder–Mead in a normalised space (0..1 per
  parameter, logarithmic with `scale: log`). It is deterministic.
* Every evaluation is a full co-simulation with whatever backends the project
  uses. To tune the **real firmware**, bind the parameter name to its RAM
  location in the Renode bindings (a `float32` `sysbus` input, as
  `bindings/renode-arm-joint.yaml` does for `firmware.kp/ki/kd`);
  `integration-validate` accepts bindings to parameter names. The adapter
  writes inputs every step, so the value survives the reset handler copying
  the flash defaults over RAM (it takes effect from the first step on).
* A failing candidate (diverging model, simulator error) is recorded with its
  error and scored as infinite; the search continues.
* **Methods** (`method:` or `--method`):
  * `nelder-mead` (default): a deterministic simplex search. It suits few
    parameters and runs that are mostly sequential.
  * `cmaes`: CMA-ES (Hansen's (μ/μ_w, λ) with rank-one and rank-μ updates and
    step-size adaptation), written with numpy, no extra dependency.
    * Each generation's `population` candidates (default 4 + 3 ln n) run as
      one batch, so with `workers` every worker stays busy. Pick a population
      that is a multiple of `workers`.
    * `sigma` is the initial step in the 0..1 space (default 0.25), and
      `seed` makes runs reproducible.
    * Results don't depend on the number of workers (tested).
    * Candidates outside the box are mirrored back in.
    * The start point is evaluated first as the reported `initial`.
* **Parallel evaluations** (`workers: 4` or `--workers 4`). Nelder–Mead is
  sequential, so the pool evaluates each iteration's reflection, expansion
  and contraction together, and a shrink's points together. Then it takes
  exactly the decisions of the one-worker search:
  * The simplex path and the evaluations that count against
    `max_evaluations` are identical; a test checks this.
  * The runs that search wouldn't have needed are *speculative*:
    `counted: false` in the history, `speculative_runs` in the result. They
    only cost idle workers, and `best` may come from one of them.
  * Workers are spawned processes. Every Renode they launch gets its own
    free port and its own log (`<log>.<pid>.log`, via
    `factory.use_private_endpoints`), so a fixed `port:` in the project is no
    obstacle. Tolerance runs with `workers` > 1 do the same.
  * Evaluations must be deterministic, which co-simulations are: a point the
    search revisits is not run again.
* `result.json` holds every evaluation: parameters, objective, violations,
  all metrics, wall time.

Results on this machine:

| Spec | Start | Best after | Time |
|------|-------|-----------|------|
| `tuning/pi-mock.yaml` (dc-motor, 1 s) | kp 0.12, ki 0.8: violates overshoot and settling | kp 2.0 (bound), ki 0.74: feasible | 40 runs, 10 s |
| `tuning/servo-pi.yaml` (geared servo, 1 s) | kp 0.5, ki 4.0: overshoot 34 % | kp 0.56, ki 1.45: feasible | 30 runs, 50 s |
| `tuning/arm-firmware.yaml` (STM32L476 firmware in Renode + MuJoCo arm, 3 s) | kp 6, ki 3, kd 0.15: settling 0.68 s violates 0.65 s | kp 26.7, ki 3.33, kd 0.53: feasible | 30 runs, 12 min |

### Firmware in the loop: the arm joint

`tuning/arm-firmware.yaml` tunes the PID gains of the real arm-joint firmware.
Each evaluation boots the firmware in Renode and runs it against the MuJoCo
arm, with the gains written into `arm_cal` in RAM.

| 0 → 1.2 rad step | Built-in gains (6 / 3 / 0.15) | Tuned (26.7 / 3.33 / 0.53) |
|---|---|---|
| Settling time (5 %) | 0.679 s | 0.630 s |
| Overshoot | 0.35 % | 0.013 % |
| Steady-state error | 3.3 mrad | 0.2 mrad |
| ITAE | 0.0971 | 0.0908 |
| Energy over 3 s | 443 J | 468 J |

What the search showed:

* **0.63 s is the drive's limit, not the controller's.** The duty saturates
  for most of the step, so every gain set that keeps it saturated until the
  end of the swing settles in 0.63 s. A 0.6 s constraint can't be met with
  this motor and gear; the spec asks for 0.65 s.
* Past kp ≈ 15 the objective is flat to 0.5 %. Higher gains mostly buy
  precision at standstill (steady-state error) and cost energy.
* Peak current is 11.5 A for any useful gains (duty saturation, stall
  12 A), so it isn't a useful constraint here.
* The project now uses the tuned gains. The firmware's built-in defaults are unchanged.

**In parallel** (`--workers 4`, firmware with the SPI/I²C sensors, 2026-11-24):

| | Value |
|---|---|
| Counted evaluations / runs | 30 / 52 (22 speculative), none failed |
| Wall time | 6.8 min, against about 11.8 min for the 30 counted runs one after another (23.5 s each) |
| Best | kp 33.3, ki 2.0, kd 0.46 (a counted point): settling 0.630 s, overshoot 0.007 %, objective within 0.01 % of the sequential result |

The speed-up is about 1.7×, not 4×. Nelder–Mead has at most three candidates
per iteration (reflection, expansion, contraction), so one worker always
idles. A population method (CMA-ES) would keep every worker busy. The
different best gains confirm how flat the optimum is past kp ≈ 15.

**CMA-ES against Nelder–Mead** on the same problem, 4 workers each
(`tuning/arm-firmware-cmaes.yaml`: population 8, σ 0.25, seed 1):

| | Nelder–Mead | CMA-ES |
|---|---|---|
| Runs (counted / total) | 30 / 52 | 49 / 49 |
| Within 0.1 % of the final best after run | 36 | 12 |
| Worker utilisation (sum of run time ÷ wall time) | 3.0 of 4 | 3.85 of 4 |
| Best objective | 13.7639 (kp 33.3, ki 2.0, kd 0.46) | 13.7675 (kp 26.1, ki 2.6, kd 0.33) |
| Wall time | 6.8 min | 9.3 min |

Read the wall times with care. Other load on the host (load average 11–14)
stretched some CMA-ES runs from about 30 s to 76–147 s. Without that the six
generations take about 6.5 min. CMA-ES finds the optimum region with a third
of the runs and keeps all four workers busy. Nelder–Mead ends a little lower
on this flat, smooth problem. Use CMA-ES when there are more parameters, a
noisy or multi-modal objective, or many workers.

The servo case first came out infeasible: the mock controller's integrator
wound up during saturation, so every gain pair that settled fast overshot.
The mock controller now has opt-in anti-windup (`controller.anti_windup: 1`,
conditional integration as in the reference firmware); the servo example
enables it.

## Python plant models (`backend: python`)

Any domain can run a Python ODE model, integrated with fixed-step RK4
(`max_step`) between co-simulation steps; inputs are held for the step.

```yaml
mechanical:
  model: "embeddyn.mechatronics.plants:GearedDrivetrain"   # or "plants/my.py:MyPlant"
  options:
    backend: python
    max_step: 2.0e-5
    parameters: {gear_ratio: 10, backlash: "@drivetrain.backlash"}  # "@name" = project parameter
    inputs: {motor_torque: mechanical.motor_torque}
    outputs: {motor_speed: mechanical.velocity, shaft_torque: mechanical.shaft_torque}
```

A plant class declares `inputs`, `outputs` and `states`, and implements
`initial_state()`, `derivatives(t, x, u)` and `output(t, x, u)`; see
`embeddyn/mechatronics/adapters/python_model.py`. Checkpoint/restore works.
Plant parameters given as `"@name"` come from project parameters, so design
values (backlash, gear ratio, inertia) can be swept and tuned like gains.

Built-in plants (`embeddyn/mechatronics/plants.py`):

* **`GearedDrivetrain`**: motor rotor → gearbox (ratio, efficiency, backlash
  gap) → compliant shaft (spring-damper that transmits torque only in contact
  and never pulls) → load with viscous, Coulomb and Stribeck friction
  (tanh-regularised) → incremental encoder (`encoder_cpr`). Outputs motor and
  load angle/speed, shaft and friction torque, encoder count, backlash contact.
* **`ThermalRC`**: winding temperature with copper loss I²·R(T) and
  R(T) = R20 (1 + α (T − 20)); feeds the resistance back to the electrical
  model.

The `thermal` and `environment` domains are now built by the factory when a
project configures them (python backend), and they run in the scheduler's
order after the mechanical domain.

### Example: geared servo with thermal feedback

`examples/mechatronics/servo-drivetrain.yaml` couples four domains: the mock
firmware PI (with anti-windup), the mock RL motor model, the geared
drivetrain with 4 mrad backlash and Stribeck friction, and the winding
thermal model, whose resistance feeds back into the motor. One second runs in
about 4 s. With the starting gains: 34 % overshoot, an 18.6 N·m shaft torque
peak during the first acceleration, and +4.9 K winding temperature. Replace
the firmware and electrical simulators with Renode and `ngspice-shared` as in
`dc-motor-full.yaml` to put the real firmware and the switching bridge in the
same loop.
