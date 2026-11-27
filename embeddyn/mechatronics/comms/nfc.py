"""NFC at 13.56 MHz: ISO/IEC 14443-A frames and NFC Forum Type 2 tags (NTAG/Ultralight), with NDEF.

Frames are the bytes on the air with CRC_A where the standard has one; REQA/WUPA
are 7-bit short frames. Event payloads:
``{"proto": "nfc", "dir": "pcd"|"picc", "data": "<hex>", "bits": 7, "src": "reader"}``
(``dir``: reader → tag is PCD, tag → reader is PICC; ``bits`` only for short frames).

`decode(data, direction, context)` follows the exchange: an answer is decoded in the
light of the command before it (READ → 16 data bytes, SELECT → SAK...). The context
also collects the tag memory pages read so far and parses the NDEF message in them.
"""
from __future__ import annotations

import struct
from typing import Any

URI_PREFIXES = ["", "http://www.", "https://www.", "http://", "https://", "tel:", "mailto:", "ftp://anonymous:anonymous@",
                "ftp://ftp.", "ftps://", "sftp://", "smb://", "nfs://", "ftp://", "dav://", "news:", "telnet://", "imap:",
                "rtsp://", "urn:", "pop:", "sip:", "sips:", "tftp:", "btspp://", "btl2cap://", "btgoep://", "tcpobex://",
                "irdaobex://", "file://", "urn:epc:id:", "urn:epc:tag:", "urn:epc:pat:", "urn:epc:raw:", "urn:epc:",
                "urn:nfc:"]
COMMANDS = {0x26: "REQA", 0x52: "WUPA", 0x30: "READ", 0xA2: "WRITE", 0x60: "GET_VERSION", 0x3A: "FAST_READ",
            0x1B: "PWD_AUTH", 0x39: "READ_CNT", 0x50: "HLTA", 0xE0: "RATS"}


def crc_a(data: bytes) -> bytes:
    crc = 0x6363
    for byte in data:
        byte ^= crc & 0xFF
        byte = (byte ^ (byte << 4)) & 0xFF
        crc = ((crc >> 8) ^ (byte << 8) ^ (byte << 3) ^ (byte >> 4)) & 0xFFFF
    return struct.pack("<H", crc)


def with_crc(data: bytes) -> bytes:
    return data + crc_a(data)


def bcc(uid_part: bytes) -> int:
    value = 0
    for byte in uid_part:
        value ^= byte
    return value


def ndef_uri(uri: str) -> bytes:
    """A one-record NDEF message with a well-known URI record."""
    code = max((i for i, prefix in enumerate(URI_PREFIXES) if prefix and uri.startswith(prefix)),
               key=lambda i: len(URI_PREFIXES[i]), default=0)
    payload = bytes([code]) + uri[len(URI_PREFIXES[code]):].encode()
    return bytes([0xD1, 1, len(payload)]) + b"U" + payload


def ndef_text(text: str, lang: str = "en") -> bytes:
    payload = bytes([len(lang)]) + lang.encode() + text.encode()
    return bytes([0xD1, 1, len(payload)]) + b"T" + payload


def type2_memory(uid: bytes, ndef: bytes, pages: int = 45) -> bytes:
    """NTAG213-like memory: UID/serial pages 0-2, capability container page 3, NDEF TLV from page 4."""
    if len(uid) != 7:
        raise ValueError("Type 2 tags have a 7-byte UID")
    head = bytes([uid[0], uid[1], uid[2], 0x88 ^ uid[0] ^ uid[1] ^ uid[2], uid[3], uid[4], uid[5], uid[6],
                  uid[3] ^ uid[4] ^ uid[5] ^ uid[6], 0x48, 0x00, 0x00, 0xE1, 0x10, 0x12, 0x00])
    tlv = bytes([0x03, len(ndef)]) + ndef + b"\xFE"
    body = tlv.ljust((pages - 4) * 4, b"\0")
    return head + body


def parse_ndef(message: bytes) -> list[dict[str, Any]]:
    records, pos = [], 0
    while pos + 3 <= len(message):
        header = message[pos]
        tnf, short, has_id = header & 7, bool(header & 0x10), bool(header & 0x08)
        type_len = message[pos + 1]
        pos += 2
        if short:
            length = message[pos]
            pos += 1
        else:
            if pos + 4 > len(message):
                break
            (length,) = struct.unpack_from(">I", message, pos)
            pos += 4
        id_len = message[pos] if has_id else 0
        pos += int(has_id)
        rtype = message[pos:pos + type_len]
        pos += type_len + id_len
        payload = message[pos:pos + length]
        pos += length
        record: dict[str, Any] = {"tnf": tnf, "type": rtype.decode(errors="replace"), "payload": payload.hex()}
        if tnf == 1 and rtype == b"U" and payload:
            record["value"] = URI_PREFIXES[payload[0]] if payload[0] < len(URI_PREFIXES) else ""
            record["value"] += payload[1:].decode(errors="replace")
        elif tnf == 1 and rtype == b"T" and payload:
            lang_len = payload[0] & 0x3F
            record["lang"] = payload[1:1 + lang_len].decode(errors="replace")
            record["value"] = payload[1 + lang_len:].decode(errors="replace")
        elif tnf == 2:
            record["value"] = payload.decode(errors="replace")
        records.append(record)
        if header & 0x40:                                    # ME: message end
            break
    return records


def ndef_from_memory(memory: bytes) -> list[dict[str, Any]] | None:
    pos = 16
    while pos + 2 <= len(memory):
        tag = memory[pos]
        if tag == 0x00:
            pos += 1
            continue
        if tag == 0xFE:
            return None
        length = memory[pos + 1]
        if tag == 0x03:
            message = memory[pos + 2:pos + 2 + length]
            return parse_ndef(message) if len(message) == length else None
        pos += 2 + length
    return None


def _field(label: str, value: Any, start: int | None = None, end: int | None = None, children=None) -> dict[str, Any]:
    item: dict[str, Any] = {"label": label, "value": value}
    if start is not None:
        item["bytes"] = [start, end if end is not None else start + 1]
    if children:
        item["children"] = children
    return item


def decode(data: bytes, direction: str, bits: int | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Decode one ISO 14443-A / Type 2 frame; `context` carries the exchange (last command, tag memory)."""
    ctx = context if context is not None else {}
    fields: list[dict[str, Any]] = []
    crc_ok = None
    if direction == "pcd":
        if bits == 7 and data:
            name = COMMANDS.get(data[0], f"short frame 0x{data[0]:02X}")
            ctx["last"] = name
            return {"type": name, "summary": f"{name} (7-bit)", "fields": [_field("command", name, 0)], "crc_ok": None}
        if len(data) >= 2 and data[0] in (0x93, 0x95, 0x97):
            level = {0x93: 1, 0x95: 2, 0x97: 3}[data[0]]
            nvb = data[1]
            fields += [_field("SEL", f"cascade level {level}", 0), _field("NVB", f"0x{nvb:02X}", 1)]
            if nvb == 0x20:
                ctx["last"] = f"ANTICOLLISION CL{level}"
                return {"type": "ANTICOLLISION", "summary": f"ANTICOLLISION CL{level}", "fields": fields, "crc_ok": None}
            if nvb == 0x70 and len(data) >= 9:
                crc_ok = crc_a(data[:7]) == data[7:9]
                uid_part = data[2:6]
                fields += [_field("UID CLn", uid_part.hex(" ").upper(), 2, 6), _field("BCC", f"0x{data[6]:02X}", 6),
                           _field("CRC_A", f"{data[7:9].hex()} ({'ok' if crc_ok else 'BAD'})", 7, 9)]
                ctx["last"] = f"SELECT CL{level}"
                return {"type": "SELECT", "summary": f"SELECT CL{level} {uid_part.hex(' ').upper()}", "fields": fields, "crc_ok": crc_ok}
        command = data[0] if data else None
        name = COMMANDS.get(command, f"command 0x{command:02X}" if command is not None else "empty")
        if len(data) >= 3:
            crc_ok = crc_a(data[:-2]) == data[-2:]
            fields.append(_field("CRC_A", f"{data[-2:].hex()} ({'ok' if crc_ok else 'BAD'})", len(data) - 2, len(data)))
        fields.insert(0, _field("command", name, 0))
        summary = name
        if command == 0x30 and len(data) >= 2:
            ctx["read_page"] = data[1]
            fields.insert(1, _field("page", data[1], 1))
            summary = f"READ page {data[1]}"
        elif command == 0xA2 and len(data) >= 6:
            fields.insert(1, _field("page", data[1], 1))
            fields.insert(2, _field("data", data[2:6].hex(" "), 2, 6))
            summary = f"WRITE page {data[1]} = {data[2:6].hex(' ')}"
        elif command == 0x50:
            summary = "HLTA (halt)"
        ctx["last"] = name
        return {"type": name, "summary": summary, "fields": fields, "crc_ok": crc_ok}
    # PICC (tag) answers, read in the light of the last command
    last = ctx.get("last", "")
    if last in ("REQA", "WUPA") and len(data) == 2:
        (atqa,) = struct.unpack("<H", data)
        size = {0: "single (4 bytes)", 1: "double (7 bytes)", 2: "triple (10 bytes)"}.get((atqa >> 6) & 3, "?")
        return {"type": "ATQA", "summary": f"ATQA 0x{atqa:04X}: UID size {size}",
                "fields": [_field("ATQA", f"0x{atqa:04X}", 0, 2), _field("UID size", size, 0)], "crc_ok": None}
    if last.startswith("ANTICOLLISION") and len(data) == 5:
        ok = bcc(data[:4]) == data[4]
        cascade = " (cascade tag 0x88: more UID follows)" if data[0] == 0x88 else ""
        return {"type": "UID", "summary": f"UID CLn {data[:4].hex(' ').upper()}{cascade}", "crc_ok": ok,
                "fields": [_field("UID CLn", data[:4].hex(" ").upper(), 0, 4), _field("BCC", f"0x{data[4]:02X} ({'ok' if ok else 'BAD'})", 4)]}
    if last.startswith("SELECT") and len(data) == 3:
        sak = data[0]
        crc_ok = crc_a(data[:1]) == data[1:]
        meaning = "UID not complete" if sak & 0x04 else ("ISO 14443-4" if sak & 0x20 else "Type 2 tag (MIFARE Ultralight / NTAG)"
                                                          if sak == 0x00 else "MIFARE Classic" if sak in (0x08, 0x18) else "")
        return {"type": "SAK", "summary": f"SAK 0x{sak:02X} {meaning}".strip(), "crc_ok": crc_ok,
                "fields": [_field("SAK", f"0x{sak:02X} {meaning}".strip(), 0), _field("CRC_A", f"{data[1:].hex()} ({'ok' if crc_ok else 'BAD'})", 1, 3)]}
    if last == "READ" and len(data) == 18:
        crc_ok = crc_a(data[:16]) == data[16:]
        page = int(ctx.get("read_page", 0))
        memory = bytearray(ctx.get("memory", b""))
        if len(memory) < page * 4 + 16:
            memory.extend(b"\0" * (page * 4 + 16 - len(memory)))
        memory[page * 4:page * 4 + 16] = data[:16]
        ctx["memory"] = bytes(memory)
        fields = [_field(f"page {page + i}", data[i * 4:i * 4 + 4].hex(" "), i * 4, i * 4 + 4) for i in range(4)]
        summary = f"data pages {page}-{page + 3}"
        if page == 0:
            uid = bytes(data[0:3]) + bytes(data[4:8])
            fields.append(_field("UID", uid.hex(":").upper(), 0, 8))
            if data[12] == 0xE1:
                fields.append(_field("capability container", f"NDEF v{data[13] >> 4}.{data[13] & 15}, {data[14] * 8} bytes", 12, 16))
        records = ndef_from_memory(ctx["memory"])
        if records:
            ctx["ndef"] = records
            fields.append(_field("NDEF (so far)", f"{len(records)} record(s)",
                                 children=[_field(f"record {i + 1} type {r['type']}", r.get("value", r["payload"])) for i, r in enumerate(records)]))
            summary += " · NDEF " + "; ".join(str(r.get("value", r["type"])) for r in records)
        fields.append(_field("CRC_A", f"{data[16:].hex()} ({'ok' if crc_ok else 'BAD'})", 16, 18))
        return {"type": "READ data", "summary": summary, "fields": fields, "crc_ok": crc_ok}
    if len(data) == 1 and data[0] & 0x0F == 0x0A:
        return {"type": "ACK", "summary": "ACK", "fields": [_field("ACK", "0xA", 0)], "crc_ok": None}
    if len(data) == 1:
        return {"type": "NAK", "summary": f"NAK 0x{data[0]:X}", "fields": [_field("NAK", f"0x{data[0]:X}", 0)], "crc_ok": None}
    return {"type": "answer", "summary": f"answer [{data.hex(' ')}]", "fields": [], "crc_ok": None}
