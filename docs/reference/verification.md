# Model verification

`embeddyn mechatronics verify <project>` runs the checks declared in the
project's `verification:` section, prints PASS/FAIL per check and a JSON
report, and **exits with status 1 if any check fails**, so it can gate CI.

```bash
embeddyn mechatronics verify examples/mechatronics/dc-motor.yaml -o report.json
embeddyn mechatronics verify examples/mechatronics/dc-motor.yaml --check energy --check convergence
embeddyn mechatronics verify examples/mechatronics/dc-motor.yaml --update-golden
```

| Check | Question it answers |
|-------|---------------------|
| `energy` | Does the coupled model conserve energy (first law over the run)? |
| `convergence` | Is the step size small enough, and how fast does the error shrink with it? |
| `golden` | Did the behaviour change against a stored reference waveform? |
| `scenarios` | Does the design meet its limits across operating conditions? |

## Energy balance

```yaml
verification:
  energy:
    tolerance: 0.02          # |residual| / source energy
    sources: [{name: electrical_input, product: [electrical.motor_voltage, electrical.motor_current]}]
    sinks:
      - {name: copper, product: [electrical.motor_current, electrical.motor_current, electrical.motor_resistance]}
      - {name: drivetrain_losses, signal: mechanical.dissipated_power}
    storage:
      - {name: inductance, product: [0.5, electrical.motor_inductance, electrical.motor_current, electrical.motor_current]}
```

Residual = ∫sources − ∫sinks − (E_end − E_start) over the storage terms. Each
term is a `signal` or a `product` of factors; a factor is a signal (value per
sample), a project parameter, or a number. Powers are integrated with the
trapezoidal rule over the run's output samples.

Energy flowing between domains through couplings (here e·i on the electrical
side and T·ω on the mechanical side, with Kt = Ke) cancels out when the
coupling is consistent. So the residual measures what the coupling scheme
(explicit steps, one-step delays) creates or destroys numerically, and any
missing physics. `GearedDrivetrain` reports `dissipated_power` (bearings,
friction, shaft damper, gear loss) and `stored_energy` (kinetic + shaft
spring) for this purpose.

## Step-size convergence

```yaml
  convergence:
    signals: [mechanical.velocity, electrical.motor_current]
    tolerance: 0.01          # RMS(x(h/2) - x(h/4)) / RMS(x(h/4))
    duration: 0.5            # optional, to keep the three runs short
```

The project runs at h, h/2 and h/4. The macro step, domain steps and every
instance step are divided together, so a multi-rate system keeps its rate
ratios. Each signal is compared on the coarse time grid. The report gives
the RMS change between successive refinements, the relative change at the
finest pair (the pass criterion), and the **observed order**
p = log₂(e(h→h/2) / e(h/2→h/4)).

An observed order near 1 is what explicit co-simulation with one-step delays
gives. A much lower order than expected points at a coupling or discontinuity
problem; a higher one means the error is dominated by an inner solver.

## Golden waveforms

```yaml
  golden:
    file: golden/dc-motor.csv    # relative to the project file
    every: 0.01                  # sampling interval
    signals: {mechanical.velocity: 0.001, electrical.motor_current: 0.01}   # absolute tolerances
```

The first run (or `--update-golden`) writes the file; later runs compare the
listed signals sample by sample and report the worst error per signal plus
the first ten violations.

## Scenario matrix

```yaml
  scenarios:
    - name: low-supply
      signals: {electrical.supply_voltage: 18.0}     # initial-value overrides
      parameters: {controller.kp: 0.2}               # parameter overrides
      duration: 1.0
      expect: [{metric: speed.final_value, min: 4.5}, {metric: current.peak, max: 15.0}]
```

Every scenario is a full run with its overrides; `expect` entries check the
project's metrics ([TUNING.md](tuning.md)) against `min`/`max`. A metric that
can't be computed (e.g. a response that never settles) counts as a failure,
and a scenario that crashes is reported with its error instead of aborting
the matrix.

Values a model holds as its own state (a plant's initial temperature, say)
only change when the scenario sets the parameter that feeds them. Setting
the signal's initial value alone changes only the first output sample. The
servo example's `hot-start` scenario sets both.

## Results on the examples

| Example | Energy residual | Convergence (relative change, order) | Scenarios |
|---------|-----------------|--------------------------------------|-----------|
| `dc-motor.yaml` (mock firmware, RL motor, rotor) | 0.05 % of 66.7 J | speed 0.12 % / p ≈ 0.99; current 0.19 % / p ≈ 1.12 | 4/4 pass |
| `servo-drivetrain.yaml` (4 domains, backlash, thermal) | 0.08 % of 34.4 J | 4 signals ≤ 0.31 %, p ≈ 1.0–1.55 | 3/3 pass |
| `two-wheel-drive.yaml` (6 instances, FMU) | – (FMU has no loss outputs) | 4 signals ≤ 0.55 %, p ≈ 0.97–1.4 | 2/2 pass |

The scenario matrix already caught a real design issue: with aggressive gains
(kp 2, ki 5) the mock controller overshoots by 20 % through integrator windup.
The example now runs that scenario with `controller.anti_windup: 1`, which
brings overshoot to 0 %.

The `Mechatronics verification` CI workflow
(`.github/workflows/mechatronics-verify.yml`) runs the unit tests and these
three verifications on every push to the mechatronics code, without Renode or
ngspice.
