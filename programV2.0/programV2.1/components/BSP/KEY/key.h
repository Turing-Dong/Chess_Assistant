#ifndef __KEY_H_
#define __KEY_H_

#include <stdint.h>
#include "pca9555a.h"

typedef enum
{
    KEY_ID_1 = 1,
    KEY_ID_2,
    KEY_ID_3,
    KEY_ID_4,
} key_id_t;

#define KEY_SCAN_SINGLE    0
#define KEY_SCAN_CONTINUE  1

#define KEY_PRES_1         PCA9555A_KEY1_PRES
#define KEY_PRES_2         PCA9555A_KEY2_PRES
#define KEY_PRES_3         PCA9555A_KEY3_PRES
#define KEY_PRES_4         PCA9555A_KEY4_PRES

#define KEY1_READ()        key_read(KEY_ID_1)
#define KEY2_READ()        key_read(KEY_ID_2)
#define KEY3_READ()        key_read(KEY_ID_3)
#define KEY4_READ()        key_read(KEY_ID_4)

void key_init(void);
uint8_t key_scan(uint8_t mode);
int key_read(key_id_t key);

#endif
