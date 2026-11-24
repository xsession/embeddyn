/*
 * Bare-metal STM32L476 RC-servo controller for the Embeddyn/Renode
 * co-simulation example (examples/mechatronics/rc-servo-l476.yaml).
 *
 *   PA0  TIM2_CH1 (AF1)  -> servo signal: 50 Hz, 1500 us = centre,
 *                           500..2500 us = -90..+90 deg
 *   PC0  ADC1_IN1        <- command potentiometer (0..3.3 V = -90..+90 deg)
 *   PC1  ADC1_IN2        <- servo feedback potentiometer (analog-feedback servo)
 *   PA5  GPIO out        -> fault LED (LD2 on a Nucleo-L476RG)
 *   PA2/PA3 USART2 (AF7) -> 115200 8N1 console: telemetry every 100 ms;
 *                           'R' clears a latched fault, '?' prints the calibration
 *   PA11/PA12 CAN1 (AF9)  -> 500 kbit/s: status frame 0x101 every 100 ms
 *                           (setpoint, feedback, trim in 0.01 deg, pulse in us,
 *                           fault); frame 0x200 with data[0] = 1 clears a fault.
 *                           Skipped when the platform has no bxCAN.
 *
 * Clock: HSI16 -> PLL -> 80 MHz SYSCLK. TIM2 counts at 1 MHz (PSC = 79) and
 * runs one 20 ms servo frame per period; its update interrupt runs the
 * frame logic, the core sleeps (WFI) in between.
 *
 * Per frame:
 *   1. command = ADC IN1 -> target angle, limited to +-soft_limit
 *   2. slew-rate limit (servo_cal.slew_deg_s)
 *   3. outer trim loop: the servo's own P loop droops under load and has a
 *      dead band; an integrator on (setpoint - feedback) adds a small pulse
 *      correction, active only while set-point and servo are at rest
 *   4. stall/fault monitor: |setpoint - feedback| > fault_deg for longer than
 *      fault_frames -> stop the pulses (servo goes limp), LED on, latched
 *
 * Calibration lives in `servo_cal` at the start of RAM (.cal section, fixed
 * address 0x20000000), so tests and tuning jobs can change it over the Renode
 * system bus without rebuilding.
 */
#include <stdint.h>

#define REG(addr) (*(volatile uint32_t *)(addr))

/* RCC (RM0351 section 6.4) */
#define RCC_BASE      0x40021000u
#define RCC_CR        REG(RCC_BASE + 0x00u)
#define RCC_CFGR      REG(RCC_BASE + 0x08u)
#define RCC_PLLCFGR   REG(RCC_BASE + 0x0Cu)
#define RCC_AHB2ENR   REG(RCC_BASE + 0x4Cu)
#define RCC_APB1ENR1  REG(RCC_BASE + 0x58u)
#define RCC_CCIPR     REG(RCC_BASE + 0x88u)
#define FLASH_ACR     REG(0x40022000u)

/* GPIO */
#define GPIOA_BASE    0x48000000u
#define GPIOC_BASE    0x48000800u
#define GPIO_MODER(b) REG((b) + 0x00u)
#define GPIO_ODR(b)   REG((b) + 0x14u)
#define GPIO_BSRR(b)  REG((b) + 0x18u)
#define GPIO_AFRL(b)  REG((b) + 0x20u)
#define GPIO_ASCR(b)  REG((b) + 0x2Cu)

/* TIM2 (32-bit general-purpose timer) */
#define TIM2_BASE     0x40000000u
#define TIM2_CR1      REG(TIM2_BASE + 0x00u)
#define TIM2_DIER     REG(TIM2_BASE + 0x0Cu)
#define TIM2_SR       REG(TIM2_BASE + 0x10u)
#define TIM2_EGR      REG(TIM2_BASE + 0x14u)
#define TIM2_CCMR1    REG(TIM2_BASE + 0x18u)
#define TIM2_CCER     REG(TIM2_BASE + 0x20u)
#define TIM2_PSC      REG(TIM2_BASE + 0x28u)
#define TIM2_ARR      REG(TIM2_BASE + 0x2Cu)
#define TIM2_CCR1     REG(TIM2_BASE + 0x34u)

/* ADC1 (RM0351 section 18.7), common registers at +0x300 */
#define ADC1_BASE     0x50040000u
#define ADC1_ISR      REG(ADC1_BASE + 0x00u)
#define ADC1_CR       REG(ADC1_BASE + 0x08u)
#define ADC1_SMPR1    REG(ADC1_BASE + 0x14u)
#define ADC1_SQR1     REG(ADC1_BASE + 0x30u)
#define ADC1_DR       REG(ADC1_BASE + 0x40u)
#define ADC_CCR       REG(ADC1_BASE + 0x308u)

/* USART2 (RM0351 section 40.8) */
#define USART2_BASE   0x40004400u
#define USART2_CR1    REG(USART2_BASE + 0x00u)
#define USART2_BRR    REG(USART2_BASE + 0x0Cu)
#define USART2_ISR    REG(USART2_BASE + 0x1Cu)
#define USART2_RDR    REG(USART2_BASE + 0x24u)
#define USART2_TDR    REG(USART2_BASE + 0x28u)
#define USART_TXE     (1u << 7)
#define USART_RXNE    (1u << 5)

/* bxCAN1 (RM0351 section 55.9) */
#define CAN1_BASE     0x40006400u
#define CAN_MCR       REG(CAN1_BASE + 0x000u)
#define CAN_MSR       REG(CAN1_BASE + 0x004u)
#define CAN_TSR       REG(CAN1_BASE + 0x008u)
#define CAN_RF0R      REG(CAN1_BASE + 0x00Cu)
#define CAN_BTR       REG(CAN1_BASE + 0x01Cu)
#define CAN_TI0R      REG(CAN1_BASE + 0x180u)
#define CAN_TDT0R     REG(CAN1_BASE + 0x184u)
#define CAN_TDL0R     REG(CAN1_BASE + 0x188u)
#define CAN_TDH0R     REG(CAN1_BASE + 0x18Cu)
#define CAN_RI0R      REG(CAN1_BASE + 0x1B0u)
#define CAN_RDT0R     REG(CAN1_BASE + 0x1B4u)
#define CAN_RDL0R     REG(CAN1_BASE + 0x1B8u)
#define CAN_FMR       REG(CAN1_BASE + 0x200u)
#define CAN_FM1R      REG(CAN1_BASE + 0x204u)
#define CAN_FS1R      REG(CAN1_BASE + 0x20Cu)
#define CAN_FFA1R     REG(CAN1_BASE + 0x214u)
#define CAN_FA1R      REG(CAN1_BASE + 0x21Cu)
#define CAN_F0R1      REG(CAN1_BASE + 0x240u)
#define CAN_F0R2      REG(CAN1_BASE + 0x244u)
#define CAN_STATUS_ID 0x101u
#define CAN_CMD_ID    0x200u

#define NVIC_ISER0    REG(0xE000E100u)
#define SCB_VTOR      REG(0xE000ED08u)
#define TIM2_IRQ      28u

#define FRAME_US      20000u        /* 50 Hz servo frame */
#define DT            0.02f
#define ADC_FULL      4095.0f
#define VREF          3.3f

struct servo_cal {
    uint32_t magic;          /* 0x5356524F "SVRO" */
    float pulse_center_us;   /* pulse for 0 deg */
    float us_per_deg;        /* 2000 us / 180 deg */
    float soft_limit_deg;    /* command clamp */
    float slew_deg_s;        /* set-point slew-rate limit */
    float fb_volts_at_zero;  /* feedback pot: volts at 0 deg */
    float fb_volts_per_deg;  /* feedback pot slope */
    float trim_ki;           /* outer trim integrator gain, 1/s (0 = off) */
    float trim_limit_deg;    /* trim clamp */
    float fault_deg;         /* stall threshold */
    uint32_t fault_frames;   /* frames above threshold before the fault trips */
};

__attribute__((section(".cal"), used))
volatile struct servo_cal servo_cal = {
    0x5356524Fu, 1500.0f, 2000.0f / 180.0f, 80.0f, 400.0f,
    1.65f, 2.7f / 180.0f, 2.0f, 6.0f, 25.0f, 25u,
};

/* Telemetry, readable over the system bus at a fixed address right after
 * servo_cal (.telemetry section, 0x2000002C): */
struct servo_telemetry {
    uint32_t frame_count;    /* +0x00 */
    uint32_t fault_latched;  /* +0x04 */
    uint32_t stall_frames;   /* +0x08 */
    float command_deg;       /* +0x0C */
    float setpoint_deg;      /* +0x10 */
    float feedback_deg;      /* +0x14 */
    float trim_deg;          /* +0x18 */
    uint32_t pulse_us;       /* +0x1C */
};

__attribute__((section(".telemetry"), used))
volatile struct servo_telemetry tm;

static float previous_fb;

static uint32_t adc_read(uint32_t channel)
{
    /* ADSTART clears when the previous sequence has ended (EOS). Renode's
     * model ends a sequence one sync point after DR is read and ignores a
     * start issued before that, so wait for it as hardware-safe code does. */
    while (ADC1_CR & (1u << 2)) {
    }
    ADC1_SQR1 = channel << 6;      /* L = 0 (one conversion), SQ1 = channel */
    ADC1_ISR = (1u << 2) | (1u << 3);  /* clear EOC, EOS */
    ADC1_CR |= (1u << 2);          /* ADSTART */
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

static int32_t centi(float v)
{
    return (int32_t)(v * 100.0f + (v < 0 ? -0.5f : 0.5f));
}

/* Console: one telemetry line every 5 frames (100 ms), commands from the host. */
static void console(void)
{
    while (USART2_ISR & USART_RXNE) {
        char c = (char)USART2_RDR;
        if (c == 'R' && tm.fault_latched) {
            tm.fault_latched = 0;
            tm.stall_frames = 0;
            GPIO_BSRR(GPIOA_BASE) = (1u << (5 + 16));        /* LED off */
            uart_puts("fault cleared\r\n");
        } else if (c == '?') {
            uart_field("cal trim_ki=", centi(servo_cal.trim_ki));
            uart_field(" fault_deg=", centi(servo_cal.fault_deg));
            uart_puts("\r\n");
        }
    }
    if (tm.frame_count % 5u == 0u) {
        uart_field("t=", (int32_t)(tm.frame_count * (FRAME_US / 1000u)));
        uart_field(" sp=", centi(tm.setpoint_deg));
        uart_field(" fb=", centi(tm.feedback_deg));
        uart_field(" trim=", centi(tm.trim_deg));
        uart_field(" pulse=", (int32_t)tm.pulse_us);
        uart_field(" fault=", (int32_t)tm.fault_latched);
        uart_puts("\r\n");
    }
}

static uint32_t can_ready;

/* bxCAN: accept-all filter into FIFO0, 500 kbit/s at 80 MHz. Bounded waits, so
 * a platform without the controller (unmapped, reads 0) just skips CAN. */
static void can_init(void)
{
    CAN_MCR = 1u;                                           /* INRQ, leave sleep */
    for (uint32_t i = 0; (CAN_MSR & 1u) == 0; i++) {        /* INAK */
        if (i > 10000u) {
            return;
        }
    }
    CAN_BTR = (1u << 24) | (6u << 20) | (7u << 16) | 9u;   /* SJW 2, TS2 7, TS1 8, BRP 10 */
    CAN_FMR |= 1u;                                          /* filter init */
    CAN_FM1R &= ~1u;                                        /* bank 0: mask mode */
    CAN_FS1R |= 1u;                                         /* 32-bit */
    CAN_FFA1R &= ~1u;                                       /* -> FIFO0 */
    CAN_F0R1 = 0;
    CAN_F0R2 = 0;                                           /* mask 0: accept all */
    CAN_FA1R |= 1u;
    CAN_FMR &= ~1u;
    CAN_MCR &= ~1u;                                         /* normal mode */
    for (uint32_t i = 0; CAN_MSR & 1u; i++) {
        if (i > 10000u) {
            return;
        }
    }
    can_ready = 1u;
}

static void can_send(uint32_t id, const uint8_t *data, uint32_t length)
{
    if (!can_ready || (CAN_TSR & (1u << 26)) == 0) {        /* TME0: mailbox 0 empty */
        return;
    }
    uint32_t low = 0, high = 0;
    for (uint32_t i = 0; i < length && i < 8u; i++) {
        if (i < 4u) {
            low |= (uint32_t)data[i] << (8u * i);
        } else {
            high |= (uint32_t)data[i] << (8u * (i - 4u));
        }
    }
    CAN_TDT0R = length;
    CAN_TDL0R = low;
    CAN_TDH0R = high;
    CAN_TI0R = (id << 21) | 1u;                             /* standard ID, TXRQ */
}

static void put16(uint8_t *p, int32_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)((v >> 8) & 0xFF);
}

/* Status frame every 5 frames (100 ms); command frames are polled every frame. */
static void can_poll(void)
{
    if (!can_ready) {
        return;
    }
    while (CAN_RF0R & 3u) {                                 /* FMP0: messages pending */
        uint32_t id = CAN_RI0R >> 21;
        uint32_t length = CAN_RDT0R & 0xFu;
        uint32_t data = CAN_RDL0R;
        CAN_RF0R |= (1u << 5);                              /* RFOM0: release */
        if (id == CAN_CMD_ID && length >= 1u && (data & 0xFFu) == 1u && tm.fault_latched) {
            tm.fault_latched = 0;
            tm.stall_frames = 0;
            GPIO_BSRR(GPIOA_BASE) = (1u << (5 + 16));
            uart_puts("fault cleared (CAN)\r\n");
        }
    }
    if (tm.frame_count % 5u == 0u) {
        uint8_t frame[8];
        put16(&frame[0], centi(tm.setpoint_deg));
        put16(&frame[2], centi(tm.feedback_deg));
        put16(&frame[4], (int32_t)tm.pulse_us);
        frame[6] = (uint8_t)(int8_t)(tm.trim_deg * 10.0f);   /* 0.1 deg */
        frame[7] = (uint8_t)tm.fault_latched;
        can_send(CAN_STATUS_ID, frame, 8u);
    }
}

static float clampf(float v, float lo, float hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

static float absf(float v)
{
    return v < 0.0f ? -v : v;
}

static void frame(void)
{
    TIM2_SR = 0;                   /* clear UIF */
    tm.frame_count++;

    float limit = servo_cal.soft_limit_deg;
    float cmd = ((float)adc_read(1) / ADC_FULL - 0.5f) * 180.0f;
    cmd = clampf(cmd, -limit, limit);
    tm.command_deg = cmd;

    float fb_volts = (float)adc_read(2) * (VREF / ADC_FULL);
    float fb = (fb_volts - servo_cal.fb_volts_at_zero) / servo_cal.fb_volts_per_deg;
    tm.feedback_deg = fb;

    /* Slew-rate limited set-point. */
    float sp = tm.setpoint_deg;
    float max_step = servo_cal.slew_deg_s * DT;
    float delta = clampf(cmd - sp, -max_step, max_step);
    sp += delta;
    tm.setpoint_deg = sp;
    /* Outer trim: integrate only while the set-point rests, the servo has
     * stopped (< 25 deg/s) and is close to it, so the trim corrects droop
     * and dead band but never winds up during a move. */
    int at_rest = absf(cmd - sp) < 1e-3f && absf(fb - previous_fb) < 25.0f * DT;
    previous_fb = fb;
    float error = sp - fb;
    float trim = tm.trim_deg;
    if (at_rest && absf(error) < 5.0f) {
        trim = clampf(trim + servo_cal.trim_ki * error * DT,
                      -servo_cal.trim_limit_deg, servo_cal.trim_limit_deg);
    }
    tm.trim_deg = trim;

    /* Stall monitor (latched). */
    if (absf(error) > servo_cal.fault_deg) {
        if (++tm.stall_frames >= servo_cal.fault_frames) {
            tm.fault_latched = 1u;
        }
    } else {
        tm.stall_frames = 0;
    }

    uint32_t pulse;
    if (tm.fault_latched) {
        pulse = 0;                 /* no pulses: the servo releases */
        GPIO_BSRR(GPIOA_BASE) = (1u << 5);
    } else {
        float us = servo_cal.pulse_center_us + (sp + trim) * servo_cal.us_per_deg;
        pulse = (uint32_t)(clampf(us, 500.0f, 2500.0f) + 0.5f);
    }
    TIM2_CCR1 = pulse;             /* preloaded: takes effect next frame */
    tm.pulse_us = pulse;
}

void TIM2_IRQHandler(void)
{
    frame();
    console();
    can_poll();
}

static void clock_80mhz(void)
{
    FLASH_ACR = (FLASH_ACR & ~7u) | 4u;     /* 4 wait states for 80 MHz */
    RCC_CR |= (1u << 8);                    /* HSION */
    while ((RCC_CR & (1u << 10)) == 0) {    /* HSIRDY */
    }
    RCC_CR &= ~(1u << 24);                  /* PLLOFF */
    while (RCC_CR & (1u << 25)) {
    }
    /* HSI16 / M(1) * N(10) = 160 MHz VCO, / R(2) = 80 MHz */
    RCC_PLLCFGR = (2u << 0) | (0u << 4) | (10u << 8) | (1u << 24);
    RCC_CR |= (1u << 24);                   /* PLLON */
    while ((RCC_CR & (1u << 25)) == 0) {    /* PLLRDY */
    }
    RCC_CFGR = (RCC_CFGR & ~3u) | 3u;       /* SW = PLL */
    while (((RCC_CFGR >> 2) & 3u) != 3u) {  /* SWS = PLL */
    }
}

static void adc_init(void)
{
    RCC_CCIPR |= (3u << 28);                /* ADCSEL = SYSCLK */
    ADC_CCR = (1u << 16);                   /* CKMODE = HCLK/1 */
    ADC1_CR &= ~(1u << 29);                 /* leave deep power-down */
    ADC1_CR |= (1u << 28);                  /* ADVREGEN */
    for (volatile uint32_t i = 0; i < 2000u; i++) {
    }                                       /* tADCVREG_STUP = 20 us */
    ADC1_CR |= (1u << 31);                  /* ADCAL (single-ended) */
    while (ADC1_CR & (1u << 31)) {
    }
    ADC1_SMPR1 = (5u << 3) | (5u << 6);     /* 92.5 cycles on IN1, IN2 */
    ADC1_ISR = 1u;                          /* clear ADRDY */
    ADC1_CR |= 1u;                          /* ADEN */
    while ((ADC1_ISR & 1u) == 0) {
    }
}

extern const void *const vectors[];

int main(void)
{
    SCB_VTOR = (uint32_t)vectors;
    clock_80mhz();
    RCC_AHB2ENR |= (1u << 0) | (1u << 2) | (1u << 13);  /* GPIOA, GPIOC, ADC */
    RCC_APB1ENR1 |= 1u | (1u << 17);                    /* TIM2, USART2 */

    /* PA0 = AF1 (TIM2_CH1), PA5 = output (fault LED, off) */
    GPIO_BSRR(GPIOA_BASE) = (1u << (5 + 16));
    GPIO_MODER(GPIOA_BASE) = (GPIO_MODER(GPIOA_BASE) & ~((3u << 0) | (3u << 10))) | (2u << 0) | (1u << 10);
    GPIO_AFRL(GPIOA_BASE) = (GPIO_AFRL(GPIOA_BASE) & ~0xFu) | 1u;
    /* PA2 = USART2_TX, PA3 = USART2_RX (AF7), 115200 8N1 */
    GPIO_MODER(GPIOA_BASE) = (GPIO_MODER(GPIOA_BASE) & ~((3u << 4) | (3u << 6))) | (2u << 4) | (2u << 6);
    GPIO_AFRL(GPIOA_BASE) = (GPIO_AFRL(GPIOA_BASE) & ~(0xFFu << 8)) | (7u << 8) | (7u << 12);
    USART2_BRR = 80000000u / 115200u;
    USART2_CR1 = (1u << 3) | (1u << 2) | 1u;           /* TE, RE, UE */
    uart_puts("rc-servo-l476 ready\r\n");
    /* PC0, PC1 analog (reset default) and connected to the ADC */
    GPIO_MODER(GPIOC_BASE) |= (3u << 0) | (3u << 2);
    GPIO_ASCR(GPIOC_BASE) |= 3u;

    adc_init();
    RCC_APB1ENR1 |= (1u << 25);                             /* CAN1 */
    can_init();

    /* Start at the commanded position so the servo doesn't jump at power-up. */
    float cmd = ((float)adc_read(1) / ADC_FULL - 0.5f) * 180.0f;
    tm.setpoint_deg = clampf(cmd, -servo_cal.soft_limit_deg, servo_cal.soft_limit_deg);
    previous_fb = ((float)adc_read(2) * (VREF / ADC_FULL) - servo_cal.fb_volts_at_zero) / servo_cal.fb_volts_per_deg;
    tm.pulse_us = (uint32_t)(servo_cal.pulse_center_us + tm.setpoint_deg * servo_cal.us_per_deg + 0.5f);

    TIM2_PSC = 79u;                         /* 80 MHz -> 1 MHz */
    TIM2_ARR = FRAME_US - 1u;
    TIM2_CCR1 = tm.pulse_us;
    TIM2_CCMR1 = (6u << 4) | (1u << 3);     /* OC1M = PWM mode 1, OC1PE */
    TIM2_CCER = 1u;                         /* CC1E */
    TIM2_DIER = 1u;                         /* UIE */
    TIM2_EGR = 1u;                          /* UG: load PSC/ARR/CCR1 */
    TIM2_SR = 0;
    NVIC_ISER0 = (1u << TIM2_IRQ);
    TIM2_CR1 = 1u;                          /* CEN */
    /* Barrier before WFI: Renode can otherwise take a pending IRQ and run
     * the WFI in the same translation block (see motor-speed-control). */
    __asm volatile ("dsb\n\tisb" ::: "memory");
    for (;;) {
        __asm volatile ("wfi");
    }
}
