"""Board photos to a netlist, and the SMD decoder.

The photo test draws a small board as a phone would see it (solder mask, copper under the mask, silver pads,
black and beige bodies, the board in slight perspective on a grey background), so the right answer is known:

    R1 (0805, "103") pin 2 ---- U1 (SOIC-8) pin 1
    R1 pin 1 ---- C1 (0805) pin 1
    C1 pin 2 ---- U1 pin 4
"""
from __future__ import annotations

import math
import random

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from embeddyn.mechatronics.smd import classify, decode, decode_resistor, number_pins

PX = 25.0                     # px per mm of the drawn board
W_MM, H_MM = 40.0, 30.0


def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _noisy(draw_target: np.ndarray, mask: np.ndarray, colour, sigma: float, rng: np.random.Generator,
           grey: bool = False) -> None:
    n = rng.normal(0, sigma, (mask.sum(), 1 if grey else 3))
    draw_target[mask] = np.clip(np.array(colour, np.float32) + n, 0, 255)


def draw_board(path, seed: int = 3):
    """The test board as a photo; returns the expected connections as part-centre pairs (mm)."""
    rng = np.random.default_rng(seed)
    w, h = int(W_MM * PX), int(H_MM * PX)
    layers = {k: Image.new("L", (w, h), 0) for k in ("copper", "pad", "dark", "beige", "text")}
    d = {k: ImageDraw.Draw(v) for k, v in layers.items()}

    def mm(x, y):
        return x * PX, y * PX

    def rect(layer, x0, y0, x1, y1):
        d[layer].rectangle([*mm(x0, y0), *mm(x1, y1)], fill=255)

    def trace(points, width=0.35):
        pts = [mm(x, y) for x, y in points]
        d["copper"].line(pts, fill=255, width=int(width * PX), joint="curve")
        for p in pts:
            r = width * PX / 2
            d["copper"].ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=255)

    # R1: 0805 at (10, 10), horizontal; pads at x = 9.0 / 11.0
    r1 = (10.0, 10.0)
    for x in (8.95, 11.05):
        rect("pad", x - 0.55, 9.3, x + 0.55, 10.7)
    rect("dark", 9.25, 9.4, 10.75, 10.6)
    # C1: 0805 at (10, 20), vertical
    c1 = (10.0, 20.0)
    for y in (18.95, 21.05):
        rect("pad", 9.3, y - 0.55, 10.7, y + 0.55)
    rect("beige", 9.4, 19.25, 10.6, 20.75)
    # U1: SOIC-8 at (25, 15), body 4.9 (vertical) x 3.9; pins 1-4 down the left, 5-8 up the right
    u1 = (25.0, 15.0)
    rect("dark", 23.05, 12.55, 26.95, 17.45)
    left = [(21.6, 15 - 1.905 + 1.27 * k) for k in range(4)]
    right = [(28.4, 15 + 1.905 - 1.27 * k) for k in range(4)]
    for x, y in left + right:
        rect("pad", x - 0.75, y - 0.3, x + 0.75, y + 0.3)
        lead_x = (x + 0.5, 23.05) if x < 25 else (26.95, x - 0.5)
        rect("pad", lead_x[0], y - 0.2, lead_x[1], y + 0.2)          # the gull-wing lead
    # traces
    trace([(11.05, 10.0), (16.0, 10.0), (16.0, left[0][1]), (21.6, left[0][1])])       # R1.2 - U1.1
    trace([(8.95, 10.0), (6.0, 10.0), (6.0, 18.95), (10.0, 18.95)])                    # R1.1 - C1.1
    trace([(10.0, 21.05), (10.0, 24.0), (19.0, 24.0), (19.0, left[3][1]), (21.6, left[3][1])])  # C1.2 - U1.4
    trace([(28.4, right[0][1]), (33.0, right[0][1]), (33.0, 8.0)])                      # U1.5 to nowhere
    # marking on R1 ("103") and U1
    font = _font(int(0.75 * PX))
    d["text"].text(mm(9.45, 9.55), "103", fill=255, font=font)
    img = Image.new("L", (int(3.9 * PX), int(1.2 * PX)), 0)
    ImageDraw.Draw(img).text((4, 2), "LM358", fill=255, font=_font(int(0.85 * PX)))
    layers["text"].paste(img.rotate(90, expand=True), (int(24.4 * PX), int(13.0 * PX)),
                         img.rotate(90, expand=True))

    rgb = np.zeros((h, w, 3), np.float32)
    rgb[:] = (38, 128, 108)
    rgb += rng.normal(0, 4, rgb.shape)
    m = {k: np.asarray(v) > 127 for k, v in layers.items()}
    _noisy(rgb, m["copper"], (70, 192, 150), 5, rng)
    _noisy(rgb, m["pad"], (215, 215, 212), 30, rng, grey=True)  # solder: shiny, uneven brightness
    _noisy(rgb, m["dark"], (40, 40, 42), 6, rng)
    _noisy(rgb, m["beige"], (212, 182, 150), 3, rng)
    _noisy(rgb, m["text"] & m["dark"], (215, 215, 210), 6, rng)
    board = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.6))
    # the photo: grey background, the board in slight perspective
    canvas = Image.new("RGB", (w + 240, h + 240), (92, 90, 88))
    src_quad = (0, 0, 0, h, w, h, w, 0)
    big = board.transform((w + 240, h + 240), Image.QUAD,
                          (-130, -110, -120, h + 125, w + 115, h + 110, w + 125, -120), Image.BICUBIC)
    mask = Image.new("L", board.size, 255).transform((w + 240, h + 240), Image.QUAD,
                                                    (-130, -110, -120, h + 125, w + 115, h + 110, w + 125, -120))
    canvas.paste(big, (0, 0), mask)
    canvas.save(path, quality=95)
    del src_quad
    return {"R1": r1, "C1": c1, "U1": u1}


def _nearest(result, centre_mm):
    px = result.px_per_mm
    return min(result.parts, key=lambda p: math.hypot(p.cx / px - centre_mm[0], p.cy / px - centre_mm[1]))


def test_board_photo_to_netlist(tmp_path):
    from embeddyn.mechatronics.importer.pcb_photo import read_board, write_all
    photo = tmp_path / "board.jpg"
    expected = draw_board(photo)
    result = read_board(photo, board_mm=(W_MM, H_MM))
    found = {name: _nearest(result, centre) for name, centre in expected.items()}
    for name, part in found.items():
        cx, cy = part.cx / result.px_per_mm, part.cy / result.px_per_mm
        assert math.hypot(cx - expected[name][0], cy - expected[name][1]) < 1.5, (name, cx, cy)
    assert len({p.ref for p in found.values()}) == 3
    assert found["U1"].package == "SOIC-8" and len(found["U1"].pads) == 8
    assert "0805" in found["R1"].candidates and "0805" in found["C1"].candidates
    assert found["R1"].kind == "resistor" and found["C1"].kind == "capacitor"
    # connections, by part (pin numbers of the passives are guessed)
    pairs = set()
    for nodes in result.nets.values():
        refs = sorted({r for r, _ in nodes})
        pairs.update((a, b) for i, a in enumerate(refs) for b in refs[i + 1:])
    ref = {k: v.ref for k, v in found.items()}
    want = {tuple(sorted((ref["R1"], ref["U1"]))), tuple(sorted((ref["R1"], ref["C1"]))),
            tuple(sorted((ref["C1"], ref["U1"])))}
    assert want <= pairs
    # U1's pins 1 and 4 (counted from the lower left with the marking read upward... or as found): the traces
    # reach two pins on the same side of U1, three apart
    files = write_all(result, tmp_path / "out", photo)
    assert files["netlist"].exists() and files["schematic"].exists() and files["overlay_top"].exists()


def test_set_marking_by_hand(tmp_path):
    from embeddyn.mechatronics.importer.pcb_photo import read_board, set_marking
    photo = tmp_path / "board.jpg"
    expected = draw_board(photo)
    result = read_board(photo, board_mm=(W_MM, H_MM))
    u1 = _nearest(result, expected["U1"])
    # the drawn marking reads upward: the text's lower left corner is the picture's lower right, so pin 1 is there
    # and pins 1-4 run up the right side
    part = set_marking(result, u1.ref, "LM358", pin1="reads_up")
    assert part.marking == "LM358" and part.value
    pads = [result.top.pads[p] for p in part.pads]
    assert {p.pin for p in pads} == {str(n) for n in range(1, 9)}
    assert all(not p.guessed for p in pads)
    by_pin = {p.pin: p for p in pads}
    assert by_pin["1"].x > u1.cx and by_pin["1"].y > by_pin["4"].y          # pin 1 lower right, 4 upper right
    assert by_pin["5"].x < u1.cx and by_pin["5"].y < by_pin["8"].y          # 5 upper left, 8 lower left


# --- the SMD decoder --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("code,value", [("103", 10e3), ("472", 4.7e3), ("4R7", 4.7), ("R047", 0.047), ("1002", 10e3),
                                        ("01C", 10e3), ("68X", 49.9), ("000", 0.0), ("680", 68.0)])
def test_resistor_codes(code, value):
    assert decode_resistor(code).value == pytest.approx(value)


def test_decoder_ranks_by_package():
    assert decode("A7", package="SOT-23").best.part == "BAV99"
    assert decode("1Ft", package="SOT-23").best.part == "BC847B"      # Nexperia site letter stripped
    best = decode("472", package="0603").best
    assert best.kind == "value" and best.value.kind == "resistor" and best.value.text == "4.7k"
    assert decode("089").best.score < decode("680").best.score        # upside-down reading ranks lower
    assert decode("104", kind="capacitor").best.value.text == "100nF"


def test_part_numbers():
    best = decode("74HC595D\nCRE79805").best
    assert best.part == "74HC595" and "shift register" in best.detail and best.package == "SOIC"
    stm = decode("F051K6U6").best
    assert stm.part == "STM32F051K6" and stm.package == "UFQFPN-32"
    fuzzy = decode("PANCSSSD").best                                     # OCR of an etched 74HC595D
    assert fuzzy.part == "74HC595?" and fuzzy.confidence == "check"


def test_user_code_table(tmp_path, monkeypatch):
    table = tmp_path / "codes.csv"
    table.write_text("code,package,part,maker,function\nQX9,SOT-23,TEST123,Acme,test transistor\n")
    monkeypatch.setenv("EMBEDDYN_SMD_CODES", str(table))
    from embeddyn.mechatronics.smd import decode as dec
    from embeddyn.mechatronics.smd.decode import _load
    _load.cache_clear()
    try:
        assert dec("QX9", package="SOT-23").best.part == "TEST123"
    finally:
        _load.cache_clear()


def test_packages_and_pin_numbers():
    assert classify(1.6, 0.8, 2)[0].package.name == "0603"
    assert classify(2.9, 1.6, 6)[0].package.name == "SOT-23-6"
    assert classify(9.9, 3.9, 16, 2)[0].package.name == "SOIC-16"
    soic = [(x, 3) for x in (-1.905, -0.635, 0.635, 1.905)] + [(x, -3) for x in (-1.905, -0.635, 0.635, 1.905)]
    assert number_pins(soic, "dual") == [1, 2, 3, 4, 8, 7, 6, 5]
    quad = [(-2.5, y) for y in (-0.75, -0.25, 0.25, 0.75)] + [(x, 2.5) for x in (-0.75, -0.25, 0.25, 0.75)] + \
           [(2.5, y) for y in (0.75, 0.25, -0.25, -0.75)] + [(x, -2.5) for x in (0.75, 0.25, -0.25, -0.75)]
    assert number_pins(quad, "quad") == list(range(1, 17))
    random.seed(0)


def test_lab_board_photos(tmp_path):
    # the Lab's Scan tab, board-photo mode: upload a photo, read it, type a marking, decode a code on its own
    import base64
    import json
    import threading
    import urllib.request

    from embeddyn.mechatronics.gui.server import serve

    photo = tmp_path / "board.jpg"
    expected = draw_board(photo)
    server = serve(None, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path, body):
        request = urllib.request.Request(f"{base}/api/{path}", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read())
    try:
        result = post("scan/board", {"root": str(tmp_path), "top": "my board.jpg", "board_mm": "40x30",
                                     "uploads": [{"name": "my board.jpg",
                                                  "data": base64.b64encode(photo.read_bytes()).decode()}]})
        assert result["mode"] == "board" and result["out"].endswith("board-my_board")
        assert [p["name"] for p in result["pages"]] == ["top", "schematic"]
        soic = next(p for p in result["parts"] if p["package"] == "SOIC-8")
        with urllib.request.urlopen(f"{base}/api/scan/image?path={result['pages'][0]['overlay']}", timeout=10) as r:
            assert r.read(2) == b"\xff\xd8"                                  # JPEG
        with urllib.request.urlopen(f"{base}/api/scan/image?path={result['pages'][1]['overlay']}", timeout=10) as r:
            assert r.headers["Content-Type"] == "image/svg+xml"
        again = post("scan/marking", {"out": result["out"], "ref": soic["ref"], "text": "LM358", "pin1": "reads_up"})
        part = next(p for p in again["parts"] if p["ref"] == soic["ref"])
        assert part["marking"] == "LM358" and part["value"]
        assert not any(k.startswith(soic["ref"] + ".") for k in again["guessed"])
        decoded = post("smd/decode", {"text": "A7", "package": "SOT-23", "size": "2.9x1.3x3"})
        assert decoded["decoded"]["readings"][0]["part"] == "BAV99"
        assert decoded["packages"][0]["package"] == "SOT-23"
        imported = post("scan/import", {"out": result["out"], "title": "photo board"})
        assert imported["mcu"] is None                                       # no MCU on the test board
        del expected
    finally:
        server.shutdown()
        server.server_close()
