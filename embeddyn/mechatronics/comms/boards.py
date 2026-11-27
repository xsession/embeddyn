"""Virtual boards of the multi-board CANopen + BLE + Wi-Fi example (python plants).

```text
  master (node 1)        axis (node 2)        sensor (node 3)       gateway (node 4)
  NMT master, SYNC,      RPDO target,         TPDO distance/temp,   CANopen listener,
  SDO client, setpoint   TPDO status/pos      EMCY over-temperature BLE advertiser ──┐
  ─────────────────────────────── CAN 500 kbit/s (CanBus) ───────────────────────  │ BLE 37/38/39
                                                                                     ▼
                                     access point  ◀── 802.11 ch 6 ──  bridge (BLE scanner + Wi-Fi STA)
```

* `CanopenMaster` waits for the boot-ups, reads each node's device type and name
  over SDO (the name is a segmented upload), writes its heartbeat time, starts the
  network with NMT, sends SYNC and the axis set-point (RPDO1 of node 2) and watches
  heartbeats.
* `AxisNode` follows the set-point with a speed and acceleration limit and reports
  statusword and position on every SYNC, velocity and current every 5th.
* `SensorNode` reports distance (an input signal) and a heating temperature every
  20 ms and raises EMCY 0x4210 above `temp_limit`.
* `BleGateway` listens to the PDOs, heartbeats and EMCYs and advertises a status
  record (ADV_NONCONN_IND, manufacturer data) on the three advertising channels.
* `WifiBridge` scans one advertising channel at a time, joins the access point
  (probe, open authentication, association) and sends the latest status as UDP JSON.
* `AccessPoint` sends beacons, answers the join and acknowledges unicast frames.
"""
from __future__ import annotations

import json
import math
import struct
from typing import Any, Mapping, Sequence

from . import ble, wifi
from .canopen import CanopenNode, Entry, Pdo


def _lcg(seed: int) -> int:
    return (seed * 1103515245 + 12345) & 0x7FFFFFFF


class CanopenMaster(CanopenNode):
    inputs = ("target_mm",)
    outputs = ("nmt_state", "nodes_operational", "axis_position_mm", "temperature_c", "nodes_lost")
    device_type = 0x00000000
    device_name = "embeddyn master"

    def __init__(self, node_id: int = 1, nodes: Sequence[int] = (2, 3, 4), sync_ms: float = 10.0,
                 heartbeat_ms: float = 100.0, boot_wait: float = 0.03, name: str = "master") -> None:
        super().__init__(node_id, heartbeat_ms=heartbeat_ms, boot_delay=0.001, name=name)
        self.nodes = [int(n) for n in nodes]
        self.sync_period = float(sync_ms) / 1000.0
        self.boot_wait = float(boot_wait)
        self.booted: set[int] = set()
        self.heartbeats: dict[int, tuple[float, int]] = {}
        self.sdo_queue: list[tuple[int, str, int, int, int | None]] = []
        self.sdo_busy: tuple[int, str, int, int, int | None] | None = None
        self.sdo_sent_at = 0.0
        self.segment: dict[str, Any] | None = None
        self.info: dict[int, dict[str, Any]] = {}
        self.phase = "boot"
        self.next_sync = None
        self.target_um = 0
        self.position_mm = 0.0
        self.temperature_c = 0.0
        self.lost: set[int] = set()
        self.od[(0x1006, 0)] = Entry(int(sync_ms * 1000), "u32", "rw", "communication cycle period")
        self.od[(0x2000, 0)] = Entry(0, "i32", "rw", "axis target (um)")
        self.od[(0x2001, 0)] = Entry(0x000F, "u16", "rw", "axis controlword")
        self.tpdos = []

    def application(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        self.target_um = int(round(float(u.get("target_mm", 0.0)) * 1000))
        if self.phase == "boot" and t1 >= self.boot_wait and (set(self.nodes) <= self.booted or t1 >= 4 * self.boot_wait):
            for node in self.nodes:
                self.sdo_queue += [(node, "read", 0x1000, 0, None), (node, "read", 0x1008, 0, None),
                                   (node, "write", 0x1017, 0, int(self.od[(0x1017, 0)].value))]
            self.phase = "configure"
        if self.phase == "configure":
            if self.sdo_busy is None and self.sdo_queue:
                self._sdo_next(t1)
            elif self.sdo_busy is not None and t1 - self.sdo_sent_at > 0.05:     # SDO timeout: skip
                self.sdo_busy, self.segment = None, None
            elif self.sdo_busy is None and not self.sdo_queue:
                self.send(t1, 0x000, bytes([0x01, 0x00]))                      # NMT start all
                self.nmt(t1, 0x01)
                self.phase = "run"
                self.next_sync = t1 + self.sync_period
        if self.phase == "run":
            while self.next_sync is not None and self.next_sync <= t1:
                at = self.next_sync
                self.send(at, 0x080)                                           # SYNC
                self.od[(0x2000, 0)].value = self.target_um
                self.send(at + 50e-6, 0x200 + 2, struct.pack("<iH", self.target_um, self.od[(0x2001, 0)].value))
                self.next_sync += self.sync_period
            for node, (seen, _) in self.heartbeats.items():
                timeout = 3 * self.od[(0x1017, 0)].value / 1000.0
                if t1 - seen > timeout and node not in self.lost:
                    self.lost.add(node)
                    self.emcy(t1, 0x8130, 0x11, bytes([node]))

    def _sdo_next(self, t: float) -> None:
        node, action, index, sub, value = self.sdo_busy = self.sdo_queue.pop(0)
        self.sdo_sent_at = t
        if action == "read":
            self.send(t, 0x600 + node, bytes([0x40]) + struct.pack("<HB", index, sub) + b"\0\0\0\0")
        else:
            self.send(t, 0x600 + node, bytes([0x2B]) + struct.pack("<HBH", index, sub, int(value)) + b"\0\0")

    def on_frame(self, t: float, cob_id: int, data: bytes) -> None:
        node = cob_id & 0x7F
        if cob_id & 0x780 == 0x700 and data:
            if data[0] == 0:
                self.booted.add(node)
            self.heartbeats[node] = (t, data[0])
            if node in self.lost:
                self.lost.discard(node)
        elif self.sdo_busy and cob_id == 0x580 + self.sdo_busy[0] and len(data) == 8:
            self._sdo_response(t, data)
        elif cob_id == 0x182 and len(data) >= 6:
            self.position_mm = struct.unpack_from("<i", data, 2)[0] / 1000.0
        elif cob_id == 0x183 and len(data) >= 4:
            self.temperature_c = struct.unpack_from("<h", data, 2)[0] / 10.0

    def _sdo_response(self, t: float, data: bytes) -> None:
        node, action, index, _, _ = self.sdo_busy
        info = self.info.setdefault(node, {})
        command = data[0]
        if command == 0x80:
            info[f"0x{index:04X}"] = f"abort 0x{struct.unpack_from('<I', data, 4)[0]:08X}"
        elif self.segment is not None:                                         # upload segment
            size = 7 - ((command >> 1) & 7)
            self.segment["data"] += data[1:1 + size]
            if not command & 1:
                self.segment["toggle"] ^= 1
                self.send(t, 0x600 + node, bytes([0x60 | (self.segment["toggle"] << 4)]) + b"\0" * 7)
                self.sdo_sent_at = t
                return
            info[f"0x{index:04X}"] = self.segment["data"].decode(errors="replace")
            self.segment = None
        elif command >> 5 == 2 and not command & 0x02:                         # segmented upload follows
            self.segment = {"data": b"", "toggle": 0}
            self.send(t, 0x600 + node, bytes([0x60]) + b"\0" * 7)
            self.sdo_sent_at = t
            return
        elif command >> 5 == 2:
            size = 4 - ((command >> 2) & 3) if command & 1 else 4
            info[f"0x{index:04X}"] = int.from_bytes(data[4:4 + size], "little")
        self.sdo_busy = None
        if self.sdo_queue:
            self._sdo_next(t + 200e-6)

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        operational = sum(1 for node, (_, state) in self.heartbeats.items() if state == 0x05 and node not in self.lost)
        return {"nmt_state": float(self.state if self.state is not None else -1),
                "nodes_operational": float(operational), "axis_position_mm": self.position_mm,
                "temperature_c": self.temperature_c, "nodes_lost": float(len(self.lost))}


class AxisNode(CanopenNode):
    outputs = ("nmt_state", "position_mm", "velocity_mm_s", "current_a")
    device_type = 0x00020192           # CiA 402 drive, servo
    device_name = "embeddyn linear axis"

    def __init__(self, node_id: int = 2, max_speed: float = 200.0, acceleration: float = 2000.0,
                 heartbeat_ms: float = 100.0, boot_delay: float = 0.004, name: str = "axis") -> None:
        super().__init__(node_id, heartbeat_ms=heartbeat_ms, boot_delay=boot_delay, name=name)
        self.max_speed, self.acceleration = float(max_speed), float(acceleration)
        self.position = 0.0                 # mm
        self.velocity = 0.0                 # mm/s
        self.current = 0.0
        self.od.update({
            (0x6040, 0): Entry(0, "u16", "rw", "controlword"), (0x6041, 0): Entry(0x0250, "u16", "ro", "statusword"),
            (0x6064, 0): Entry(0, "i32", "ro", "position actual (um)"), (0x606C, 0): Entry(0, "i32", "ro", "velocity actual (um/s)"),
            (0x6078, 0): Entry(0, "i16", "ro", "current actual (mA)"), (0x607A, 0): Entry(0, "i32", "rw", "target position (um)"),
        })
        self.rpdos = [Pdo(0x200 + node_id, [(0x607A, 0), (0x6040, 0)])]
        self.tpdos = [Pdo(0x180 + node_id, [(0x6041, 0), (0x6064, 0)], sync_every=1),
                      Pdo(0x280 + node_id, [(0x606C, 0), (0x6078, 0)], sync_every=5)]

    def application(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        enabled = self.state == 0x05 and (self.od[(0x6040, 0)].value & 0x0F) == 0x0F
        target = self.od[(0x607A, 0)].value / 1000.0
        steps = max(1, int(math.ceil((t1 - t0) / 1e-4)))
        h = (t1 - t0) / steps
        for _ in range(steps):
            if enabled:
                error = target - self.position
                stop = self.velocity ** 2 / (2 * self.acceleration)
                wanted = math.copysign(self.max_speed, error) if abs(error) > stop + 0.01 else 0.0
                if abs(error) < 0.01 and abs(self.velocity) < 1.0:
                    wanted, self.velocity = 0.0, 0.0
                dv = max(-self.acceleration * h, min(self.acceleration * h, wanted - self.velocity))
                accel = dv / h if h else 0.0
            else:
                dv = max(-self.acceleration * h, min(self.acceleration * h, -self.velocity))
                accel = dv / h if h else 0.0
            self.velocity += dv
            self.position += self.velocity * h
            self.current = 0.2 + 0.0012 * abs(accel) + 0.0005 * abs(self.velocity)
        reached = abs(target - self.position) < 0.05
        self.od[(0x6041, 0)].value = (0x0237 if enabled else 0x0250) | (0x0400 if reached else 0)
        self.od[(0x6064, 0)].value = int(round(self.position * 1000))
        self.od[(0x606C, 0)].value = int(round(self.velocity * 1000))
        self.od[(0x6078, 0)].value = int(round(self.current * 1000))

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        return {"nmt_state": float(self.state if self.state is not None else -1), "position_mm": self.position,
                "velocity_mm_s": self.velocity, "current_a": self.current}


class SensorNode(CanopenNode):
    inputs = ("distance_mm",)
    outputs = ("nmt_state", "temperature_c", "overtemp")
    device_type = 0x00040194           # CiA 404 measuring device
    device_name = "embeddyn distance+temp sensor"

    def __init__(self, node_id: int = 3, period_ms: float = 20.0, temp_start: float = 25.0, temp_rise: float = 45.0,
                 temp_tau: float = 0.8, temp_limit: float = 60.0, heartbeat_ms: float = 100.0,
                 boot_delay: float = 0.006, name: str = "sensor") -> None:
        super().__init__(node_id, heartbeat_ms=heartbeat_ms, boot_delay=boot_delay, name=name)
        self.temp_start, self.temp_rise, self.temp_tau, self.temp_limit = map(float, (temp_start, temp_rise, temp_tau, temp_limit))
        self.temperature = self.temp_start
        self.overtemp = False
        self.od.update({(0x6000, 0): Entry(0, "u16", "ro", "distance (mm)"),
                        (0x6001, 0): Entry(0, "i16", "ro", "temperature (0.1 degC)")})
        self.tpdos = [Pdo(0x180 + node_id, [(0x6000, 0), (0x6001, 0)], event_timer=float(period_ms) / 1000.0)]

    def application(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        self.temperature = self.temp_start + self.temp_rise * (1 - math.exp(-t1 / self.temp_tau))
        self.od[(0x6000, 0)].value = max(0, min(65535, int(round(float(u.get("distance_mm", 0.0))))))
        self.od[(0x6001, 0)].value = int(round(self.temperature * 10))
        if self.state == 0x05:
            if not self.overtemp and self.temperature > self.temp_limit:
                self.overtemp = True
                self.emcy(t1, 0x4210, 0x09, struct.pack("<h", int(self.temperature * 10)))
            elif self.overtemp and self.temperature < self.temp_limit - 2:
                self.overtemp = False
                self.emcy(t1, 0x0000)

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        return {"nmt_state": float(self.state if self.state is not None else -1), "temperature_c": self.temperature,
                "overtemp": float(self.overtemp)}


STATUS_COMPANY = 0xFFFF


def status_record(version: int, nodes: int, position_um: int, temp_decic: int, distance_mm: int, flags: int,
                  counter: int) -> bytes:
    return struct.pack("<HBBihHBB", STATUS_COMPANY, version, nodes, position_um, temp_decic, distance_mm, flags, counter)


class BleGateway(CanopenNode):
    outputs = ("nmt_state", "adv_count")
    event_outputs = ("can_tx", "ble")
    device_type = 0x00000000
    device_name = "embeddyn BLE gateway"

    def __init__(self, node_id: int = 4, address: str = "C0:FF:EE:00:00:04", local_name: str = "EMB-GW",
                 interval_ms: float = 100.0, rssi: float = -62.0, heartbeat_ms: float = 100.0,
                 boot_delay: float = 0.008, name: str = "gateway") -> None:
        super().__init__(node_id, heartbeat_ms=heartbeat_ms, boot_delay=boot_delay, name=name)
        self.address, self.local_name = address, local_name
        self.interval = float(interval_ms) / 1000.0
        self.rssi = float(rssi)
        self.next_adv = 0.05
        self.seed = 4
        self.counter = 0
        self.status = {"position_um": 0, "temp": 0, "distance": 0, "states": {}, "emcy": False}

    def on_frame(self, t: float, cob_id: int, data: bytes) -> None:
        node = cob_id & 0x7F
        if cob_id == 0x182 and len(data) >= 6:
            self.status["position_um"] = struct.unpack_from("<i", data, 2)[0]
        elif cob_id == 0x183 and len(data) >= 4:
            self.status["distance"], self.status["temp"] = struct.unpack_from("<Hh", data)
        elif cob_id & 0x780 == 0x700 and data:
            self.status["states"][node] = data[0]
        elif cob_id & 0x780 == 0x080 and node and len(data) >= 2:
            self.status["emcy"] = struct.unpack_from("<H", data)[0] != 0

    def application(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        while self.next_adv <= t1:
            at = self.next_adv
            states = self.status["states"]
            operational = sum(1 << (node - 1) for node, state in states.items() if state == 0x05 and 1 <= node <= 8)
            if self.state == 0x05:
                operational |= 1 << (self.node_id - 1)
            flags = int(self.status["emcy"])
            record = status_record(1, operational, self.status["position_um"], self.status["temp"],
                                   self.status["distance"], flags, self.counter & 0xFF)
            adv = ble.ad(0x01, b"\x06") + ble.ad(0x09, self.local_name.encode()) + ble.ad(0xFF, record)
            packet = ble.adv_packet(self.address, adv)
            air = (len(packet) + 1) * 8e-6                   # 1 Mbit/s, preamble included
            for k, channel in enumerate((37, 38, 39)):
                self._out.append((at + k * (air + 150e-6), "ble", {"proto": "ble", "channel": channel, "rssi": self.rssi,
                                                                   "data": packet.hex(), "src": self.name}))
            self.counter += 1
            self.seed = _lcg(self.seed)
            self.next_adv = at + self.interval + (self.seed % 10_000) * 1e-6     # advDelay 0..10 ms

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        return {"nmt_state": float(self.state if self.state is not None else -1), "adv_count": float(self.counter)}


class WifiBridge:
    """BLE scanner + Wi-Fi station that forwards the gateway's status as UDP JSON."""

    inputs = ()
    outputs = ("wifi_state", "ble_seen", "udp_sent", "temperature_c")
    states = ()
    event_inputs = ("ble", "wifi_rx")
    event_outputs = ("wifi_tx",)
    STATES = ("idle", "probing", "authenticating", "associating", "connected")

    def __init__(self, address: str = "24:0A:C4:00:00:05", ssid: str = "embeddyn-lab", channel: int = 6,
                 ip: str = "192.168.4.2", server_ip: str = "192.168.4.1", port: int = 5000,
                 scan_window_ms: float = 30.0, sensitivity: float = -90.0, report_ms: float = 250.0,
                 join_at: float = 0.05, rssi: float = -48.0, company: int = STATUS_COMPANY) -> None:
        self.address, self.ssid, self.channel = address, ssid, int(channel)
        self.ip, self.server_ip, self.port = ip, server_ip, int(port)
        self.scan_window = float(scan_window_ms) / 1000.0
        self.sensitivity = float(sensitivity)
        self.report = float(report_ms) / 1000.0
        self.join_at, self.rssi, self.company = float(join_at), float(rssi), int(company)
        self.state = 0
        self.bssid: str | None = None
        self.sequence = 0
        self.seen = 0
        self.sent = 0
        self.latest: dict[str, Any] | None = None
        self.next_report = None
        self.ident = 0
        self._out: list[tuple[float, str, Any]] = []

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        return []

    def scan_channel(self, t: float) -> int:
        return 37 + int(t / self.scan_window) % 3

    def _tx(self, t: float, frame_bytes: bytes) -> None:
        self._out.append((t, "wifi_tx", {"proto": "wifi", "channel": self.channel, "rssi": self.rssi,
                                         "data": frame_bytes.hex(), "src": "bridge"}))
        self.sequence += 1

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        data = bytes.fromhex(str(payload.get("data", "")))
        if port == "ble":
            if payload.get("channel") != self.scan_channel(t) or float(payload.get("rssi", -200)) < self.sensitivity:
                return                                   # listening on another channel, or too weak
            if ble.crc24(data[4:-3]) != data[-3:]:
                return
            self.seen += 1
            record = _manufacturer(data, self.company)
            if record is not None and len(record) >= 12:
                version, nodes, pos, temp, dist, flags, counter = struct.unpack_from("<BBihHBB", record)
                self.latest = {"pos_mm": round(pos / 1000, 3), "temp_c": temp / 10, "dist_mm": dist,
                               "nodes": f"0x{nodes:02X}", "flags": flags, "cnt": counter,
                               "rssi": int(payload.get("rssi", 0))}
        elif port == "wifi_rx" and payload.get("src") != "bridge":
            decoded = wifi.decode(data)
            if decoded.get("target") not in (self.address, wifi.BROADCAST) and \
                    wifi.mac_text(data[4:10]) not in (self.address, wifi.BROADCAST):
                return
            kind = decoded["type"]
            if kind == "Probe Response" and self.state == 1:
                self.bssid = wifi.mac_text(data[10:16])
                self.state = 2
                self._tx(t + 300e-6, wifi.frame(0, 11, self.bssid, self.address, self.bssid,
                                                 struct.pack("<HHH", 0, 1, 0), sequence=self.sequence))
            elif kind == "Authentication" and self.state == 2:
                self.state = 3
                body = struct.pack("<HH", 0x0401, 10) + wifi.element(0, self.ssid.encode()) + \
                    wifi.element(1, bytes([0x82, 0x84, 0x8B, 0x96]))
                self._tx(t + 300e-6, wifi.frame(0, 0, self.bssid, self.address, self.bssid, body, sequence=self.sequence))
            elif kind == "Association Response" and self.state == 3:
                self.state = 4
                self.next_report = t + 0.02

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        if self.state == 0 and t1 >= self.join_at:
            self.state = 1
            body = wifi.element(0, self.ssid.encode()) + wifi.element(1, bytes([0x82, 0x84, 0x8B, 0x96]))
            self._tx(max(t0, self.join_at), wifi.frame(0, 4, wifi.BROADCAST, self.address, wifi.BROADCAST, body,
                                                       sequence=self.sequence))
        while self.state == 4 and self.next_report is not None and self.next_report <= t1:
            at = self.next_report
            if self.latest is not None:
                payload = json.dumps(self.latest, separators=(",", ":")).encode()
                packet = wifi.ipv4_udp(self.ip, self.server_ip, 40000, self.port, payload, ident=self.ident)
                self.ident += 1
                self._tx(at, wifi.frame(2, 0, self.bssid, self.address, "02:00:00:00:00:01",
                                        wifi.llc_snap(0x0800, packet), to_ds=True, sequence=self.sequence, duration=44))
                self.sent += 1
            self.next_report = at + self.report

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        return {"wifi_state": float(self.state), "ble_seen": float(self.seen), "udp_sent": float(self.sent),
                "temperature_c": float((self.latest or {}).get("temp_c", 0.0))}

    def pop_events(self) -> list[tuple[float, str, Any]]:
        out, self._out = self._out, []
        return out


def _manufacturer(packet: bytes, company: int) -> bytes | None:
    pos, end = 12, 6 + packet[5]
    while pos < end:
        length = packet[pos]
        if length == 0:
            break
        if packet[pos + 1] == 0xFF and length >= 3 and struct.unpack_from("<H", packet, pos + 2)[0] == company:
            return packet[pos + 4:pos + 1 + length]
        pos += 1 + length
    return None


class AccessPoint:
    """Open 802.11 access point: beacons, probe/auth/association responses, ACKs."""

    inputs = ()
    outputs = ("stations",)
    states = ()
    event_inputs = ("wifi_rx",)
    event_outputs = ("wifi_tx",)

    def __init__(self, bssid: str = "02:00:00:00:00:01", ssid: str = "embeddyn-lab", channel: int = 6,
                 beacon_tu: int = 100, rssi: float = -40.0) -> None:
        self.bssid, self.ssid, self.channel = bssid, ssid, int(channel)
        self.beacon_period = beacon_tu * 1.024e-3
        self.beacon_tu = int(beacon_tu)
        self.rssi = float(rssi)
        self.next_beacon = 0.0
        self.sequence = 0
        self.stations: dict[str, int] = {}
        self._out: list[tuple[float, str, Any]] = []

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        return []

    def _tx(self, t: float, frame_bytes: bytes) -> None:
        self._out.append((t, "wifi_tx", {"proto": "wifi", "channel": self.channel, "rssi": self.rssi,
                                         "data": frame_bytes.hex(), "src": "ap"}))
        self.sequence += 1

    def receive(self, t: float, port: str, payload: Any) -> None:
        if port != "wifi_rx" or not isinstance(payload, dict) or payload.get("src") == "ap":
            return
        data = bytes.fromhex(str(payload.get("data", "")))
        if len(data) < 16 or wifi.fcs(data[:-4]) != data[-4:]:
            return
        control = struct.unpack_from("<H", data)[0]
        kind, subtype = (control >> 2) & 3, (control >> 4) & 15
        receiver, sender = wifi.mac_text(data[4:10]), wifi.mac_text(data[10:16])
        if receiver not in (self.bssid, wifi.BROADCAST):
            return
        if receiver == self.bssid:
            self._tx(t + 16e-6, wifi.frame(1, 13, sender))                     # ACK after SIFS
        if kind == 0 and subtype == 4:
            body = struct.pack("<QHH", int(t * 1e6), self.beacon_tu, 0x0401) + wifi.element(0, self.ssid.encode()) + \
                wifi.element(1, bytes([0x82, 0x84, 0x8B, 0x96])) + wifi.element(3, bytes([self.channel]))
            self._tx(t + 400e-6, wifi.frame(0, 5, sender, self.bssid, self.bssid, body, sequence=self.sequence))
        elif kind == 0 and subtype == 11:
            self._tx(t + 400e-6, wifi.frame(0, 11, sender, self.bssid, self.bssid, struct.pack("<HHH", 0, 2, 0),
                                            sequence=self.sequence))
        elif kind == 0 and subtype == 0:
            aid = self.stations.setdefault(sender, len(self.stations) + 1)
            body = struct.pack("<HHH", 0x0401, 0, 0xC000 | aid) + wifi.element(1, bytes([0x82, 0x84, 0x8B, 0x96]))
            self._tx(t + 400e-6, wifi.frame(0, 1, sender, self.bssid, self.bssid, body, sequence=self.sequence))

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        while self.next_beacon <= t1:
            self._tx(self.next_beacon, wifi.beacon(self.bssid, self.ssid, self.channel, self.sequence,
                                                   int(self.next_beacon * 1e6), self.beacon_tu))
            self.next_beacon += self.beacon_period

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        return {"stations": float(len(self.stations))}

    def pop_events(self) -> list[tuple[float, str, Any]]:
        out, self._out = self._out, []
        return out
