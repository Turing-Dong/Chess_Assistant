#ifndef LVGL_APP_H
#define LVGL_APP_H

#include <stdbool.h>

#include "esp_err.h"

esp_err_t lvgl_app_start(void);
esp_err_t lvgl_app_set_wifi_connected(bool connected);
esp_err_t lvgl_app_set_bluetooth_connected(bool connected);

#endif
