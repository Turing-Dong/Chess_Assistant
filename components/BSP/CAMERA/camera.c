/**
 ****************************************************************************************************
 * @file        camera.c
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

#include "camera.h"


/* 摄像头配置 */
static camera_config_t camera_config = {
    /* 引脚配置 */
    .pin_pwdn = CAM_PIN_PWDN,
    .pin_reset = CAM_PIN_RESET,
    .pin_xclk = CAM_PIN_XCLK,
    .pin_sccb_sda = CAM_PIN_SIOD,
    .pin_sccb_scl = CAM_PIN_SIOC,
    .pin_d7 = CAM_PIN_D7,
    .pin_d6 = CAM_PIN_D6,
    .pin_d5 = CAM_PIN_D5,
    .pin_d4 = CAM_PIN_D4,
    .pin_d3 = CAM_PIN_D3,
    .pin_d2 = CAM_PIN_D2,
    .pin_d1 = CAM_PIN_D1,
    .pin_d0 = CAM_PIN_D0,
    .pin_vsync = CAM_PIN_VSYNC,
    .pin_href = CAM_PIN_HREF,
    .pin_pclk = CAM_PIN_PCLK,
    /* 图像配置 */
    .xclk_freq_hz = 24000000,
    .ledc_timer = LEDC_TIMER_0,
    .ledc_channel = LEDC_CHANNEL_0,
    .fb_location = CAMERA_FB_IN_PSRAM,
    .pixel_format = PIXFORMAT_JPEG,         /* 图像输出模式 */
    .frame_size = FRAMESIZE_QVGA,           /* 图像输出大小 */
    .jpeg_quality = 12,                     /* 0-63，对于OV系列相机传感器，数量越少意味着质量越高 */
    .fb_count = 2,                          /* 当使用jpeg模式时，如果fb_count超过一个，则驱动程序将在连续模式下工作 */
    .grab_mode = CAMERA_GRAB_WHEN_EMPTY,
};

/**
 * @brief       摄像头初始化
 * @param       cmd 传输的8位命令数据
 * @retval      无
 */
uint8_t camera_init(void)
{
    esp_err_t err = ESP_OK;

    if (CAM_PIN_PWDN == GPIO_NUM_NC)
    {
        CAM_PWDN(0);
    } 

    if (CAM_PIN_RESET == GPIO_NUM_NC)
    { 
        CAM_RST(0);
        vTaskDelay(20);
        CAM_RST(1);
        vTaskDelay(20);
    }

    /* 摄像头初始化 */
    err = esp_camera_init(&camera_config);

    if (err != ESP_OK)
    {
        return 1;
    }

    sensor_t * s = esp_camera_sensor_get();

    s->set_brightness(s, 0);      /* 设置亮度 (-2 ~ 2) */
    s->set_contrast(s, 0);        /* 设置对比度 (-2 ~ 2) */
    s->set_saturation(s, 0);      /* 设置饱和度 (-2 ~ 2) */
    s->set_hmirror(s, 0);         /* 不设置水平方向翻转 */

    /* 如果摄像头模块是OV3660或者是OV5640，则需要以下配置 */
    if (s->id.PID == OV3660_PID)
    {
        s->set_vflip(s, 1);         /* 向后翻转 */
        s->set_brightness(s, 1);    /* 亮度提高 */
        s->set_saturation(s, -2);   /* 降低饱和度 */
    }
    else if (s->id.PID == OV5640_PID)
    {
        s->set_vflip(s, 1);         /* 向后翻转 */
    }

    return err;
}

unsigned long i = 0;
unsigned long j = 0;
camera_fb_t *fb = NULL;

/**
 * @brief       显示摄像头数据（RGB565）
 * @param       x：x轴坐标
 * @param       y：y轴坐标
 * @retval      无
 */
void camera_show(uint16_t x, uint16_t y)
{
    fb = esp_camera_fb_get();

    if (x + fb->width > lcd_self.width || y + fb->height > lcd_self.height)
    {
        printf("显示区域大小屏幕区域！！！\r\n");
        goto err;
    }

    lcd_set_window(x, y, x + fb->width - 1, y + fb->height - 1);

    /* lcd_buf存储摄像头整一帧RGB数据 */
    for (j = 0; j < fb->width * fb->height; j++)
    {
        lcd_buf[2 * j] = (fb->buf[2 * i]) ;
        lcd_buf[2 * j + 1] =  (fb->buf[2 * i + 1]);
        i ++;
    }
    
    /* 例如：96*96*2/1536 = 12;分12次发送RGB数据 */
    for(j = 0; j < (fb->width * fb->height * 2 / LCD_BUF_SIZE); j++)
    {
        /* &lcd_buf[j * LCD_BUF_SIZE] 偏移地址发送数据 */
        lcd_write_data(&lcd_buf[j * LCD_BUF_SIZE] , LCD_BUF_SIZE);
    }
err:
    esp_camera_fb_return(fb);
    i = 0;
    fb = NULL;
}
