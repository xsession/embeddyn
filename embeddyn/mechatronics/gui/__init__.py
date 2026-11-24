"""Embeddyn Lab: interactive schematic, instruments and 3-D view for mechatronic co-simulation.

`embeddyn mechatronics gui [project.yaml]` serves a local single-page app (no
CDN, no extra Python dependencies). See docs/reference/lab.md.
"""
from .document import graph_from_raw, raw_from_graph, load_graph, project_from_graph
from .parts import part_library, instantiate_part

__all__ = ["graph_from_raw", "raw_from_graph", "load_graph", "project_from_graph", "part_library", "instantiate_part"]
