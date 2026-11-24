/*
 * One robot-arm joint, position-controlled by an STM32L476 (embeddyn example).
 *
 *   PC0  ADC1_IN1        -> position command, 0..3.3 V = -range..+range rad
 *   PA6/PA7 TIM3 (AF2)   -> quadrature encoder on the joint, encoder mode 3
 *   PA0/PA1 TIM2 CH1/CH2 -> H-bridge IN1/IN2, 20 kHz PWM (sign-magnitude)
 *   PB3/4/5 SPI1, PB6 CS -> AS5047P absolute angle sensor on the joint
 *   PB8/PB9 I2C1         -> AS6221 temperature sensor on the motor winding
 *
 * At start-up the firmware waits out the AS5047P's 10 ms power-up, then homes
 * the incremental encoder to the absolute angle. Every loop it reads the
 * absolute angle again and faults (drive off) if the encoder disagrees for
 * 20 ms. The main loop reads the motor temperature every 100 ms and derates
 * the duty limit linearly between derate_start and derate_end.
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
#define RCC_APB2ENR   REG(RCC_BASE + 0x60u)
#define GPIOB_BASE    0x48000400u
#define GPIO_AFRH(b)  REG((b) + 0x24u)
#define GPIO_BSRR(b)  REG((b) + 0x18u)
#define SPI1_BASE     0x40013000u
#define SPI1_CR1      REG(SPI1_BASE + 0x00u)
#define SPI1_CR2      REG(SPI1_BASE + 0x04u)
#define SPI1_SR       REG(SPI1_BASE + 0x08u)
#define SPI1_DR8      (*(volatile uint8_t *)(SPI1_BASE + 0x0Cu))
#define I2C1_BASE     0x40005400u
#define I2C1_CR1      REG(I2C1_BASE + 0x00u)
#define I2C1_CR2      REG(I2C1_BASE + 0x04u)
#define I2C1_TIMINGR  REG(I2C1_BASE + 0x10u)
#define I2C1_ISR      REG(I2C1_BASE + 0x18u)
#define I2C1_ICR      REG(I2C1_BASE + 0x1Cu)
#define I2C1_RXDR     REG(I2C1_BASE + 0x24u)
#define I2C1_TXDR     REG(I2C1_BASE + 0x28u)
#define AS5047_READ_ANGLECOM 0xFFFFu   /* PARC=1, R=1, ADDR=0x3FFF */
#define AS6221_ADDR   0x48u
#define FAULT_ENCODER_MISMATCH (1u << 0)
#define FAULT_OVER_TEMPERATURE (1u << 1)
#define TWO_PI        6.2831853f

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
    float abs_zero;            /* +0x24 AS5047P reading (rad) at joint angle 0 */
    float abs_tolerance;       /* +0x28 rad, encoder vs absolute sensor */
    float derate_start;        /* +0x2C degC: full duty below */
    float derate_end;          /* +0x30 degC: zero duty above */
    uint32_t features;         /* +0x34 bit0 home to the absolute sensor, bit1 cross-check, bit2 derate */
};

__attribute__((section(".cal"), used))
volatile struct arm_cal arm_cal = {
    0x41524D4Au, 6.0f, 3.0f, 0.15f, 1.0f, 4096.0f * 20.0f / 6.2831853f, 1.5f, 0.3f, 0.2f,
    0.0f, 0.05f, 70.0f, 110.0f, 0x7u,
};

struct arm_telemetry {
    uint32_t loops;            /* +0x00 */
    float target;              /* +0x04 rad */
    float angle;               /* +0x08 rad */
    float speed;               /* +0x0C rad/s */
    float duty;                /* +0x10 signed */
    float integral;            /* +0x14 rad*s */
    int32_t counts;            /* +0x18 accumulated encoder counts */
    float abs_angle;           /* +0x1C rad, AS5047P (one loop old, the sensor pipelines) */
    float temperature;         /* +0x20 degC, AS6221 */
    float duty_limit;          /* +0x24 after derating */
    uint32_t fault;            /* +0x28 FAULT_* bits, latched */
    uint32_t spi_errors;       /* +0x2C AS5047P answers with EF set or bad parity */
    uint32_t i2c_errors;       /* +0x30 failed temperature reads */
    float home_angle;          /* +0x34 rad, absolute angle the encoder was homed to */
};

__attribute__((section(".telemetry"), used))
volatile struct arm_telemetry tm;

static uint16_t last_cnt;
static uint32_t divider;
static uint32_t mismatch_loops;
static float last_angle;

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

/* ---------------------------------------------------------------- AS5047P (SPI1, mode 1) */
static uint8_t spi_byte(uint8_t out)
{
    while ((SPI1_SR & (1u << 1)) == 0) {      /* TXE */
    }
    SPI1_DR8 = out;
    while ((SPI1_SR & 1u) == 0) {             /* RXNE */
    }
    return SPI1_DR8;
}

static int even_parity(uint16_t word)
{
    word ^= word >> 8;
    word ^= word >> 4;
    word ^= word >> 2;
    word ^= word >> 1;
    return (word & 1u) == 0;
}

/* One 16-bit frame; returns the answer to the previous frame's command. */
static uint16_t as5047_frame(uint16_t command)
{
    GPIO_BSRR(GPIOB_BASE) = 1u << (16 + 6);   /* CS low */
    uint16_t answer = (uint16_t)(spi_byte((uint8_t)(command >> 8)) << 8);
    answer |= spi_byte((uint8_t)command);
    GPIO_BSRR(GPIOB_BASE) = 1u << 6;          /* CS high */
    return answer;
}

/* Absolute joint angle in (-pi, pi]; returns 0 on an error answer. */
static int as5047_angle(uint16_t answer, float *angle)
{
    if (!even_parity(answer) || (answer & 0x4000u)) {
        tm.spi_errors++;
        return 0;
    }
    float a = (float)(answer & 0x3FFFu) * (TWO_PI / 16384.0f) - arm_cal.abs_zero;
    while (a > 3.14159265f) a -= TWO_PI;
    while (a <= -3.14159265f) a += TWO_PI;
    *angle = a;
    return 1;
}

static float wrap_pi(float a)
{
    while (a > 3.14159265f) a -= TWO_PI;
    while (a <= -3.14159265f) a += TWO_PI;
    return a;
}

/* ---------------------------------------------------------------- AS6221 (I2C1) */
static int i2c_wait(uint32_t flag)
{
    for (uint32_t i = 0; i < 20000u; i++) {
        uint32_t isr = I2C1_ISR;
        if (isr & flag) {
            return 1;
        }
        if (isr & (1u << 4)) {                /* NACKF */
            I2C1_ICR = (1u << 4) | (1u << 5);
            return 0;
        }
    }
    return 0;
}

static int as6221_read(float *celsius)
{
    I2C1_CR2 = (AS6221_ADDR << 1) | (1u << 16) | (1u << 13);              /* write 1 byte, START */
    if (!i2c_wait(1u << 1)) return 0;                                       /* TXIS */
    I2C1_TXDR = 0x00u;                                                      /* register: TVAL */
    if (!i2c_wait(1u << 6)) return 0;                                       /* TC */
    I2C1_CR2 = (AS6221_ADDR << 1) | (1u << 10) | (2u << 16) | (1u << 25) | (1u << 13);  /* read 2, AUTOEND */
    if (!i2c_wait(1u << 2)) return 0;                                       /* RXNE */
    uint32_t hi = I2C1_RXDR & 0xFFu;
    if (!i2c_wait(1u << 2)) return 0;
    uint32_t lo = I2C1_RXDR & 0xFFu;
    if (!i2c_wait(1u << 5)) return 0;                                       /* STOPF */
    I2C1_ICR = 1u << 5;
    *celsius = (float)(int16_t)((hi << 8) | lo) / 128.0f;
    return 1;
}

static void update_duty_limit(void)
{
    float limit = arm_cal.max_duty;
    if (arm_cal.features & 4u) {
        float span = arm_cal.derate_end - arm_cal.derate_start;
        float scale = span > 0.0f ? (arm_cal.derate_end - tm.temperature) / span : 1.0f;
        limit *= clampf(scale, 0.0f, 1.0f);
        if (tm.temperature >= arm_cal.derate_end) {
            tm.fault |= FAULT_OVER_TEMPERATURE;
        }
    }
    tm.duty_limit = limit;
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

    /* The answer belongs to the previous loop's command: compare it with the previous encoder angle. */
    float abs_angle;
    if (as5047_angle(as5047_frame(AS5047_READ_ANGLECOM), &abs_angle)) {
        tm.abs_angle = abs_angle;
        if (arm_cal.features & 2u) {
            float diff = wrap_pi(abs_angle - last_angle);
            mismatch_loops = (diff > arm_cal.abs_tolerance || diff < -arm_cal.abs_tolerance) ? mismatch_loops + 1u : 0u;
            if (mismatch_loops >= 20u) {
                tm.fault |= FAULT_ENCODER_MISMATCH;
            }
        }
    }
    last_angle = tm.angle;

    float command = (float)adc_read(1) / ADC_FULL;
    tm.target = (command - 0.5f) * 2.0f * arm_cal.command_range;

    float error = tm.target - tm.angle;
    float u = arm_cal.kp * error + arm_cal.ki * tm.integral - arm_cal.kd * tm.speed;
    /* Anti-windup: integrate only while not saturated in the error's direction. */
    if (!((u >= tm.duty_limit && error > 0) || (u <= -tm.duty_limit && error < 0))) {
        tm.integral = clampf(tm.integral + error * DT, -arm_cal.integral_limit, arm_cal.integral_limit);
    }
    float limit = (tm.fault & FAULT_ENCODER_MISMATCH) ? 0.0f : tm.duty_limit;
    u = clampf(u, -limit, limit);
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
    RCC_APB1ENR1 |= (1u << 0) | (1u << 1) | (1u << 21);     /* TIM2, TIM3, I2C1 */
    RCC_APB2ENR |= (1u << 12);                              /* SPI1 */
    RCC_AHB2ENR |= (1u << 1);                               /* GPIOB */

    /* PA0/PA1 = AF1 (TIM2 CH1/CH2), PA6/PA7 = AF2 (TIM3 CH1/CH2) */
    GPIO_MODER(GPIOA_BASE) = (GPIO_MODER(GPIOA_BASE) & ~((3u << 0) | (3u << 2) | (3u << 12) | (3u << 14)))
                           | (2u << 0) | (2u << 2) | (2u << 12) | (2u << 14);
    GPIO_AFRL(GPIOA_BASE) = (GPIO_AFRL(GPIOA_BASE) & ~(0xFFu | (0xFFu << 24))) | 0x11u | (0x22u << 24);
    GPIO_MODER(GPIOC_BASE) |= (3u << 0);
    GPIO_ASCR(GPIOC_BASE) |= 1u;
    /* PB3/4/5 = AF5 (SPI1), PB6 = output (CS, idle high), PB8/PB9 = AF4 (I2C1) */
    GPIO_BSRR(GPIOB_BASE) = 1u << 6;
    GPIO_MODER(GPIOB_BASE) = (GPIO_MODER(GPIOB_BASE) & ~(0x3FFu << 6 | 0xFu << 16))
                           | (2u << 6) | (2u << 8) | (2u << 10) | (1u << 12) | (2u << 16) | (2u << 18);
    GPIO_AFRL(GPIOB_BASE) = (GPIO_AFRL(GPIOB_BASE) & ~(0xFFFu << 12)) | (0x555u << 12);
    GPIO_AFRH(GPIOB_BASE) = (GPIO_AFRH(GPIOB_BASE) & ~0xFFu) | 0x44u;
    adc_init();

    /* SPI1: master, mode 1 (CPHA), 80 MHz / 16 = 5 MHz, software NSS, 8-bit frames with RXNE per byte. */
    SPI1_CR1 = (1u << 0) | (1u << 2) | (3u << 3) | (1u << 8) | (1u << 9);
    SPI1_CR2 = (7u << 8) | (1u << 12);
    SPI1_CR1 |= 1u << 6;                                    /* SPE */

    /* I2C1: 400 kHz timing for an 80 MHz kernel clock (RM0351 example), enable. */
    I2C1_TIMINGR = 0x00702991u;
    I2C1_CR1 = 1u;
    tm.duty_limit = arm_cal.max_duty;
    tm.temperature = 25.0f;

    /* Encoder: TI1/TI2 inputs, encoder mode 3 (both edges of both channels). */
    TIM_ARR(TIM3_BASE) = 0xFFFFu;
    TIM_CCMR1(TIM3_BASE) = (1u << 0) | (1u << 8);           /* CC1S = TI1, CC2S = TI2 */
    TIM_SMCR(TIM3_BASE) = 3u;                               /* SMS = encoder mode 3 */
    TIM_CR1(TIM3_BASE) = 1u;

    /* H-bridge PWM: 20 kHz, PWM mode 1 on CH1 and CH2, update interrupt drives the loop. */
    TIM_PSC(TIM2_BASE) = 0;
    TIM_ARR(TIM2_BASE) = PWM_ARR;
    TIM_CCR1(TIM2_BASE) = 0;
    TIM_CCR2(TIM2_BASE) = 0;
    TIM_CCMR1(TIM2_BASE) = (6u << 4) | (1u << 3) | (6u << 12) | (1u << 11);
    TIM_CCER(TIM2_BASE) = 1u | (1u << 4);
    TIM_EGR(TIM2_BASE) = 1u;
    TIM_SR(TIM2_BASE) = 0;
    TIM_CR1(TIM2_BASE) = 1u | (1u << 7);                    /* CEN, ARPE: PWM at 0 % until the loop runs */

    /* Wait out the AS5047P power-up time (10 ms = 200 PWM periods), then home the
     * incremental encoder: two frames, the second answer is the angle. */
    for (uint32_t periods = 0; periods < 200u; periods++) {
        while ((TIM_SR(TIM2_BASE) & 1u) == 0) {
        }
        TIM_SR(TIM2_BASE) = 0;
    }
    float home = 0.0f;
    as5047_frame(AS5047_READ_ANGLECOM);
    if ((arm_cal.features & 1u) && as5047_angle(as5047_frame(AS5047_READ_ANGLECOM), &home)) {
        tm.home_angle = home;
    }
    last_cnt = (uint16_t)TIM_CNT(TIM3_BASE);
    tm.counts = (int32_t)(home * arm_cal.counts_per_rad + (home >= 0.0f ? 0.5f : -0.5f));
    tm.angle = last_angle = (float)tm.counts / arm_cal.counts_per_rad;

    TIM_SR(TIM2_BASE) = 0;
    TIM_DIER(TIM2_BASE) = 1u;
    NVIC_ISER0 = (1u << TIM2_IRQ);
    __asm volatile ("dsb\n\tisb" ::: "memory");

    uint32_t next_temperature = 0;
    for (;;) {
        if ((int32_t)(tm.loops - next_temperature) >= 0) {  /* every 100 control loops = 100 ms */
            next_temperature = tm.loops + 100u;
            float celsius;
            if (as6221_read(&celsius)) {
                tm.temperature = celsius;
            } else {
                tm.i2c_errors++;
            }
            update_duty_limit();
        }
        __asm volatile ("wfi");
    }
}
