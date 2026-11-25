---
name: renode-tlib-core-integration
description: Wire a C interpreter core into Renode tlib and C#
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [embedded, renode, tlib, cpu]
    category: embedded-emulation
---

# Interpreter core inside Renode (tlib + C# CPU class)

## When to Use

- The CPU core passes its differential tests and must now run inside Renode.

## Procedure

1. **Files** in `src/Infrastructure/src/Emulator/Cores/tlib/arch/<arch>/`:
   - `cpu.h`: CPUState embeds the core state before `CPU_COMMON`;
   - `translate.c`, `op_helper.c`, `helper.c`, `helper.h`;
   - `arch_callbacks.[ch]` (C to C#) and `arch_exports.[ch]` (C# to C);
   - `cpu_registers.[ch]`;
   - the core `.c`/`.h`.

   The arch CMake collects `*.c` by glob: rerun `cmake .` in the obj directory after adding files.
2. **Bus layout.** Give program memory its own bus window if the architecture is Harvard, for
   example data at 0x0000–0xFFFF and program word *a* at `BASE + scale·a`, laid out like the
   toolchain's HEX bytes so that `hex_to_images` works.
   - Set `TARGET_PAGE_BITS` so that RAM starts on a page boundary (1 KB pages for RAM at 0x0800).
   - Set `TARGET_PHYS_ADDR_SPACE_BITS` large enough for the program window.
3. **One helper per instruction** (the translate.c fragment in `references/glue.md`):
   - `gen_intermediate_code` emits `gen_helper_step(sequential, env, index)`. If the helper
     returns 0 (branch, skip, loop end, trap), it stores the executed count into
     `instructions_count_declaration` and ends the block with `gen_exit_tb_no_chaining`.
   - Otherwise it continues with the next instruction.
   - Stop the block after control-flow opcodes and at page ends.
   - The epilogue sets the PC to the next instruction and exits.
4. **The helper:**
   1. converts the bus PC to the architectural PC;
   2. calls `step()`;
   3. converts back;
   4. services traps;
   5. maps the events: sleep/idle set `wfi` and call a callback, reset calls a callback and waits,
      an undefined opcode raises an exception (the device reset), break raises EXCP_DEBUG.
5. **Interrupts.** The C# interrupt controller publishes its best pending source and priority
   through an export (`tlib_set_pending_interrupt`) and drives the CPU's IRQ line.
   `process_interrupt` compares the published priority with the CPU's (in C, without a callback
   per block), then calls `take_interrupt` and clears `wfi`.
6. **Registers for C#.** Write `tlib_get/set_register_value_32` by hand when the state isn't made of
   `uint32_t` fields. Mirror the indices in `<Cpu>Registers.cs`.
7. **C# CPU class.**
   - `[Export]` methods for the callbacks; `[Import]` fields for the exports.
   - Public events for reset requests, watchdog clears, table writes, power save and wake.
   - A `ProgramAddress()` helper.
8. **Build:** `renode_build target=core` while iterating on C; `renode_build target=all` after
   C# changes. Then check with `renode_check_platform`, and run a smoke firmware with `renode_run`
   and its expected `reads`.

## Pitfalls

- **The footer trap.** If the block falls off its end, tlib's footer exit is the "interrupted"
  path: it restores the PC to the block start. The symptom: PC never moves while instructions are
  "executed". The epilogue must exit the block itself.
- **Instruction counts.** An early exit must declare how many instructions really ran
  (`instructions_count_declaration`), or virtual time drifts.
- Helpers that call `cpu_loop_exit` must have updated `env->pc` first.
- Peripherals that need virtual time mid-block (timer reads) must call `SyncTime()` on the CPU
  thread. See `renode-peripheral-models`.
- Renode's CPU rate is whole MIPS (`PerformanceInMips` is an integer). Keep peripherals on the
  exact clock and log the rounding.
- The default dotnet environment is not in a plain shell. The MCP server needs `DOTNET_ROOT`/`PATH`
  in its `env`.

## Verification

- `renode_run` of the smoke firmware returns the expected RAM values, with
  `executed_instructions` equal to MIPS × run time.
- `pc_hotspots` shows the final idle loop.
- Host speed: compute-bound firmware should reach 10+ MIPS. Below 3 MIPS, check for per-instruction
  blocks or chatty callbacks.

Reference: `references/glue.md` (the translate.c, op_helper.c and C# fragments that worked).
