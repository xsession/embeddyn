# Physical parameter estimation

`embeddyn mechatronics estimate spec.yaml` fits physical parameters of a model
(resistance, inductance, inertia, friction, load, …) to bench measurements
and reports how well the data determine each one. It replaces the
per-signal affine calibration of the digital-twin module when the goal is a
physically meaningful model.

```bash
embeddyn mechatronics estimate examples/mechatronics/estimation/dc-motor.yaml -o result.json
```

## Spec

```yaml
project: ../dc-motor.yaml
measurements: bench/dc-motor-step-test.csv   # time + measured columns
disable: [firmware]                 # open loop: the recorded input replaces the controller
replay:                             # recorded inputs driven into the model
  domain: firmware
  columns: {duty: firmware.pwm_duty}
  interpolation: previous           # sample-and-hold (or linear)
fit:                                # model signal -> measured column, noise sigma
  electrical.motor_current: {column: current, sigma: 0.05}
  mechanical.velocity: {column: speed, sigma: 0.02}
fixed: {}                           # values known from elsewhere, held constant
parameters:
  electrical.motor_resistance: {initial: 2.0, min: 0.1, max: 20, scale: log}
  mechanical.load_torque: {initial: 0.03, min: -1, max: 1}
max_iterations: 30
```

* A parameter name is either a **constant signal** (its initial value is
  set, as for `electrical.motor_resistance` in the built-in models) or a
  **project parameter** (reaching Python plants and FMUs through `"@name"`,
  and adapters as inputs). So any model in the system can be fitted.
* `replay` adds a `replay` instance that plays the measured input columns
  into model signals. The `replay` backend can also be used directly in any
  project to drive a model from a recording.
* The simulated signals are interpolated at the measurement times; residuals
  are (simulated − measured) / sigma over all fitted signals.

## Method

Levenberg–Marquardt over full co-simulations, in a bounded space
(logarithmic for `scale: log`, so positive quantities stay positive and are
searched by ratio). The Jacobian is by forward differences: one run per
parameter per iteration. After convergence:

* covariance = s²·(JᵀJ)⁻¹ in natural units, with s² the **reduced
  chi-square** (SSR / (samples − parameters)). The intervals are therefore
  correct even if the given sigmas are off; s² ≈ 1 means the sigmas were right
  and the model explains the data down to the noise.
* `ci95`: estimate ± 1.96 standard errors; `relative_ci95_percent`.
* `correlation`: the parameter correlation matrix. |r| close to 1 means the
  experiment can't tell those parameters apart; design another test (or fix
  one parameter) rather than trusting either value.
* `at_bound`: an estimate stuck on its bound has no meaningful interval.
* `fit`: RMSE and R² per fitted signal; `history`: every iteration.

The solver is pure Python (no numpy/scipy dependency).

## Example and validation

`examples/mechatronics/estimation/make_bench_data.py` produces a synthetic
open-loop step test: the DC motor model runs with known "true" parameters
(different from the project's nominal values) under a duty profile
(0 → 0.4 → 0.9 → 0.2 → −0.3), and current and speed get Gaussian noise
(σ = 0.05 A, 0.02 rad/s; fixed seed). Fitting five parameters from the
nominal values:

| Parameter | Nominal (start) | True | Estimate | 95 % CI |
|-----------|-----------------|------|----------|---------|
| R [Ω] | 2.0 | 2.4 | 2.4003 | ± 0.08 % |
| L [H] | 0.003 | 0.0045 | 0.004482 | ± 1.2 % |
| J [kg·m²] | 0.02 | 0.025 | 0.025026 | ± 0.17 % |
| b [N·m·s/rad] | 0.08 | 0.06 | 0.06013 | ± 0.59 % |
| T_load [N·m] | 0.03 | 0.05 | 0.04952 | ± 1.95 % |

All five true values lie inside their intervals. Convergence took 5
iterations (37 runs, 16 s). The residual RMSE equals the injected noise
(0.049 A, 0.019 rad/s) and the reduced chi-square is 0.96, so nothing
systematic is left. The correlation matrix flags **damping vs load torque
(r = 0.95)**: both just decelerate the motor in this test, so their individual
values are the least certain. A coast-down at several speeds would separate
them.

`tests/mechatronics/test_estimation.py` regenerates the data and checks that
the true values fall inside the intervals and that the reduced chi-square is
near 1.

## Limits

* The synthetic example fits the same model that produced the data. Real
  bench data adds model error, which shows up as a reduced chi-square well
  above 1 and structured residuals.
* Time alignment between recording and model is assumed; there is no clock
  offset/drift estimation yet.
* Simulations run one after another; on the distributed workers they could
  run in parallel.
