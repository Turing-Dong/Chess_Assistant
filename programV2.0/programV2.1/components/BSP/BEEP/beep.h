#ifndef __BEEP_H_
#define __BEEP_H_

#include "pca9555a.h"

#define BEEP_OFF_STATE  0
#define BEEP_ON_STATE   1

#define BEEP(x)         beep_write(x)
#define BEEP_ON()       beep_write(BEEP_ON_STATE)
#define BEEP_OFF()      beep_write(BEEP_OFF_STATE)
#define BEEP_TOGGLE()   beep_toggle()

void beep_init(void);
void beep_write(int val);
void beep_toggle(void);
int beep_read(void);

#endif
