"""python -m embeddyn.mechatronics.importer.pcb_photo TOP.jpg [--bottom BOTTOM.jpg] [--closeup C.jpg ...] --out DIR

Reverse engineer a board from photos: DIR/board.net (KiCad netlist), DIR/board.json (parts, packages, markings,
pins, nets), DIR/overlay-top.jpg / overlay-bottom.jpg (nets in colour over the board), DIR/schematic.svg (a draft
net-label schematic). --board-mm 107x126 gives the board size (else it is estimated from parts of known size).
--marking REF=TEXT sets a marking read by eye (repeatable).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import read_board, set_marking, write_all


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m embeddyn.mechatronics.importer.pcb_photo", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("top", type=Path, help="photo of the top (component) side, whole board in view")
    ap.add_argument("--bottom", type=Path, help="photo of the bottom side")
    ap.add_argument("--closeup", type=Path, action="append", default=[], help="close-up photo (markings)")
    ap.add_argument("--board-mm", help="board width x height in mm, e.g. 107x126")
    ap.add_argument("--marking", action="append", default=[], help="REF=TEXT, a marking read by eye")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    board_mm = tuple(float(v) for v in a.board_mm.lower().split("x")) if a.board_mm else None
    result = read_board(a.top, a.bottom, a.closeup, board_mm)
    for item in a.marking:
        ref, _, text = item.partition("=")
        set_marking(result, ref.strip(), text.replace("\\n", "\n"))
    files = write_all(result, a.out, a.top)
    marked = sum(1 for p in result.parts if p.marking)
    print(f"{len(result.parts)} parts ({marked} with a marking), {len(result.nets)} nets, "
          f"{result.px_per_mm:.1f} px/mm")
    for note in result.notes + result.connectivity.notes:
        print("note:", note)
    for entry in result.closeups:
        print("close-up:", Path(entry["photo"]).name, "placed" if entry["placed"] else "not placed",
              f"({entry['markings']} markings)")
    for name, path in files.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
