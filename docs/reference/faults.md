# Fault injection

A fault changes what a signal's consumers see during a time window: a sensor
that sticks, drifts or gets noisy, a wire that drops samples, a supply that
sags. Faults sit between the simulators, so they work the same with every
backend: a fault on a signal bound to an ADC channel, a GPIO pin, a timer
counter or an SPI sensor register reaches the **real firmware** in Renode, and
you can check that it reacts the way it should.

```yaml
faults:
  - name: feedback-pot-stuck          # optional (default fault1, fault2, ...)
    signal: servo.feedback_voltage
    type: stuck
    start: 1.2 s                      # default 0
    end: 1.8 s                        # default: until the end of the run
```

## Types

| `type` | While active, consumers see | Fields |
|---|---|---|
| `stuck` | the value the signal had when the fault began, or `value` | `value` (optional: stuck at, e.g. `0` for a shorted pin) |
| `offset` | true value + `offset` | `offset` (required) |
| `gain` | true value × `gain` | `gain` (required) |
| `noise` | true value + Gaussian noise | `std` (required), `seed` |
| `drift` | true value + `rate` × (t − `start`) | `rate` (required, signal unit per second) |
| `dropout` | each update is lost with `probability`, so the previous value stays | `probability` (default 1), `seed` |

Every fault also takes:

* `start`, `end`: the window, `start ≤ t < end`.
* `period`, `duty`: make the fault **intermittent**. It is active for the first `duty × period` of every `period` after `start`, like a loose connector.
* `seed`: the random stream for `noise` and `dropout`. The default is the fault's position in the list, so runs are reproducible.

Values with units are converted to the signal's unit (`std: 20 mV` on a
signal in V, `offset: 60 rpm` on one in rad/s). A unit of the wrong kind is an
error, and so are an unknown signal or type, a missing required field, `end ≤ start`
or a probability outside 0…1. These types apply to continuous and discrete
signals ([bus frames](#bus-frames) below cover UART and CAN traffic);
several faults on one signal apply in the order they are listed. The result is
clamped to the signal's `min`/`max`.

## Bus frames

On the event signal of a Renode UART (`uarts: [{signal: mcu.console}]`) or
CAN bus (`can: {signal: mcu.can}`), a fault acts on the **traffic** instead of
a value. Every frame that crosses the boundary while the fault is active is
hit with `probability`. For a UART a frame is one byte; for CAN it is one
frame.

```yaml
faults:
  - {name: can-loss, signal: mcu.can, type: dropout, id: 0x101, direction: output, start: 1 s, end: 1.2 s}
  - {name: clear-lost, signal: mcu.can, type: dropout, id: 0x200, direction: input}
  - {name: line-noise, signal: mcu.console, type: corrupt, probability: 0.01, bits: 1, seed: 4}
```

| `type` | The frame | Fields |
|---|---|---|
| `dropout` | is lost: no event, not decoded into `fields`, not delivered to the firmware | `probability` (default 1), `seed` |
| `corrupt` | arrives with `bits` distinct random bits of its data flipped | `bits` (default 1), `probability` (default 1), `seed` |

* `direction`: `output` (frames the firmware sends), `input` (frames sent to
  it with `uart_write`, `can_write` or the Lab's serial monitor) or `both`
  (default).
* `id`: a CAN frame id or a list of them; without it every frame is hit.
* `start`, `end`, `period`, `duty` work as for signal faults, evaluated at
  each frame's own time: Renode's timestamp for a CAN frame the firmware
  sends, the end of the step for UART bytes, the current time for inputs.

`can_write` returns `False` when a fault lost the frame. Each frame fault
counts what it did (`dropped`, `corrupted`); checkpoints keep the counts and
the random streams.

Real CAN controllers check a CRC and drop a frame with bit errors (and the
sender retries it), so `corrupt` on CAN models a sender that puts wrong data
in a valid frame; use `dropout` for frames lost on the wire. Frame faults
need the Renode adapter in the orchestrator's process; with `--isolate` they
are not applied.

## Where to declare them

* **In the project:** a top-level `faults:` list. Every run uses it.
* **On the command line:** `run --fault '<mapping>'`, repeatable, adds faults
  without editing the project:

    ```bash
    embeddyn mechatronics run examples/mechatronics/rc-servo-l476.yaml -o stuck.csv \
      --fault '{signal: servo.feedback_voltage, type: stuck, start: 1.2 s}'
    ```

* **In a verification scenario:** `faults:` in a scenario adds faults for that
  scenario only, and its `expect:` entries say what the system must do about
  them ([verification](verification.md#scenario-matrix)).

## What is recorded

The faulted value is what every consumer sees **and** what is recorded: the
telemetry, the CSV export and the Lab's instruments all show the signal as
corrupted. To compare it with the truth, fault a sensor signal (the coupling
target the firmware reads) and keep the physical signal it comes from: in the
RC servo, `servo.feedback_voltage` is faulted and `servo.angle_deg` stays true.

Each fault publishes an event `fault.<name>` with `state: start` or `end` and
the fault's settings when it switches. The events land in the run's
`signals/events` telemetry. The Lab keeps a project's `faults:` when it saves
and applies them in its runs.

The compiled run records the faults in `project.normalized.json`, and
checkpoints hold their state (held values, random streams), so a resumed run
continues exactly like an uninterrupted one.

## Timing

Faults switch on and off at macro-step boundaries, and the faulted value is
re-applied at every boundary and every time the signal is written. A fault
therefore reaches signals that nothing writes as well, such as constants or a
supply voltage held at its initial value. A `period` much shorter than the
macro step is sampled, not resolved.

## Detecting the reaction: the `threshold` metric

```yaml
metrics:
  fault_trip: {type: threshold, signal: mcu.fault_led, threshold: 0.5}   # direction: rising | falling | either
```

`fault_trip.time` is when the signal first crosses the threshold
(interpolated), `fault_trip.delay` is that time minus the window `start`, and
`fault_trip.crossings` counts the crossings. A signal that never crosses
reports `time: null`, which fails a `min`/`max` check.

## Example: does the firmware catch a stuck pot?

`examples/mechatronics/rc-servo-l476.yaml` runs the STM32L476 servo firmware
in Renode three ways:

```yaml
verification:
  scenarios:
    - name: nominal
      expect: [{metric: fault_trip.crossings, max: 0}]
    - name: feedback-pot-stuck
      faults: [{name: feedback-pot-stuck, signal: servo.feedback_voltage, type: stuck, start: 1.2 s}]
      expect: [{metric: fault_trip.time, min: 1.2, max: 2.0}]
    - name: feedback-pot-noisy
      faults: [{name: wiper-noise, signal: servo.feedback_voltage, type: noise, std: 20 mV, seed: 1}]
      expect: [{metric: fault_trip.crossings, max: 0}, {metric: loaded_hold.mean, min: 25, max: 35}]
```

```bash
embeddyn mechatronics verify examples/mechatronics/rc-servo-l476.yaml --check scenarios
```

| Scenario | Result |
|---|---|
| nominal | no trip |
| feedback pot stuck at 1.2 s | stall monitor trips at 1.70 s: pulses stop, fault LED on |
| 20 mV wiper noise | no false trip; holds 28.6° under load |

All three run against the real firmware in about 25 s. The
[RC servo tutorial](../tutorials/rc-servo-firmware.md#step-7-inject-a-fault)
walks through the stuck-pot run.
