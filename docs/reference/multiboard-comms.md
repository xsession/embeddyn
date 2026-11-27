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
* candump, pcap and pcapng reading, named fields and DBC output, injection through the
  orchestrator (a node obeys an injected NMT stop), and the send, import and config
  endpoints.

The pcap layouts follow the link-type specifications. They have not been opened in
Wireshark here, and the BLE CRC-24 follows the Ubertooth convention without an outside
check.
