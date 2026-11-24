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
   analysis. ngspice turns the condition into a breakpoint at `t`, and its
   `GetSyncData` callback shortens the last internal timestep, so the
   analysis lands **exactly** on the co-simulation time. (The stop used to be
   set a hair before `t`; when a source breakpoint, such as a PWM carrier
   corner, fell on the grid, the next step then had to be 10⁻¹⁶ s long and
   failed with "timestep too small".)
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
      # save: bound                          (default) or all, see below
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

### What ngspice keeps: `save`

ngspice keeps every saved vector for the whole run. With `save: bound` (the
default) only `time` and the vectors the output bindings read are saved:
`V(a,b)` saves nodes `a` and `b`, `I(Vx)` saves `vx#branch`, a raw vector
name is saved as written. `save: all` keeps ngspice's default, every node and
branch current, which you need only to inspect other vectors interactively.
On the MOSFET bridge below this keeps 6 vectors instead of 50 (about 5 MB
instead of 44 MB per 20 ms simulated) with identical outputs.

libngspice ignores `.save` cards outside batch mode, and `save` entries share
ngspice's debug list with `stop` breakpoints, which the adapter clears every
step; so the adapter issues the `save` command itself before each run/resume.
A `.save` card in your netlist has no effect.

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

## Example: MOSFET H-bridge with dead time

`examples/mechatronics/circuits/motor-hbridge-mosfet.cir` replaces the ideal
switches with four N-channel power MOSFETs (a generic 60 V, ~10 mΩ `VDMOS`
model with its body diode, gate charge and junction capacitances) and adds
the gate drive:

* **Dead time.** Each leg's high side is on while duty > carrier + δ and its
  low side while duty < carrier − δ, with δ = `tdead` × carrier slope / 2, so
  both are off for `tdead` around every edge. Duty 0 (below 0.5 %) and 1
  (above 99.5 %) hold a switch on, so an idle leg is never blanked.
* **Gate drivers**: a 12 V output with about 10 ns rise and fall (a `tanh`
  edge, which ngspice integrates more easily than a step) through the gate
  resistor; the high-side driver floats on its switch node (an ideal
  bootstrap supply).
* **Losses**: `V(ploss)` is the dissipation of the four devices,
  V<sub>DS</sub>·I<sub>D</sub> summed over channel and body diode, measured
  through 0 V sources in the drains.

`{{bridge.dead_time}}` and `{{bridge.gate_resistance}}` are **project
parameters** in `dc-motor-mosfet.yaml` (200 ns and 10 Ω), so `-p
bridge.dead_time=100ns`, `tune` and `tolerance` reach the netlist like any
parameter. The project uses a ten times lighter rotor than
`dc-motor-ngspice.yaml`, so the speed loop leaves saturation after about
15 ms and the bridge switches at about 55 % duty.

Bridge loss at 0.1 s (24 V, about 6 A), from real runs:

| Dead time | 0 | 100 ns | 200 ns | 500 ns | 1 µs |
|---|---|---|---|---|---|
| MOSFET losses | 11.2 W | 0.80 W | 0.82 W | 0.85 W | 0.93 W |

Without dead time both switches of a leg conduct at every edge
(shoot-through, current spikes of about 90 A). Above about 100 ns the
losses rise slowly, because the body diodes (about 0.8 V) carry the
current for longer than the channels (a few mΩ), and the mean bridge voltage
sags by about t<sub>d</sub>·f<sub>PWM</sub>·V<sub>bus</sub> (0.98 V at 2 µs).

`tuning/dead-time.yaml` lets `tune` choose the dead time against the loss
energy over 30 ms:

```bash
embeddyn mechatronics tune examples/mechatronics/tuning/dead-time.yaml --workers 4
```

12 evaluations take about 70 s on 4 workers (each worker runs its own
libngspice). From 500 ns (35.5 mJ) Nelder–Mead settles at 89 ns (32.0 mJ);
at 45 ns shoot-through starts (45.0 mJ). The optimum sits just above
that cliff, and the model has no driver propagation-delay spread, so a real
design adds margin: put the spread into `tolerance` or constrain the dead
time's lower bound.
The peak drain currents are dominated by body-diode reverse recovery and
ringing of this generic model, so they aren't bound as outputs.

Solver settings: `spice_options: {method: gear}`. The default trapezoidal
rule rings on the stiff switching edges and stopped one closed-loop run with
"timestep too small" at a carrier corner; `max_step: 2.0e-7` resolves the
dead time. 0.1 s takes about 40 s (vs about 2 s for the ideal-switch bridge),
so use this circuit where the switching itself matters, and the ideal-switch
bridge or the averaged mock elsewhere.

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
* All result points of the saved vectors stay in memory for the whole run
  (about 250 000 points per 0.25 s at a 1 µs max step); `save: bound` keeps
  that to the bound vectors. Very long runs also need a larger max step.
* No checkpoint/restore.
* `alter` changes during a transient work for most passive and source
  parameters; check ngspice's documentation for the device you alter.
