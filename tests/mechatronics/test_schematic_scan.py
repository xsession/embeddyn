"""Schematic scan: netlists from printed schematics (vector PDF, and the same page as a picture).

The tests draw a small schematic as a PDF by hand, so the right answer is known:

    VIN ---[R1 10k]---+--- VOUT              SENSE ---+
                      |                               |   (crosses the VOUT wire without a dot)
                     === C1 100n                    [R2 1k]
                      |                               |
                     GND                             GND

Expected nets: VIN {R1.1}, VOUT {R1.2, C1.1}, GND {C1.2, R2.2} (two ground symbols, one net), SENSE {R2.1}.
They need MuPDF's ``mutool``; the picture test also needs ``tesseract``.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from embeddyn.mechatronics.importer.kicad_netlist import Component, Net, Netlist, Node, read_netlist
from embeddyn.mechatronics.importer.schematic_scan import scan, scan_to_files
from embeddyn.mechatronics.importer.schematic_scan.assemble import write_kicad_netlist
from embeddyn.mechatronics.importer.schematic_scan.compare import compare

needs_mutool = pytest.mark.skipif(not shutil.which("mutool"), reason="mutool (MuPDF tools) not installed")
needs_ocr = pytest.mark.skipif(not (shutil.which("mutool") and shutil.which("tesseract")),
                               reason="mutool and tesseract needed")

W, H = 400, 300


def _pdf(path: Path, ops: list[str]) -> Path:
    """A one-page PDF (W x H points) with the given content stream operators and Helvetica as /F1."""
    content = "\n".join(ops).encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W} {H}] /Contents 4 0 R "
        f"/Resources << /Font << /F1 5 0 R >> >> >>".encode(),
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for o in offsets:
        out += b"%010d 00000 n \n" % o
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def _line(x1, y1, x2, y2):
    return f"{x1} {H - y1} m {x2} {H - y2} l S"


def _rect(x0, y0, x1, y1):
    return f"{x0} {H - y1} {x1 - x0} {y1 - y0} re S"


def _text(x, y, size, s):
    """Text with its baseline at y (top-down coordinates)."""
    return f"BT /F1 {size} Tf {x} {H - y} Td ({s}) Tj ET"


def _dot(x, y, r=1.6):
    c = 0.552 * r
    y = H - y
    return (f"{x + r} {y} m {x + r} {y + c} {x + c} {y + r} {x} {y + r} c {x - c} {y + r} {x - r} {y + c} {x - r} {y} c "
            f"{x - r} {y - c} {x - c} {y - r} {x} {y - r} c {x + c} {y - r} {x + r} {y - c} {x + r} {y} c f")


def _ground(x, y):
    # stem end at (x, y); a triangle below it
    return f"{x - 8} {H - y} m {x + 8} {H - y} l {x} {H - y - 7} l h S"


def schematic_ops(size: float = 8) -> list[str]:
    return [
        "0.6 w 1 J",
        # VIN wire and label, R1 (horizontal) with its pin lines
        _line(40, 100, 95, 100), _text(45, 97, size, "VIN"),
        _line(95, 100, 100, 100), _rect(100, 95, 130, 105), _line(130, 100, 135, 100),
        _text(106, 91, size, "R1"), _text(106, 81, size, "10k"),
        # node: R1.2 -> dot -> C1, and on to the VOUT label
        _line(135, 100, 260, 100), _text(205, 97, size, "VOUT"), _dot(180, 100),
        _line(180, 100, 180, 140),
        _line(172, 140, 188, 140), _line(172, 146, 188, 146),      # C1 plates
        _text(192, 141, size, "C1"), _text(192, 151, size, "100n"),
        _line(180, 146, 180, 175), _ground(180, 175), _text(171, 193, size, "GND"),
        # SENSE: a wire crossing the VOUT wire without a dot, down to R2 and a second ground
        _line(230, 60, 290, 60), _text(250, 57, size, "SENSE"),
        _line(230, 60, 230, 140), _rect(226, 140, 234, 160), _line(230, 160, 230, 175),
        _text(238, 148, size, "R2"), _text(238, 158, size, "1k"),
        _ground(230, 175), _text(221, 193, size, "GND"),
    ]


def _nets(netlist: Netlist) -> dict[frozenset, str]:
    return {frozenset(f"{n.ref}.{n.pin}" for n in net.nodes): net.name for net in netlist.nets}


EXPECTED = {
    frozenset({"R1.1"}): "VIN",
    frozenset({"R1.2", "C1.1"}): "VOUT",
    frozenset({"C1.2", "R2.2"}): "GND",
    frozenset({"R2.1"}): "SENSE",
}


@needs_mutool
def test_vector_pdf(tmp_path):
    pdf = _pdf(tmp_path / "sch.pdf", schematic_ops())
    result = scan(pdf)
    assert {r: c.value for r, c in result.netlist.components.items()} == {"R1": "10k", "R2": "1k", "C1": "100n"}
    assert _nets(result.netlist) == EXPECTED


@needs_mutool
def test_files_round_trip(tmp_path):
    pdf = _pdf(tmp_path / "sch.pdf", schematic_ops())
    result, files = scan_to_files(pdf, tmp_path / "out")
    assert files["netlist"].exists() and files["report"].exists() and files["overlay1"].exists()
    again = read_netlist(files["netlist"])
    assert _nets(again) == EXPECTED
    assert set(again.components) == {"R1", "R2", "C1"}


@needs_ocr
def test_picture_of_the_page(tmp_path):
    # the same drawing with larger text, as a 300 dpi PNG: lines from pixels, text by OCR
    pdf = _pdf(tmp_path / "sch.pdf", schematic_ops(size=10))
    png = tmp_path / "sch.png"
    subprocess.run(["mutool", "draw", "-q", "-r", "300", "-o", str(png), str(pdf), "1"], check=True,
                   capture_output=True)
    result = scan(png, dpi=300)
    assert {"R1", "R2", "C1"} <= set(result.netlist.components)
    nets = _nets(result.netlist)
    # the connections are what matter (OCR may miss a label's name)
    assert set(nets) == set(EXPECTED)
    assert nets[frozenset({"C1.2", "R2.2"})] == "GND"


def test_compare_scores():
    def netlist(groups):
        n = Netlist(source=Path("x"))
        for name, nodes in groups.items():
            for ref, _ in nodes:
                n.components.setdefault(ref, Component(ref=ref))
            n.nets.append(Net(name=name, nodes=[Node(ref=r, pin=p) for r, p in nodes]))
        return n
    reference = netlist({"A": [("R1", "1"), ("U1", "3")], "B": [("R1", "2"), ("C1", "1"), ("U1", "4")]})
    same = compare(reference, reference)
    assert same.precision == 1.0 and same.recall == 1.0 and same.nets_exact == 2
    # a two-pin part read the other way round is matched when its numbers were guessed
    flipped = netlist({"A": [("R1", "2"), ("U1", "3")], "B": [("R1", "1"), ("C1", "1"), ("U1", "4")]})
    assert compare(flipped, reference, {"R1.1", "R1.2"}).recall == 1.0
    assert compare(flipped, reference).recall < 1.0


def test_netlist_writer_quotes(tmp_path):
    n = Netlist(source=Path("a b.pdf"))
    n.components["R1"] = Component(ref="R1", value='1"k')
    n.nets.append(Net(name="SPI2_!CS", nodes=[Node("R1", "1", "A")]))
    back = read_netlist(write_kicad_netlist(n, tmp_path / "x.net"))
    assert back.components["R1"].value == '1"k'
    assert back.nets[0].name == "SPI2_!CS" and back.nets[0].nodes[0].pin_name == "A"


@needs_mutool
def test_lab_scan_view(tmp_path):
    # the Lab's Scan tab: upload a PDF, get parts, nets and positions, fetch the overlay, import as a board
    import base64
    import json
    import threading
    import urllib.error
    import urllib.request

    from embeddyn.mechatronics.gui.server import serve

    server = serve(None, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path, body):
        request = urllib.request.Request(f"{base}/api/{path}", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    try:
        pdf = _pdf(tmp_path / "sch.pdf", schematic_ops())
        result = post("scan/run", {"root": str(tmp_path), "uploads": [
            {"name": "my sch.pdf", "data": base64.b64encode(pdf.read_bytes()).decode()}]})
        assert result["out"] == str(tmp_path / ".embeddyn" / "scans" / "my_sch")
        assert {p["ref"]: p["value"] for p in result["parts"]} == {"C1": "100n", "R1": "10k", "R2": "1k"}
        assert {frozenset(f"{n['ref']}.{n['pin']}" for n in net["nodes"]) for net in result["nets"]} == set(EXPECTED)
        assert result["positions"]["R1.1"][0] == 1 and result["boxes"]["C1"][0] == 1
        overlay = result["pages"][0]["overlay"]
        with urllib.request.urlopen(f"{base}/api/scan/image?path={overlay}", timeout=10) as response:
            assert response.read(4) == b"\x89PNG"
        with pytest.raises(urllib.error.HTTPError):          # only the scan's own pictures are served
            urllib.request.urlopen(f"{base}/api/scan/image?path={pdf}", timeout=10)
        # by path, scored against itself
        again = post("scan/run", {"root": str(tmp_path), "paths": ["sch.pdf"], "out": "scan2",
                                  "reference": result["netlist"]})
        assert again["score"]["precision"] == 1.0 and again["score"]["recall"] == 1.0
        imported = post("scan/import", {"out": again["out"], "title": "rc"})
        assert imported["mcu"] is None and (tmp_path / "scan2" / "board" / "board.repl").exists()
    finally:
        server.shutdown()
        server.server_close()
