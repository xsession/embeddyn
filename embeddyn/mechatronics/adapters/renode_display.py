"""Frames a firmware shows on an LCD, read out of the emulated memory of a Renode machine.

A display binding (``options.displays`` of a Renode instance) names the display controller the firmware
drives. Supported: ``ltdc`` (the STM32 LCD-TFT controller, e.g. the Arduino GIGA R1 with its Display
Shield over DSI). The grabber reads the controller's registers over the External Control bus API to
find the layer it shows (the top enabled one), its frame buffer address, pixel format and pitch, then
reads the frame buffer itself, exactly what the firmware put in memory::

    displays:
      - {name: lcd, signal: mcu.display, controller: ltdc, base: 0x50001000, period: 0.1}

Every ``period`` seconds of virtual time a changed frame becomes an event on ``signal``
(payload ``{"frame": n, "width": w, "height": h}``); the pixels are kept (latest frame only) and
served as PNG by :meth:`DisplayGrabber.png`.
"""
from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable

# LTDC registers (RM0399 / RM0433): global, then layer 1 at 0x84 and layer 2 at 0x104
_BPCR, _AWCR, _GCR = 0x0C, 0x10, 0x18
_LAYERS = (0x84, 0x104)
_CR, _PFCR, _CFBAR, _CFBLR, _CFBLNR = 0x00, 0x10, 0x28, 0x2C, 0x30
# pixel format code -> bytes per pixel (ARGB8888, RGB888, RGB565, ARGB1555, ARGB4444, L8, AL44, AL88)
_BPP = {0: 4, 1: 3, 2: 2, 3: 2, 4: 2, 5: 1, 6: 1, 7: 2}

ReadWord = Callable[[int], int]
ReadBytes = Callable[[int, int], bytes]


@dataclass
class Frame:
    seq: int
    time_seconds: float
    width: int
    height: int
    rgb: bytes                    # width * height * 3, row major


@dataclass
class DisplayGrabber:
    name: str
    signal: str
    base: int = 0x50001000
    period: float = 0.1
    controller: str = "ltdc"
    frame: Frame | None = None
    _next: float = 0.0
    _digest: bytes = b""
    _seq: int = 0
    _png: tuple[int, bytes] | None = field(default=None, repr=False)

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> "DisplayGrabber":
        name = str(spec.get("name") or "display")
        controller = str(spec.get("controller", "ltdc")).lower()
        if controller != "ltdc":
            raise ValueError(f"display {name}: unsupported controller {controller!r} (supported: ltdc)")
        base = spec.get("base", 0x50001000)
        period = float(spec.get("period", 0.1))
        if period <= 0:
            raise ValueError(f"display {name}: period must be > 0")
        return cls(name=name, signal=str(spec.get("signal") or f"display.{name}"), controller=controller,
                   base=int(base, 0) if isinstance(base, str) else int(base), period=period)

    def due(self, now: float) -> bool:
        return now + 1e-12 >= self._next

    def grab(self, now: float, read_word: ReadWord, read_bytes: ReadBytes) -> dict[str, Any] | None:
        """Read the shown frame; returns an event when the picture changed since the last grab."""
        self._next = now + self.period
        if not read_word(self.base + _GCR) & 1:
            return None                                       # controller off
        bpcr, awcr = read_word(self.base + _BPCR), read_word(self.base + _AWCR)
        width = ((awcr >> 16) & 0xFFF) - ((bpcr >> 16) & 0xFFF)
        height = (awcr & 0x7FF) - (bpcr & 0x7FF)
        if width <= 0 or height <= 0 or width * height > 4096 * 4096:
            return None
        layer = None
        for offset in reversed(_LAYERS):                       # layer 2 is drawn over layer 1
            if read_word(self.base + offset + _CR) & 1:
                layer = offset
                break
        if layer is None:
            rgb = bytes(width * height * 3)
        else:
            pf = read_word(self.base + layer + _PFCR) & 7
            bpp = _BPP[pf]
            address = read_word(self.base + layer + _CFBAR)
            pitch = (read_word(self.base + layer + _CFBLR) >> 16) & 0x1FFF or width * bpp
            raw = read_bytes(address, pitch * (height - 1) + width * bpp)
            rgb = _to_rgb(raw, width, height, pitch, pf)
        digest = hashlib.blake2b(rgb, digest_size=16).digest()
        if digest == self._digest:
            return None
        self._digest = digest
        self._seq += 1
        self.frame = Frame(self._seq, now, width, height, rgb)
        return {"time_seconds": now, "topic": self.signal,
                "payload": {"frame": self._seq, "width": width, "height": height, "display": self.name}}

    def png(self) -> bytes | None:
        frame = self.frame
        if frame is None:
            return None
        if self._png is None or self._png[0] != frame.seq:
            self._png = (frame.seq, encode_png(frame.width, frame.height, frame.rgb))
        return self._png[1]


def _to_rgb(raw: bytes, width: int, height: int, pitch: int, pf: int) -> bytes:
    import numpy as np
    bpp = _BPP[pf]
    padded = raw + bytes(pitch * height - len(raw))
    rows = np.frombuffer(padded, dtype=np.uint8).reshape(height, pitch)[:, :width * bpp]
    if pf == 2:                                                # RGB565
        v = rows.reshape(height, width, 2).astype(np.uint16)
        v = v[..., 0] | (v[..., 1] << 8)
        r, g, b = (v >> 11) & 0x1F, (v >> 5) & 0x3F, v & 0x1F
        out = np.stack([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)], axis=-1)
    elif pf == 0:                                              # ARGB8888, little endian: B G R A
        out = rows.reshape(height, width, 4)[..., [2, 1, 0]]
    elif pf == 1:                                              # RGB888: B G R
        out = rows.reshape(height, width, 3)[..., [2, 1, 0]]
    else:                                                      # other formats: grey from the first byte
        out = np.repeat(rows.reshape(height, width, bpp)[..., :1], 3, axis=-1)
    return np.ascontiguousarray(out, dtype=np.uint8).tobytes()


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 3)) + chunk(b"IEND", b""))
