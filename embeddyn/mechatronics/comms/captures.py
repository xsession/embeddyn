"""Read bus and radio captures from real hardware into analyzer events.

* candump logs (``candump -l``: ``(1700000000.123456) can0 123#DEADBEEF``, CAN FD
  ``123##1DEADBEEF``, also ``can0  123   [4]  DE AD BE EF`` as printed by ``candump can0``,
  ``R`` remote frames and 29-bit ids);
* pcap and pcapng files with SocketCAN (227), Bluetooth LE LL (251), Bluetooth LE LL with
  PHDR (256), IEEE 802.11 (105) or 802.11 with radiotap (127), LoRaTap (270), ISO 14443 (264),
  Linux I2C (209), Ethernet (1) packets, and the analyzer's own UART (147), SPI (148) and
  RS-485 (150) captures.

Events look like the co-simulation's: ``{"time_seconds", "topic", "payload", "source"}``
with payloads ``{"proto": "can"|"ble"|"wifi"|..., ...}``, times relative to the first packet.
"""
from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Any

_LOG = re.compile(r"^\s*\((?P<t>\d+(?:\.\d+)?)\)\s+(?P<iface>\S+)\s+(?P<id>[0-9A-Fa-f]{1,8})#(?P<rest>\S*)")
_DUMP = re.compile(r"^\s*(?:\((?P<t>\d+(?:\.\d+)?)\)\s+)?(?P<iface>\S+)\s+(?P<id>[0-9A-Fa-f]{3,8})\s+\[(?P<dlc>\d{1,2})\]\s*(?P<data>(?:[0-9A-Fa-f]{2}\s*)*|remote request)")


def read_candump(text: str, topic: str = "can") -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    index = 0
    last = None                                  # lines without a timestamp: 1 ms after the previous frame
    for line in text.splitlines():
        match = _LOG.match(line)
        if match:
            raw_id, rest = match.group("id"), match.group("rest")
            rtr = rest.upper().startswith("R")
            fd = rest.startswith("#")                       # CAN FD: 123##<flags nibble><data>
            data = b"" if rtr else bytes.fromhex(rest[2:] if fd else rest)
            stamp = float(match.group("t"))
            iface = match.group("iface")
        else:
            match = _DUMP.match(line)
            if not match:
                continue
            raw_id, fd = match.group("id"), int(match.group("dlc")) > 8
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
        if fd:
            payload["fd"] = True
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


def _with_fcs(frame: bytes) -> bytes:
    """802.11 and Ethernet frames as the decoders expect them, ending in their CRC-32 FCS: captures
    that do not say whether the FCS is there are checked, and it is added when missing."""
    from .wifi import fcs
    if len(frame) >= 8 and fcs(frame[:-4]) == frame[-4:]:
        return frame
    return frame + fcs(frame)


def _ble_channel(rf: int) -> int:
    """BLE channel index of an RF channel (2402 + 2·rf MHz): 0 → 37, 12 → 38, 39 → 39, others data 0..36."""
    if rf in (0, 12, 39):
        return {0: 37, 12: 38, 39: 39}[rf]
    return rf - 1 if rf < 12 else rf - 2


def _packet_event(link: int, stamp: float, data: bytes) -> dict[str, Any] | list[dict[str, Any]] | None:
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
        if link == 256 and len(data) >= 10:                # LE LL with PHDR: rf channel, signal power ... flags
            rf, power = data[0], struct.unpack_from("<b", data, 1)[0]
            (flags,) = struct.unpack_from("<H", data, 8)
            channel = _ble_channel(rf)
            rssi = power if flags & 0x0002 else None       # signal power valid
            data = data[10:]
        return {"time_seconds": stamp, "topic": "ble", "source": "capture",
                "payload": {"proto": "ble", "channel": channel, "rssi": rssi, "data": data.hex(), "src": "capture"}}
    if link == 105:                                        # 802.11, with or without the FCS
        return {"time_seconds": stamp, "topic": "wifi", "source": "capture",
                "payload": {"proto": "wifi", "channel": None, "rssi": None, "data": _with_fcs(data).hex(), "src": "capture"}}
    if link == 127 and len(data) >= 8:                     # radiotap: keep channel and signal, drop the header
        length, present = struct.unpack_from("<HI", data, 2)
        channel, rssi, fcs = None, None, None             # no flags field: the FCS may or may not be there
        pos = 8
        while present & 0x80000000 and pos + 4 <= length:   # extended presence bitmaps
            present2 = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            if not present2 & 0x80000000:
                break
        fields = [(0, 8, 8), (1, 1, 1), (2, 1, 1), (3, 4, 2), (4, 2, 1), (5, 1, 1)]   # (bit, size, alignment)
        for bit, size, align in fields:
            if not present >> bit & 1:
                continue
            pos += -pos % align
            if bit == 1:
                fcs = bool(data[pos] & 0x10)
            elif bit == 3:
                freq = struct.unpack_from("<H", data, pos)[0]
                channel = (freq - 2407) // 5 if 2412 <= freq <= 2472 else 14 if freq == 2484 else (freq - 5000) // 5
            elif bit == 5:
                rssi = struct.unpack_from("<b", data, pos)[0]
            pos += size
        frame = data[length:]
        if fcs is None:
            frame = _with_fcs(frame)
        elif not fcs:                                      # our decoder expects the FCS: add it
            from .wifi import fcs as wifi_fcs
            frame += wifi_fcs(frame)
        return {"time_seconds": stamp, "topic": "wifi", "source": "capture",
                "payload": {"proto": "wifi", "channel": channel, "rssi": rssi, "data": frame.hex(), "src": "capture"}}
    if link == 270 and len(data) >= 4:                     # LoRaTap v0 (15-byte header)
        length = struct.unpack_from(">H", data, 2)[0]
        lora: dict[str, Any] = {"proto": "lora", "data": data[length:].hex(), "src": "capture"}
        if data[0] == 0 and length >= 15:
            freq, bandwidth, sf, packet_rssi, _, _, snr = struct.unpack_from(">IBBBBBb", data, 4)
            lora.update(freq=freq / 1e6, bw=125 * bandwidth, sf=sf, rssi=packet_rssi - 139, snr=snr / 4)
        return {"time_seconds": stamp, "topic": "lora", "source": "capture", "payload": lora}
    if link == 264 and len(data) >= 4:                     # ISO 14443: version, event, length
        event, length = data[1], struct.unpack_from(">H", data, 2)[0]
        if event not in (0xFC, 0xFD, 0xFE, 0xFF):
            return None                                    # field on/off and other events
        frame = data[4:4 + length]
        to_tag = event in (0xFC, 0xFE)
        short = to_tag and len(frame) == 1 and frame[0] in (0x26, 0x52)
        if event in (0xFC, 0xFD) and len(frame) > 2 and not short:   # the sniffer dropped the CRC_A: put it back
            from .nfc import with_crc
            frame = with_crc(frame)
        nfc: dict[str, Any] = {"proto": "nfc", "dir": "pcd" if to_tag else "picc", "data": frame.hex(), "src": "capture"}
        if short:
            nfc["bits"] = 7
        return {"time_seconds": stamp, "topic": "nfc", "source": "capture", "payload": nfc}
    if link == 209 and len(data) >= 6:                     # Linux I2C: bus, flags, address byte, data
        flags = struct.unpack_from(">I", data, 1)[0]
        message = data[5:]
        if flags & 1 and len(message) > 1:                 # a read: the request, then the data the slave drove
            return [{"time_seconds": stamp, "topic": "i2c", "source": "capture",
                     "payload": {"proto": "i2c", "dir": "m2s", "data": message[:1].hex(), "src": "capture"}},
                    {"time_seconds": stamp, "topic": "i2c", "source": "capture",
                     "payload": {"proto": "i2c", "dir": "s2m", "data": message[1:].hex(), "src": "capture"}}]
        return {"time_seconds": stamp, "topic": "i2c", "source": "capture",
                "payload": {"proto": "i2c", "dir": "m2s", "data": message.hex(), "src": "capture"}}
    if link == 1 and len(data) >= 14:                      # Ethernet II / 802.3, FCS kept or added
        return {"time_seconds": stamp, "topic": "eth", "source": "capture",
                "payload": {"proto": "eth", "data": _with_fcs(data).hex(), "src": "capture"}}
    if link in (147, 150):                                 # the analyzer's UART and RS-485 captures: raw bytes
        proto = "uart" if link == 147 else "rs485"
        return {"time_seconds": stamp, "topic": proto, "source": "capture",
                "payload": {"proto": proto, "data": data.hex(), "src": "capture"}}
    if link == 148 and len(data) >= 1:                     # SPI: direction byte (0 MOSI, 1 MISO), then the bytes
        return {"time_seconds": stamp, "topic": "spi", "source": "capture",
                "payload": {"proto": "spi", "dir": "miso" if data[0] & 1 else "mosi", "data": data[1:].hex(), "src": "capture"}}
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
            events.extend(event if isinstance(event, list) else [event])
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
                    events.extend(event if isinstance(event, list) else [event])
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
