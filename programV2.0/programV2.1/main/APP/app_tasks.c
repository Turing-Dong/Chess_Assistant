#include "app_tasks.h"
#include "led.h"
#include "beep.h"
#include "lcd.h"
/*FreeRTOS*********************************************************************************************/
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define LED_TASK_PRIO      10
#define LED_TASK_STK_SIZE  2*1024
TaskHandle_t              LedTask_Handler;
void led_task(void *pvParameters);

#define LCD_TASK_PRIO      9
#define LCD_TASK_STK_SIZE  4*1024
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
    beep_init();
    beep_write(1);
    while (1)
    {
        LED_TOGGLE();
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

void lcd_task(void *pvParameters)
{
    pvParameters = pvParameters;
    uint32_t refresh_count = 0;

    lcd_fill(0, 0, lcd_self.width - 1, 180, BLACK);
    lcd_fill(0, 0, lcd_self.width - 1, 39, BLUE);
    lcd_show_string(12, 10, lcd_self.width - 24, 24, 16, "ST7796 LCD TEST", WHITE);

    lcd_show_string(12, 60, lcd_self.width - 24, 24, 16, "GPIO9  RS/DC", WHITE);
    lcd_show_string(12, 84, lcd_self.width - 24, 24, 16, "GPIO10 CS", WHITE);
    lcd_show_string(12, 108, lcd_self.width - 24, 24, 16, "GPIO11 MOSI", WHITE);
    lcd_show_string(12, 132, lcd_self.width - 24, 24, 16, "GPIO12 SCLK", WHITE);
    lcd_show_string(12, 156, lcd_self.width - 24, 24, 16, "GPIO13 SDO", WHITE);

    lcd_fill(12, 210, 71, 269, RED);
    lcd_fill(84, 210, 143, 269, GREEN);
    lcd_fill(156, 210, 215, 269, BLUE);
    lcd_fill(228, 210, 287, 269, YELLOW);

    lcd_draw_rectangle(8, 204, 291, 275, BLACK);
    lcd_draw_line(12, 300, lcd_self.width - 13, 300, RED);
    lcd_draw_line(12, 320, lcd_self.width - 13, 360, GREEN);
    lcd_draw_circle(lcd_self.width / 2, 410, 35, MAGENTA);

    while (1)
    {
        lcd_fill(12, 452, 220, 475, BLACK);
        lcd_show_string(12, 452, 120, 24, 16, "Refresh:", WHITE);
        lcd_show_num(88, 452, refresh_count++, 6, 16, RED);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
