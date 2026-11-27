"""Virtual radios of the RF lab example (examples/mechatronics/rf-lab.yaml), as python plants.

* `NfcReader` / `Type2Tag`: ISO 14443-A polling, two-level anticollision and select of
  a 7-byte UID, READ of the NTAG memory until the NDEF TLV ends, HLTA. The tag answers
  only while `present` > 0.5.
* `Em4100Card`: a 125 kHz card in front of a reader repeats its 64-bit ID (RF/64: 32.8 ms).
* `Gen2Reader` / `Gen2Tags`: UHF inventory rounds (Query, QueryRep, RN16, ACK, EPC) with
  slotted ALOHA, so tags that pick the same slot collide and wait for the next round.
* `LoraNode` / `LoraGateway`: LoRaWAN uplinks (AES-encrypted sensor record on port 2,
  every third one confirmed) and the gateway's downlinks (ACK, LinkADRReq once).
* `BleBeacon`: iBeacon and Eddystone-UID/TLM adverts. `BleSensor` / `BlePhone`:
  connectable adverts with Environmental Sensing data, active scanning (SCAN_REQ /
  SCAN_RSP), then CONNECT_IND.
* `WpaAccessPoint` / `WpaStation` / `Deauther`: WPA2-PSK beacons with RSN, open
  authentication, association, the EAPOL 4-way handshake, encrypted (CCMP) data, and a
  spoofed deauthentication that makes the station join again.
"""
from __future__ import annotations

import random
import struct
from typing import Any, Mapping, Sequence

from . import ble, lora, nfc, rfid, wifi


class _Plant:
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    states = ()

    def __init__(self) -> None:
        self._out: list[tuple[float, str, Any]] = []

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        return []

    def pop_events(self) -> list[tuple[float, str, Any]]:
        out, self._out = self._out, []
        return out

    def emit(self, t: float, port: str, payload: dict[str, Any]) -> None:
        self._out.append((float(t), port, payload))


# -- NFC ----------------------------------------------------------------------------------------------------

class NfcReader(_Plant):
    outputs = ("tags_read",)
    event_inputs = ("rx",)
    event_outputs = ("tx",)

    def __init__(self, poll_ms: float = 200.0, name: str = "reader") -> None:
        super().__init__()
        self.poll, self.name = poll_ms / 1000.0, name
        self.next_poll = 0.01
        self.step = "idle"
        self.page = 0
        self.uid = b""
        self.memory = b""
        self.reads = 0
        self.timeout = None

    def _send(self, t: float, data: bytes, bits: int | None = None) -> None:
        payload = {"proto": "nfc", "dir": "pcd", "data": data.hex(), "src": self.name}
        if bits:
            payload["bits"] = bits
        self.emit(t, "tx", payload)
        self.timeout = t + 0.003

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("dir") != "picc":
            return
        data = bytes.fromhex(payload["data"])
        if len(data) in (3, 18) and nfc.crc_a(data[:-2]) != data[-2:]:
            return                                         # corrupted SAK / READ data: the reader times out, polls again
        if len(data) == 5 and nfc.bcc(data[:4]) != data[4]:
            return
        at = t + 100e-6                                    # frame delay time, then the next command
        if self.step == "reqa" and len(data) == 2:
            self.step = "ac1"
            self._send(at, b"\x93\x20")
        elif self.step == "ac1" and len(data) == 5:
            self.uid = data[1:4] if data[0] == 0x88 else data[:4]
            self.cl1 = data
            self.step = "sel1"
            self._send(at, nfc.with_crc(b"\x93\x70" + data))
        elif self.step == "sel1" and len(data) == 3:
            if data[0] & 0x04:
                self.step = "ac2"
                self._send(at, b"\x95\x20")
            else:
                self._start_read(at)
        elif self.step == "ac2" and len(data) == 5:
            self.uid += data[:4]
            self.step = "sel2"
            self._send(at, nfc.with_crc(b"\x95\x70" + data))
        elif self.step == "sel2" and len(data) == 3:
            self._start_read(at)
        elif self.step == "read" and len(data) == 18:
            self.memory += data[:16]
            if nfc.ndef_from_memory(self.memory) is not None or self.page >= 40:     # NDEF TLV complete
                self.reads += 1
                self.step = "idle"
                self._send(at, nfc.with_crc(b"\x50\x00"))       # HLTA: no answer expected
                self.timeout = None
            else:
                self.page += 4
                self._send(at, nfc.with_crc(bytes([0x30, self.page])))

    def _start_read(self, t: float) -> None:
        self.step, self.page, self.memory = "read", 0, b""
        self._send(t, nfc.with_crc(b"\x30\x00"))

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        if self.timeout is not None and t1 > self.timeout:       # no answer: back to polling
            self.step, self.timeout = "idle", None
        if self.step == "idle" and t1 >= self.next_poll:
            self.step = "reqa"
            self._send(self.next_poll, b"\x26", bits=7)
            self.next_poll += self.poll

    def output(self, t, x, u):
        return {"tags_read": float(self.reads)}


class Type2Tag(_Plant):
    inputs = ("present",)
    outputs = ("in_field",)
    event_inputs = ("rx",)
    event_outputs = ("tx",)

    def __init__(self, uid: str = "04A2244A8B3C80", uri: str = "https://embeddyn.dev/rf-lab", name: str = "ntag213") -> None:
        super().__init__()
        self.uid = bytes.fromhex(uid)
        self.memory = nfc.type2_memory(self.uid, nfc.ndef_uri(uri))
        self.name = name
        self.state = "idle"
        self.present = False

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not self.present or not isinstance(payload, dict) or payload.get("dir") != "pcd":
            return
        data = bytes.fromhex(payload["data"])
        bad = (len(data) >= 3 and data[0] not in (0x93, 0x95) and nfc.crc_a(data[:-2]) != data[-2:]) or \
            (len(data) == 9 and data[0] in (0x93, 0x95) and nfc.crc_a(data[:7]) != data[7:])
        if bad or (payload.get("bits") != 7 and data[:1] not in (b"\x93", b"\x95", b"\x30", b"\x50", b"\xa2")):
            if self.state != "halt":                       # ISO 14443-3: an invalid frame sends the tag back to IDLE
                self.state = "idle"
            return
        if payload.get("bits") == 7 and data and data[0] not in (0x26, 0x52):
            if self.state != "halt":
                self.state = "idle"
            return
        at = t + 90e-6

        def answer(reply: bytes) -> None:
            self.emit(at, "tx", {"proto": "nfc", "dir": "picc", "data": reply.hex(), "src": self.name, "peer": payload.get("src")})

        if payload.get("bits") == 7 and data and data[0] in (0x26, 0x52):
            if self.state == "idle" or (data[0] == 0x52):
                self.state = "ready1"
                answer(b"\x44\x00")                              # ATQA: double-size UID, Type 2
        elif data[:2] == b"\x93\x20" and self.state == "ready1":
            cl1 = bytes([0x88]) + self.uid[:3]
            answer(cl1 + bytes([nfc.bcc(cl1)]))
        elif data[:2] == b"\x93\x70" and self.state == "ready1":
            self.state = "ready2"
            answer(nfc.with_crc(b"\x04"))                        # SAK: UID not complete
        elif data[:2] == b"\x95\x20" and self.state == "ready2":
            cl2 = self.uid[3:7]
            answer(cl2 + bytes([nfc.bcc(cl2)]))
        elif data[:2] == b"\x95\x70" and self.state == "ready2":
            self.state = "active"
            answer(nfc.with_crc(b"\x00"))                        # SAK: Type 2 tag
        elif data[:1] == b"\x30" and self.state == "active" and len(data) == 4:
            page = data[1]
            chunk = (self.memory + self.memory)[page * 4:page * 4 + 16]
            answer(nfc.with_crc(chunk))
        elif data[:2] == b"\x50\x00":
            self.state = "halt"

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        present = float(u.get("present", 1.0)) > 0.5
        if present and not self.present:
            self.state = "idle"
        self.present = present

    def output(self, t, x, u):
        return {"in_field": float(self.present)}


# -- 125 kHz and UHF RFID ------------------------------------------------------------------------------------

class Em4100Card(_Plant):
    inputs = ("present",)
    outputs = ("frames",)
    event_outputs = ("tx",)

    def __init__(self, version: int = 0x1A, card: int = 0x00B4C7E2, name: str = "em4100") -> None:
        super().__init__()
        self.bits = rfid.em4100_bits(version, card)
        self.name = name
        self.period = 64 * 64 / 125_000.0                      # 64 bits at RF/64
        self.next = None
        self.count = 0

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        if float(u.get("present", 1.0)) <= 0.5:
            self.next = None
            return
        if self.next is None:
            self.next = t0 + 0.002                              # power-up
        while self.next <= t1:
            data, count = rfid.bits_to_bytes(self.bits)
            self.emit(self.next, "tx", {"proto": "rfid", "air": "em4100", "dir": "tag", "data": data, "bits": count,
                                        "src": self.name, "peer": "125k reader"})
            self.count += 1
            self.next += self.period

    def output(self, t, x, u):
        return {"frames": float(self.count)}


class Gen2Reader(_Plant):
    outputs = ("tags_seen", "rounds")
    event_inputs = ("rx",)
    event_outputs = ("tx",)

    def __init__(self, q: int = 2, pause_ms: float = 150.0, name: str = "uhf reader") -> None:
        super().__init__()
        self.q, self.pause, self.name = int(q), pause_ms / 1000.0, name
        self.next_round = 0.02
        self.slot = None
        self.waiting = None
        self.seen: set[str] = set()
        self.rounds = 0
        self.replies: list[str] = []

    def _send(self, t: float, bits: str) -> None:
        data, count = rfid.bits_to_bytes(bits)
        self.emit(t, "tx", {"proto": "rfid", "air": "gen2", "dir": "reader", "data": data, "bits": count, "src": self.name})
        self.waiting = t + 0.0008

    def receive(self, t: float, port: str, payload: Any) -> None:
        if isinstance(payload, dict) and payload.get("air") == "gen2" and payload.get("dir") == "tag":
            self.replies.append(rfid.bytes_to_bits(payload["data"], int(payload["bits"])))
            if payload.get("epc"):
                self.seen.add(payload["epc"])

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        if self.slot is None:
            if t1 >= self.next_round:
                self.slot, self.rounds = 0, self.rounds + 1
                self.replies = []
                self._send(self.next_round, rfid.gen2_query(self.q))
            return
        if self.waiting is not None and t1 < self.waiting:
            return
        replies, self.replies = self.replies, []
        rn16 = [r for r in replies if len(r) == 16]
        if len(rn16) == 1:                                       # one tag in this slot: acknowledge it
            self._send(t1, rfid.gen2_ack(int(rn16[0], 2)))
            return
        if len(rn16) > 1:                                        # collision: show it, move on
            self.emit(t1 - 0.0002, "tx", {"proto": "rfid", "air": "gen2", "dir": "tag", "data": "", "bits": 0,
                                          "src": f"{len(rn16)} tags", "peer": self.name})
        self.slot += 1
        if self.slot >= 2 ** self.q:
            self.slot, self.waiting = None, None
            self.next_round = t1 + self.pause
            return
        self._send(t1, rfid.gen2_query_rep())

    def output(self, t, x, u):
        return {"tags_seen": float(len(self.seen)), "rounds": float(self.rounds)}


class Gen2Tags(_Plant):
    outputs = ("inventoried",)
    event_inputs = ("rx",)
    event_outputs = ("tx",)

    def __init__(self, epcs: Sequence[str] = ("E28011606000020A1B2C3D01", "E28011606000020A1B2C3D02", "E28011606000020A1B2C3D03"),
                 seed: int = 7) -> None:
        super().__init__()
        self.epcs = [bytes.fromhex(e) for e in epcs]
        self.random = random.Random(seed)
        self.slots = [None] * len(self.epcs)
        self.rn = [0] * len(self.epcs)
        self.done = [False] * len(self.epcs)
        self.inventoried = 0

    def _reply(self, t: float, index: int, bits: str, epc: str | None = None) -> None:
        data, count = rfid.bits_to_bytes(bits)
        payload = {"proto": "rfid", "air": "gen2", "dir": "tag", "data": data, "bits": count, "src": f"tag{index + 1}",
                   "peer": "uhf reader"}
        if epc:
            payload["epc"] = epc
        self.emit(t, "tx", payload)

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("dir") != "reader":
            return
        bits = rfid.bytes_to_bits(payload["data"], int(payload["bits"]))
        at = t + 60e-6
        if bits.startswith("1000") and len(bits) == 22:          # Query: every tag picks a slot
            q = int(bits[13:17], 2)
            self.done = [False] * len(self.epcs)
            self.slots = [self.random.randrange(2 ** q) for _ in self.epcs]
            for i, slot in enumerate(self.slots):
                if slot == 0:
                    self.rn[i] = self.random.getrandbits(16)
                    self._reply(at, i, f"{self.rn[i]:016b}")
        elif bits.startswith("00") and len(bits) == 4:           # QueryRep: count down
            for i in range(len(self.epcs)):
                if self.slots[i] is None or self.done[i]:
                    continue
                self.slots[i] -= 1
                if self.slots[i] == 0:
                    self.rn[i] = self.random.getrandbits(16)
                    self._reply(at, i, f"{self.rn[i]:016b}")
        elif bits.startswith("01") and len(bits) == 18:          # ACK: the tag with that RN16 sends its EPC
            rn = int(bits[2:], 2)
            for i in range(len(self.epcs)):
                if self.slots[i] == 0 and self.rn[i] == rn and not self.done[i]:
                    self.done[i] = True
                    self.inventoried += 1
                    self._reply(at, i, rfid.gen2_epc_reply(self.epcs[i]), self.epcs[i].hex().upper())

    def output(self, t, x, u):
        return {"inventoried": float(self.inventoried)}


# -- LoRaWAN --------------------------------------------------------------------------------------------------

class LoraNode(_Plant):
    inputs = ("temperature", "humidity")
    outputs = ("fcnt", "acks")
    event_inputs = ("rx",)
    event_outputs = ("tx",)

    def __init__(self, dev_addr: str = "26011BDA", nwkskey: str = "2B7E151628AED2A6ABF7158809CF4F3C",
                 appskey: str = "000102030405060708090A0B0C0D0E0F", interval_ms: float = 400.0, sf: int = 9,
                 freqs: Sequence[float] = (868.1, 868.3, 868.5), name: str = "lora node") -> None:
        super().__init__()
        self.dev_addr = int(dev_addr, 16)
        self.nwk, self.app = bytes.fromhex(nwkskey), bytes.fromhex(appskey)
        self.interval, self.sf, self.freqs, self.name = interval_ms / 1000.0, int(sf), list(freqs), name
        self.fcnt = 0
        self.acks = 0
        self.next = 0.05
        self.pending_ack = False

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("proto") != "lora" or payload.get("src") == self.name:
            return
        data = bytes.fromhex(payload["data"])
        if len(data) >= 12 and data[0] >> 5 in (3, 5) and struct.unpack_from("<I", data, 1)[0] == self.dev_addr:
            fcnt = struct.unpack_from("<H", data, 6)[0]
            if lora.mic(self.nwk, self.dev_addr, fcnt, False, data[:-4]) != data[-4:]:
                return                                       # corrupted downlink: dropped
            if data[5] & 0x20:
                self.acks += 1
            if data[5] & 0x0F:                                   # LinkADRReq in FOpts: answer in the next uplink
                self.pending_ack = True

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        while self.next <= t1:
            temp = int(round(float(u.get("temperature", 21.5)) * 100))
            hum = int(round(float(u.get("humidity", 45.0)) * 2))
            record = struct.pack(">hBH", temp, hum & 0xFF, 3300 - self.fcnt)
            confirmed = self.fcnt % 3 == 2
            fopts = b"\x03\x07" if self.pending_ack else b""     # LinkADRAns: all accepted
            self.pending_ack = False
            frame = lora.data_frame(self.dev_addr, self.fcnt, 2, record, self.nwk, self.app, confirmed=confirmed, fopts=fopts)
            freq = self.freqs[self.fcnt % len(self.freqs)]
            self.emit(self.next, "tx", {"proto": "lora", "freq": freq, "sf": self.sf, "bw": 125, "cr": "4/5",
                                        "rssi": -97 - (self.fcnt % 4), "snr": 7.5 - (self.fcnt % 3), "data": frame.hex(), "src": self.name})
            self.fcnt += 1
            self.next += self.interval

    def output(self, t, x, u):
        return {"fcnt": float(self.fcnt), "acks": float(self.acks)}


class LoraGateway(_Plant):
    outputs = ("uplinks", "bad_mic")
    event_inputs = ("rx",)
    event_outputs = ("tx",)

    def __init__(self, dev_addr: str = "26011BDA", nwkskey: str = "2B7E151628AED2A6ABF7158809CF4F3C",
                 appskey: str = "000102030405060708090A0B0C0D0E0F", rx1_delay_ms: float = 100.0, name: str = "gateway") -> None:
        super().__init__()
        self.dev_addr = int(dev_addr, 16)
        self.nwk, self.app = bytes.fromhex(nwkskey), bytes.fromhex(appskey)
        self.rx1 = rx1_delay_ms / 1000.0
        self.name = name
        self.uplinks = 0
        self.bad_mic = 0
        self.fcnt_down = 0
        self.adr_sent = False

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("proto") != "lora" or payload.get("src") == self.name:
            return
        data = bytes.fromhex(payload["data"])
        if len(data) < 12 or data[0] >> 5 not in (2, 4):
            return
        fcnt = struct.unpack_from("<H", data, 6)[0]
        if lora.mic(self.nwk, self.dev_addr, fcnt, True, data[:-4]) != data[-4:]:
            self.bad_mic += 1                                 # corrupted on the air: the network server drops it
            return
        self.uplinks += 1
        confirmed = data[0] >> 5 == 4
        if not confirmed and self.adr_sent:
            return
        fopts = b"" if self.adr_sent else b"\x03\x51\x07\x00\x01"   # LinkADRReq: DR5, 14 dBm, ch 0-2
        self.adr_sent = True
        frame = lora.data_frame(self.dev_addr, self.fcnt_down, 0, b"", self.nwk, self.app, uplink=False, ack=confirmed,
                                adr=False, fopts=fopts)
        self.fcnt_down += 1
        self.emit(t + self.rx1, "tx", {"proto": "lora", "freq": payload.get("freq"), "sf": payload.get("sf"), "bw": 125,
                                       "cr": "4/5", "rssi": -60, "snr": 10.0, "data": frame.hex(), "src": self.name})

    def output(self, t, x, u):
        return {"uplinks": float(self.uplinks), "bad_mic": float(self.bad_mic)}


# -- BLE ------------------------------------------------------------------------------------------------------

def _adv(t: float, address: str, adv_data: bytes, pdu_type: int, rssi: float, src: str) -> list[tuple[float, str, Any]]:
    packet = ble.adv_packet(address, adv_data, pdu_type=pdu_type)
    air = (len(packet) + 1) * 8e-6
    return [(t + k * (air + 150e-6), "ble", {"proto": "ble", "channel": ch, "rssi": rssi, "data": packet.hex(), "src": src})
            for k, ch in enumerate((37, 38, 39))]


class BleBeacon(_Plant):
    outputs = ("adverts",)
    event_outputs = ("ble",)

    def __init__(self, address: str = "D4:CA:6E:00:00:B1", interval_ms: float = 100.0, rssi: float = -71.0,
                 uuid: str = "E2C56DB5DFFB48D2B060D0F5A71096E0", major: int = 7, minor: int = 42, name: str = "beacon") -> None:
        super().__init__()
        self.address, self.interval, self.rssi, self.name = address, interval_ms / 1000.0, float(rssi), name
        self.uuid, self.major, self.minor = bytes.fromhex(uuid), int(major), int(minor)
        self.next = 0.013
        self.count = 0

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        while self.next <= t1:
            kind = self.count % 3
            flags = ble.ad(0x01, b"\x06")
            if kind == 0:                                        # iBeacon
                data = flags + ble.ad(0xFF, b"\x4c\x00\x02\x15" + self.uuid + struct.pack(">HHb", self.major, self.minor, -59))
            elif kind == 1:                                      # Eddystone-UID
                data = flags + ble.ad(0x03, b"\xaa\xfe") + ble.ad(0x16, b"\xaa\xfe\x00\xee" + self.uuid[:10] + b"\x00\x00\x00\x00\x00\x2a")
            else:                                                # Eddystone-TLM
                data = flags + ble.ad(0x03, b"\xaa\xfe") + ble.ad(0x16, b"\xaa\xfe\x20\x00" + struct.pack(">HhII", 2950, int(23.5 * 256),
                                                                                                         self.count, int(self.next * 10)))
            for item in _adv(self.next, self.address, data, 2, self.rssi, self.name):
                self._out.append((item[0], "ble", item[2]))
            self.count += 1
            self.next += self.interval

    def output(self, t, x, u):
        return {"adverts": float(self.count)}


class BleSensor(_Plant):
    inputs = ("temperature",)
    outputs = ("connected",)
    event_inputs = ("ble_rx",)
    event_outputs = ("ble",)

    def __init__(self, address: str = "C0:98:E5:00:00:5E", name_text: str = "ENV-5E", interval_ms: float = 50.0,
                 rssi: float = -55.0, name: str = "sensor") -> None:
        super().__init__()
        self.address, self.name_text, self.interval, self.rssi, self.name = address, name_text, interval_ms / 1000.0, float(rssi), name
        self.next = 0.021
        self.connected = False
        self.temperature = 21.0

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("src") == self.name or self.connected:
            return
        data = bytes.fromhex(payload["data"])
        if len(data) < 9 or ble.crc24(data[4:-3]) != data[-3:]:
            return
        kind = data[4] & 0x0F
        if kind == 3 and ble.mac_text(data[12:18]) == self.address:              # SCAN_REQ for us: SCAN_RSP after 150 us
            rsp = ble.ad(0x09, self.name_text.encode()) + ble.ad(0x0A, b"\x00")
            packet = ble.adv_packet(self.address, rsp, pdu_type=4)
            self.emit(t + 150e-6, "ble", {"proto": "ble", "channel": payload.get("channel"), "rssi": self.rssi,
                                           "data": packet.hex(), "src": self.name})
        elif kind == 5 and ble.mac_text(data[12:18]) == self.address:
            self.connected = True

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        self.temperature = float(u.get("temperature", self.temperature))
        while not self.connected and self.next <= t1:
            data = ble.ad(0x01, b"\x06") + ble.ad(0x03, b"\x1a\x18") + ble.ad(0x16, b"\x1a\x18" + struct.pack("<h", int(self.temperature * 100)))
            for item in _adv(self.next, self.address, data, 0, self.rssi, self.name):
                self._out.append(item)
            self.next += self.interval

    def output(self, t, x, u):
        return {"connected": float(self.connected)}


class BlePhone(_Plant):
    outputs = ("scan_responses",)
    event_inputs = ("ble_rx",)
    event_outputs = ("ble",)

    def __init__(self, address: str = "5A:3B:00:00:00:F0", target: str = "C0:98:E5:00:00:5E", connect_after: int = 3,
                 rssi: float = -50.0, name: str = "phone") -> None:
        super().__init__()
        self.address, self.target, self.connect_after, self.rssi, self.name = address, target, int(connect_after), float(rssi), name
        self.responses = 0
        self.connected = False

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("src") == self.name or self.connected:
            return
        data = bytes.fromhex(payload["data"])
        if len(data) < 9 or ble.crc24(data[4:-3]) != data[-3:]:
            return
        kind = data[4] & 0x0F
        if ble.mac_text(data[6:12]) != self.target:
            return
        if kind == 4:
            self.responses += 1
        elif kind == 0 and payload.get("channel") == 37:
            if self.responses >= self.connect_after:
                ll = struct.pack("<I", 0x50654C5A) + b"\x55\x55\x55" + bytes([2]) + struct.pack("<HHHH", 4, 24, 0, 200) + \
                    b"\xff\xff\xff\xff\x1f" + bytes([0x05 | (5 << 5)])
                body = ble.mac_bytes(self.address) + ble.mac_bytes(self.target) + ll
                header = bytes([0x05 | 0x40 | 0x80 | 0x20, len(body)])
                pdu = header + body
                packet = struct.pack("<I", ble.ADV_ACCESS_ADDRESS) + pdu + ble.crc24(pdu)
                self.emit(t + 150e-6, "ble", {"proto": "ble", "channel": 37, "rssi": self.rssi, "data": packet.hex(), "src": self.name})
                self.connected = True
            else:
                req = ble.mac_bytes(self.address) + ble.mac_bytes(self.target)
                pdu = bytes([0x03 | 0x40 | 0x80, len(req)]) + req
                packet = struct.pack("<I", ble.ADV_ACCESS_ADDRESS) + pdu + ble.crc24(pdu)
                self.emit(t + 150e-6, "ble", {"proto": "ble", "channel": 37, "rssi": self.rssi, "data": packet.hex(), "src": self.name})

    def output(self, t, x, u):
        return {"scan_responses": float(self.responses)}


# -- Wi-Fi WPA2 ---------------------------------------------------------------------------------------------

RSN_PSK_CCMP = bytes.fromhex("0100000fac040100000fac040100000fac020000")


class WpaAccessPoint(_Plant):
    outputs = ("stations", "handshakes")
    event_inputs = ("wifi_rx",)
    event_outputs = ("wifi_tx",)

    def __init__(self, bssid: str = "02:11:22:33:44:01", ssid: str = "lab-wpa2", channel: int = 11, name: str = "ap",
                 seed: int = 1) -> None:
        super().__init__()
        self.rng = random.Random(seed)
        self.bssid, self.ssid, self.channel, self.name = bssid, ssid, int(channel), name
        self.next_beacon = 0.0
        self.sequence = 0
        self.stations: dict[str, str] = {}
        self.replay = 0
        self.handshakes = 0
        self.anonce = b""

    def _tx(self, t: float, frame: bytes) -> None:
        self.emit(t, "wifi_tx", {"proto": "wifi", "channel": self.channel, "rssi": -38, "data": frame.hex(), "src": self.name})
        self.sequence += 1

    def _eapol(self, t: float, station: str, message: int, nonce: bytes) -> None:
        body = b"\x00\x00" + wifi.llc_snap(0x888E, wifi.eapol_key(message, self.replay, nonce, self.rng.randbytes(16) if message == 3 else bytes(16),
                                                                   self.rng.randbytes(24) if message == 3 else b""))
        self._tx(t, wifi.frame(2, 8, station, self.bssid, self.bssid, body, from_ds=True, sequence=self.sequence))

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("src") == self.name:
            return
        data = bytes.fromhex(payload["data"])
        if len(data) < 16 or wifi.fcs(data[:-4]) != data[-4:] or wifi.mac_text(data[4:10]) != self.bssid:
            return
        control = struct.unpack_from("<H", data)[0]
        kind, subtype = (control >> 2) & 3, (control >> 4) & 15
        sender = wifi.mac_text(data[10:16])
        self._tx(t + 16e-6, wifi.frame(1, 13, sender))
        if kind == 0 and subtype == 11:
            self._tx(t + 300e-6, wifi.frame(0, 11, sender, self.bssid, self.bssid, struct.pack("<HHH", 0, 2, 0), sequence=self.sequence))
        elif kind == 0 and subtype == 0:
            body = struct.pack("<HHH", 0x0411, 0, 0xC001) + wifi.element(1, bytes([0x82, 0x84, 0x8B, 0x96]))
            self._tx(t + 300e-6, wifi.frame(0, 1, sender, self.bssid, self.bssid, body, sequence=self.sequence))
            self.stations[sender] = "4way"
            self.replay += 1
            self.anonce = self.rng.randbytes(32)
            self._eapol(t + 2e-3, sender, 1, self.anonce)
        elif kind == 2 and b"\x88\x8e" in data[24:40] and self.stations.get(sender) == "4way":
            info = struct.unpack_from(">H", data, data.index(b"\x88\x8e") + 2 + 4 + 1)[0]
            nonce_zero = data[data.index(b"\x88\x8e") + 2 + 4 + 13:data.index(b"\x88\x8e") + 2 + 4 + 45] == bytes(32)
            if not nonce_zero:                                    # message 2: send 3
                self.replay += 1
                self._eapol(t + 1e-3, sender, 3, self.anonce)
            elif info & 0x0200:                                   # message 4: keys installed
                self.stations[sender] = "connected"
                self.handshakes += 1

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        while self.next_beacon <= t1:
            self._tx(self.next_beacon, wifi.beacon(self.bssid, self.ssid, self.channel, self.sequence, int(self.next_beacon * 1e6),
                                                   rsn=RSN_PSK_CCMP))
            self.next_beacon += 0.1024

    def output(self, t, x, u):
        return {"stations": float(sum(1 for s in self.stations.values() if s == "connected")), "handshakes": float(self.handshakes)}


class WpaStation(_Plant):
    outputs = ("state", "frames_sent", "joins")
    event_inputs = ("wifi_rx",)
    event_outputs = ("wifi_tx",)

    def __init__(self, address: str = "3C:71:BF:00:00:77", bssid: str = "02:11:22:33:44:01", channel: int = 11,
                 report_ms: float = 100.0, join_at: float = 0.03, name: str = "station", seed: int = 2) -> None:
        super().__init__()
        self.rng = random.Random(seed)
        self.address, self.bssid, self.channel, self.report, self.name = address, bssid, int(channel), report_ms / 1000.0, name
        self.state = 0                                          # 0 idle, 1 auth, 2 assoc, 3 4-way, 4 connected
        self.join_at = join_at
        self.sequence = 0
        self.pn = 1
        self.sent = 0
        self.joins = 0
        self.next = None

    def _tx(self, t: float, frame: bytes) -> None:
        self.emit(t, "wifi_tx", {"proto": "wifi", "channel": self.channel, "rssi": -52, "data": frame.hex(), "src": self.name})
        self.sequence += 1

    def receive(self, t: float, port: str, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("src") == self.name:
            return
        data = bytes.fromhex(payload["data"])
        if len(data) < 14 or wifi.fcs(data[:-4]) != data[-4:] or wifi.mac_text(data[4:10]) != self.address:
            return
        control = struct.unpack_from("<H", data)[0]
        kind, subtype = (control >> 2) & 3, (control >> 4) & 15
        if kind == 0 and subtype == 12:                         # deauthenticated: join again
            self.state, self.next, self.join_at = 0, None, t + 0.02
        elif kind == 0 and subtype == 11 and self.state == 1:
            self.state = 2
            body = struct.pack("<HH", 0x0411, 10) + wifi.element(0, b"lab-wpa2") + wifi.element(48, RSN_PSK_CCMP)
            self._tx(t + 300e-6, wifi.frame(0, 0, self.bssid, self.address, self.bssid, body, sequence=self.sequence))
        elif kind == 0 and subtype == 1 and self.state == 2:
            self.state = 3
        elif kind == 2 and b"\x88\x8e" in data[24:40] and self.state == 3:
            key = data.index(b"\x88\x8e") + 2 + 4
            info = struct.unpack_from(">H", data, key + 1)[0]
            replay = struct.unpack_from(">Q", data, key + 5)[0]
            if not info & 0x0100:                               # message 1: answer with 2 (SNonce)
                message = wifi.eapol_key(2, replay, self.rng.randbytes(32), self.rng.randbytes(16), self.rng.randbytes(22))
            else:                                               # message 3: answer with 4, then encrypted traffic
                message = wifi.eapol_key(4, replay, bytes(32), self.rng.randbytes(16))
                self.state, self.joins, self.next = 4, self.joins + 1, t + 0.01
            self._tx(t + 1e-3, wifi.frame(2, 8, self.bssid, self.address, self.bssid, b"\x00\x00" + wifi.llc_snap(0x888E, message),
                                         to_ds=True, sequence=self.sequence))

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        if self.state == 0 and t1 >= self.join_at:
            self.state = 1
            self._tx(max(t0, self.join_at), wifi.frame(0, 11, self.bssid, self.address, self.bssid, struct.pack("<HHH", 0, 1, 0),
                                                       sequence=self.sequence))
        while self.state == 4 and self.next is not None and self.next <= t1:
            ccmp = bytes([self.pn & 0xFF, (self.pn >> 8) & 0xFF, 0, 0x20, 0, 0, 0, 0])
            body = b"\x00\x00" + ccmp + self.rng.randbytes(48) + self.rng.randbytes(8)
            control = (2 << 2) | (8 << 4) | (1 << 8) | (1 << 14)
            header = struct.pack("<HH", control, 44) + wifi.mac(self.bssid) + wifi.mac(self.address) + wifi.mac(self.bssid) + \
                struct.pack("<H", (self.sequence & 0xFFF) << 4)
            frame = header + body
            self._tx(self.next, frame + wifi.fcs(frame))
            self.pn += 1
            self.sent += 1
            self.next += self.report

    def output(self, t, x, u):
        return {"state": float(self.state), "frames_sent": float(self.sent), "joins": float(self.joins)}


class Deauther(_Plant):
    """Spoofs one deauthentication from the AP to a station (a classic Wi-Fi attack) at `at`."""

    outputs = ("sent",)
    event_outputs = ("wifi_tx",)

    def __init__(self, at: float = 1.2, bssid: str = "02:11:22:33:44:01", station: str = "3C:71:BF:00:00:77",
                 reason: int = 7, channel: int = 11) -> None:
        super().__init__()
        self.at, self.bssid, self.station, self.reason, self.channel = float(at), bssid, station, int(reason), int(channel)
        self.sent = 0

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        if not self.sent and t1 >= self.at:
            frame = wifi.frame(0, 12, self.station, self.bssid, self.bssid, struct.pack("<H", self.reason), sequence=0)
            self.emit(self.at, "wifi_tx", {"proto": "wifi", "channel": self.channel, "rssi": -35, "data": frame.hex(),
                                           "src": "attacker"})
            self.sent = 1

    def output(self, t, x, u):
        return {"sent": float(self.sent)}
