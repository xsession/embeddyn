# Phase 5 Operations Guide

## 1. Register a worker

```bash
embeddyn mechatronics worker-register \
  --name local-simulation-worker \
  --worker-id local-01 \
  --simulators mock,webots,ngspice,renode \
  --labels simulation,linux \
  --max-parallel-jobs 2
```

Record the returned worker ID. The same ID is used when starting the worker agent.

## 2. Start the worker

```bash
embeddyn mechatronics worker-run local-01
```

For CI or smoke testing, process no more than one available job:

```bash
embeddyn mechatronics worker-run local-01 --once
```

## 3. Submit an experiment

```bash
embeddyn mechatronics experiment-submit \
  examples/mechatronics/experiments/pid-sweep.yaml
```

Inspect progress:

```bash
embeddyn mechatronics experiment-status <experiment-id>
embeddyn mechatronics jobs-list
```

## 4. Worker matching

A worker is eligible only when all requested capabilities are present.

Supported requirement fields:

```yaml
requirements:
  simulators: [webots, ngspice]
  hardware: [can0, usb-debugger]
  labels: [simulation, lab-a]
  operating_system: linux
  architecture: x86_64
  real_time: true
```

## 5. HIL policy

Validate the policy before connecting equipment:

```bash
embeddyn mechatronics hil-validate examples/mechatronics/hil/safe-motor.yaml
```

Required operational rules:

1. Start disarmed.
2. Apply safe outputs before opening control flow.
3. Verify the physical emergency stop independently.
4. Arm only after operator authorization.
5. Send watchdog heartbeats from the real-time loop.
6. Trip on lost communication, emergency stop, or deadline violations.
7. Apply safe outputs before closing the hardware driver.

## 6. Recovery

- Queued jobs can be cancelled immediately.
- Active jobs retain a cancellation request for cooperative worker shutdown.
- Offline workers are marked with `workers-list --stale-after <seconds>`.
- Failed worker commands retain bounded stdout/stderr and output directory metadata.
- Run artifacts remain managed by the Phase 2 content-addressed store.
