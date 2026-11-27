"""Read bus and radio captures from real hardware into analyzer events.

* candump logs (``candump -l``: ``(1700000000.123456) can0 123#DEADBEEF``, also
  ``can0  123   [4]  DE AD BE EF`` as printed by ``candump can0``, ``R`` remote frames and
  29-bit ids);
* pcap and pcapng files with SocketCAN (227), Bluetooth LE LL (251), Bluetooth LE LL with
  PHDR (256) or IEEE 802.11 (105) packets.

Events look like the co-simulation's: ``{"time_seconds", "topic", "payload", "source"}``
with payloads ``{"proto": "can"|"ble"|"wifi", ...}``, times relative to the first packet.
"""
from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Any

_LOG = re.compile(r"^\s*\((?P<t>\d+(?:\.\d+)?)\)\s+(?P<iface>\S+)\s+(?P<id>[0-9A-Fa-f]{1,8})#(?P<rest>\S*)")
_DUMP = re.compile(r"^\s*(?:\((?P<t>\d+(?:\.\d+)?)\)\s+)?(?P<iface>\S+)\s+(?P<id>[0-9A-Fa-f]{3,8})\s+\[(?P<dlc>\d)\]\s*(?P<data>(?:[0-9A-Fa-f]{2}\s*)*|remote request)")


def read_candump(text: str, topic: str = "can") -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    index = 0
    last = None                                  # lines without a timestamp: 1 ms after the previous frame
    for line in text.splitlines():
        match = _LOG.match(line)
        if match:
            raw_id, rest = match.group("id"), match.group("rest")
            rtr = rest.upper().startswith("R")
            data = b"" if rtr else bytes.fromhex(rest.split("#")[-1] if "##" in rest else rest)
            stamp = float(match.group("t"))
            iface = match.group("iface")
        else:
            match = _DUMP.match(line)
            if not match:
                continue
            raw_id = match.group("id")
            text_data = match.group("data") or ""
            rtr = text_data.startswith("remote")
            data = b"" if rtr else bytes.fromhex(text_data.replace(" ", ""))
            stamp = float(match.group("t")) if match.group("t") else (last + 1e-3 if last is not None else index * 1e-3)
            iface = match.group("iface")
        last = stamp
        frame_id = int(raw_id, 16)
        payload: dict[str, Any] = {"proto": "can", "id": frame_id, "data": data.hex(), "src": iface}
        if len(raw_id) > 3 or frame_id > 0x7FF:
            payload["ext"] = True
        if rtr:
            payload["rtr"] = True
        events.append({"time_seconds": stamp, "topic": topic, "payload": payload, "source": "capture"})
        index += 1
    return _relative(events)


def _relative(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if events:
        start = min(e["time_seconds"] for e in events)
        for event in events:
            event["time_seconds"] -= start
        events.sort(key=lambda e: e["time_seconds"])
    return events


def _packet_event(link: int, stamp: float, data: bytes) -> dict[str, Any] | None:
    if link == 227 and len(data) >= 8:                     # SocketCAN can_frame, id in network order
        (can_id,) = struct.unpack_from(">I", data)
        if can_id & 0x20000000:                            # error frame
            return None
        length = data[4]
        payload: dict[str, Any] = {"proto": "can", "id": can_id & 0x1FFFFFFF, "data": data[8:8 + length].hex(),
                                   "src": "capture"}
        if can_id & 0x80000000:
            payload["ext"] = True
        if can_id & 0x40000000:
            payload["rtr"] = True
        return {"time_seconds": stamp, "topic": "can", "payload": payload, "source": "capture"}
    if link in (251, 256):
        channel, rssi = None, None
        if link == 256 and len(data) >= 10:                # LE LL with PHDR: rf channel, signal power ...
            rf, power = data[0], struct.unpack_from("<b", data, 1)[0]
            channel = {0: 37, 12: 38, 39: 39}.get(rf, rf)
            rssi = power
            data = data[10:]
        return {"time_seconds": stamp, "topic": "ble", "source": "capture",
                "payload": {"proto": "ble", "channel": channel, "rssi": rssi, "data": data.hex(), "src": "capture"}}
    if link == 105:
        return {"time_seconds": stamp, "topic": "wifi", "source": "capture",
                "payload": {"proto": "wifi", "channel": None, "rssi": None, "data": data.hex(), "src": "capture"}}
    return None


def read_pcap(raw: bytes) -> list[dict[str, Any]]:
    """Events from a pcap or pcapng file (supported link types only; others are skipped)."""
    if len(raw) < 24:
        raise ValueError("not a pcap file: too short")
    if raw[:4] == b"\x0a\x0d\x0d\x0a":
        return _relative(_read_pcapng(raw))
    magic = raw[:4]
    formats = {b"\xd4\xc3\xb2\xa1": ("<", 1e-6), b"\xa1\xb2\xc3\xd4": (">", 1e-6),
               b"\x4d\x3c\xb2\xa1": ("<", 1e-9), b"\xa1\xb2\x3c\x4d": (">", 1e-9)}
    if magic not in formats:
        raise ValueError("not a pcap or pcapng file")
    order, unit = formats[magic]
    link = struct.unpack_from(order + "I", raw, 20)[0] & 0x0FFFFFFF
    events, pos = [], 24
    while pos + 16 <= len(raw):
        seconds, fraction, captured, _ = struct.unpack_from(order + "IIII", raw, pos)
        data = raw[pos + 16:pos + 16 + captured]
        pos += 16 + captured
        event = _packet_event(link, seconds + fraction * unit, data)
        if event is not None:
            events.append(event)
    return _relative(events)


def _read_pcapng(raw: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    interfaces: list[tuple[int, float]] = []          # (link type, seconds per timestamp unit)
    order = "<"
    pos = 0
    while pos + 12 <= len(raw):
        block_type = struct.unpack_from(order + "I", raw, pos)[0]
        if block_type == 0x0A0D0D0A:
            order = "<" if raw[pos + 8:pos + 12] == b"\x4d\x3c\x2b\x1a" else ">"
            interfaces = []
        length = struct.unpack_from(order + "I", raw, pos + 4)[0]
        if length < 12 or pos + length > len(raw):
            break
        body = raw[pos + 8:pos + length - 4]
        if block_type == 1:                                 # interface description
            link = struct.unpack_from(order + "H", body, 0)[0]
            resolution = 1e-6
            opt = 8
            while opt + 4 <= len(body):
                code, size = struct.unpack_from(order + "HH", body, opt)
                if code == 0:
                    break
                if code == 9 and size >= 1:                 # if_tsresol
                    value = body[opt + 4]
                    resolution = 2.0 ** -(value & 0x7F) if value & 0x80 else 10.0 ** -value
                opt += 4 + ((size + 3) & ~3)
            interfaces.append((link, resolution))
        elif block_type == 6 and len(body) >= 20:           # enhanced packet
            interface, high, low, captured, _ = struct.unpack_from(order + "IIIII", body, 0)
            if interface < len(interfaces):
                link, resolution = interfaces[interface]
                event = _packet_event(link, ((high << 32) | low) * resolution, body[20:20 + captured])
                if event is not None:
                    events.append(event)
        pos += length
    return events


def read_capture(path: Path | str) -> list[dict[str, Any]]:
    """A capture file by content: pcap/pcapng, else a candump log."""
    raw = Path(path).read_bytes()
    if raw[:4] in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d", b"\x0a\x0d\x0d\x0a"):
        return read_pcap(raw)
    events = read_candump(raw.decode(errors="replace"))
    if not events:
        raise ValueError(f"{path}: no packets recognised (pcap, pcapng or candump log expected)")
    return events
