/*
 * Bare-metal STM32L476 body-contour controller for a dual-head SPECT camera
 * (Embeddyn/Renode example examples/mechatronics/spect-body-contour.yaml).
 *
 * Each detector head carries an infrared light curtain a few millimetres
 * below its face: 16 emitters on one rail, 16 receivers on the other. The
 * controller fires one beam at a time and reads the matching receiver, then
 * moves each head in until one of its beams is interrupted, so the detector
 * faces follow the patient's body contour while the gantry orbits.
 *
 *   PB0..PB4  GPIO out  -> beam address (0..15 head A, 16..31 head B), to the
 *                          emitter decoder and the receiver multiplexer
 *   PB5       GPIO out  -> emitter enable
 *   PC0       ADC1_IN1  <- receiver multiplexer output (analog signal level)
 *   PC2       GPIO in   <- receiver comparator: 1 = light received
 *   PC3, PC4  GPIO in   <- touch pads on head A, head B (1 = contact)
 *   PA5       GPIO out  -> fault LED (touch pad hit: heads retracted, latched)
 *   PA2/PA3   USART2    -> 115200 8N1 console: status every 100 ms;
 *                          'R' clears a latched fault
 *
 * TIM2 interrupts every 200 us (5 kHz). Each interrupt samples the beam
 * selected by the previous one (200 us of settling for the receiver) and
 * selects the next: a full scan of 32 beams takes 6.4 ms. After each scan
 * the motion commands are updated:
 *
 *   touch pad hit            -> fault: both heads retract at v_retract (latched)
 *   any beam of a head dark  -> that head moves out at v_out
 *   all beams clear          -> that head moves in at v_in
 *
 * The commands (m/s, positive = away from the patient) are the set-points of
 * the head drives, read from the telemetry block (fixed address after the
 * calibration) like a motor controller's set-point register.
 */
#include <stdint.h>

#define REG(addr) (*(volatile uint32_t *)(addr))

#define RCC_BASE      0x40021000u
#define RCC_CR        REG(RCC_BASE + 0x00u)
#define RCC_CFGR      REG(RCC_BASE + 0x08u)
#define RCC_PLLCFGR   REG(RCC_BASE + 0x0Cu)
#define RCC_AHB2ENR   REG(RCC_BASE + 0x4Cu)
#define RCC_APB1ENR1  REG(RCC_BASE + 0x58u)
#define RCC_CCIPR     REG(RCC_BASE + 0x88u)
#define FLASH_ACR     REG(0x40022000u)

#define GPIOA_BASE    0x48000000u
#define GPIOB_BASE    0x48000400u
#define GPIOC_BASE    0x48000800u
#define GPIO_MODER(b) REG((b) + 0x00u)
#define GPIO_IDR(b)   REG((b) + 0x10u)
#define GPIO_ODR(b)   REG((b) + 0x14u)
#define GPIO_BSRR(b)  REG((b) + 0x18u)
#define GPIO_AFRL(b)  REG((b) + 0x20u)
#define GPIO_ASCR(b)  REG((b) + 0x2Cu)

#define TIM2_BASE     0x40000000u
#define TIM2_CR1      REG(TIM2_BASE + 0x00u)
#define TIM2_DIER     REG(TIM2_BASE + 0x0Cu)
#define TIM2_SR       REG(TIM2_BASE + 0x10u)
#define TIM2_EGR      REG(TIM2_BASE + 0x14u)
#define TIM2_PSC      REG(TIM2_BASE + 0x28u)
#define TIM2_ARR      REG(TIM2_BASE + 0x2Cu)

#define ADC1_BASE     0x50040000u
#define ADC1_ISR      REG(ADC1_BASE + 0x00u)
#define ADC1_CR       REG(ADC1_BASE + 0x08u)
#define ADC1_SMPR1    REG(ADC1_BASE + 0x14u)
#define ADC1_SQR1     REG(ADC1_BASE + 0x30u)
#define ADC1_DR       REG(ADC1_BASE + 0x40u)
#define ADC_CCR       REG(ADC1_BASE + 0x308u)

#define USART2_BASE   0x40004400u
#define USART2_CR1    REG(USART2_BASE + 0x00u)
#define USART2_BRR    REG(USART2_BASE + 0x0Cu)
#define USART2_ISR    REG(USART2_BASE + 0x1Cu)
#define USART2_RDR    REG(USART2_BASE + 0x24u)
#define USART2_TDR    REG(USART2_BASE + 0x28u)
#define USART_TXE     (1u << 7)
#define USART_RXNE    (1u << 5)

#define NVIC_ISER0    REG(0xE000E100u)
#define SCB_VTOR      REG(0xE000ED08u)
#define TIM2_IRQ      28u

#define SLOT_US       200u          /* one beam per slot */
#define BEAMS         32u           /* 16 per head */
#define BEAMS_PER_HEAD 16u
#define EMITTER_ON    (1u << 5)
#define ADC_FULL      4095.0f
#define VREF_MV       3300u

struct contour_cal {
    uint32_t magic;          /* 0x434E5452 "CNTR" */
    float v_in;              /* approach speed, m/s */
    float v_out;             /* back-off speed while a beam is dark, m/s */
    float v_retract;         /* retract speed after a touch, m/s */
    uint32_t weak_mv;        /* a lit beam below this level is reported weak */
    uint32_t status_scans;   /* console line every n scans */
};

__attribute__((section(".cal"), used))
volatile struct contour_cal contour_cal = {
    0x434E5452u, 0.05f, 0.08f, 0.10f, 900u, 16u,
};

/* Telemetry (.telemetry section, right after contour_cal at 0x20000018). */
struct contour_telemetry {
    uint32_t scan_count;     /* +0x00 */
    uint32_t beam;           /* +0x04 beam selected now */
    uint32_t dark;           /* +0x08 dark beams of the last scan (bit = beam) */
    uint32_t weak;           /* +0x0C lit beams below weak_mv in the last scan */
    uint32_t fault;          /* +0x10 1 = touch pad hit, heads retracted */
    float v_a;               /* +0x14 head A speed command, m/s (+ = out) */
    float v_b;               /* +0x18 head B speed command */
    uint32_t min_lit_mv;     /* +0x1C weakest lit beam of the last scan, mV */
};

__attribute__((section(".telemetry"), used))
volatile struct contour_telemetry tm;

static uint32_t dark_acc, weak_acc, min_lit_acc = 0xFFFFFFFFu;

static uint32_t adc_read(uint32_t channel)
{
    while (ADC1_CR & (1u << 2)) {   /* previous sequence still running (see rc-servo-l476) */
    }
    ADC1_SQR1 = channel << 6;
    ADC1_ISR = (1u << 2) | (1u << 3);
    ADC1_CR |= (1u << 2);           /* ADSTART */
    while ((ADC1_ISR & (1u << 2)) == 0) {
    }
    return ADC1_DR & 0xFFFu;
}

static void uart_putc(char c)
{
    while ((USART2_ISR & USART_TXE) == 0) {
    }
    USART2_TDR = (uint32_t)(unsigned char)c;
}

static void uart_puts(const char *text)
{
    while (*text) {
        uart_putc(*text++);
    }
}

static void uart_put_int(int32_t value)
{
    char digits[12];
    int n = 0;
    uint32_t magnitude = value < 0 ? (uint32_t)(-value) : (uint32_t)value;
    if (value < 0) {
        uart_putc('-');
    }
    do {
        digits[n++] = (char)('0' + magnitude % 10u);
        magnitude /= 10u;
    } while (magnitude && n < 11);
    while (n) {
        uart_putc(digits[--n]);
    }
}

static void uart_field(const char *name, int32_t value)
{
    uart_puts(name);
    uart_put_int(value);
}

static uint32_t popcount16(uint32_t v)
{
    uint32_t n = 0;
    for (v &= 0xFFFFu; v; v &= v - 1u) {
        n++;
    }
    return n;
}

static int32_t mm_per_s(float v)
{
    return (int32_t)(v * 1000.0f + (v < 0 ? -0.5f : 0.5f));
}

static void console(void)
{
    while (USART2_ISR & USART_RXNE) {
        char c = (char)USART2_RDR;
        if (c == 'R' && tm.fault) {
            tm.fault = 0;
            GPIO_BSRR(GPIOA_BASE) = (1u << (5 + 16));
            uart_puts("fault cleared\r\n");
        }
    }
    if (tm.scan_count % contour_cal.status_scans == 0u) {
        uart_field("scan=", (int32_t)tm.scan_count);
        uart_field(" dark_a=", (int32_t)popcount16(tm.dark));
        uart_field(" dark_b=", (int32_t)popcount16(tm.dark >> 16));
        uart_field(" va=", mm_per_s(tm.v_a));
        uart_field(" vb=", mm_per_s(tm.v_b));
        uart_field(" lit_min=", (int32_t)(tm.min_lit_mv == 0xFFFFFFFFu ? 0 : tm.min_lit_mv));
        uart_field(" weak=", (int32_t)(popcount16(tm.weak) + popcount16(tm.weak >> 16)));
        uart_field(" fault=", (int32_t)tm.fault);
        uart_puts("\r\n");
    }
}

static float head_speed(uint32_t dark, uint32_t touched)
{
    if (touched || tm.fault) {
        return contour_cal.v_retract;
    }
    return dark ? contour_cal.v_out : -contour_cal.v_in;
}

static void end_of_scan(void)
{
    tm.scan_count++;
    tm.dark = dark_acc;
    tm.weak = weak_acc;
    tm.min_lit_mv = min_lit_acc;
    dark_acc = weak_acc = 0;
    min_lit_acc = 0xFFFFFFFFu;

    uint32_t inputs = GPIO_IDR(GPIOC_BASE);
    uint32_t touch_a = (inputs >> 3) & 1u, touch_b = (inputs >> 4) & 1u;
    if ((touch_a || touch_b) && !tm.fault) {
        tm.fault = 1u;
        GPIO_BSRR(GPIOA_BASE) = (1u << 5);
        uart_puts(touch_a ? "touch A: retract\r\n" : "touch B: retract\r\n");
    }
    tm.v_a = head_speed(tm.dark & 0xFFFFu, touch_a);
    tm.v_b = head_speed(tm.dark >> 16, touch_b);
    console();
}

void TIM2_IRQHandler(void)
{
    TIM2_SR = 0;
    /* Sample the beam selected one slot ago. */
    uint32_t beam = tm.beam;
    uint32_t lit = (GPIO_IDR(GPIOC_BASE) >> 2) & 1u;
    uint32_t mv = adc_read(1) * VREF_MV / 4095u;
    if (!lit) {
        dark_acc |= 1u << beam;
    } else {
        if (mv < contour_cal.weak_mv) {
            weak_acc |= 1u << beam;
        }
        if (mv < min_lit_acc) {
            min_lit_acc = mv;
        }
    }
    uint32_t next = beam + 1u;
    if (next == BEAMS) {
        next = 0;
        end_of_scan();
    }
    GPIO_ODR(GPIOB_BASE) = next | EMITTER_ON;
    tm.beam = next;
}

static void clock_80mhz(void)
{
    FLASH_ACR = (FLASH_ACR & ~7u) | 4u;
    RCC_CR |= (1u << 8);                    /* HSION */
    while ((RCC_CR & (1u << 10)) == 0) {
    }
    RCC_CR &= ~(1u << 24);
    while (RCC_CR & (1u << 25)) {
    }
    RCC_PLLCFGR = (2u << 0) | (0u << 4) | (10u << 8) | (1u << 24);
    RCC_CR |= (1u << 24);                   /* PLLON */
    while ((RCC_CR & (1u << 25)) == 0) {
    }
    RCC_CFGR = (RCC_CFGR & ~3u) | 3u;       /* SW = PLL */
    while (((RCC_CFGR >> 2) & 3u) != 3u) {
    }
}

static void adc_init(void)
{
    RCC_CCIPR |= (3u << 28);
    ADC_CCR = (1u << 16);
    ADC1_CR &= ~(1u << 29);
    ADC1_CR |= (1u << 28);                  /* ADVREGEN */
    for (volatile uint32_t i = 0; i < 2000u; i++) {
    }
    ADC1_CR |= (1u << 31);                  /* ADCAL */
    while (ADC1_CR & (1u << 31)) {
    }
    ADC1_SMPR1 = (2u << 3);                 /* 12.5 cycles on IN1: fast scan */
    ADC1_ISR = 1u;
    ADC1_CR |= 1u;                          /* ADEN */
    while ((ADC1_ISR & 1u) == 0) {
    }
}

extern const void *const vectors[];

int main(void)
{
    SCB_VTOR = (uint32_t)vectors;
    clock_80mhz();
    RCC_AHB2ENR |= (1u << 0) | (1u << 1) | (1u << 2) | (1u << 13);  /* GPIOA, B, C, ADC */
    RCC_APB1ENR1 |= 1u | (1u << 17);                                /* TIM2, USART2 */

    /* PA5 fault LED (off); PA2/PA3 USART2 (AF7) */
    GPIO_BSRR(GPIOA_BASE) = (1u << (5 + 16));
    GPIO_MODER(GPIOA_BASE) = (GPIO_MODER(GPIOA_BASE) & ~((3u << 10) | (3u << 4) | (3u << 6)))
                             | (1u << 10) | (2u << 4) | (2u << 6);
    GPIO_AFRL(GPIOA_BASE) = (GPIO_AFRL(GPIOA_BASE) & ~(0xFFu << 8)) | (7u << 8) | (7u << 12);
    USART2_BRR = 80000000u / 115200u;
    USART2_CR1 = (1u << 3) | (1u << 2) | 1u;
    uart_puts("body-contour-l476 ready\r\n");

    /* PB0..PB5 outputs: beam 0 selected, emitters on */
    GPIO_ODR(GPIOB_BASE) = EMITTER_ON;
    GPIO_MODER(GPIOB_BASE) = (GPIO_MODER(GPIOB_BASE) & ~0xFFFu) | 0x555u;
    tm.beam = 0;
    /* PC0 analog to the ADC; PC2..PC4 digital inputs */
    GPIO_MODER(GPIOC_BASE) = (GPIO_MODER(GPIOC_BASE) & ~((3u << 4) | (3u << 6) | (3u << 8))) | (3u << 0);
    GPIO_ASCR(GPIOC_BASE) |= 1u;
    adc_init();

    TIM2_PSC = 79u;                         /* 1 MHz */
    TIM2_ARR = SLOT_US - 1u;
    TIM2_DIER = 1u;
    TIM2_EGR = 1u;
    TIM2_SR = 0;
    NVIC_ISER0 = (1u << TIM2_IRQ);
    TIM2_CR1 = 1u;
    __asm volatile ("dsb\n\tisb" ::: "memory");
    for (;;) {
        __asm volatile ("wfi");
    }
}
