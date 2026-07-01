#ifndef WIFI_APP_H
#define WIFI_APP_H

#include <stdbool.h>

#include "esp_err.h"
#include "freertos/FreeRTOS.h"

esp_err_t wifi_app_start(void);
bool wifi_app_is_connected(void);
esp_err_t wifi_app_wait_connected(TickType_t timeout_ticks);

#endif
