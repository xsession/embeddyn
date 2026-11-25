---
name: mcu-differential-testing
description: Verify a CPU core step by step against a reference
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [embedded, cpu, testing, simulator]
    category: embedded-emulation
---

# Differential testing of a CPU core

The core is correct only when it matches a reference (the vendor's instruction simulator, a
second simulator, or silicon traces) on every instruction form, for millions of steps. This skill
turns "it seems to work" into "identical except for these documented cases".

## When to Use

- After writing or changing a CPU core (`mcu-cpu-core`), and before any Renode integration.
- After every core fix: rerun the whole suite, not just the failing test.

## Procedure

1. **Reference trace.** Script the reference simulator to load the ELF, reset, then per step print
   the PC, the opcode, the registers and the status, and step. At the end, dump RAM.
   - The trace must hold the state before every instruction, the same as the native runner prints.
   - `references/sim30-example.md` shows a complete script and parser for Microchip's sim30.
2. **Same program, both sides.** Build it once (`build_firmware`). Run the reference and the native
   runner for the same number of steps, then convert both traces to JSON Lines
   (`trace_from_columns`, or your own converter).
3. **Compare** with `compare_traces reference=... candidate=...`.
   - On `ok: false`, the tool gives `step`, `differences` and `previous_reference`: the
     instruction that ran just before the divergence is the suspect.
   - Disassemble around its PC (`disassemble`).
4. **Random generators.** Write a generator per instruction class that emits assembly:
   1. set all registers to random values;
   2. point the address registers into valid RAM, with alignment respected;
   3. seed the memory under test;
   4. emit the instruction under test.

   Branches and skips jump over a marker instruction (`inc R7`), so that a wrong decision shows up
   in a register. Use 4–6 seeds per class, a few hundred instructions each, sized to fit the flash.
5. **Classes to cover:**
   - every ALU form in every addressing mode, at byte and word size;
   - shifts with literal and register counts;
   - multiply/divide;
   - compares and skips; branches on every condition;
   - stack, call/return, link/unlink;
   - repeat and hardware loops;
   - table and program-space reads;
   - DSP/accumulator units under every mode bit;
   - special addressing (modulo, bit-reversed), with legal buffers only;
   - compiled C (start-up code, soft float, 64-bit division, switch tables, function pointers,
     constant tables).
6. **When they differ, decide who is right.** Check, in order:
   1. the reference manual's instruction page;
   2. a second simulator, if one exists (vendor IDE simulator sources are a good second opinion);
   3. the generator: are the operands legal? An offset register used as a pointer writes into
      core registers.

   If the reference is wrong, keep the manual's behaviour. Then either make the comparison
   tolerate that difference (with a counted mask) or stop the generator from producing that form.
   Add a row to the "known differences" table: case, reference behaviour, core behaviour, source.
7. **Record the status** in the test tool's README: the suites, the step counts, the tolerated
   counts, and the known-differences table.

## Pitfalls

- Inside a repeat loop the reference may expose intermediate states that the core computes at
  once. Compare again once the repeat has ended, and do not count those steps as failures.
- Reference simulators model a generic "super" device. Its memory map (RAM ranges, special data
  spaces) can differ from the real part: make such ranges parameters of the core.
- Operations the manual leaves undefined (for example division-overflow results) are not bugs.
  Tolerate them and document them.
- Flash is small. Generators must keep each test under the program-memory size, or the link fails.
- A tolerance must name the exact bits (mask), never "ignore the status register".

## Verification

- Every suite prints "N steps identical (tolerated: ...)", with N in the thousands per test and the
  tolerated counts explained in the README table.
- Rerun the full suite after the last core change.
