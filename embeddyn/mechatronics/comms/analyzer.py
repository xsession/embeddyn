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
and object descriptions, BLE manufacturer-data layouts, 802.11 station names.

`write_pcap(events, path, proto)` writes one capture per link type: SocketCAN
(227), Bluetooth LE LL (251) or 802.11 (105).
"""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import ble, canopen, wifi

LINKTYPES = {"can": 227, "ble": 251, "wifi": 105}


def protocol_of(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    proto = payload.get("proto")
    if proto in ("can", "ble", "wifi"):
        return str(proto)
    if "id" in payload and "data" in payload:          # Renode CAN events
        return "can"
    return None


def decode_event(event: Mapping[str, Any], config: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    config = config or {}
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


def decode_events(events: Iterable[Mapping[str, Any]], config: Mapping[str, Any] | None = None,
                  start: int = 0) -> list[dict[str, Any]]:
    rows = []
    for index, event in enumerate(events):
        row = decode_event(event, config)
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
