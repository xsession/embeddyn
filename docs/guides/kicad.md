# Export a KiCad schematic to SPICE

Simulate the analog part of your board (a current-sense amplifier, a gate driver, an input filter) in the loop, straight from the schematic.

## 1. Prepare the schematic

* Give every component a SPICE model (KiCad's *Simulation Model* field), or exclude it from simulation.
* Put a voltage source where the co-simulation drives the circuit (a PWM node, a sensor voltage) and name it, e.g. `Vduty`.
* Name the nets you want to read (`I_SENSE`, `VBUS`).

## 2. Export

```bash
embeddyn mechatronics kicad-export hardware/driver.kicad_sch -o circuits/driver.cir
```

This runs `kicad-cli sch export netlist --format spice`, so the result is exactly what KiCad's simulator would use, reproducibly in CI. `kicad-inspect` lists what the netlist contains (sources, models, subcircuits) without KiCad installed.

## 3. Make it co-simulatable

Edit the exported netlist (or post-process it in your build) so driven sources are external:

```spice
Vduty duty 0 dc 0 external
```

and write a bindings file:

```yaml title="bindings/driver.yaml"
bindings:
  - {signal: mcu.pwm_duty, direction: input, source: Vduty}
  - {signal: driver.current_sense, direction: output, vector: "V(i_sense)", mode: mean}
```

## 4. Use it

```yaml
instances:
  driver:
    domain: electrical
    model: circuits/driver.cir
    options: {backend: ngspice-shared, bindings: bindings/driver.yaml, max_step: 1.0e-7}
```

See [Electronics](../theory/electronics.md) for how the circuit is stepped and [Tutorial 2](../tutorials/ngspice.md) for a complete example.

## KiCad plugin

`embeddyn mechatronics kicad-plugin plugins/kicad-embeddyn-bridge` creates a KiCad IPC plugin skeleton that can send the selected schematic context to the local Studio API.
