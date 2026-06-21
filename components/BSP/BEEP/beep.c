#include "beep.h"

static int beep_state = BEEP_OFF_STATE;

void beep_init(void)
{
    beep_write(BEEP_OFF_STATE);
}

void beep_write(int val)
{
    beep_state = val ? BEEP_ON_STATE : BEEP_OFF_STATE;

    /* The board buzzer is active low through the PCA9555A. */
    pca9555a_beep_write(beep_state == BEEP_ON_STATE ? 0 : 1);
}

void beep_toggle(void)
{
    beep_write(!beep_state);
}

int beep_read(void)
{
    return beep_state;
}
