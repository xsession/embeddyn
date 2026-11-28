"""python -m embeddyn.mechatronics.importer.schematic_scan SCHEMATIC.pdf|.png ... --out DIR

Reads printed schematics (PDF, vector or scanned; PNG, JPEG, TIFF ...) and writes DIR/schematic.net (a KiCad
netlist the project importer reads), DIR/scan.json (parts, nets, what was guessed) and DIR/overlay-pN.png (the
nets, pins and ports drawn over each page, to check by eye). Several files are the pages of one design.
``--reference NETLIST`` scores the result against a known netlist (e.g. the EDA tool's export).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import scan_to_files


def _pages(text: str | None) -> list[int] | None:
    if not text:
        return None
    pages: list[int] = []
    for part in text.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            pages.extend(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return pages


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m embeddyn.mechatronics.importer.schematic_scan",
                                 description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path, help="PDF or image files (the pages of one design)")
    ap.add_argument("--out", type=Path, required=True, help="output folder")
    ap.add_argument("--pages", help="PDF pages to read, e.g. 2-5 or 1,3 (default: all)")
    ap.add_argument("--dpi", type=int, help="image resolution (default: from the file, else 200); scanned PDF "
                                            "pages are rendered at this resolution (default 300)")
    ap.add_argument("--no-overlay", action="store_true", help="do not draw the check pictures")
    ap.add_argument("--reference", type=Path, help="a KiCad netlist of the same design to score the scan against")
    a = ap.parse_args(argv)
    for path in a.inputs:
        if not path.exists():
            ap.error(f"{path}: no such file")
    result, files = scan_to_files(a.inputs if len(a.inputs) > 1 else a.inputs[0], a.out, _pages(a.pages), a.dpi,
                                  overlay=not a.no_overlay)
    netlist, report = result.netlist, result.report
    print(f"{len(netlist.components)} parts, {len(netlist.nets)} nets from {len(report.pages)} page(s)")
    if report.guessed_pins:
        print(f"{len(report.guessed_pins)} pin numbers guessed (not printed on the drawing)")
    for note in report.notes:
        print("note:", note)
    for name, path in files.items():
        print(f"{name}: {path}")
    if a.reference:
        from ..kicad_netlist import read_netlist
        from .compare import compare
        score = compare(netlist, read_netlist(a.reference), set(report.guessed_pins))
        print("against", a.reference.name + ":", score.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
