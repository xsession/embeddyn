"""The Renode display grabber: an LTDC frame read through bus callbacks, converted to RGB and PNG."""
import struct
import zlib

from embeddyn.mechatronics.adapters.renode_display import DisplayGrabber, encode_png

BASE = 0x50001000
FB = 0x60000000


def _ltdc(width, height, layer_on=(True, False), pf=2):
    regs = {BASE + 0x18: 1,                                   # GCR: LTDCEN
            BASE + 0x0C: (0x35 << 16) | 0x35,                 # BPCR
            BASE + 0x10: ((0x35 + width) << 16) | (0x35 + height)}
    for i, offset in enumerate((0x84, 0x104)):
        regs[BASE + offset] = 1 if layer_on[i] else 0
        regs[BASE + offset + 0x10] = pf
        regs[BASE + offset + 0x28] = FB
        regs[BASE + offset + 0x2C] = (width * 2) << 16 | (width * 2 + 7)
    return regs


def test_grabs_the_enabled_rgb565_layer_and_skips_unchanged_frames():
    width, height = 4, 3
    regs = _ltdc(width, height)
    pixels = [0xF800, 0x07E0, 0x001F, 0xFFFF] * height          # red, green, blue, white per row
    memory = b"".join(struct.pack("<H", p) for p in pixels)
    reads = []

    def read_bytes(address, count):
        reads.append((address, count))
        return memory[address - FB:address - FB + count]

    grabber = DisplayGrabber.from_spec({"name": "lcd", "signal": "mcu.display", "period": 0.1})
    event = grabber.grab(0.2, regs.__getitem__, read_bytes)
    assert event["topic"] == "mcu.display"
    assert event["payload"] == {"frame": 1, "width": 4, "height": 3, "display": "lcd"}
    assert reads == [(FB, width * 2 * height)]
    assert grabber.frame.rgb[:12] == bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])
    assert not grabber.due(0.25) and grabber.due(0.3)
    assert grabber.grab(0.3, regs.__getitem__, read_bytes) is None     # same picture: no event
    png = grabber.png()
    assert png.startswith(b"\x89PNG") and struct.unpack(">II", png[16:24]) == (4, 3)


def test_controller_off_or_no_layer():
    regs = _ltdc(2, 2, layer_on=(False, False))
    grabber = DisplayGrabber.from_spec({})
    event = grabber.grab(0.0, regs.__getitem__, lambda a, c: b"")
    assert event["payload"]["frame"] == 1 and grabber.frame.rgb == bytes(12)   # black screen
    regs[BASE + 0x18] = 0
    assert grabber.grab(1.0, regs.__getitem__, lambda a, c: b"") is None


def test_png_round_trip():
    png = encode_png(2, 1, bytes([1, 2, 3, 4, 5, 6]))
    idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
    assert zlib.decompress(idat) == b"\x00" + bytes([1, 2, 3, 4, 5, 6])
