#include "led.h"

static int led_state = PIN_RESET;

void led_init(void)
{
    led_write(PIN_RESET);
}

void led_write(int val)
{
    led_state = val ? PIN_SET : PIN_RESET;
    pca9555a_led_write(led_state);
}

void led_toggle(void)
{
    led_write(!led_state);
}

int led_read(void)
{
    return led_state;
}
