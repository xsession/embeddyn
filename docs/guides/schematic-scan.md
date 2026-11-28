# Import a printed schematic (PDF, PNG, JPEG)

When a design exists only as a drawing (a PDF someone printed from their EDA tool, a scan, a screenshot), the
schematic scanner reads the parts, pins and wires off the page and writes a KiCad netlist. That netlist is what
[Import a board project](import-a-board.md) reads, so a drawing can stand in for the KiCad project.

```bash
python -m embeddyn.mechatronics.importer.schematic_scan board.pdf --out scan/
python -m embeddyn.mechatronics.importer.schematic_scan page1.png page2.png --out scan/ --dpi 300
```

| Output | What it is |
|---|---|
| `scan/schematic.net` | KiCad netlist (`(export (version "E") ...)`): parts with values, nets with pin numbers and pin names |
| `scan/scan.json` | per page counts, every part and net, the pin numbers that were guessed, notes |
| `scan/overlay-pN.png` | the page with each net in its own colour, parts boxed with their reference, pins as dots (red: number guessed), ports and labels in orange, symbols without a reference in magenta |

Several input files are the pages of one design: nets with the same name (power ports, net labels, label flags)
join across them. `--pages 2-5` picks PDF pages. `--reference my.net` scores the result against a known netlist
(precision and recall of pin-to-pin connections, identical nets, matching values).

In the Lab (`python -m embeddyn mechatronics gui`) the **Scan** tab does the same: drop the PDF or pictures on
it, check the overlay, click parts and nets to find them on the page, then **Import & open** turns the netlist
into an emulated board project (see [the Lab reference](../reference/lab.md#scan-view)).

In Python:

```python
from pathlib import Path
from embeddyn.mechatronics.importer.schematic_scan import scan, scan_to_files

result = scan(Path("board.pdf"))
result.netlist.components["R12"].value            # "10k"
[(n.ref, n.pin) for n in result.netlist.nets[0].nodes]
result.report.guessed_pins                         # ["R12.1", "R12.2", ...]
```

## Needs

`mutool` (MuPDF tools) for PDFs, `tesseract` for pictures and scanned PDF pages; numpy, scipy and Pillow.

## How it reads a page

Everything ends up as the same primitives (straight strokes, closed shapes, junction dots, positioned words), in
page points, and one analysis turns them into a netlist.

* **Vector PDFs** (exported by an EDA tool): `mutool draw -F trace` gives every path with its transform,
  `mutool draw -F stext` every character with its position. KiCad draws text with strokes and puts an invisible
  copy of the text on top; the strokes under a word are dropped.
* **Pictures and scanned pages**: black and white by Otsu's threshold (coloured ink counts as ink); OCR with
  tesseract on the page, on a copy with the long lines removed and on a thinned copy (bold stroke fonts), upright
  and turned for vertical text, keeping the most confident reading; horizontal and vertical strokes by
  morphological opening; rectangles of four strokes meeting at their corners are symbol bodies; solid round blobs
  are junction dots. **Coloured drawings** (KiCad and Eagle exports, screenshots) are split by colour: the wire
  colour is the colour of the junction dots (or the colour with the most long straight runs), everything else is
  symbol ink. Symbols left without a reference are read again from a close crop (short words like "C1").

The analysis:

1. Wire ink is open horizontal / vertical strokes; symbol ink is closed shapes, curves, diagonals, filled shapes,
   anything inside a closed body (a fuse's centre line) and *plates*: short strokes touched only in the middle
   (capacitor plates, a diode's bar, ground bars). A big closed frame around wires and dots is an annotation box,
   not a body; an X of two short diagonals is a no-connect marker.
2. Wire strokes joined end to end, end to side (a T) or across under a junction dot are one net; a crossing without
   a dot is not a connection.
3. Touching symbol ink is one symbol; a wire ending on it is a pin.
4. Text: reference designators go to symbols (the assignment with the least total distance, so a reference that
   touches a neighbour's body still goes to its own), the value is the word just below, above or beside it, a
   short number by a pin line is its pin number, the word inside the body in line with the pin its pin name. A
   small symbol with one connection and a power name (GND, VCC, +3.3V ...) is a power port; a closed shape with a
   name inside is a label flag; a word standing on a wire is a net label. Same names are one net.
5. Pins without a printed number (resistors, capacitors) are numbered 1, 2 left to right or top to bottom and
   listed in `guessed_pins`: check the orientation where it matters (diodes, polarised capacitors).
6. A wire that two different power ports name (a misread symbol would short GND to +5V) joins neither and is
   reported as `CONFLICT(...)`.

## How good it is

Measured against the netlists the EDA tool exported for the same design:

| Input | Parts | Pin connections (precision / recall) | Identical nets |
|---|---|---|---|
| KiCad 5 PDF, 5 pages, 86 parts (STM32F405 board) | 82 of 82 with connections | 87 % / 82 % | 66 of 80 |
| the same pages as 300 dpi black-and-white PNGs | 69 of 82 | 9 % / 10 % | 2 of 80 |
| one simple page of it (buck converter) as a 300 dpi PNG | 13 of 18 | 35 % / 37 % | 2 of 11 |
| the test drawing (`tests/mechatronics/test_schematic_scan.py`), PDF and 300 dpi PNG | all | 100 % / 100 % | all |

(The PDF of that board is a few weeks older than its netlist: one part, a USB ESD chip, is in the drawing and not in
the netlist, which accounts for some of the misses.)

So: **vector PDFs** give a usable netlist to review; **pictures** are read well when the drawing is clean and
simple (or coloured, which separates wires from symbols), while dense black-and-white pages with thin lines and
small text (an MCU with a hundred labelled pins) are not reliable yet: OCR misses short words, and unread text
becomes stray symbol ink. Always look at the overlay before using a scanned netlist, and import it with the project
importer only after checking the parts that matter (the MCU's pins, the power rails).
