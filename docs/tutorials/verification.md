# 10 · Verifying a model

**Goal:** check that the DC motor simulation conserves energy, converges with the step size, has not changed since it was last approved, and meets its requirements across operating conditions.

**You need:** the Python package.

## Step 1 — the checks in the project

The `verification:` section of `examples/mechatronics/dc-motor.yaml`:

```yaml
verification:
  energy:
    tolerance: 0.02
    sources: [{name: electrical_input, product: [electrical.motor_voltage, electrical.motor_current]}]
    sinks:
      - {name: copper,  product: [electrical.motor_current, electrical.motor_current, electrical.motor_resistance]}
      - {name: viscous, product: [mechanical.damping, mechanical.velocity, mechanical.velocity]}
      - {name: load,    product: [mechanical.load_torque, mechanical.velocity]}
    storage:
      - {name: inductance, product: [0.5, electrical.motor_inductance, electrical.motor_current, electrical.motor_current]}
      - {name: rotor,      product: [0.5, mechanical.inertia, mechanical.velocity, mechanical.velocity]}
  convergence:
    signals: [mechanical.velocity, electrical.motor_current]
    tolerance: 0.01
  golden:
    file: golden/dc-motor.csv
    every: 0.01
    signals: {mechanical.velocity: 0.001, electrical.motor_current: 0.01}
  scenarios:
    - name: low-supply
      signals: {electrical.supply_voltage: 18.0}
      expect: [{metric: speed.final_value, min: 4.5}, {metric: current.peak, max: 15.0}]
    - name: heavy-load
      signals: {mechanical.load_torque: 0.3}
      expect: [{metric: speed.final_value, min: 4.5}]
```

## Step 2 — run

```bash
embeddyn mechatronics verify examples/mechatronics/dc-motor.yaml --mock -o verify.json
```

```text
energy: PASS
golden: PASS
convergence: PASS
  scenario nominal: PASS (current.peak=11.9191)
  scenario low-supply: PASS (speed.final_value=5.2754, current.peak=8.9799)
  scenario heavy-load: PASS (speed.final_value=5.0406)
  scenario aggressive-gains-anti-windup: PASS (speed.overshoot=0.0, current.peak=11.9839)
scenarios: PASS
```

The command exits with status 1 if anything fails, so it can gate CI.

## Step 3 — read the numbers

**Energy.** 66.74 J supplied; 65.67 J copper loss, 0.42 J viscous, 0.03 J load work, 0.65 J stored in inductance and rotor. The residual is −0.036 J: **0.05 %** of the input. The coupling scheme neither creates nor loses meaningful energy at 0.5 ms.

**Convergence.** Runs at 0.5, 0.25 and 0.125 ms:

| Signal | relative change h/2 → h/4 | observed order |
|---|---|---|
| velocity | 0.12 % | 0.99 |
| current | 0.19 % | 1.12 |

First order, as expected for explicit co-simulation with one-step delays. Halving the step halves the coupling error.

**Golden.** Maximum difference \(3.6\times10^{-15}\): the run is bit-for-bit reproducible.

## Step 4 — break it on purpose

| Change | Relative residual | With `tolerance: 0.02` | With `tolerance: 0.002` |
|---|---|---|---|
| none | 0.05 % | pass | pass |
| `viscous` sink removed (missing physics) | 0.58 % | pass | **fail** |
| torque coupling `gain: 0.09` (Kt ≠ Ke, energy created at the coupling) | 0.24 % | pass | **fail** |

Both mistakes move the residual by a factor of 5–10, but the example's 2 % tolerance is loose enough to hide them, because in this short run little energy passes through the rotor. The lesson: **set the tolerance just above the residual of the known-good model** (here 0.2 %), so the check is sensitive to the errors it exists to catch.

Changing a parameter and running without `--update-golden` makes the golden check report the first deviations with times and values.

## Step 5 — approve a change

When a model change is intended:

```bash
embeddyn mechatronics verify examples/mechatronics/dc-motor.yaml --mock --update-golden
```

and commit the new `golden/dc-motor.csv` with the change, so the review shows the waveform difference.

Theory: [Model verification](../theory/verification.md).
