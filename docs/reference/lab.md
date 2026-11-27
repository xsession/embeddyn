# embeddyn Lab: the interactive GUI

The Lab is a browser GUI for building and running mechatronic co-simulations,
using the same project files as the CLI. Its parts come from four places:

| Inspiration | What the Lab takes from it |
|---|---|
| Proteus (schematic capture + virtual instruments) | parts with pins, wires, scopes, meters and logic analysers wired onto the schematic |
| Fritzing (breadboard view) | ngspice circuits drawn as prototypes on a solderless breadboard, with parts, jumpers and supply modules |
| Webots (robot simulator) | a 3-D view of the mechanism that moves with the simulation |
| velxio-style emulators | a parts library, run/stop toolbar and live controls next to real firmware emulation |
| embeddyn | the co-simulation engine underneath (Renode, ngspice, Webots, FMU, python plants, replay) |

No code is taken from these tools; velxio is AGPL. The Lab is plain HTML and
JavaScript with no build step and no CDN, served by Python's standard library.
It needs nothing beyond embeddyn's own dependencies.

```bash
embeddyn mechatronics gui examples/mechatronics/rc-servo-lab.yaml --open
# or: python -m embeddyn mechatronics gui [project.yaml] [--port 8710] [--host 127.0.0.1]
```

To use Renode parts, start the Lab with `RENODE_BIN` set (see [RENODE.md](renode.md)),
or tick **mock hardware** to replace firmware, electrical and mechanical backends with
the built-in mocks.

## Screen layout

```text
┌ toolbar: New · Open ▾ · Save · Save as · YAML · Check │ ▶ Run ❚❚ ■ · mock hardware · real-time × │ t · status ┐
├ parts library ┬─ Schematic │ Breadboard │ 3-D view │ Split ─────────┬ inspector ─────────────┤
│ Controllers   │                                                       │ project / part / wire  │
│ Electrical    │   parts with pins, wires, live values on the pins     │ parameters, options,   │
│ Mechanical    │                                                       │ pins, transforms       │
│ Sources …     │                                                       │                        │
├───────────────┴───────────────────────────────────────────────────────┴────────────────────────┤
│ Instruments │ Console │ Metrics          ⟲ replay ─────── time cursor ─────── follow · CSV       │
│ [scope] [multimeter] [logic analyser] [gauge] [LED] [slider] [switch] …                          │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Building a system

* **Add a part:** drag it from the library onto the schematic, or double-click it.
* **Wire:** drag from an output pin (right side) to an input pin (left side). One
  driver per input; a new wire replaces the old one. Depending on the input, a wire is
  one of three kinds:
  * **net** (green): a python or FMU input reads the signal directly (`options.inputs`).
  * **coupling** (blue): the input has its own signal, such as a Renode ADC binding. The
    wire becomes a `couplings:` entry. Select it to set gain, offset, clamp, dead band,
    quantisation or a delay in steps.
  * **probe** (orange, dashed): an instrument or 3-D body observes a signal. Probes never
    change the simulation. An instrument can also pick any signal from the inspector,
    even one no part exposes as a pin.
* **Edit a part:** the inspector shows python plant parameters, with defaults as
  placeholders, and every backend option as YAML. **Apply options** re-derives the pins;
  for example, set `bindings:` on a Renode MCU and its ADC, GPIO, PWM and memory
  bindings appear as pins.
* **Delete:** select a part or wire, then press Delete, or use the button in the inspector.
* **Pan / zoom / fit:** drag the background, use the mouse wheel, press **F** or click **Fit**.

| Library category | Parts |
|---|---|
| Controllers | STM32L476 servo firmware (Renode), Renode MCU (generic), PI controller, servo pulse generator |
| Electrical | DC motor armature, SPICE circuit (ngspice-shared) |
| Mechanical | RC servo, geared drivetrain, MuJoCo model (pins from the MJCF), Webots world, FMU |
| Thermal | winding thermal RC |
| Sources | function generator (sine, square, triangle, sawtooth, step, ramp, pulse, DC), CSV replay, slider, switch |
| Instruments | oscilloscope, multimeter, logic analyser, X-Y plotter, gauge, LED, watch table, serial monitor (Renode UART console with a send box) |
| 3-D bodies | servo + arm, motor shaft, pendulum, wheeled cart, linear slide, lamp, bar |

The function generator runs inside the simulation as a python plant
(`embeddyn.mechatronics.gui.sources:FunctionGenerator`), so it is exact at every step
and a saved project behaves the same from the CLI.

## Running and measuring

* **▶ Run** (F5) checks the project and starts it in a background thread on the server.
  Rows stream to the browser while it runs. Instruments, pin values and the 3-D view update
  live. **❚❚** pauses, **■** stops. Simulators are shut down cleanly, Renode included.
* **real-time ×** paces the run to the wall clock times the factor; untick it to run as fast
  as possible. Renode-in-the-loop usually runs slower than real time.
* **Live controls:** a slider or switch from *Sources* drives its signal. Moving it during a
  run applies at the next macro step (`CoSimulationOrchestrator.forced`). Its value is saved
  as the signal's initial value.
* **Instruments:**
  * oscilloscope: four channels, roll or sweep, 1 ms…60 s or the whole run, stacked or
    overlaid, auto-scaled with min/max decimation;
  * multimeter: value, mean, RMS, min, max or peak-to-peak over a window;
  * logic analyser: eight channels with a threshold, plus frequency and duty of each channel;
  * also an X-Y plotter, gauge, LED and watch table.
* **After a run:** drag the time cursor, or press **⟲ replay**, to scrub or replay the
  recording. Every instrument and the 3-D view follow the cursor. **CSV** downloads the run.
  **Metrics** shows the project's `metrics:` results.
* **Analyzer** (dock tab): decoded CAN/CANopen, BLE and 802.11 traffic of the run, with
  protocol toggles, a filter, one lane per sender, a field tree with a linked hex dump, and
  pcap export. Clicking a packet moves the time cursor there. Its **Changes** view shows
  one row per message with the changed bytes highlighted, bit flip counts, and any byte
  range read as an integer and plotted, for reverse engineering. See
  [Multi-board buses, radios and the analyzer](multiboard-comms.md#in-the-lab).
* **3-D view:** drag to orbit, shift- or right-drag to pan, wheel to zoom, double-click to
  re-frame. Angles follow the signal unit (deg or rad) unless the body's `unit` is set.

## Breadboard view

The **Breadboard** tab draws every ngspice circuit of the project the way it would sit on a
solderless breadboard, in the manner of Fritzing:

* resistors carry colour bands, from their value or from the `.param` / `{{signal}}` they
  refer to;
* ceramic and electrolytic capacitors, inductors, diodes and LEDs (IR LEDs glow violet) are
  drawn as such;
* transistors are TO-92 packages, power MOSFETs TO-220;
* switches, dependent sources and subcircuits are DIP chips across the trench;
* behavioural `B` sources are small `ƒ` chips;
* zero-volt sense sources are yellow ammeters.

Sources sit beside the board as modules: a bench supply, a function generator (`PULSE`,
`SIN` and the like), an **MCU pin** for an `external` source fed by a firmware-domain
instance, or a signal input for any other instance. Jumper wires run from the modules to
the board: red for the supply rail, black for ground, other colours by net.

```text
┌ toolbar: circuit ▾ · ＋ Part ▾ · ↶ · Auto-layout · Netlist… · Save layout · Export .cir · ✓ matches netlist ┐
│ [Bench supply 5 V]──red──┐   ┌──────────── breadboard (rails, a–e │ trench │ f–j) ─────────────────┐  │
│ [MCU pin · gpio ]──────┐ └──▶│  Rled ▭▭▭  D1 ◉ IR   Q2 ◗ PT   Q1 ◗ NPN   Bcmp ƒ   Rout ▭▭▭  Cout ◍  │  │
│ [Current input  ]────┐ └────▶│  Vsled [A 23.7 mA]        ⚑ V(rx) = 126 mV    ⚑ V(rx_out) = 3.3 V │  │
└──────────────────────┴───────┴────────────────────────────────────────────────────────────────────┴──┘
```

**Connectivity follows the real board.** The five holes of a column half (a–e or f–j) form
one strip, each power rail is one conductor, and a jumper joins its two ends. Hover a hole,
leg or wire to see its net, with every hole on that net lit up. The status line and the
inspector compare the board with the netlist:

* **open**: a netlist net is split into unconnected pieces;
* **short**: one piece joins different nets;
* **unplaced**: a leg is in no hole.

**Editing:**

* drag a part to move all its legs, or drag a leg end to bend it to another hole;
* drag from a free hole or a module terminal to draw a jumper;
* select a part to edit its value or model and see its pins, nets and SPICE card;
* **Del** removes a part or wire, **R** swaps a part's legs, **Ctrl+Z** undoes, **F** fits.
* **＋ Part** adds resistors, capacitors, LEDs (red, 940 nm IR), photodiodes, 1N4148,
  2N3904/2N3906, a phototransistor, MOSFETs, a bench supply, a function generator and an
  ammeter. Each added part brings its `.model` card.

**Live values:** after or during a run, the bound vectors show on the board:

* `V(node)` as a flag on the net;
* `i(Vsense)` on its ammeter;
* input sources on their module's display;
* an LED lights with the current of the ammeter in series with it.

Flags name the instances that read the signal (`→ beam`). They follow the time cursor and
replay like the instruments.

**Files:**

* **Save layout** (and the project's Save) writes `<netlist>.breadboard.yaml` next to the
  netlist. Without one, the board is laid out automatically in signal-flow order, starting
  from the nets the modules drive.
* **Netlist…** shows the netlist the board makes.
* **Export .cir** writes it out. Element cards get the board's nets and values. Removed
  parts stay as comments, and added parts and their model cards go before `.end`. Every
  other line (`.param`, `.model`, comments, `{{placeholders}}`) is kept.

**Overview:** the modules show where each circuit input comes from (for example
`strobe firmware → electrical.gpio` on an MCU pin). The probe flags show which instances
read each output, so one board shows the whole electrical side of the co-simulation.

## Files

Saving writes an ordinary project file. Simulator instances, signals and couplings
go where the CLI expects them. Sections the Lab does not edit (`metrics`,
`verification`, `scheduler`, `parameters`, `components`) are kept verbatim. The
layout, sources, instruments and 3-D bodies are stored under `metadata.gui`, which
the CLI ignores. **Save as** into another folder rewrites relative model, script,
firmware, bindings and log paths so they still resolve.

Projects without `metadata.gui` open with an automatic layout in columns:
sources | firmware | electrical | mechanical | observers. A coupling whose signal no
part exposes gets a net-label terminal.

## Architecture

| Piece | Role |
|---|---|
| `gui/document.py` | project file ↔ schematic graph; pins from plants, replay columns or binding files (Renode, Webots, ngspice) |
| `gui/parts.py` | parts library and `instantiate_part` |
| `gui/sources.py` | `FunctionGenerator`, `ServoPulseGenerator` plants |
| `gui/session.py` | `RunSession`: thread around `CoSimulationOrchestrator.iterate()`; pause, stop, real-time pacing, forced signals, metrics, CSV |
| `gui/server.py` | `ThreadingHTTPServer`; JSON API under `/api`, Server-Sent Events at `/api/stream` |
| `gui/breadboard.py` | SPICE netlist parser, part types and palette, automatic breadboard layout, connectivity check, netlist export, `.breadboard.yaml` sidecars |
| `gui/static/` | `app.js` (shell), `schematic.js` (SVG editor), `breadboard.js` (breadboard view), `ops.js` (edit operations), `inspector.js`, `instruments.js`, `analyzer.js` (protocol analyzer) with `bytediff.js` (changes view), `scene3d.js` (flat-shaded canvas renderer) |

Two core changes support the Lab:

* `CoSimulationOrchestrator.iterate()` yields one row per macro step, and `run()` now uses it.
* `MechatronicProject.from_mapping()` builds a project from a parsed document.

**Security:** the API opens and writes project files and launches simulators, so the
server binds to 127.0.0.1 by default. It also refuses cross-origin POSTs and only writes
`.yaml` files. Do not expose it on a network.

## Example: `rc-servo-lab.yaml`

This example runs without firmware. A square-wave angle command (±45°, 0.5 Hz) feeds
a software pulse generator, which drives the RC servo plant. A slider sets the arm load.
The scope shows command, angle and current, with a current meter, an angle gauge and a 3-D
servo alongside. Moving the load slider during a run makes the proportional servo droop.
A load above the 0.48 N·m stall torque back-drives it.

The firmware-in-the-loop version is `rc-servo-l476.yaml` ([RC_SERVO_L476.md](rc-servo-l476.md)).
It opens in the Lab the same way, and running it there gives the same metrics as the CLI.

## Tests

`tests/mechatronics/test_gui.py` covers:

* every example round-trips through the graph;
* pins derived from binding files;
* every library part instantiates;
* a graph built the way the GUI builds one runs and saves;
* forced signals and `iterate()`;
* run-session pause, stop and real-time pacing;
* function-generator waveforms;
* the HTTP API: open, run, stream, CSV, save-as with path rebasing, cross-origin and
  path-traversal refusal, and pin re-derivation.

`tests/mechatronics/test_breadboard.py` covers the breadboard view:

* netlist parsing: models, continuations, subcircuits, `.control`;
* an automatic layout of every example netlist that matches it, with one lead per hole;
* opens, shorts and unplaced legs;
* export with changed, removed and added parts;
* sidecar files and the breadboard API;
* an ngspice run of `ir-curtain-channel.yaml`.
