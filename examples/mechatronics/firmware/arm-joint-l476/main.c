/*
 * One robot-arm joint, position-controlled by an STM32L476 (embeddyn example).
 *
 *   PC0  ADC1_IN1        -> position command, 0..3.3 V = -range..+range rad
 *   PA6/PA7 TIM3 (AF2)   -> quadrature encoder on the joint, encoder mode 3
 *   PA0/PA1 TIM2 CH1/CH2 -> H-bridge IN1/IN2, 20 kHz PWM (sign-magnitude)
 *
 * TIM2's update interrupt (20 kHz) runs the 1 kHz control loop every 20th
 * period: encoder counts -> angle and filtered speed, command -> target, PID
 * with clamped integral -> signed duty. Calibration (`arm_cal`, 0x20000000)
 * and telemetry (`arm_tm`, right after it) sit at fixed addresses so tuning
 * jobs can change gains over the system bus without a rebuild.
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
#define GPIOC_BASE    0x48000800u
#define GPIO_MODER(b) REG((b) + 0x00u)
#define GPIO_AFRL(b)  REG((b) + 0x20u)
#define GPIO_ASCR(b)  REG((b) + 0x2Cu)
#define TIM2_BASE     0x40000000u
#define TIM3_BASE     0x40000400u
#define TIM_CR1(b)    REG((b) + 0x00u)
#define TIM_SMCR(b)   REG((b) + 0x08u)
#define TIM_DIER(b)   REG((b) + 0x0Cu)
#define TIM_SR(b)     REG((b) + 0x10u)
#define TIM_EGR(b)    REG((b) + 0x14u)
#define TIM_CCMR1(b)  REG((b) + 0x18u)
#define TIM_CCER(b)   REG((b) + 0x20u)
#define TIM_CNT(b)    REG((b) + 0x24u)
#define TIM_PSC(b)    REG((b) + 0x28u)
#define TIM_ARR(b)    REG((b) + 0x2Cu)
#define TIM_CCR1(b)   REG((b) + 0x34u)
#define TIM_CCR2(b)   REG((b) + 0x38u)
#define ADC1_BASE     0x50040000u
#define ADC1_ISR      REG(ADC1_BASE + 0x00u)
#define ADC1_CR       REG(ADC1_BASE + 0x08u)
#define ADC1_SMPR1    REG(ADC1_BASE + 0x14u)
#define ADC1_SQR1     REG(ADC1_BASE + 0x30u)
#define ADC1_DR       REG(ADC1_BASE + 0x40u)
#define ADC_CCR       REG(ADC1_BASE + 0x308u)
#define NVIC_ISER0    REG(0xE000E100u)
#define SCB_VTOR      REG(0xE000ED08u)
#define TIM2_IRQ      28u

#define PWM_ARR       3999u          /* 80 MHz / 4000 = 20 kHz */
#define LOOP_DIVIDER  20u            /* 20 kHz / 20 = 1 kHz control loop */
#define DT            0.001f
#define ADC_FULL      4095.0f

struct arm_cal {
    uint32_t magic;            /* +0x00 'ARMJ' */
    float kp;                  /* +0x04 duty per rad */
    float ki;                  /* +0x08 duty per rad*s */
    float kd;                  /* +0x0C duty per rad/s */
    float max_duty;            /* +0x10 */
    float counts_per_rad;      /* +0x14 encoder counts per joint radian */
    float command_range;       /* +0x18 rad at 0 V / 3.3 V */
    float integral_limit;      /* +0x1C rad*s */
    float speed_filter;        /* +0x20 first-order filter weight of the new sample */
};

__attribute__((section(".cal"), used))
volatile struct arm_cal arm_cal = {
    0x41524D4Au, 6.0f, 3.0f, 0.15f, 1.0f, 4096.0f * 20.0f / 6.2831853f, 1.5f, 0.3f, 0.2f,
};

struct arm_telemetry {
    uint32_t loops;            /* +0x00 */
    float target;              /* +0x04 rad */
    float angle;               /* +0x08 rad */
    float speed;               /* +0x0C rad/s */
    float duty;                /* +0x10 signed */
    float integral;            /* +0x14 rad*s */
    int32_t counts;            /* +0x18 accumulated encoder counts */
};

__attribute__((section(".telemetry"), used))
volatile struct arm_telemetry tm;

static uint16_t last_cnt;
static uint32_t divider;

static float clampf(float v, float lo, float hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

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

static void control(void)
{
    uint16_t cnt = (uint16_t)TIM_CNT(TIM3_BASE);
    int16_t delta = (int16_t)(uint16_t)(cnt - last_cnt);   /* wraps correctly */
    last_cnt = cnt;
    tm.counts += delta;
    tm.angle = (float)tm.counts / arm_cal.counts_per_rad;
    float raw_speed = (float)delta / arm_cal.counts_per_rad / DT;
    tm.speed += arm_cal.speed_filter * (raw_speed - tm.speed);

    float command = (float)adc_read(1) / ADC_FULL;
    tm.target = (command - 0.5f) * 2.0f * arm_cal.command_range;

    float error = tm.target - tm.angle;
    float u = arm_cal.kp * error + arm_cal.ki * tm.integral - arm_cal.kd * tm.speed;
    /* Anti-windup: integrate only while not saturated in the error's direction. */
    if (!((u >= arm_cal.max_duty && error > 0) || (u <= -arm_cal.max_duty && error < 0))) {
        tm.integral = clampf(tm.integral + error * DT, -arm_cal.integral_limit, arm_cal.integral_limit);
    }
    u = clampf(u, -arm_cal.max_duty, arm_cal.max_duty);
    tm.duty = u;
    uint32_t level = (uint32_t)((u < 0 ? -u : u) * (float)(PWM_ARR + 1u) + 0.5f);
    TIM_CCR1(TIM2_BASE) = u > 0 ? level : 0u;            /* IN1: forward */
    TIM_CCR2(TIM2_BASE) = u < 0 ? level : 0u;            /* IN2: reverse */
    tm.loops++;
}

void TIM2_IRQHandler(void)
{
    TIM_SR(TIM2_BASE) = 0;
    if (++divider >= LOOP_DIVIDER) {
        divider = 0;
        control();
    }
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
    RCC_AHB2ENR |= (1u << 0) | (1u << 2) | (1u << 13);      /* GPIOA, GPIOC, ADC */
    RCC_APB1ENR1 |= (1u << 0) | (1u << 1);                  /* TIM2, TIM3 */

    /* PA0/PA1 = AF1 (TIM2 CH1/CH2), PA6/PA7 = AF2 (TIM3 CH1/CH2) */
    GPIO_MODER(GPIOA_BASE) = (GPIO_MODER(GPIOA_BASE) & ~((3u << 0) | (3u << 2) | (3u << 12) | (3u << 14)))
                           | (2u << 0) | (2u << 2) | (2u << 12) | (2u << 14);
    GPIO_AFRL(GPIOA_BASE) = (GPIO_AFRL(GPIOA_BASE) & ~(0xFFu | (0xFFu << 24))) | 0x11u | (0x22u << 24);
    GPIO_MODER(GPIOC_BASE) |= (3u << 0);
    GPIO_ASCR(GPIOC_BASE) |= 1u;
    adc_init();

    /* Encoder: TI1/TI2 inputs, encoder mode 3 (both edges of both channels). */
    TIM_ARR(TIM3_BASE) = 0xFFFFu;
    TIM_CCMR1(TIM3_BASE) = (1u << 0) | (1u << 8);           /* CC1S = TI1, CC2S = TI2 */
    TIM_SMCR(TIM3_BASE) = 3u;                               /* SMS = encoder mode 3 */
    TIM_CR1(TIM3_BASE) = 1u;
    last_cnt = (uint16_t)TIM_CNT(TIM3_BASE);

    /* H-bridge PWM: 20 kHz, PWM mode 1 on CH1 and CH2, update interrupt drives the loop. */
    TIM_PSC(TIM2_BASE) = 0;
    TIM_ARR(TIM2_BASE) = PWM_ARR;
    TIM_CCR1(TIM2_BASE) = 0;
    TIM_CCR2(TIM2_BASE) = 0;
    TIM_CCMR1(TIM2_BASE) = (6u << 4) | (1u << 3) | (6u << 12) | (1u << 11);
    TIM_CCER(TIM2_BASE) = 1u | (1u << 4);
    TIM_DIER(TIM2_BASE) = 1u;
    TIM_EGR(TIM2_BASE) = 1u;
    TIM_SR(TIM2_BASE) = 0;
    NVIC_ISER0 = (1u << TIM2_IRQ);
    TIM_CR1(TIM2_BASE) = 1u | (1u << 7);                    /* CEN, ARPE */
    __asm volatile ("dsb\n\tisb" ::: "memory");
    for (;;) {
        __asm volatile ("wfi");
    }
}
