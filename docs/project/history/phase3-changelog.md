# Phase 3 Runtime Implementation Changelog

## Implemented baseline

- Scheduler strategy interface.
- Deterministic fixed-step scheduler.
- True integer-ratio multi-rate fixed-step scheduler.
- Stable activation ordering by simulation time and domain priority.
- Adapter lifecycle manager with prepare, start, health, checkpoint, restore, and stop operations.
- Typed runtime event queue with deterministic same-time ordering.
- Bus/event output handling in the orchestrator.
- JSON checkpoint creation and resume support.
- Adapter state checkpointing for deterministic mock simulators.
- Process-isolated adapter worker protocol using supervised child processes.
- Health and capability reporting for adapters.
- Runtime activation counts in run manifests and CLI output.
- CLI options for isolated adapters, periodic checkpoints, and resume.

## New CLI options

```bash
embeddyn mechatronics run examples/mechatronics/dc-motor.yaml \
  --mock \
  --isolate-adapters \
  --checkpoint-interval 0.1

embeddyn mechatronics run examples/mechatronics/dc-motor.yaml \
  --mock \
  --resume-from .embeddyn/runs/<run-id>/checkpoints/checkpoint-0000000000100000.json
```

## Current boundaries

- Generic process isolation is implemented, but automatic worker specifications are currently wired for built-in mock adapters. External adapters require explicit serializable worker specifications.
- Iterative convergence scheduling is not yet implemented.
- Real-time/HIL scheduling remains planned.
- External simulator checkpoint support depends on each simulator adapter.
- Event telemetry has an in-memory deterministic queue; a dedicated columnar event dataset is planned.
