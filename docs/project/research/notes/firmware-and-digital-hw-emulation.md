# Firmware/MCU emulators and digital-hardware simulators for embeddyn co-simulation

Research date: 2026-11-23. GitHub metadata (stars, license SPDX, last push, latest release/tag) was pulled from the GitHub REST API on that date (`api.github.com/repos/<owner>/<repo>` and `/releases/latest` or `/tags`). Where a cited fact comes from an older source, it is flagged.

## Quick comparison matrix

| Candidate | License | Stars / latest release (2026-11-23) | Cores / targets | Peripheral depth relevant to gaps | External control / time | Lock-step fit |
|---|---|---|---|---|---|---|
| **Renode** (already used) | MIT-style (GitHub reports NOASSERTION) | 3180 stars; v1.17.1 released 2026-11-23; v1.17.0 2026-09-07; v1.16.0 2025-08-03 | Arm Cortex-M/A/R, RISC-V, Xtensa (LX7 groundwork), x86, SPARC, MSP430 | UART, CAN (MCAN, FlexCAN), SPI, I2C sensor models (IMU, magnetometer, pressure, humidity, current), RESD sensor-data feeds, STM32 qdec on reference board | C External Control API: `run_for`, quantum, ADC, GPIO, CAN send/receive, SPI callbacks, external bus peripheral, sysbus R/W; socket or in-process | Excellent: virtual time, ends at sync points |
| Renode + Verilator / DPI | MIT (renode-verilator-integration) | 29 stars, last push 2025-05 | HDL peripherals and CPU RTL | APB3, AXI4, AXI4-Lite, Wishbone, CFU (C++); AHB, APB3, AXI4(-Lite) (DPI) | Driven by Renode | Good, inherits Renode time |
| Renode + SystemC | Apache-2.0 (examples) | 16 stars, active (push 2026-11-16) | SystemC/TLM models, SystemCCPU | TLM sockets, IRQ, GPIO signals, DMA | TCP, timesync demo | Good |
| QEMU | GPL-2.0 (GitHub reports NOASSERTION) | 14073 stars; v11.1.3 stable, v11.2.0-rc1 | Very broad (Arm, RISC-V, x86, etc.); MCU boards limited | Weak MCU sensor/bus modelling out of the box | `-icount shift=N,sleep=off,rr=record/replay`; QMP | Medium: deterministic only in icount mode, no built-in "run for N ns" API documented on the pages read |
| xPack QEMU Arm | MIT (packaging) | 158 stars; v9.2.4-1 2025-10-09 | QEMU Arm for Cortex-M boards | As QEMU | As QEMU | Medium |
| Wokwi (cloud) + wokwi-cli | CLI MIT; simulator is a cloud service with paid tiers | wokwi-cli 74 stars, v0.29.1 2026-10-23 | AVR, RP2040, ESP32 family (per Wokwi) | Custom Chips API: GPIO, analog, timers, UART, I2C, SPI | Cloud only; `--timeout`; scenario scripts | Poor for lock-step (remote, metered) |
| avr8js / rp2040js | MIT | 859 / 536 stars; v0.21.1 2026-08-31 / v1.4.2 2026-11-14 | AVR (ATmega), RP2040 | Library-level peripherals | JS library, stepped by caller | Good in principle (JS) |
| Velxio | AGPLv3 + commercial dual license | n/a | AVR (avr8js), RP2040 (rp2040js), ESP32 & Pi 3 (QEMU) | Browser UI | Browser/Web Worker | Poor (UI-first, AGPL) |
| simavr | GPL-3.0 | 1804 stars; tag v1.8; active (push 2026-11-19) | AVR | AVR peripherals, VCD | C library, cycle-stepped | Good for AVR |
| Unicorn | GPL-2.0 | 9603 stars; 2.1.4 2025-09-09 | CPU only (QEMU-derived) | None (CPU only) | C/Python API | CPU-only, needs everything else |
| Qiling | GPL-2.0 | 6196 stars; v1.4.11 2026-09-06 | OS-level emulation on Unicorn, includes MCU mode | Limited | Python | Niche (security/fuzzing) |
| Zephyr native_sim | Apache-2.0 (Zephyr) | Zephyr project | Host-compiled Zephyr app | UART/PTY, SocketCAN, I2C/SPI/GPIO/ADC emulators | Virtual time decoupled from host, deterministic | Good but only Zephyr firmware, not the binary |
| Zephyr nrf52_bsim / BabbleSim | Apache-2.0 | Zephyr/BabbleSim | nRF52833 models | Radio, timers, GPIO, UART, RTC, TEMP... | Fully deterministic, Phy as separate process | Good for BLE multi-node |
| Verilator | LGPL-3.0 / Artistic-2.0 (GitHub NOASSERTION) | 4107 stars; tag v5.054 | Verilog/SystemVerilog to C++ | n/a | C++ model stepped by the host | Excellent (host owns time) |
| GHDL | GPL-2.0 | 2931 stars; v6.0.0 2026-03-07 | VHDL | n/a | VPI/VHPIDIRECT, cocotb | Good via cocotb |
| Icarus Verilog | GPL-2.0 | 3753 stars; v13_0 2026-03-02 | Verilog | n/a | VPI, cocotb | Good via cocotb (slow) |
| cocotb | BSD-3-Clause | 2584 stars; v2.1.0 2026-08-30 | Python testbench over Verilator/Icarus/GHDL/... | n/a | Python coroutines in sim time | Good (Python, same as orchestrator) |
| SystemC (Accellera) | Apache-2.0 | 695 stars; tag 3.0.3 | TLM-2.0 virtual platforms | n/a | Kernel stepped by `sc_start(t)` | Excellent |

## Renode: built-in peripherals, sensors, CAN/UART/encoder, extensions, External Control API, Robot, HDL/SystemC co-simulation

### Takeaway
Renode already covers most of embeddyn's gaps. It has bus-level CAN and SPI hooks in the External Control API, a library of I2C/SPI sensor models fed by RESD sample files, UART models, and maintained Verilator, DPI and SystemC co-simulation bridges. The biggest missing piece is a dedicated quadrature-encoder stimulus model: STM32 timer/qdec models exist on the reference board, but nothing generates encoder A/B signals on its own. embeddyn can drive A/B pins via GPIO set at each sync point, but only at the ~1 ms quantum resolution.

### Cited Findings
- Release cadence: v1.16.0 (2025-08-03) added MSP430 as the first interpreted ISA, initial x86 KVM, SystemCCPU model and a Cortex-M wrapper in SystemCPlugin, plus a "SystemC cosimulation" test — [Renode v1.16.0 release notes](https://github.com/renode/renode/releases/tag/v1.16.0)
- v1.17.0 (2026-09-07) added Cortex-M52, Cortex-A7, many Helium/MVE instructions, precise BusFaults, CPUWAIT/SYSRESETREQ signals, Lockup handling and semihosting calls — [Renode v1.17.0 release notes](https://github.com/renode/renode/releases/tag/v1.17.0)
- v1.17.1 (2026-11-23) added Ethos-U55/U85 NPU models, Xtensa LX7 decoding (groundwork for ESP32-S2/S3), "extensive support for the STM32H7 Renode Reference Platform", SiLabs SiXG301/EFR32xG2x, NXP S32K389, and instruction fetches from unmapped memory now raise BusFault instead of halting the CPU — [Renode v1.17.1 release notes](https://github.com/renode/renode/releases/tag/v1.17.1)
- A 1.16.1 entry dated 2026-02-16 made .NET the default runtime, with Mono available through a flag. This comes from a changelog mirror (secondary source) — [opensecura changelog mirror](https://opensecura.googlesource.com/3p/renode/renode/+/a9c9cecfade9876d487188704948f956e6df7e5d/CHANGELOG.rst)
- GitHub: 3180 stars, last push 2026-11-23 — GitHub API `repos/renode/renode`
- Sensor and peripheral test suites in `tests/peripherals` (names point to models):
  - IMU/accelerometer/magnetometer: LSM6DSO_IMU, LSM9DS1, LSM303, LSM330, LIS2DS12, LIS2DW12, MC3635, IIS2MDC, AK09918
  - Pressure/humidity/temperature: BME280, BMP180, LPS25HB, ICP_101xx, HS3001, SHT45
  - Current/power monitor: INA228
  - Light: VEML7700
  - ADC chip: AD4011
  - FRAM: MB85RC1MT
  - CAN: MCAN, NXP_FlexCAN, IMX8MP_Flexcan, UT32_CAN
  - UART: STM32_UART, ARM_SBSA_UART
  - Also SPIMultiplexer, ST7789 display, a PythonPeripheral test, and many `*-samples.csv` sensor data files
  - No encoder test was found
  
  — [Renode tests/peripherals (mirror)](https://opensecura.googlesource.com/3p/renode/renode/+/refs/heads/master/tests/peripherals)
- The STM32H7 Renode Reference Board, a Zephyr board, has:
  - `st,stm32-qdec` quadrature decoders (6 instances) and two FDCAN controllers with GPIO-controlled transceivers
  - On-board I2C sensors: LSM6DSO IMU, IIS2MDC magnetometer, LPS25HB pressure, TMP108 temperature, VEML7700 light
  - A rotary potentiometer on ADC1, buttons and LEDs
  - It is presented as "a digital twin of the board is represented in the Renode simulation framework"

  — [Zephyr docs: STM32H7 Renode Reference Platform](https://docs.zephyrproject.org/latest/boards/antmicro/stm32h7_renode_reference_board/doc/index.html)
- RESD (Renode Sensor Data) is a binary format with typed channels, so one file can feed several sensors such as an IMU. It is loaded with the `FeedSamplesFromRESD` Monitor command — [Renode RESD docs](https://renode.readthedocs.io/en/latest/basic/resd.html); [Zephyr blog: synchronized multi-sensor data in Renode](https://zephyrproject.org/providing-synchronized-multi-sensor-data-in-renode/)
- External Control API (C, `tools/external_control_client`, `renode_api.h`, `librenode_api.a`):
  - Transports: TCP socket (`renode_connect`) or in-process via librenode (`renode_connect_in_process`)
  - Emulation and time: `renode_run_for`, `renode_start`, `renode_pause`, `renode_set_quantum`, current virtual time, a per-quantum callback, and running `.resc` scripts
  - ADC get/set channel; GPIO get/set plus state-change callbacks
  - **CAN**: get bus, send messages (payload up to 64 bytes), receive callback
  - **SPI**: requires `SPI.ExternalControlSPIPeripheral`, with transmit (MOSI to MISO) and finish callbacks
  - **External Bus peripheral**: `Bus.ExternalControlBusPeripheral`, with sysbus access callbacks per access type and width
  - sysbus reads and writes in all widths; custom Monitor command callbacks
  
  — [Renode docs: External control client](https://renode.readthedocs.io/en/latest/api-description/external-control.html)
- Determinism semantics in that API:
  - `run_for` ends at a synchronization point, and callbacks complete before it returns
  - A GPIO input change takes effect at the next sync point; GPIO callbacks fire at the sync point, except in SynchronizedTimers mode
  - CAN frames reach other controllers at the next sync point (SynchronizedIO, the default) or before `send` returns (SynchronizedTimers)
  - An ADC value takes effect before `set` returns; SPI callbacks fire only during `run_for`
  - Only one thread may call `run_for`
  - Nanosecond precision holds for about 93 days of virtual time
  
  — [Renode docs: External control client](https://renode.readthedocs.io/en/latest/api-description/external-control.html)
- An older snapshot of the client listed only connect/disconnect as implemented, so the API has grown over time — [opensecura external_control_client README](https://opensecura.googlesource.com/3p/renode/renode/+show/07559ce790d247007f28da6fa6d46158bef03f14/tools/external_control_client/README.md)
- pyrenode3 is a Python library that drives Renode through Python.NET (in-process C# interop). Apache-2.0, 35 stars, last push 2026-10-09 — [Antmicro blog 2023-11 on pyrenode3](https://antmicro.com/blog/2023/11/python-driven-automation-and-scripting-in-renode-with-pyrenode3/); GitHub API `repos/antmicro/pyrenode3`
- HDL co-simulation has two methods:
  - DPI-based SystemVerilog over TCP sockets, for Verilator, Questa or VCS; covers AHB, APB3, AXI4 and AXI4-Lite
  - Verilator-only C++ method that compiles the HDL model to a dynamic library loaded by Renode at runtime; covers APB3, AXI4, AXI4-Lite, Wishbone and CFU
  - Models prefixed `V` use sockets and those prefixed `libV` use library calls
  - Examples live in `renode-verilator-integration` and `renode-dpi-examples`
  
  — [Renode docs: Co-simulating with an HDL simulator](https://renode.readthedocs.io/en/latest/advanced/co-simulating-with-an-hdl-simulator.html)
- HDL peripherals can initiate Wishbone and AXI bus transactions (bus mastering, e.g. DMA). FastVDMA on PolarFire SoC is the 2021 example (older) — [Antmicro blog 2021-06](https://antmicro.com/blog/2021/06/advanced-co-simulation-with-renode-and-verilator)
- Renode also supports co-simulating CPU RTL — [RISC-V blog: CPU RTL co-simulation in Renode](https://riscv.org/blog/cpu-rtl-co-simulation-in-renode/); and complex DPI-based HDL co-simulation (2025-04) — [Antmicro blog 2025-04](https://antmicro.com/blog/2025/04/complex-dpi-based-hdl-co-simulation-in-renode/)
- SystemC co-simulation (2024-07):
  - A generic `SystemCPeripheral` in Renode talks over TCP to a `renode_bridge` SystemC module, so Renode looks like a module with standard TLM sockets
  - Demos cover interrupts, DMA with a GPIO "bus free" signal, multiple peripherals, timesync, and the loosely-timed example
  - The post does not describe the time-sync mechanism or quantum
  
  — [Antmicro blog: SystemC co-simulation in Renode](https://antmicro.com/blog/2024/07/systemc-co-simulation-in-renode/); examples repo Apache-2.0, 16 stars, last push 2026-11-16 (GitHub API `repos/antmicro/renode-systemc-examples`)

### Inferences
- For UART/SPI/I2C/CAN frames, the cheapest route is to extend embeddyn's existing External Control binding:
  - CAN send/receive and the SPI ExternalControl peripheral are already in the C API
  - UART and I2C have no dedicated external-control hooks in the docs read. They can be bridged via a custom Monitor command, a Python/C# peripheral, or the ExternalControlBusPeripheral (sysbus callbacks)
- For sensor ICs, Renode's native models plus RESD (or setting sample values each sync point) let embeddyn feed physics-derived IMU, temperature and current values into realistic register-level I2C sensors without writing models. INA228 covers current sense; LSM6DSO, LSM9DS1 and LIS2DW12 cover IMUs.
- Quadrature encoder: two options.
  - (a) Toggle GPIO A/B at sync points. This is limited to quantum resolution, about 1 kHz edge rate at 1 ms, too slow for real encoders.
  - (b) Write a small C# or Python peripheral that turns a commanded position/velocity into timer counter updates or edges inside Renode's virtual time. This is the better path.
- Fault injection is natural in Renode (sysbus writes, GPIO forcing, CPU fault handling improved in 1.17.x). No dedicated fault-injection framework was found in this session.
- The xsession custom-cores fork should be rebased onto v1.17.x to get the BusFault-on-unmapped-fetch fix, STM32H7 reference platform models and newer External Control features. This is an inference; check whether the fork diverges.

### Gaps
- No primary doc page was read for Renode's Python peripheral (`PythonPeripheral`) or C# peripheral extension API details. Only the existence of a `PythonPeripheral.robot` test is confirmed.
- The exact license text was not verified. GitHub reports NOASSERTION; Renode is commonly described as MIT, but that was not confirmed this session.
- No Renode quadrature-encoder stimulus model was found. Whether the STM32 qdec timer model accepts external A/B edge input is unverified.
- No UART- or I2C-specific External Control hooks were found in the docs. RobotFramework keyword capabilities (UART testers, `Wait For Line On Uart`, etc.) were not fetched this session.

## QEMU, Wokwi, simavr, Unicorn/Qiling, Zephyr native_sim/BabbleSim, Velxio/avr8js/rp2040js

### Takeaway
Among the MCU emulators surveyed, only Zephyr native_sim/BabbleSim and library-style emulators (simavr, avr8js/rp2040js, Unicorn) give the host full ownership of virtual time. QEMU is deterministic only with `-icount` (plus record/replay) and has weak MCU peripheral models. Wokwi's simulator runs in a metered cloud, so it is unsuitable for 1 ms lock-step.

### Cited Findings
- QEMU: 14073 stars; latest stable tag v11.1.3, with v11.2.0-rc1 in progress — GitHub API `repos/qemu/qemu/tags`
- QEMU icount mode:
  - Counts executed instructions; it is not cycle-accurate
  - System emulation only, and incompatible with multi-threaded TCG
  - Provides a degree of determinism and underpins record/replay
  - QEMU_CLOCK_VIRTUAL is derived from the instruction count
  - MMIO accesses recompile a single-instruction block, keeping the clock exact at device accesses
  
  — [QEMU docs: TCG Instruction Counting](https://www.qemu.org/docs/master/devel/tcg-icount.html)
- `-icount shift=N`: "one instruction every 2^N ns of virtual time"; `align=on` syncs to the host clock; `sleep=off` stops virtual time advancing at default speed while the CPU sleeps; `rr` plus `rrfile` gives deterministic record/replay. `-rtc clock=vm` uses the virtual clock — [QEMU docs: invocation](https://www.qemu.org/docs/master/system/invocation.html)
- xPack QEMU Arm (Cortex-M-oriented packaging): MIT, 158 stars, v9.2.4-1 released 2025-10-09 — GitHub API `repos/xpack-dev-tools/qemu-arm-xpack`
- Wokwi CI and wokwi-cli:
  - The CLI runs locally, but the simulation runs on Wokwi's cloud servers; on-premise deployment is by arrangement
  - Monthly simulation-minute limits per rolling 30 days: Free 50, Hobby 200, Hobby+ 500, Pro 2000
  - Per-session wall-clock caps: 5, 5, 10 and 50 min for those plans
  - Automation scenarios can press buttons, change sensor states and check serial output; time control documented is `--timeout`
  
  — [Wokwi CI getting started](https://docs.wokwi.com/wokwi-ci/getting-started)
- wokwi-cli: MIT, 74 stars, v0.29.1 2026-10-23 — GitHub API `repos/wokwi/wokwi-cli`
- Wokwi Custom Chips API (beta): written in C or any WASM language (Rust, AssemblyScript), with experimental Verilog. Covers GPIO, analog, timers, UART, I2C, SPI, attributes and a framebuffer. Examples include LM75A, DS18B20, PCA9685, SSD1306 and EEPROMs — [Wokwi Chips API](https://docs.wokwi.com/chips-api/getting-started)
- avr8js: MIT, 859 stars, v0.21.1 2026-08-31. rp2040js: MIT, 536 stars, v1.4.2 2026-11-14 — GitHub API
- Velxio:
  - Browser emulator using avr8js (Uno/Nano/Mega, ATtiny85) and rp2040js (Pico/Pico W) in a Web Worker, plus QEMU for ESP32 and Raspberry Pi 3; 19 boards in total
  - Dual-licensed: AGPLv3 for personal/educational/OSS use, commercial for proprietary or SaaS
  - Self-hostable via Docker; not a Wokwi fork
  
  — [CNX Software 2026-04-04](https://www.cnx-software.com/2026/04/04/velxio-open-source-self-hosted-arduino-raspberry-pi-and-esp32-simulator/); [Espressif dev blog 2026-07](https://developer.espressif.com/blog/2026/07/velxio-browser-based-esp32-simulation/)
- simavr: GPL-3.0, 1804 stars, last tag v1.8, still pushed (2026-11-19) — GitHub API `repos/buserror/simavr`
- Unicorn: GPL-2.0, 9603 stars, 2.1.4 (2025-09-09). Qiling: GPL-2.0, 6196 stars, v1.4.11 (2026-09-06) — GitHub API
- Zephyr native_sim:
  - Simulated time is decoupled from host time and runs as fast as possible; `--rt`, `--rt-ratio` and `--rt-drift` optionally lock it to real time
  - Runs are normally deterministic, with `--seed` for the entropy device
  - Peripherals: PTY UART, SocketCAN (`zcan0`) and CAN loopback, i2c-dev and I2C emulation, SPI, GPIO and ADC emulation controllers, plus SDL display, flash and EEPROM
  
  — [Zephyr docs: native_sim](https://docs.zephyrproject.org/latest/boards/native/native_sim/doc/index.html)
- nrf52_bsim:
  - Models the nRF52833: radio, timers, AAR, AES, CLOCK, EGU, GPIO/GPIOTE, NVMC, PPI, RNG, RTC, TEMP, UART/UARTE, UICR
  - BabbleSim is "fully deterministic by design", with seeded randomness; the 2G4 Phy runs as a separate process coordinating devices
  - Simulated time is decoupled from real time, with an optional handbrake device to pace it
  
  — [Zephyr docs: nrf52_bsim](https://docs.zephyrproject.org/latest/boards/native/nrf_bsim/doc/nrf52_bsim.html)

### Inferences
- QEMU has nothing like Renode's `run_for(quantum)` client library, so lock-step would need one of:
  - (a) icount plus a custom device or plugin that blocks at time boundaries
  - (b) the qtest accelerator's clock-step protocol, which is not verified this session
  - (c) GDB-stub stepping
  
  All are more work than Renode, which already does this. QEMU is worth it mainly for Linux-class (Cortex-A) or ESP32 targets Renode lacks.
- Wokwi is a good free/cheap UX for hobby firmware tests but is cloud-metered. Its simulator core is not open source (only the CLI, avr8js and rp2040js are MIT), so it cannot be embedded in a deterministic local lock-step loop.
- avr8js/rp2040js are MIT and stepped by the caller (cycle-driven JS loops), so they could be embedded via Node. That only adds value if embeddyn wants AVR/RP2040 support, and Renode now covers neither natively; this is not verified.
- simavr is the most mature embeddable AVR core in C with a caller-driven cycle loop. GPL-3.0 linking affects distribution if embeddyn links it in-process; running it as a separate process avoids that.
- native_sim/bsim fit Zephyr-based products: firmware compiles to a host binary and time is deterministic. They do not emulate the real firmware binary, so they complement rather than replace Renode.
- Unicorn/Qiling are CPU-only or security-oriented. They are not useful for whole-system co-simulation without writing all peripherals.

### Gaps
- QEMU qtest/clock_step and QMP-based time stepping were not verified this session.
- Wokwi's simulator-engine license was not stated on the pages read; that it is closed is an inference from the cloud-only model.
- Velxio GitHub stars and repo activity were not retrieved.
- PlatformIO "sim" was not researched: no time left, and it appears to wrap other simulators (unverified).
- simavr's peripheral list and API docs were not fetched.
- I found no source on whether native_sim exposes an external stepping API (e.g. pausing at time T for an external orchestrator). BabbleSim's Phy protocol suggests a device could be written to do this, but this is unverified.

## Digital hardware: Verilator, GHDL, Icarus, cocotb, SystemC/TLM and bridges

### Takeaway
All of these are open source and actively maintained in 2026. Verilator and SystemC give the host full control of time. GHDL and Icarus are best driven through cocotb, which is Python like embeddyn's orchestrator. For FPGA/CPLD parts attached to the MCU bus, Renode's own Verilator/DPI/SystemC bridges are the lowest-friction path. For standalone FPGA logic, such as a motor-control PWM/encoder FPGA, cocotb or a direct Verilator C++ model stepped by embeddyn is the cleanest.

### Cited Findings
- Verilator: 4107 stars, latest tag v5.054, last push 2026-11-22 — GitHub API `repos/verilator/verilator`
- GHDL: GPL-2.0, 2931 stars, v6.0.0 (2026-03-07) — GitHub API `repos/ghdl/ghdl`
- Icarus Verilog: GPL-2.0, 3753 stars, v13_0 (2026-03-02) — GitHub API `repos/steveicarus/iverilog`
- cocotb: BSD-3-Clause, 2584 stars, v2.1.0 (2026-08-30) — GitHub API `repos/cocotb/cocotb`
- Accellera SystemC: Apache-2.0, 695 stars, latest tag 3.0.3 — GitHub API `repos/accellera-official/systemc`
- Renode bridges to HDL: Verilator C++ library (APB3, AXI4, AXI4-Lite, Wishbone, CFU) and DPI over sockets for Verilator, Questa or VCS (AHB, APB3, AXI4, AXI4-Lite) — [Renode docs: Co-simulating with an HDL simulator](https://renode.readthedocs.io/en/latest/advanced/co-simulating-with-an-hdl-simulator.html)
- Renode to SystemC over TCP with TLM sockets; the SystemCCPU and Cortex-M wrapper arrived in Renode 1.16 — [Antmicro blog 2024-07](https://antmicro.com/blog/2024/07/systemc-co-simulation-in-renode/); [Renode v1.16.0 release notes](https://github.com/renode/renode/releases/tag/v1.16.0)
- SystemC TLM can be integrated into FMI 3.0 co-simulations; 2025 papers cover open-source approaches — [arXiv 2508.20223](https://arxiv.org/html/2508.20223v1); [arXiv 2508.19665](https://arxiv.org/html/2508.19665v1)

### Inferences
- Verilator's generated C++ model is advanced by the host calling `eval()` and setting time, and SystemC by `sc_start(dt)`. Both are naturally externally stepped and deterministic, so wrapping them as an embeddyn component stepped at 1 ms (with internal sub-stepping) is straightforward. This mechanism comes from general Verilator/SystemC knowledge, not fetched docs.
- Event-driven simulators (GHDL, Icarus) need VPI/VHPI callbacks to pause at time boundaries. cocotb provides this from Python (`await Timer(...)`), but running cocotb as a slave of an external orchestrator inverts its usual control flow. That needs either a socket bridge inside a cocotb coroutine or running the embeddyn step inside cocotb. This is an inference.
- SystemC plus FMI 3.0 is relevant if embeddyn adopts FMI for other domains (mechanics, electronics).

### Gaps
- Verilator's exact license SPDX was not confirmed this session (GitHub reports NOASSERTION; Verilator is commonly LGPL-3.0/Artistic-2.0).
- No Renode-cocotb bridge was found; I did not search specifically.
- Performance numbers for Renode-Verilator co-simulation were not collected.

## Deterministic, externally stepped time vs. free-running, and recommendation for embeddyn

### Takeaway
- **Externally stepped and deterministic:**
  - Renode (`run_for`, quantum, sync points)
  - SystemC (`sc_start`) and Verilator (`eval`), by design
  - simavr, avr8js/rp2040js and Unicorn, as libraries where the host runs cycles
  - BabbleSim/native_sim (deterministic virtual time, with stepping via the Phy/device protocol)
- **Deterministic only in a special mode:** QEMU (`-icount` with `sleep=off`, `rr`)
- **Free-running or remote:** Wokwi cloud, Velxio browser UI, QEMU without icount

**Recommendation:** stay on Renode and deepen it first.
1. Rebase the fork on v1.17.x.
2. Add embeddyn bindings for CAN (already in the External Control API) and for the SPI and ExternalControlBus peripherals.
3. Use Renode's I2C sensor models (LSM6DSO, INA228, etc.) fed from embeddyn physics each step.
4. Write a Renode C# or Python quadrature-encoder peripheral.
5. Add Verilator via Renode's C++ co-simulation for bus-attached FPGA logic.

Second candidates are a standalone Verilator/cocotb component for non-bus FPGA logic, and Zephyr native_sim/bsim for fast host-built firmware runs. Consider QEMU only for CPU families Renode cannot cover.

### Cited Findings
- Renode `run_for` ends at sync points, and the quantum sets synchronization granularity — [Renode External Control docs](https://renode.readthedocs.io/en/latest/api-description/external-control.html)
- QEMU icount determinism and rr — [QEMU TCG icount](https://www.qemu.org/docs/master/devel/tcg-icount.html); [QEMU invocation](https://www.qemu.org/docs/master/system/invocation.html)
- native_sim time is decoupled from host time and deterministic; BabbleSim is "fully deterministic by design" — [native_sim](https://docs.zephyrproject.org/latest/boards/native/native_sim/doc/index.html); [nrf52_bsim](https://docs.zephyrproject.org/latest/boards/native/nrf_bsim/doc/nrf52_bsim.html)
- Wokwi simulations run in the cloud with metered minutes — [Wokwi CI](https://docs.wokwi.com/wokwi-ci/getting-started)

### Inferences
- Integration effort ranking (lowest first):
  1. Renode CAN/SPI/bus bindings, which are already in the API
  2. Renode sensor models plus RESD or value setting
  3. A Renode encoder peripheral
  4. Renode-Verilator
  5. Standalone Verilator/SystemC component
  6. cocotb bridge
  7. native_sim/bsim
  8. QEMU lock-step
  9. Wokwi, which is not viable for lock-step
- License risk: GPL tools (simavr, GHDL, Icarus, Unicorn, QEMU) should run as separate processes to avoid copyleft on embeddyn. Velxio's AGPL is the most restrictive. Renode, cocotb, SystemC, avr8js and rp2040js are permissive.
- The CPU family gap is largely covered by Renode: Arm M/A/R, RISC-V, Xtensa (ESP32-S3 emerging in 1.17.1), MSP430, x86. AVR and RP2040 are the main families that may need avr8js/rp2040js/simavr. RP2040 support in Renode is not verified.

### Gaps
- Renode AVR and RP2040 support was not verified this session.
- No benchmark data was found comparing step overhead (ms of host time per 1 ms macro step) across tools.
