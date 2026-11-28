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
4. **Markings** are read text-first (`pcb_photo.markings`), on the board photo and on every close-up placed on
   the board (by correlating edge images over scale and rotation):
   * text is found two ways: dark bodies, and lines of bright character-sized strokes on dark plastic (so packed
     parts whose bodies touch still give one word each);
   * each is turned to its axis in every orientation it can have, its body's texture removed (a white top-hat);
   * it is read by tesseract and by a character classifier: nearest neighbours on gradient features, trained
     once on about 60 installed sans fonts with blur, weight, squeeze and noise variations (cached in
     `~/.cache/embeddyn/`), plus digit shapes common on markings that fonts draw differently (the open 4);
   * the orientation and reading whose words decode best win; chip parts are read as digits and R only, and
     codes that are not standard E24/E96 values rank lower (`561` upside down is `195`: 19 is not an E24 value);
   * every word keeps its position, so it lands on the part under it; close-ups count more than the board photo.

   **Teaching the font.** Marking fonts repeat across a board (all resistors of one maker look alike). A marking
   typed in by hand (the Lab's part panel, or `--marking`) is also learned: its characters, cut from the sharpest
   photo of that part, are kept in `~/.config/embeddyn/glyphs/learned.npz` (12 per character at most) and every
   part is read again with them, in seconds. A few typed codes on a board improve the rest.
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
  * markings: measured on four close-ups of that board against the 18 markings visible in
    them (codes like `472`, `680`, `561`, `BZCW`, `74HC595D`, `F051K6`): 2-3 are read, plus many readings that
    are not markings. Only confident readings that decode well are put on parts (7 parts on that board, about 4
    of them right), each with "check it". Laser-etched IC markings and codes smaller than about 20 px are rarely
    read. Type the marking in (the Lab's part panel or `--marking`): the decoder, pin numbering and netlist follow,
    and the reader learns the font for the rest of the board. Close-ups at full resolution read a little more,
    at three times the time; they are read at 2400 px.
  * shadows of tall parts (battery clips) and glare are taken for copper or bodies near them;
  * parts with many leads in shade (SOT-23s, optocouplers) can lose a lead;
  * what is not visible is not found: inner layers, traces under parts, tented vias whose hole is covered.

Always compare the overlay with the board before using the netlist, check the red (guessed) pins of ICs, and
check the parts whose package lists alternatives.
