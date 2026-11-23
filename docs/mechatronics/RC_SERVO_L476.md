# RC servo controlled by an STM32L476 (Renode in the loop)

[`examples/mechatronics/rc-servo-l476.yaml`](../../examples/mechatronics/rc-servo-l476.yaml)
runs real bare-metal STM32L476 firmware in Renode against a hobby RC-servo model.
The firmware is unmodified between emulation and hardware. It sees the plant
only through its peripherals: ADC channels in, the TIM2 pulse pin and a fault LED out.

```bash
make -C examples/mechatronics/firmware/rc-servo-l476        # arm-none-eabi-gcc, no SDK
export RENODE_BIN=/path/to/renode                           # custom-cores build, see RENODE.md
embeddyn mechatronics run examples/mechatronics/rc-servo-l476.yaml -o servo.csv
pytest tests/mechatronics/test_rc_servo_l476.py
```

## System

| Instance | Domain | Backend | What it is |
|----------|--------|---------|------------|
| `mcu` | firmware | Renode | STM32L476 running `firmware/rc-servo-l476.elf` |
| `servo` | mechanical | python `RCServo` | pulse decoder, servo amplifier, DC motor, 200:1 gearbox, horn |
| `operator` | environment | replay | command potentiometer and arm load torque over time (`rc-servo/command-profile.csv`) |

| Pin | Peripheral | Co-simulation signal |
|-----|------------|----------------------|
| PA0 | TIM2_CH1 (AF1), 50 Hz | `mcu.servo_pulse`: width of the last pulse in seconds (`gpio_pulse_width`) |
| PC0 | ADC1_IN1 | `operator.command_voltage`, 0–3.3 V = −90…+90° |
| PC1 | ADC1_IN2 | `servo.feedback_voltage`, the servo's feedback potentiometer |
| PA5 | GPIO out | `mcu.fault_led` |
| PA2 / PA3 | USART2 TX/RX (AF7), 115200 8N1 | `mcu.console` events: telemetry every 100 ms; `R` clears a fault, `?` prints the calibration |

## Firmware

[`firmware/rc-servo-l476/main.c`](../../examples/mechatronics/firmware/rc-servo-l476/main.c)
is plain register-level C. The clock path is HSI16 → PLL → 80 MHz. TIM2 runs at
1 MHz (PSC = 79) with a 20 ms period. Its update interrupt runs one servo frame,
and the core sleeps (WFI) between frames. Each frame:

1. reads the command potentiometer → target angle, clamped to ±80°;
2. limits the set-point slew rate to 400°/s;
3. **trim:** an integrator on (set-point − feedback angle) adds a pulse
   correction (±6° max) that removes the servo's droop under load and its
   dead band. It only integrates while the set-point and the servo are both at
   rest, so it never winds up during a move;
4. **stall monitor:** an error above 25° for 25 frames (0.5 s) latches a fault.
   The pulses stop, so the servo goes limp, and the LED turns on.

The calibration `servo_cal` sits at 0x20000000 and the telemetry `tm` at
0x2000002C (fixed by the `.cal` / `.telemetry` sections in `link.ld`). Tests
and tuning jobs can change gains or limits over the system bus without
rebuilding. The bindings read the set-point and trim from `tm`.

## Renode platform

[`renode/stm32l476-servo.repl`](../../examples/mechatronics/renode/stm32l476-servo.repl)
is self-contained and uses the real L476 addresses. The custom-cores
`stm32l476.repl` can't be used: it pulls in an `STM32SPI` constructor the
branch doesn't have, and it models neither ADC nor RCC.

* **RCC:** a small Python peripheral where the ready flags follow their enable bits and SWS
  follows SW, so the firmware's real clock bring-up completes.
* **ADC1:** Renode's `STM32F3_ADC`, the same ADC IP and register layout as the L4,
  common registers at +0x300 included. It needs patch 0002 (IADC on
  `STM32_ADC_Common`). Values are set in microvolts (`scale: 1.0e6`).
* **TIM2:** `STM32_Timer` at 80 MHz, 32-bit; channel 1 drives PA0.

One emulation detail shaped the firmware. Renode's ADC model finishes a
conversion sequence one sync point after DR is read, and it ignores an ADSTART
issued before then. `adc_read()` therefore waits for ADSTART to clear before
starting the next conversion, which is also valid on hardware.

## Servo model (`embeddyn.mechatronics.plants:RCServo`)

* **Pulse decoding:** 1.5 ms = 0°, ±1 ms = ±90°. A width outside
  0.4–2.7 ms means no signal, and the amplifier switches off.
* **Amplifier:** proportional (150 V/rad), with a 0.46° dead band (≈ 5 µs) and speed
  feedback, saturating at the 6 V supply.
* **Motor and gearbox:** kt = ke = 2.3 mN·m/A, R = 4 Ω, 200:1 at 70 % efficiency.
  Stall torque is 0.48 N·m and no-load speed 0.08 s/60°.
* **Output:** horn and arm inertia, viscous and Coulomb friction, an external load
  torque, and soft end stops at ±100°. The feedback pot gives 1.65 V + 2.7 V/180° · angle.

## Results (Renode custom-cores, 10 µs quantum)

Profile: 0° → +45° at 0.1 s → −45° at 0.6 s → +30° at 1.1 s, and a 0.3 N·m load from 1.5 s.

| | Result |
|---|---|
| +45° move | rise 0.10 s, settles (5 %) in 0.17 s, 2.8 % overshoot |
| −45° move | rise 0.16 s, settles in 0.26 s, 0.9 % overshoot |
| Hold at 30° under 0.3 N·m | 29.7° with trim (1.6° of trim); without trim the servo droops 1–4° (test) |
| Load above stall (1 N·m) | fault latched 0.5 s after the error passes 25°, pulses stop, LED on (test) |
| Pulse measured vs firmware | equals 1500 + (set-point + trim) · 11.11 µs to ±1.5 µs |

The 2.5 s run took 15–35 s of wall time on a heavily loaded host, Renode start-up included.
The first complete pulse arrives within the second frame (≤ 40 ms),
because the timer output only goes high at its first update event.

### Pulse resolution and speed

`gpio_pulse_width` measures each pulse from GPIO edge timestamps. They resolve
1 µs (0.09°) even with a 10 µs sync quantum. Larger quanta run faster: a
standalone 1 s run, start-up included, took 7.4 s at a 50 µs quantum, 9.3 s at 10 µs and
18.3 s at 2 µs.
Set the quantum with `variables: {quantum: ...}` in the `mcu` options.

## Tests

`tests/mechatronics/test_rc_servo_l476.py`:

* always run: the servo model (pulse mapping, droop under load, going limp without a
  signal) and the pulse-width decoder;
* with `RENODE_BIN` and the built firmware:
  * the full profile, checking move metrics, trimmed hold, and pulse width against the firmware telemetry;
  * trim switched off by writing `servo_cal.trim_ki = 0` over the bus, so the servo droops;
  * the USART2 console: identical telemetry lines in two runs, and `R` over the UART clears a latched stall fault;
  * stall: fault latched, pulses stopped, motor current zero.

The `renode-cosim.yml` CI workflow builds the firmware and runs these tests.
