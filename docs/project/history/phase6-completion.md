# Phase 6 Completion — Digital Twin and Engineering Workflows

## Scope

Phase 6 adds the first production-oriented digital-twin workflow layer on top of the simulator, storage, distributed execution, and HIL foundations implemented in Phases 1–5.

## Implemented capabilities

### Measurement ingestion and state snapshots

- CSV, JSON, JSONL, and Parquet measurement input
- latest-state extraction for digital-twin synchronization
- normalized state snapshots with source time and provenance

### Simulation-to-measurement comparison

- time-axis discovery
- deterministic linear interpolation
- explicit signal-name mappings
- overlap filtering
- per-signal metrics:
  - mean absolute error
  - root-mean-square error
  - maximum absolute error
  - bias
  - normalized RMSE
- optional aligned CSV export

### Calibration fitting

- affine scale and offset fitting for each mapped signal
- before/after RMSE reporting
- coefficient-of-determination reporting
- versioned YAML calibration artifacts
- calibration registration in the metadata database

### Uncertainty analysis

- deterministic seeded Monte Carlo sampling
- normal, uniform, triangular, and discrete-choice distributions
- mean, standard deviation, minimum, maximum, P05, P50, and P95 summaries
- reproducible sample-set persistence

### Requirements traceability

- YAML requirement definitions
- metric-path lookup
- comparison operators: `<`, `<=`, `>`, `>=`, `==`, `!=`, and `between`
- passed, failed, and unresolved requirement states
- evidence-source recording
- machine-readable traceability JSON

### Evidence reports

- Markdown evidence package generation
- requirement summary
- simulation-to-measurement metric tables
- per-requirement verification details
- SHA-256 content-addressed report storage
- SQLite evidence-report registry
- optional linking of reports to simulation runs

### Phase 6 metadata storage

Database schema version 3 adds:

- `digital_twin_datasets`
- `calibrations`
- `evidence_reports`

## Commands

Create a current digital-twin state snapshot:

```bash
embeddyn mechatronics twin-state measurements.csv \
  --output .embeddyn/digital-twin/state.json
```

Compare simulation and measurements:

```bash
embeddyn mechatronics twin-compare \
  .embeddyn/runs/<run-id>/signals/telemetry.parquet \
  captures/motor-test.csv \
  --mapping examples/mechatronics/digital-twin/signal-mapping.yaml \
  --output .embeddyn/digital-twin/comparison.json \
  --aligned-output .embeddyn/digital-twin/aligned.csv
```

Fit calibration transforms:

```bash
embeddyn mechatronics twin-calibrate \
  simulation.parquet measurements.csv \
  --mapping signal-mapping.yaml \
  --output calibration/motor-unit-001.yaml
```

Run uncertainty sampling:

```bash
embeddyn mechatronics uncertainty-run \
  examples/mechatronics/digital-twin/uncertainty.yaml \
  --samples 10000 \
  --seed 42 \
  --output .embeddyn/digital-twin/uncertainty.json
```

Verify requirements:

```bash
embeddyn mechatronics requirements-verify \
  examples/mechatronics/digital-twin/requirements.yaml \
  .embeddyn/digital-twin/comparison.json \
  --output .embeddyn/evidence/traceability.json
```

Generate an evidence report:

```bash
embeddyn mechatronics evidence-report \
  .embeddyn/evidence/traceability.json \
  --comparison .embeddyn/digital-twin/comparison.json \
  --run-id <run-id> \
  --output .embeddyn/evidence/report.md
```

## Important limits

The implemented calibration is an affine signal correction baseline, not yet a nonlinear physical-parameter optimizer. Uncertainty sampling currently generates deterministic parameter ensembles and descriptive statistics; automatic distributed execution of every sample remains an extension of the Phase 5 experiment queue. Requirements verification evaluates stored metrics and does not replace formal safety certification.

## Next maturation steps

- nonlinear and bounded physical-parameter estimation
- experiment-queue integration for Monte Carlo samples
- live streaming twin synchronization
- time-offset and clock-drift estimation
- confidence intervals for fitted calibration values
- sensitivity and Sobol analysis
- automatic model-fidelity selection
- requirements-to-test-to-artifact graph UI
- signed evidence packages
- PDF and HTML engineering reports
