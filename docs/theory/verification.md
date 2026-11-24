# Model verification

Validation asks "does the model match the hardware?" ([estimation](estimation.md) helps there). **Verification** asks the prior question: "does the simulation solve the model correctly?" `embeddyn mechatronics verify` runs four independent checks declared in the project's `verification:` section.

| Check | Catches |
|---|---|
| [Energy balance](#energy-balance) | inconsistent couplings, missing loss terms, numerical energy creation |
| [Step-size convergence](#step-size-convergence) | step too large, coupling-order problems, discontinuities |
| [Golden waveforms](#golden-waveforms) | unintended changes (regressions) |
| [Scenario matrix](#scenario-matrix) | requirements across operating conditions |

## Energy balance

Over a run from \(t_0\) to \(T\), energy must be conserved:

\[
\underbrace{\int P_\text{sources}\,dt}_{\text{supplied}} = \int P_\text{sinks}\,dt + \big(E_\text{stored}(T) - E_\text{stored}(t_0)\big) + \text{residual}.
\]

Each term is a signal or a product of signals, parameters and numbers, integrated with the trapezoidal rule. For the DC motor:

\[
\int V i\,dt = \int i^2 R\,dt + \int b\,\omega^2\,dt + \int \tau_L\,\omega\,dt + \Delta\big(\tfrac12 L i^2\big) + \Delta\big(\tfrac12 J \omega^2\big) + \text{residual}.
\]

Power crossing between domains through couplings (\(e\,i\) on the electrical side, \(\tau\,\omega\) on the mechanical side, with \(K_t = K_e\)) cancels when the coupling is consistent. The residual therefore measures what the **coupling scheme** creates or destroys numerically (explicit steps, one-step delays), plus any physics left out of the balance. The pass criterion is \(\lvert\text{residual}\rvert / \text{supplied} \le\) `tolerance`. The DC motor gives 0.05 % at \(h\) = 0.5 ms.

## Step-size convergence

The project runs at \(h\), \(h/2\) and \(h/4\) (macro, domain and instance steps all divided together, so multi-rate ratios are kept). With \(e_1 = \lVert x_h - x_{h/2}\rVert_\text{RMS}\) and \(e_2 = \lVert x_{h/2} - x_{h/4}\rVert_\text{RMS}\) on the coarse grid:

\[
\text{relative change} = \frac{e_2}{\lVert x_{h/4}\rVert_\text{RMS}}, \qquad p_\text{obs} = \log_2\frac{e_1}{e_2}.
\]

The check passes when the relative change is below `tolerance`. The **observed order** \(p_\text{obs}\) diagnoses the scheme:

| \(p_\text{obs}\) | Meaning |
|---|---|
| ≈ 1 | expected for explicit co-simulation with one-step delays (DC motor: 0.99 and 1.12) |
| ≪ 1 | a coupling or discontinuity problem: error not shrinking with \(h\) |
| > 1 | the error is dominated by an inner solver, not the coupling |

## Golden waveforms

The first run (or `--update-golden`) writes selected signals at a fixed interval to a CSV; later runs compare sample by sample against absolute tolerances and list the worst error per signal and the first violations. Because runs are deterministic, the DC motor reproduces its golden file to \(3.6\times10^{-15}\): any visible difference is a real change.

## Scenario matrix

Each scenario is a full run with overrides of signal initial values and parameters, an optional duration, and `expect` entries checked against the project's metrics:

```yaml
scenarios:
  - name: low-supply
    signals: {electrical.supply_voltage: 18.0}
    expect: [{metric: speed.final_value, min: 4.5}, {metric: current.peak, max: 15.0}]
```

A metric that cannot be computed counts as a failure; a scenario that crashes is reported with its error and does not abort the matrix. This is the natural home for requirements ("at 18 V supply the joint still reaches 4.5 rad/s").

## In CI

`verify` exits non-zero when a check fails and writes a JSON report (`-o`), so it can gate merges. With `--mock` it needs no external simulators; with Renode and ngspice available it verifies the real thing.
