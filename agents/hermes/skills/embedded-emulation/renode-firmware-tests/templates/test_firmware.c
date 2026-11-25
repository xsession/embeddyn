/* <MCU> model test: <what>. Results in r[] at a fixed RAM address; r[0] = last step reached.
 * Expected values (computed before running):
 *   r[1] = ...   because ...
 */
#include <xc.h>                          /* or the vendor device header */

/* configuration fuses: they decide Fcy, set them explicitly */
/* #pragma config ... */

volatile unsigned int r[32] __attribute__((address(0x0A00)));   /* adapt: a RAM address free of the stack */
volatile unsigned int events;

/* void __attribute__((interrupt, no_auto_psv)) _T1Interrupt(void) { events++; IFS0bits.T1IF = 0; } */

int main(void)
{
    unsigned int timeout;

    /* step 1: <peripheral setup per the data sheet> */
    /* ... */
    for(timeout = 0; timeout < 60000 && !0 /* done flag */; timeout++);
    r[1] = 0;            /* the value to check */
    r[0] = 1;

    /* step 2: ... */
    r[0] = 2;

    for(;;);
}
