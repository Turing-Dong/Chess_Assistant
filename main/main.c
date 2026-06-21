/**
 ****************************************************************************************************
 * @file        main.c
 * @author      正点原子团队(ALIENTEK)
 * @version     V1.0
 * @date        2023-12-01
 * @brief       FreeRTOS 时间片调度实验
 * @license     Copyright (c) 2020-2032, 广州市星翼电子科技有限公司
 ****************************************************************************************************
 * @attention
 *
 * 实验平台:正点原子 ESP32-S3开发板
 * 在线视频:www.yuanzige.com
 * 技术论坛:www.openedv.com
 * 公司网址:www.alientek.com
 * 购买地址:openedv.taobao.com
 *
 ****************************************************************************************************
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "led.h"
#include "beep.h"
#include "lcd.h"
#include "camera.h"
#include "spi.h"
#include "pca9555a.h"
#include "esp_system.h"
#include "nvs_flash.h"
#include "app_tasks.h"


i2c_obj_t i2c0_master;

/**
 * @brief       程序入口
 * @param       无
 * @retval      无
 */
void app_main(void)
{
    esp_err_t ret;
    
    ret= nvs_flash_init();  /* 初始化NVS */

    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND)
    {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    i2c0_master = iic_init(I2C_NUM_0);  /* 初始化IIC0 */
#if LCD_DRIVER_MODE == LCD_DRIVER_HARDWARE_SPI
    spi2_init();                        /* 初始化SPI2 */
#endif
    pca9555a_init(i2c0_master);         /* PCA9555A IO扩展芯片初始化 */
    beep_init();                        /* 蜂鸣器关闭，硬件为低电平有效 */
    led_init();                         /* 初始化LED */
    lcd_init();                         /* 初始化ST7796 LCD */
#if LCD_DIAGNOSTIC_MODE
    printf("LCD diagnostic mode: the panel should remain solid red.\r\n");
    while (1)
    {
        LED_TOGGLE();
        vTaskDelay(pdMS_TO_TICKS(500));
    }
#endif
#if !CAMERA_USE_TEST_FRAME
    ret = camera_init();

    if (ret != ESP_OK)
    {
        printf("camera_init failed: %s\r\n", esp_err_to_name(ret));
    }
#endif

    freertos_demo();    /* 运行FreeRTOS例程 */
}
