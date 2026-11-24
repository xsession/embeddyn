# Run many simulations in parallel

Tuning and tolerance analysis are made of independent co-simulations. Both take a worker count:

```bash
embeddyn mechatronics tune examples/mechatronics/tuning/arm-firmware.yaml --workers 4
embeddyn mechatronics tolerance examples/mechatronics/tolerance/servo.yaml --workers 8
```

or `workers: 4` in the spec file.

## What happens

* A **process pool** with the `spawn` start method: every worker is a fresh Python process. Nothing leaks from the parent (sockets, threads, a loaded libngspice).
* Each evaluation builds its own adapters. Projects whose Renode instances have `launch: true` get **private endpoints**: a free TCP port per Renode and the log file renamed to `<log>.<pid>.log`, so emulators never collide.
* Results are keyed by input, not by completion order, so **the result is the same for any number of workers**. Tests check this for tuning (1 vs 4 workers give the same history) and tolerance.

## How much faster

| Workload | 1 worker | 4 workers |
|---|---|---|
| Nelder–Mead, arm firmware, 30 evaluations | ~11.8 min | 6.8 min (×1.7; 52 runs incl. speculative) |
| CMA-ES, arm firmware, 49 evaluations, λ = 8 | — | ~6.5 min on an idle host (utilisation 3.85/4) |
| tolerance, servo, 712 runs (200 Monte Carlo + 512 Sobol) | — | 3.6 min |

Nelder–Mead is sequential by nature; embeddyn runs its candidate points [speculatively](../theory/optimisation.md#speculative-parallel-neldermead), which caps utilisation at about 3 of 4 workers. CMA-ES and Monte Carlo/Sobol use every worker.

## Choosing the number of workers

* **CPU-bound.** Each Renode instance uses about one core while running; Python plants one core. Use at most the number of physical cores, minus one for the system.
* **Memory.** A Renode instance needs a few hundred MB. Check before going wide.
* **Shared hosts.** Wall times stretch with host load (we saw 30 s runs take 76–147 s at load average 11–14). The results do not change, only the time.
* **Temporary files.** Each run writes telemetry and logs; point `TMPDIR` and `--output-root` at a disk with room.

## Not (yet) supported

Spreading evaluations over the distributed worker queue (`worker-register`, `job-submit`) is not wired into `tune`/`tolerance`; they run on the local machine. See [Distributed runs, HIL and digital twin](../user/MECHATRONICS.md) for the queue itself.
