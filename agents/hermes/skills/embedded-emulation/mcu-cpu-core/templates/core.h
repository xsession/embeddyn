/*
 * MYMCU CPU core: a standalone instruction-set interpreter with memory callbacks.
 * No tlib/Renode headers: the same file builds in tlib and natively for the differential tests.
 * Replace MYMCU/mymcu, the register set and the events with the target's.
 */
#pragma once
#include <stdint.h>

/* status register bits (from the programmer's reference manual) */
#define MY_SR_C (1u << 0)
#define MY_SR_Z (1u << 1)
#define MY_SR_N (1u << 2)
#define MY_SR_V (1u << 3)

/* step events (bit flags returned by mymcu_step) */
#define MY_EV_SLEEP  (1u << 0)
#define MY_EV_IDLE   (1u << 1)
#define MY_EV_RESET  (1u << 2)   /* software reset instruction */
#define MY_EV_BREAK  (1u << 3)
#define MY_EV_UNDEF  (1u << 4)   /* undefined opcode: PC not advanced */
#define MY_EV_TRAP   (1u << 5)   /* a trap is pending: call mymcu_service_traps */
#define MY_EV_CLRWDT (1u << 6)

typedef struct mymcu_bus {
    void *ctx;
    uint8_t  (*rd8)(void *ctx, uint32_t addr);
    uint16_t (*rd16)(void *ctx, uint32_t addr);
    void     (*wr8)(void *ctx, uint32_t addr, uint8_t value);
    void     (*wr16)(void *ctx, uint32_t addr, uint16_t value);
    uint32_t (*prog_rd)(void *ctx, uint32_t paddr);          /* one instruction word */
    void     (*prog_wr)(void *ctx, uint32_t paddr, uint32_t value, int flags);
} mymcu_bus;

typedef struct mymcu_state {
    uint16_t r[16];            /* general registers */
    uint32_t pc;               /* architectural program counter */
    uint16_t sr;               /* status */
    uint16_t repeat_count;     /* repeat/loop state lives here, never in statics */
    uint32_t pending_traps;    /* bit n = trap n requested */
    uint32_t cycles;           /* cycles of the last step */
    uint32_t last_insn;
} mymcu_state;

void     mymcu_reset(mymcu_state *s);
uint32_t mymcu_step(mymcu_state *s, const mymcu_bus *bus);
int      mymcu_cpu_priority(const mymcu_state *s);
void     mymcu_take_interrupt(mymcu_state *s, const mymcu_bus *bus, int irq, int priority);
int      mymcu_service_traps(mymcu_state *s, const mymcu_bus *bus);
/* registers the architecture maps into data space (served before the bus) */
int      mymcu_core_read16(mymcu_state *s, uint32_t addr, uint16_t *value);
int      mymcu_core_write16(mymcu_state *s, uint32_t addr, uint16_t value);
