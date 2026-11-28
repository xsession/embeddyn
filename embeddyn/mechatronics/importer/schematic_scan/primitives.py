"""What a schematic page is made of once read: straight strokes, closed shapes, junction dots and positioned text.

Coordinates are page points (1/72 inch), x to the right, y down, for PDF and image input alike (an image is
scaled by its resolution), so the tolerances mean the same thing for both.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Segment:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float = 0.0
    # "wire": an open horizontal or vertical stroke (a wire or a pin line); "body": symbol graphics (closed shapes,
    # curves, diagonals, filled shapes)
    kind: str = "wire"
    path: int = -1          # the drawing (path) it came from, to keep a shape's pieces together

    @property
    def length(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)

    @property
    def horizontal(self) -> bool:
        return abs(self.y2 - self.y1) <= abs(self.x2 - self.x1)

    @property
    def manhattan(self) -> bool:
        slack = 0.02 * self.length + 0.05
        return abs(self.x2 - self.x1) <= slack or abs(self.y2 - self.y1) <= slack

    @property
    def ends(self) -> tuple[tuple[float, float], tuple[float, float]]:
        return (self.x1, self.y1), (self.x2, self.y2)

    def bbox(self) -> tuple[float, float, float, float]:
        return min(self.x1, self.x2), min(self.y1, self.y2), max(self.x1, self.x2), max(self.y1, self.y2)

    def distance(self, x: float, y: float) -> tuple[float, float]:
        """Distance from (x, y) to the segment, and where its foot lies along it (0 = first end, 1 = second)."""
        dx, dy = self.x2 - self.x1, self.y2 - self.y1
        n = dx * dx + dy * dy
        t = 0.0 if n == 0 else max(0.0, min(1.0, ((x - self.x1) * dx + (y - self.y1) * dy) / n))
        return math.hypot(self.x1 + t * dx - x, self.y1 + t * dy - y), t


@dataclass
class Text:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    vertical: bool = False
    confidence: float = 1.0     # OCR confidence 0..1 (1 for text read from a PDF)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        """The character height: the box's extent across the reading direction."""
        return (self.x1 - self.x0) if self.vertical else (self.y1 - self.y0)

    @property
    def box(self) -> tuple[float, float, float, float]:
        return self.x0, self.y0, self.x1, self.y1

    def box_distance(self, box: tuple[float, float, float, float]) -> float:
        x0, y0, x1, y1 = box
        dx = max(x0 - self.x1, self.x0 - x1, 0.0)
        dy = max(y0 - self.y1, self.y0 - y1, 0.0)
        return math.hypot(dx, dy)

    def contains(self, x: float, y: float, margin: float = 0.0) -> bool:
        return self.x0 - margin <= x <= self.x1 + margin and self.y0 - margin <= y <= self.y1 + margin


@dataclass
class Dot:
    """A filled junction dot: wires crossing under it are connected."""
    x: float
    y: float
    r: float


@dataclass
class Page:
    index: int
    width: float
    height: float
    segments: list[Segment] = field(default_factory=list)
    polygons: list[list[tuple[float, float]]] = field(default_factory=list)   # closed shapes (symbol bodies)
    dots: list[Dot] = field(default_factory=list)
    texts: list[Text] = field(default_factory=list)
    line_width: float = 0.0     # the typical stroke width
    raster: bool = False        # read from pixels (looser tolerances)
    image: object = None        # the page as a PIL image, for the overlay (optional)
    scale: float = 1.0          # image pixels per point
    source: object = None       # the file it came from
    source_page: int = 1        # its page number in that file
    text_image: object = None   # (pictures) the page's text-only image, for reading parts again


def polygon_area(points: list[tuple[float, float]]) -> float:
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]))) / 2


def point_in_polygon(x: float, y: float, points: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(points) - 1
    for i in range(len(points)):
        xi, yi = points[i]
        xj, yj = points[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside
