"""SMD part identification: value codes (resistors, capacitors, inductors), marking codes (short top-marks of
small packages), printed part numbers (74-series logic, STM32 ...) and packages from their size.

    from embeddyn.mechatronics.smd import decode, classify
    decode("472").best.part                      # '4.7k resistor'
    decode("A7", package="SOT-23").best.part     # 'BAV99'
    classify(1.6, 0.8, leads=2)[0].package.name  # '0603'

``python -m embeddyn.mechatronics.smd 472 A7:SOT-23 --size 3x1.6x6`` from a shell. See docs/guides/pcb-photo.md.
"""
from .decode import Decoded, Reading, code_table, decode
from .packages import PACKAGES, Match, Package, classify, number_pins, package
from .values import Value, decode_capacitor, decode_inductor, decode_resistor, decode_value

__all__ = ["Decoded", "Reading", "code_table", "decode", "PACKAGES", "Match", "Package", "classify", "number_pins",
           "package", "Value", "decode_capacitor", "decode_inductor", "decode_resistor", "decode_value"]
