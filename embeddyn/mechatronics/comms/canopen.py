"""A small CANopen (CiA 301) stack for virtual boards, and a CANopen frame decoder.

`CanopenNode` is a python-plant base class. It keeps an object dictionary, runs
the NMT state machine (boot-up, pre-operational, operational, stopped, resets),
produces heartbeats (0x1017), answers SDO uploads and downloads (expedited, and
segmented uploads for long values such as 0x1008 device name), sends TPDOs on
SYNC or on an event timer, applies RPDOs and sends EMCY frames. Subclasses add
the application in `application(t0, t1, u)` and read or write the dictionary.

Frames go out on the `can_tx` event port and come in on `can_rx` (the bus's
wire); see `comms.can.CanBus`.

`decode(frame, config)` names any 11-bit frame by its CANopen function code and
decodes NMT, SYNC, EMCY, heartbeat, SDO and, with mappings from `config`, PDOs.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

NMT_STATES = {0x00: "boot-up", 0x04: "stopped", 0x05: "operational", 0x7F: "pre-operational"}
NMT_COMMANDS = {0x01: "start", 0x02: "stop", 0x80: "enter pre-operational", 0x81: "reset node",
                0x82: "reset communication"}
FUNCTIONS = [  # (base COB-ID, name, per node)
    (0x000, "NMT", False), (0x080, "SYNC/EMCY", True), (0x100, "TIME", False),
    (0x180, "TPDO1", True), (0x200, "RPDO1", True), (0x280, "TPDO2", True), (0x300, "RPDO2", True),
    (0x380, "TPDO3", True), (0x400, "RPDO3", True), (0x480, "TPDO4", True), (0x500, "RPDO4", True),
    (0x580, "SDO tx", True), (0x600, "SDO rx", True), (0x700, "heartbeat", True),
]
EMCY_CODES = {0x0000: "error reset / no error", 0x1000: "generic error", 0x2000: "current", 0x2310: "output overcurrent",
              0x3000: "voltage", 0x3210: "DC link overvoltage", 0x4000: "temperature", 0x4210: "device over temperature",
              0x5000: "device hardware", 0x6000: "device software", 0x7000: "additional modules",
              0x8000: "monitoring", 0x8110: "CAN overrun", 0x8120: "CAN error passive", 0x8130: "life guard / heartbeat",
              0x8140: "recovered from bus off", 0x8611: "following error", 0x9000: "external error"}
SDO_ABORTS = {0x05030000: "toggle bit not alternated", 0x05040001: "command specifier invalid",
              0x06010000: "unsupported access", 0x06010001: "read of a write-only object",
              0x06010002: "write to a read-only object", 0x06020000: "object does not exist",
              0x06040041: "object cannot be mapped", 0x06070010: "data type mismatch",
              0x06090011: "sub-index does not exist", 0x08000000: "general error"}
FORMATS = {"u8": "<B", "i8": "<b", "u16": "<H", "i16": "<h", "u32": "<I", "i32": "<i", "f32": "<f"}


def function_of(cob_id: int) -> tuple[str, int | None]:
    """CANopen function and node id of an 11-bit identifier (predefined connection set)."""
    if cob_id == 0x000:
        return "NMT", None
    if cob_id == 0x080:
        return "SYNC", None
    if cob_id == 0x100:
        return "TIME", None
    base = cob_id & 0x780
    node = cob_id & 0x7F
    for start, name, per_node in FUNCTIONS:
        if per_node and base == start and node:
            return ("EMCY" if name == "SYNC/EMCY" else name), node
    return "unknown", None


@dataclass
class Entry:
    value: Any
    kind: str = "u32"           # u8..i32, f32, str
    access: str = "rw"          # ro, rw, wo
    name: str = ""

    def encode(self) -> bytes:
        if self.kind == "str":
            return str(self.value).encode()
        return struct.pack(FORMATS[self.kind], self.value)

    def decode(self, data: bytes) -> Any:
        if self.kind == "str":
            return data.decode(errors="replace")
        size = struct.calcsize(FORMATS[self.kind])
        return struct.unpack(FORMATS[self.kind], data[:size].ljust(size, b"\0"))[0]


@dataclass
class Pdo:
    cob_id: int
    mapping: list[tuple[int, int]]            # (index, sub) of dictionary entries, packed in order
    sync_every: int = 0                       # TPDO: send on every n-th SYNC (0 = not on SYNC)
    event_timer: float = 0.0                  # TPDO: also send every event_timer seconds (0 = off)
    next_due: float = field(default=0.0, repr=False)
    sync_count: int = field(default=0, repr=False)


class CanopenNode:
    """Base class of a CANopen device as a python plant (see module docstring)."""

    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ("nmt_state",)
    states: tuple[str, ...] = ()
    event_inputs = ("can_rx",)
    event_outputs = ("can_tx",)
    device_type = 0x00000000
    device_name = "embeddyn node"

    def __init__(self, node_id: int = 1, heartbeat_ms: float = 100.0, boot_delay: float = 0.002,
                 name: str = "") -> None:
        if not 1 <= int(node_id) <= 127:
            raise ValueError("node_id must be 1..127")
        self.node_id = int(node_id)
        self.name = name or f"node{self.node_id}"
        self.boot_delay = float(boot_delay)
        self.od: dict[tuple[int, int], Entry] = {
            (0x1000, 0): Entry(self.device_type, "u32", "ro", "device type"),
            (0x1001, 0): Entry(0, "u8", "ro", "error register"),
            (0x1008, 0): Entry(self.device_name, "str", "ro", "manufacturer device name"),
            (0x1017, 0): Entry(int(heartbeat_ms), "u16", "rw", "producer heartbeat time"),
            (0x1018, 1): Entry(0xE3B0, "u32", "ro", "vendor id"),
        }
        self.tpdos: list[Pdo] = []
        self.rpdos: list[Pdo] = []
        self.state = None                     # NMT state code, None until boot-up
        self.next_heartbeat = None
        self._sdo_upload: dict[str, Any] | None = None
        self._out: list[tuple[float, str, Any]] = []
        self._emcy_active: set[int] = set()

    # -- plant interface -----------------------------------------------------------------------
    def initial_state(self) -> list[float]:
        return []

    def derivatives(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> list[float]:
        return []

    def output(self, t: float, x: Sequence[float], u: Mapping[str, float]) -> dict[str, float]:
        return {"nmt_state": float(self.state if self.state is not None else -1)}

    def pop_events(self) -> list[tuple[float, str, Any]]:
        out, self._out = self._out, []
        return out

    def advance(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        if self.state is None and t1 >= self.boot_delay:
            self._boot(max(t0, self.boot_delay))
        if self.state is None:
            return
        period = self.od[(0x1017, 0)].value / 1000.0
        while period > 0 and self.next_heartbeat is not None and self.next_heartbeat <= t1:
            self.send(self.next_heartbeat, 0x700 + self.node_id, bytes([self.state]))
            self.next_heartbeat += period
        if self.state == 0x05:
            for pdo in self.tpdos:
                while pdo.event_timer > 0 and pdo.next_due <= t1:
                    self.send(max(pdo.next_due, t0), pdo.cob_id, self.pack(pdo))
                    pdo.next_due += pdo.event_timer
        self.application(t0, t1, u)

    def receive(self, t: float, port: str, frame: Any) -> None:
        if port != "can_rx" or not isinstance(frame, dict) or frame.get("ext") or frame.get("src") == self.name:
            return
        cob, data = int(frame["id"]), bytes.fromhex(str(frame.get("data", "")))
        if cob == 0x000 and len(data) >= 2 and data[1] in (0, self.node_id):
            self.nmt(t, data[0])
        elif self.state is None:
            return
        elif cob == 0x080 and not data:
            self.on_sync(t)
        elif cob == 0x600 + self.node_id and self.state in (0x05, 0x7F):
            self.sdo_server(t, data)
        else:
            if self.state == 0x05:
                for pdo in self.rpdos:
                    if pdo.cob_id == cob:
                        self.unpack(pdo, data)
                        self.on_rpdo(t, pdo)
            self.on_frame(t, cob, data)

    # -- hooks for subclasses ----------------------------------------------------------------------
    def application(self, t0: float, t1: float, u: Mapping[str, float]) -> None:
        """Called every step after the stack's timers."""

    def on_frame(self, t: float, cob_id: int, data: bytes) -> None:
        """Every frame the stack did not consume (other nodes' PDOs, heartbeats, SDO responses...)."""

    def on_rpdo(self, t: float, pdo: Pdo) -> None:
        """After an RPDO wrote the dictionary."""

    # -- services ------------------------------------------------------------------------------------
    def send(self, t: float, cob_id: int, data: bytes = b"") -> None:
        self._out.append((float(t), "can_tx", {"proto": "can", "id": int(cob_id), "data": bytes(data).hex(),
                                                "src": self.name}))

    def _boot(self, t: float) -> None:
        self.state = 0x7F
        self.send(t, 0x700 + self.node_id, b"\x00")             # boot-up message
        period = self.od[(0x1017, 0)].value / 1000.0
        self.next_heartbeat = t + period if period > 0 else None

    def nmt(self, t: float, command: int) -> None:
        if command == 0x01:
            self.state = 0x05
            for pdo in self.tpdos:
                pdo.next_due = t + pdo.event_timer
        elif command == 0x02:
            self.state = 0x04
        elif command == 0x80:
            self.state = 0x7F
        elif command in (0x81, 0x82):
            self.state = None
            self._boot(t + 0.001)
        if self.next_heartbeat is None and self.state is not None:
            period = self.od[(0x1017, 0)].value / 1000.0
            self.next_heartbeat = t + period if period > 0 else None

    def on_sync(self, t: float) -> None:
        if self.state != 0x05:
            return
        for pdo in self.tpdos:
            if pdo.sync_every:
                pdo.sync_count += 1
                if pdo.sync_count >= pdo.sync_every:
                    pdo.sync_count = 0
                    self.send(t, pdo.cob_id, self.pack(pdo))

    def pack(self, pdo: Pdo) -> bytes:
        return b"".join(self.od[key].encode() for key in pdo.mapping)[:8]

    def unpack(self, pdo: Pdo, data: bytes) -> None:
        offset = 0
        for key in pdo.mapping:
            entry = self.od[key]
            size = struct.calcsize(FORMATS[entry.kind])
            if offset + size > len(data):
                break
            entry.value = entry.decode(data[offset:offset + size])
            offset += size

    def emcy(self, t: float, code: int, register: int = 0x01, extra: bytes = b"") -> None:
        """Send an emergency; code 0 clears (error reset)."""
        if code:
            self._emcy_active.add(code)
        else:
            self._emcy_active.clear()
        self.od[(0x1001, 0)].value = register if code else 0
        self.send(t, 0x080 + self.node_id, struct.pack("<HB", code, register if code else 0) + extra[:5].ljust(5, b"\0"))

    def sdo_server(self, t: float, data: bytes) -> None:
        if len(data) < 8:
            return
        ccs = data[0] >> 5
        index, sub = struct.unpack_from("<HB", data, 1)
        reply = 0x580 + self.node_id
        if ccs == 3:                                         # upload segment request
            upload = self._sdo_upload
            if upload is None:
                return self._abort(t, reply, index, sub, 0x05040001)
            toggle = (data[0] >> 4) & 1
            chunk = upload["data"][upload["pos"]:upload["pos"] + 7]
            upload["pos"] += len(chunk)
            last = upload["pos"] >= len(upload["data"])
            command = (toggle << 4) | ((7 - len(chunk)) << 1) | int(last)
            self.send(t, reply, bytes([command]) + chunk.ljust(7, b"\0"))
            if last:
                self._sdo_upload = None
            return
        entry = self.od.get((index, sub))
        if entry is None:
            return self._abort(t, reply, index, sub, 0x06020000)
        if ccs == 2:                                         # initiate upload
            if entry.access == "wo":
                return self._abort(t, reply, index, sub, 0x06010001)
            value = entry.encode()
            if len(value) <= 4:
                command = 0x43 | ((4 - len(value)) << 2)
                self.send(t, reply, bytes([command]) + data[1:4] + value.ljust(4, b"\0"))
            else:
                self._sdo_upload = {"data": value, "pos": 0}
                self.send(t, reply, bytes([0x41]) + data[1:4] + struct.pack("<I", len(value)))
        elif ccs == 1:                                       # initiate download (expedited)
            if entry.access == "ro":
                return self._abort(t, reply, index, sub, 0x06010002)
            size = 4 - ((data[0] >> 2) & 3) if data[0] & 0x02 else 4
            entry.value = entry.decode(data[4:4 + size])
            self.send(t, reply, bytes([0x60]) + data[1:4] + b"\0\0\0\0")
            self.on_download(t, index, sub)
        else:
            self._abort(t, reply, index, sub, 0x05040001)

    def on_download(self, t: float, index: int, sub: int) -> None:
        if (index, sub) == (0x1017, 0):
            period = self.od[(0x1017, 0)].value / 1000.0
            self.next_heartbeat = t + period if period > 0 else None

    def _abort(self, t: float, cob: int, index: int, sub: int, code: int) -> None:
        self.send(t, cob, bytes([0x80]) + struct.pack("<HBI", index, sub, code))


# ---------------------------------------------------------------------------------------------
# Decoder

def _field(label: str, value: Any, start: int | None = None, end: int | None = None, children=None) -> dict[str, Any]:
    item: dict[str, Any] = {"label": label, "value": value}
    if start is not None:
        item["bytes"] = [start, end if end is not None else start + 1]
    if children:
        item["children"] = children
    return item


def decode(cob_id: int, data: bytes, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """CANopen meaning of a frame: {"type", "node", "summary", "fields"}; byte ranges index `data`."""
    config = config or {}
    names = {int(k): str(v) for k, v in (config.get("nodes") or {}).items()}
    pdos = {int(k, 0) if isinstance(k, str) else int(k): v for k, v in (config.get("pdos") or {}).items()}
    kind, node = function_of(cob_id)
    who = f"{names.get(node, f'node {node}')}" if node else ""
    fields: list[dict[str, Any]] = []
    if kind == "NMT" and len(data) >= 2:
        command = NMT_COMMANDS.get(data[0], f"0x{data[0]:02X}")
        target = "all nodes" if data[1] == 0 else names.get(data[1], f"node {data[1]}")
        fields = [_field("command", f"{command} (0x{data[0]:02X})", 0), _field("node", target, 1)]
        return {"type": "NMT", "node": data[1] or None, "summary": f"NMT {command} → {target}", "fields": fields}
    if kind == "SYNC":
        counter = f" counter {data[0]}" if data else ""
        return {"type": "SYNC", "node": None, "summary": f"SYNC{counter}",
                "fields": [_field("counter", data[0], 0)] if data else []}
    if kind == "heartbeat" and data:
        state = NMT_STATES.get(data[0] & 0x7F, f"0x{data[0]:02X}")
        label = "boot-up" if data[0] == 0 else f"heartbeat {state}"
        return {"type": "heartbeat", "node": node, "summary": f"{who}: {label}",
                "fields": [_field("NMT state", f"{state} (0x{data[0]:02X})", 0)]}
    if kind == "EMCY" and len(data) >= 3:
        code, register = struct.unpack_from("<HB", data)
        text = EMCY_CODES.get(code) or EMCY_CODES.get(code & 0xFF00) or "manufacturer specific"
        fields = [_field("error code", f"0x{code:04X} {text}", 0, 2), _field("error register", f"0x{register:02X}", 2),
                  _field("manufacturer data", data[3:8].hex(" "), 3, 8)]
        return {"type": "EMCY", "node": node, "summary": f"{who}: EMCY 0x{code:04X} {text}", "fields": fields}
    if kind in ("SDO rx", "SDO tx") and len(data) >= 1:
        return _decode_sdo(kind, node, who, data, config)
    if kind.startswith(("TPDO", "RPDO")):
        spec = pdos.get(cob_id)
        title = (spec or {}).get("name") or f"{who} {kind}"
        if spec:
            offset, parts = 0, []
            for item in spec.get("fields", []):
                fmt = FORMATS.get(str(item.get("format", "u8")), str(item.get("format", "<B")))
                size = struct.calcsize(fmt)
                if offset + size > len(data):
                    break
                (raw,) = struct.unpack_from(fmt, data, offset)
                value = raw * float(item.get("scale", 1.0))
                unit = item.get("unit", "")
                shown = f"{value:g} {unit}".strip() if isinstance(value, float) else str(raw)
                if item.get("bits"):
                    shown = f"0x{raw:0{size * 2}X} " + ", ".join(name for bit, name in item["bits"].items()
                                                                 if raw >> int(bit) & 1)
                fields.append(_field(str(item.get("name", f"byte {offset}")), shown, offset, offset + size))
                parts.append(f"{item.get('name')}={shown}")
                offset += size
            return {"type": kind, "node": node, "summary": f"{title}: " + " ".join(parts[:4]), "fields": fields}
        return {"type": kind, "node": node, "summary": f"{title} [{data.hex(' ')}]", "fields": []}
    return {"type": kind, "node": node, "summary": f"COB-ID 0x{cob_id:03X} [{data.hex(' ')}]", "fields": []}


def _decode_sdo(kind: str, node: int | None, who: str, data: bytes, config: Mapping[str, Any]) -> dict[str, Any]:
    objects = {str(k).lower(): v for k, v in (config.get("objects") or {}).items()}
    command = data[0]
    spec = command >> 5
    request = kind == "SDO rx"
    fields = [_field("command", f"0x{command:02X}", 0)]
    index = sub = None
    if len(data) >= 4 and not ((request and spec == 3) or (not request and spec == 0)):
        index, sub = struct.unpack_from("<HB", data, 1)
        name = objects.get(f"0x{index:04x}") or objects.get(f"0x{index:04x}:{sub}") or STANDARD_OBJECTS.get(index, "")
        fields.append(_field("object", f"0x{index:04X}:{sub:02X} {name}".strip(), 1, 4))
    where = f"0x{index:04X}:{sub:02X}" if index is not None else ""
    arrow = f"→ {who}" if request else f"← {who}"
    if command == 0x80:
        (code,) = struct.unpack_from("<I", data, 4)
        fields.append(_field("abort code", f"0x{code:08X} {SDO_ABORTS.get(code, '')}".strip(), 4, 8))
        return {"type": "SDO abort", "node": node, "response": not request,
                "summary": f"SDO abort {where} {arrow}: {SDO_ABORTS.get(code, hex(code))}", "fields": fields}
    if request:
        if spec == 2:
            text = f"SDO read {where} {arrow}"
        elif spec == 1:
            size = 4 - ((command >> 2) & 3) if command & 0x02 else 4
            value = int.from_bytes(data[4:4 + size], "little")
            fields.append(_field("data", f"{value} (0x{value:X})", 4, 4 + size))
            text = f"SDO write {where} = {value} {arrow}"
        elif spec == 3:
            text = f"SDO upload segment (toggle {(command >> 4) & 1}) {arrow}"
        else:
            text = f"SDO request 0x{command:02X} {arrow}"
    else:
        if spec == 2 and command & 0x02:
            size = 4 - ((command >> 2) & 3) if command & 0x01 else 4
            value = int.from_bytes(data[4:4 + size], "little")
            fields.append(_field("data", f"{value} (0x{value:X})", 4, 4 + size))
            text = f"SDO {where} = {value} {arrow}"
        elif spec == 2:
            size = struct.unpack_from("<I", data, 4)[0] if command & 0x01 else 0
            fields.append(_field("size", f"{size} bytes (segmented)", 4, 8))
            text = f"SDO {where}: {size} bytes follow {arrow}"
        elif spec == 0:
            size = 7 - ((command >> 1) & 7)
            chunk = data[1:1 + size]
            fields.append(_field("segment", repr(chunk.decode(errors="replace")), 1, 1 + size))
            text = f"SDO segment {chunk.decode(errors='replace')!r}{' (last)' if command & 1 else ''} {arrow}"
        elif spec == 3:
            text = f"SDO write {where} done {arrow}"
        else:
            text = f"SDO response 0x{command:02X} {arrow}"
    return {"type": "SDO", "node": node, "response": not request, "summary": text, "fields": fields}


STANDARD_OBJECTS = {0x1000: "device type", 0x1001: "error register", 0x1008: "device name",
                    0x1009: "hardware version", 0x100A: "software version", 0x1017: "producer heartbeat time",
                    0x1018: "identity", 0x6040: "controlword", 0x6041: "statusword", 0x6064: "position actual",
                    0x606C: "velocity actual", 0x607A: "target position"}
