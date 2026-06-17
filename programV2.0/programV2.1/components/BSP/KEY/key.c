#include "key.h"

void key_init(void)
{
    /* PCA9555A key pins are configured by pca9555a_init(). */
}

uint8_t key_scan(uint8_t mode)
{
    return pca9555a_key_scan(mode);
}

int key_read(key_id_t key)
{
    switch (key)
    {
    case KEY_ID_1:
        return pca9555a_key1_read();

    case KEY_ID_2:
        return pca9555a_key2_read();

    case KEY_ID_3:
        return pca9555a_key3_read();

    case KEY_ID_4:
        return pca9555a_key4_read();

    default:
        return 1;
    }
}
