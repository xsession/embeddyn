"""LoRa radio parameters and LoRaWAN 1.0.x frames: build, decrypt, check and decode.

Event payloads: ``{"proto": "lora", "freq": 868.1, "sf": 7, "bw": 125, "cr": "4/5",
"rssi": -97, "snr": 8.5, "data": "<PHYPayload hex>", "src": "node"}``.

* `time_on_air` follows Semtech's formula (AN1200.13): preamble, explicit header,
  CRC, low-data-rate optimisation for SF11/12 at 125 kHz.
* LoRaWAN: MHDR (message type), FHDR (DevAddr, FCtrl, FCnt, FOpts), FPort,
  FRMPayload, MIC; Join-request / Join-accept. With the session keys in the analyzer
  config (``lorawan: {devices: {"26011BDA": {nwkskey, appskey}}}``) the MIC is checked
  (AES-CMAC) and FRMPayload decrypted (AES-CTR as in the spec). The `cryptography`
  package does the AES; without it frames decode without those two steps.
"""
from __future__ import annotations

import math
import struct
from typing import Any, Mapping

MTYPES = {0: "Join-request", 1: "Join-accept", 2: "Unconfirmed Data Up", 3: "Unconfirmed Data Down",
          4: "Confirmed Data Up", 5: "Confirmed Data Down", 6: "Rejoin-request", 7: "Proprietary"}
MAC_COMMANDS_UP = {0x02: ("LinkCheckReq", 0), 0x03: ("LinkADRAns", 1), 0x04: ("DutyCycleAns", 0), 0x05: ("RXParamSetupAns", 1),
                   0x06: ("DevStatusAns", 2), 0x07: ("NewChannelAns", 1), 0x08: ("RXTimingSetupAns", 0)}
MAC_COMMANDS_DOWN = {0x02: ("LinkCheckAns", 2), 0x03: ("LinkADRReq", 4), 0x04: ("DutyCycleReq", 1), 0x05: ("RXParamSetupReq", 4),
                     0x06: ("DevStatusReq", 0), 0x07: ("NewChannelReq", 5), 0x08: ("RXTimingSetupReq", 1)}


def time_on_air(payload_len: int, sf: int = 7, bw_khz: float = 125.0, cr: int = 1, preamble: int = 8,
                explicit_header: bool = True, crc: bool = True) -> float:
    """Seconds on air of a LoRa packet (cr: 1..4 for 4/5..4/8)."""
    t_sym = (2 ** sf) / (bw_khz * 1000.0)
    low_dr = 1 if t_sym > 0.016 else 0
    numerator = 8 * payload_len - 4 * sf + 28 + 16 * int(crc) - 20 * int(not explicit_header)
    payload_symbols = 8 + max(math.ceil(numerator / (4 * (sf - 2 * low_dr))) * (cr + 4), 0)
    return (preamble + 4.25) * t_sym + payload_symbols * t_sym


def _aes():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.cmac import CMAC
        return Cipher, algorithms, modes, CMAC
    except ImportError:                                     # pragma: no cover - optional
        return None


def _aes_ecb(key: bytes, block: bytes) -> bytes:
    Cipher, algorithms, modes, _ = _aes()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(block) + encryptor.finalize()


def cmac(key: bytes, data: bytes) -> bytes:
    _, algorithms, _, CMAC = _aes()
    mac = CMAC(algorithms.AES(key))
    mac.update(data)
    return mac.finalize()


def crypt_payload(key: bytes, dev_addr: int, fcnt: int, uplink: bool, payload: bytes) -> bytes:
    """FRMPayload encryption = decryption (LoRaWAN 1.0.x, 4.3.3)."""
    out = bytearray()
    for i in range(math.ceil(len(payload) / 16)):
        block = bytes([0x01, 0, 0, 0, 0, 0 if uplink else 1]) + struct.pack("<II", dev_addr, fcnt) + bytes([0, i + 1])
        stream = _aes_ecb(key, block)
        chunk = payload[i * 16:(i + 1) * 16]
        out += bytes(a ^ b for a, b in zip(chunk, stream))
    return bytes(out)


def mic(key: bytes, dev_addr: int, fcnt: int, uplink: bool, message: bytes) -> bytes:
    b0 = bytes([0x49, 0, 0, 0, 0, 0 if uplink else 1]) + struct.pack("<II", dev_addr, fcnt) + bytes([0, len(message)])
    return cmac(key, b0 + message)[:4]


def data_frame(dev_addr: int, fcnt: int, port: int, payload: bytes, nwkskey: bytes, appskey: bytes,
               confirmed: bool = False, uplink: bool = True, adr: bool = True, ack: bool = False, fopts: bytes = b"") -> bytes:
    """A complete PHYPayload (encrypted, with MIC)."""
    mtype = (4 if confirmed else 2) if uplink else (5 if confirmed else 3)
    fctrl = (0x80 if adr else 0) | (0x20 if ack else 0) | (len(fopts) & 0x0F)
    message = bytes([mtype << 5]) + struct.pack("<IBH", dev_addr, fctrl, fcnt & 0xFFFF) + fopts
    if payload or port:
        key = nwkskey if port == 0 else appskey
        message += bytes([port]) + crypt_payload(key, dev_addr, fcnt, uplink, payload)
    return message + mic(nwkskey, dev_addr, fcnt, uplink, message)


def join_request(app_eui: bytes, dev_eui: bytes, dev_nonce: int, appkey: bytes) -> bytes:
    message = bytes([0x00]) + app_eui[::-1] + dev_eui[::-1] + struct.pack("<H", dev_nonce)
    return message + cmac(appkey, message)[:4]


def _field(label: str, value: Any, start: int | None = None, end: int | None = None, children=None) -> dict[str, Any]:
    item: dict[str, Any] = {"label": label, "value": value}
    if start is not None:
        item["bytes"] = [start, end if end is not None else start + 1]
    if children:
        item["children"] = children
    return item


def _mac_commands(data: bytes, uplink: bool, offset: int) -> list[dict[str, Any]]:
    table = MAC_COMMANDS_UP if uplink else MAC_COMMANDS_DOWN
    items, pos = [], 0
    while pos < len(data):
        name, size = table.get(data[pos], (f"CID 0x{data[pos]:02X}", 0))
        items.append(_field(name, data[pos + 1:pos + 1 + size].hex(" ") or "-", offset + pos, offset + pos + 1 + size))
        pos += 1 + size
    return items


def decode(data: bytes, radio: Mapping[str, Any] | None = None, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    radio = radio or {}
    config = config or {}
    devices = {k.upper(): v for k, v in ((config.get("devices") or {}).items())}
    fields: list[dict[str, Any]] = []
    sf, bw = radio.get("sf"), radio.get("bw")
    if sf and bw:
        cr = {"4/5": 1, "4/6": 2, "4/7": 3, "4/8": 4}.get(str(radio.get("cr", "4/5")), 1)
        airtime = time_on_air(len(data), int(sf), float(bw), cr)
        fields.append(_field("radio", f"{radio.get('freq')} MHz SF{sf} BW{bw} CR {radio.get('cr', '4/5')}, "
                                      f"RSSI {radio.get('rssi')} dBm, SNR {radio.get('snr')} dB, time on air {airtime * 1000:.1f} ms"))
    if len(data) < 5:
        return {"type": "malformed", "summary": f"short LoRaWAN frame ({len(data)} bytes)", "fields": fields, "ok": False}
    mtype = data[0] >> 5
    name = MTYPES[mtype]
    fields.append(_field("MHDR", f"{name}, major {data[0] & 3}", 0))
    if mtype == 0 and len(data) == 23:
        app_eui, dev_eui = data[1:9][::-1].hex().upper(), data[9:17][::-1].hex().upper()
        (nonce,) = struct.unpack_from("<H", data, 17)
        fields += [_field("JoinEUI", app_eui, 1, 9), _field("DevEUI", dev_eui, 9, 17), _field("DevNonce", f"0x{nonce:04X}", 17, 19),
                   _field("MIC", data[19:].hex(), 19, 23)]
        return {"type": name, "summary": f"Join-request DevEUI {dev_eui} nonce 0x{nonce:04X}", "fields": fields, "ok": True,
                "source": dev_eui}
    if mtype == 1:
        fields.append(_field("encrypted join-accept", data[1:].hex(" "), 1, len(data)))
        return {"type": name, "summary": f"Join-accept ({len(data)} bytes, encrypted)", "fields": fields, "ok": True}
    if mtype not in (2, 3, 4, 5) or len(data) < 12:
        return {"type": name, "summary": name, "fields": fields, "ok": True}
    uplink = mtype in (2, 4)
    dev_addr, fctrl, fcnt = struct.unpack_from("<IBH", data, 1)
    fopts_len = fctrl & 0x0F
    flags = [n for bit, n in ((7, "ADR"), (6, "ADRACKReq" if uplink else "RFU"), (5, "ACK"), (4, "ClassB" if uplink else "FPending"))
             if fctrl >> bit & 1]
    pos = 8 + fopts_len
    fhdr = [_field("DevAddr", f"{dev_addr:08X}", 1, 5), _field("FCtrl", f"0x{fctrl:02X} {', '.join(flags) or ''}".strip(), 5),
            _field("FCnt", fcnt, 6, 8)]
    if fopts_len:
        fhdr.append(_field("FOpts (MAC commands)", f"{fopts_len} bytes", 8, pos, _mac_commands(data[8:pos], uplink, 8)))
    fields.append(_field("FHDR", f"DevAddr {dev_addr:08X} FCnt {fcnt}", 1, pos, fhdr))
    body_end = len(data) - 4
    port = data[pos] if pos < body_end else None
    keys = devices.get(f"{dev_addr:08X}") or {}
    ok, info = True, ""
    plain = None
    mic_text = data[body_end:].hex()
    if keys and _aes() is not None:
        nwk = bytes.fromhex(str(keys.get("nwkskey", "")))
        app = bytes.fromhex(str(keys.get("appskey", "")))
        full_fcnt = fcnt | (int(keys.get("fcnt_high", 0)) << 16)
        if len(nwk) == 16:
            ok = mic(nwk, dev_addr, full_fcnt, uplink, data[:body_end]) == data[body_end:]
            mic_text += " (ok)" if ok else " (BAD: wrong key or corrupted)"
        if port is not None and pos + 1 < body_end:
            key = nwk if port == 0 else app
            if len(key) == 16:
                plain = crypt_payload(key, dev_addr, full_fcnt, uplink, data[pos + 1:body_end])
    if port is not None:
        fields.append(_field("FPort", port, pos))
        frm = data[pos + 1:body_end]
        children = None
        if plain is not None:
            children = [_field("decrypted", plain.hex(" "), pos + 1, body_end)]
            layout = (config.get("ports") or {}).get(str(port)) or (config.get("ports") or {}).get(port)
            if layout:
                offset, parts = 0, []
                for item in layout.get("fields", []):
                    fmt = {"u8": ">B", "i8": ">b", "u16": ">H", "i16": ">h", "u32": ">I", "i32": ">i"}.get(str(item.get("format")), ">B")
                    size = struct.calcsize(fmt)
                    if offset + size > len(plain):
                        break
                    (raw,) = struct.unpack_from(fmt, plain, offset)
                    value = raw * float(item.get("scale", 1.0))
                    text = f"{value:g} {item.get('unit', '')}".strip()
                    children.append(_field(str(item.get("name")), text, pos + 1 + offset, pos + 1 + offset + size))
                    parts.append(f"{item.get('name')}={text}")
                    offset += size
                info = " " + " ".join(parts)
            elif port == 0:
                children.append(_field("MAC commands", "", children=_mac_commands(plain, uplink, pos + 1)))
        fields.append(_field("FRMPayload", f"{len(frm)} bytes" + (" (decrypted)" if plain is not None else " (encrypted)"),
                             pos + 1, body_end, children))
    fields.append(_field("MIC", mic_text, body_end, len(data)))
    summary = f"{name} {dev_addr:08X} FCnt {fcnt}" + (f" port {port}" if port is not None else "") + \
        (" ACK" if fctrl & 0x20 else "") + info + ("" if ok else " [MIC error]")
    return {"type": name, "summary": summary, "fields": fields, "ok": ok, "source": f"{dev_addr:08X}" if uplink else None,
            "target": None if uplink else f"{dev_addr:08X}"}
