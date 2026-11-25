---
name: mcu-facts-extraction
description: Collect MCU register maps, vectors, pins, timing
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [embedded, datasheet, svd, registers]
    category: embedded-emulation
---

# MCU facts: profile, register map, vectors, pins

Every later phase depends on exact addresses and bit positions. Take them from machine-readable
vendor files with the tools, never from memory.

## When to Use

- At the start of an emulation project, or when adding a peripheral whose registers are not yet
  in the SFR map.

## Procedure

1. **Find the vendor files.** In priority order:
   1. CMSIS-SVD (Arm, many RISC-V parts);
   2. the toolchain's device header (`*.h` with bit-field structs or `#define` addresses);
   3. the toolchain's linker script (`NAME = 0xADDR;` equates, vector table);
   4. the data sheet and family reference manual (PDF; for behaviour and timing).

   Record each file's path and version.
2. **Write the profile.** Copy `templates/profile.json` to a directory named by
   `MCU_EMU_PROFILES`, then fill in the toolchain commands, files, image layout, memory map and
   Renode platform. Use `${VAR}` for machine paths. Run `mcu_profile_show name=<mcu>` until
   `missing` is `[]`.
3. **SFR map.** Run `extract_sfr_map profile=<mcu> out=<work>/sfr.json`, with `max_address` at the
   end of the SFR area if the linker script also defines RAM symbols. Check the result:
   - `registers` is in the hundreds for a typical MCU;
   - `clusters` lists one range per peripheral block;
   - `with_fields` is greater than 0 (bit fields were found).
4. **Spot-check three registers** against the data sheet, using `sfr_lookup` (for example the
   UART status register, a timer control register and an interrupt flag register). If a field
   position differs, the header parse picked the wrong union member: report it and use the data
   sheet.
5. **Interrupt vectors.** Run `extract_vectors profile=<mcu>`, using `skip_traps` for cores whose
   table starts with traps.
   - The vector index is the interrupt-controller input number of the source.
   - Write the table into the platform file's header comment.
6. **Pin map.** For each peripheral pin (external interrupts, capture inputs, compare outputs,
   timer clocks, serial pins, change-notification numbers), record port and bit. Sources: the data
   sheet pin table, and the vendor or product pin header. This drives the port routing later.
7. **Clock and reset facts.** Record:
   - the oscillator modes and PLL factors from the configuration-fuse encodings in the header
     (`#define XT_PLL8 0x...`);
   - the internal oscillator frequencies;
   - the reset causes;
   - the watchdog period formula.
8. **Facts file.** Write `facts.md` next to the profile, holding:
   - the source files and versions;
   - the memory map;
   - the vector table;
   - the pin map;
   - the clock table;
   - open questions, each marked UNVERIFIED.

## Pitfalls

- Linker scripts define aliases (`_U1MODE` and `U1MODE`); the tool drops the `_` variants. Count
  peripherals by `clusters`, not by raw register count.
- Bit-field structs often have unions with several layouts; the first one is the canonical one.
  Check any register whose layout looks odd against the data sheet.
- Never invent numeric facts (device IDs, voltage thresholds, segment sizes). If no file states
  them, make them parameters with a default marked "unverified" in the docs.
- Peripherals with the same layout (UART1/UART2) share one model: record base addresses and
  interrupt numbers per instance.

## Verification

- `mcu_profile_show` returns `missing: []`.
- `sfr_lookup` spot checks match the data sheet.
- The vector count matches the data sheet's interrupt table.
