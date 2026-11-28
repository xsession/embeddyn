# Import a board project

`embeddyn mechatronics import` turns a hardware project, a KiCad schematic plus its STM32CubeMX/STM32CubeIDE
firmware, into an emulated mechatronic system: the board's own firmware runs in Renode on a board description
generated from the netlist, its sensors are driven by a motion plant, and its LEDs and USB serial port show up as
instruments.

```bash
embeddyn mechatronics import my-board.zip -o .embeddyn/imports/my-board
embeddyn mechatronics run .embeddyn/imports/my-board/project.yaml -o run.csv
```

The source is a project folder or a `.zip` of one (archives are unpacked into the output folder; paths that would
leave it are refused). Set `GCC_ARM` (or `--gcc`) to a GNU Arm toolchain and `RENODE_ROOT` (or `--renode-root`)
to the Renode tree with the platform files; run with `RENODE_BIN` pointing at Renode.

## What it reads

| Input | Used for |
|---|---|
| KiCad netlist (`.net` / `.xml`, KiCad 5 or 6+; the newest export wins) or a KiCad 6+ schematic (exported with `kicad-cli`) | components, values, symbols and every pin-to-net connection |
| STM32CubeMX project (`.ioc`) | the MCU, which peripheral each pin carries (SPI1_SCK, GPIO_EXTI2, TIM3_CH1), enabled peripherals, HSE frequency, USB device class, FreeRTOS |
| STM32CubeIDE / SW4STM32 projects (`.project` + `.cproject`), or a `Makefile` project | the firmware build: target MCU, FPU and float ABI, defines, include paths, optimisation, linker script, source folders. The generated `Debug/makefile` is not used (it holds the author's absolute paths); a shipped `Debug/*.elf` is used only when building fails or with `--no-build` |
| firmware sources | which part drivers the firmware has (compared with the parts on the board) |

## How parts become emulated hardware

The part library (`embeddyn/mechatronics/importer/parts.yaml`, extend it with `--parts my-parts.yaml`) maps a
component, by MPN/value, symbol or footprint, to what it is:

* **MCU**: the Renode platform (STM32F405/F407, L476, L496, L053, U575/U585).
* **Devices** (sensors, memories): one or more Renode models with pin roles. For each device the importer follows
  the clock, data, chip-select and interrupt pins through the netlist to MCU pins, also through series resistors,
  ferrites and jumpers, and asks the `.ioc` (else the net name) which SPI/I2C instance the clock pin belongs to.
  A dual-interface part's bus comes from its mode pin (BMI088 `PS` to GND = SPI, a BMP280 `CSB` driven by a GPIO =
  SPI), an I2C address from its address pin. SPI devices sit behind a chip-select multiplexer in which several
  selected devices share the bus as on the board (each receives the bytes, MISO is the wired-AND), with a warning.
* **LEDs and RGB LEDs**: the MCU pin (through the series resistor), and whether it is active low (anode on a supply).
* **Regulators**: the output voltage from the feedback divider (resistor chains are followed), checked against the
  rail's name (`+3V3` -> 3.3 V).
* **Crystal**: the HSE frequency for the clock tree; **USB connector**: a USB host is attached to the MCU's OTG
  device and turns its CDC serial port into a UART.

Built-in device models: Bosch BMI088 (accelerometer and gyroscope dies, SPI/I2C, data-ready interrupts at the
configured ODR), BMP280 and Goertek SPL06-001 (raw values from the datasheet compensation, inverted), Winbond
W25N01GV SPI NAND, plus the existing Renode sensor models you add to the library.

`--substitute REF=PART_ID` emulates a part as another library part on the same pins, e.g. when the firmware was
written for a different sensor than the one on the schematic: `--substitute U300=goertek-spl06-001`.

## What it writes

| File | Content |
|---|---|
| `board.repl` | the board for Renode: platform, HSE, SPI multiplexers and chip selects, devices with their interrupt lines, LEDs, the USB serial host, and a sensor feed bridge |
| `board.resc` | loads the board and the firmware; `$renode_root` (path to the platform files) and `$quantum` can be overridden from the project's `variables` |
| `bindings.yaml` | the sensor feed (one float32 slot per sensor quantity component, SI units converted to the model's) and LED pin duty |
| `project.yaml` (+ `project-<firmware>.yaml` per extra firmware) | the mechatronic project: the board in Renode, a `motion` instance, one `LedBrightness` per LED, a serial monitor for the USB CDC port, a scope, LED instruments |
| `firmware/*.elf`, `build/` | the firmware built from source, with build logs |
| `report.md`, `import.json` | what was found: devices, buses, chip selects, interrupts, LEDs, rails, and findings |

## Findings

The report lists what deserves a look before trusting the emulation, for instance:

* the firmware has a driver for a part that is not on the schematic (and the reverse);
* a part sits on a bus the CubeMX project does not enable, so the firmware cannot use it;
* the crystal and CubeMX's `HSE_VALUE` disagree, or a regulator's divider misses its rail's nominal voltage;
* several chip selects active together (the firmware leaves two devices selected): the run log says so.

## Mechanics

The `motion` instance is `embeddyn.mechatronics.motion:PendulumMount`: the board hangs from a pivot and swings.
Its outputs are what the board's sensors measure in the board frame: specific force (`acc_*`, m/s^2), angular rate
(`gyro_*`, rad/s), pressure at the board's height (ISA) and temperature. Parameters: `length`, `damping`, `angle`
(initial, deg), `altitude`, `temperature`, `axes` (sensor axes relative to the board, e.g. `"y,-x,z"`), and a
`drive` input (angular acceleration). Replace it with a CSV replay or a MuJoCo body that publishes the same signals
(`motion.acc_x` ...) to put the board on your own mechanism.

## Limits

Sensor axes are assumed aligned with the board (set `axes` for a rotated part); FIFOs and self-tests of the IMU are
not modelled; the regulator is checked, not simulated (couple an ngspice instance for the power stage); parts the
library does not know are listed under "Not emulated".
