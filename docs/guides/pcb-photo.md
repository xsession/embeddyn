# Reverse engineer a board from photos

When a board exists only as hardware, photos of it give a first netlist: which parts there are, what package each
is, what its marking says, and which pads the copper joins. The result is a KiCad netlist (`board.net`), so the
[board importer](import-a-board.md) and the Lab can use it like any other.

```bash
python -m embeddyn.mechatronics.importer.pcb_photo top.jpg --bottom bottom.jpg \
    --closeup close1.jpg --closeup close2.jpg --board-mm 107x126 --out board/
python -m embeddyn.mechatronics.importer.pcb_photo top.jpg --out board/ --marking "U3=74HC595D"
```

| Output | What it is |
|---|---|
| `board/board.net` | KiCad netlist: parts (value from the marking, package as footprint), nets |
| `board/board.json` | every part: package and alternatives, body size, marking and its readings, pins (guessed or not), position in mm |
| `board/overlay-top.jpg`, `overlay-bottom.jpg` | the straightened board, each net's copper in its own colour, parts boxed with their reference, pins as dots (red: number guessed), vias in cyan |
| `board/schematic.svg` | a draft net-label schematic: each part as a box, the net name at every pin |

In the Lab, the **Scan** tab has a **Board photos** mode: choose the photos (top, bottom, then close-ups), read,
click a part to see what was found and to type its marking; the **SMD decoder** box decodes a marking or a body
size on its own. **Import & open** turns the netlist into an emulated board project.

## Taking the photos

* One photo of each side with the **whole board** in view and the board filling most of the picture. A phone is
  fine; hold it square to the board, use even light (no direct lamp reflections on the solder), no flash.
* Put the board on a plain, non-green background.
* **Close-ups** for the markings: 30-60 mm of board in view, sharp, taken square to the board. Oblique or blurred
  close-ups are not placed (the report says so).
* Give the board size (`--board-mm`, or **size mm** in the Lab) if you know it; otherwise the scale comes from
  parts whose part number fixes their size (a marking typed in for an SOIC, for example), else a 100 mm board is
  assumed and the package names can be off by one size.

## How it reads a board

1. **Straighten.** The board is the largest region of the solder-mask colour; its four corners map onto a
   rectangle (a homography), so everything after works in board millimetres. The bottom photo is mirrored (seen
   through the board) and fitted onto the top by matching via holes.
2. **Classify pixels**, after white balance (the brightest off-mask pixels, solder highlights, are taken as
   white): solder mask; copper under the mask (lighter than the mask around it); bare metal (pads, solder, leads:
   neutral and textured); dark bodies (ICs, transistors, resistors, with their marking text); light bodies
   (ceramic capacitors, white and beige plastic: smooth); silkscreen (thin white); holes (small dark spots inside a
   metal ring).
3. **Parts** are body regions with the metal blobs around them as pads. Body size (two-terminal parts: the distance
   between their metal ends), number of pads and on how many sides they sit give the **package**, from a table of
   about 100 SMD packages (chip sizes, SOD, SMA/B/C, tantalum cases, SOT, SOIC, TSSOP, SSOP, MSOP, DFN, QFN, LGA,
   LQFP, crystals, LEDs, electrolytics, power inductors).
4. **Markings** are read by OCR on each part, turned to its axis, with the body's own texture removed; close-ups
   are placed on the board by correlating edge images over scale and rotation, and their markings go to the parts
   they show. A reading is kept only when it decodes (see below) or is a confident alphanumeric string.
5. **Pins** are numbered counter-clockwise from pin 1: the lower left of the marking for dual-row and SOT parts,
   the upper left for quad parts. Without a marking (or a typed-in orientation) the numbering is a guess and
   marked red.
6. **Nets**: connected copper (copper under the mask plus pads) is one net per side; vias and through-hole pads
   join the two sides.

## The SMD decoder

`python -m embeddyn.mechatronics.smd 472 A7:SOT-23 74HC595D F051K6U6 --size 2.9x1.6x6`, or in Python
`embeddyn.mechatronics.smd.decode(text, package)`:

* **Values**: resistors (3- and 4-digit, `4R7`, `R047`, EIA-96 `01C`), capacitors (pF digit codes, tantalum
  voltage letters, EIA-198 letter+digit), inductors (µH digit codes). Implausible readings rank lower (an upside
  down `680` reads `089`).
* **Marking codes** of small packages, ranked by package (`A7` in SOT-23 is a BAV99; Nexperia's site letter
  `1Ft` is stripped). The built-in table has common, widely second-sourced parts; codes worth confirming are
  marked `check`. Add your own tables (CSV `code,package,part,maker,function[,pins][,confidence][,prefix]`, or
  YAML/JSON with the same keys) in `~/.config/embeddyn/smd-codes/` or list them in `$EMBEDDYN_SMD_CODES`.
  `prefix: true` is for makers that add a date or lot code after a device code (Microchip, TI small packages).
* **Part numbers**: 74-series logic (function, family, package from the suffix), STM32 (ST prints `F051K6U6` for
  STM32F051K6U6: pins, flash, package, temperature), anything else as "look it up". An OCR reading close to a
  known 74-series number (`PANCSSSD` for an etched `74HC595D`) is offered with a `?` and `check`.
* **Packages from size**: `classify(length, width, leads, sides)` ranks the table's packages.

No openly licensed marking-code database exists to ship; the big code books (DL7AVF/KyteLabs, the SMD Codebook)
carry no licence that allows copying them into the repository. Codes you collect go in your own table.

## How good it is

* A drawn test board (`tests/mechatronics/test_pcb_photo.py`: an 0805 resistor marked `103`, an 0805 capacitor, an
  SOIC-8, three traces, slight perspective): all parts, packages and connections are found.
* A real two-layer board (about 85 x 100 mm, phone photos of both sides): 132 parts, 67 nets with copper traced
  across the board, the bottom fitted onto the top to 0.4 mm. The weak points:
  * markings: on the whole-board photo (25 px/mm) only a few are read; ICs with laser-etched markings are
    unreadable there. Close-ups help where they can be placed; otherwise type the marking in (the Lab's part
    panel or `--marking`): the decoder, the pin numbering and the netlist follow.
  * shadows of tall parts (battery clips) and glare are taken for copper or bodies near them;
  * parts with many leads in shade (SOT-23s, optocouplers) can lose a lead;
  * what is not visible is not found: inner layers, traces under parts, tented vias whose hole is covered.

Always compare the overlay with the board before using the netlist, check the red (guessed) pins of ICs, and
check the parts whose package lists alternatives.
