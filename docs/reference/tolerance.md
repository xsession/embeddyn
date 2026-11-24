# Tolerance analysis: Monte Carlo yield and Sobol sensitivity

`embeddyn mechatronics tolerance spec.yaml` answers two questions about a
design whose components have manufacturing spread:

* **Monte Carlo:** what fraction of built units meets the requirements
  (yield), and how are the response metrics distributed?
* **Sensitivity (Sobol indices):** which tolerances cause that spread, and so
  which ones are worth tightening?

```bash
embeddyn mechatronics tolerance examples/mechatronics/tolerance/servo.yaml -o result.json
embeddyn mechatronics tolerance spec.yaml --workers 8 --samples 500
```

## Spec

```yaml
project: ../servo-drivetrain.yaml
duration: 1.0
seed: 1                       # reproducible samples
workers: 8                    # parallel processes (launched Renodes get their own ports and logs)
parameters:                   # project parameters or constant signals
  motor.resistance_20c: {nominal: 2.0, tolerance: 0.05, distribution: normal}   # +-5 % = 3 sigma
  drivetrain.backlash: {distribution: uniform, minimum: 0.002, maximum: 0.008}
  drivetrain.coulomb_friction: {distribution: triangular, minimum: 0.15, mode: 0.25, maximum: 0.4}
  electrical.supply_voltage: {distribution: normal, mean: 24.0, sigma: 0.4}
outputs: [speed.overshoot, speed.settling_time, shaft.peak]
limits:
  - {metric: speed.overshoot, max: 10.0}
  - {metric: shaft.peak, max: 20.0}
monte_carlo: {samples: 200, sampling: lhs}      # lhs (Latin hypercube) or random
sensitivity: {base_samples: 64, bootstrap: 200}
fixed: {}                                       # values held constant
```

Distributions: `normal` (mean, sigma), `uniform` (minimum, maximum),
`triangular` (minimum, mode, maximum), `lognormal` (median, sigma_log),
`choice` (values), and the shorthand `nominal` + relative `tolerance` with
`distribution: uniform` (± tolerance) or `normal` (tolerance = 3 σ, the usual
reading of a datasheet tolerance). Parameters are applied the same way as in
[estimation](estimation.md): a constant signal gets its initial value, any
other name becomes a project parameter (Python plants and FMUs read those
through `"@name"`). Metrics come from the project's `metrics:`
([TUNING.md](tuning.md)); limit metrics are added to `outputs` automatically.

## Monte Carlo

Samples are drawn by Latin hypercube (each parameter's range is split into
equal-probability strata, one sample per stratum) or plain random sampling,
then mapped through each distribution's inverse CDF. The report has:

* per output: mean, standard deviation, min/max, 5/50/95 % percentiles, and
  how many runs gave no value (e.g. never settled);
* **yield**: the fraction of samples meeting every limit, with a Wilson 95 %
  interval, plus the pass rate of each limit on its own;
* the first 20 failing samples with their parameter values, and the full
  sample set.

## Sobol sensitivity

Two independent sample matrices A and B (N rows) and, for each of the k
parameters, A with that column taken from B: N·(k + 2) runs in total. The
outputs are centred, then:

* **first-order index S_i** (Saltelli 2010): the share of output variance
  caused by parameter i on its own;
* **total index S_Ti** (Jansen 1999): S_i plus every interaction involving i.
  S_Ti ≈ 0 means the tolerance doesn't matter for that output;
* 95 % bootstrap intervals for both, and a ranking by S_Ti.

ΣS_i ≈ 1 means the output is additive in the parameters; well below 1 means
interactions matter. An output that is missing in some runs is skipped with
a note.

The estimators are checked against closed-form answers: a linear model
(S = 0.2 / 0.8) and the Ishigami benchmark (S₁ = 0.314, S₂ = 0.442, S₃ = 0,
S_T3 = 0.244), both within ±0.05 (`tests/mechatronics/test_tolerance.py`).

Centring matters in practice. Before it was added, the winding-temperature
output (mean 28 °C, spread 0.2 K) gave first-order intervals of about ±40;
after centring they are ±0.25.

## Example: geared servo

`examples/mechatronics/tolerance/servo.yaml`
varies six quantities of `servo-drivetrain.yaml`: winding resistance
(±5 %), gearbox backlash (2–8 mrad), load inertia (±10 %), Coulomb friction,
process load torque and supply voltage. 712 runs (200 Monte Carlo + 512 Sobol)
took 109 s with 8 workers.

**Yield: 64.5 % (95 % CI 57.7–70.8 %).** Overshoot (mean 4.1 %, p95 5.1 %)
and settling time (mean 0.28 s, p95 0.32 s) pass in every sample; the only
failing limit is the **20 N·m shaft-torque peak** (mean 19.2, p95 20.7), which
fails in 35.5 % of units.

| Output | Dominant tolerance (S_T) | Next |
|--------|--------------------------|------|
| shaft torque peak | **backlash 0.92** (S₁ 1.0) | resistance 0.04, supply 0.03 |
| overshoot | load inertia 0.86 | backlash 0.23 |
| settling time | load inertia 0.90 | resistance 0.09 |
| winding temperature | load inertia 0.80 | supply 0.04 |
| energy | load inertia 0.76 | load torque 0.05 |

So the yield problem is the gearbox backlash: tightening the motor or supply
tolerances would not help, and a tighter backlash spec (or limiting the
acceleration that slams through the gap) would. Friction and process load
barely matter for any output.

## Running in parallel and limits

* `workers` runs evaluations in a process pool; results are identical to a
  serial run (tested). Projects that launch Renode work too: with more than
  one worker every launched Renode gets a free port and its own log file
  (`<log>.<pid>.log`). ngspice-shared works, because each worker process has
  its own library instance.
* The runs execute on the local machine. Spreading them over the distributed
  worker queue (`mechatronics worker`) is not wired up yet.
* Sample sizes: Sobol indices with N = 64 resolve the dominant effects
  (intervals about ±0.2–0.3); small indices need larger N.
