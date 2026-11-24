# Command line

All mechatronics commands live under `embeddyn mechatronics` (or `python -m embeddyn mechatronics`). This page is generated from the command definitions by `tools/docs/make_cli_reference.py`; `--help` on any command shows the same.

| Command | Purpose |
|---|---|
| [`compile`](#compile) | Compile a project into a run directory with its manifest and lock file, without running it. |
| [`run`](#run) | Run a co-simulation; write telemetry, a manifest and optionally a CSV, and print final values and metrics. |
| [`metrics`](#metrics) | Compute the project's declared metrics from an exported run CSV. |
| [`tune`](#tune) | Tune project parameters by repeated co-simulation (bounded Nelder-Mead or CMA-ES). |
| [`verify`](#verify) | Verify a model: energy balance, step-size convergence, golden waveforms, scenario matrix. Exits with status 1 when a check fails. |
| [`estimate`](#estimate) | Estimate physical parameters from bench measurements (Levenberg-Marquardt, 95 % confidence intervals). |
| [`tolerance`](#tolerance) | Monte Carlo yield and Sobol sensitivity over component tolerances. |
| [`validate`](#validate) | Parse and validate a project file without starting any simulator. |
| [`runs-list`](#runs-list) | List recorded runs from the metadata database. |
| [`runs-inspect`](#runs-inspect) | Show one recorded run: manifest, parameters, artifacts. |
| [`runs-export`](#runs-export) | Export a recorded run's signals. |
| [`artifacts-verify`](#artifacts-verify) | Verify the content hashes of every artifact in the content-addressed store. |
| [`replay`](#replay) | Check that a recorded run directory replays consistently. |
| [`gui`](#gui) | Embeddyn Lab: schematic editor, virtual instruments and 3-D view in the browser. |
| [`fmu-export`](#fmu-export) | Export a python plant as an FMI 3.0 co-simulation FMU (PythonFMU3), with state save/restore. |
| [`fmu-check`](#fmu-check) | Validate an FMU (FMPy model-description checks) and run a short co-simulation. |
| [`bindings-from-pinplan`](#bindings-from-pinplan) | Generate Renode GPIO/PWM/ADC bindings from a CodeLayer Forge pin plan. |
| [`doctor`](#doctor) | Report which external tools (Renode, ngspice, Webots, KiCad, LTspice) are found. |
| [`kicad-export`](#kicad-export) | Export a KiCad schematic to a SPICE netlist with kicad-cli. |
| [`kicad-plugin`](#kicad-plugin) | Create a KiCad IPC plugin skeleton that talks to the local Studio API. |
| [`integration-validate`](#integration-validate) | Check a project's integrations: model files, bindings, FMU variables, tool availability. |
| [`catalog-list`](#catalog-list) | List the component definitions found under a catalog root. |
| [`kicad-inspect`](#kicad-inspect) | Summarise a SPICE netlist (sources, models, subcircuits) without KiCad. |
| [`worker-register`](#worker-register) | Register a worker with the distributed coordinator. |
| [`workers-list`](#workers-list) | List registered workers. |
| [`job-submit`](#job-submit) | Submit a simulation job to the distributed queue. |
| [`jobs-list`](#jobs-list) | List queued and finished jobs. |
| [`job-cancel`](#job-cancel) | Cancel a queued job. |
| [`experiment-expand`](#experiment-expand) | Expand an experiment file into its individual runs (sweeps, matrices). |
| [`experiment-submit`](#experiment-submit) | Submit every run of an experiment to the queue. |
| [`experiment-status`](#experiment-status) | Show the progress of a submitted experiment. |
| [`hil-validate`](#hil-validate) | Validate a hardware-in-the-loop safety policy (watchdog, emergency stop, outputs, deadline trips). |
| [`worker-run`](#worker-run) | Run a worker agent that executes queued jobs. |
| [`twin-state`](#twin-state) | Create a digital-twin state snapshot. |
| [`twin-compare`](#twin-compare) | Compare simulated and measured signal files. |
| [`twin-calibrate`](#twin-calibrate) | Fit digital-twin parameters to measured data. |
| [`uncertainty-run`](#uncertainty-run) | Run an uncertainty propagation from a specification file. |
| [`requirements-verify`](#requirements-verify) | Check requirements against computed metrics. |
| [`evidence-report`](#evidence-report) | Render a Markdown evidence report from verification results. |

## compile

Compile a project into a run directory with its manifest and lock file, without running it.

```text
embeddyn mechatronics compile PROJECT_FILE [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--output-root` | `.embeddyn/runs` |  |

## run

Run a co-simulation; write telemetry, a manifest and optionally a CSV, and print final values and metrics.

```text
embeddyn mechatronics run PROJECT_FILE [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--output`, `-o` |  | Optional CSV export path. |
| `--output-root` | `.embeddyn/runs` |  |
| `--mock`, `--no-mock` | `False` | Use deterministic built-in simulators for all domains. |
| `--isolate-adapters` | `False` | Run supported adapters in supervised child processes. |
| `--checkpoint-interval` |  | Checkpoint interval in seconds. |
| `--resume-from` |  |  |
| `--parameter-overrides` |  | JSON object of resolved parameter overrides. |

## metrics

Compute the project's declared metrics from an exported run CSV.

```text
embeddyn mechatronics metrics PROJECT_FILE RUN_CSV [OPTIONS]
```

## tune

Tune project parameters by repeated co-simulation (bounded Nelder-Mead or CMA-ES).

```text
embeddyn mechatronics tune SPEC_FILE [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--output`, `-o` | `.embeddyn/tuning/result.json` |  |
| `--max-evaluations` |  |  |
| `--workers` |  | Parallel evaluations (overrides the spec). |
| `--method` |  | nelder-mead or cmaes (overrides the spec). |

## verify

Verify a model: energy balance, step-size convergence, golden waveforms, scenario matrix. Exits with status 1 when a check fails.

```text
embeddyn mechatronics verify PROJECT_FILE [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--check` |  | Run only these checks: energy, convergence, golden, scenarios. |
| `--mock`, `--no-mock` | `False` | Use the built-in simulators for the core domains. |
| `--update-golden` | `False` | Rewrite the golden waveform file. |
| `--output`, `-o` |  | Write the JSON report here. |

## estimate

Estimate physical parameters from bench measurements (Levenberg-Marquardt, 95 % confidence intervals).

```text
embeddyn mechatronics estimate SPEC_FILE [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--output`, `-o` | `.embeddyn/estimation/result.json` |  |

## tolerance

Monte Carlo yield and Sobol sensitivity over component tolerances.

```text
embeddyn mechatronics tolerance SPEC_FILE [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--output`, `-o` | `.embeddyn/tolerance/result.json` |  |
| `--workers` |  | Parallel processes (overrides the spec). |
| `--samples` |  | Monte Carlo samples (overrides the spec). |

## validate

Parse and validate a project file without starting any simulator.

```text
embeddyn mechatronics validate PROJECT_FILE [OPTIONS]
```

## runs-list

List recorded runs from the metadata database.

```text
embeddyn mechatronics runs-list [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--database` | `.embeddyn/database/metadata.sqlite3` |  |
| `--limit` | `50` |  |

## runs-inspect

Show one recorded run: manifest, parameters, artifacts.

```text
embeddyn mechatronics runs-inspect RUN_ID [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--database` | `.embeddyn/database/metadata.sqlite3` |  |

## runs-export

Export a recorded run's signals.

```text
embeddyn mechatronics runs-export RUN_ID [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--output`, `-o` |  |  |
| `--database` | `.embeddyn/database/metadata.sqlite3` |  |

## artifacts-verify

Verify the content hashes of every artifact in the content-addressed store.

```text
embeddyn mechatronics artifacts-verify [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--root` | `.embeddyn/artifacts` |  |

## replay

Check that a recorded run directory replays consistently.

```text
embeddyn mechatronics replay RUN_DIRECTORY [OPTIONS]
```

## gui

Embeddyn Lab: schematic editor, virtual instruments and 3-D view in the browser.

```text
embeddyn mechatronics gui PROJECT_FILE [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Interface to bind (keep it local: the API runs simulators). |
| `--port` | `8710` |  |
| `--open` | `False` | Open the Lab in the default browser. |

## fmu-export

Export a python plant as an FMI 3.0 co-simulation FMU (PythonFMU3), with state save/restore.

```text
embeddyn mechatronics fmu-export PLANT [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--output`, `-o` | `.` | Directory for <name>.fmu. |
| `--name` |  | Model name (default: the class name). |
| `--param`, `-p` |  | Parameter default, name=value (repeat). |
| `--input-start` |  | Input start value, name=value. |
| `--max-step` | `0.0001` | RK4 sub-step inside each doStep [s]. |
| `--check`, `--no-check` | `True` | Validate and simulate the result with FMPy. |

## fmu-check

Validate an FMU (FMPy model-description checks) and run a short co-simulation.

```text
embeddyn mechatronics fmu-check FMU [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--stop-time` | `0.01` |  |

## bindings-from-pinplan

Generate Renode GPIO/PWM/ADC bindings from a CodeLayer Forge pin plan.

```text
embeddyn mechatronics bindings-from-pinplan PLAN_FILE [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--device` |  | Device with renode pin data. |
| `--output`, `-o` |  | Bindings YAML to write (default: stdout). |
| `--merge` |  | Existing bindings whose non-pin entries (sysbus...) are kept. |
| `--machine` |  |  |

## doctor

Report which external tools (Renode, ngspice, Webots, KiCad, LTspice) are found.

```text
embeddyn mechatronics doctor [OPTIONS]
```

## kicad-export

Export a KiCad schematic to a SPICE netlist with kicad-cli.

```text
embeddyn mechatronics kicad-export SCHEMATIC [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--output`, `-o` | `artifacts/kicad/circuit.cir` |  |
| `--executable` |  |  |

## kicad-plugin

Create a KiCad IPC plugin skeleton that talks to the local Studio API.

```text
embeddyn mechatronics kicad-plugin DESTINATION [OPTIONS]
```

## integration-validate

Check a project's integrations: model files, bindings, FMU variables, tool availability.

```text
embeddyn mechatronics integration-validate PROJECT_FILE [OPTIONS]
```

## catalog-list

List the component definitions found under a catalog root.

```text
embeddyn mechatronics catalog-list [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--root` | `components/mechatronics` |  |

## kicad-inspect

Summarise a SPICE netlist (sources, models, subcircuits) without KiCad.

```text
embeddyn mechatronics kicad-inspect NETLIST [OPTIONS]
```

## worker-register

Register a worker with the distributed coordinator.

```text
embeddyn mechatronics worker-register [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--name` |  |  |
| `--endpoint` | `local://worker` |  |
| `--worker-id` |  |  |
| `--simulators` | `` | Comma-separated simulator names. |
| `--hardware` | `` | Comma-separated hardware capabilities. |
| `--labels` | `` | Comma-separated scheduling labels. |
| `--max-parallel-jobs` | `1` |  |
| `--real-time` | `False` |  |
| `--database` | `.embeddyn/database/metadata.sqlite3` |  |

## workers-list

List registered workers.

```text
embeddyn mechatronics workers-list [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--database` | `.embeddyn/database/metadata.sqlite3` |  |
| `--stale-after` |  |  |

## job-submit

Submit a simulation job to the distributed queue.

```text
embeddyn mechatronics job-submit PROJECT_FILE [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--requirements` |  |  |
| `--priority` | `0` |  |
| `--database` | `.embeddyn/database/metadata.sqlite3` |  |

## jobs-list

List queued and finished jobs.

```text
embeddyn mechatronics jobs-list [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--status` |  |  |
| `--limit` | `100` |  |
| `--database` | `.embeddyn/database/metadata.sqlite3` |  |

## job-cancel

Cancel a queued job.

```text
embeddyn mechatronics job-cancel JOB_ID [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--database` | `.embeddyn/database/metadata.sqlite3` |  |

## experiment-expand

Expand an experiment file into its individual runs (sweeps, matrices).

```text
embeddyn mechatronics experiment-expand EXPERIMENT_FILE [OPTIONS]
```

## experiment-submit

Submit every run of an experiment to the queue.

```text
embeddyn mechatronics experiment-submit EXPERIMENT_FILE [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--database` | `.embeddyn/database/metadata.sqlite3` |  |

## experiment-status

Show the progress of a submitted experiment.

```text
embeddyn mechatronics experiment-status EXPERIMENT_ID [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--database` | `.embeddyn/database/metadata.sqlite3` |  |

## hil-validate

Validate a hardware-in-the-loop safety policy (watchdog, emergency stop, outputs, deadline trips).

```text
embeddyn mechatronics hil-validate POLICY_FILE [OPTIONS]
```

## worker-run

Run a worker agent that executes queued jobs.

```text
embeddyn mechatronics worker-run WORKER_ID [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--once` | `False` |  |
| `--poll-interval` | `1.0` |  |
| `--output-root` | `.embeddyn/worker-runs` |  |
| `--database` | `.embeddyn/database/metadata.sqlite3` |  |

## twin-state

Create a digital-twin state snapshot.

```text
embeddyn mechatronics twin-state MEASUREMENTS [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--output`, `-o` | `.embeddyn/digital-twin/state.json` |  |

## twin-compare

Compare simulated and measured signal files.

```text
embeddyn mechatronics twin-compare SIMULATION MEASUREMENTS [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--mapping` |  |  |
| `--output`, `-o` | `.embeddyn/digital-twin/comparison.json` |  |
| `--aligned-output` |  |  |

## twin-calibrate

Fit digital-twin parameters to measured data.

```text
embeddyn mechatronics twin-calibrate SIMULATION MEASUREMENTS [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--mapping` |  |  |
| `--output`, `-o` | `.embeddyn/digital-twin/calibration.yaml` |  |
| `--database` | `.embeddyn/database/metadata.sqlite3` |  |

## uncertainty-run

Run an uncertainty propagation from a specification file.

```text
embeddyn mechatronics uncertainty-run SPECIFICATION [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--samples` | `1000` |  |
| `--seed` | `1` |  |
| `--output`, `-o` | `.embeddyn/digital-twin/uncertainty.json` |  |

## requirements-verify

Check requirements against computed metrics.

```text
embeddyn mechatronics requirements-verify REQUIREMENTS METRICS [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--output`, `-o` | `.embeddyn/evidence/traceability.json` |  |

## evidence-report

Render a Markdown evidence report from verification results.

```text
embeddyn mechatronics evidence-report TRACEABILITY [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--comparison` |  |  |
| `--output`, `-o` | `.embeddyn/evidence/report.md` |  |
| `--run-id` |  |  |
| `--database` | `.embeddyn/database/metadata.sqlite3` |  |
| `--artifact-root` | `.embeddyn/artifacts` |  |

