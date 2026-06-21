#include "lvgl_app.h"

#include <stdbool.h>
#include <stdint.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "camera.h"
#include "lcd.h"
#include "lvgl.h"

#define LVGL_TASK_STACK_SIZE       (8 * 1024)
#define LVGL_TASK_PRIORITY         5
#define LVGL_DRAW_BUFFER_LINES     20
#define LVGL_TICK_PERIOD_MS        2
#define LVGL_FLUSH_BUFFER_BYTES    4096
#define CAMERA_TASK_STACK_SIZE     (8 * 1024)
#define CAMERA_TASK_PRIORITY       4
#define CAMERA_IMAGE_WIDTH         320
#define CAMERA_IMAGE_HEIGHT        240
#define CAMERA_IMAGE_BUFFER_SIZE   \
    (CAMERA_IMAGE_WIDTH * CAMERA_IMAGE_HEIGHT * sizeof(lv_color_t))
#define CAMERA_BUFFER_COUNT        2

static const char *TAG = "lvgl_app";

static lv_disp_draw_buf_t display_buffer;
static lv_disp_drv_t display_driver;
static lv_color_t *draw_buffer;
static uint8_t flush_buffer[LVGL_FLUSH_BUFFER_BYTES] __attribute__((aligned(4)));
static lv_color_t *camera_buffers[CAMERA_BUFFER_COUNT];
static lv_img_dsc_t camera_images[CAMERA_BUFFER_COUNT];
static QueueHandle_t camera_free_queue;
static QueueHandle_t camera_ready_queue;
static lv_obj_t *camera_image_object;
static lv_obj_t *camera_status_label;
static uint32_t camera_displayed_frames;

static void lvgl_tick_callback(void *argument)
{
    (void)argument;
    lv_tick_inc(LVGL_TICK_PERIOD_MS);
}

static void lvgl_flush_callback(lv_disp_drv_t *driver,
                                const lv_area_t *area,
                                lv_color_t *color_map)
{
    int32_t width = area->x2 - area->x1 + 1;
    int32_t height = area->y2 - area->y1 + 1;
    size_t remaining_pixels = (size_t)width * height;

    lcd_set_window((uint16_t)area->x1,
                   (uint16_t)area->y1,
                   (uint16_t)area->x2,
                   (uint16_t)area->y2);

    while (remaining_pixels > 0)
    {
        size_t chunk_pixels = remaining_pixels;
        size_t maximum_pixels = sizeof(flush_buffer) / 2;

        if (chunk_pixels > maximum_pixels)
        {
            chunk_pixels = maximum_pixels;
        }

        for (size_t i = 0; i < chunk_pixels; i++)
        {
            uint16_t color = color_map[i].full;
            flush_buffer[i * 2] = (uint8_t)(color >> 8);
            flush_buffer[i * 2 + 1] = (uint8_t)color;
        }

        lcd_write_data(flush_buffer, (int)(chunk_pixels * 2));
        color_map += chunk_pixels;
        remaining_pixels -= chunk_pixels;
    }

    lv_disp_flush_ready(driver);
}

static esp_err_t lvgl_display_init(void)
{
    size_t buffer_pixels = (size_t)lcd_self.width * LVGL_DRAW_BUFFER_LINES;

    draw_buffer = heap_caps_malloc(buffer_pixels * sizeof(lv_color_t),
                                   MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (draw_buffer == NULL)
    {
        ESP_LOGE(TAG, "Unable to allocate LVGL draw buffer");
        return ESP_ERR_NO_MEM;
    }

    lv_disp_draw_buf_init(&display_buffer,
                          draw_buffer,
                          NULL,
                          (uint32_t)buffer_pixels);

    lv_disp_drv_init(&display_driver);
    display_driver.hor_res = lcd_self.width;
    display_driver.ver_res = lcd_self.height;
    display_driver.flush_cb = lvgl_flush_callback;
    display_driver.draw_buf = &display_buffer;

    if (lv_disp_drv_register(&display_driver) == NULL)
    {
        heap_caps_free(draw_buffer);
        draw_buffer = NULL;
        ESP_LOGE(TAG, "Unable to register LVGL display");
        return ESP_FAIL;
    }

    return ESP_OK;
}

static void camera_task(void *argument)
{
    (void)argument;

    while (true)
    {
        uint8_t buffer_index;

        if (xQueueReceive(camera_free_queue,
                          &buffer_index,
                          portMAX_DELAY) != pdTRUE)
        {
            continue;
        }

        camera_fb_t *frame = camera_capture();
        if (frame == NULL)
        {
            xQueueSend(camera_free_queue, &buffer_index, portMAX_DELAY);
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        esp_err_t error = camera_decode_frame_rgb565(
            frame,
            (uint16_t *)camera_buffers[buffer_index],
            CAMERA_IMAGE_WIDTH,
            CAMERA_IMAGE_HEIGHT);
        camera_release(frame);

        if (error != ESP_OK)
        {
            ESP_LOGW(TAG, "Camera frame decode failed: %s",
                     esp_err_to_name(error));
            xQueueSend(camera_free_queue, &buffer_index, portMAX_DELAY);
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        xQueueSend(camera_ready_queue, &buffer_index, portMAX_DELAY);
    }
}

static esp_err_t camera_preview_init(void)
{
    camera_free_queue = xQueueCreate(CAMERA_BUFFER_COUNT, sizeof(uint8_t));
    camera_ready_queue = xQueueCreate(CAMERA_BUFFER_COUNT, sizeof(uint8_t));

    if (camera_free_queue == NULL || camera_ready_queue == NULL)
    {
        return ESP_ERR_NO_MEM;
    }

    for (uint8_t i = 0; i < CAMERA_BUFFER_COUNT; i++)
    {
        camera_buffers[i] = heap_caps_malloc(CAMERA_IMAGE_BUFFER_SIZE,
                                             MALLOC_CAP_SPIRAM |
                                             MALLOC_CAP_8BIT);
        if (camera_buffers[i] == NULL)
        {
            ESP_LOGE(TAG, "Unable to allocate camera buffer %u", i);
            return ESP_ERR_NO_MEM;
        }

        camera_images[i].header.always_zero = 0;
        camera_images[i].header.w = CAMERA_IMAGE_WIDTH;
        camera_images[i].header.h = CAMERA_IMAGE_HEIGHT;
        camera_images[i].header.cf = LV_IMG_CF_TRUE_COLOR;
        camera_images[i].data_size = CAMERA_IMAGE_BUFFER_SIZE;
        camera_images[i].data = (const uint8_t *)camera_buffers[i];

        xQueueSend(camera_free_queue, &i, portMAX_DELAY);
    }

    BaseType_t task_created = xTaskCreatePinnedToCore(camera_task,
                                                      "camera_preview",
                                                      CAMERA_TASK_STACK_SIZE,
                                                      NULL,
                                                      CAMERA_TASK_PRIORITY,
                                                      NULL,
                                                      0);
    if (task_created != pdPASS)
    {
        return ESP_ERR_NO_MEM;
    }

    return ESP_OK;
}

static void lvgl_create_demo_screen(void)
{
    lv_obj_t *screen = lv_scr_act();
    lv_obj_set_style_bg_color(screen, lv_color_hex(0x101820), 0);
    lv_obj_set_style_bg_grad_color(screen, lv_color_hex(0x203A43), 0);
    lv_obj_set_style_bg_grad_dir(screen, LV_GRAD_DIR_VER, 0);

    lv_obj_t *title = lv_label_create(screen);
    lv_label_set_text(title, "Chess Assistant");
    lv_obj_set_style_text_color(title, lv_color_hex(0xFFFFFF), 0);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 18);

    lv_obj_t *camera_panel = lv_obj_create(screen);
    lv_obj_set_size(camera_panel, 328, 248);
    lv_obj_align(camera_panel, LV_ALIGN_BOTTOM_LEFT, 8, -8);
    lv_obj_set_style_bg_color(camera_panel, lv_color_hex(0x111111), 0);
    lv_obj_set_style_border_color(camera_panel, lv_color_hex(0x45B7D1), 0);
    lv_obj_set_style_border_width(camera_panel, 2, 0);
    lv_obj_set_style_radius(camera_panel, 8, 0);
    lv_obj_set_style_pad_all(camera_panel, 2, 0);
    lv_obj_clear_flag(camera_panel, LV_OBJ_FLAG_SCROLLABLE);

    camera_image_object = lv_img_create(camera_panel);
    lv_obj_center(camera_image_object);

    lv_obj_t *status_panel = lv_obj_create(screen);
    lv_obj_set_size(status_panel, 128, 248);
    lv_obj_align(status_panel, LV_ALIGN_BOTTOM_RIGHT, -8, -8);
    lv_obj_set_style_bg_color(status_panel, lv_color_hex(0x1D3557), 0);
    lv_obj_set_style_border_width(status_panel, 0, 0);
    lv_obj_set_style_radius(status_panel, 8, 0);

    camera_status_label = lv_label_create(status_panel);
    lv_label_set_text(camera_status_label,
                      "LVGL 8.3\n\nCAMERA\nSTARTING\n\n320x240");
    lv_obj_set_style_text_align(camera_status_label, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(camera_status_label,
                                lv_color_hex(0xF1FAEE),
                                0);
    lv_obj_center(camera_status_label);
}

static void lvgl_task(void *argument)
{
    (void)argument;
    int8_t displayed_buffer = -1;

    lvgl_create_demo_screen();

    while (true)
    {
        uint8_t ready_buffer;

        if (xQueueReceive(camera_ready_queue,
                          &ready_buffer,
                          0) == pdTRUE)
        {
            int8_t previous_buffer = displayed_buffer;

            lv_img_set_src(camera_image_object,
                           &camera_images[ready_buffer]);
            lv_obj_center(camera_image_object);
            lv_refr_now(NULL);

            displayed_buffer = (int8_t)ready_buffer;
            camera_displayed_frames++;
            lv_label_set_text_fmt(camera_status_label,
                                  "LVGL 8.3\n\nCAMERA\nLIVE\n\n320x240\n\nFRAME\n%lu",
                                  (unsigned long)camera_displayed_frames);

            if (previous_buffer >= 0)
            {
                uint8_t free_buffer = (uint8_t)previous_buffer;
                xQueueSend(camera_free_queue, &free_buffer, portMAX_DELAY);
            }
        }

        uint32_t delay_ms = lv_timer_handler();

        if (delay_ms < 5)
        {
            delay_ms = 5;
        }
        else if (delay_ms > 20)
        {
            delay_ms = 20;
        }

        vTaskDelay(pdMS_TO_TICKS(delay_ms));
    }
}

esp_err_t lvgl_app_start(void)
{
    lv_init();

    esp_err_t error = lvgl_display_init();
    if (error != ESP_OK)
    {
        return error;
    }

    const esp_timer_create_args_t tick_timer_arguments = {
        .callback = lvgl_tick_callback,
        .name = "lvgl_tick",
    };
    esp_timer_handle_t tick_timer;

    error = esp_timer_create(&tick_timer_arguments, &tick_timer);
    if (error != ESP_OK)
    {
        return error;
    }

    error = esp_timer_start_periodic(tick_timer,
                                     LVGL_TICK_PERIOD_MS * 1000);
    if (error != ESP_OK)
    {
        esp_timer_delete(tick_timer);
        return error;
    }

    error = camera_preview_init();
    if (error != ESP_OK)
    {
        ESP_LOGE(TAG, "Camera preview initialization failed: %s",
                 esp_err_to_name(error));
        esp_timer_stop(tick_timer);
        esp_timer_delete(tick_timer);
        return error;
    }

    BaseType_t task_created = xTaskCreatePinnedToCore(lvgl_task,
                                                      "lvgl",
                                                      LVGL_TASK_STACK_SIZE,
                                                      NULL,
                                                      LVGL_TASK_PRIORITY,
                                                      NULL,
                                                      1);
    if (task_created != pdPASS)
    {
        esp_timer_stop(tick_timer);
        esp_timer_delete(tick_timer);
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "LVGL started at %ux%u",
             lcd_self.width,
             lcd_self.height);
    return ESP_OK;
}
