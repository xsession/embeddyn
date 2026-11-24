# Electronics in the loop: ngspice shared library

The `ngspice-shared` electrical backend runs the circuit in **libngspice**
inside the co-simulation process. One transient analysis covers the whole run,
so inductor currents, capacitor voltages and switch states carry from one
co-simulation step to the next. Switching circuits (PWM bridges, converters)
are simulated with their real ripple; the adapter averages it per step for
the slower domains.

The older `ngspice` backend (one `ngspice -b` process per step, no carried
state) stays for batch use; use `ngspice-shared` for co-simulation.

## How a step works

1. **Inputs** are held constant for the step (zero-order hold). They reach the
   circuit through `external` voltage or current sources (libngspice's
   `GetVSRCData`/`GetISRCData` callbacks), or through `alter` when a device
   parameter changes (sent only when the value changes).
2. The adapter sets `stop when time >= t` and runs (first step) or resumes the
   analysis. ngspice's `GetSyncData` callback shortens the last internal
   timestep, so the analysis lands **exactly** on the co-simulation time.
3. **Outputs** are read from the result vectors over the step window: the last
   value, or the time-weighted `mean`, `rms`, `min` or `max`.

An RC test (`tests/mechatronics/test_ngspice_shared.py`) checks the stepped
response against the analytic curve within 2·10⁻⁴ V at every step.

## Configuration

```yaml
simulators:
  electrical:
    model: circuits/motor-hbridge.cir
    options:
      backend: ngspice-shared
      bindings: bindings/ngspice-hbridge.yaml
      max_step: 1.0e-6        # ngspice max timestep (default: step_seconds / 50)
      # library: /path/to/libngspice.so.0   (or NGSPICE_LIBRARY, or the system library)
      # spice_options: {reltol: 0.001}       (written as one .options card)
      # inline_bindings: [...]               (same format as the bindings file)
```

The netlist is a plain SPICE deck:

* `{{signal.name}}` placeholders are replaced by the signal's **initial**
  value at start (for fixed parameters and initial conditions). Placeholders
  in comment lines are left alone.
* Analysis cards (`.tran`, `.op`, `.ac`, `.dc`, …), `.control` blocks and
  `.end` are removed; the adapter appends its own `.tran … uic`.
* Sources to be driven from the co-simulation are written as
  `Vname n+ n- dc 0 external`.

### Bindings

| Direction | Keys | Meaning |
|-----------|------|---------|
| input | `source: Vname` | value of an `external` voltage source |
| input | `current_source: Iname` | value of an `external` current source |
| input | `alter: Rname` or `alter: "@Lname[inductance]"` | `alter` command when the value changes |
| output | `vector: "V(node)"`, `"V(n1,n2)"`, `"I(Vname)"` or a raw vector name | `mode`: `mean` (default), `last`, `rms`, `min`, `max` |

All bindings take `scale` and `offset`; inputs also take `min`/`max`
(applied after scaling). One signal can drive several sources, e.g. a signed
duty split into two legs with `scale: -1` and `min: 0`. Quote vector
expressions in YAML flow style (`vector: "V(a,b)"`), otherwise the comma
splits the mapping.

## Example: switching H-bridge

`examples/mechatronics/circuits/motor-hbridge.cir`
is a full H-bridge with 20 kHz sign-magnitude PWM (triangle carrier and
voltage-controlled switches), a supply with source resistance and DC-link
capacitor, and the motor's R, L and back-EMF. Bindings are in
`bindings/ngspice-hbridge.yaml` (`examples/mechatronics/bindings/ngspice-hbridge.yaml`).

* `examples/mechatronics/dc-motor-ngspice.yaml`: mock firmware + H-bridge +
  mock mechanics. 0.25 s (about 250 000 SPICE points) in about 5 s.
* `examples/mechatronics/dc-motor-full.yaml`: real STM32 firmware in Renode
  + H-bridge in ngspice + mock mechanics. 0.25 s in about 11 s.

Compared with the ideal RL mock, the bridge shows real losses: at full duty
and 11.4 A it delivers 22.97 V instead of 24 V (0.05 Ω source resistance plus
two 0.02 Ω switches).

Two netlist pitfalls found while building it:

* A triangle `PULSE` with `TR + TF = PER` (no flat top) makes the transient
  fail with "timestep too small" at the first period boundary; give it a
  short `PW` (10 ns here).
* Keep the carrier inside 0..1 by more than the switch hysteresis, so duty 0
  and 1 hold a switch firmly on or off.

## Getting libngspice

* Debian/Ubuntu: `sudo apt install libngspice0`.
* Without root: `apt-get download libngspice0`, then
  `dpkg-deb -x libngspice0_*.deb <dir>` and set
  `NGSPICE_LIBRARY=<dir>/usr/lib/x86_64-linux-gnu/libngspice.so.0`
  (its only dependencies are libc, libstdc++ and libgomp).

The simulation tests in `tests/mechatronics/test_ngspice_shared.py` run when
the library is found and are skipped otherwise.

## Limits

* libngspice holds global state: **one instance per process**. A second
  adapter in the same process fails with a clear error; run with process
  isolation (`--isolate`) to use several circuits.
* All result points stay in memory for the whole run (about 250 000 points
  per 0.25 s at a 1 µs max step). Very long runs need a larger max step or a
  future `.save`-based vector filter.
* No checkpoint/restore.
* `alter` changes during a transient work for most passive and source
  parameters; check ngspice's documentation for the device you alter.
