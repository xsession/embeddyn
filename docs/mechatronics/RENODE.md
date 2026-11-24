# Renode firmware-in-the-loop

The firmware domain can run **real, unmodified firmware** in Renode while the
electrical and mechanical domains run in SPICE, Webots or the built-in models.
Embeddyn drives Renode through Renode's **External Control API**: a binary TCP
protocol served from inside Renode
(`src/Renode/Network/ExternalControl` in the
[`custom-cores` branch of xsession/renode](https://github.com/xsession/renode/tree/custom-cores)).

```
 co-simulation step k (t_k-1 -> t_k)
 ┌──────────────┐  ADC / GPIO / register writes   ┌─────────────────────────┐
 │  Embeddyn    │ ──────────────────────────────▶ │ Renode (virtual time)   │
 │ orchestrator │  RunFor(t_k - t_k-1)  [µs]      │  firmware.elf on STM32  │
 │ RenodeAdapter│ ◀────────────────────────────── │  timer/GPIO edge events │
 └──────────────┘  PWM duty, pin levels, regs     └─────────────────────────┘
```

Why this route rather than an in-process `pyrenode3` bridge:

* Renode stays its own process: a crash or hang in the emulator doesn't take
  the orchestrator down, and it works with Renode running in Docker, on
  another machine, or started by hand for debugging (GDB, Monitor).
* No .NET runtime inside the Python process.
* Virtual time only moves when Embeddyn sends `RunFor`, so runs are
  deterministic and the firmware sees exactly the co-simulation time.

## Protocol coverage

`embeddyn/mechatronics/adapters/renode_api.py` is a pure-Python client for the
full command set of that branch:

| Command     | Version | Used for |
|-------------|---------|----------|
| `RunFor`    | 0       | advance virtual time by one co-simulation step (µs) |
| `GetTime`   | 0       | align the adapter's time origin at start |
| `GetMachine`| 0       | resolve the machine name |
| `ADC`       | 0       | set/get channel values on peripherals implementing `IADC` |
| `GPIOPort`  | 1       | get/set pins, subscribe to edge events (timestamped in virtual µs) |
| `SystemBus` | 0       | read/write memory-mapped registers (byte…qword) |

GPIO edge events are only sent while `RunFor` executes; the client collects
them before the `RunFor` reply.

## Binding kinds

Bindings live in the YAML file named by `simulators.firmware.options.bindings`
(entries whose `endpoint.adapter` is not `renode` are ignored), or inline in
`options.inline_bindings`. `object` is the Renode element name
(e.g. `sysbus.timer1`). Every numeric binding accepts `scale` and `offset`
(`renode_value = signal * scale + offset` for inputs, the reverse direction for
outputs).

**Outputs** (Renode to co-simulation):

| `property`       | Required keys | Value |
|------------------|---------------|-------|
| `gpio_duty`      | `object`, `pin` | fraction of the step the line was high, from edge timestamps (`invert` optional) |
| `gpio_pulse_width` | `object`, `pin` (`timeout`) | width in seconds of the last complete high pulse, held until the next one ends (RC servo / ESC input); 0 after `timeout` seconds without a pulse |
| `gpio_state`     | `object`, `pin` | line level at the end of the step (0/1) |
| `sysbus`         | `address` (`width`, `signed`, `mask`, `float32`) | register/RAM value; `float32: true` decodes IEEE-754 |
| `register_ratio` | `address`, `denominator_address` (`denominator_offset`, `width`) | e.g. `CCR1 / (ARR + 1)` for a PWM duty without edge events |

**Inputs** (co-simulation to Renode, applied before each `RunFor`):

| `property` | Required keys | Effect |
|------------|---------------|--------|
| `adc`      | `object`, `channel` (`min`, `max`) | `IADC.SetADCValue`, rounded and clamped |
| `gpio`     | `object`, `pin` (`threshold`, `invert`) | drive an input pin |
| `sysbus`   | `address` (`width`, `float32`) | write a register or firmware variable (encoder counter, calibration value) |

### PWM resolution

Edge timestamps are taken from Renode's master time source, which moves in
sync-quantum steps. Set the quantum in the `.resc` to suit the PWM frequency,
e.g. `emulation SetGlobalQuantum "0.00001"` (10 µs, so 1 % duty resolution at
1 kHz). If the timer model doesn't toggle a line, use `register_ratio`.

### STM32 ADCs

In this Renode tree only `CAES_ADC` implements `IADC`. Other ADC models accept
samples only through the Monitor (`FeedSample`). Two patches in
`externals/patches/renode-infrastructure/` add it to the STM32 ADCs:

| Patch | Models | Value unit |
|-------|--------|------------|
| [`0001-STM32_ADC-implement-IADC.patch`](../../externals/patches/renode-infrastructure/0001-STM32_ADC-implement-IADC.patch) | `STM32_ADC` (F2/F4/F7) | raw conversion counts |
| [`0002-STM32_ADC_Common-implement-IADC.patch`](../../externals/patches/renode-infrastructure/0002-STM32_ADC_Common-implement-IADC.patch) | `STM32_ADC_Common`: `STM32F0_ADC`, `STM32F3_ADC`, `STM32G0_ADC`, `STM32WBA_ADC` (also used for H7) | microvolts (the `IADC` convention) |

The units differ because `STM32_ADC` models samples as counts while the
`STM32_ADC_Common` family converts millivolts with the configured resolution
and reference voltage. Pick the binding `scale` to match: for example
`scale: 1000000` to send volts to an `STM32G0_ADC`. Values apply to every later
conversion of that channel until changed.

Both patches are applied to the vendored `externals/renode/src/Infrastructure`
and by `tools/renode/build-custom-cores.sh`. Both are verified on a real build:
0001 by the closed-loop tests; 0002 on a minimal STM32G0 platform, where
1.65 V set over External Control converts to 2048 (12-bit, 3.3 V reference).
They still have to be committed to `xsession/renode-infrastructure` (the
`src/Infrastructure` submodule of the custom-cores branch).

## Renode 1.17 and the External Control protocols

Renode 1.17 replaced the External Control handshake protocol with framed
messages and added CAN and SPI. embeddyn speaks both:

* `api: legacy`: the custom-cores fork up to 1.16 (`renode_api.py`).
* `api: message`: Renode ≥ 1.17 (`renode_api_v2.py`).
* `api: auto` (default): chosen from `renode --version`.

The fork has been merged with v1.17.1 on local branches (not pushed yet), and
every test passes on both. The merge and all its decisions are recorded in
[RENODE_1_17_MERGE.md](RENODE_1_17_MERGE.md).

### CAN (Renode ≥ 1.17)

```yaml
options:
  script: renode/stm32l476-servo-can.resc     # bxCAN + CANExternalControlBus on one CAN hub
  can:
    bus: extcan
    signal: mcu.can                          # every received frame -> event {id, data}
    fields:                                  # decode into numeric signals
      - {signal: mcu.can_setpoint_deg, id: 0x101, offset: 0, format: "<h", scale: 0.01}
```

`RenodeAdapter.can_write(frame_id, data)` sends a frame. Like UART input it
arrives at Renode's next sync point and is a logged boundary input. In
`rc-servo-l476-can.yaml` the firmware sends status frame `0x101` every
100 ms, and frame `0x200` with `[1]` clears a latched fault (tested).

## Quadrature encoders

On Renode ≥ 1.17 the STM32 timer models encoder mode, which stops internal
counting. embeddyn feeds an encoder by writing the count into the timer's
`CNT` every step with an ordinary `sysbus` input binding, scaled with a
coupling (counts per radian). The count is exact at every sync point, and
there is no edge-rate limit as with GPIO toggling. The firmware reads CNT as
it would on hardware. See `bindings/renode-arm-joint.yaml`.

## UART consoles

```yaml
options:
  uarts:
    - {name: console, peripheral: sysbus.usart2, signal: mcu.console}   # port: 0 = pick a free one
signals:
  mcu.console: {domain: firmware, kind: event}
```

The fork's External Control API has no UART command, so each UART goes
through a Renode socket terminal instead. At launch the adapter adds
`emulation CreateServerSocketTerminal <port> "<name>" false; connector Connect
<peripheral> <name>` and then connects to the port.

* **Output:** after every `RunFor` the adapter drains what the firmware
  transmitted. Each complete line becomes an event on the UART's signal, stamped
  with the step's end time. On loopback the bytes are already in the socket
  when `RunFor` returns, and two runs give identical lines at identical times
  (test).
* **Input:** `RenodeAdapter.uart_write()` (from the Lab's serial monitor, or
  `POST /api/uart`) sends text to the UART. Renode delivers it asynchronously
  at its next sync point. That makes it a logged, non-deterministic boundary
  input, recorded as a `<signal>.tx` event, unlike the lock-step pin
  bindings.
* CAN and SPI hooks need Renode ≥ 1.17's External Control commands, which the
  custom-cores fork doesn't have yet (see the integration research).

In the RC-servo example, USART2 prints a telemetry line every 100 ms. `R`
clears a latched stall fault and `?` prints the calibration.

## Project configuration

See `examples/mechatronics/dc-motor-renode.yaml`:

```yaml
simulators:
  firmware:
    options:
      backend: renode
      launch: true                         # or false to attach to a running Renode
      script: renode/stm32-motor.resc      # creates the machine; must not `start`
      firmware: firmware/motor-speed-control.elf   # becomes $bin in the script
      machine: stm32
      port: 21234
      bindings: bindings/renode.yaml
      log: ../../.embeddyn/renode-dc-motor.log
```

Other options: `host`, `timeout`, `startup_timeout` (default 120 s, Renode
starts slowly), `executable` (or `simulators.firmware.executable`; default
`$RENODE_BIN`, then `renode` on `PATH`), `renode_args` (default
`--disable-gui --console --plain`), `server_name`, and `variables` (Monitor
variables set before the script, e.g. `{quantum: "0.0001"}`).

If the `.resc` fails (bad platform, missing ELF), Renode stays alive without
opening the server. The adapter watches Renode's output (the `log` file, or a
temporary one) and fails straight away with the Monitor's error message,
instead of waiting out `startup_timeout`.

With `launch: true` Embeddyn runs

```
renode --disable-gui --console --plain -e '$bin=@<firmware>; include @<script>; emulation CreateExternalControlServer "embeddyn" <port>'
```

then connects, retrying until `startup_timeout`, and terminates Renode at the
end of the run. Renode runs in its own process group, and the adapter stops
the whole group and waits until it is gone. The `renode` launcher script
starts .NET as a child process, so stopping only the script used to leave
Renode listening on the port. The next run (a tuning evaluation, say) then
connected to the dying instance and failed with "Can't start emulation while
disposing". To debug the firmware, start Renode yourself (with a GDB
server if you like), run the same two Monitor commands, and set `launch: false`.

Check a project before running it:

```bash
embeddyn mechatronics integration-validate examples/mechatronics/dc-motor-renode.yaml
embeddyn mechatronics run examples/mechatronics/dc-motor-renode.yaml --output out.csv
```

`--mock` still replaces Renode with the built-in PI controller.

## Firmware contract for the example

The example expects firmware for the STM32F4 platform
(`renode/stm32f4-motor.repl`) that:

1. reads ADC1 channel 0 (speed: counts = rad/s × 100, 12-bit),
2. runs the speed controller,
3. drives TIM1 CH1 (PA8) in PWM mode 1 with duty = controller output.

The [reference firmware](#reference-firmware) does exactly this.

## Testing without Renode

`tests/mechatronics/fake_renode.py` is a byte-compatible fake of the External
Control server with a small simulated firmware (ADC to proportional PWM with
timestamped edges, timer registers). `tests/mechatronics/test_renode_adapter.py`
covers the protocol, duty measurement, integer-µs time alignment, binding
validation, launch command and a closed-loop run of the example project.

## Validated against real Renode (2026-11-21)

Renode built from `custom-cores` (`6520932`, reported as `1.16.1+20261121git6520932b6`)
with `tools/renode/build-custom-cores.sh`, plus the reference firmware:

* Every External Control command used works against the real server:
  handshake, GetTime, RunFor, GetMachine, ADC (through the STM32_ADC patch),
  GPIOPort edge events from `timer1`, and SystemBus reads/writes.
* `tests/mechatronics/test_renode_real.py` passes (opt-in: `RENODE_BIN` set,
  firmware built). It covers the closed loop and changing the firmware
  set-point at run time through a `float32` sysbus binding. The closed-loop
  test also compares duty, current and speed every 10 ms against
  `tests/mechatronics/data/renode_dc_motor_golden.csv`; regenerate it after an
  intended change with `EMBEDDYN_UPDATE_GOLDEN=1`.
* `.github/workflows/renode-cosim.yml` runs the same tests in CI. The Renode
  build is cached per fork commit, patch set and build script.
* `embeddyn mechatronics run examples/mechatronics/dc-motor-renode.yaml`
  runs 0.25 s of simulated time in about 10 s of wall time, including
  Renode startup. With the same PI gains the speed matches the `--mock`
  controller to within 0.04 rad/s after the first 10 ms. Duty is lower than the mock's
  once the loop leaves saturation (up to 0.09 at 0.25 s); the firmware runs
  its PI once per 1 ms PWM period on a one-period-old ADC sample, while the
  mock updates every 0.5 ms step. Measured with the timer patch 0003 (before
  it, PWM dropouts during saturation cost about 0.5 rad/s):

  | t [s] | real firmware duty / ω [rad/s] | mock PI duty / ω [rad/s] |
  |------:|-------------------------------|---------------------------|
  | 0.010 | 0.988 / 0.36 | 0.980 / 0.40 |
  | 0.100 | 0.990 / 3.74 | 0.997 / 3.76 |
  | 0.200 | 0.884 / 6.16 | 0.942 / 6.17 |
  | 0.250 | 0.812 / 6.94 | 0.905 / 6.95 |

## Reference firmware

`examples/mechatronics/firmware/motor-speed-control/` is bare-metal C for
`arm-none-eabi-gcc` (no SDK). `make` writes
`examples/mechatronics/firmware/motor-speed-control.elf`.

* PI speed loop at 1 kHz, run from the TIM1 update interrupt; the core sleeps
  in `WFI` between periods. `make POLL=1` builds a variant that polls the
  update flag instead (about 10× more CPU to emulate). ADC1 ch0 is read
  without blocking (the conversion starts at the end of one period and is read
  at the start of the next). The output is TIM1 CH1 PWM.
* `motor_cal` (`0x20000000`: magic, `target_counts`, `kp`, `ki` as float32)
  can be changed over the system bus while running, which is the hook for
  tuning without rebuilding. `loop_count`, `last_adc` and `last_duty` are
  exposed for observation (addresses in the `.map`/ELF symbols).
* Enabling an interrupt that is already pending and then executing `WFI`
  needs `DSB; ISB` in between (as ARM recommends). Without it, Renode takes
  the interrupt *and* executes the `WFI` from the same translation block: the
  core sleeps at the handler entry with IRQ 25 active, so no later timer
  interrupt can wake it. Upstream Renode 1.16.1 (`3f5a91013`) and the
  custom-cores build behave the same, so this is not a fork regression.

## Renode model notes found while validating

* `STM32_Timer` uses ARR ticks per period (999 µs for ARR = 999); real
  hardware uses ARR + 1. That's a 0.1 % frequency error.
* Because the period is ARR ticks, a compare at `CCR = ARR` fires at the same
  instant as the update event. When it lands after the update it clears a
  PWM1 output for a **whole period**, so firmware that saturates its duty saw
  1–3 ms dropouts (56 of 500 steps in the example run). Patch
  [`0003-STM32_Timer-keep-PWM-active-for-CCR-at-or-beyond-ARR.patch`](../../externals/patches/renode-infrastructure/0003-STM32_Timer-keep-PWM-active-for-CCR-at-or-beyond-ARR.patch)
  keeps the output active for `CCR >= ARR` (compare flag and interrupt still
  fire). `test_renode_real.py` checks that saturated duty never drops out.
* Writing `CCR = 0` disables the compare channel rather than holding PWM low,
  so the firmware clamps CCR to at least 1 (0.1 % minimum duty).
* The branch's `platforms/cpus/st/stm32f4.repl` references `STM32_RNG`, which
  its own `src/Infrastructure` doesn't contain, and `stm32g0.repl` fails with
  `Could not find suitable constructor for type ...STM32SPI`. The example therefore ships a
  minimal `renode/stm32f4-motor.repl`.
* Speed (CPU time, standalone `emulation RunFor "0.3"`, 10 µs quantum):
  Renode startup costs 5–8 s; on top of that the WFI firmware needs under
  3 s for 0.3 s of virtual time and the polling variant about 30 s. Polling
  spends its time on MMIO reads, so the quantum matters less there (a 100 µs
  quantum is about 20 % faster). In co-simulation the per-step External
  Control round trips add to this. Set the quantum with
  `variables: {quantum: "0.0001"}` in the simulator options.

## Building the custom-cores branch

`tools/renode/build-custom-cores.sh <workdir>` clones the branch, installs a
user-local .NET 8 SDK, applies the patches in `externals/patches/` and builds.
It then writes `<workdir>/env.sh`, which sets `RENODE_BIN`; the adapter and
`integration-validate` both use that variable.

Out of the box the branch doesn't build. Its top level (`build.sh`,
`Renode.sln`, `Renode.csproj`) comes from a newer Renode than its submodules
(`src/Infrastructure` and `lib/*` still have the two-project `X.csproj` +
`X_NET.csproj` layout). The script works around this:

| Problem | Workaround in the script | Proper fix in the fork |
|---|---|---|
| `Renode.sln`/`Renode.csproj` reference mono-style `X.csproj` | repoint to the `X_NET.csproj` siblings | align submodule pins with the top level, or the top level with the submodules |
| unit-test projects target net462 | drop them from the solution | same |
| `Xwt.WPF`, `SystemCPlugin` don't build on Linux / with this Infrastructure | drop them from the solution | same |
| `output/properties.csproj` missing | copy `linux-properties.csproj` | older `build.sh` step |
| prebuilt DLLs (`protobuf-net`, FlatBuffers, …) missing | run `fetch_libraries.sh` | not needed once `lib/*` is updated |
| cores built into `Cores/bin/Release/linux-x64`, project embeds from `…/lib` | symlink | same |
| `System.CodeDom` 6.0.0 vs DLR's ≥ 8.0.0 | bump to 8.0.0 | commit the bump |
| `Antmicro.Renode.Exceptions.UnreachableException`, `Misc.TakeLast` don't exist | use the .NET 8 equivalents | commit |
| `RenodeWPF.dll` symlink makes `TypeManager` load `Renode.dll` twice | remove the symlink | same |
| `libllvm-disas.so` not copied (needed for tracing) | copy from renode-resources | same |

## Limits

* No checkpoint/restore. Renode's `Save`/`Load` exist, but External Control
  has no command to trigger them, and `Load` replaces the whole emulation,
  including the External Control server the adapter is connected to. Doing it
  needs either a Monitor connection next to External Control plus a reconnect
  after `Load`, or a new External Control command in the fork.
* Bus signals (UART/SPI/I²C/CAN frames) aren't bound yet; External Control
  has no command for them.
