# Case study: dsPIC30F5011 in Renode

The workflow applied once, end to end. Use it as the yardstick for scope and verification depth.

## Starting point

- Renode had a `CPU.dsPIC33` class with a skeleton translator (about ten opcodes).
- Peripheral stubs existed but were not compiled.
- The vendor toolchain (XC16) ships `p30F5011.h` (bit-field structs), `p30F5011.gld` (SFR
  addresses, vector table) and `sim30` (an instruction-level simulator).

## Phase results

1. **Facts.** `extract_sfr_map` gave 618 SFR names, 242 with bit fields. `extract_vectors` gave 54
   vectors (`skip_traps: 8`). The pin map (CN numbers, INTx/ICx/OCx pins) came from the product
   firmware's pin header and the data sheet's pin table.
2. **Core.** One C file, `dspic30_core.c`, with callbacks for byte and word data, program read and
   table write. It serves the core registers mapped into data space (0x0000–0x0053, INTCON1)
   itself. It has a native runner (`d30run`) that prints `PC OP W0..W15 SR` per step.
3. **Differential tests against sim30.**
   - Six random seeds for every non-DSP form.
   - Six random DSP seeds under random CORCON modes.
   - Six modulo/bit-reversed addressing seeds.
   - Compiled C (soft float, 64-bit division, PSV tables, function pointers).

   All identical except for documented sim30 deviations: word DC flag, DIV iteration flags, a
   destination post-decrement bug under modulo addressing, and others.
4. **Renode.**
   - One helper call per instruction. Blocks run straight-line code and end at the first
     non-sequential PC.
   - Bus layout: data space at 0x0000; program word *a* at 0x01000000 + 2a (4 bytes each, the
     byte layout of the HEX file).
   - 1 KB pages, because RAM starts at 0x0800.
   - Speed: about 18 MIPS for compute-bound code.
5. **Peripherals** (all SFR blocks):
   - interrupt controller;
   - system (clock from fuses, power states, reset causes, watchdog, NVM, PMD);
   - Timer1–5;
   - OC ×8, IC ×8;
   - 12-bit ADC;
   - UART ×2, SPI ×2, I²C, CAN ×2;
   - DCI;
   - GPIO ×5 with change notification.
6. **Tests.** Five scripted test firmwares (smoke, periph, periph_all, periph_detail, wdt_reset).
7. **Real firmware.** It ran at once without illegal opcodes or unmapped accesses.
   - Its fuses gave Fcy 20 MHz, and the OC PWM came out at 298.5 kHz, matching the firmware's own
     303 kHz constant.
   - Speed went 0.7 → 2.6 MIPS after lazy timers, and → 4.7 MIPS after incremental port updates.
     The PC hot spots showed bit-banged chain clocking.

## Bugs that only tests found

- Translation-block exit through the footer's "interrupted" path restored the PC to the block start,
  so the CPU never advanced. Fix: the epilogue must exit the block itself.
- The watchdog read configuration word 2 instead of 1. A dedicated watchdog test caught it.
- Timer values read mid-block lagged by a time quantum. Fix: SyncTime before timer reads.
- A test generator fed an offset register as a pointer, so stores hit core registers. Always check
  that generated operands are valid before blaming the core.
