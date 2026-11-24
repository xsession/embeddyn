# Project file

A project is one YAML file. Paths inside it are relative to the file. Values may carry a unit string (`"0.5 ms"`, `"60 rpm"`, `"25 degC"`); it is converted to the unit expected where the value is used, and incompatible units are an error ([units](../theory/system-model.md#units)).

```yaml
schema_version: 1                     # 1 or 2
name: my-system
step_seconds: 0.001                   # macro step h
duration_seconds: 4.0
metadata: {description: "...", gui: {...}}     # gui: Lab layout, ignored by the CLI

parameters: {...}
instances: {...}                      # and/or simulators: {...}
signals: {...}
couplings: [...]
scheduler: {...}
metrics: {...}
verification: {...}
components: {definitions: {...}, instances: {...}}
```

## `parameters`

```yaml
parameters:
  firmware.kp: 26.7                                   # plain number
  motor.resistance_20c: {value: 2.0, unit: ohm, provenance: [{source: datasheet}]}
```

Referenced as `"@name"` from Python plant/FMU parameters and coupling transforms, by Renode input bindings, and by tuning/tolerance/estimation specs.

## `instances`

```yaml
instances:
  <name>:                             # ^[A-Za-z][A-Za-z0-9_-]{0,63}$, unique
    domain: firmware | electrical | mechanical | thermal | environment
    enabled: true
    model: <file or module:Class>
    executable: <path>                # external tool override
    step: 0.00025                     # optional; must divide step_seconds (switches to multi-rate)
    options:
      backend: renode | ngspice-shared | ngspice | ltspice | python | mujoco | fmu | webots | replay | mock
      ...                             # backend options, see below
```

`simulators: {<domain>: {...}}` is the legacy form: one instance per domain, named after the domain.

### Backends by domain

| Domain | Backends |
|---|---|
| firmware | `renode`, `mock`, and the generic ones |
| electrical | `ngspice-shared`, `ngspice`, `ltspice`, `mock`, and the generic ones |
| mechanical | `mujoco`, `webots`, `mock`, and the generic ones |
| any | generic: `python`, `fmu`, `replay`, `mujoco` |

### Backend options

| Backend | Key options | Reference |
|---|---|---|
| `renode` | `script`, `firmware`, `machine`, `launch`, `port`, `api`, `bindings`/`inline_bindings`, `variables`, `log`, `uarts`, `can`, `spi` | [Renode adapter](renode.md) |
| `ngspice-shared` | `bindings`/`inline_bindings`, `max_step`, `library`, `spice_options` | [ngspice adapter](ngspice.md) |
| `python` | `parameters`, `inputs`, `outputs`, `max_step` | [Python plants](../theory/mechanics.md#python-plants) |
| `mujoco` | `inputs`, `outputs` (model addresses), `timestep`, `initial` | [MuJoCo adapter](mujoco.md) |
| `fmu` | `parameters`, `inputs`, `outputs`, `max_step` | [Instances and FMI](instances-and-fmi.md) |
| `replay` | `columns`, `interpolation` (`previous`, `linear`) | model is a CSV file |
| `webots` | `headless`, `port`, `bindings` | [Mechanics](../theory/mechanics.md#webots) |

## `signals`

```yaml
signals:
  <name>:
    domain: firmware | electrical | mechanical | thermal | environment   # default environment
    unit: A
    initial: 0.0
    min: -15.0                        # optional clamp
    max: 15.0
    kind: continuous | discrete | event | bus                            # default continuous
    data_type: float64
    quantity: current                 # optional semantic tag
    retention: full
```

## `couplings`

```yaml
couplings:
  - source: arm.angle
    target: mcu.encoder_counts
    gain: 13037.97                    # shorthand for transform: {type: affine, gain, offset}
    offset: 0.0
    delay_steps: 1                    # or timing: {delay_steps: 1}
  - source: sensor.raw
    target: mcu.adc
    transform: {type: quantize, gain: 0.0008, min: 0, max: 3.3}   # identity | affine | clamp | deadband | quantize
  - source: arm.speed
    target: motor.shaft_speed
    gain: "@gear.ratio"               # a parameter: "@name", "-@name" or "1/@name" (gain, offset, min, max, deadband)
```

`expression:` is rejected (no code in project files).

## `scheduler`

```yaml
scheduler:
  algorithm: deterministic-fixed-step     # multirate-fixed-step | real-time-fixed-step | real-time-multirate
  macro_step: 0.001                       # must equal step_seconds if given
  domains: {electrical: 0.00025}          # per-domain steps (imply multirate)
  convergence: {enabled: false, tolerance: 1.0e-9, max_iterations: 8, failure_policy: error}   # error | warn | accept
  real_time: {enabled: false, speed_factor: 1.0, deadline_tolerance: 0.01, missed_deadline_policy: continue}  # continue | warn | abort
```

## `metrics`

```yaml
metrics:
  <name>: {type: step_response, signal: <signal>, target: <signal or number>, settling_band: 0.02, start: 0.0, end: 1.0}
  <name>: {type: stats, signal: <signal>, start: ..., end: ...}
  <name>: {type: integral, signal: <signal>}
  <name>: {type: threshold, signal: <signal>, threshold: 0.5, direction: rising}   # rising | falling | either
```

Figures: see [Metrics](../theory/metrics.md).

## `verification`

`energy`, `convergence`, `golden`, `scenarios`; see the [verification reference](verification.md).

## `faults`

```yaml
faults:
  - {name: pot-stuck, signal: servo.feedback_voltage, type: stuck, start: 1.2 s, end: 1.8 s}
  - {signal: sensor.temp, type: noise, std: 0.5 degC, seed: 1}
  - {signal: encoder.count, type: offset, offset: 12, start: 1 s, period: 0.1 s, duty: 0.3}   # intermittent
  - {signal: mcu.can, type: dropout, id: 0x101, direction: output, start: 1 s}               # bus frames
```

Types `stuck` (`value`), `offset`, `gain`, `noise` (`std`, `seed`), `drift`
(`rate`), `dropout` (`probability`, `seed`) on continuous and discrete
signals; `dropout` and `corrupt` (`bits`) on the event signal of a Renode UART
or CAN bus, with `direction` and `id`; see [Fault injection](faults.md).

## `components`

Reusable component definitions and their instances. Instance parameters become project parameters named `<instance>.<param>` with provenance `component-instance`. See [Architecture and parameter storage](../project/architecture-and-parameter-storage.md) for the design.

## Companion spec files

| File | Command | Reference |
|---|---|---|
| tuning spec | `tune` | [Metrics and tuning](tuning.md) |
| tolerance spec | `tolerance` | [Tolerance analysis](tolerance.md) |
| estimation spec | `estimate` | [Parameter estimation](estimation.md) |
| Renode bindings | (`options.bindings`) | [Renode adapter](renode.md#binding-kinds) |
| ngspice bindings | (`options.bindings`) | [ngspice adapter](ngspice.md#bindings) |
