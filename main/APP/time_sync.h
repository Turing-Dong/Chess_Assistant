#ifndef TIME_SYNC_H
#define TIME_SYNC_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

bool system_time_is_valid(void);
uint64_t system_time_now_ms(void);

/* Required before HTTPS because certificate validation depends on time. */
esp_err_t system_time_sync(uint32_t timeout_ms);

#endif
