# Multi-board buses, radios and the analyzer

Several boards can share a bus or a radio channel in one co-simulation. Each board
publishes frames on an **event topic**, and the orchestrator hands them to every other
instance subscribed to that topic. `embeddyn/mechatronics/comms/` provides:

* a CAN bus model with arbitration and exact frame timing;
* a CANopen stack for virtual boards;
* BLE advertising and IEEE 802.11 frame builders;
* decoders for all of these.

The Lab's **Analyzer** tab shows the traffic the way Wireshark would, and exports pcap
files that Wireshark opens.

```bash
embeddyn mechatronics run examples/mechatronics/multiboard-canopen-ble.yaml
embeddyn mechatronics gui examples/mechatronics/multiboard-canopen-ble.yaml   # Run, then the Analyzer tab
```

## The example: four CANopen boards, BLE and Wi-Fi

```text
  master (node 1)        axis (node 2)          sensor (node 3)        gateway (node 4)
  NMT master, SYNC       RPDO set-point         TPDO distance, temp    CANopen listener
  SDO client, set-point  TPDO status/position   EMCY over-temperature  BLE advertiser ──┐
  ───────────────────────────── CAN 500 kbit/s (CanBus) ───────────────────────────    │ BLE ch 37/38/39
                                                                                        ▼
                                   access point ◀── 802.11 ch 6 (UDP JSON) ── bridge (BLE scanner + Wi-Fi)
```

`multiboard-canopen-ble.yaml` (2 s at 0.5 ms steps, about 5 s of wall time) plays out:

1. **Boot.** Every node sends its boot-up message (`0x700+n`, `00`).
2. **Configuration.** The master reads each node's device type (0x1000, expedited SDO)
   and name (0x1008, a segmented SDO upload), then writes the heartbeat time
   (0x1017, expedited download).
3. **Start.** `NMT start all`. From then on the master sends **SYNC** every 10 ms and the
   axis set-point as RPDO1 (`0x202`: target µm, controlword), which follows a 1 Hz square
   wave.
4. **Axis.** On every SYNC the axis sends TPDO1 (`0x182`: statusword, position); TPDO2
   (`0x282`: velocity, current) goes every 5th SYNC. The axis moves with a speed and
   acceleration limit; statusword bit 10 is *target reached*.
5. **Sensor.** Sends TPDO1 (`0x183`: distance from a 1.5 Hz sine, temperature) every 20 ms.
   It heats up and sends **EMCY 0x4210** (device over-temperature) at about 1.2 s.
6. **Heartbeats.** All nodes send them every 100 ms. The master counts operational nodes
   and would send EMCY 0x8130 for a node it lost.
7. **Gateway (BLE).** Node 4 listens to the PDOs, heartbeats and EMCYs. Every 100 ms (plus
   a 0–10 ms random advDelay) it advertises an `ADV_NONCONN_IND` named `EMB-GW` on
   channels 37, 38 and 39. Its manufacturer data (company 0xFFFF) carry: version,
   operational-node bits, position, temperature, distance, flags (EMCY active) and a counter.
8. **Bridge (Wi-Fi).** An ESP32-style station scans one advertising channel at a time
   (30 ms windows), so it sees about a third of the packets. It joins the access point
   (probe, open authentication, association) and every 250 ms sends the latest status as
   UDP JSON to 192.168.4.1:5000.
9. **Access point.** Sends beacons every 100 TU, answers the join and acknowledges each
   unicast frame.

Signals worth watching: `can.load_avg` (about 5 % average bus load; `can.load` is the
share of each 0.5 ms step, near 100 % in the SYNC bursts), `master.nodes_operational`,
`axis.position_mm` against `master.axis_position_mm` (what the master sees over PDO),
`sensor.temperature_c` against `bridge.temperature_c` (the same value after CAN → BLE →
Wi-Fi), `bridge.ble_seen`, `bridge.udp_sent`.

## The RF lab: NFC, RFID, LoRa, BLE and Wi-Fi

`examples/mechatronics/rf-lab.yaml` runs 2 s at 0.25 ms steps (about 8 s of wall time).
Every radio below is a python plant in `comms/rf_boards.py`, and the analyzer decodes all
of them.

| Radio | Boards | What the traffic shows |
|---|---|---|
| NFC 13.56 MHz (`comms.nfc`) | `NfcReader`, `Type2Tag` (NTAG213) | REQA (7-bit), ATQA, anticollision and select at two cascade levels for a 7-byte UID (cascade tag 0x88, BCC), SAK, READ of 4 pages at a time with CRC_A, HLTA. The tag answers only while `nfc.tag_present` is high. The analyzer collects the pages read and parses the NDEF message (URI and text records). |
| RFID 125 kHz (`comms.rfid`) | `Em4100Card` | The 64-bit EM4100 frame every 32.8 ms: header, row and column parity, the card number in printed form (`180,51170`). |
| RFID UHF EPC Gen2 | `Gen2Reader`, `Gen2Tags` | Inventory rounds: Query (CRC-5), QueryRep, RN16, ACK, then PC + EPC + CRC-16. Tags pick slots at random, so some slots are collisions that resolve in a later round. |
| LoRa / LoRaWAN 1.0.x (`comms.lora`) | `LoraNode`, `LoraGateway` | AES-encrypted uplinks on port 2 (temperature, humidity, battery), every third one confirmed. The gateway answers with ACKs and once with a LinkADRReq (FOpts), and the node answers with LinkADRAns. With the session keys in `analyzer.lorawan`, the MIC is checked and the payload decrypted and decoded. Time on air uses Semtech's formula. |
| BLE | `BleBeacon`, `BleSensor`, `BlePhone` | iBeacon, Eddystone-UID and Eddystone-TLM adverts. A connectable sensor sends Environmental Sensing service data; a phone scans it actively (SCAN_REQ / SCAN_RSP with its name), then sends CONNECT_IND (access address, interval, channel map). |
| Wi-Fi WPA2 | `WpaAccessPoint`, `WpaStation`, `Deauther` | Beacons with privacy and an RSN element (CCMP, PSK); open authentication and association; the EAPOL-Key 4-way handshake (messages 1–4 recognised from the key-info bits); CCMP-encrypted data (PN shown, payload not decodable). A spoofed deauthentication (reason 7) at 1.2 s makes the station join again: two handshakes in the run. |

The codecs are checked against published values:

* CRC_A of `50 00` (HLTA) is `57 CD`;
* the LoRaWAN example frame `40F17DBE…` from the `lora-packet` project decrypts to
  "test" with a valid MIC;
* the time on air of 13 bytes at SF7 / 125 kHz is 46.3 ms.

LoRaWAN decryption needs the `cryptography` package. Without it, frames decode without
the MIC check and the payload.

## Impaired media: noise, fading, interference, crosstalk

For stress tests, a project's `media:` section degrades the traffic on any event topic,
wired or radio. The orchestrator passes every published event through the topic's medium
(`comms.medium.Media`) before any receiver or the analyzer sees it. All receivers
therefore see the same spoiled frame, and the capture shows what happened:

```yaml
media:
  can:                                   # the CAN wire
    bit_error_rate: 1.0e-4
    bursts: [{start: 1.0, end: 1.2, bit_error_rate: 3.0e-3}]
    crosstalk: [{signal: axis.current_a, ber_per_unit: 2.0e-4}]   # EMI that grows with the motor current
  ble:
    rssi_offset: -20                     # dB: distance, walls
    fading_std: 6                        # dB per packet: multipath
    sensitivity: -90                     # weaker packets are lost
    interferer: {period_ms: 10, duration_us: 1500, loss: 0.9}
  lora:
    modulation: {shape: sine, frequency: 0.5, depth: 1.0}   # error rate and loss swing over time
    loss: 0.3
```

| Setting | Effect |
|---|---|
| `bit_error_rate` | each payload bit flips with this probability (Poisson count, random positions) |
| `loss` | the frame is lost |
| `jitter_us` | random extra delay |
| `bursts` | windows (`start`, `end`, or `period` and `duty`) with their own `bit_error_rate` and `loss` |
| `modulation` | scales the error rate and the loss by `1 − depth + depth·w(t)`, where `w` is a sine or a square of `frequency` Hz (fading, periodic EMI) |
| `crosstalk` | `{signal, ber_per_unit}`: the error rate grows with an analog signal's magnitude (motor current, PWM). `{topic, window_us, probability, bits}`: a frame within `window_us` after a frame on another topic gets `bits` flipped |
| `rssi_offset`, `fading_std`, `sensitivity` | radio: shift and Gaussian fading of RSSI (and SNR); packets below the sensitivity are lost |
| `interferer` | a periodic interferer: frames during its on-time are lost with `loss` |
| `can_errors` (default on) | CAN: a frame with bit errors is replaced by an error frame and retransmitted (up to `max_retries`, 8), timed at the bus `bitrate`. Frames that fail every retry count as `abandoned` |
| `seed` | makes the noise repeatable |

What receivers see:

* **Lost frames and CAN error frames** stay in the capture, flagged, but are delivered to
  no board.
* **Corrupted frames** are delivered with the flipped bits, and the board's own integrity
  check decides, as in hardware:
  * CRC-24 for BLE, FCS for Wi-Fi, MIC for LoRaWAN;
  * CRC_A and BCC for NFC. An NFC tag that gets an invalid frame goes back to IDLE
    (ISO 14443-3), so the reader polls again.

  Stress runs therefore show retries, timeouts, missed adverts and rejoins, not just bad
  bytes.

**In the analyzer:**

* impaired frames carry a ⚡ tag in the summary (bit errors, lost and why, error frame,
  retransmission) and a *⚡ medium* branch listing the flipped bits, the bytes as they
  were sent, and the crosstalk source;
* their rows and lane ticks are magenta, and the damaged bytes are red in the hex dump;
* the filter `⚡` lists only the impaired traffic.

The **⚡ Medium…** panel sets all of the above for a topic:

* **Apply** changes the running simulation at once (the next published frame) or, when
  nothing runs, the next run;
* **Save to project** writes it under `media:`;
* the status line counts frames, corrupted and flipped bits, losses, error frames and
  retransmissions per topic.

In the example, `can` at BER 1e-3 produces error frames and retransmissions while the
CANopen network stays operational. BLE with −20 dB and 6 dB fading loses about a sixth
of the adverts, and the Wi-Fi bridge still forwards the status.

### Distance, cables and I2C (`comms.physical`)

Instead of fixed numbers, a medium can describe where the devices are and how they are
wired. The physical models then work out RSSI, bit errors, delays and losses.

**Radio distance (`link:`).** The model is log-distance path loss:

    PL = 20·log10(f / MHz) − 27.55 + 10·n·log10(d / m) + walls

* **RSSI** = transmit power − PL, plus `fading_std` if set.
* **Bit errors** follow a waterfall around the receiver sensitivity: 1e-3 at the
  sensitivity, ten times fewer for every 1.5 dB above it.
* **Losses:** packets more than 3 dB below the sensitivity are not detected.

Each radio has typical defaults:

* **BLE:** 0 dBm, −94 dBm sensitivity.
* **Wi-Fi:** 18 dBm, −82 dBm; the frequency comes from the channel.
* **LoRa:** 14 dBm; sensitivity −123 dBm (SF7) to −137 dBm (SF12), taken from each packet,
  so a higher SF reaches further.
* **NFC and 125 kHz RFID:** inductive near field, 60 dB per decade, reaching about 5 cm and
  8 cm.
* **UHF Gen2:** 33 dBm EIRP. A tag needs −18 dBm on the forward link to answer, so tags
  past about 10 m go silent.

```yaml
media:
  lora: {link: {distance_m: 1500, exponent: 2.9}}                       # 1.5 km suburban
  ble:  {link: {distance_m: 3, exponent: 2.7, sources: {beacon: {distance_m: 12}}}, fading_std: 4}
  wifi: {link: {distance_m: 8, exponent: 3.0, walls_db: 5, tx_dbm: 15}}
```

* `sources` gives each sender its own distance, keyed by the payload's `src`.
* The computed RSSI and SNR replace the transmitter's numbers in the packets.
* The analyzer shows a *link* line (distance, RSSI, BER) for each packet, and the Medium
  panel shows the budget per sender.

The RF lab ships with realistic placements: tag at 2 cm, EM4100 at 3 cm, UHF tags at
3–6 m, LoRa at 1.5 km, BLE phone at 3 m and beacon at 12 m, Wi-Fi at 8 m through one
wall. Moving NFC to 9 cm, or LoRa to 9 km, breaks those links, as the tests check.

**Cables (`cable:`).**

* **Delay:** 5 ns/m.
* **EMI pickup:** `emi_ber_per_m` × length, multiplied by 0.05 for a twisted pair, 0.5
  for ribbon cable, 0.02 for coax and 0.1 for a shield. The same factor scales
  `crosstalk` from signals.
* **CAN bit timing:** twice the cable delay plus the transceiver and controller delays
  must fit in about 70 % of a bit. The limit comes out at about 30 m at 1 Mbit/s, 100 m at
  500 kbit/s and 520 m at 125 kbit/s. Past it, a share of the frames (growing with the
  overrun) fail arbitration or ACK on every attempt and become error frames.

**I2C bus capacitance (`cable: {i2c: {...}}`).**

* **Bus capacitance** = cable (pF/m: twisted 52, untwisted 40, ribbon 45, coax 100, +60
  with a shield) + 10 pF per device + 10 pF.
* **Rise time** t_r = 0.8473·Rp·Cb, checked against UM10204:

  | mode | max rise time | max bus capacitance |
  |---|---|---|
  | 100 kHz | 1000 ns | 400 pF |
  | 400 kHz | 300 ns | 400 pF |
  | 1 MHz | 120 ns | 550 pF |

* **Pull-up range:** from (VDD − 0.4 V) / I_OL up to the value that still meets the rise
  time.
* **Bit errors:** an edge too slow for the spec causes bit errors that grow as it eats the
  SCL high time. I2C has no checksum: a flipped data bit is a wrong reading, and a flipped
  address bit is a NACK.

```yaml
media:
  can: {kind: wired, bitrate: 500000, cable: {length_m: 130, type: twisted}}
  i2c:
    kind: wired
    cable: {length_m: 2, type: untwisted, shielded: true, i2c: {mode: fast, pullup_ohm: 4700, vdd: 3.3, devices: 2}}
```

`examples/mechatronics/cable-lab.yaml` wires exactly that.

* **CAN:** three CANopen boards on 130 m of twisted pair at 500 kbit/s (limit 100 m), with
  the axis current coupling in. Hundreds of error frames and retransmissions; the network
  stays up but is busy recovering.
* **I2C:** an MCU polling a TMP102-style sensor (`comms.i2c`) over 2 m of shielded cable in
  fast mode with 4.7 kΩ pull-ups. 230 pF and a 916 ns rise time against 300 ns cause
  NACKs and readings off by up to 128 °C (flipped sign bits). The advice is 967–1539 Ω.
* **The fix:** 1.2 kΩ pull-ups and a 40 m CAN cable, set in the panel, clear every error.

The Medium panel has the matching fields:

* **radio link:** distance, Ptx, exponent, walls, per-sender distances;
* **cable:** length, twisted / untwisted / ribbon / coax, shielded, EMI, CAN bit rate;
* **I2C:** mode, pull-up, VDD, devices.

The checks (link budget per sender, cable delay and EMI, the CAN length limit and timing
margin, the I2C capacitance, rise time, verdict and pull-up advice) update as you type,
before any run (`POST /api/analyzer/medium_check`). During a run they show what the run
measures.

## Event ports of python plants

A python plant can send and receive events besides its signals:

```yaml
axis:
  model: embeddyn.mechatronics.comms.boards:AxisNode
  options:
    backend: python
    events: {can_tx: can.tx}        # plant port -> topic it publishes on
    subscribe: {can_rx: can}        # plant port <- topic it receives
```

```python
class MyBoard:
    event_inputs = ("can_rx",)
    event_outputs = ("can_tx",)
    def receive(self, t, port, payload): ...          # each event, in time order, before the step
    def advance(self, t0, t1, u): ...                 # timers inside the step (optional)
    def pop_events(self): return [(t, "can_tx", {...})]   # events produced this step
```

The orchestrator keeps a log of published events (adapter `diagnostics["events"]`,
event and bus signals). Before each activation it hands an instance the events
published since its last activation, on topics it subscribed to, from **other**
instances. A frame therefore reaches its receivers at their next activation: one step of
latency per hop, 0.5 ms in the example. Adapters other than python plants take part by
implementing `receive_events(events)` and `subscribed_topics`.

## CAN bus (`comms.can`)

`CanBus` receives frames on `tx` and puts them on the wire (`wire` port, topic `can` in
the example):

* **Timing:** each frame takes its exact length in bits (`frame_bits`: SOF, identifier,
  control, data, CRC-15, stuff bits, delimiters, ACK, EOF and the 3-bit intermission)
  at the bit rate.
* **Arbitration:** when several frames wait, the lowest identifier goes first. 29-bit
  frames compare by their base identifier, with 11-bit frames winning ties.
* **Wire frames** carry `start`, `bits` and `seq`. Their event time is the end of the
  frame, when receivers see it.
* **Outputs:** `load` (share of the step), `load_avg` (first-order, `average_ms`) and
  `frames`.

Frame payload: `{"proto": "can", "id": 0x182, "data": "<hex>", "src": "axis"}`, plus
`ext` and `rtr` flags. Renode's CAN events (`{"id", "data"}`, see
[Renode CAN](renode.md#can-renode-117)) decode the same way.

## CANopen (`comms.canopen`)

`CanopenNode` is a base class for CiA 301 devices:

* object dictionary entries (`Entry`, types u8…i32, f32, str);
* the NMT state machine (boot-up, pre-operational, operational, stopped, the resets);
* heartbeat producer (0x1017);
* SDO server: expedited upload and download, segmented upload, abort codes;
* TPDOs on SYNC (`sync_every`) or on an event timer, and RPDOs;
* EMCY with error register (0x1001).

Subclasses implement `application(t0, t1, u)`, `on_frame` and `on_rpdo`. The example's
boards are in `comms/boards.py`. `CanopenMaster` adds the NMT master, the SYNC producer,
an SDO client (expedited and segmented) and a heartbeat consumer.

Not modelled: SDO block transfer, segmented downloads, LSS, TIME, PDO remapping over SDO
at run time, and error-control guarding.

## BLE and 802.11 (`comms.ble`, `comms.wifi`)

**BLE** packets are stored the way sniffers capture them, de-whitened:

* access address `0x8E89BED6`;
* PDU header and payload (AdvA, AD structures);
* a 3-byte CRC-24 (initial value 0x555555), computed the way Ubertooth and btle sniffers
  do.

`adv_packet` builds legacy advertising PDUs (31 bytes of AdvData at most), and `ad`
builds AD structures.

**802.11** frames include the FCS (CRC-32). There are builders for beacons, probe,
authentication and association frames, ACKs and data frames with LLC/SNAP, IPv4 (with
header checksum) and UDP. Networks are open, so the sniffer sees payloads; encryption is
not modelled.

Radio payloads: `{"proto": "ble"|"wifi", "channel": 37, "rssi": -62, "data": "<hex>",
"src": "gateway"}`. Receivers model what they can hear: the bridge drops packets on
channels it is not scanning and packets below its sensitivity. No propagation model is
applied otherwise.

## The analyzer

`comms.analyzer.decode_event(event, config)` turns an event into a packet row:

* time and protocol (CANopen, CAN, BLE, 802.11);
* source, destination and length;
* a one-line summary;
* a field tree whose entries carry byte ranges;
* the hex bytes.

`config` is the project's `metadata.analyzer`:

```yaml
metadata:
  analyzer:
    topics: [can, ble, wifi]                 # which event topics to decode
    can: {bitrate: 500000}
    canopen:
      master: 1                              # SDO responses are addressed to it
      nodes: {1: master, 2: axis, 3: sensor, 4: gateway}
      pdos:
        "0x182": {name: axis TPDO1, fields: [{name: status, format: u16, bits: {10: target reached}},
                                             {name: position, format: i32, scale: 0.001, unit: mm}]}
    ble:
      devices: {"C0:FF:EE:00:00:04": gateway}
      manufacturer:
        "0xFFFF": {name: gateway status, fields: [{name: temperature, format: i16, scale: 0.1, unit: degC}]}
    wifi:
      stations: {"02:00:00:00:00:01": ap}
```

Without a `canopen` section, CAN frames show as plain CAN. Without PDO or manufacturer
layouts, the payload shows as bytes.

### In the Lab

The **Analyzer** tab of the dock (next to Instruments, Console and Metrics) fills while a
run streams.

* **Protocol toggles** with packet counts, and a **filter**. Plain words match the summary,
  source, destination or type. Terms such as `id==0x182`, `node==3`, `src==gateway`,
  `type==EMCY`, `ch==37`, `rssi<-60`, `len>8` and `dst~ap` test fields, and all terms
  must match.
* **Lanes:** one row per sender, with a tick per packet over the run. EMCY and SDO
  aborts are red, SDOs amber and NMT violet; the yellow line is the time cursor. Click to
  jump to the nearest packet.
* **Packet list** (No., time, protocol, source, destination, length, info). It is
  virtualised, so thousands of packets scroll smoothly; arrow keys step through it.
* **Field tree and hex dump:** selecting a field highlights its bytes.
* **Time cursor:** clicking a packet moves the run's time cursor there, so the scope and
  meters show that instant. With **follow cursor**, scrubbing or **replay** selects the
  packet at the cursor.
* **pcap:** CAN (LINKTYPE_CAN_SOCKETCAN, 227), BLE (LINKTYPE_BLUETOOTH_LE_LL, 251) and
  Wi-Fi (LINKTYPE_IEEE802_11, 105), nanosecond timestamps.

### Changes view (reverse engineering)

**Packets | Changes** switches the analyzer to a view in the style of cansniffer and
SavvyCAN, for working out what an unknown payload means. There is one row per message:

* the CAN identifier;
* the BLE sender and PDU type;
* or the 802.11 sender, receiver and frame type.

Each row shows the message count, its period and the latest bytes:

* **orange:** changed in the latest packet;
* **amber, fading:** changed within the fade window (500 ms by default);
* **grey:** changed earlier;
* **dim:** never changed;
* **struck through:** ignored (BLE CRC, 802.11 FCS, sequence number, beacon timestamp).

**hide static** leaves only the messages that carry changing data. The view follows the
time cursor: with **follow** off, scrub or press **⟲ replay**, and the bytes light up as
the run plays.

Select a row to see the **bits** of its changing bytes as a flip-count heat map. Bits that
flipped in the latest packet are outlined, so a counter, a flag bit and a slowly moving
value look different. Click a byte (shift-click to extend to 4 bytes) to read the selection
as u8/i8, u16/i16 and u32/i32 in both byte orders. When the decoder knows those bytes, the
view names them (*decoder: temperature: 65.9 degC*). The chosen reading is plotted over
the whole run, which tells a counter from a set-point or a sensor value at a glance.

In **Packets**, the hex dump marks the bytes that differ from the previous packet of the
same message (hover for the old value).

### Reverse-engineering tools

* **Find the bytes that carry a signal** (Changes view, top of the side panel). After a
  run, pick any project signal, for example `sensor.temperature_c`, and press **Find**.
  Every 1-, 2- and 4-byte reading of every message (both byte orders, signed and
  unsigned) is correlated with the signal at the packet times. The best matches are listed
  with Pearson *r* and the least-squares fit `signal ≈ scale × raw + offset`.
  * In the example, `axis.position_mm` lands on CAN 0x182 bytes 2–5 (scale 0.001, µm →
    mm), `sensor.temperature_c` on 0x183 bytes 2–3 (×0.1) and `sensor.distance_mm` on
    0x183 bytes 0–1.
  * Clicking a match selects those bytes and that reading.
  * This is how simulated ground truth helps decode a protocol: on the bench you would log
    the physical value next to the bus.
* **Name these bytes…** adds the selection to the decoder as a named field: name, unit,
  and scale and offset, filled in from the fit. Fields live in `metadata.analyzer.fields`,
  keyed by message (`"CAN 0x183"`, `"BLE <address> <PDU type>"`, `"Wi-Fi <from> → <to>
  <type>"`). They are saved with the project:

  ```yaml
  fields:
    "CAN 0x183": [{name: heat, start: 2, reading: i16 LE, scale: 0.1, offset: 0, unit: degC}]
  ```

  The run is decoded again: every matching packet gets a *named fields* branch, and the
  values are appended to its summary.
* **DBC** writes a CAN database from the CANopen PDO layouts and the named CAN fields
  (Intel/Motorola byte order, signedness, scale, offset, range, unit), for SavvyCAN,
  cantools or a CAN tool chain.
* **Open capture…** loads traffic recorded from real hardware without running anything:
  * pcap or pcapng with SocketCAN (227), BLE LL (251, or 256 with PHDR) or 802.11 (105);
  * `candump -l` logs and `candump` console output (29-bit ids, remote frames).

  Times start at the first packet. CAN can be decoded as CANopen with the project's
  node names, or as plain CAN. Every view works on the capture: changes, bits, readings,
  plots, named fields, DBC. Correlation needs a run's signals.
* **Send…** puts frames into the running simulation:
  * enter a topic, a CAN id or a radio packet, data bytes, and optionally a period and
    repeat count;
  * **Stop all** or **×** on an entry ends a periodic sender;
  * in the packet view, **↻ Replay** sends the selected packet again, and **✎ Edit & send**
    copies it into the sender.

  Injected events go out at the next macro step with source `lab`. CAN frames go to the
  bus model's input (`metadata.analyzer.inject: {can: can.tx}`), so they are arbitrated
  and timed like any board's. Use it for replay tests, an NMT stop to a node, malformed
  SDOs, or flooding the bus to see the load and the latencies.

  `CoSimulationOrchestrator.inject(topic, payload)` does the same from scripts.

The API is `GET /api/analyzer?session=…&from=N` (decoded packets from packet N, in time
order, cached per run) and `GET /api/pcap?session=…&proto=can|ble|wifi`, plus POST
`analyzer/inject`, `analyzer/inject_stop`, `analyzer/import`, `analyzer/config` and
`analyzer/dbc`.

## Tests

`tests/mechatronics/test_comms.py` covers:

* frame lengths, CRC-15, arbitration order and back-to-back timing;
* a CANopen node through boot-up, expedited and segmented SDO, abort, NMT, RPDO and
  SYNC → TPDO;
* a master bringing up a network over the bus model;
* BLE build and decode with CRC errors;
* 802.11 beacons, UDP/JSON data, ACKs and FCS errors;
* the example (network up, EMCY, the BLE → Wi-Fi path, bus load);
* the decoded traffic of every kind, pcap headers, Renode-style CAN events, and the
  analyzer and pcap API;
* `tests/mechatronics/test_physical.py`:
  * path loss, the waterfall, RSSI and errors growing with distance;
  * the LoRa SF reach, the NFC and UHF limits, per-sender distances and walls;
  * cable twist and shield, the CAN length limits against the textbook table;
  * I2C capacitance, rise time and pull-up advice, the I2C decoder and boards;
  * the cable lab failing as wired and clean when fixed, the RF lab with a device moved
    out of range, and the check endpoint;
* `tests/mechatronics/test_rf.py`:
  * NFC (CRC_A, NDEF, the exchange), EM4100 and Gen2 (parity and CRC errors);
  * the LoRaWAN reference frame and time on air;
  * BLE CONNECT_IND, iBeacon and Eddystone, the four EAPOL messages, deauthentication
    and RSN;
  * the RF lab with every radio working;
  * medium strength, bit-error statistics, radio loss, signal and topic crosstalk, CAN
    retransmission and abandonment, a noisy CAN bus that keeps the network up, lost
    frames that reach no receiver, and the live medium API;
* candump, pcap and pcapng reading, named fields and DBC output, injection through the
  orchestrator (a node obeys an injected NMT stop), and the send, import and config
  endpoints.

The pcap layouts follow the link-type specifications. They have not been opened in
Wireshark here, and the BLE CRC-24 follows the Ubertooth convention without an outside
check.
