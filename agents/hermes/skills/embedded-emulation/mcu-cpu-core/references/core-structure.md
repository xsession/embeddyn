# Core structure that worked (dsPIC30F, 16-bit, 24-bit instructions)

## State and API

```c
typedef struct dspic30_bus {
    void *ctx;
    uint8_t  (*rd8)(void *ctx, uint32_t addr);
    uint16_t (*rd16)(void *ctx, uint32_t addr);          /* addr even */
    void     (*wr8)(void *ctx, uint32_t addr, uint8_t v);
    void     (*wr16)(void *ctx, uint32_t addr, uint16_t v);
    uint32_t (*prog_rd)(void *ctx, uint32_t paddr);      /* 24-bit word */
    void     (*prog_wr)(void *ctx, uint32_t paddr, uint16_t v, int high, int byte);
} dspic30_bus;

void     dspic30_reset(dspic30_state *s);
uint32_t dspic30_step(dspic30_state *s, const dspic30_bus *bus);   /* returns D30_EV_* */
int      dspic30_cpu_priority(const dspic30_state *s);
void     dspic30_take_interrupt(dspic30_state *s, const dspic30_bus *bus, int irq, int priority);
int      dspic30_service_traps(dspic30_state *s, const dspic30_bus *bus);
```

## Dispatch

`switch(insn >> 16)` over the top opcode byte, with a field macro
`#define F(hi, lo) ((insn >> (lo)) & ((1u << ((hi)-(lo)+1)) - 1u))`.

- One helper per instruction family (`op_math3`, `op_bits_ws`, `op_mac`, ...).
- A `ctx` struct carries the per-step results: next PC, skip flag, jumped flag, cycles, events.

## Effective addresses

```c
typedef struct { int direct; int reg; uint32_t addr; } opnd_t;
opnd_t ea(ctx, mode, reg, byte, wb);  /* 0 Wn, 1 [Wn], 2 [Wn--], 3 [Wn++], 4 [--Wn], 5 [++Wn], 6/7 [Wn+Wb] */
```

Pointer updates go through one function, `w_step()`, which also applies modulo and bit-reversed
addressing.

## End of step

1. REPEAT: decrement the count, or clear the repeat flag and advance.
2. Skip: add the next instruction's length.
3. DO: on the end address, loop back or pop.
4. DISI: decrement its counter.
5. If traps are pending, return the TRAP event.

## Lessons

- Serve the data-space-mapped core registers in `core_read16`/`core_write16`, so that byte writes
  merge correctly and read-only bits stay read-only.
- Keep model-specific facts (Y data space range, device variants) in the state, set by reset or by
  the glue, never as compile-time constants.
