#include <stdint.h>

extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss, _estack;
int main(void);

void Reset_Handler(void)
{
    /* Enable the FPU (CP10/CP11 full access) before any float code runs. */
    *(volatile uint32_t *)0xE000ED88u |= (0xFu << 20);
    __asm volatile ("dsb\n\tisb" ::: "memory");
    uint32_t *src = &_sidata;
    for (uint32_t *dst = &_sdata; dst < &_edata;) {
        *dst++ = *src++;
    }
    for (uint32_t *dst = &_sbss; dst < &_ebss;) {
        *dst++ = 0;
    }
    main();
    for (;;) {
    }
}

static void Default_Handler(void)
{
    for (;;) {
    }
}

void TIM2_IRQHandler(void);

/* STM32L476: 16 core exceptions + external IRQs up to TIM2 (IRQ 28). */
__attribute__((section(".isr_vector"), used))
const void *const vectors[16 + 29] = {
    [0] = &_estack,
    [1] = Reset_Handler,
    [2 ... 15] = Default_Handler,
    [16 ... 16 + 27] = Default_Handler,
    [16 + 28] = TIM2_IRQHandler,
};
