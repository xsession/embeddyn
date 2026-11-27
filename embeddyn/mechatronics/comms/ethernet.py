"""Ethernet: frames with FCS, ARP, IPv4, ICMP, UDP, TCP and Modbus/TCP; a decoder and two boards (python plants).

Payloads: ``{"proto": "eth", "data": "<hex frame from the destination MAC to the FCS>",
"src": "plc", "speed": 100e6}``. Frames shorter than 64 bytes are padded like a MAC does.

`decode` checks everything a NIC and Wireshark check: the FCS (CRC-32), runts (< 64
bytes) and giants (> 1518, 1522 with a VLAN tag), the IPv4 header checksum and the
UDP/TCP checksums over the pseudo-header, and names hosts from the analyzer config::

    ethernet:
      hosts: {"02:00:00:00:00:10": plc, "192.168.0.20": remote-io}

Modbus/TCP (port 502) is decoded with the same PDU decoder as Modbus RTU (`serial`).
`EthPlc` resolves the remote I/O with ARP, opens a TCP connection and polls holding
registers with Modbus/TCP, retransmitting a request whose answer does not come;
`EthIo` answers it and broadcasts UDP JSON telemetry.
"""
from __future__ import annotations

import json
import random
import struct
from typing import Any, Mapping

from .serial import decode_pdu
from .wifi import fcs

BROADCAST = "FF:FF:FF:FF:FF:FF"
ETHERTYPES = {0x0800: "IPv4", 0x0806: "ARP", 0x86DD: "IPv6", 0x8100: "802.1Q VLAN", 0x88A4: "EtherCAT",
              0x8892: "PROFINET", 0x88F7: "PTP", 0x88CC: "LLDP", 0x88B8: "IEC 61850 GOOSE", 0x88BA: "IEC 61850 SV",
              0x22F0: "AVTP", 0x8809: "Slow protocols (LACP)", 0x88E5: "MACsec"}
IP_PROTOCOLS = {1: "ICMP", 2: "IGMP", 6: "TCP", 17: "UDP", 58: "ICMPv6"}
TCP_FLAGS = ((0x01, "FIN"), (0x02, "SYN"), (0x04, "RST"), (0x08, "PSH"), (0x10, "ACK"), (0x20, "URG"))


def mac(text: str) -> bytes:
    return bytes(int(x, 16) for x in text.replace("-", ":").split(":"))


def mac_text(data: bytes) -> str:
    return ":".join(f"{b:02X}" for b in data)


def ip(text: str) -> bytes:
    return bytes(int(x) for x in text.split("."))


def ip_text(data: bytes) -> str:
    return ".".join(str(b) for b in data)


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def frame(dst: str, src: str, ethertype: int, payload: bytes, vlan: int | None = None) -> bytes:
    """A frame with padding to the 64-byte minimum and the FCS."""
    header = mac(dst) + mac(src) + (struct.pack("!HH", 0x8100, vlan & 0xFFFF) if vlan is not None else b"")
    body = header + struct.pack("!H", ethertype) + payload
    body = body.ljust(60, b"\0")
    return body + fcs(body)


def ipv4(src: str, dst: str, proto: int, payload: bytes, ident: int = 0, ttl: int = 64) -> bytes:
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), ident & 0xFFFF, 0x4000, ttl, proto, 0, ip(src), ip(dst))
    return header[:10] + struct.pack("!H", checksum(header)) + header[12:] + payload


def _pseudo(src: str, dst: str, proto: int, length: int) -> bytes:
    return ip(src) + ip(dst) + struct.pack("!BBH", 0, proto, length)


def udp(src: str, dst: str, sport: int, dport: int, payload: bytes, ident: int = 0) -> bytes:
    segment = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    value = checksum(_pseudo(src, dst, 17, len(segment)) + segment) or 0xFFFF
    return ipv4(src, dst, 17, segment[:6] + struct.pack("!H", value) + segment[8:], ident)


def tcp(src: str, dst: str, sport: int, dport: int, seq: int, ack: int, flags: int, payload: bytes = b"",
        window: int = 8192, ident: int = 0) -> bytes:
    segment = struct.pack("!HHIIBBHHH", sport, dport, seq & 0xFFFFFFFF, ack & 0xFFFFFFFF, 5 << 4, flags, window, 0, 0) + payload
    value = checksum(_pseudo(src, dst, 6, len(segment)) + segment)
    return ipv4(src, dst, 6, segment[:16] + struct.pack("!H", value) + segment[18:], ident)


def arp(op: int, sender_mac: str, sender_ip: str, target_mac: str, target_ip: str) -> bytes:
    return struct.pack("!HHBBH", 1, 0x0800, 6, 4, op) + mac(sender_mac) + ip(sender_ip) + mac(target_mac) + ip(target_ip)


def mbap(transaction: int, unit: int, pdu: bytes) -> bytes:
    return struct.pack("!HHHB", transaction & 0xFFFF, 0, len(pdu) + 1, unit) + pdu


def _f(label: str, value: Any, start: int | None = None, end: int | None = None, children=None) -> dict[str, Any]:
    item: dict[str, Any] = {"label": label, "value": value}
    if start is not None:
        item["bytes"] = [start, end if end is not None else start + 1]
    if children:
        item["children"] = children
    return item


def decode(data: bytes, config: Mapping[str, Any] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    context = context if context is not None else {}
    hosts = {str(k).upper(): str(v) for k, v in (config.get("hosts") or {}).items()}
    name = lambda key: hosts.get(key.upper(), key)                                  # noqa: E731
    size = len(data)
    if size < 18:
        return {"type": "runt", "summary": f"runt: {size} bytes", "fields": [_f("data", data.hex(" "), 0, size)], "ok": False,
                "problems": ["runt"]}
    end = size - 4
    fcs_ok = fcs(data[:end]) == data[end:]
    dst, src = mac_text(data[0:6]), mac_text(data[6:12])
    pos = 12
    (ethertype,) = struct.unpack_from("!H", data, pos)
    fields = [_f("destination", name(dst) + (f" ({dst})" if name(dst) != dst else ""), 0, 6),
              _f("source", name(src) + (f" ({src})" if name(src) != src else ""), 6, 12)]
    vlan = None
    if ethertype == 0x8100 and size >= 22:
        (tci,) = struct.unpack_from("!H", data, 14)
        vlan = tci & 0x0FFF
        fields.append(_f("802.1Q", f"VLAN {vlan}, priority {tci >> 13}", 12, 16))
        pos = 16
        (ethertype,) = struct.unpack_from("!H", data, pos)
    problems = []
    if size < 64:
        problems.append(f"runt ({size} < 64 bytes)")
    if size > (1522 if vlan is not None else 1518):
        problems.append(f"giant ({size} > {1522 if vlan is not None else 1518} bytes)")
    if not fcs_ok:
        problems.append("FCS error")
    out: dict[str, Any] = {"source": name(src), "target": "broadcast" if dst == BROADCAST else name(dst), "type": "Ethernet"}
    if ethertype <= 1500:
        fields.append(_f("length (802.3)", ethertype, pos, pos + 2))
        summary = f"802.3 LLC {data[pos + 2:pos + 5].hex(' ')}"
        kind = "802.3"
        pos += 2
    else:
        fields.append(_f("EtherType", f"0x{ethertype:04X} {ETHERTYPES.get(ethertype, '')}".strip(), pos, pos + 2))
        pos += 2
        kind, summary, more = _ethertype(ethertype, data, pos, end, name, context)
        fields += more
    fields.append(_f("FCS", f"{data[end:].hex(' ')} ({'ok' if fcs_ok else f'BAD, computed {fcs(data[:end]).hex(" ")}'})", end, size))
    if problems:
        summary = f"[{', '.join(problems)}] {summary}"
    out.update(type=kind, summary=summary, fields=fields, ok=fcs_ok and not problems, problems=problems, vlan=vlan)
    addresses = context.pop("_ip", None)               # IP hosts are better names than MACs when configured
    if addresses:
        out["source"] = hosts.get(addresses[0], out["source"])
        if dst != BROADCAST:
            out["target"] = hosts.get(addresses[1], out["target"])
    return out


def _ethertype(ethertype: int, data: bytes, pos: int, end: int, name, context: dict[str, Any]) -> tuple[str, str, list]:
    if ethertype == 0x0806 and end - pos >= 28:
        op, = struct.unpack_from("!H", data, pos + 6)
        sha, spa = mac_text(data[pos + 8:pos + 14]), ip_text(data[pos + 14:pos + 18])
        tha, tpa = mac_text(data[pos + 18:pos + 24]), ip_text(data[pos + 24:pos + 28])
        text = f"who has {tpa}? tell {spa}" if op == 1 else f"{spa} is at {sha}" if op == 2 else f"op {op}"
        return "ARP", text, [_f("ARP", text, pos, pos + 28, [_f("operation", "request" if op == 1 else "reply" if op == 2 else op, pos + 6, pos + 8),
                                                            _f("sender", f"{name(spa)} {sha}", pos + 8, pos + 18),
                                                            _f("target", f"{name(tpa)} {tha}", pos + 18, pos + 28)])]
    if ethertype != 0x0800 or end - pos < 20:
        name_text = ETHERTYPES.get(ethertype, f"0x{ethertype:04X}")
        return name_text, f"{name_text} {end - pos} bytes", [_f("payload", data[pos:end].hex(" "), pos, end)]
    ihl = (data[pos] & 15) * 4
    total, ident, frag, ttl, proto, _ = struct.unpack_from("!HHHBBH", data, pos + 2)
    src, dst = ip_text(data[pos + 12:pos + 16]), ip_text(data[pos + 16:pos + 20])
    ip_ok = checksum(data[pos:pos + ihl]) == 0
    ip_end = min(end, pos + total)
    fields = [_f("IPv4", f"{name(src)} → {name(dst)}", pos, pos + ihl,
                 [_f("source", src, pos + 12, pos + 16), _f("destination", dst, pos + 16, pos + 20), _f("TTL", ttl, pos + 8),
                  _f("protocol", f"{proto} {IP_PROTOCOLS.get(proto, '')}".strip(), pos + 9),
                  _f("identification", f"0x{ident:04X}", pos + 4, pos + 6),
                  _f("flags", ("DF " if frag & 0x4000 else "") + ("MF" if frag & 0x2000 else "") or "none", pos + 6, pos + 8),
                  _f("header checksum", "ok" if ip_ok else "BAD", pos + 10, pos + 12)])]
    context["_ip"] = (src, dst)
    head = f"{src} → {dst}" + ("" if ip_ok else " [IP checksum BAD]")
    l4 = pos + ihl
    segment = data[l4:ip_end]
    pseudo_ok = checksum(ip(src) + ip(dst) + struct.pack("!BBH", 0, proto, len(segment)) + segment) == 0
    if proto == 17 and len(segment) >= 8:
        sport, dport, length, value = struct.unpack_from("!HHHH", segment)
        ok = value == 0 or pseudo_ok
        fields.append(_f("UDP", f"{sport} → {dport}, length {length}, checksum {'ok' if ok else 'BAD'}", l4, l4 + 8))
        payload = segment[8:]
        info = _app_payload(payload, l4 + 8, fields)
        return "UDP", f"{head} UDP {sport}→{dport}{info}" + ("" if ok else " [UDP checksum BAD]"), fields
    if proto == 6 and len(segment) >= 20:
        sport, dport, seq, ack, offset, flags, window = struct.unpack_from("!HHIIBBH", segment)
        header = (offset >> 4) * 4
        names = ",".join(n for bit, n in TCP_FLAGS if flags & bit)
        payload = segment[header:]
        fields.append(_f("TCP", f"{sport} → {dport} [{names}] seq {seq} ack {ack} win {window}, checksum {'ok' if pseudo_ok else 'BAD'}",
                         l4, l4 + header, [_f("source port", sport, l4, l4 + 2), _f("destination port", dport, l4 + 2, l4 + 4),
                                           _f("sequence", seq, l4 + 4, l4 + 8), _f("acknowledgment", ack, l4 + 8, l4 + 12),
                                           _f("flags", names, l4 + 13), _f("window", window, l4 + 14, l4 + 16)]))
        text = f"{head} TCP {sport}→{dport} [{names}]" + ("" if pseudo_ok else " [TCP checksum BAD]")
        if payload and 502 in (sport, dport) and len(payload) >= 8:
            tid, pid, length, unit = struct.unpack_from("!HHHB", payload)
            start = l4 + header
            key = f"mbtcp:{tid}"
            response = sport == 502
            request = context.pop(key, None) if response else None
            kind, summary, pdu_fields, info = decode_pdu(payload[7:7 + length - 1], start + 7, response, request, {})
            if not response:
                context[key] = info
            fields.append(_f("Modbus/TCP", f"transaction {tid}, unit {unit}", start, start + len(payload),
                             [_f("transaction", tid, start, start + 2), _f("protocol", pid, start + 2, start + 4),
                              _f("length", length, start + 4, start + 6), _f("unit", unit, start + 6), *pdu_fields]))
            return "Modbus/TCP", f"{head} Modbus/TCP #{tid} {summary}", fields
        if payload:
            text += _app_payload(payload, l4 + header, fields)
        return "TCP", text, fields
    if proto == 1 and len(segment) >= 8:
        kind_code, code = segment[0], segment[1]
        text = {0: "echo reply", 8: "echo request", 3: "destination unreachable", 11: "time exceeded"}.get(kind_code, f"type {kind_code}")
        fields.append(_f("ICMP", f"{text} (code {code}), checksum {'ok' if checksum(segment) == 0 else 'BAD'}", l4, ip_end))
        return "ICMP", f"{head} ICMP {text}", fields
    fields.append(_f("payload", segment.hex(" "), l4, ip_end))
    return f"IPv4 {IP_PROTOCOLS.get(proto, proto)}", f"{head} proto {proto}", fields


def _app_payload(payload: bytes, start: int, fields: list) -> str:
    if not payload:
        return ""
    try:
        obj = json.loads(payload.decode())
    except (ValueError, UnicodeDecodeError):
        fields.append(_f("data", repr(payload[:60].decode("latin-1")), start, start + len(payload)))
        return f" {len(payload)} bytes"
    if isinstance(obj, dict):
        fields.append(_f("JSON", f"{len(payload)} bytes", start, start + len(payload),
                         [_f(str(k), v if isinstance(v, str) else json.dumps(v)) for k, v in obj.items()]))
        return " " + " ".join(f"{k}={v}" for k, v in list(obj.items())[:4])
    fields.append(_f("JSON", json.dumps(obj)[:80], start, start + len(payload)))
    return ""


class _Node:
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    states = ()
    event_inputs = ("rx",)
    event_outputs = ("tx",)

    def __init__(self, mac_address: str, ip_address: str, speed: float, name: str) -> None:
        self.mac, self.ip, self.speed, self.name = mac_address.upper(), ip_address, float(speed), name
        self.ident = 0
        self._out: list[tuple[float, str, Any]] = []

    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t, x, u):
        return []

    def send(self, t: float, dst: str, ethertype: int, payload: bytes) -> None:
        self.ident += 1
        data = frame(dst, self.mac, ethertype, payload)
        self._out.append((t, "tx", {"proto": "eth", "data": data.hex(), "src": self.name, "speed": self.speed}))

    def wire_time(self, size: int) -> float:
        return (size + 8 + 12) * 8 / self.speed                      # preamble + frame + inter-frame gap

    def after(self, t: float, payload: Mapping[str, Any], turnaround: float) -> float:
        """When an answer to a frame that started at t can start: after its last bit, plus `turnaround`."""
        return t + self.wire_time(len(str(payload.get("data", ""))) // 2) + turnaround

    @staticmethod
    def parse(payload: Any) -> tuple[bytes, bytes, int, bytes] | None:
        """(destination MAC, source MAC, EtherType, payload) of a frame with a good FCS, else None (the MAC drops it)."""
        if not isinstance(payload, Mapping) or payload.get("proto") != "eth":
            return None
        data = bytes.fromhex(str(payload.get("data", "")))
        if len(data) < 64 or fcs(data[:-4]) != data[-4:]:
            return None
        return data[0:6], data[6:12], struct.unpack_from("!H", data, 12)[0], data[14:-4]

    def pop_events(self) -> list[tuple[float, str, Any]]:
        out, self._out = self._out, []
        return out


class EthPlc(_Node):
    """ARP for the remote I/O, TCP connect to port 502, poll holding registers 0..3 with Modbus/TCP every
    `period_ms`; a request without answer is retransmitted after `rto_ms` (up to 3 times)."""

    outputs = ("value", "good", "retransmissions", "timeouts", "connected")

    def __init__(self, mac_address: str = "02:00:00:00:00:10", ip_address: str = "192.168.0.10",
                 server_ip: str = "192.168.0.20", period_ms: float = 10.0, rto_ms: float = 5.0, speed: float = 100e6,
                 name: str = "plc") -> None:
        super().__init__(mac_address, ip_address, speed, name)
        self.server_ip, self.period, self.rto = server_ip, period_ms / 1000.0, rto_ms / 1000.0
        self.server_mac: str | None = None
        self.state = "arp"
        self.next = 0.002
        self.seq, self.ack, self.port = 1000, 0, 49152
        self.tid = 0
        self.inflight: dict[str, Any] | None = None
        self.value = 0.0
        self.good = self.retransmissions = self.timeouts = 0

    def _tcp(self, t: float, flags: int, payload: bytes = b"") -> None:
        packet = tcp(self.ip, self.server_ip, self.port, 502, self.seq, self.ack, flags, payload, ident=self.ident)
        self.send(t, self.server_mac or BROADCAST, 0x0800, packet)

    def receive(self, t: float, port: str, payload: Any) -> None:
        parsed = self.parse(payload)
        if parsed is None:
            return
        dst, _, ethertype, body = parsed
        if ethertype == 0x0806 and len(body) >= 28 and struct.unpack_from("!H", body, 6)[0] == 2 and ip_text(body[14:18]) == self.server_ip:
            self.server_mac = mac_text(body[8:14])
            if self.state == "arp":
                self.state = "syn"
                at = self.after(t, payload, 20e-6)
                self._tcp(at, 0x02)
                self.seq += 1
                self.inflight = {"at": at, "kind": "syn", "tries": 0}
            return
        if ethertype != 0x0800 or mac_text(dst) != self.mac or len(body) < 40 or body[9] != 6:
            return
        segment = body[(body[0] & 15) * 4:]
        flags = segment[13]
        seq, = struct.unpack_from("!I", segment, 4)
        data = segment[(segment[12] >> 4) * 4:]
        if self.state == "syn" and flags & 0x12 == 0x12:
            self.ack = seq + 1
            self._tcp(self.after(t, payload, 20e-6), 0x10)
            self.state = "run"
            self.inflight = None
            return
        if self.state == "run" and data and self.inflight and self.inflight.get("kind") == "modbus" and len(data) >= 9:
            tid = struct.unpack_from("!H", data)[0]
            if tid != self.tid:
                return
            self.ack = seq + len(data)
            if data[7] == 3 and len(data) >= 9 + 2:
                self.value = struct.unpack_from("!h", data, 9)[0] / 10.0
            self.good += 1
            self.inflight = None
            self._tcp(self.after(t, payload, 20e-6), 0x10)

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        if self.inflight and t1 > self.inflight["at"] + self.rto * (2 ** self.inflight["tries"]):
            if self.inflight["tries"] >= 3:
                self.timeouts += 1
                self.state = "arp" if self.inflight["kind"] != "modbus" else "run"
                self.inflight = None
            else:
                self.inflight["tries"] += 1
                self.retransmissions += 1
                if self.inflight["kind"] == "syn":
                    self.seq -= 1
                    self._tcp(t1, 0x02)
                    self.seq += 1
                else:
                    self.seq -= len(self.inflight["payload"])
                    self._tcp(t1, 0x18, self.inflight["payload"])
                    self.seq += len(self.inflight["payload"])
        while self.next <= t1:
            at = self.next
            self.next += self.period
            if self.state == "arp":
                self.send(at, BROADCAST, 0x0806, arp(1, self.mac, self.ip, "00:00:00:00:00:00", self.server_ip))
            elif self.state == "run" and self.inflight is None:
                self.tid = (self.tid + 1) & 0xFFFF
                request = mbap(self.tid, 1, struct.pack("!BHH", 3, 0, 4))
                self._tcp(at, 0x18, request)
                self.seq += len(request)
                self.inflight = {"at": at, "kind": "modbus", "tries": 0, "payload": request}

    def output(self, t, x, u):
        return {"value": self.value, "good": float(self.good), "retransmissions": float(self.retransmissions),
                "timeouts": float(self.timeouts), "connected": 1.0 if self.state == "run" else 0.0}


class EthIo(_Node):
    """Remote I/O: answers ARP, accepts the TCP connection, answers Modbus/TCP reads of registers 0..3
    (0: `value` × 10) and broadcasts UDP JSON telemetry every `telemetry_ms` (± jitter)."""

    inputs = ("value",)
    outputs = ("requests",)

    def __init__(self, mac_address: str = "02:00:00:00:00:20", ip_address: str = "192.168.0.20",
                 telemetry_ms: float = 2.0, jitter_ms: float = 1.0, speed: float = 100e6, seed: int = 3,
                 name: str = "remote-io") -> None:
        super().__init__(mac_address, ip_address, speed, name)
        self.telemetry, self.jitter = telemetry_ms / 1000.0, jitter_ms / 1000.0
        self.random = random.Random(seed)
        self.next = 0.001
        self.peer: dict[str, Any] | None = None
        self.seq = 5000
        self.requests = 0
        self.value = 0.0
        self.count = 0

    def receive(self, t: float, port: str, payload: Any) -> None:
        parsed = self.parse(payload)
        if parsed is None:
            return
        dst, source, ethertype, body = parsed
        if ethertype == 0x0806 and len(body) >= 28 and struct.unpack_from("!H", body, 6)[0] == 1 and ip_text(body[24:28]) == self.ip:
            sender_mac, sender_ip = mac_text(body[8:14]), ip_text(body[14:18])
            self.send(self.after(t, payload, 10e-6), sender_mac, 0x0806, arp(2, self.mac, self.ip, sender_mac, sender_ip))
            return
        if ethertype != 0x0800 or mac_text(dst) != self.mac or len(body) < 40 or body[9] != 6:
            return
        src_ip = ip_text(body[12:16])
        segment = body[(body[0] & 15) * 4:]
        sport, _, seq, _, offset, flags = struct.unpack_from("!HHIIBB", segment)
        data = segment[(offset >> 4) * 4:]
        src_mac = mac_text(source)
        if flags & 0x02:
            self.peer = {"ip": src_ip, "mac": src_mac, "port": sport}
            packet = tcp(self.ip, src_ip, 502, sport, self.seq, seq + 1, 0x12, ident=self.count)
            self.seq += 1
            self.send(self.after(t, payload, 15e-6), src_mac, 0x0800, packet)
            return
        if data and len(data) >= 12 and data[7] == 3:
            self.requests += 1
            tid, _, _, unit = struct.unpack_from("!HHHB", data)
            address, count = struct.unpack_from("!HH", data, 8)
            regs = [int(round(self.value * 10)), self.requests & 0xFFFF, 0x0001, 0][:count]
            pdu = struct.pack("!BB", 3, 2 * len(regs)) + b"".join(struct.pack("!H", r & 0xFFFF) for r in regs)
            reply = mbap(tid, unit, pdu)
            packet = tcp(self.ip, src_ip, 502, sport, self.seq, seq + len(data), 0x18, reply, ident=self.count)
            self.seq += len(reply)
            self.send(self.after(t, payload, 30e-6), src_mac, 0x0800, packet)

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        self.value = float(u.get("value", self.value))
        while self.next <= t1:
            at = self.next
            self.count += 1
            body = json.dumps({"io": self.name, "n": self.count, "value": round(self.value, 3)}).encode()
            self.send(at, BROADCAST, 0x0800, udp(self.ip, "192.168.0.255", 5005, 5005, body, ident=self.count))
            self.next += max(1e-4, self.telemetry + self.random.uniform(-self.jitter, self.jitter))

    def output(self, t, x, u):
        return {"requests": float(self.requests)}
