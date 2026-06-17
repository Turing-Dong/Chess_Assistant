/**
 ****************************************************************************************************
 * @file        camera.h
 * @author      正点原子团队(ALIENTEK)
 * @version     V1.0
 * @date        2023-08-26
 * @brief       摄像头驱动代码
 *
 * @license     Copyright (c) 2020-2032, 广州市星翼电子科技有限公司
 ****************************************************************************************************
 * @attention
 *
 * 实验平台:正点原子 ESP32-S3 开发板
 * 在线视频:www.yuanzige.com
 * 技术论坛:www.openedv.com
 * 公司网址:www.alientek.com
 * 购买地址:openedv.taobao.com
 *
 ****************************************************************************************************
 */

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
#define CAM_PIN_D7       GPIO_NUM_18
#define CAM_PIN_D6       GPIO_NUM_17
#define CAM_PIN_D5       GPIO_NUM_16
#define CAM_PIN_D4       GPIO_NUM_15
#define CAM_PIN_D3       GPIO_NUM_7
#define CAM_PIN_D2       GPIO_NUM_6
#define CAM_PIN_D1       GPIO_NUM_5
#define CAM_PIN_D0       GPIO_NUM_4
#define CAM_PIN_VSYNC    GPIO_NUM_47
#define CAM_PIN_HREF     GPIO_NUM_48
#define CAM_PIN_PCLK     GPIO_NUM_45

#define CAM_PWDN(x)         do{ x ? \
                                (xl9555_pin_write(OV_PWDN_IO, 1)):       \
                                (xl9555_pin_write(OV_PWDN_IO, 0));       \
                            }while(0)

#define CAM_RST(x)          do{ x ? \
                                (xl9555_pin_write(OV_RESET_IO, 1)):       \
                                (xl9555_pin_write(OV_RESET_IO, 0));       \
                            }while(0)

/* 函数声明 */
uint8_t camera_init(void);                 /* 摄像头初始化 */
void camera_show(uint16_t x, uint16_t y);  /* 摄像头显示 */
#endif
