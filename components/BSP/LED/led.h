#ifndef __LED_H_
#define __LED_H_

#include "pca9555a.h"

enum GPIO_OUTPUT_STATE
{
    PIN_RESET,
    PIN_SET
};

#define LED(x)          led_write(x)
#define LED_TOGGLE()    led_toggle()

void led_init(void);
void led_write(int val);
void led_toggle(void);
int led_read(void);

#endif
