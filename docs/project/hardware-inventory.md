# Bench hardware inventory and emulation status

Inventory of the development boards on the bench (from 33 photos taken 2026-11-25), every chip that matters for
emulation, and what embeddyn / the Renode fork (`renode-merge`, v1.17.1 + custom cores) already provides for it.

Legend for **Status**: **done** = platform + peripherals + tested profile in `embeddyn/agents/mcu_emu/profiles`;
**base** = a Renode platform or model exists but has not been checked against this board / has no profile;
**near** = a sibling part exists that can be derived from; **new** = nothing yet; **n/a** = not an emulation target
(debug-probe chip, analog/power part).

"Seen" means the part number was read from the photo; "doc" means it comes from the board's user manual because
the chip is on a side that was not photographed or its marking is unreadable.

## Boards

| # | Board | Main MCU / SoC | Status of the MCU |
|---|---|---|---|
| 1 | WS2812B-64 8×8 RGB LED matrix | — (64 × WS2812B) | new (LED model) |
| 2 | nRF52840 SuperMini (Pro Micro form factor) | nRF52840-QIAA (seen), 32 MHz xtal | done (`nrf52840` profile, board `nordic/nrf52840_supermini.repl`; 24/25 nrfx samples) |
| 3 | Raspberry Pi Pico | RP2040 B2 (seen) | done (`rp2040` profile) |
| 4 | NUCLEO-L476RG (MB1136 rev C) | STM32L476RGT6 (seen) | done (`stm32l476` profile) |
| 5 | X-NUCLEO-IKS01A2 sensor shield | — (sensors only) | see sensors |
| 6 | Renesas SK-S7G2 (SK-S7G2-REV3.1) | R7FS7G27H3A01CFC Synergy S7G2, Cortex-M4 240 MHz (doc) | near (RA6M5 / RA8M1 platforms) |
| 7 | LAUNCHXL-F28069M (InstaSPIN-MOTION) | TMS320F28069M | done (`tms320f28069` profile) |
| 8 | Renesas YRPBRL78G13 | R5F100LEA RL78/G13 (seen) | new — RL78 core does not exist in tlib |
| 9 | SAMD10 Xplained Mini | ATSAMD10D14AM (seen "SAMD10E…") | near (`atsamd21j17d`) |
| 10 | NodeMCU-32S | ESP-WROOM-32 (ESP32-D0WDQ6, dual Xtensa LX6) | new — only ESP32-C3/C6/H2 (RISC-V) exist |
| 11 | LP-MSPM0G3507 | MSPM0G3507 | done (`mspm0g3507` profile) |
| 12 | EK-LM3S2965 CAN evaluation board | LM3S2965 (seen), Cortex-M3 50 MHz | near (`ti/tm4c123gh6pm.repl`, the Stellaris successor) |
| 13 | CH32V003F4P6 board (QSZNTEC) | CH32V003F4P6 (seen), 24 MHz xtal | new — the existing `wch/ch32v003.repl` is a placeholder (SiFive UART/GPIO, PLIC instead of PFIC) |
| 14 | STM32L496G-DISCO (MB1261 B-01) | STM32L496AGI6 (seen) | done (`stm32l496` profile, board `st/stm32l496g_disco.repl`; STM32CubeL4 examples 17 pass / 18 skipped, no known gaps: LCD, camera, audio, USB OTG still to model) |
| 15 | Arduino UNO Q (ABX00162) | Qualcomm QRB2210 (4× Cortex-A53, Linux) + STM32U585AII6 (seen) | U585: done (`stm32u585` profile; STM32CubeU5 examples on NUCLEO-U575ZI-Q 88 pass / 59 skipped, no known gaps, B-U585I-IOT02A 4 pass; UNO Q pin map still to add); QRB2210: new, Linux-class SoC |
| 16 | STM32L0538-DISCO (MB1143 B-01) | STM32L053C8T6 (seen) | done (`stm32l053` profile, board `st/stm32l0538_disco.repl`; STM32CubeL0 examples 145 pass / 19 skipped, no known gaps, over this and NUCLEO-L053R8; e-paper still to model) |
| 17 | LPCXpresso LPC812 rev A (with LPC-Link) | LPC812M101JDH20 (doc; marking under glare) | near (`nxp/lpc845.repl`) |
| 18 | FRDM-KL05Z | MKL05Z32VFM4 (seen "M05…") | done (`mkl05z32` profile, board `nxp/frdm_kl05z.repl`; Mbed OS 5.15 KL05Z HAL test programs 11 pass; TSI slider still to model) |

## Secondary MCUs on the boards

| Chip | Board | Role | Status |
|---|---|---|---|
| STM32L152CCT6 (seen) | STM32L496G-DISCO | MFX: multi-function expander (IO expander, IDD measurement) over I²C | near (`stm32l151.repl`) — modelled as an I²C device, not as a CPU |
| STM32L152C6T6 (seen) | STM32L0538-DISCO | IDD measurement MCU | near — I²C device model is enough |
| STM32F103CBT6 (seen) | NUCLEO-L476RG, L496G-DISCO, L0538-DISCO | ST-LINK/V2-1 probe | n/a |
| R5F56216BDFP RX621 (seen) | SK-S7G2 | J-Link OB probe | n/a |
| µPD78F0730 (seen) | YRPBRL78G13 | USB–UART / OCD bridge | n/a |
| ATmega32U4 (seen) | SAMD10 Xplained Mini | mEDBG probe | n/a (platform exists anyway) |
| FT2232D + LC4032V CPLD + 93C46 (seen) | EK-LM3S2965 | USB JTAG/SWD + UART probe | n/a |
| LPC3154 (seen) | LPCXpresso LPC812 | LPC-Link probe | n/a |
| MK20DX128VFM5 (seen "M20…") | FRDM-KL05Z | OpenSDA probe | n/a |
| CP2102-class USB-UART (seen) | NodeMCU-32S | USB–UART | n/a |
| XDS100v2 (FT2232H) / XDS110 | F28069M / MSPM0G3507 LaunchPads | debug probes | n/a |

## Sensors

| Part | Board | Bus / address | Status |
|---|---|---|---|
| LSM6DSL 6-axis IMU (U2) | X-NUCLEO-IKS01A2 | I²C 0xD6 / 0xD4 (8-bit) | base (`Sensors/LSM6DSL.cs`) |
| LSM303AGR accel + mag (U1) | X-NUCLEO-IKS01A2 | I²C acc 0x32, mag 0x3C | base (`LSM303AGR_Accelerometer.cs`, `_Magnetic.cs`) |
| HTS221 humidity + temperature (U3) | X-NUCLEO-IKS01A2 | I²C 0xBE | new |
| LPS22HB pressure (U4) | X-NUCLEO-IKS01A2 | I²C 0xBA / 0xB8 | base (`LPS22HB.cs`) |
| MMA8451Q 3-axis accelerometer (U7, seen "MMA 451") | FRDM-KL05Z | I²C 0x1D | done (`Sensors.MMA8451Q`: real register map, 2/4/8 g, fast read) |
| MP34DT01 PDM MEMS microphones ×2 (seen) | STM32L496G-DISCO | DFSDM / SAI PDM | new |
| Capacitive touch slider (TSI electrodes) | FRDM-KL05Z | TSI peripheral | new (part of the KL05 TSI model) |
| Touch-sensing linear slider (TSC electrodes) | STM32L0538-DISCO | TSC peripheral | done (STM32_TSC; TouchSensing_Linear passes) |
| Capacitive touch panel controller (FT6x06-class, on LCD flex) | STM32L496G-DISCO | I²C | new |
| Resistive/capacitive touch controller (on LCD flex) | SK-S7G2 | I²C | new |
| Trim potentiometer (10 k, seen "WR10K") | YRPBRL78G13 | ADC input | covered by the ADC + signal source |
| Trim potentiometer | LPCXpresso LPC812 | ADC / comparator input | covered by ACMP + signal source |
| Light sensor + thermistor (doc) | LP-MSPM0G3507 | ADC | covered by the MSPM0 ADC + signal source |

## Displays and light outputs

| Part | Board | Controller / interface | Status |
|---|---|---|---|
| 2.4″ QVGA TFT 240×320 (FPC "BT024K5QV50T", seen) | SK-S7G2 | ILI9341-class, SPI | base (`Video/ILI9341.cs`) |
| 1.54″ 240×240 TFT (doc) | STM32L496G-DISCO | ST7789H2, 8080 parallel over FMC | base (`Video/ST7789.cs`, needs FMC bus binding) |
| 2.04″ 172×72 e-paper (GDE021A1, doc) | STM32L0538-DISCO | SSD1607-class, SPI | new |
| 128×96 OLED, 16 grey levels (RIT P14201, doc) | EK-LM3S2965 | SSD1329, SPI | new (SSD1327 is a close sibling) |
| 8×8 WS2812B RGB matrix | WS2812B-64 | one-wire 800 kHz NRZ | new |
| 8×13 LED matrix (charlieplexed, seen) | Arduino UNO Q | GPIO from STM32U585 | new |
| RGB / user LEDs | most boards | GPIO | covered by the GPIO LED model |

## Communication modules and transceivers

| Part | Board | Status |
|---|---|---|
| ESP-WROOM-32 Wi-Fi + BT module | NodeMCU-32S | new (with the ESP32 core) |
| nRF52840 BLE / 802.15.4 radio | nRF52840 SuperMini | base (Renode `RADIO` model, wireless medium) |
| WCBN3536A Wi-Fi 5 + BT 5.1 module (seen) | Arduino UNO Q | new — Linux side only |
| CAN transceivers (CANL/CANH header) | LAUNCHXL-F28069M | done (eCAN on F28069) |
| CAN0/CAN1 (on-chip, transceiver on the EK) | EK-LM3S2965 | new (with LM3S2965) |
| Ethernet / USB HS / CAN / RS-485 (doc) | SK-S7G2 | new (with S7G2) |
| CS42L51 audio codec (seen) | STM32L496G-DISCO | new (I²C control + SAI) |
| USB device (on-chip) | Pico, nRF52840, L496, L0538, S7G2 | partly base; host-side USB not modelled |

## Memories

| Part | Board | Interface | Status |
|---|---|---|---|
| W25Q16JV 2 MB QSPI flash (doc, marking unreadable) | Raspberry Pi Pico | QSPI XIP | done (in the `rp2040` profile) |
| Winbond W25Q-series QSPI flash (seen "winbond", 32 MB per doc) | SK-S7G2 | QSPI | base (generic SPI flash model) |
| IS66WV51216EBLL 8 Mbit PSRAM (seen) | STM32L496G-DISCO | FMC asynchronous SRAM | near (memory region behind FMC) |
| MX25R6435F 64 Mbit QSPI flash (seen) | STM32L496G-DISCO | QUADSPI | base (generic SPI flash model) |
| Kingston EMMC16G-MW28 16 GB eMMC (seen) | Arduino UNO Q | eMMC (QRB2210) | new — Linux side only |
| Micron LPDDR4 2 GB (seen "D8CSC") | Arduino UNO Q | DRAM (QRB2210) | new — Linux side only |
| 93C46 Microwire EEPROM (seen) | EK-LM3S2965 | FT2232D config | n/a |
| microSD socket | STM32L496G-DISCO | SDMMC | done (FatFs polled and DMA/FreeRTOS examples pass) |

## Scope notes

* **RL78/G13** needs a new CPU core in tlib (RL78 is a 16-bit CISC, no existing translator). It is the same kind of
  work as the dsPIC and C28x cores.
* **ESP32 (Xtensa LX6)**: Renode has an Xtensa translator, but no ESP32 SoC; the ROM, cache/MMU, RTC and Wi-Fi are
  the large parts. Wi-Fi can be stubbed at the driver level the way ESP-IDF's QEMU target does.
* **Arduino UNO Q** has two halves. The STM32U585 MCU half is in reach; the QRB2210 Linux half (Adreno GPU, LPDDR4,
  eMMC, Qualcomm PMIC) is not a Renode-class target and is listed for completeness only.
