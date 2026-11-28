"""Reverse engineer a board from photos: parts, packages, markings and nets, as a KiCad netlist and a draft
schematic.

``read_board(top, bottom, closeups, board_mm)`` straightens the board photos by their outline, finds parts (body +
pads), names their package from the measured size (``embeddyn.mechatronics.smd``), reads markings (OCR; close-ups
are placed onto the board by image correlation), traces copper into nets on each side and joins the sides through
vias. ``write_all`` writes ``board.net`` (KiCad netlist), ``board.json``, overlays and ``schematic.svg``.
``set_marking`` takes a marking read by a person and updates the part, its pins and the nets.

Photos from a phone work; the board must fill most of the picture, lit evenly, with its solder mask in one colour.
What cannot be seen is not found: inner layers, traces under parts, tented vias without a visible hole. See
docs/guides/pcb-photo.md.
"""
from .output import netlist, report, schematic_svg, write_all
from .reader import BoardResult, read_board, set_marking

__all__ = ["BoardResult", "read_board", "set_marking", "write_all", "netlist", "report", "schematic_svg"]
