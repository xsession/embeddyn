---
name: mcu-cpu-core
description: Write an instruction-set interpreter for an MCU
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [embedded, cpu, interpreter, isa]
    category: embedded-emulation
---

# CPU core as a standalone interpreter

Renode cores are tlib (QEMU-derived) translators. For a CPU that Renode lacks, write a plain C
interpreter and call it from tlib once per instruction. It is simpler than a TCG translator,
testable natively, and fast enough (tens of MIPS).

## When to Use

- The target CPU has no working tlib core, or the existing one is incomplete.

## Procedure

1. **Gather the encodings** from the toolchain's assembler opcode table (binutils `*-opc.c`/`*-opc.h`
   in the vendor's open-source drop) and the programmer's reference manual. For every
   instruction group, write probe assembly, assemble it, and disassemble it (`disassemble` tool) to
   confirm the bit fields. Fields guessed from the manual alone are often off by a bit.
2. **Copy `templates/core.h`** and adapt the state struct:
   - registers, status bits and control registers;
   - loop and repeat state;
   - pending traps;
   - the cycles of the last step.

   Keep everything in one struct: Renode saves and restores it.
3. **Bus callbacks:** data read and write of 8 and 16 (or 32) bits, program read, program write.
   The core never touches memory directly. Registers that the architecture maps into data space
   are served by the core itself before calling the bus.
4. **`step()` executes one instruction** (one iteration of a repeat) and returns event flags:
   - SLEEP, IDLE, RESET, BREAK;
   - UNDEF (PC not advanced);
   - TRAP;
   - a watchdog clear.

   Dispatch on the top opcode bits with a `switch`, and keep per-group helpers short.
5. **Order of implementation:**
   1. moves, ALU with flags, branches;
   2. calls and returns, stack;
   3. addressing modes;
   4. multiply/divide;
   5. loops/repeat;
   6. table/program-space access;
   7. traps and interrupt entry/return;
   8. DSP or extension units;
   9. special addressing (modulo, bit-reversed).
6. **Native runner** (`templates/runner.c`): load an Intel HEX file, step N instructions, and
   print one line per step: `PC OPCODE R0..Rn STATUS`, then `RAM ...` at the end. The differential
   tests compare that output (`trace_from_columns`).
7. **Interrupts and traps** need two entry points next to `step()`:
   - `take_interrupt(irq, priority)`: push the return state and vector;
   - `service_traps()`: take the highest pending trap.

   The CPU-side glue decides when to call them.

## Pitfalls

- Flags:
  - word versus byte carry, half carry, overflow on subtract (a - b - !C), and "sticky Z" on
    multi-precision ops each need their own test.
  - Write each flag rule as it appears in the manual's per-instruction table.
- Repeat loops: the repeated instruction runs count + 1 times, the PC does not advance meanwhile,
  and interrupts may occur between iterations. Keep the repeat count in architectural state.
- Hardware loops: test the "last iteration" and "nested level" rules separately; they are easy to
  get one iteration off.
- Shift counts from a register use only the low bits the manual names (for example 4:0).
- Byte operations on word registers must leave the upper byte alone.
- Self-modifying or table-written program memory: tlib must invalidate translated code (handled in
  the peripheral that writes flash).

## Verification

- The native runner executes the vendor's C start-up code and reaches `main`.
- A smoke program (arithmetic, recursion, constant tables) leaves the expected values in RAM.
- Then run the `mcu-differential-testing` skill. This skill is not done until that one passes.

References: `references/core-structure.md` (state, dispatch and helpers from the dsPIC30 core).
