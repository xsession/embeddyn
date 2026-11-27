"""Communication buses and radios for multi-board co-simulation, and their decoders.

* `can`: classic CAN frames with exact bit lengths (stuffing, CRC-15) and `CanBus`,
  a python plant that arbitrates frames from several boards and puts them on the wire.
* `canopen`: a small CANopen stack (NMT, heartbeat, SYNC, PDO, expedited and
  segmented SDO, EMCY) for virtual boards, and a CANopen decoder.
* `ble`: Bluetooth LE advertising packets (access address, PDU, CRC-24) and AD structures.
* `wifi`: IEEE 802.11 management, data and control frames with FCS, LLC/SNAP, IPv4, UDP.
* `analyzer`: turns bus and radio events into decoded packets (summary, field tree with
  byte ranges, hex) for the Lab's analyzer, and writes pcap files Wireshark opens.
* `boards`: the virtual boards of examples/mechatronics/multiboard-canopen-ble.yaml.
* `serial`: a bit-level UART line model, NMEA and Modbus RTU, GPS and Modbus boards (UART, RS-485).
* `spi`: SPI register devices, a LIS3DH-style accelerometer and its master.
* `ethernet`: Ethernet II, ARP, IPv4, UDP, TCP, Modbus/TCP, a PLC and a remote I/O.
* `faults`: what goes wrong on wired buses (baud errors, contention, SPI modes, stuck I2C,
  duplex mismatch...), applied by `medium`.

Boards talk through event ports of python plants (`events:` / `subscribe:` options):
a board publishes `{"proto": "can", "id": ..., "data": "hex"}` on a topic, the
orchestrator hands it to every instance subscribed to that topic.
"""
