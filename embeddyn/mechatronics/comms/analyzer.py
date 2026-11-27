"""Protocol analyzer: decoded packets from bus and radio events, and pcap export.

`decode_event(event, config)` recognises CAN frames (also Renode's CAN events,
``{"id", "data"}``), BLE advertising packets and 802.11 frames and returns a packet
row for the Lab's analyzer:

```text
{"time", "topic", "proto": "CANopen"|"CAN"|"BLE"|"802.11", "type", "source", "target",
 "length", "summary", "fields": [{"label", "value", "bytes": [start, end], "children"}],
 "hex", "channel", "rssi", "ok"}
```

`config` is the project's ``metadata.analyzer`` section: CANopen node names, PDO
and object descriptions, BLE manufacturer-data layouts, 802.11 station names, and
``fields``: byte fields named while reverse engineering, per message key (see
`message_key`), applied on top of every protocol decoder. `dbc(config)` writes the
CAN ones (and CANopen PDO layouts) as a DBC file.

`write_pcap(events, path, proto)` writes one capture per link type: SocketCAN
(227), Bluetooth LE LL (251) or 802.11 (105).
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import ble, canopen, i2c, lora, nfc, rfid, wifi

LINKTYPES = {"can": 227, "ble": 251, "wifi": 105}


def protocol_of(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    proto = payload.get("proto")
    if proto in ("can", "ble", "wifi", "nfc", "rfid", "lora", "i2c"):
        return str(proto)
    if "id" in payload and "data" in payload:          # Renode CAN events
        return "can"
    return None


def message_key(row: Mapping[str, Any]) -> str:
    """The analyzer's message grouping (same as bytediff.js): CAN id, BLE sender + type, Wi-Fi link + type."""
    if row["proto"] in ("CAN", "CANopen"):
        return f"CAN 0x{int(row['id']):03X}"
    if row["proto"] == "BLE":
        return f"BLE {row.get('source') or '?'} {row['type']}"
    if row["proto"] in ("NFC", "RFID", "LoRa", "I2C"):
        return f"{row['proto']} {row.get('source') or row.get('target') or '?'} {row['type']}"
    return f"Wi-Fi {row.get('source') or '?'} → {row.get('target') or '?'} {row['type']}"


def read_field(data: bytes, start: int, reading: str) -> int | None:
    """Integer reading of bytes at `start`: "u8", "i8", "u16 LE", "i32 BE" ..."""
    parts = reading.split()
    kind, order = parts[0], (parts[1] if len(parts) > 1 else "LE")
    size = int(kind[1:]) // 8
    if size not in (1, 2, 4) or start < 0 or start + size > len(data):
        return None
    return int.from_bytes(data[start:start + size], "little" if order == "LE" else "big", signed=kind[0] == "i")


def apply_fields(row: dict[str, Any], fields: list[Mapping[str, Any]]) -> None:
    data = bytes.fromhex(row["hex"])
    found, parts = [], []
    for spec in fields:
        start, reading = int(spec.get("start", 0)), str(spec.get("reading", "u8"))
        raw = read_field(data, start, reading)
        if raw is None:
            continue
        value = raw * float(spec.get("scale", 1.0)) + float(spec.get("offset", 0.0))
        shown = str(int(value)) if float(value).is_integer() and abs(value) < 1e15 else f"{value:.6g}"
        text = f"{shown} {spec.get('unit', '')}".strip()
        size = int(reading.split()[0][1:]) // 8
        found.append({"label": str(spec.get("name", f"byte {start}")), "value": f"{text}  ({reading}, raw {raw})",
                      "bytes": [start, start + size]})
        parts.append(f"{spec.get('name')}={text}")
    if found:
        row["fields"] = [*row["fields"], {"label": "named fields", "value": f"{len(found)}", "children": found}]
        row["summary"] = f"{row['summary']} | " + " ".join(parts)
        row["named"] = {f["label"]: f["value"] for f in found}


def decode_event(event: Mapping[str, Any], config: Mapping[str, Any] | None = None,
                 context: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """`context` keeps exchange state between calls (NFC command → answer, Gen2 ACK → EPC); pass the
    same dict for every event of a capture or run."""
    config = config or {}
    row = _decode(event, config, context if context is not None else {})
    payload = event.get("payload")
    if row is not None and isinstance(payload, Mapping) and isinstance(payload.get("link"), Mapping):
        link = payload["link"]
        row["distance_m"] = link.get("distance_m")
        row["fields"] = [{"label": "link", "value": f"distance {link.get('distance_m'):g} m, RSSI {payload.get('rssi')} dBm, "
                                                    f"BER {link.get('ber'):.1e}"}, *row["fields"]]
    if row is not None and isinstance(payload, Mapping) and (payload.get("medium") or payload.get("error")):
        _medium_notes(row, payload)
    if row is not None and config.get("fields"):
        fields = (config.get("fields") or {}).get(message_key(row))
        if fields:
            apply_fields(row, list(fields))
    return row


def _decode(event: Mapping[str, Any], config: Mapping[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    payload = event.get("payload")
    proto = protocol_of(payload)
    if proto is None:
        return None
    try:
        data = bytes.fromhex(str(payload.get("data", "")))
    except ValueError:
        data = b""
    row: dict[str, Any] = {"time": float(event.get("time_seconds", 0.0)), "topic": event.get("topic"),
                           "hex": data.hex(), "length": len(data), "channel": payload.get("channel"),
                           "rssi": payload.get("rssi"), "ok": True}
    if proto == "can":
        frame_id = int(payload["id"])
        ext = bool(payload.get("ext"))
        bits = payload.get("bits")
        canopen_config = config.get("canopen")
        frame_fields = [{"label": "identifier", "value": f"0x{frame_id:0{8 if ext else 3}X}" + (" (29-bit)" if ext else "")},
                        {"label": "DLC", "value": len(data)}]
        if bits:
            rate = float((config.get("can") or {}).get("bitrate", 500_000))
            frame_fields.append({"label": "on the bus", "value": f"{bits} bits, {bits / rate * 1e6:.0f} µs at {rate / 1000:g} kbit/s"})
        sender = payload.get("src")
        if canopen_config is not None and not ext:
            decoded = canopen.decode(frame_id, data, canopen_config)
            row.update(proto="CANopen", type=decoded["type"], summary=decoded["summary"],
                       source=sender or _node_name(decoded.get("node"), canopen_config),
                       target=_canopen_target(decoded, canopen_config),
                       fields=[{"label": "CAN frame", "value": f"0x{frame_id:03X} [{len(data)}]", "children": frame_fields},
                               {"label": f"CANopen {decoded['type']}", "value": "", "children": decoded["fields"]}])
        else:
            row.update(proto="CAN", type="data", summary=f"0x{frame_id:03X} [{len(data)}] {data.hex(' ')}",
                       source=sender, target=None, fields=[{"label": "CAN frame", "value": "", "children": frame_fields}])
        row["id"] = frame_id
        return row
    if proto == "ble":
        decoded = ble.decode(data, config.get("ble"))
        names = {k.upper(): v for k, v in ((config.get("ble") or {}).get("devices") or {}).items()}
        source = decoded.get("source")
        row.update(proto="BLE", type=decoded["type"], summary=decoded["summary"],
                   source=names.get(source or "", source) or payload.get("src"), target=decoded.get("target") or "broadcast",
                   ok=decoded.get("crc_ok", True),
                   fields=[{"label": "radio", "value": f"channel {payload.get('channel')} "
                                                       f"({ble.CHANNEL_MHZ.get(payload.get('channel'), '?')} MHz), "
                                                       f"RSSI {payload.get('rssi')} dBm"}, *decoded["fields"]])
        return row
    if proto == "nfc":
        direction = str(payload.get("dir", "pcd"))
        stream = context.setdefault(f"nfc:{event.get('topic')}", {})
        decoded = nfc.decode(data, direction, payload.get("bits"), stream)
        reader, tag = (payload.get("src"), payload.get("peer")) if direction == "pcd" else (payload.get("peer"), payload.get("src"))
        row.update(proto="NFC", type=decoded["type"], summary=decoded["summary"], ok=decoded.get("crc_ok") is not False,
                   source=payload.get("src") or ("reader" if direction == "pcd" else "tag"),
                   target=(tag or "tag") if direction == "pcd" else (reader or "reader"),
                   fields=[{"label": "radio", "value": f"13.56 MHz ISO 14443-A, {'reader → tag (PCD)' if direction == 'pcd' else 'tag → reader (PICC)'}"
                                                       + (f", {payload['bits']}-bit short frame" if payload.get("bits") else "")},
                           *decoded["fields"]])
        if stream.get("ndef"):
            row["ndef"] = stream["ndef"]
        return row
    if proto == "rfid":
        bits_count = int(payload.get("bits", len(data) * 8))
        bits = rfid.bytes_to_bits(data.hex(), bits_count)
        air = str(payload.get("air", "em4100"))
        stream = context.setdefault(f"rfid:{event.get('topic')}", {})
        decoded = rfid.decode_em4100(bits) if air == "em4100" else rfid.decode_gen2(bits, str(payload.get("dir", "tag")), stream)
        band = "125 kHz EM4100 (ASK, Manchester, RF/64)" if air == "em4100" else "UHF 865-928 MHz EPC Gen2"
        row.update(proto="RFID", type=decoded["type"], summary=decoded["summary"], ok=decoded.get("ok", True),
                   source=payload.get("src"), target=payload.get("peer") or ("reader" if payload.get("dir", "tag") == "tag" else "tags"),
                   length=bits_count, fields=[{"label": "radio", "value": f"{band}, {bits_count} bits"},
                                              {"label": "bits", "value": bits}, *decoded["fields"]])
        return row
    if proto == "i2c":
        direction = str(payload.get("dir", "m2s"))
        stream = context.setdefault(f"i2c:{event.get('topic')}", {})
        decoded = i2c.decode(data, direction, config.get("i2c"), stream)
        address = decoded.get("address")
        names = {int(k, 0) if isinstance(k, str) else int(k): v.get("name") for k, v in ((config.get("i2c") or {}).get("devices") or {}).items()}
        device = names.get(address) or (f"0x{address:02X}" if address is not None else "?")
        row.update(proto="I2C", type=decoded["type"], summary=decoded["summary"], ok=True,
                   source=payload.get("src"), target=device if direction == "m2s" else "master",
                   fields=[{"label": "bus", "value": "master → slave" if direction == "m2s" else "slave → master"}, *decoded["fields"]])
        return row
    if proto == "lora":
        decoded = lora.decode(data, payload, config.get("lorawan"))
        row.update(proto="LoRa", type=decoded["type"], summary=decoded["summary"], ok=decoded.get("ok", True),
                   source=decoded.get("source") or payload.get("src"), target=decoded.get("target") or "network",
                   channel=payload.get("freq"), fields=decoded["fields"], snr=payload.get("snr"), sf=payload.get("sf"))
        return row
    decoded = wifi.decode(data, config.get("wifi"))
    row.update(proto="802.11", type=decoded["type"], summary=decoded["summary"], source=decoded.get("source"),
               target=decoded.get("target"), ok=decoded.get("fcs_ok", True),
               fields=[{"label": "radio", "value": f"channel {payload.get('channel')}, RSSI {payload.get('rssi')} dBm"},
                       *decoded["fields"]])
    return row


def _node_name(node: int | None, config: Mapping[str, Any]) -> str | None:
    if not node:
        return None
    names = {int(k): str(v) for k, v in (config.get("nodes") or {}).items()}
    return names.get(node, f"node {node}")


def _canopen_target(decoded: Mapping[str, Any], config: Mapping[str, Any]) -> str | None:
    kind = decoded.get("type", "")
    if kind in ("NMT",):
        return _node_name(decoded.get("node"), config) or "all"
    if kind in ("SYNC", "heartbeat", "EMCY") or str(kind).startswith("TPDO"):
        return "broadcast"
    if kind in ("SDO", "SDO abort") and decoded.get("response"):
        return _node_name(int(config.get("master", 0)) or None, config) or "SDO client"
    if str(kind).startswith("RPDO") or kind in ("SDO", "SDO abort"):
        return _node_name(decoded.get("node"), config)
    return None


def _medium_notes(row: dict[str, Any], payload: Mapping[str, Any]) -> None:
    """What the impaired medium did to this frame (comms.medium): a branch, a summary tag, byte marks."""
    notes = dict(payload.get("medium") or {})
    children = []
    tags = []
    if payload.get("error"):
        row["type"] = "error frame"
        tags.append("CAN error frame → retransmission")
    if payload.get("retry"):
        tags.append(f"retransmission {payload['retry']}")
    if notes.get("lost"):
        tags.append(f"lost ({notes['lost']})")
        children.append({"label": "lost", "value": f"{notes['lost']}: no receiver got this frame"})
    flipped = list(notes.get("flipped") or [])
    if flipped:
        tags.append(f"{len(flipped)} bit error{'s' if len(flipped) > 1 else ''}")
        children.append({"label": "flipped bits", "value": ", ".join(f"byte {b // 8} bit {7 - b % 8}" for b in flipped[:12])
                         + (" …" if len(flipped) > 12 else "")})
        if notes.get("original"):
            children.append({"label": "sent as", "value": " ".join(notes["original"][i:i + 2] for i in range(0, len(notes["original"]), 2))})
        row["flipped_bytes"] = sorted({b // 8 for b in flipped})
    if notes.get("crosstalk"):
        children.append({"label": "crosstalk from", "value": str(notes["crosstalk"])})
    if notes.get("timing"):
        tags.append("bit timing")
        children.append({"label": "bit timing", "value": str(notes["timing"])})
    row["fields"] = [{"label": "⚡ medium", "value": "; ".join(tags) or "impaired", "children": children}, *row["fields"]]
    row["summary"] = f"⚡ [{'; '.join(tags)}] {row['summary']}" if tags else row["summary"]
    row["impaired"] = True
    if payload.get("error") or notes.get("lost"):
        row["ok"] = False


def decode_events(events: Iterable[Mapping[str, Any]], config: Mapping[str, Any] | None = None,
                  start: int = 0) -> list[dict[str, Any]]:
    rows = []
    context: dict[str, Any] = {}
    for index, event in enumerate(events):
        row = decode_event(event, config, context)
        if row is not None:
            row["no"] = start + index
            rows.append(row)
    return rows


def write_pcap(events: Iterable[Mapping[str, Any]], path: Path | str | Any, proto: str) -> int:
    """Write the events of one protocol as a pcap file (a path or a binary file); returns the packet count."""
    if proto not in LINKTYPES:
        raise ValueError(f"proto must be one of {', '.join(LINKTYPES)}")
    if not isinstance(path, (str, Path)):
        return _write_pcap(events, path, proto)
    with open(path, "wb") as handle:
        return _write_pcap(events, handle, proto)


def _write_pcap(events: Iterable[Mapping[str, Any]], handle: Any, proto: str) -> int:
    count = 0
    events = sorted(events, key=lambda event: float(event.get("time_seconds", 0.0)))
    handle.write(struct.pack("<IHHiIII", 0xA1B23C4D, 2, 4, 0, 0, 65535, LINKTYPES[proto]))   # ns resolution
    for event in events:
        payload = event.get("payload")
        if protocol_of(payload) != proto:
            continue
        data = bytes.fromhex(str(payload.get("data", "")))
        if proto == "can":                         # struct can_frame, network byte order id
            can_id = int(payload["id"]) | (0x80000000 if payload.get("ext") else 0) | \
                (0x40000000 if payload.get("rtr") else 0)
            data = struct.pack(">IB3x", can_id, len(data)) + data.ljust(8, b"\0")[:8]
        t = max(0.0, float(event.get("time_seconds", 0.0)))
        seconds = int(t)
        nanos = int(round((t - seconds) * 1e9))
        if nanos >= 1_000_000_000:
            seconds, nanos = seconds + 1, nanos - 1_000_000_000
        handle.write(struct.pack("<IIII", seconds, nanos, len(data), len(data)) + data)
        count += 1
    return count


def _dbc_name(text: str) -> str:
    name = "".join(c if c.isalnum() else "_" for c in str(text)).strip("_") or "signal"
    return name if not name[0].isdigit() else "s_" + name


def dbc(config: Mapping[str, Any]) -> str:
    """A DBC file with the CAN messages the analyzer knows: named fields and CANopen PDO layouts."""
    messages: dict[int, dict[str, Any]] = {}
    sizes = {"u8": 1, "i8": 1, "u16": 2, "i16": 2, "u32": 4, "i32": 4, "f32": 4}
    for key, pdo in ((config.get("canopen") or {}).get("pdos") or {}).items():
        frame_id = int(key, 0) if isinstance(key, str) else int(key)
        message = messages.setdefault(frame_id, {"name": _dbc_name(pdo.get("name", f"PDO_{frame_id:03X}")), "signals": []})
        offset = 0
        for field in pdo.get("fields", []):
            kind = str(field.get("format", "u8"))
            size = sizes.get(kind, 1)
            message["signals"].append({"name": _dbc_name(field.get("name", f"b{offset}")), "start": offset,
                                       "reading": f"{kind if kind != 'f32' else 'u32'} LE",
                                       "scale": float(field.get("scale", 1.0)), "offset": 0.0, "unit": field.get("unit", "")})
            offset += size
    for key, fields in (config.get("fields") or {}).items():
        if not str(key).startswith("CAN 0x"):
            continue
        frame_id = int(str(key)[4:], 16)
        message = messages.setdefault(frame_id, {"name": f"MSG_{frame_id:03X}", "signals": []})
        names = {s["name"] for s in message["signals"]}
        for field in fields:
            name = _dbc_name(field.get("name", "signal"))
            if name in names:
                continue
            message["signals"].append({"name": name, "start": int(field.get("start", 0)), "reading": str(field.get("reading", "u8")),
                                       "scale": float(field.get("scale", 1.0)), "offset": float(field.get("offset", 0.0)),
                                       "unit": field.get("unit", "")})
    lines = ['VERSION ""', "", "NS_ :", "", "BS_:", "", "BU_: embeddyn", ""]
    for frame_id in sorted(messages):
        message = messages[frame_id]
        dlc = max([s["start"] + int(s["reading"].split()[0][1:]) // 8 for s in message["signals"]] or [0])
        ident = frame_id | (0x80000000 if frame_id > 0x7FF else 0)
        lines.append(f"BO_ {ident} {message['name']}: {max(dlc, 1)} embeddyn")
        for signal in message["signals"]:
            kind, *order = signal["reading"].split()
            bits = int(kind[1:])
            big = bool(order) and order[0] == "BE"
            start_bit = signal["start"] * 8 + 7 if big else signal["start"] * 8   # Motorola: MSB of the first byte
            sign = "-" if kind[0] == "i" else "+"
            raw_min, raw_max = ((-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) if sign == "-" else (0, 2 ** bits - 1))
            low = raw_min * signal["scale"] + signal["offset"]
            high = raw_max * signal["scale"] + signal["offset"]
            lines.append(f' SG_ {signal["name"]} : {start_bit}|{bits}@{0 if big else 1}{sign} '
                         f'({signal["scale"]:.10g},{signal["offset"]:.10g}) [{min(low, high):.10g}|{max(low, high):.10g}] '
                         f'"{signal["unit"]}" Vector__XXX')
        lines.append("")
    return "\n".join(lines) + "\n"
