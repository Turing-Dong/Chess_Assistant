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
#include "key.h"
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
#define CONNECTION_COMMAND_COUNT   4
#define KEY_COMMAND_COUNT          8
#define KEY_TASK_STACK_SIZE        (2 * 1024)
#define KEY_TASK_PRIORITY          3
#define KEY_VISUAL_PRESS_MS        120
#define UI_BUTTON_COUNT            4
#define MOVE_PANEL_WIDTH           88
#define MOVE_PANEL_HEIGHT          248
#define MOVE_SECTION_WIDTH         84
#define MOVE_SECTION_HEIGHT        120
#define MOVE_COORDINATE_WIDTH      78
#define MOVE_COORDINATE_HEIGHT     42

#define CONNECTION_COLOR_OFFLINE   0x7A8793
#define CONNECTION_COLOR_ONLINE    0x2196F3
#define WIFI_COLOR_ONLINE          0x2ECC71

LV_FONT_DECLARE(lv_font_chess_16);

typedef enum
{
    CONNECTION_WIFI,
    CONNECTION_BLUETOOTH,
} connection_type_t;

typedef struct
{
    connection_type_t type;
    bool connected;
} connection_command_t;

static const char *TAG = "lvgl_app";

static lv_disp_draw_buf_t display_buffer;
static lv_disp_drv_t display_driver;
static lv_color_t *draw_buffer;
static uint8_t flush_buffer[LVGL_FLUSH_BUFFER_BYTES] __attribute__((aligned(4)));
static lv_color_t *camera_buffers[CAMERA_BUFFER_COUNT];
static lv_img_dsc_t camera_images[CAMERA_BUFFER_COUNT];
static QueueHandle_t camera_free_queue;
static QueueHandle_t camera_ready_queue;
static QueueHandle_t connection_command_queue;
static QueueHandle_t key_command_queue;
static lv_obj_t *camera_image_object;
static lv_obj_t *wifi_icon;
static lv_obj_t *bluetooth_icon;
static lv_obj_t *key_buttons[UI_BUTTON_COUNT];
static uint32_t key_release_time[UI_BUTTON_COUNT];

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

static void key_task(void *argument)
{
    (void)argument;

    while (true)
    {
        uint8_t key = key_scan(KEY_SCAN_SINGLE);

        if (key >= KEY_PRES_1 && key <= KEY_PRES_4)
        {
            xQueueSend(key_command_queue, &key, 0);
        }

        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

static esp_err_t key_control_init(void)
{
    key_command_queue = xQueueCreate(KEY_COMMAND_COUNT, sizeof(uint8_t));
    if (key_command_queue == NULL)
    {
        return ESP_ERR_NO_MEM;
    }

    key_init();

    BaseType_t task_created = xTaskCreatePinnedToCore(key_task,
                                                      "key_control",
                                                      KEY_TASK_STACK_SIZE,
                                                      NULL,
                                                      KEY_TASK_PRIORITY,
                                                      NULL,
                                                      0);
    return task_created == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

static void key_button_event_callback(lv_event_t *event)
{
    uint32_t key_number = (uint32_t)(uintptr_t)lv_event_get_user_data(event);
    ESP_LOGI(TAG, "KEY%lu clicked", (unsigned long)key_number);
}

static void connection_icon_set_state(lv_obj_t *icon,
                                      bool connected,
                                      uint32_t online_color)
{
    lv_obj_set_style_text_color(
        icon,
        lv_color_hex(connected ? online_color
                               : CONNECTION_COLOR_OFFLINE),
        0);
}

static void create_coordinate_box(lv_obj_t *parent,
                                  lv_align_t alignment,
                                  lv_coord_t y_offset)
{
    lv_obj_t *coordinate_box = lv_obj_create(parent);
    lv_obj_set_size(coordinate_box,
                    MOVE_COORDINATE_WIDTH,
                    MOVE_COORDINATE_HEIGHT);
    lv_obj_align(coordinate_box, alignment, 0, y_offset);
    lv_obj_set_style_bg_color(coordinate_box, lv_color_hex(0x142A46), 0);
    lv_obj_set_style_border_color(coordinate_box, lv_color_hex(0x6EC6E8), 0);
    lv_obj_set_style_border_width(coordinate_box, 1, 0);
    lv_obj_set_style_radius(coordinate_box, 4, 0);
    lv_obj_set_style_pad_all(coordinate_box, 0, 0);
    lv_obj_clear_flag(coordinate_box, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *coordinate_label = lv_label_create(coordinate_box);
    lv_label_set_text(coordinate_label, "(0, 0)");
    lv_obj_set_style_text_color(coordinate_label, lv_color_hex(0xF1FAEE), 0);
    lv_obj_center(coordinate_label);
}

static void create_move_section(lv_obj_t *parent,
                                lv_align_t alignment,
                                const char *title_text,
                                uint32_t accent_color)
{
    lv_obj_t *section = lv_obj_create(parent);
    lv_obj_set_size(section, MOVE_SECTION_WIDTH, MOVE_SECTION_HEIGHT);
    lv_obj_align(section, alignment, 0, 0);
    lv_obj_set_style_bg_color(section, lv_color_hex(0x1D3557), 0);
    lv_obj_set_style_border_color(section, lv_color_hex(accent_color), 0);
    lv_obj_set_style_border_width(section, 1, 0);
    lv_obj_set_style_radius(section, 6, 0);
    lv_obj_set_style_pad_top(section, 24, 0);
    lv_obj_set_style_pad_left(section, 2, 0);
    lv_obj_set_style_pad_right(section, 2, 0);
    lv_obj_set_style_pad_bottom(section, 2, 0);
    lv_obj_clear_flag(section, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *title = lv_label_create(section);
    lv_label_set_text(title, title_text);
    lv_obj_set_style_text_font(title, &lv_font_chess_16, 0);
    lv_obj_set_style_text_color(title, lv_color_hex(accent_color), 0);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, -22);

    create_coordinate_box(section, LV_ALIGN_TOP_MID, 2);
    create_coordinate_box(section, LV_ALIGN_BOTTOM_MID, -2);
}

static void lvgl_create_demo_screen(void)
{
    lv_obj_t *screen = lv_scr_act();
    lv_obj_set_style_bg_color(screen, lv_color_hex(0x101820), 0);
    lv_obj_set_style_bg_grad_color(screen, lv_color_hex(0x203A43), 0);
    lv_obj_set_style_bg_grad_dir(screen, LV_GRAD_DIR_VER, 0);

    lv_obj_t *title = lv_label_create(screen);
    lv_label_set_text(title, "Chess Assistant");
    lv_obj_set_style_text_font(title, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(title, lv_color_hex(0xFFFFFF), 0);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 4);

    bluetooth_icon = lv_label_create(screen);
    lv_label_set_text(bluetooth_icon, LV_SYMBOL_BLUETOOTH);
    connection_icon_set_state(bluetooth_icon,
                              false,
                              CONNECTION_COLOR_ONLINE);

    wifi_icon = lv_label_create(screen);
    lv_label_set_text(wifi_icon, LV_SYMBOL_WIFI);
    connection_icon_set_state(wifi_icon, false, WIFI_COLOR_ONLINE);
    lv_obj_align(wifi_icon, LV_ALIGN_TOP_RIGHT, -14, 18);
    lv_obj_align_to(bluetooth_icon,
                    wifi_icon,
                    LV_ALIGN_OUT_LEFT_MID,
                    -10,
                    0);

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

    lv_obj_t *move_panel = lv_obj_create(screen);
    lv_obj_set_size(move_panel, MOVE_PANEL_WIDTH, MOVE_PANEL_HEIGHT);
    lv_obj_align_to(move_panel,
                     camera_panel,
                     LV_ALIGN_OUT_RIGHT_MID,
                     8,
                     0);
    lv_obj_set_style_bg_color(move_panel, lv_color_hex(0x101820), 0);
    lv_obj_set_style_border_width(move_panel, 0, 0);
    lv_obj_set_style_radius(move_panel, 8, 0);
    lv_obj_set_style_pad_all(move_panel, 2, 0);
    lv_obj_clear_flag(move_panel, LV_OBJ_FLAG_SCROLLABLE);

    create_move_section(move_panel,
                        LV_ALIGN_TOP_MID,
                        "帅方走法",
                        0xFF6B6B);
    create_move_section(move_panel,
                        LV_ALIGN_BOTTOM_MID,
                        "将方走法",
                        0x45B7D1);

    for (uint8_t i = 0; i < UI_BUTTON_COUNT; i++)
    {
        key_buttons[i] = lv_btn_create(screen);
        lv_obj_set_size(key_buttons[i], 32, 56);
        lv_obj_align(key_buttons[i],
                     LV_ALIGN_TOP_RIGHT,
                     -8,
                     64 + (i * 64));
        lv_obj_set_style_bg_color(key_buttons[i],
                                  lv_color_hex(0x1D6FA5),
                                  LV_STATE_DEFAULT);
        lv_obj_set_style_bg_color(key_buttons[i],
                                  lv_color_hex(0x45B7D1),
                                  LV_STATE_PRESSED);
        lv_obj_add_event_cb(key_buttons[i],
                            key_button_event_callback,
                            LV_EVENT_CLICKED,
                            (void *)(uintptr_t)(i + 1));

        lv_obj_t *label = lv_label_create(key_buttons[i]);
        lv_label_set_text_fmt(label, "K%u", i + 1);
        lv_obj_center(label);
    }
}

static void lvgl_task(void *argument)
{
    (void)argument;
    int8_t displayed_buffer = -1;

    lvgl_create_demo_screen();

    while (true)
    {
        uint8_t ready_buffer;
        uint8_t pressed_key;
        connection_command_t connection_command;
        uint32_t now = lv_tick_get();

        while (xQueueReceive(connection_command_queue,
                             &connection_command,
                             0) == pdTRUE)
        {
            if (connection_command.type == CONNECTION_WIFI)
            {
                connection_icon_set_state(wifi_icon,
                                          connection_command.connected,
                                          WIFI_COLOR_ONLINE);
            }
            else
            {
                connection_icon_set_state(bluetooth_icon,
                                          connection_command.connected,
                                          CONNECTION_COLOR_ONLINE);
            }
        }

        while (xQueueReceive(key_command_queue,
                             &pressed_key,
                             0) == pdTRUE)
        {
            uint8_t button_index = pressed_key - KEY_PRES_1;

            lv_obj_add_state(key_buttons[button_index], LV_STATE_PRESSED);
            lv_event_send(key_buttons[button_index], LV_EVENT_CLICKED, NULL);
            key_release_time[button_index] = now + KEY_VISUAL_PRESS_MS;
        }

        for (uint8_t i = 0; i < UI_BUTTON_COUNT; i++)
        {
            if (key_release_time[i] != 0 &&
                (int32_t)(now - key_release_time[i]) >= 0)
            {
                lv_obj_clear_state(key_buttons[i], LV_STATE_PRESSED);
                key_release_time[i] = 0;
            }
        }

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

    connection_command_queue = xQueueCreate(CONNECTION_COMMAND_COUNT,
                                             sizeof(connection_command_t));
    if (connection_command_queue == NULL)
    {
        return ESP_ERR_NO_MEM;
    }

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

    error = key_control_init();
    if (error != ESP_OK)
    {
        ESP_LOGE(TAG, "Key control initialization failed: %s",
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

static esp_err_t connection_set_state(connection_type_t type, bool connected)
{
    if (connection_command_queue == NULL)
    {
        return ESP_ERR_INVALID_STATE;
    }

    connection_command_t command = {
        .type = type,
        .connected = connected,
    };

    return xQueueSend(connection_command_queue, &command, 0) == pdTRUE
               ? ESP_OK
               : ESP_ERR_TIMEOUT;
}

esp_err_t lvgl_app_set_wifi_connected(bool connected)
{
    return connection_set_state(CONNECTION_WIFI, connected);
}

esp_err_t lvgl_app_set_bluetooth_connected(bool connected)
{
    return connection_set_state(CONNECTION_BLUETOOTH, connected);
}
