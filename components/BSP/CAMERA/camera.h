#ifndef __CAMERA_H__
#define __CAMERA_H__

#include "esp_log.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_camera.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "pca9555a.h"
#include "lcd.h"


/* 引脚声明 */
#define CAM_PIN_PWDN     GPIO_NUM_NC
#define CAM_PIN_RESET    GPIO_NUM_NC
#define CAM_PIN_XCLK     GPIO_NUM_NC
#define CAM_PIN_SIOD     GPIO_NUM_39
#define CAM_PIN_SIOC     GPIO_NUM_38
#define CAM_PIN_D7       GPIO_NUM_3
#define CAM_PIN_D6       GPIO_NUM_16
#define CAM_PIN_D5       GPIO_NUM_17
#define CAM_PIN_D4       GPIO_NUM_7
#define CAM_PIN_D3       GPIO_NUM_15
#define CAM_PIN_D2       GPIO_NUM_5
#define CAM_PIN_D1       GPIO_NUM_6
#define CAM_PIN_D0       GPIO_NUM_4
#define CAM_PIN_VSYNC    GPIO_NUM_47
#define CAM_PIN_HREF     GPIO_NUM_48
#define CAM_PIN_PCLK     GPIO_NUM_18

/*
 * PWDN and RESET are connected to PCA9555A P0_1 and P0_2.
 * The native GPIO fields remain GPIO_NUM_NC so esp32-camera will not
 * configure the IO-expander pins as ESP32-S3 GPIOs.
 */
#define CAM_PWDN(level)  do { pca9555a_ov5640_pwdn_write((level)); } while (0)
#define CAM_RST(level)   do { pca9555a_ov5640_reset_write((level)); } while (0)

/* 函数声明 */
uint8_t camera_init(void);                 /* 摄像头初始化 */
void camera_show(uint16_t x, uint16_t y);  /* 摄像头显示 */
#endif
