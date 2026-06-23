#include "app_tasks.h"
#include "led.h"
#include "beep.h"
#include "lcd.h"
#include "camera.h"
/*FreeRTOS*********************************************************************************************/
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define LED_TASK_PRIO      10
#define LED_TASK_STK_SIZE  2*1024
TaskHandle_t              LedTask_Handler;
void led_task(void *pvParameters);

#define LCD_TASK_PRIO      9
#define LCD_TASK_STK_SIZE  6*1024
TaskHandle_t              LcdTask_Handler;
void lcd_task(void *pvParameters);

/******************************************************************************************************/
/*FreeRTOS配置*/

/* TASK1 任务 配置
 * 包括: 任务句柄 任务优先级 堆栈大小 创建任务
 */
#define TASK1_PRIO      10                   /* 任务优先级 */
#define TASK1_STK_SIZE  5*1024              /* 任务堆栈大小 */
TaskHandle_t            Task1Task_Handler;  /* 任务句柄 */
void task1(void *pvParameters);             /* 任务函数 */

/* TASK2 任务 配置
 * 包括: 任务句柄 任务优先级 堆栈大小 创建任务
 */
#define TASK2_PRIO      10                   /* 任务优先级 */
#define TASK2_STK_SIZE  5*1024              /* 任务堆栈大小 */
TaskHandle_t            Task2Task_Handler;  /* 任务句柄 */
void task2(void *pvParameters);             /* 任务函数 */

/******************************************************************************************************/

/**
 * @brief       FreeRTOS例程入口函数
 * @param       无
 * @retval      无
 */
void freertos_demo(void)
{
    /* 创建任务1 */
    xTaskCreatePinnedToCore((TaskFunction_t )task1,                 /* 任务函数 */
                            (const char*    )"task1",               /* 任务名称 */
                            (uint16_t       )TASK1_STK_SIZE,        /* 任务堆栈大小 */
                            (void*          )NULL,                  /* 传入给任务函数的参数 */
                            (UBaseType_t    )TASK1_PRIO,            /* 任务优先级 */
                            (TaskHandle_t*  )&Task1Task_Handler,    /* 任务句柄 */
                            (BaseType_t     ) 1);                   /* 该任务哪个内核运行 */
    /* 创建任务2 */
    xTaskCreatePinnedToCore((TaskFunction_t )task2,                 /* 任务函数 */
                            (const char*    )"task2",               /* 任务名称 */
                            (uint16_t       )TASK2_STK_SIZE,        /* 任务堆栈大小 */
                            (void*          )NULL,                  /* 传入给任务函数的参数 */
                            (UBaseType_t    )TASK2_PRIO,            /* 任务优先级 */
                            (TaskHandle_t*  )&Task2Task_Handler,    /* 任务句柄 */
                            (BaseType_t     ) 1);                   /* 该任务哪个内核运行 */
    xTaskCreatePinnedToCore((TaskFunction_t )led_task,
                            (const char*    )"led_task",
                            (uint16_t       )LED_TASK_STK_SIZE,
                            (void*          )NULL,
                            (UBaseType_t    )LED_TASK_PRIO,
                            (TaskHandle_t*  )&LedTask_Handler,
                            (BaseType_t     ) 1);
    xTaskCreatePinnedToCore((TaskFunction_t )lcd_task,
                            (const char*    )"lcd_task",
                            (uint16_t       )LCD_TASK_STK_SIZE,
                            (void*          )NULL,
                            (UBaseType_t    )LCD_TASK_PRIO,
                            (TaskHandle_t*  )&LcdTask_Handler,
                            (BaseType_t     ) 1);
}

/**
 * @brief       task1
 * @param       pvParameters : 传入参数(未用到)
 * @retval      无
 */
void task1(void *pvParameters)
{
    pvParameters = pvParameters;
    uint32_t task1_num = 0;
    
    while (1)
    {
        printf("任务1运行次数:%ld\r\n", ++task1_num);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

/**
 * @brief       task2
 * @param       pvParameters : 传入参数(未用到)
 * @retval      无
 */
void task2(void *pvParameters)
{
    pvParameters = pvParameters;
    uint32_t task2_num = 0;
    
    while (1)
    {
        printf("任务2运行次数:%ld\r\n", ++task2_num);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

/**
 * @brief       task2
 * @param       pvParameters : 传入参数(未用到)
 * @retval      无
 */
void led_task(void *pvParameters)
{
    pvParameters = pvParameters;

    while (1)
    {
        LED_TOGGLE();
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

void lcd_task(void *pvParameters)
{
    pvParameters = pvParameters;

    lcd_clear(BLACK);
    lcd_fill(0, 0, lcd_self.width - 1, 39, BLUE);
    lcd_show_string(12, 10, lcd_self.width - 24, 24, 16,
                    "ST7796 LANDSCAPE JPEG TEST", WHITE);

    uint16_t camera_y = lcd_self.height - CAMERA_DISPLAY_HEIGHT;
    lcd_fill(CAMERA_DISPLAY_X,
             camera_y,
             CAMERA_DISPLAY_X + CAMERA_DISPLAY_WIDTH - 1,
             lcd_self.height - 1,
             BLACK);
    uint16_t status_x = CAMERA_DISPLAY_X + CAMERA_DISPLAY_WIDTH + 8;
    uint16_t status_width = lcd_self.width - status_x - 4;

    lcd_fill(status_x, camera_y, lcd_self.width - 1, lcd_self.height - 1, BLACK);
    lcd_show_string(status_x, 96, status_width, 24, 16, "CAMERA", GREEN);
    lcd_show_string(status_x, 128, status_width, 24, 16, "360x240", WHITE);
    lcd_show_string(status_x, 160, status_width, 24, 16, "LEFT", WHITE);
    lcd_show_string(status_x, 184, status_width, 24, 16, "BOTTOM", WHITE);

#if CAMERA_USE_TEST_FRAME
    esp_err_t camera_error = camera_show_test_frame();

    lcd_fill(status_x, 240, lcd_self.width - 1, 287, BLACK);

    if (camera_error == ESP_OK)
    {
        lcd_show_string(status_x, 240, status_width, 24, 16, "TEST OK", GREEN);
    }
    else
    {
        lcd_show_string(status_x, 240, status_width, 24, 16, "ERROR", RED);
        lcd_show_num(status_x, 264, (uint32_t)camera_error, 6, 16, RED);
    }

    while (1)
    {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
#else
    uint32_t refresh_count = 0;

    while (1)
    {
        esp_err_t camera_error = camera_show();

        lcd_fill(status_x, 240, lcd_self.width - 1, 287, BLACK);

        if (camera_error == ESP_OK)
        {
            lcd_show_string(status_x, 240, status_width, 24, 16, "FRAME", WHITE);
            lcd_show_num(status_x, 264, refresh_count++, 6, 16, RED);
        }
        else
        {
            lcd_show_string(status_x, 240, status_width, 24, 16, "ERROR", RED);
            lcd_show_num(status_x, 264, (uint32_t)camera_error, 6, 16, RED);
        }

        vTaskDelay(pdMS_TO_TICKS(100));
    }
#endif
}
