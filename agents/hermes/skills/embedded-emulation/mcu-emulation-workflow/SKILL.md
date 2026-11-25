---
name: mcu-emulation-workflow
description: Plan and run full MCU emulation in Renode
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [embedded, renode, emulation, mcu, firmware]
    category: embedded-emulation
    requires_toolsets: [terminal]
---

# Full MCU emulation: the workflow

Goal: unmodified firmware for a microcontroller runs in Renode, instruction-exact, with every
on-chip peripheral modelled well enough that the product firmware behaves as on silicon. This is
the umbrella skill. It says which phase comes next and which skill and tools to use. Never skip a
phase's "done when" gate.

## When to Use

- The user wants a new MCU (CPU core and/or peripherals) emulated in Renode.
- The user wants an existing partial model completed ("support all peripherals").
- Real product firmware must run in emulation for testing or co-simulation (embeddyn).

## Procedure

Work in this order. After each phase, write a short status (what is done, what is verified, what
is not) before starting the next.

1. **Profile and facts** (skill `mcu-facts-extraction`).
   - Create an MCU profile JSON from the profile template in the `mcu-facts-extraction` skill.
   - Run `mcu_profile_show` until `missing` is empty.
   - Run `extract_sfr_map` and `extract_vectors`.
   - Done when: the SFR map has every peripheral block, the vector order is known, and the pin map
     is written down.
2. **Does Renode already have the CPU?** Check `src/Infrastructure/src/Emulator/Cores/tlib/arch/`
   and the existing `.repl` files.
   - If a correct core exists, skip to phase 5.
   - If the core is a skeleton or missing, do phases 3 and 4.
3. **CPU core as a standalone interpreter** (skill `mcu-cpu-core`). Write it in plain C with memory
   callbacks, plus a native runner. Done when it executes the vendor C start-up code and a test
   program.
4. **Differential testing** (skill `mcu-differential-testing`). Compare the core with the vendor's
   simulator, instruction by instruction:
   - random tests of every instruction form;
   - compiled C;
   - special modes.

   Done when every test is identical, or differs only in documented, justified ways.
5. **Into Renode** (skill `renode-tlib-core-integration`). Wire the interpreter into tlib and the
   C# CPU class. Done when `renode_run` of the smoke firmware reads the expected RAM values.
6. **Peripherals** (skill `renode-peripheral-models`). Build them in this order:
   1. interrupt controller;
   2. system/clock/reset/watchdog/NVM;
   3. timers;
   4. GPIO;
   5. UART;
   6. compare/capture;
   7. ADC;
   8. SPI;
   9. I2C;
   10. CAN;
   11. the rest.

   Check `sfr_coverage` after each one. Done when `sfr_coverage` reports 0 uncovered SFRs.
7. **Firmware tests** (skill `renode-firmware-tests`). Give every model a test firmware with
   computed expected values. Done when all tests pass and are scripted.
8. **Detail pass.** Go back over the models for clocks from fuses, power states, bus timing, lazy
   timers, pin routing and module disable (same skill as phase 6, section "Detail"). Then rerun
   every test.
9. **Real firmware** (skill `real-firmware-bringup`). Run the product firmware and resolve:
   - unmapped accesses;
   - wrong clocks;
   - hot spots.

   Done when it runs without problems at the expected rates.
10. **Documentation and hygiene** (skill `emulation-privacy-hygiene`).
    - Write a peripheral reference (registers, behaviour, timing, limits).
    - Run `privacy_scan`.
    - Commit only when the user asks. Never push without the user's OK.

## Tools (MCP server mcu-emu)

| Step | Tool |
|---|---|
| profiles | `mcu_profile_list`, `mcu_profile_show` |
| register map | `extract_sfr_map`, `sfr_lookup`, `extract_vectors`, `sfr_coverage` |
| firmware | `build_firmware`, `hex_to_images`, `disassemble` |
| Renode | `renode_build`, `renode_check_platform`, `renode_run`, `pc_hotspots` |
| core testing | `trace_from_columns`, `compare_traces` |
| code | `peripheral_skeleton` |
| hygiene | `privacy_scan` |

Every tool returns JSON with `ok`. On `ok: false`, read `error`, fix the cause, and call again.
Don't guess around it.

## Pitfalls

- Do not write peripheral code before the SFR map and vector order exist: register addresses
  guessed from memory are wrong more often than not.
- Do not trust one reference. Where the vendor simulator and the reference manual disagree, follow
  the manual (or the second simulator) and record the difference in a table, because silicon
  follows the manual.
- Never claim "verified" for something no test checked. Say "implemented, not tested".
- Keep the core self-contained (no Renode headers), because the same file must build natively for
  the differential tests.
- Make one change, rebuild, and test. Large unverified batches hide which edit broke the build.
- Private product names and paths must never enter any repository file; see
  `emulation-privacy-hygiene`.

## Verification

- `renode_check_platform` reports no errors.
- `sfr_coverage` reports `uncovered_count: 0` (with the CPU's register ranges as `cpu_ranges`).
- Every firmware test reads its expected values through `renode_run`.
- The real firmware runs with `problems.distinct == 0`, or only with explained, harmless warnings.

See `references/dspic30-case-study.md` for the complete worked example (numbers, decisions, bugs
found).
