"""IEEE 802.11 frames: build and decode (management, control, data with LLC/SNAP, IPv4, UDP).

Frames are kept as Wireshark's LINKTYPE_IEEE802_11 shows them: MAC header, body and
the 4-byte FCS (CRC-32, little endian). Event payloads: ``{"proto": "wifi",
"channel": 6, "rssi": -48, "data": "<hex>", "src": "bridge"}``. Networks are open
(no encryption), so the sniffer sees the payloads, as on a lab network.
"""
from __future__ import annotations

import json
import struct
import zlib
from typing import Any, Mapping

SUBTYPES = {
    (0, 0): "Association Request", (0, 1): "Association Response", (0, 4): "Probe Request",
    (0, 5): "Probe Response", (0, 8): "Beacon", (0, 10): "Disassociation", (0, 11): "Authentication",
    (0, 12): "Deauthentication", (0, 13): "Action",
    (1, 11): "RTS", (1, 12): "CTS", (1, 13): "ACK",
    (2, 0): "Data", (2, 4): "Null", (2, 8): "QoS Data",
}
ELEMENTS = {0: "SSID", 1: "Supported Rates", 3: "DS Parameter Set", 5: "TIM", 48: "RSN", 50: "Extended Rates",
            221: "Vendor Specific"}
BROADCAST = "FF:FF:FF:FF:FF:FF"


def mac(text: str) -> bytes:
    return bytes(int(part, 16) for part in text.split(":"))


def mac_text(data: bytes) -> str:
    return ":".join(f"{b:02X}" for b in data[:6])


def fcs(frame: bytes) -> bytes:
    return struct.pack("<I", zlib.crc32(frame) & 0xFFFFFFFF)


def frame(kind: int, subtype: int, addr1: str, addr2: str | None = None, addr3: str | None = None, body: bytes = b"",
          to_ds: bool = False, from_ds: bool = False, sequence: int = 0, duration: int = 0) -> bytes:
    control = (kind << 2) | (subtype << 4) | ((int(to_ds) | (int(from_ds) << 1)) << 8)
    header = struct.pack("<HH", control, duration) + mac(addr1)
    if addr2 is not None:
        header += mac(addr2)
    if addr3 is not None:
        header += mac(addr3) + struct.pack("<H", (sequence & 0xFFF) << 4)
    data = header + body
    return data + fcs(data)


def element(eid: int, value: bytes) -> bytes:
    return bytes([eid, len(value)]) + value


def beacon(bssid: str, ssid: str, channel: int, sequence: int, timestamp_us: int, interval_tu: int = 100) -> bytes:
    body = struct.pack("<QHH", timestamp_us, interval_tu, 0x0401)  # ESS, short slot
    body += element(0, ssid.encode()) + element(1, bytes([0x82, 0x84, 0x8B, 0x96, 0x0C, 0x12, 0x18, 0x24]))
    body += element(3, bytes([channel]))
    return frame(0, 8, BROADCAST, bssid, bssid, body, sequence=sequence)


def ipv4_udp(src_ip: str, dst_ip: str, src_port: int, dst_port: int, payload: bytes, ident: int = 0) -> bytes:
    udp = struct.pack("!HHHH", src_port, dst_port, 8 + len(payload), 0) + payload
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), ident & 0xFFFF, 0x4000, 64, 17, 0,
                         bytes(int(x) for x in src_ip.split(".")), bytes(int(x) for x in dst_ip.split(".")))
    checksum = _ip_checksum(header)
    return header[:10] + struct.pack("!H", checksum) + header[12:] + udp


def _ip_checksum(header: bytes) -> int:
    total = sum(struct.unpack(f"!{len(header) // 2}H", header))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def llc_snap(ethertype: int, payload: bytes) -> bytes:
    return bytes([0xAA, 0xAA, 0x03, 0, 0, 0]) + struct.pack("!H", ethertype) + payload


def _field(label: str, value: Any, start: int | None = None, end: int | None = None, children=None) -> dict[str, Any]:
    item: dict[str, Any] = {"label": label, "value": value}
    if start is not None:
        item["bytes"] = [start, end if end is not None else start + 1]
    if children:
        item["children"] = children
    return item


def decode(data: bytes, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Decode an 802.11 frame with FCS; byte ranges index `data`."""
    config = config or {}
    names = {k.upper(): v for k, v in (config.get("stations") or {}).items()}
    label = lambda address: f"{names[address]} ({address})" if address in names else address   # noqa: E731
    if len(data) < 14:
        return {"type": "malformed", "summary": f"short 802.11 frame ({len(data)} bytes)", "fields": []}
    control, duration = struct.unpack_from("<HH", data)
    kind, subtype = (control >> 2) & 3, (control >> 4) & 15
    to_ds, from_ds = bool(control & 0x100), bool(control & 0x200)
    name = SUBTYPES.get((kind, subtype), f"type {kind} subtype {subtype}")
    body_end = len(data) - 4
    fcs_ok = fcs(data[:body_end]) == data[body_end:]
    flags = [n for bit, n in ((8, "ToDS"), (9, "FromDS"), (10, "MoreFrag"), (11, "Retry"), (12, "PwrMgt"),
                              (14, "Protected")) if control >> bit & 1]
    fields = [_field("frame control", f"{name}, flags {', '.join(flags) or 'none'}", 0, 2,
                     [_field("type", ["management", "control", "data", "extension"][kind], 0),
                      _field("subtype", subtype, 0), _field("flags", ", ".join(flags) or "none", 1)]),
              _field("duration", f"{duration} µs", 2, 4)]
    addresses = [mac_text(data[4:10])]
    fields.append(_field("receiver (addr1)", label(addresses[0]), 4, 10))
    pos = 10
    if kind != 1 or subtype not in (12, 13):
        if len(data) >= 16:
            addresses.append(mac_text(data[10:16]))
            fields.append(_field("transmitter (addr2)", label(addresses[1]), 10, 16))
            pos = 16
    if kind in (0, 2) and len(data) >= 24:
        addresses.append(mac_text(data[16:22]))
        fields.append(_field("addr3 (" + ("BSSID" if kind == 0 or not (to_ds or from_ds) else "DA" if to_ds else "SA") + ")",
                             label(addresses[2]), 16, 22))
        (seq,) = struct.unpack_from("<H", data, 22)
        fields.append(_field("sequence", f"{seq >> 4} fragment {seq & 15}", 22, 24))
        pos = 24
    source = addresses[1] if len(addresses) > 1 else None
    target = addresses[0]
    summary = name
    if kind == 0:
        body, info = _management(subtype, data, pos, body_end)
        fields += body
        summary += info
    elif kind == 2 and subtype in (0, 8) and body_end > pos:
        if subtype == 8:
            fields.append(_field("QoS control", data[pos:pos + 2].hex(), pos, pos + 2))
            pos += 2
        body, info = _payload(data, pos, body_end)
        fields += body
        summary += info
        if to_ds and len(addresses) > 2:
            target = addresses[2]
    fields.append(_field("FCS", f"{data[body_end:].hex()} ({'ok' if fcs_ok else 'BAD'})", body_end, len(data)))
    return {"type": name, "summary": summary + ("" if fcs_ok else " [FCS error]"), "fields": fields,
            "source": names.get(source, source) if source else None, "target": names.get(target, target),
            "fcs_ok": fcs_ok}


def _management(subtype: int, data: bytes, pos: int, end: int) -> tuple[list, str]:
    fields, info = [], ""
    if subtype in (8, 5) and end - pos >= 12:
        timestamp, interval, capability = struct.unpack_from("<QHH", data, pos)
        fields.append(_field("fixed parameters", f"interval {interval} TU, capability 0x{capability:04X}", pos, pos + 12,
                             [_field("timestamp", f"{timestamp} µs", pos, pos + 8),
                              _field("beacon interval", f"{interval} TU ({interval * 1.024:.1f} ms)", pos + 8, pos + 10),
                              _field("capability", f"0x{capability:04X}", pos + 10, pos + 12)]))
        pos += 12
    elif subtype == 11 and end - pos >= 6:
        algorithm, sequence, status = struct.unpack_from("<HHH", data, pos)
        fields.append(_field("authentication", f"{'open' if algorithm == 0 else algorithm}, seq {sequence}, status {status}", pos, pos + 6))
        info = f" seq {sequence}" + (" (success)" if status == 0 and sequence == 2 else "")
        pos += 6
    elif subtype == 0 and end - pos >= 4:
        fields.append(_field("capability / listen interval", data[pos:pos + 4].hex(" "), pos, pos + 4))
        pos += 4
    elif subtype == 1 and end - pos >= 6:
        capability, status, aid = struct.unpack_from("<HHH", data, pos)
        fields.append(_field("association", f"status {status}, AID {aid & 0x3FFF}", pos, pos + 6))
        info = f" status {status}" + (f", AID {aid & 0x3FFF}" if status == 0 else "")
        pos += 6
    elements = []
    while pos + 2 <= end:
        eid, length = data[pos], data[pos + 1]
        value = data[pos + 2:pos + 2 + length]
        if eid == 0:
            ssid = value.decode(errors="replace")
            shown = repr(ssid) if ssid else "(wildcard)"
            info = (info + f" SSID={ssid!r}") if ssid else info + " SSID=*"
        elif eid == 3 and value:
            shown = f"channel {value[0]}"
        elif eid in (1, 50):
            shown = ", ".join(f"{(b & 0x7F) / 2:g}" + ("*" if b & 0x80 else "") for b in value) + " Mb/s"
        else:
            shown = value.hex(" ")
        elements.append(_field(ELEMENTS.get(eid, f"element {eid}"), shown, pos, pos + 2 + length))
        pos += 2 + length
    if elements:
        fields.append(_field("tagged parameters", f"{len(elements)} elements", elements[0]["bytes"][0], pos, elements))
    return fields, info


def _payload(data: bytes, pos: int, end: int) -> tuple[list, str]:
    fields, info = [], ""
    if end - pos >= 8 and data[pos:pos + 3] == b"\xAA\xAA\x03":
        (ethertype,) = struct.unpack_from("!H", data, pos + 6)
        fields.append(_field("LLC/SNAP", f"EtherType 0x{ethertype:04X}" + (" IPv4" if ethertype == 0x0800 else ""), pos, pos + 8))
        pos += 8
        if ethertype == 0x0800 and end - pos >= 20:
            ihl = (data[pos] & 15) * 4
            total, ident, _, ttl, proto, checksum = struct.unpack_from("!HHHBBH", data, pos + 2)
            src = ".".join(str(b) for b in data[pos + 12:pos + 16])
            dst = ".".join(str(b) for b in data[pos + 16:pos + 20])
            ok = _ip_checksum(data[pos:pos + ihl]) == 0
            fields.append(_field("IPv4", f"{src} → {dst}, proto {proto}, TTL {ttl}", pos, pos + ihl,
                                 [_field("source", src, pos + 12, pos + 16), _field("destination", dst, pos + 16, pos + 20),
                                  _field("header checksum", f"0x{checksum:04X} ({'ok' if ok else 'BAD'})", pos + 10, pos + 12)]))
            info = f" {src} → {dst}"
            pos += ihl
            if proto == 17 and end - pos >= 8:
                sport, dport, length, _ = struct.unpack_from("!HHHH", data, pos)
                fields.append(_field("UDP", f"{sport} → {dport}, length {length}", pos, pos + 8))
                info += f" UDP {sport}→{dport}"
                pos += 8
                payload = data[pos:end]
                text = payload.decode(errors="replace")
                try:
                    obj = json.loads(text)
                    children = [_field(str(k), json.dumps(v) if not isinstance(v, str) else v) for k, v in obj.items()] \
                        if isinstance(obj, dict) else None
                    fields.append(_field("JSON", f"{len(payload)} bytes", pos, end, children))
                    if isinstance(obj, dict):
                        info += " " + " ".join(f"{k}={v}" for k, v in list(obj.items())[:4])
                except ValueError:
                    fields.append(_field("data", repr(text[:60]), pos, end))
                return fields, info
    fields.append(_field("data", data[pos:end].hex(" "), pos, end))
    return fields, info
