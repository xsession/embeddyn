---
name: renode-peripheral-models
description: Write Renode C# models of MCU peripherals
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [embedded, renode, peripherals, csharp]
    category: embedded-emulation
---

# Renode peripheral models for an MCU

## When to Use

- After the CPU runs in Renode, to model every on-chip peripheral until `sfr_coverage` reports 0
  uncovered SFRs.
- For a detail pass on existing models: clock, power, timing, pins.

## Procedure

1. **Plan the blocks.** `sfr_coverage sfr_map=... platform=...` lists the uncovered registers, and
   the SFR map clusters give the blocks. Model in this order:
   1. interrupt controller;
   2. system (oscillator/clock, reset causes, watchdog, NVM, module disable);
   3. timers;
   4. GPIO ports and change notification;
   5. UART;
   6. output compare / input capture / PWM;
   7. ADC;
   8. SPI;
   9. I2C;
   10. CAN;
   11. anything else (codec, DMA, comparators, ...).
2. **One block at a time:**
   1. Generate a skeleton: `peripheral_skeleton name=<Family>_<Block> out=<Peripherals>/<Family>/<file>.cs
      sfr_map=... start=0x.. end=0x.. irqs=[IRQ]`.
   2. Derive it from the shared base (`templates/SfrBase.cs`): word and byte access with byte
      merge, plus module disable.
   3. Fill in the behaviour from the reference manual chapter, using the matching checklist in
      `references/peripheral-checklists.md`.
   4. Write the class `<summary>` as a behaviour spec: registers, modes, timing, power, limits.
3. **Platform entry** in `platforms/cpus/<vendor>/<mcu>.repl`:
   - the base address;
   - `system: system` for the clock;
   - `IRQ -> intc@<vector>`;
   - `Pmd: system` and `PmdBit: n` when the MCU has module disable;
   - pins through the port mirrors.
4. **Build and check:**
   1. `renode_build target=all`;
   2. `renode_check_platform`;
   3. a firmware test for the block (`renode-firmware-tests`).
5. **Coverage.** Run `sfr_coverage` again; continue until `uncovered_count` is 0.
6. **Detail pass** (after all blocks exist). Go through each item and test each one:
   - **Clock:** Fcy from the configuration fuses and clock-switch registers, published by the
     system model (`ClockChanged`). Peripherals read it instead of having fixed frequencies.
   - **Power:** Run, Idle and Sleep from the CPU's power-save events, with wake-up on the next
     instruction or interrupt. Models stop in Sleep when they run from the CPU clock, and stop in
     Idle when their SIDL bit is set. Wake sources: interrupts, UART start bit, ADC on its RC
     clock.
   - **Bus timing:** UART character time, SPI clock, I2C bit time and CAN frame time, each with a
     LimitTimer in nanoseconds. Busy bits stay set until the action is done.
   - **Lazy timers:** a fast timer with its interrupt disabled and no consumer must not raise an
     event per period. Compute TMR and the period flag from the count when the firmware reads
     them.
   - **SyncTime:** call `SyncTime()` (on the CPU thread) before reading timer counts, polling
     lazy flags or capturing.
   - **Pins:** port mirror outputs feed peripheral inputs (INTx, capture, timer clocks, slave
     selects, faults). Peripheral outputs (compare/PWM) override their port pins. Analog-selected
     pins read 0.
   - **Fault injection hooks:** CAN error counters, missing ACK, supply voltage (brown-out,
     low-voltage detect), oscillator failure (clock monitor trap).

## Pitfalls

- A byte write to an SFR must not trigger the read side effects of the register (FIFO pops). Merge
  the byte in the model (`Write(offset, value, mask)`); never read-modify-write through the bus.
- Interrupt flags are edge-set by hardware and cleared by software: raise a GPIO pulse (`Blink()`)
  into the controller, and never clear the flag from the model.
- Constructors run before memory is loaded. Read configuration fuses on the machine `Started`
  event, never in the constructor.
- Two models that reference each other in a `.repl` (system ↔ ADC) form a cycle: make one side a
  settable property.
- LimitTimer events are exact in virtual time, but plain reads mid-block lag by up to a quantum.
  Hence `SyncTime()`.
- High-rate events (a 300 kHz PWM edge, a timer period) cost a CPU sync each. Make them lazy or
  optional (`pwmEdges`), and expose duty and frequency as properties instead.
- When a test fails, first check the test's own register setup against the data sheet (wrong
  config word index, missing UNFM bit, unaligned buffers), then the model.
- Constants that no vendor file states (device ID, voltage thresholds, segment sizes) become
  constructor parameters, with defaults marked "unverified" in the reference.

## Verification

- `renode_check_platform`: no errors.
- `sfr_coverage`: `uncovered_count` is 0.
- One firmware test per block, plus one detail test, all passing (`renode-firmware-tests`).
- The reference document (registers, behaviour, timing, limits) lists every model. See
  `references/reference-doc-outline.md`.
