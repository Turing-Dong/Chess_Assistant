#include "beep.h"

static int beep_state = BEEP_OFF_STATE;

void beep_init(void)
{
    beep_write(BEEP_OFF_STATE);
}

void beep_write(int val)
{
    beep_state = val ? BEEP_ON_STATE : BEEP_OFF_STATE;
    pca9555a_beep_write(beep_state);
}

void beep_toggle(void)
{
    beep_write(!beep_state);
}

int beep_read(void)
{
    return beep_state;
}
