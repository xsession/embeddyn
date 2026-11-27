"""RFID: 125 kHz EM4100 read-only cards and UHF (860-960 MHz) EPC Class-1 Gen2 inventory.

Both are bit-oriented: payloads carry the bits as hex plus a bit count, MSB first:
``{"proto": "rfid", "air": "em4100"|"gen2", "dir": "tag"|"reader", "data": "<hex>",
"bits": 64, "src": "tag1"}``.

* EM4100: 64 bits = 9 header ones, 10 rows of 4 data bits + even row parity, 4 column
  parity bits, stop bit 0. Data = 8-bit version/customer id + 32-bit card id.
* Gen2: reader commands Query (22 bits, CRC-5), QueryRep, QueryAdjust, ACK (2 + RN16),
  Req_RN, Select; tag replies RN16 (16 bits) and the PC + EPC + CRC-16 reply to an ACK.
  `decode_gen2` names them and checks CRC-5/CRC-16.
"""
from __future__ import annotations

from typing import Any


def bits_to_bytes(bits: str) -> tuple[str, int]:
    count = len(bits)
    padded = bits + "0" * (-count % 8)
    return (int(padded, 2).to_bytes(len(padded) // 8, "big").hex() if padded else ""), count


def bytes_to_bits(data_hex: str, count: int) -> str:
    raw = bytes.fromhex(data_hex)
    return "".join(f"{b:08b}" for b in raw)[:count]


# -- EM4100 ---------------------------------------------------------------------------------------

def em4100_bits(version: int, card: int) -> str:
    data = f"{version & 0xFF:08b}{card & 0xFFFFFFFF:032b}"
    bits = "1" * 9
    for row in range(10):
        nibble = data[row * 4:row * 4 + 4]
        bits += nibble + str(nibble.count("1") % 2)
    for col in range(4):
        bits += str(sum(int(data[row * 4 + col]) for row in range(10)) % 2)
    return bits + "0"


def decode_em4100(bits: str) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    if len(bits) != 64 or not bits.startswith("1" * 9):
        start = bits.find("1" * 9 + "0")                  # resynchronise on a stream
        if start < 0 or len(bits) - start < 64:
            return {"type": "EM4100 ?", "summary": "EM4100: no header found", "fields": [], "ok": False}
        bits = bits[start:start + 64]
    data, row_ok = "", True
    for row in range(10):
        chunk = bits[9 + row * 5:9 + row * 5 + 5]
        data += chunk[:4]
        row_ok &= chunk[:4].count("1") % 2 == int(chunk[4])
    columns = bits[59:63]
    col_ok = all(sum(int(data[row * 4 + col]) for row in range(10)) % 2 == int(columns[col]) for col in range(4))
    ok = row_ok and col_ok and bits[63] == "0"
    version, card = int(data[:8], 2), int(data[8:], 2)
    fields = [{"label": "header", "value": "9 × 1", "bits": [0, 9]},
              {"label": "version / customer", "value": f"0x{version:02X} ({version})"},
              {"label": "card id", "value": f"0x{card:08X} ({card}) · printed {(card >> 16) & 0xFF:03d},{card & 0xFFFF:05d}"},
              {"label": "row parity", "value": "ok" if row_ok else "BAD"},
              {"label": "column parity", "value": f"{columns} ({'ok' if col_ok else 'BAD'})"},
              {"label": "stop bit", "value": bits[63]}]
    return {"type": "EM4100 ID", "summary": f"EM4100 card 0x{version:02X}{card:08X} ({(card >> 16) & 0xFF:03d},{card & 0xFFFF:05d})"
            + ("" if ok else " [parity error]"), "fields": fields, "ok": ok, "id": f"{version:02X}{card:08X}"}


# -- EPC Gen2 ---------------------------------------------------------------------------------------

def crc5(bits: str) -> str:
    reg = 0b01001
    for bit in bits:
        top = ((reg >> 4) & 1) ^ int(bit)
        reg = (reg << 1) & 0x1F
        if top:
            reg ^= 0b01001
    return f"{reg:05b}"


def crc16(bits: str) -> str:
    reg = 0xFFFF
    for bit in bits:
        top = ((reg >> 15) & 1) ^ int(bit)
        reg = (reg << 1) & 0xFFFF
        if top:
            reg ^= 0x1021
    return f"{reg ^ 0xFFFF:016b}"


def gen2_query(q: int, session: int = 0, target: int = 0, dr: int = 0, m: int = 0, trext: int = 0, sel: int = 0) -> str:
    body = "1000" + f"{dr}{m:02b}{trext}{sel:02b}{session:02b}{target}{q:04b}"
    return body + crc5(body)


def gen2_query_rep(session: int = 0) -> str:
    return "00" + f"{session:02b}"


def gen2_query_adjust(session: int = 0, updn: str = "000") -> str:
    return "1001" + f"{session:02b}" + updn


def gen2_ack(rn16: int) -> str:
    return "01" + f"{rn16 & 0xFFFF:016b}"


def gen2_epc_reply(epc: bytes) -> str:
    words = len(epc) // 2
    pc = f"{words:05b}" + "0" * 11
    body = pc + "".join(f"{b:08b}" for b in epc)
    return body + crc16(body)


def decode_gen2(bits: str, direction: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx = context if context is not None else {}
    n = len(bits)
    if direction == "reader":
        if bits.startswith("1000") and n == 22:
            ok = crc5(bits[:17]) == bits[17:]
            dr, m, trext, sel, session, target, q = bits[4], int(bits[5:7], 2), bits[7], int(bits[8:10], 2), int(bits[10:12], 2), bits[12], int(bits[13:17], 2)
            ctx["last"], ctx["q"] = "Query", q
            return {"type": "Query", "ok": ok, "summary": f"Query Q={q} ({2 ** q} slots) S{session} {'B' if target == '1' else 'A'}"
                    + ("" if ok else " [CRC-5 error]"),
                    "fields": [{"label": "DR", "value": "64/3" if dr == "1" else "8"}, {"label": "M (Miller)", "value": [1, 2, 4, 8][m]},
                               {"label": "TRext", "value": trext}, {"label": "Sel", "value": ["All", "All", "~SL", "SL"][sel]},
                               {"label": "session", "value": f"S{session}"}, {"label": "target", "value": "B" if target == "1" else "A"},
                               {"label": "Q", "value": q}, {"label": "CRC-5", "value": f"{bits[17:]} ({'ok' if ok else 'BAD'})"}]}
        if bits.startswith("00") and n == 4:
            ctx["last"] = "QueryRep"
            return {"type": "QueryRep", "ok": True, "summary": f"QueryRep S{int(bits[2:4], 2)} (next slot)", "fields": []}
        if bits.startswith("1001") and n == 9:
            ctx["last"] = "QueryAdjust"
            change = {"110": "Q+1", "000": "Q unchanged", "011": "Q-1"}.get(bits[6:9], bits[6:9])
            return {"type": "QueryAdjust", "ok": True, "summary": f"QueryAdjust {change}", "fields": []}
        if bits.startswith("01") and n == 18:
            rn = int(bits[2:], 2)
            ctx["last"], ctx["acked"] = "ACK", rn
            return {"type": "ACK", "ok": True, "summary": f"ACK RN16 0x{rn:04X}", "fields": [{"label": "RN16", "value": f"0x{rn:04X}"}]}
        if bits.startswith("1010") and n >= 45:
            ctx["last"] = "Select"
            return {"type": "Select", "ok": crc16(bits[:-16]) == bits[-16:], "summary": "Select", "fields": []}
        if bits.startswith("11000001") and n == 40:
            ctx["last"] = "Req_RN"
            return {"type": "Req_RN", "ok": crc16(bits[:-16]) == bits[-16:], "summary": f"Req_RN 0x{int(bits[8:24], 2):04X}", "fields": []}
        return {"type": "command", "ok": True, "summary": f"reader command ({n} bits)", "fields": []}
    if n == 16:
        rn = int(bits, 2)
        ctx["rn16"] = rn
        return {"type": "RN16", "ok": True, "summary": f"RN16 0x{rn:04X}", "fields": [{"label": "RN16", "value": f"0x{rn:04X}"}]}
    if n >= 32 and ctx.get("last") == "ACK" or n >= 48:
        ok = crc16(bits[:-16]) == bits[-16:]
        words = int(bits[:5], 2)
        epc_bits = bits[16:16 + words * 16]
        epc = bytes(int(epc_bits[i:i + 8], 2) for i in range(0, len(epc_bits), 8))
        return {"type": "EPC", "ok": ok, "epc": epc.hex().upper(),
                "summary": f"EPC {epc.hex().upper()}" + ("" if ok else " [CRC-16 error]"),
                "fields": [{"label": "PC", "value": f"0x{int(bits[:16], 2):04X} ({words} words)"},
                           {"label": "EPC", "value": epc.hex(" ").upper()},
                           {"label": "CRC-16", "value": f"0x{int(bits[-16:], 2):04X} ({'ok' if ok else 'BAD'})"}]}
    if n == 0:
        return {"type": "collision", "ok": True, "summary": "collision: several tags answered in this slot", "fields": []}
    return {"type": "reply", "ok": True, "summary": f"tag reply ({n} bits)", "fields": []}
