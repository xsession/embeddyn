# Bind firmware variables

The cleanest way to observe and parameterise firmware is a **telemetry and calibration struct at a fixed RAM address**. The firmware stays unmodified for production: the struct is just ordinary globals the linker places predictably.

## 1. Place the structs

```c title="firmware sketch"
typedef struct {
    float kp, ki, kd;              /* written by the test bench (or flash defaults) */
    float abs_zero, abs_tolerance;
    uint32_t features;
} arm_cal_t;

typedef struct {
    float target, angle, duty;     /* read by the test bench */
    uint32_t fault;
} arm_telemetry_t;

__attribute__((section(".cal")))       volatile arm_cal_t       arm_cal = {6.0f, 3.0f, 0.15f, 0.0f, 0.05f, 0x7};
__attribute__((section(".telemetry"))) volatile arm_telemetry_t arm_tm;
```

Give the sections fixed addresses in the linker script, or simply read the addresses from the ELF:

```bash
arm-none-eabi-nm -n firmware/arm-joint-l476.elf | grep -E 'arm_cal|arm_tm'
```

Mark them `volatile` so the compiler re-reads values the bench may change between loop iterations.

## 2. Bind them

```yaml title="bindings/my-board.yaml"
adapter: renode
machine: l476
bindings:
  # outputs: firmware → signals (read after each step)
  - signal: mcu.angle
    endpoint: {object: sysbus, property: sysbus, address: 0x20000040, float32: true, direction: output}
  - signal: mcu.fault
    endpoint: {object: sysbus, property: sysbus, address: 0x20000060, width: 4, direction: output}
  # inputs: parameters/signals → firmware (written before each step)
  - signal: firmware.kp
    endpoint: {object: sysbus, property: sysbus, address: 0x20000004, float32: true, direction: input}
```

Inputs can name a **project parameter** (`firmware.kp`) as well as a signal, which is how [tuning](../tutorials/tuning.md) reaches unmodified firmware.

## 3. Mind the timing

* Inputs are written **before** each `RunFor`, outputs read **after** it. If the firmware copies calibration values only at boot, a value written later has no effect: read the struct in the control loop, or write it at step 0 only (initial value).
* Writes are not atomic with respect to the firmware. For multi-word values, use a sequence counter or have the firmware read them once per loop.

## Other binding kinds

| Need | Binding |
|---|---|
| PWM duty of a pin | `gpio_duty` (edges) or `register_ratio` (CCR/ARR) |
| RC/ESC pulse width | `gpio_pulse_width` |
| LED / digital output | `gpio_state` |
| analog input | `adc` (+ `scale` to the ADC's unit) |
| digital input | `gpio` (`threshold`, `invert`) |
| quadrature encoder | `sysbus` input into the timer's `CNT` |

Full table: [Renode adapter reference](../reference/renode.md#binding-kinds). To generate GPIO/PWM/ADC bindings from a CodeLayer Forge pin plan, use `embeddyn mechatronics bindings-from-pinplan`.
