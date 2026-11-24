# Design: SPECT body contour on dsPIC30F5011 with iC-Haus light chains

*Status: design, 2026-11-24. The STM32L476 prototype
(`examples/mechatronics/spect-body-contour.yaml`) already closes the loop with
real firmware in Renode and MuJoCo beam physics; this page plans the
product-faithful version.*

The workflow behind this page, phase by phase, is in the guide
[Emulate a real product end to end](../guides/emulate-a-product.md).

## The system to emulate

Each detector head carries two body-contour boards, each with its own
**dsPIC30F5011**:

| Board | Chain | Job |
|---|---|---|
| master | IR LEDs, each driven by an **iC-NT** light-chain pulse driver | fires the beams one after another |
| slave | photodiodes on **iC-ME** dual light-grid pulse receivers (two beams per IC) | evaluates each beam |

Master and slave talk **CANopen** (CANopenNode). The master sends the slave the
start sign of a chain scan, so that LED *k* and photodiode *k* are active
together. Two heads make four dsPIC nodes on one CAN bus; a gantry controller
(modelled in embeddyn) is the NMT master and moves the heads.

### One pixel (beam) measurement, as built

1. **Master:** activates iC-NT *k* through the chain (DIN/CLK/SHEN) and
   modulates its LED with a **300 kHz PWM** burst, so the light sits in the
   iC-ME's 300 kHz bandpass and near-static background IR (lamps, daylight,
   heat) falls outside it.
2. **Slave:** activates the matching iC-ME channel (PD0 or PD1 of IC *k*/2).
   The receiver's SP−SN signal goes to a **comparator**; while the received
   carrier exceeds its threshold the comparator pulses at 300 kHz.
3. The comparator output **charges a measurement capacitor**. At the end of the
   pixel the slave's ADC reads the capacitor voltage (a measure of how much of
   the burst got through), then a **transistor shorts the capacitor to ground**
   to reset it for the next pixel.

So a beam's result is analog: a clear beam fills the capacitor to a known
level, a blocked one leaves it near zero, and a grazing or dimmed one lands in
between, which the firmware can threshold or trend (dirty optics, alignment).

## Facts from the datasheets

**iC-NT** (light chain pulse driver, Rev A0):

* SO8: DIN, VDD, CLK, SHEN, ISET, DOUT, GND, LED. Chained DOUT → DIN of the
  next IC, CLK and RSET shared ("ca. 5 ICs" per RSET).
* 3-flip-flop shift register. DIN = hi activates the current sink (standby →
  operation ≥ 5 µs, `ten`). The CLK **rising** edge loads FF1 and switches the
  LED on; the next rising edge with DIN = lo resets FF1 and switches it off, so
  the light pulse lasts from one rising edge to the next (`tw` ≥ 1 µs, typically
  1–3 µs). FF3 takes the bit on the **falling** edge and drives DOUT, but only
  if SHEN = hi: SHEN gates the hand-over to the next IC.
* LED current = 1200 × I(ISET), V(ISET) = 1.50 V at 27 °C (+0.33 %/K):
  RSET = 1.8 kΩ → 0.82…1.18 A (typ. 1.0 A), 4.5 kΩ → 0.33…0.48 A.
  Rise/fall ≤ 150 ns; turn-on delay ≤ 100 ns, turn-off ≤ 80 ns.
* Shift register reset and sink off on over-temperature (110…150 °C) or
  undervoltage (VDD < 2.7 V; power-on at 4.3 V).

**iC-NE** (light-grid pulse receiver, Rev E1) and **iC-ME** (dual, Rev A1):

* iC-NE: DIN, VDD, CLK, SP, SN, DOUT, GND, PD. iC-ME adds a second photodiode
  input (PD0, PD1) and a 6-stage register: one IC is two chain positions.
* DIN = hi enables the bandpass amplifier (`ten` ≥ 10 µs before the first
  CLK rising edge); the output stage turns on at that edge and needs
  `tinh` ≥ 5 µs to settle. The next rising edge turns it off; the token moves on
  at the falling edge (iC-ME: PD0, then PD1, then DOUT after the 4th edge).
  Only one device in the chain is active, so all SP/SN outputs share one
  two-wire bus.
* Bandpass 100…530 kHz (centre ≈ 300 kHz): the receiver answers the **rising
  edge** of a light pulse (≥ 1 µs); DC photocurrent up to 15 µA (ambient light)
  is suppressed. Evaluate the rising edge, not the level.
* Output: I(SP) − I(SN) pulse, gain Gpk 360…620 (typ. 490) for small signals,
  compressing: −5 mA typ. at 10 µA photocurrent, −7.3 mA at 100 µA, monotone up
  to 1.5 mA; I(SN) + I(SP) ≈ 7.5 mA; noise ≈ 5 µA rms (3.5 µA filtered).
* iC-ME's input safety switch shorts the deselected photodiode (≤ 40 Ω).

## Emulation architecture

The chains clock at microsecond scale: a beam lasts one or two CLK periods
of a few µs, and the slave samples its receiver right after its own clock
edge. That loop is far below a co-simulation step, so it runs **inside
Renode**, in virtual time, driven by the dsPIC pins:

```
master dsPIC ──DIN/CLK/SHEN──► iC-NT chain ──┐
master dsPIC ──300 kHz PWM pin──────────────┤ LED k on, modulated
                                             ▼
                          LightLink (emulation-level hub)
                          transmittance T[emitter][receiver] from MuJoCo
                                             │ photocurrent bursts
slave dsPIC ──DIN/CLK──► iC-ME chain ◄───────┘
                            │ SP−SN (300 kHz bandpass)
                            ▼
                         comparator ──► measurement capacitor ──► slave ADC
                                              ▲
slave dsPIC ──reset transistor pin────────────┘
both dsPICs ◄── CAN hub (dsPIC30 CAN models) ──► External Control CAN ◄── embeddyn gantry controller
```

* **Geometry stays in MuJoCo.** Every co-simulation step embeddyn casts the
  beams (`optics.py`, MuJoCo `light_curtain`) and writes the transmittance
  matrix, including neighbour cross-talk from the emitter/receiver angular
  profiles, into the link. The electrical chain behaviour (activation times,
  pulse widths, gain compression, ambient rejection, SHEN gating, the
  CANopen-synchronised start) is then exact to the instruction.
* **Renode work** (local fork branch `merge-renode-1.17.1`): compile the
  dsPIC30 peripheral models now parked in `_backup_peripherals`, add a dsPIC30
  UART, make `dsPIC30_CAN` a CAN-hub node, and add the iC-NT, iC-ME, receiver front end
  and LightLink models (C#, GPIO-connected).
* **Firmware**: XC16 v2.10 (Linux, Free mode), CANopenNode with a dsPIC30 CAN
  driver; master = sends the start sign and clocks the LED chain with the
  300 kHz burst per pixel, slave = clocks the receiver chain, resets and reads
  the measurement capacitor per pixel and publishes the beam values.
* **Receiver front end model**: iC-ME bandpass response to the carrier
  (amplitude ∝ Gpk × photocurrent, compressing), comparator with hysteresis,
  RC charge of the measurement capacitor while the comparator is high,
  discharge through the reset transistor; the slave's ADC samples the
  capacitor node.

## Open questions for the product owner

Answered: iC-ME on the slave; 300 kHz LED modulation from the master; comparator
→ measurement capacitor → ADC, reset by a transistor; master sends the start
sign to the slave.

Still open (defaults used until known):

* How the 300 kHz PWM gates the iC-NT LED current (iC-NT's own LED pulse is
  one CLK period long, so the carrier presumably switches the shared RSET/ISET
  path or the LED supply). Default: the PWM pin gates the active LED's current.
* Beams per head, burst length per pixel, comparator threshold, capacitor and
  charge resistor, reset transistor pin.
* The CANopen object for the start sign (SYNC or an RPDO), the slave's TPDO
  layout, and who moves the heads.
