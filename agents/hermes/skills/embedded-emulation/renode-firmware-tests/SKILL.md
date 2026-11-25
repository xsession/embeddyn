---
name: renode-firmware-tests
description: Test Renode MCU models with small firmware programs
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [embedded, renode, testing, firmware]
    category: embedded-emulation
---

# Firmware tests for MCU models

A model is verified only by firmware that uses it the way real code does, with expected values
computed from the data sheet before the run.

## When to Use

- After writing or changing any peripheral model or the CPU glue.
- Before committing emulator changes: rerun every test.

## Procedure

1. **Copy `templates/test_firmware.c`.** Every check stores a value in `r[k]` at a fixed RAM
   address, and `r[0]` holds the number of the last step reached. Use the vendor's register
   names from the device header.
2. **Write the expected value of each `r[k]` first**, from the data sheet, in a table: register
   setup → formula → number. Examples:
   - timer ticks = run time × Fcy / prescale / (PR + 1);
   - UART time = characters × frame bits / baud;
   - ADC count = round(V / Vref × 4095).
3. **Build:** `build_firmware profile=<mcu> source=<file>.c`. Fix compile errors from the returned
   `errors`.
4. **Run:**

   ```
   renode_run platform=<platform> loads=<load_commands> run_for=0.1
              reads=["0xA00", "0xA02", ...]
   ```

   Add these as needed:
   - `stimuli`, for pin edges or supply changes at set times: `[{"at": 0.03, "command": "portf OnGPIO 6 true"}]`;
   - `before`, for analog inputs (`adc SetVoltage 1250000 3`) or a CAN hub
     (`emulation CreateCANHub "hub"` and `connector Connect sysbus.can1 hub`);
   - `after`, for model properties (`oc1 DutyCycle`, `can1 BitRate`).
5. **Compare** each read with the expected table.
   - If `r[0]` is lower than the last step, the firmware hung waiting for a flag: that peripheral
     is the suspect.
   - Look at `problems` too: unmapped accesses and model warnings.
6. **Script it.** Save a `.resc` next to the firmware with `$ORIGIN/` paths and repo-relative
   platform paths, plus `echo` lines naming the expectation. Add it to the tests table in the
   reference document.

## Pitfalls

- A wrong expectation is as likely as a wrong model. If the numbers disagree, recheck the test's
  register setup against the data sheet before changing the model. Real examples of test bugs:
  - a missing underflow-mode bit;
  - a pointer register that held an offset;
  - a clock assumed instead of read from the fuses.
- Configuration fuses in the test firmware decide the clock (PLL!). Write them explicitly
  (`#pragma config`) and compute expectations with that Fcy.
- `while(!flag);` loops hang on model bugs. Keep timeouts in loops, or check `r[0]` to see where
  it stopped.
- Renode is whole-MIPS: tests that count instructions against a timer must allow for the rounding.
- Timing tests need a margin: one quantum or one prescaler tick.

## Verification

- Every `r[k]` matches its expected value; `r[0]` equals the last step; `problems.distinct` is 0.
