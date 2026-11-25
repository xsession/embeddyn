# Per-peripheral checklists

Tick every item, or write it in the model's limits. Each "test:" line is something a firmware test
must check.

## Interrupt controller

- [ ] Flag, enable and priority registers at the right offsets; reset priorities as in the data
  sheet (often not 0).
- [ ] Inputs in vector order; a rising edge sets the flag; software clears it.
- [ ] Highest priority wins, ties go to the lower vector; publish to the CPU (export) and drive its
  IRQ line.
- [ ] External interrupt pins with edge polarity; nesting disable; alternate vector table; global
  disable instructions (DISI) respected by the CPU glue.
- [ ] Lazy sources: poll before flag reads; report enable changes.
- test: a timer interrupt at the expected rate; nesting by priority; edge polarity.

## System: clock, reset, watchdog, NVM, power, module disable

- [ ] Oscillator modes from the fuse encodings (PLL factors, internal RC frequencies, postscaler);
  clock switching with its lock bits; publish Fcy and changes; CPU MIPS from Fcy.
- [ ] Reset causes into the reset-control register (power-on, brown-out, software, watchdog,
  illegal opcode, trap conflict, external pin); kept across the machine reset.
- [ ] Watchdog: enable fuse or software bit, prescalers, period, clear instruction, reset, or wake
  from sleep.
- [ ] NVM: unlock sequence, write latches from table writes, row erase/program, EEPROM
  word/row/all with its timing, write-error bit, completion interrupt, invalidating translated
  code.
- [ ] Power states: Idle, Sleep, wake (CPU callback); SIDL bits per module.
- [ ] Brown-out (supply voltage property, threshold fuse), power-up timer, low-voltage detect
  interrupt, clock monitor (oscillator-fail trap).
- [ ] Module-disable registers (PMD).
- test: Fcy from the fuses; clock switch; watchdog reset; Sleep woken by an interrupt; Idle; PMD
  read 0; EEPROM and flash program/read-back.

## Timers

- [ ] Prescalers; period register; counter read/write; external clock and gate; 32-bit pairing
  with a holding register.
- [ ] Interrupt on the period match (or the gate's falling edge).
- [ ] Lazy mode while the interrupt is off and no consumer needs events; SyncTime on reads.
- [ ] Freeze in Sleep (internal clock); SIDL in Idle.
- test: the interrupt rate equals Fcy / prescale / (PR + 1); a polled flag of a fast timer.

## GPIO ports and change notification

- [ ] TRIS/PORT/LAT; PORT reads the pin (driven level, external level, pull-up, analog = 0).
- [ ] External inputs, peripheral output overrides, mirror outputs to peripheral inputs; pin map
  per data sheet.
- [ ] Change notification numbers and enables, pull-ups, its interrupt.
- [ ] Incremental output updates (only the changed pins).
- test: output pin toggling; an input pin raising INTx and capture; CN; an analog pin reads 0.

## UART

- [ ] Baud rate from BRG and Fcy; frame bits from the data/parity/stop settings.
- [ ] TX buffer depth, shift register timing, TX-buffer-full and TX-empty status bits, TX
  interrupt modes.
- [ ] RX FIFO depth, RX interrupt levels, overrun stops reception until cleared, loopback.
- [ ] Wake on start bit in Sleep; SIDL.
- test: N characters take N × frame time; buffer-full after quick writes; interrupt-driven echo.

## Output compare / PWM / input capture

- [ ] Every compare mode (single high/low, toggle, single/continuous pulse, PWM, PWM with fault).
- [ ] Time-base select; duty latched at the period; duty/frequency properties; pin output to the
  port.
- [ ] Capture modes (edges, prescaled), timer select, FIFO depth, overflow, interrupt per N
  captures.
- test: PWM duty and frequency; toggle output captured by a capture unit.

## ADC

- [ ] Channel inputs as IADC (microvolts) for External Control; references; negative input.
- [ ] Triggers: manual, external pin, timer, auto; sample/convert timing in TAD; internal RC clock
  in Sleep.
- [ ] Scan, alternate MUX, conversions per interrupt, split buffer, result formats.
- [ ] Analog pin configuration feeding the port.
- test: a known voltage gives the expected count; the format; timer-triggered conversion.

## SPI / I2C

- [ ] SPI: master timing at Fsck; TX/RX buffer flags; overflow; 8/16-bit; slave mode as an
  ISPIPeripheral; slave select.
- [ ] I2C: start/restart/stop/receive/ack each take bus time; busy bits; ACK/NACK; collision
  bit; device registry by address.
- test: busy right after the write, done after the bus time; NACK from a missing device.

## CAN

- [ ] Filters and masks (standard/extended), buffers, double buffering, overflow.
- [ ] Modes (normal, disable, loopback, listen-only, configuration, listen-all).
- [ ] Bit rate from the timing registers; frame time; priorities; abort.
- [ ] Error counters and states (warning, passive, bus-off with recovery), missing ACK, injection
  methods; wake-up.
- [ ] ICAN for the CAN hub / External Control.
- test: a frame from CAN1 to CAN2 through a hub; bit rate; error states after injected errors.

## Codec / serial audio (DCI, I2S, SAI)

- [ ] Frame/slot structure, master/slave frame sync, slot enables, buffer swap, underflow and
  overflow, loopback.
- test: loopback of a known word.
