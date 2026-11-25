# Example: Microchip sim30 as the reference

## Script (stdin of sim30)

```
LD dspic30super          # generic device
LC program.elf           # load COFF/ELF
RP                       # reset
DC                       # per step: display PC + opcode   } repeated
DW                       # display W registers             } N times
DS                       # display status (binary)         }
S                        # step                            }
DF 800 17fe              # at the end: dump RAM
Q
```

## Parsing

- `PC = 000104 20C00F` gives the PC and opcode.
- `Loc <  0>` lines give the W registers (8 per line).
- `Binary = ...` gives SR.
- `Memory dump from 0x0800` gives RAM.

## sim30 quirks found (the core follows the manual; the generators avoid these forms or the comparison tolerates them)

| Case | sim30 | core (manual) |
|---|---|---|
| DC after word operations | carry out of bit 3 | carry out of bit 7 |
| N/C/OV after DIV | iteration-exact flags | exact quotient/remainder, Z exact |
| destination post-modify after an indirect source | skipped | applied |
| destination post-decrement under modulo addressing | compares the old address with the start | compares the new address |
| `dspic30super` Y RAM | starts at 0x3000 | the 5011's at 0x1000 (core parameter) |
| first S after RP | does not advance | the harness adds one step |
