"""Bluetooth LE link-layer advertising packets: build and decode.

A packet is kept the way sniffers and Wireshark's LINKTYPE_BLUETOOTH_LE_LL show it:
access address (4 bytes, little endian), PDU (2-byte header + payload) and the
3-byte CRC-24 (polynomial x^24+x^10+x^9+x^6+x^4+x^3+x+1, initial value 0x555555
for advertising, computed the way Ubertooth/btle sniffers do). Packets are
de-whitened. Event payloads: ``{"proto": "ble", "channel": 37, "rssi": -60,
"data": "<hex>", "src": "gateway"}``.
"""
from __future__ import annotations

import struct
from typing import Any, Mapping

ADV_ACCESS_ADDRESS = 0x8E89BED6
PDU_TYPES = {0: "ADV_IND", 1: "ADV_DIRECT_IND", 2: "ADV_NONCONN_IND", 3: "SCAN_REQ", 4: "SCAN_RSP",
             5: "CONNECT_IND", 6: "ADV_SCAN_IND", 7: "ADV_EXT_IND"}
AD_TYPES = {0x01: "Flags", 0x02: "Incomplete 16-bit UUIDs", 0x03: "Complete 16-bit UUIDs", 0x08: "Shortened local name",
            0x09: "Complete local name", 0x0A: "Tx power level", 0x16: "Service data (16-bit UUID)",
            0x19: "Appearance", 0xFF: "Manufacturer specific data"}
COMPANIES = {0x004C: "Apple", 0x0006: "Microsoft", 0x0059: "Nordic Semiconductor", 0x02E5: "Espressif",
             0x0075: "Samsung", 0x00E0: "Google", 0x0157: "Huami", 0x038F: "Xiaomi", 0xFFFF: "test / unassigned"}
UUIDS16 = {0x1800: "Generic Access", 0x1801: "Generic Attribute", 0x180A: "Device Information", 0x180D: "Heart Rate",
           0x180F: "Battery", 0x1809: "Health Thermometer", 0x181A: "Environmental Sensing", 0x1812: "HID",
           0x181C: "User Data", 0xFEAA: "Eddystone", 0xFE9F: "Google", 0xFD6F: "Exposure Notification", 0xFE2C: "Google Fast Pair"}
EDDYSTONE_SCHEMES = ["http://www.", "https://www.", "http://", "https://"]
EDDYSTONE_SUFFIXES = [".com/", ".org/", ".edu/", ".net/", ".info/", ".biz/", ".gov/", ".com", ".org", ".edu", ".net", ".info",
                      ".biz", ".gov"]
CHANNEL_MHZ = {37: 2402, 38: 2426, 39: 2480}


def _reverse24(value: int) -> int:
    return int(f"{value:024b}"[::-1], 2)


def crc24(pdu: bytes, init: int = 0x555555) -> bytes:
    state = _reverse24(init)
    for byte in pdu:
        for _ in range(8):
            next_bit = (state ^ byte) & 1
            byte >>= 1
            state >>= 1
            if next_bit:
                state |= 1 << 23
                state ^= 0x5A6000
    return state.to_bytes(3, "little")


def mac_bytes(address: str) -> bytes:
    """"C0:FF:EE:00:00:04" -> 6 bytes in air order (least significant first)."""
    return bytes(int(part, 16) for part in reversed(address.split(":")))


def mac_text(data: bytes) -> str:
    return ":".join(f"{b:02X}" for b in reversed(data[:6]))


def ad(kind: int, value: bytes) -> bytes:
    return bytes([len(value) + 1, kind]) + value


def adv_packet(address: str, adv_data: bytes, pdu_type: int = 2, random_address: bool = True) -> bytes:
    """Access address + advertising PDU (AdvA + AdvData) + CRC."""
    payload = mac_bytes(address) + adv_data
    if len(adv_data) > 31:
        raise ValueError("legacy advertising data is at most 31 bytes")
    header = bytes([(pdu_type & 0x0F) | (0x40 if random_address else 0), len(payload)])
    pdu = header + payload
    return struct.pack("<I", ADV_ACCESS_ADDRESS) + pdu + crc24(pdu)


def _field(label: str, value: Any, start: int | None = None, end: int | None = None, children=None) -> dict[str, Any]:
    item: dict[str, Any] = {"label": label, "value": value}
    if start is not None:
        item["bytes"] = [start, end if end is not None else start + 1]
    if children:
        item["children"] = children
    return item


def decode(data: bytes, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Decode an advertising-channel packet; byte ranges index `data`."""
    config = config or {}
    fields: list[dict[str, Any]] = []
    if len(data) < 9:
        return {"type": "malformed", "summary": f"short BLE packet ({len(data)} bytes)", "fields": []}
    (access,) = struct.unpack_from("<I", data)
    fields.append(_field("access address", f"0x{access:08X}" + (" (advertising)" if access == ADV_ACCESS_ADDRESS else ""), 0, 4))
    header, length = data[4], data[5]
    pdu_type = PDU_TYPES.get(header & 0x0F, f"type {header & 0x0F}")
    pdu_end = 6 + length
    crc_ok = len(data) >= pdu_end + 3 and crc24(data[4:pdu_end]) == data[pdu_end:pdu_end + 3]
    fields.append(_field("PDU header", f"{pdu_type}, TxAdd {'random' if header & 0x40 else 'public'}, length {length}", 4, 6,
                         [_field("PDU type", pdu_type, 4), _field("ChSel", (header >> 5) & 1, 4),
                          _field("TxAdd", "random" if header & 0x40 else "public", 4),
                          _field("RxAdd", "random" if header & 0x80 else "public", 4), _field("length", length, 5)]))
    summary = pdu_type
    source = target = None
    if (header & 0x0F) in (0, 2, 4, 6) and length >= 6:
        source = mac_text(data[6:12])
        fields.append(_field("AdvA", source, 6, 12))
        ads, name, info = _decode_ads(data, 12, pdu_end, config)
        fields.append(_field("AdvData", f"{pdu_end - 12} bytes", 12, pdu_end, ads))
        summary = f"{pdu_type} {name or ''}".strip() + (f" · {info}" if info else "")
    elif (header & 0x0F) == 3 and length >= 12:
        source, target = mac_text(data[6:12]), mac_text(data[12:18])
        fields += [_field("ScanA", source, 6, 12), _field("AdvA", target, 12, 18)]
        summary = f"SCAN_REQ → {target}"
    elif (header & 0x0F) == 5 and length >= 34:
        source, target = mac_text(data[6:12]), mac_text(data[12:18])
        ll = data[18:40]
        access, = struct.unpack_from("<I", ll)
        win_size, win_offset, interval, latency, timeout = ll[7], *struct.unpack_from("<HHHH", ll, 8)
        channels = int.from_bytes(ll[16:21], "little")
        used = [c for c in range(37) if channels >> c & 1]
        hop, sca = ll[21] & 0x1F, ll[21] >> 5
        fields += [_field("InitA", source, 6, 12), _field("AdvA", target, 12, 18),
                   _field("LLData", f"interval {interval * 1.25:g} ms", 18, 40, [
                       _field("access address", f"0x{access:08X}", 18, 22), _field("CRCInit", ll[4:7].hex(), 22, 25),
                       _field("WinSize / WinOffset", f"{win_size * 1.25:g} ms / {win_offset * 1.25:g} ms", 25, 28),
                       _field("interval", f"{interval * 1.25:g} ms", 28, 30), _field("latency", latency, 30, 32),
                       _field("supervision timeout", f"{timeout * 10} ms", 32, 34),
                       _field("channel map", f"{len(used)} of 37 data channels", 34, 39), _field("hop / SCA", f"{hop} / {sca}", 39, 40)])]
        summary = f"CONNECT_IND → {target}: AA 0x{access:08X}, interval {interval * 1.25:g} ms, {len(used)} channels"
    elif (header & 0x0F) == 1 and length >= 12:
        source, target = mac_text(data[6:12]), mac_text(data[12:18])
        fields += [_field("AdvA", source, 6, 12), _field("TargetA", target, 12, 18)]
    fields.append(_field("CRC", f"{data[pdu_end:pdu_end + 3].hex()} ({'ok' if crc_ok else 'BAD'})", pdu_end, pdu_end + 3))
    return {"type": pdu_type, "summary": summary + ("" if crc_ok else " [CRC error]"), "fields": fields,
            "source": source, "target": target, "crc_ok": crc_ok}


def _decode_ads(data: bytes, start: int, end: int, config: Mapping[str, Any]) -> tuple[list, str, str]:
    items, name, info = [], "", ""
    formats = {int(k, 0) if isinstance(k, str) else int(k): v for k, v in (config.get("manufacturer") or {}).items()}
    pos = start
    while pos < end:
        length = data[pos]
        if length == 0 or pos + 1 + length > end:
            break
        kind = data[pos + 1]
        value = data[pos + 2:pos + 1 + length]
        label = AD_TYPES.get(kind, f"AD type 0x{kind:02X}")
        children = None
        if kind == 0x01 and value:
            flags = [n for bit, n in ((0, "LE Limited"), (1, "LE General"), (2, "BR/EDR not supported")) if value[0] >> bit & 1]
            shown = ", ".join(flags)
        elif kind in (0x08, 0x09):
            name = value.decode(errors="replace")
            shown = repr(name)
        elif kind == 0x0A and value:
            shown = f"{struct.unpack('<b', value[:1])[0]} dBm"
        elif kind in (0x02, 0x03) and len(value) >= 2:
            uuids = [struct.unpack_from("<H", value, i)[0] for i in range(0, len(value) - 1, 2)]
            shown = ", ".join(f"0x{u:04X} {UUIDS16.get(u, '')}".strip() for u in uuids)
        elif kind == 0x16 and len(value) >= 2:
            (uuid,) = struct.unpack_from("<H", value)
            data_part = value[2:]
            shown, children = _service_data(uuid, data_part, pos + 4)
            if uuid == 0xFEAA and children:
                info = (info + " " if info else "") + shown
        elif kind == 0x19 and len(value) == 2:
            shown = f"0x{struct.unpack('<H', value)[0]:04X}"
        elif kind == 0xFF and len(value) >= 25 and value[:4] == b"\x4c\x00\x02\x15":
            uuid = value[4:20].hex()
            major, minor = struct.unpack_from(">HH", value, 20)
            power = struct.unpack_from("<b", value, 24)[0]
            shown = f"iBeacon {uuid[:8]}-{uuid[8:12]}-{uuid[12:16]}-{uuid[16:20]}-{uuid[20:]} major {major} minor {minor}"
            children = [_field("company", "0x004C Apple", pos + 2, pos + 4), _field("type", "iBeacon (0x02, 0x15)", pos + 4, pos + 6),
                        _field("proximity UUID", uuid.upper(), pos + 6, pos + 22), _field("major", major, pos + 22, pos + 24),
                        _field("minor", minor, pos + 24, pos + 26), _field("measured power", f"{power} dBm at 1 m", pos + 26)]
            info = f"iBeacon major {major} minor {minor}"
        elif kind == 0xFF and len(value) >= 2:
            (company,) = struct.unpack_from("<H", value)
            spec = formats.get(company)
            shown = f"{COMPANIES.get(company, f'0x{company:04X}')}: {value[2:].hex(' ')}"
            children = [_field("company", f"0x{company:04X} {COMPANIES.get(company, '')}".strip(), pos + 2, pos + 4)]
            if spec:
                offset, parts = 2, []
                for item in spec.get("fields", []):
                    fmt = {"u8": "<B", "i8": "<b", "u16": "<H", "i16": "<h", "u32": "<I", "i32": "<i"}.get(
                        str(item.get("format", "u8")), str(item.get("format", "<B")))
                    size = struct.calcsize(fmt)
                    if offset + size > len(value):
                        break
                    (raw,) = struct.unpack_from(fmt, value, offset)
                    scaled = raw * float(item.get("scale", 1.0))
                    text = f"{scaled:g} {item.get('unit', '')}".strip()
                    if item.get("enum"):
                        text = str({int(k): v for k, v in item["enum"].items()}.get(raw, raw))
                    if item.get("bits"):
                        text = f"0x{raw:02X} " + (", ".join(n for b, n in item["bits"].items() if raw >> int(b) & 1) or "none")
                    children.append(_field(str(item.get("name")), text, pos + 2 + offset, pos + 2 + offset + size))
                    if item.get("summary", True):
                        parts.append(f"{item.get('name')}={text}")
                    offset += size
                info = " ".join(parts)
                shown = spec.get("name", shown)
        else:
            shown = value.hex(" ")
        items.append(_field(label, shown, pos, pos + 1 + length, children))
        pos += 1 + length
    return items, name, info


def _service_data(uuid: int, data: bytes, start: int) -> tuple[str, list | None]:
    """Service data: Eddystone frames, Environmental Sensing temperature, else bytes."""
    name = UUIDS16.get(uuid, f"0x{uuid:04X}")
    if uuid == 0xFEAA and data:
        frame = data[0]
        if frame == 0x00 and len(data) >= 18:
            power = struct.unpack_from("<b", data, 1)[0]
            return (f"Eddystone-UID {data[2:12].hex().upper()}/{data[12:18].hex().upper()}",
                    [_field("frame", "UID", start), _field("Tx power", f"{power} dBm at 0 m", start + 1),
                     _field("namespace", data[2:12].hex().upper(), start + 2, start + 12),
                     _field("instance", data[12:18].hex().upper(), start + 12, start + 18)])
        if frame == 0x10 and len(data) >= 3:
            url = EDDYSTONE_SCHEMES[data[2]] if data[2] < 4 else "?"
            for byte in data[3:]:
                url += EDDYSTONE_SUFFIXES[byte] if byte < len(EDDYSTONE_SUFFIXES) else chr(byte)
            return f"Eddystone-URL {url}", [_field("frame", "URL", start), _field("URL", url, start + 2, start + len(data))]
        if frame == 0x20 and len(data) >= 14:
            battery, temp_raw, count, uptime = struct.unpack_from(">HhII", data, 2)
            return (f"Eddystone-TLM battery {battery} mV, {temp_raw / 256:.1f} degC, {count} adverts",
                    [_field("frame", "TLM", start), _field("battery", f"{battery} mV", start + 2, start + 4),
                     _field("temperature", f"{temp_raw / 256:.2f} degC", start + 4, start + 6),
                     _field("advert count", count, start + 6, start + 10),
                     _field("uptime", f"{uptime / 10:.1f} s", start + 10, start + 14)])
    if uuid == 0x181A and len(data) >= 2:
        (raw,) = struct.unpack_from("<h", data)
        return f"{name}: {raw / 100:.2f} degC", [_field("temperature (0x2A6E)", f"{raw / 100:.2f} degC", start, start + 2)]
    if uuid == 0x180F and data:
        return f"{name}: {data[0]} %", [_field("battery level", f"{data[0]} %", start)]
    return f"{name}: {data.hex(' ')}", None
