#include "camera.h"

#include <stdbool.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_jpg_decode.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "camera_bsp";
static bool camera_initialized;
static uint8_t *camera_display_buffer;
static uint32_t displayed_frames;

extern const uint8_t camera_test_jpg_start[] asm("_binary_camera_test_jpg_start");
extern const uint8_t camera_test_jpg_end[] asm("_binary_camera_test_jpg_end");

static camera_config_t camera_config = {
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
    .xclk_freq_hz = 24000000,
    .ledc_timer = LEDC_TIMER_0,
    .ledc_channel = LEDC_CHANNEL_0,
    .fb_location = CAMERA_FB_IN_PSRAM,
    .pixel_format = PIXFORMAT_JPEG,
    .frame_size = FRAMESIZE_QVGA,
    .jpeg_quality = 12,
    .fb_count = 2,
    .grab_mode = CAMERA_GRAB_WHEN_EMPTY,
};

typedef struct
{
    const camera_fb_t *frame;
    uint16_t source_width;
    uint16_t source_height;
    uint16_t display_x;
    uint16_t display_y;
    uint8_t *rgb565_frame;
} camera_display_context_t;

static esp_err_t camera_display_buffer_init(void)
{
    if (camera_display_buffer != NULL)
    {
        return ESP_OK;
    }

    camera_display_buffer = heap_caps_malloc(CAMERA_DISPLAY_BUFFER_SIZE,
                                             MALLOC_CAP_SPIRAM |
                                             MALLOC_CAP_8BIT);
    if (camera_display_buffer == NULL)
    {
        ESP_LOGE(TAG, "Failed to allocate %u-byte RGB565 display buffer in PSRAM",
                 (unsigned)CAMERA_DISPLAY_BUFFER_SIZE);
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "Allocated %u-byte RGB565 display buffer in PSRAM",
             (unsigned)CAMERA_DISPLAY_BUFFER_SIZE);
    return ESP_OK;
}

static size_t camera_jpeg_reader(void *arg, size_t index, uint8_t *buffer, size_t length)
{
    camera_display_context_t *context = (camera_display_context_t *)arg;

    if (index >= context->frame->len)
    {
        return 0;
    }

    if (length > context->frame->len - index)
    {
        length = context->frame->len - index;
    }

    if (buffer != NULL)
    {
        memcpy(buffer, context->frame->buf + index, length);
    }

    return length;
}

static bool camera_jpeg_writer(void *arg,
                               uint16_t x,
                               uint16_t y,
                               uint16_t width,
                               uint16_t height,
                               uint8_t *rgb888)
{
    camera_display_context_t *context = (camera_display_context_t *)arg;

    if (rgb888 == NULL)
    {
        if (x == 0 && y == 0)
        {
            context->source_width = width;
            context->source_height = height;

            if (width == 0 || height == 0 ||
                context->display_x + CAMERA_DISPLAY_WIDTH > lcd_self.width ||
                context->display_y + CAMERA_DISPLAY_HEIGHT > lcd_self.height)
            {
                return false;
            }
        }

        return true;
    }

    /*
     * The decoder supplies RGB888 tiles. Each destination pixel is mapped to
     * its nearest source pixel and stored as RGB565 in the PSRAM display
     * buffer. The complete image is sent with one LCD window after decoding.
     */
    for (uint16_t destination_y = 0; destination_y < CAMERA_DISPLAY_HEIGHT; destination_y++)
    {
        uint16_t source_y = (uint32_t)destination_y * context->source_height /
                            CAMERA_DISPLAY_HEIGHT;

        if (source_y < y || source_y >= y + height)
        {
            continue;
        }

        for (uint16_t destination_x = 0; destination_x < CAMERA_DISPLAY_WIDTH; destination_x++)
        {
            uint16_t source_x = (uint32_t)destination_x * context->source_width /
                                CAMERA_DISPLAY_WIDTH;

            if (source_x < x || source_x >= x + width)
            {
                continue;
            }

            size_t source_offset =
                (((size_t)(source_y - y) * width) + (source_x - x)) * 3;
            uint8_t red = rgb888[source_offset];
            uint8_t green = rgb888[source_offset + 1];
            uint8_t blue = rgb888[source_offset + 2];
            uint16_t rgb565 = ((uint16_t)(red & 0xF8) << 8) |
                              ((uint16_t)(green & 0xFC) << 3) |
                              (blue >> 3);
            size_t destination_offset =
                (((size_t)destination_y * CAMERA_DISPLAY_WIDTH) +
                 destination_x) * 2;

            context->rgb565_frame[destination_offset] =
                (uint8_t)(rgb565 >> 8);
            context->rgb565_frame[destination_offset + 1] =
                (uint8_t)rgb565;
        }
    }

    return true;
}

esp_err_t camera_init(void)
{
    if (camera_initialized)
    {
        return ESP_OK;
    }

    if (CAM_PIN_PWDN == GPIO_NUM_NC)
    {
        CAM_PWDN(0);
        vTaskDelay(pdMS_TO_TICKS(10));
    }

    if (CAM_PIN_RESET == GPIO_NUM_NC)
    {
        CAM_RST(0);
        vTaskDelay(pdMS_TO_TICKS(20));
        CAM_RST(1);
        vTaskDelay(pdMS_TO_TICKS(20));
    }

    esp_err_t error = esp_camera_init(&camera_config);

    if (error != ESP_OK)
    {
        ESP_LOGE(TAG, "Camera initialization failed: %s", esp_err_to_name(error));
        return error;
    }

    sensor_t *sensor = esp_camera_sensor_get();

    if (sensor == NULL)
    {
        esp_camera_deinit();
        return ESP_FAIL;
    }

    sensor->set_brightness(sensor, 0);
    sensor->set_contrast(sensor, 0);
    sensor->set_saturation(sensor, 0);
    sensor->set_hmirror(sensor, 0);

    if (sensor->id.PID == OV3660_PID)
    {
        sensor->set_vflip(sensor, 1);
        sensor->set_brightness(sensor, 1);
        sensor->set_saturation(sensor, -2);
    }
    else if (sensor->id.PID == OV5640_PID)
    {
        sensor->set_vflip(sensor, 1);
    }

    camera_initialized = true;
    ESP_LOGI(TAG, "Camera initialized in QVGA JPEG mode");

    return ESP_OK;
}

camera_fb_t *camera_capture(void)
{
    if (!camera_initialized)
    {
        return NULL;
    }

    return esp_camera_fb_get();
}

void camera_release(camera_fb_t *frame)
{
    if (frame != NULL)
    {
        esp_camera_fb_return(frame);
    }
}

esp_err_t camera_display_frame(const camera_fb_t *frame)
{
    if (frame == NULL)
    {
        return ESP_ERR_INVALID_ARG;
    }

    if (frame->format != PIXFORMAT_JPEG)
    {
        return ESP_ERR_NOT_SUPPORTED;
    }

    esp_err_t error = camera_display_buffer_init();

    if (error != ESP_OK)
    {
        return error;
    }

    int64_t start_time = esp_timer_get_time();
    camera_display_context_t context = {
        .frame = frame,
        .display_x = CAMERA_DISPLAY_X,
        .display_y = lcd_self.height - CAMERA_DISPLAY_HEIGHT,
        .rgb565_frame = camera_display_buffer,
    };

    error = esp_jpg_decode(frame->len,
                           JPG_SCALE_NONE,
                           camera_jpeg_reader,
                           camera_jpeg_writer,
                           &context);
    if (error != ESP_OK)
    {
        return error;
    }

    lcd_set_window(context.display_x,
                   context.display_y,
                   context.display_x + CAMERA_DISPLAY_WIDTH - 1,
                   context.display_y + CAMERA_DISPLAY_HEIGHT - 1);
    lcd_write_data(camera_display_buffer, CAMERA_DISPLAY_BUFFER_SIZE);

    displayed_frames++;
    if (displayed_frames == 1 || displayed_frames % 10 == 0)
    {
        ESP_LOGI(TAG, "Displayed frame %lu in %lld ms",
                 (unsigned long)displayed_frames,
                 (long long)((esp_timer_get_time() - start_time) / 1000));
    }

    return ESP_OK;
}

esp_err_t camera_show(void)
{
    camera_fb_t *frame = camera_capture();

    if (frame == NULL)
    {
        return camera_initialized ? ESP_FAIL : ESP_ERR_INVALID_STATE;
    }

    esp_err_t error = camera_display_frame(frame);
    camera_release(frame);

    return error;
}

esp_err_t camera_show_test_frame(void)
{
    camera_fb_t test_frame = {
        .buf = (uint8_t *)camera_test_jpg_start,
        .len = (size_t)(camera_test_jpg_end - camera_test_jpg_start),
        .width = 320,
        .height = 240,
        .format = PIXFORMAT_JPEG,
    };

    return camera_display_frame(&test_frame);
}
