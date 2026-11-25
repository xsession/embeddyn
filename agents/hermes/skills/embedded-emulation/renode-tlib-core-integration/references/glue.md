# Glue that worked (dsPIC30 in tlib)

Copied from the working fork (MIT). Adapt names, PC conversion and opcode lengths.

## translate.c: one helper per instruction, early exit on non-sequential PC

```c
int gen_intermediate_code(CPUState *env, DisasContextBase *base)
{
    uint32_t insn = ldl_code(base->pc) & 0xFFFFFFu;
    uint8_t op = (uint8_t)(insn >> 16);
    int words = (op == 0x02 || op == 0x04 || op == 0x08) ? 2 : 1;   /* CALL, GOTO, DO */
    uint32_t index = base->tb->icount - 1;                         /* already counted */

    tcg_gen_insn_start(base->pc);
    TCGv_i32 sequential = tcg_temp_new_i32();
    TCGv_i32 position = tcg_const_i32(index);
    gen_helper_step(sequential, cpu_env, position);
    tcg_temp_free_i32(position);

    /* not sequential: the helper has set the PC; end the block after this instruction */
    int next = gen_new_label();
    tcg_gen_brcondi_i32(TCG_COND_NE, sequential, 0, next);
    TCGv_i32 executed = tcg_const_i32(index + 1);
    tcg_gen_st_i32(executed, cpu_env, offsetof(CPUState, instructions_count_declaration));
    tcg_temp_free_i32(executed);
    gen_exit_tb_no_chaining(base->tb);
    gen_set_label(next);
    tcg_temp_free_i32(sequential);

    base->tb->size += 4 * words;
    base->pc += 4 * words;
    /* stop at page boundaries and on instructions that change control flow anyway */
    if((base->pc & (TARGET_PAGE_SIZE - 1)) < 8 && (base->pc & (TARGET_PAGE_SIZE - 1)) < 4 * words) {
        return 0;
    }
    switch(op) {
    case 0x01: case 0x02: case 0x04: case 0x05: case 0x06: case 0x07: case 0x09: case 0xFE:
        return 0;   /* computed branches, CALL, GOTO, RETLW/RETURN/RETFIE, RCALL, REPEAT, RESET/PWRSAV */
    default:
        return 1;
    }
}

void gen_intermediate_code_epilogue(CPUState *env, DisasContextBase *base)
{
    /* fell through the last instruction: continue at the next one */
    tcg_gen_movi_tl(cpu_pc, base->pc);
    gen_exit_tb_no_chaining(base->tb);
}

```

## op_helper.c: the step helper

```c
uint32_t HELPER(step)(CPUState *env, uint32_t index)
{
    dspic30_state *s = &env->d30;
    if(s->pending_traps && dspic30_service_traps(s, &dspic_bus)) {
        env->pc = dspic_bus_pc(s->pc);
        declare_executed(env, index);
        cpu_loop_exit(env);
    }
    uint32_t start = env->pc;
    if(env->power_save) {
        env->power_save = 0;
        tlib_dspic_wake();
    }
    s->pc = dspic_isa_pc(start);
    uint8_t op = (uint8_t)(s->last_insn = 0, ldl_code(start) >> 16);
    uint32_t next = start + ((op == 0x02 || op == 0x04 || op == 0x08) ? 8 : 4);
    uint32_t ev = dspic30_step(s, &dspic_bus);
    if(ev) {
        declare_executed(env, index + 1);
    }
    if(ev & D30_EV_UNDEF) {
        declare_executed(env, index);
        tlib_printf(LOG_LEVEL_WARNING, "dsPIC: illegal opcode 0x%06X at 0x%06X, resetting", s->last_insn, s->pc);
        raise_exception(env, EXCP_UNDEF);
    }
    env->pc = dspic_bus_pc(s->pc);
    if(ev & D30_EV_TRAP) {
        dspic30_service_traps(s, &dspic_bus);
        env->pc = dspic_bus_pc(s->pc);
    }
    if(ev & D30_EV_CLRWDT) {
        tlib_dspic_clrwdt();
    }
    if(ev & D30_EV_RESET) {
        tlib_dspic_reset_request(DSPIC_RESET_SOFTWARE);
        env->wfi = 1;
        cpu_loop_exit(env);
    }
    if(ev & (D30_EV_SLEEP | D30_EV_IDLE)) {
        env->wfi = 1;
        env->power_save = 1;
        if(ev & D30_EV_SLEEP) {
            tlib_on_cpu_power_down();
        } else {
            tlib_on_cpu_halted();
        }
        cpu_loop_exit(env);
    }
    if(ev & D30_EV_BREAK) {
        raise_exception(env, EXCP_DEBUG);
    }
    return env->pc == next;
}

```

## op_helper.c: interrupts published by the C# controller

```c
int process_interrupt(int interrupt_request, CPUState *env)
{
    if(tlib_is_in_debug_mode()) {
        return 0;
    }
    if(interrupt_request & CPU_INTERRUPT_RESET) {
        cpu_reset(env);
        return 1;
    }
    if(interrupt_request & CPU_INTERRUPT_HARD) {
        dspic30_state *s = &env->d30;
        int irq = env->pending_irq, prio = env->pending_priority;
        if(irq >= 0 && prio > dspic30_cpu_priority(s) && !(s->disicnt && prio < 7)) {
            if(env->power_save) {
                env->power_save = 0;
                tlib_dspic_wake();
            }
            s->pc = dspic_isa_pc(env->pc);
            dspic30_take_interrupt(s, &dspic_bus, irq, prio);
            if(s->intcon1 & D30_INTCON1_NSTDIS) {
                s->sr = (uint16_t)((s->sr & ~D30_SR_IPL) | (7u << D30_SR_IPL_SHIFT));
            }
            env->pc = dspic_bus_pc(s->pc);
            env->wfi = 0;
            return 1;
        }
    }
    return 0;
}

```

## helper.h

```c
DEF_HELPER_2(step, i32, env, i32)
DEF_HELPER_2(raise_exception, void, env, i32)
```

## C#: exports (callbacks from C) and imports (calls into C)

```csharp
        [Export]
        private void OnCpuHalted()
        {
            PowerSave?.Invoke(false);
        }
[Import]
        private readonly Action<uint> TlibSetEntryPoint;

        [Import]
        private readonly Action TlibClearWfi;

        [Import]
        private readonly Action TlibSetWfi;

        [Import]
        private readonly Action<int, int> TlibSetPendingInterrupt;

        [Import]
        private readonly Action<int> TlibSetAltivt;

        [Import]
        private readonly Func<int> TlibGetCpuPriority;

        [Import]
        private readonly Action<int> TlibRaiseTrap;
```

C side of a callback: `DEFAULT_VOID_HANDLER1(void tlib_dspic_reset_request, int32_t reason)` in arch_callbacks.c and `EXTERNAL_AS(void, OnResetRequest, tlib_dspic_reset_request, int32_t)` in Cores/renode/arch/<arch>/renode_<arch>_callbacks.c; an export is a C function plus `EXC_VOID_1(name, type, arg)`.
