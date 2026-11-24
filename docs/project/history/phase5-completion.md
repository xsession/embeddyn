# Phase 5 Completion: Distributed Execution, Experiments, and HIL Safety

## Scope

Phase 5 adds a scalable local coordinator baseline for remote-capable workers, persistent experiment queues, capability-aware scheduling, cancellable jobs, and a fail-safe hardware-in-the-loop boundary.

The implementation remains usable on one workstation with SQLite, while the coordinator interfaces are isolated so PostgreSQL and network service transports can replace the local backend later without changing the engineering model.

## Distributed coordinator

Implemented in `embeddyn/mechatronics/distributed/`:

- persistent worker registry
- worker heartbeats and stale-worker detection
- simulator, hardware, label, operating-system, architecture, and real-time capabilities
- maximum concurrent-job limits
- persistent priority job queue
- atomic job claiming through `BEGIN IMMEDIATE`
- capability-aware worker/job matching
- claimed, running, completed, failed, and cancelled states
- cancellation requests for active jobs
- experiment registry and aggregate status
- local worker agent that launches the existing `embeddyn mechatronics run` workflow
- bounded stdout/stderr capture in job results

## Experiment execution

Experiment YAML can define:

- a base project
- discrete parameter values
- linear ranges
- logarithmic ranges
- worker requirements
- priority
- base run arguments
- expansion safety limit through `max_jobs`

Expansion uses a deterministic Cartesian product and stores each variation as a separate immutable job.

Example:

```yaml
name: dc-motor-pid-sweep
project: ../dc-motor.yaml
requirements:
  simulators: [mock]
  labels: [simulation]
variables:
  controller.kp:
    values: [0.2, 0.4, 0.6]
  controller.ki:
    range:
      start: 0.01
      stop: 0.1
      count: 3
      scale: logarithmic
```

## HIL safety boundary

Implemented in `embeddyn/mechatronics/hil/`:

- disarmed-by-default state
- explicit arm-token policy
- output allowlist
- minimum and maximum output limits
- safe values for every hardware output
- optional output slew-rate limits
- emergency-stop input monitoring
- watchdog timeout
- deadline-miss trip policy
- immediate safe-output application on trip, disarm, or shutdown
- structured safety-event history
- bounded I/O queue with oldest-sample dropping
- deterministic mock HIL device for tests

This layer is intentionally separated from simulator adapters so unsafe output cannot bypass the safety gate.

## CLI commands

```bash
embeddyn mechatronics worker-register \
  --name worker-a \
  --simulators mock,webots,ngspice \
  --labels simulation \
  --max-parallel-jobs 2

embeddyn mechatronics workers-list

embeddyn mechatronics job-submit examples/mechatronics/dc-motor.yaml
embeddyn mechatronics jobs-list
embeddyn mechatronics job-cancel <job-id>

embeddyn mechatronics worker-run <worker-id>
embeddyn mechatronics worker-run <worker-id> --once

embeddyn mechatronics experiment-expand \
  examples/mechatronics/experiments/pid-sweep.yaml

embeddyn mechatronics experiment-submit \
  examples/mechatronics/experiments/pid-sweep.yaml

embeddyn mechatronics experiment-status <experiment-id>

embeddyn mechatronics hil-validate \
  examples/mechatronics/hil/safe-motor.yaml
```

## Database objects

The coordinator creates these tables in the selected SQLite database:

- `distributed_workers`
- `distributed_jobs`
- `distributed_experiments`

The existing run, artifact, telemetry, parameter, checkpoint, and component registries remain unchanged.

## Tests

Phase 5 tests cover:

- capability-aware job matching
- priority behavior with incompatible jobs
- atomic lifecycle transitions
- active-job accounting
- queued-job cancellation
- Cartesian experiment expansion
- linear range expansion
- HIL arm authorization
- output clamping
- emergency-stop trip
- watchdog trip and safe-state application

## Production qualification still required

The software architecture and local coordinator baseline are implemented, but production deployment still requires environment-specific qualification for:

- PostgreSQL repository implementation
- S3-compatible artifact backend
- authenticated network coordinator API
- TLS and worker identity provisioning
- deployment-specific queue recovery policy
- physical emergency-stop hardware wiring
- real CAN, serial, USB, GPIO, and FPGA I/O drivers
- operating-system real-time scheduling privileges
- measured latency and jitter limits
- safety review appropriate to the controlled equipment

The HIL subsystem is a software safety layer and must not replace independently wired safety hardware.
