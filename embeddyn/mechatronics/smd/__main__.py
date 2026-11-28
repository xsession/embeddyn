"""python -m embeddyn.mechatronics.smd MARKING[:PACKAGE] ... [--kind resistor] [--size LxW[xLEADS]]

Decode SMD markings (value codes, marking codes, printed part numbers) and find packages from their size.
Examples: 472   A7:SOT-23   "74HC595D"   F051K6U6   --size 3x1.6x6
"""
from __future__ import annotations

import argparse
import json
import sys

from . import classify, decode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m embeddyn.mechatronics.smd", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("markings", nargs="*", help="marking text, optionally :PACKAGE (e.g. A7:SOT-23)")
    ap.add_argument("--kind", help="resistor, capacitor, inductor, diode, transistor, ic ...")
    ap.add_argument("--size", help="body LxW in mm, optionally xLEADS (e.g. 2.9x1.6x6)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    out = []
    for item in a.markings:
        text, _, pkg = item.partition(":") if ":" in item and not item.endswith(":") else (item, "", "")
        result = decode(text.replace("\\n", "\n"), pkg or None, a.kind)
        out.append(result.as_dict())
        if not a.json:
            print(f"{text!r}" + (f" ({pkg})" if pkg else ""))
            for r in result.readings[:6]:
                flag = "  [check]" if r.confidence == "check" else ""
                print(f"  {r.score:4.2f}  {r.part}" + (f"  {r.package}" if r.package else "") + (f"  - {r.detail}" if r.detail else "")
                      + (f"  pins: {r.pins}" if r.pins else "") + flag)
            for note in result.notes:
                print("  note:", note)
    if a.size:
        parts = [float(x) for x in a.size.lower().split("x")]
        matches = classify(parts[0], parts[1], int(parts[2]) if len(parts) > 2 else None)
        out.append({"size": a.size, "packages": [m.as_dict() for m in matches]})
        if not a.json:
            print(f"body {a.size}:")
            for m in matches:
                print(f"  {m.score:4.2f}  {m.package.name}  ({m.why})")
    if a.json:
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
