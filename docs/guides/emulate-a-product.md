# Emulate a real product, end to end

This guide describes the workflow for turning an existing embedded product into
a whole-system emulation: its physics, its electronics, its real firmware on
an emulated MCU, and the buses between boards. It is written from one worked
example, the **IR body-contour system of a dual-head SPECT camera**, and keeps
the decisions, the dead ends and the checks that caught mistakes, because those
are what transfer to the next product.

The worked example, in one paragraph: each detector head carries a light
curtain a few millimetres below its face. A **master** board (dsPIC30F5011)
fires a chain of IR LEDs through iC-Haus **iC-NT** pulse drivers, modulating
each LED with a 300 kHz burst. A **slave** board (dsPIC30F5011) reads a chain of
photodiodes through iC-Haus **iC-ME** dual receivers, whose 300 kHz bandpass
rejects background IR; a comparator charges a measurement capacitor per pixel,
the ADC reads it and a transistor resets it. The boards talk CANopen; the master
sends the slave the start sign of each scan. The heads move in until a beam
breaks, so they follow the patient's contour.

## The phases at a glance

| # | Phase | Output | Example status |
|---|---|---|---|
| 1 | Capture the system | a design page with boards, ICs, buses, roles, open questions | [design page](../project/body-contour-dspic.md) |
| 2 | Collect primary sources | datasheets, device files, opcode tables, each with its revision | iC-NE Rev E1, iC-NT Rev A0, iC-ME Rev A1, XC16 v2.10 device files |
| 3 | Split by time scale | which part runs where, and at what step | geometry in MuJoCo, chains inside Renode |
| 4 | Model the physics | a tested model with numbers you can check by hand | `optics.py`, MuJoCo `light_curtain` |
| 5 | Close the loop on a supported MCU first | a running prototype and a profile | STM32L476 prototype, heads hold 11–12 mm |
| 6 | Get the target toolchain | a compiler that builds for the real MCU | XC16 v2.10 on Linux |
| 7 | Audit the emulator for the target | a list of what really works | the dsPIC core was a skeleton |
| 8 | Complete the emulator | a CPU core and peripherals that pass differential tests | in progress |
| 9 | Model the board electronics in the emulator | chain ICs, front end, light link | planned |
| 10 | Port or write the firmware | the product's firmware running unmodified | planned (CANopenNode) |
| 11 | Verify with scenarios and faults | requirements as executable checks | prototype: gap and touch metrics |
| 12 | Keep the record | design page, roadmap, commits per topic | ongoing |

The order matters: each phase removes a risk before the expensive phases
depend on it.

## 1. Capture the system

Start with a page, not code. Write down every board, every MCU and every IC on
the signal path, every bus, and who is master of what. Then write the questions
you cannot answer yet. For the body contour the first description was "an
optical IR gate like a SPECT body contour"; three rounds of questions turned it
into the real architecture:

* **How many MCUs and what does each do?** First answer: one dsPIC30F5011.
  Refined: per head a master (LED chain) and a slave (receiver chain).
* **What does the bus carry?** CANopen, used to synchronise the scan start.
* **Which ICs, and how are they used?** iC-NT drivers, iC-ME receivers (two
  beams per IC), a 300 kHz carrier, a comparator into a measurement capacitor
  read by the ADC and reset by a transistor.

Ask about behaviour that a datasheet cannot tell you: carrier frequency, burst
length, who sends the start sign, how a pixel is evaluated. Record every answer
in the design page the moment you get it, and keep a list of what is still
open, with the default you use until it is answered.

## 2. Collect primary sources

Model from documents, not from memory, and note the revision of each.

* **Chip datasheets.** iC-NE was publicly available (Rev E1). iC-NT and iC-ME
  sit behind a vendor login; the product owner supplied them. Do not model a
  part from a marketing summary: the iC-NE chain mechanism (a 3-flip-flop shift
  register clocked on both edges, two clocks per IC, ≥ 10 µs activation, ≥ 5 µs
  output settling, a 100–530 kHz bandpass that answers the *rising edge* of a
  light pulse) only appears in the full datasheet, and it decides how the
  firmware has to clock the chain.
* **MCU device files.** The compiler's linker script is the authoritative SFR
  map (`p30F5011.gld`: `SR = 0x42`, `SPLIM = 0x20`, interrupt flags at
  `0x84…`, CAN1 at `0x300`, CAN2 at `0x3C0`, data RAM from `0x800`).
* **Instruction encodings.** The assembler's own opcode table
  (`binutils` `opcodes/pic30-opc.h` and `pic30-opc.c` in the XC16 sources) gives
  every instruction's encoding, mask, operand fields and the device families it
  belongs to. Extract it to a table filtered to your core; it becomes the
  checklist for the decoder.
* **A reference implementation.** Microchip's `sim30` (shipped with XC16)
  executes the full dsPIC30 instruction set. It is too slow and too coarse to
  co-simulate with, but it is a perfect oracle for testing an emulator.

Write the facts you use into the design page, with figures and limits, so the
model can be reviewed against the source.

## 3. Split by time scale

Every loop in the system has a time scale, and the co-simulation step decides
where each part may run:

| Part | Time scale | Runs in |
|---|---|---|
| gantry orbit, head motion, patient geometry | ms | MuJoCo, co-simulation step 100 µs |
| beam geometry (which beam is blocked, how much) | changes over ms | MuJoCo ray casts, written each step |
| LED pulse, carrier, receiver bandpass, comparator, capacitor | 1–10 µs | **inside the MCU emulator**, in virtual time |
| firmware | instructions | the MCU emulator |
| CANopen between boards | 100 µs – ms | the emulator's CAN hub |

The rule: **anything that closes a loop faster than the co-simulation step
must live inside the emulator.** The slave samples its comparator a few
microseconds after its own clock edge; if the light pulse were computed outside
Renode it would arrive a whole step late. So the chain ICs and the light link
become emulator peripherals driven by the MCU pins, and only the slowly
changing beam transmittances cross the co-simulation boundary.

## 4. Model the physics

Build the physical model as a small module with no simulator dependency, and
check it with numbers you can verify by hand. For the light curtain
(`embeddyn/mechatronics/optics.py`):

* a beam is a bundle of rays between the emitter lens and the receiver
  aperture, so partial blocking dims it smoothly;
* power follows inverse square and generalised-Lambertian emitter and receiver
  profiles, so a tilted part loses signal;
* the receiver chain adds ambient light with demodulation rejection, shot
  noise, settling and a comparator with hysteresis.

Checks that caught real mistakes:

* **Geometry sanity.** With a 12 mm beam stand-off and the torso top at 110 mm,
  the beam must break when the face reaches ≈ 122 mm: it did, and the beam
  dims from 100 % to 0 % over ≈ 2 mm, the lens diameter.
* **Site axes.** The first version read the *row* of MuJoCo's row-major
  rotation matrix instead of the column; the off-axis terms were wrong. Check
  frames on one known site before trusting them.
* **Units.** `10 mW/sr` silently became 10 W/sr because the steradian was not
  a known unit, and every receiver sat at the rail. The fix was twofold: add
  the unit, and make model parameters reject any unit that does not convert.
  Prefer loud failures in parsers.

## 5. Close the loop on a supported MCU first

Before investing in the target MCU, close the whole loop with an MCU the
emulator already runs well (here an STM32L476 in Renode): scene, optics,
firmware scanning the beams, motion control, ground-truth metrics. This exposes
interface and timing problems while they are cheap:

* the beam address had to be read from the GPIO output register every step,
  while slower values (speed commands, status) could be read every millisecond
  (`interval` on Renode output bindings);
* Renode inputs that rarely change (touch pads, comparator) are now sent only
  when they change;
* ray casting dominated MuJoCo's cost until rays were batched per emitter point
  (`mj_multiRay`) and the blocked-beam diagnostic ran once per millisecond.

Profile with `cProfile` early; the first run took 22 s of wall time per
simulated second. The prototype's result (both heads close in from 350 mm and
hold 10.9–12 mm from the body without touching) is the reference the target
version must reproduce.

## 6. Get the target toolchain

Install the vendor toolchain on the machine that runs the emulation, and prove
it with a build for the exact part. The XC16 installer insisted on root to
create `/opt/microchip`; instead of `sudo` it ran in an unprivileged sandbox
that maps that path onto the data drive:

```bash
bwrap --unshare-user --uid 0 --gid 0 --dev-bind / / --tmpfs /opt \
  --bind <drive>/xc16-linux/opt-microchip /opt/microchip \
  ./xc16-v2.10-full-install-linux64-installer.run --mode unattended \
  --LicenseType FreeMode --netservername localhost --prefix <drive>/xc16-linux/v2.10 --ModifyAll 0
```

Verify the download against the published checksum and compile a test program
with `-mcpu=30F5011` before going on. (Every compile prints an `XCLM` privilege
warning; in Free mode it is harmless.)

## 7. Audit the emulator for the target

"The emulator supports device X" has to be checked, not assumed. Three quick
audits found that the dsPIC30F5011 could not run firmware yet:

1. **Does the CPU execute real code?** Count what the translator decodes. The
   dsPIC core in the fork decoded about ten opcode groups, with no literal
   moves, no addressing modes, no branches, no return, no flags. XC16's
   start-up code alone would trap. The existing validation only exercised
   flash and debugger operations, never execution.
2. **Are the peripherals compiled?** The dsPIC30 peripheral models existed only
   as sources in `_backup_peripherals`; neither Renode build contained them,
   there was no UART model, and the CAN model was not connected to a CAN hub.
3. **Does the platform match the datasheet?** Data RAM was mapped at `0x1000`
   while the device (and the C runtime's stack) starts at `0x800`; the second
   CAN module was missing.

Write the audit result down and decide with the product owner how to proceed.
Here the options were: complete the Renode core, drive Microchip's MPLAB X
simulator, or write a separate emulator. Completing the core was chosen because
it keeps the CAN hub, GPIO events and External Control that the rest of the
system already uses.

## 8. Complete the emulator core

The approach that keeps a CPU core honest:

* **An interpreter with memory callbacks.** Write the instruction set as one
  plain C module (`dspic30_step()`), with callbacks for data-space and
  program-space access. In Renode every translated instruction calls it, and
  the translation block ends whenever the PC does not simply advance. That one
  rule covers branches, skips, REPEAT, DO loops and interrupts, and the same
  file compiles natively for fast testing.
* **The opcode table as checklist.** Implement every non-DSP entry of the
  extracted table (≈ 300 base operations for dsPIC30), then the DSP class.
* **Core registers in data space.** On dsPIC the W registers, SR, CORCON,
  TBLPAG, PSVPAG, RCOUNT and friends are memory-mapped (`0x0000…0x0052`); the C
  runtime writes them as memory, so data accesses in that range must go to the
  CPU state, not the bus.
* **Program space visibility.** Data addresses ≥ `0x8000` with CORCON.PSV set
  read the lower 16 bits of program words (`PSVPAG:EA`); TBLRDL/H read the
  low word and the high byte.
* **Differential testing against the vendor simulator.** Step the same ELF in
  `sim30` and in the new core and compare W registers, SR and data memory after
  every instruction (`sim30` prints them with `DW`, `DS`, `DF`). Test
  material: per-instruction programs with random operands built with the
  assembler, plus real compiled C (start-up code, integer division, interrupts).
  Any difference is either a core bug or a documented simulator limitation.

## 9. Model the board electronics in the emulator

With a working core, the board-level models become Renode peripherals,
connected to MCU pins and running in virtual time:

* **iC-NT chain:** DIN/CLK/SHEN in, LED on between two CLK rising edges, the
  token passed on the falling edge when SHEN is high, LED current from RSET.
* **iC-ME chain:** two channels per IC, activation and settling times, the
  bandpass response to the 300 kHz carrier, compression of the output current.
* **Front end:** comparator with hysteresis, RC charge of the measurement
  capacitor while the comparator is high, discharge through the reset
  transistor, the capacitor node on the slave's ADC.
* **Light link:** an emulation-level object between the master and slave
  machines holding the transmittance matrix (including neighbour cross-talk),
  updated from MuJoCo every co-simulation step.

Test each model against the timing diagrams of its datasheet before connecting
firmware to it.

## 10. Port or write the firmware

Run the product firmware unmodified where it exists; otherwise write firmware
that follows the real board's design. For the body contour: CANopenNode with a
dsPIC30 CAN driver; the master sends the start sign, clocks the LED chain and
modulates each LED with the 300 kHz burst; the slave clocks the receiver chain,
resets and reads the measurement capacitor per pixel and publishes the beam
values.

## 11. Verify with scenarios and faults

Turn requirements into checks that run against the real firmware:

* ground truth from the physics (head-to-body clearance from MuJoCo distance
  queries; touch-pad forces) next to what the firmware believes;
* metrics such as `gap` statistics and `threshold` crossings for the touch pads
  and the fault output;
* fault injection: a stuck comparator, a dimmed LED, lost or corrupted CAN
  frames, and a scenario per fault that states what the system must do
  ([fault injection](../reference/faults.md)).

## 12. Keep the record

* **Design page** in the docs for each product, updated with every answer.
* **Roadmap** entries for gaps found on the way.
* **Commits per topic**, even when two pieces of work share a file; stage
  hunks separately.
* **Forks stay local** until their owner decides to push.
* **Notes on tool locations** (emulator builds, toolchains, sandboxes) so the
  next session does not rediscover them.

## Lessons that transfer

* Ask about the product's behaviour early; datasheets give limits, not designs.
* Get primary sources for every chip on the signal path, with revisions.
* Split by time scale before writing code; fast loops belong inside the emulator.
* Prototype the loop on a supported MCU, then port.
* Audit emulator support by executing code, not by reading platform names.
* Use the vendor's simulator as an oracle and test differentially.
* Make parsers fail loudly on what they do not understand.
