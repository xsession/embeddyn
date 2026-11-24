# Add an SPI sensor model

When the firmware talks to an SPI sensor that Renode does not model, write the sensor in Python and let Renode forward every byte to it. Requires Renode ≥ 1.17 (`api: message` or `auto`).

## 1. Put an External Control SPI peripheral in the platform

```text title="my-board.repl"
spi1: SPI.STM32SPI @ sysbus 0x40013000
    series: STM32Series.L5
    IRQ -> nvic@35

pressure: SPI.ExternalControlSPIPeripheral @ spi1
```

## 2. Write the device

Subclass `SpiDevice`; implement `transmit(mosi) -> miso`. Here is a minimal 24-bit pressure sensor that answers a read command `0x80` with three data bytes:

```python title="mydevices.py"
from embeddyn.mechatronics.spi_devices import SpiDevice

class PressureSensor(SpiDevice):
    inputs = ("pressure",)          # set from signals before each step
    outputs = ("reads",)            # reported back as signals

    def __init__(self, full_scale=200_000.0):
        self.full_scale = float(full_scale)
        self.pressure, self.reads = 0.0, 0
        self._out = []                       # bytes still to shift out

    def transmit(self, mosi: int) -> int:
        if self._out:
            return self._out.pop(0)
        if mosi == 0x80:                     # read command: latch and queue 3 bytes
            code = int(max(0.0, min(1.0, self.pressure / self.full_scale)) * 0xFFFFFF)
            self._out = [(code >> 16) & 0xFF, (code >> 8) & 0xFF, code & 0xFF]
            self.reads += 1
        return 0x00

    def finish(self) -> None:               # chip select released (if the controller reports it)
        self._out.clear()
```

`SpiDevice.set_inputs` and `output_values` come for free from the `inputs`/`outputs` tuples. If the controller model never reports chip-select release (Renode's STM32 SPI does not), frame by byte count as the built-in `AS5047P` does.

## 3. Wire it into the project

```yaml
instances:
  mcu:
    options:
      backend: renode
      spi:
        - peripheral: sysbus.spi1.pressure
          device: mydevices:PressureSensor          # or a short name registered in spi_devices.DEVICES
          parameters: {full_scale: 200000}
          inputs: {pressure: {signal: tank.pressure, scale: 1.0, offset: 0.0}}
          outputs: {reads: mcu.pressure_reads}
```

The adapter validates device, inputs and outputs before launching Renode, creates a **fresh device per run** (every run starts from power-up), feeds inputs at the start of each step and reports outputs after it.

## 4. Test without Renode

Drive the model byte by byte in a unit test, as `tests/mechatronics/test_spi_devices.py` does for the AS5047P:

```python
s = PressureSensor(); s.set_inputs({"pressure": 100_000})
assert [s.transmit(b) for b in (0x80, 0, 0, 0)][1:] == [0x7F, 0xFF, 0xFF]
```

## Costs and limits

* Each byte is one round trip between Renode and Python (tens of µs). A few kHz of SPI traffic is fine; a 10 MHz streaming ADC is not: model it as a register feed instead.
* The model runs inside the adapter's process, so keep it deterministic (no wall-clock, no unseeded randomness).
