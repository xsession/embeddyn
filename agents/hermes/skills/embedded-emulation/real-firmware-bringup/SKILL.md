---
name: real-firmware-bringup
description: Run product firmware on an emulated MCU and fix gaps
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [embedded, renode, firmware, debugging]
    category: embedded-emulation
---

# Bringing up real product firmware

## When to Use

- The models pass their own tests, and the product's firmware (HEX/ELF) should now run.

## Procedure

1. **Read-only.** Never modify the product firmware or its repository. Work on copies in a
   scratch directory.
2. **Board file.** Write a board `.repl` that `using`s the MCU platform and sets the board facts:
   - the external oscillator frequency (from the firmware's clock constants or the schematic);
   - connected devices (I2C/SPI parts, CAN hub).

   Give the board file a neutral name, never the product's (`emulation-privacy-hygiene`).
3. **Images.** `hex_to_images profile=<mcu> hex=<copy of the product hex>`.
4. **First run:**

   ```
   renode_run platform=<board.repl> loads=... run_for=0.5 after=["system CpuFrequency"] reads=[...]
   ```

   Check:
   - `problems`: unmapped reads/writes name missing registers;
   - illegal opcodes point to core gaps;
   - `after`: is Fcy what the firmware's own constants imply (timer periods, baud divisors)?
5. **Each problem** goes back to the model or core skill, with a firmware test that reproduces it.
6. **Speed.** If `host_mips` is far below that of compute-bound code, run `pc_hotspots`, then
   `disassemble` the top PCs. Typical causes, and their fixes:
   - a fast timer raising events → lazy mode;
   - polling a flag in a tight loop → SyncTime + lazy flag;
   - bit-banged pins → incremental port updates; MMIO cost is per access.
7. **Behaviour checks.** Compare what the firmware should be doing with model properties:
   - PWM frequency and duty;
   - UART output (`uart` capture);
   - CAN frames through a hub;
   - GPIO levels.
8. **Co-simulation.** Hand over to embeddyn bindings (adc/gpio/can/sysbus endpoints) once the
   firmware behaves.

## Pitfalls

- Product firmware often derives everything from one clock constant. If PWM or baud rates are off
  by a constant factor, the board's oscillator frequency or a fuse decode is wrong, not the
  peripheral.
- Disassembly and file listings of the product firmware are private. Keep them out of commits,
  docs and chat logs (redaction rules).
- Do not "fix" the emulator to match a firmware bug. Silicon behaviour is the reference.

## Verification

- `problems.distinct` is 0 (or only explained warnings); rates match the firmware's own constants;
  speed is recorded.
