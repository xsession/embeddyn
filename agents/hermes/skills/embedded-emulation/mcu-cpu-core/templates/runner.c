/*
 * Native runner: load an Intel HEX file, execute N steps, print one line per step
 *   PC OPCODE R0 .. R15 SR      (hex)
 * then "RAM <words>" for the data RAM. trace_from_columns turns this into JSON Lines.
 * Adapt: program memory layout (HEX byte address -> program address), RAM range, register count.
 *
 *   cc -O2 -o runner runner.c core.c && ./runner firmware.hex 20000 > trace.txt
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "core.h"

static uint32_t prog[0x40000];
static uint8_t data[0x10000];
#define RAM_START 0x0800
#define RAM_END   0x1800

static uint8_t rd8(void *c, uint32_t a) { (void)c; return data[a & 0xFFFF]; }
static uint16_t rd16(void *c, uint32_t a) { (void)c; a &= 0xFFFE; return (uint16_t)(data[a] | data[a + 1] << 8); }
static void wr8(void *c, uint32_t a, uint8_t v) { (void)c; data[a & 0xFFFF] = v; }
static void wr16(void *c, uint32_t a, uint16_t v) { (void)c; a &= 0xFFFE; data[a] = (uint8_t)v; data[a + 1] = (uint8_t)(v >> 8); }
static uint32_t prog_rd(void *c, uint32_t pa) { (void)c; return prog[(pa >> 1) & 0x3FFFF]; }

static int load_hex(const char *path)
{
    FILE *f = fopen(path, "r");
    char line[600];
    uint32_t base = 0;
    if(!f) return -1;
    while(fgets(line, sizeof line, f)) {
        unsigned n, addr, type, b;
        if(line[0] != ':' || sscanf(line + 1, "%2x%4x%2x", &n, &addr, &type) != 3) continue;
        if(type == 4) { unsigned hi; sscanf(line + 9, "%4x", &hi); base = hi << 16; continue; }
        if(type != 0) continue;
        for(unsigned i = 0; i < n; i++) {
            sscanf(line + 9 + 2 * i, "%2x", &b);
            uint32_t byte_addr = base + addr + i;
            uint32_t word = byte_addr >> 2, lane = byte_addr & 3;      /* TODO: target layout */
            if(word < 0x40000 && lane < 3) prog[word] = (prog[word] & ~(0xFFu << (8 * lane))) | (b << (8 * lane));
        }
    }
    fclose(f);
    return 0;
}

int main(int argc, char **argv)
{
    if(argc < 3) { fprintf(stderr, "usage: runner file.hex steps\n"); return 2; }
    if(load_hex(argv[1])) { perror(argv[1]); return 1; }
    long steps = strtol(argv[2], NULL, 0);
    mymcu_state s;
    mymcu_reset(&s);
    mymcu_bus bus = { NULL, rd8, rd16, wr8, wr16, prog_rd, NULL };
    for(long k = 0; k < steps; k++) {
        printf("%06X %06X", s.pc, prog_rd(NULL, s.pc));
        for(int r = 0; r < 16; r++) printf(" %04X", s.r[r]);
        printf(" %04X\n", s.sr);
        uint32_t ev = mymcu_step(&s, &bus);
        if(ev & MY_EV_TRAP) mymcu_service_traps(&s, &bus);
        if(ev & MY_EV_UNDEF) { printf("UNDEF %06X at %06X\n", s.last_insn, s.pc); return 3; }
    }
    printf("RAM");
    for(uint32_t a = RAM_START; a < RAM_END; a += 2) printf(" %04X", rd16(NULL, a));
    printf("\n");
    return 0;
}
