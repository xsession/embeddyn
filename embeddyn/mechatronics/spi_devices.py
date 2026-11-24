"""SPI sensor models served to Renode firmware through External Control (Renode >= 1.17).

The platform puts an `SPI.ExternalControlSPIPeripheral` on the MCU's SPI
controller. Each byte the firmware clocks out arrives in embeddyn, and the
device model answers with the byte the firmware clocks in. Co-simulation
signals feed the model (an angle, a pressure, ...), so firmware reads live
physics through its real SPI driver.

```yaml
instances:
  mcu:
    options:
      backend: renode
      spi:
        - peripheral: sysbus.spi1.abs_encoder      # the ExternalControlSPIPeripheral
          device: as5047p                          # or "package.module:Class"
          parameters: {zero: 0.0, direction: 1}
          inputs: {angle: arm.angle}               # device input <- signal
          outputs: {errors: mcu.abs_encoder_errors}   # device output -> signal
```

A device implements `transmit(mosi) -> miso` and, optionally, `finish()`,
`set_inputs()` and `output_values()`. Renode's STM32 SPI model does not report
chip-select edges, so word-oriented devices count bytes to find frame
boundaries. `finish()` resynchronises when a controller does report them.
"""
from __future__ import annotations

import importlib
import math
from typing import Any, Mapping


class SpiDevice:
    """Base class: a byte-in/byte-out SPI slave with named inputs and outputs."""

    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()

    def transmit(self, mosi: int) -> int:
        raise NotImplementedError

    def finish(self) -> None:
        """Chip select released (only if the controller reports it)."""

    def set_inputs(self, values: Mapping[str, float]) -> None:
        for name, value in values.items():
            if name not in self.inputs:
                raise KeyError(f"{type(self).__name__} has no input {name!r} (inputs: {', '.join(self.inputs)})")
            setattr(self, name, float(value))

    def output_values(self) -> dict[str, float]:
        return {name: float(getattr(self, name)) for name in self.outputs}


def _even_parity_bit(word15: int) -> int:
    """Bit that makes the 16-bit word's number of ones even."""
    return bin(word15 & 0x7FFF).count("1") & 1


class AS5047P(SpiDevice):
    """ams AS5047P 14-bit magnetic rotary position sensor, SPI interface (mode 1, 16-bit frames, MSB first).

    Command frame: PARC (even parity) | R/W (1 = read) | ADDR[13:0].
    The answer comes in the *next* frame: PARD | EF (error in the previous
    frame) | DATA[13:0]. Reading `ANGLECOM` (command 0xFFFF) every frame
    therefore returns the angle latched one frame earlier, as on the chip.

    Inputs: `angle` (rad, mechanical angle of the magnet), `magnitude`
    (CORDIC magnitude, default 0x1000; 0 simulates a missing magnet).
    Parameters: `zero` (rad, magnet angle that reads 0) and `direction` (+1
    or -1), the effect of ZPOSM/ZPOSL and SETTINGS1.DIR. The firmware can also
    write those registers.
    Outputs: `frames` (complete frames) and `errors` (frames with a parity or
    command error).
    """

    inputs = ("angle", "magnitude")
    outputs = ("frames", "errors")

    NOP, ERRFL, PROG, DIAAGC, MAG, ANGLEUNC, ANGLECOM = 0x0000, 0x0001, 0x0003, 0x3FFC, 0x3FFD, 0x3FFE, 0x3FFF
    ZPOSM, ZPOSL, SETTINGS1, SETTINGS2 = 0x0016, 0x0017, 0x0018, 0x0019
    _WRITABLE = {PROG, ZPOSM, ZPOSL, SETTINGS1, SETTINGS2}
    FRERR, INVCOMM, PARERR = 1 << 0, 1 << 1, 1 << 2

    def __init__(self, zero: float = 0.0, direction: int = 1, magnitude: float = 0x1000) -> None:
        if direction not in (1, -1):
            raise ValueError("direction must be +1 or -1")
        self.zero, self.direction = float(zero), int(direction)
        self.angle, self.magnitude = 0.0, float(magnitude)
        self.frames = self.errors = 0
        self._registers = {self.PROG: 0, self.ZPOSM: 0, self.ZPOSL: 0, self.SETTINGS1: 0x0001, self.SETTINGS2: 0}
        self._errfl = 0
        self._error_flag = False          # framing error to report in the next answer
        self._answer = 0                  # 16-bit word shifted out during the current frame
        self._shift = bytearray()
        self._write_address: int | None = None

    # ------------------------------------------------------------------ data
    def counts(self) -> int:
        """14-bit angle as the sensor reports it (zero position and direction applied)."""
        zero_counts = ((self._registers[self.ZPOSM] & 0xFF) << 6) | (self._registers[self.ZPOSL] & 0x3F)
        sign = -1 if (self._registers[self.SETTINGS1] >> 2) & 1 else 1
        unit = (self.direction * sign * self.angle - self.zero) / (2 * math.pi)
        return (int(math.floor(unit * 16384 + 0.5)) - zero_counts) & 0x3FFF

    def _read(self, address: int) -> int:
        if address in (self.ANGLECOM, self.ANGLEUNC):
            return self.counts()
        if address == self.MAG:
            return int(max(0.0, min(self.magnitude, 0x3FFF)))
        if address == self.DIAAGC:
            magnitude = self.magnitude
            low, high = magnitude < 0x0400, magnitude > 0x3000
            return (1 << 8) | (int(high) << 11) | (int(low) << 10) | 0x80      # LF, MAGH, MAGL, AGC
        if address == self.ERRFL:
            value, self._errfl = self._errfl, 0                                 # cleared by reading
            return value
        if address == self.NOP:
            return 0
        if address in self._registers:
            return self._registers[address]
        self._errfl |= self.INVCOMM
        return 0

    # ------------------------------------------------------------------ SPI
    def transmit(self, mosi: int) -> int:
        index = len(self._shift)
        self._shift.append(mosi & 0xFF)
        miso = (self._answer >> 8) & 0xFF if index == 0 else self._answer & 0xFF
        if len(self._shift) == 2:
            self._frame((self._shift[0] << 8) | self._shift[1])
            self._shift.clear()
        return miso

    def finish(self) -> None:
        if self._shift:                   # chip select released mid-frame: framing error, resync
            self._shift.clear()
            self._errfl |= self.FRERR
            self._error_flag = True

    def _frame(self, word: int) -> None:
        self.frames += 1
        error = False
        if _even_parity_bit(word) != (word >> 15) & 1:
            self._errfl |= self.PARERR
            error = True
            data = 0
        elif self._write_address is not None:              # data frame of a write
            address, self._write_address = self._write_address, None
            data = self._registers.get(address, 0)          # the chip answers with the old content
            self._registers[address] = word & 0x3FFF
        elif word & 0x4000:                                 # read
            before = self._errfl
            data = self._read(word & 0x3FFF)
            error = bool(self._errfl & ~before & self.INVCOMM)
        else:                                               # write command: data follows
            address = word & 0x3FFF
            if address in self._WRITABLE:
                self._write_address = address
            else:
                self._errfl |= self.INVCOMM
                error = True
            data = 0
        if error:
            self.errors += 1
        # EF flags an error in the frame this answer belongs to (or a framing error before it).
        word15 = (int(error or self._error_flag) << 14) | (data & 0x3FFF)
        self._answer = (_even_parity_bit(word15) << 15) | word15
        self._error_flag = False


DEVICES: dict[str, type[SpiDevice]] = {"as5047p": AS5047P}


def create_device(spec: str, parameters: Mapping[str, Any] | None = None) -> SpiDevice:
    """A device from a short name (`as5047p`) or `package.module:Class`."""
    if spec.lower() in DEVICES:
        cls: Any = DEVICES[spec.lower()]
    elif ":" in spec:
        module, _, name = spec.partition(":")
        cls = getattr(importlib.import_module(module), name)
    else:
        raise ValueError(f"unknown SPI device {spec!r} (known: {', '.join(sorted(DEVICES))}, or module:Class)")
    device = cls(**dict(parameters or {}))
    if not hasattr(device, "transmit"):
        raise TypeError(f"{spec} is not an SPI device (no transmit method)")
    return device
