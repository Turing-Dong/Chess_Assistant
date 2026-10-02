#ifndef __ADC_H_
#define __ADC_H_

#include "driver/gpio.h"
#include "esp_err.h"

#define LIGHT_ADC_GPIO             GPIO_NUM_NC /* IO8 is camera D7; no light sensor. */
#define LIGHT_ADC_MAX_RAW          4095

esp_err_t light_adc_init(void);
esp_err_t light_adc_read_raw(int *raw);
esp_err_t light_adc_read_voltage_mv(int *voltage_mv);
esp_err_t light_adc_read_percent(int *percent);

#endif
