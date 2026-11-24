# Mechatronic Architecture Implementation Status

## Phase 1 — Core model: complete baseline

- versioned project model and compatibility loading
- unit-aware quantities and typed signals
- declarative safe transforms
- immutable compilation, lock file, and run manifest
- component definition/instance baseline
- parameter provenance baseline
- scheduler, convergence, and real-time schema validation

## Phase 2 — Storage: complete baseline

- SQLite migrations and metadata registry
- content-addressed artifacts with integrity verification
- streaming Parquet/JSONL telemetry
- dedicated events and buses datasets
- run, artifact, telemetry, checkpoint, component, and parameter registration
- checkpoint artifacts and parameter provenance persistence

## Phase 3 — Runtime: complete baseline

- deterministic, multi-rate, real-time fixed-step, and real-time multi-rate scheduling
- declarative iterative coupling convergence
- adapter lifecycle, capabilities, health, and configurable worker timeout
- generic process-isolated workers for all built-in external adapters
- deterministic typed event queue and dedicated event/bus telemetry
- checkpoint/resume and checkpoint artifact registration
- deterministic replay verification
- activation and deadline diagnostics

Further detailed Webots, SPICE, KiCad, Renode, distributed execution, and HIL safety work continues in Phases 4 and 5.

## Phase 4 completed baseline

Engineering-tool integration contracts are now implemented for Webots, SPICE, KiCad, and Renode. The project includes validated binding manifests, a versioned component catalog, SPICE inspection, adapter capability declarations, integration validation commands, and deterministic CI tests. See `PHASE4_COMPLETION.md`.


## Phase 5

Status: **local distributed-execution and HIL-safety baseline implemented**.

Implemented worker registration, heartbeats, capability matching, persistent priority jobs, cancellation, experiment expansion/submission, local worker execution, HIL output gating, watchdogs, emergency-stop handling, deadline-miss trips, bounded I/O queues, examples, CLI commands, and tests. See `PHASE5_COMPLETION.md`.


## Phase 6 — Completed baseline

- digital-twin state snapshots
- simulation-to-measurement alignment and comparison
- MAE, RMSE, maximum error, bias, and normalized RMSE metrics
- affine calibration fitting and persistence
- deterministic uncertainty sampling
- requirements traceability verification
- Markdown evidence report generation
- content-addressed evidence artifacts
- database schema version 3 for datasets, calibrations, and reports

See `PHASE6_COMPLETION.md` for commands, implementation details, and remaining maturation work.


## Roadmap phase R1: Renode firmware-in-the-loop (working, hardening)

- External Control API client (`adapters/renode_api.py`) for the `custom-cores` Renode branch
- `RenodeAdapter`: virtual-time stepping with RunFor, PWM duty from GPIO edge events, ADC/GPIO/register I/O
- STM32_ADC `IADC` patch; example `dc-motor-renode.yaml`; fake-server tests
- Validated on a real custom-cores build (`tools/renode/build-custom-cores.sh`) with the reference STM32 firmware
  (`examples/mechatronics/firmware/motor-speed-control`); opt-in test `tests/mechatronics/test_renode_real.py`

See [GUI.md](../../reference/lab.md), [RENODE.md](../../reference/renode.md), [RC_SERVO_L476.md](../../reference/rc-servo-l476.md), [NGSPICE.md](../../reference/ngspice.md), [TUNING.md](../../reference/tuning.md), [INSTANCES_AND_FMI.md](../../reference/instances-and-fmi.md), [VERIFICATION.md](../../reference/verification.md), [ESTIMATION.md](../../reference/estimation.md), [TOLERANCE.md](../../reference/tolerance.md) and [ROADMAP.md](../roadmap.md).
