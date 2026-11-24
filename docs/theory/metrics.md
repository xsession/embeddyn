# Metrics

A run produces rows; **metrics** reduce them to numbers you can compare, optimise and test against limits. They are declared in the project and computed by `metrics.compute_metrics` after every run (`run`, `tune`, `tolerance`, `verify`, the Lab).

```yaml
metrics:
  lift:    {type: step_response, signal: arm.angle, target: mcu.target, start: 2.0, end: 3.0, settling_band: 0.02}
  current: {type: stats, signal: motor.current}
  energy:  {type: integral, signal: motor.power}
```

Each metric `name` yields several figures named `name.figure` (`lift.overshoot`, `current.peak`, `energy.integral`). An optional `[start, end]` window restricts the samples.

## Step response

Let \(y_0\) be the first sample in the window, \(r\) the reference (the last value of `target`, which may be a signal or a number), \(\Delta = r - y_0\) the step, and \(e(t) = r(t) - y(t)\) the error.

| Figure | Definition |
|---|---|
| `final_value` | mean of the last 5 % of samples |
| `steady_state_error` | \(r - y_\text{final}\) |
| `iae` | \(\int \lvert e\rvert\,dt\) |
| `ise` | \(\int e^2\,dt\) |
| `itae` | \(\int (t - t_0)\,\lvert e\rvert\,dt\) |
| `rise_time` | time from 10 % to 90 % of \(\Delta\) (linear interpolation between samples) |
| `overshoot` | \(100 \cdot \max\!\big(0, \operatorname{sign}(\Delta)(y_\text{peak} - r)/\lvert\Delta\rvert\big)\) % |
| `peak_time` | time of the extreme sample in the step direction |
| `settling_time` | time after which \(\lvert y - r\rvert \le \beta\lvert\Delta\rvert\) for good (\(\beta\) = `settling_band`, default 2 %) |

Integrals use the trapezoidal rule on the macro-step samples.

**ITAE** weights late errors by time, so it penalises slow settling and sustained oscillation much more than the initial transient that every controller must have. That makes it a good single objective for tuning.

**Missing values.** A response that never reaches 90 % has `rise_time: null`; one that is still outside the band at the end has `settling_time: null`. They are reported as missing, not as a large number, so a tuning constraint on settling time treats "never settles" as a violation rather than a lucky small value.

## Statistics

`peak` = \(\max\lvert x\rvert\), `rms` = \(\sqrt{\tfrac{1}{T}\int x^2\,dt}\), `mean` = \(\tfrac1T\int x\,dt\), `min`, `max`. The time-weighted forms stay correct with non-uniform samples.

## Integral

`integral` = \(\int x\,dt\). With a power signal it is energy in joules.

## Using metrics

| Where | How |
|---|---|
| `run` output | the summary JSON lists all figures |
| tuning | `objective:` weights and `constraints:` limits ([Optimisation](optimisation.md)) |
| tolerance | `outputs:` and `limits:` define yield ([Uncertainty](uncertainty.md)) |
| verification | scenario `expect:` entries ([Verification](verification.md)) |
| Lab | the **Metrics** tab after each run |

Figures can be named in full (`lift.overshoot`) or by a unique suffix (`overshoot`).
