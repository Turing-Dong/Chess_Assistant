#include "camera.h"

#include <stdbool.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_jpg_decode.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "camera_bsp";
static bool camera_initialized;
static SemaphoreHandle_t camera_mutex;
static uint8_t *camera_display_buffer;
static uint32_t displayed_frames;
static framesize_t camera_current_frame_size = FRAMESIZE_FHD;
static volatile bool camera_upload_capture_active;
static camera_fb_t *camera_upload_frame;

#define CAMERA_INIT_ATTEMPTS       2
#define CAMERA_PWDN_RESET_MS       20
#define CAMERA_PWDN_STABLE_MS      80
#define CAMERA_RESET_ASSERT_MS     50
#define CAMERA_RESET_STABLE_MS     200
#define CAMERA_PREVIEW_FRAME_SIZE  FRAMESIZE_HD
#define CAMERA_UPLOAD_FRAME_SIZE   FRAMESIZE_FHD
#define CAMERA_FRAME_SIZE_SWITCH_DELAY_MS 150
#define CAMERA_PREVIEW_MUTEX_TIMEOUT_MS 1000
#define CAMERA_UPLOAD_MUTEX_TIMEOUT_MS 8000

extern const uint8_t camera_test_jpg_start[] asm("_binary_camera_test_jpg_start");
extern const uint8_t camera_test_jpg_end[] asm("_binary_camera_test_jpg_end");

static void camera_hardware_reset(void);

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
    /* Allocate buffers for 1920x1080; preview switches down after init. */
    .frame_size = CAMERA_UPLOAD_FRAME_SIZE,
    .jpeg_quality = 12,
    .fb_count = 2,
    .grab_mode = CAMERA_GRAB_WHEN_EMPTY,
};

typedef struct
{
    const camera_fb_t *frame;
    uint16_t source_width;
    uint16_t source_height;
    uint16_t output_width;
    uint16_t output_height;
    bool output_big_endian;
    uint8_t *rgb565_frame;
} camera_display_context_t;

static const char *camera_frame_size_name(framesize_t frame_size)
{
    switch (frame_size)
    {
    case FRAMESIZE_HD:
        return "HD 1280x720";
    case FRAMESIZE_FHD:
        return "FHD 1920x1080";
    default:
        return "custom";
    }
}

static esp_err_t camera_set_frame_size_locked(framesize_t frame_size, bool *changed)
{
    if (changed != NULL)
    {
        *changed = false;
    }

    if (camera_current_frame_size == frame_size)
    {
        return ESP_OK;
    }

    sensor_t *sensor = esp_camera_sensor_get();
    if (sensor == NULL)
    {
        return ESP_FAIL;
    }

    int result = sensor->set_framesize(sensor, frame_size);
    if (result != 0)
    {
        ESP_LOGE(TAG, "Failed to switch camera to %s",
                 camera_frame_size_name(frame_size));
        return ESP_FAIL;
    }

    camera_current_frame_size = frame_size;
    if (changed != NULL)
    {
        *changed = true;
    }
    vTaskDelay(pdMS_TO_TICKS(CAMERA_FRAME_SIZE_SWITCH_DELAY_MS));
    ESP_LOGI(TAG, "Camera frame size switched to %s",
             camera_frame_size_name(frame_size));
    return ESP_OK;
}

static esp_err_t camera_apply_sensor_settings(void)
{
    sensor_t *sensor = esp_camera_sensor_get();

    if (sensor == NULL)
    {
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

    return ESP_OK;
}

static esp_err_t camera_recover_locked(const char *reason)
{
    ESP_LOGW(TAG, "Recovering camera after %s", reason);

    esp_camera_deinit();
    camera_initialized = false;
    camera_current_frame_size = CAMERA_UPLOAD_FRAME_SIZE;
    vTaskDelay(pdMS_TO_TICKS(100));

    camera_hardware_reset();
    esp_err_t error = esp_camera_init(&camera_config);
    if (error != ESP_OK)
    {
        ESP_LOGE(TAG, "Camera reinit failed: %s", esp_err_to_name(error));
        return error;
    }

    error = camera_apply_sensor_settings();
    if (error != ESP_OK)
    {
        ESP_LOGE(TAG, "Camera sensor restore failed: %s", esp_err_to_name(error));
        esp_camera_deinit();
        return error;
    }

    error = camera_set_frame_size_locked(CAMERA_PREVIEW_FRAME_SIZE, NULL);
    if (error != ESP_OK)
    {
        esp_camera_deinit();
        return error;
    }

    camera_initialized = true;
    ESP_LOGI(TAG, "Camera recovered to %s", camera_frame_size_name(CAMERA_PREVIEW_FRAME_SIZE));
    return ESP_OK;
}

static jpg_scale_t camera_select_decode_scale(const camera_fb_t *frame,
                                              uint16_t output_width,
                                              uint16_t output_height)
{
    if (frame->width >= output_width * 2U &&
        frame->height >= output_height * 2U)
    {
        return JPG_SCALE_2X;
    }

    return JPG_SCALE_NONE;
}

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

static esp_err_t camera_mutex_init(void)
{
    if (camera_mutex != NULL)
    {
        return ESP_OK;
    }

    camera_mutex = xSemaphoreCreateMutex();
    if (camera_mutex == NULL)
    {
        ESP_LOGE(TAG, "Failed to create camera mutex");
        return ESP_ERR_NO_MEM;
    }

    return ESP_OK;
}

static void camera_hardware_reset(void)
{
    if (CAM_PIN_PWDN == GPIO_NUM_NC)
    {
        CAM_PWDN(1);
        vTaskDelay(pdMS_TO_TICKS(CAMERA_PWDN_RESET_MS));
        CAM_PWDN(0);
        vTaskDelay(pdMS_TO_TICKS(CAMERA_PWDN_STABLE_MS));
    }

    if (CAM_PIN_RESET == GPIO_NUM_NC)
    {
        CAM_RST(0);
        vTaskDelay(pdMS_TO_TICKS(CAMERA_RESET_ASSERT_MS));
        CAM_RST(1);
        vTaskDelay(pdMS_TO_TICKS(CAMERA_RESET_STABLE_MS));
    }
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
                context->output_width == 0 ||
                context->output_height == 0 ||
                context->rgb565_frame == NULL)
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
    for (uint16_t destination_y = 0;
         destination_y < context->output_height;
         destination_y++)
    {
        uint16_t source_y = (uint32_t)destination_y * context->source_height /
                            context->output_height;

        if (source_y < y || source_y >= y + height)
        {
            continue;
        }

        for (uint16_t destination_x = 0;
             destination_x < context->output_width;
             destination_x++)
        {
            uint16_t source_x = (uint32_t)destination_x * context->source_width /
                                context->output_width;

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
            size_t destination_pixel =
                ((size_t)destination_y * context->output_width) +
                destination_x;

            if (context->output_big_endian)
            {
                size_t destination_offset = destination_pixel * 2;
                context->rgb565_frame[destination_offset] =
                    (uint8_t)(rgb565 >> 8);
                context->rgb565_frame[destination_offset + 1] =
                    (uint8_t)rgb565;
            }
            else
            {
                ((uint16_t *)context->rgb565_frame)[destination_pixel] =
                    rgb565;
            }
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

    esp_err_t error = ESP_FAIL;
    for (uint8_t attempt = 1; attempt <= CAMERA_INIT_ATTEMPTS; attempt++)
    {
        camera_hardware_reset();
        error = esp_camera_init(&camera_config);
        if (error == ESP_OK)
        {
            break;
        }

        ESP_LOGW(TAG,
                 "Camera initialization attempt %u/%u failed: %s",
                 (unsigned)attempt,
                 (unsigned)CAMERA_INIT_ATTEMPTS,
                 esp_err_to_name(error));
        esp_camera_deinit();
        vTaskDelay(pdMS_TO_TICKS(200));
    }

    if (error != ESP_OK)
    {
        ESP_LOGE(TAG, "Camera initialization failed: %s", esp_err_to_name(error));
        return error;
    }

    error = camera_apply_sensor_settings();
    if (error != ESP_OK)
    {
        esp_camera_deinit();
        return error;
    }

    error = camera_mutex_init();
    if (error != ESP_OK)
    {
        esp_camera_deinit();
        return error;
    }

    if (xSemaphoreTake(camera_mutex, pdMS_TO_TICKS(1000)) != pdTRUE)
    {
        esp_camera_deinit();
        return ESP_ERR_TIMEOUT;
    }
    error = camera_set_frame_size_locked(CAMERA_PREVIEW_FRAME_SIZE, NULL);
    xSemaphoreGive(camera_mutex);
    if (error != ESP_OK)
    {
        esp_camera_deinit();
        return error;
    }

    camera_initialized = true;
    ESP_LOGI(TAG,
             "Camera initialized: preview=%s upload=%s JPEG mode",
             camera_frame_size_name(CAMERA_PREVIEW_FRAME_SIZE),
             camera_frame_size_name(CAMERA_UPLOAD_FRAME_SIZE));

    return ESP_OK;
}

static camera_fb_t *camera_capture_with_frame_size(framesize_t frame_size,
                                                  uint32_t mutex_timeout_ms,
                                                  bool for_upload)
{
    if (!camera_initialized)
    {
        return NULL;
    }

    if (camera_mutex_init() != ESP_OK)
    {
        return NULL;
    }

    if (!for_upload && camera_upload_capture_active)
    {
        return NULL;
    }

    if (for_upload)
    {
        camera_upload_capture_active = true;
        camera_upload_frame = NULL;
    }

    if (xSemaphoreTake(camera_mutex, pdMS_TO_TICKS(mutex_timeout_ms)) != pdTRUE)
    {
        ESP_LOGW(TAG, "Timed out waiting for camera");
        if (for_upload)
        {
            camera_upload_capture_active = false;
        }
        return NULL;
    }

    bool frame_size_changed = false;
    esp_err_t error = camera_set_frame_size_locked(frame_size,
                                                  &frame_size_changed);
    if (error != ESP_OK)
    {
        if (for_upload)
        {
            camera_set_frame_size_locked(CAMERA_PREVIEW_FRAME_SIZE, NULL);
            camera_upload_capture_active = false;
        }
        xSemaphoreGive(camera_mutex);
        return NULL;
    }

    camera_fb_t *frame = esp_camera_fb_get();
    if (frame == NULL)
    {
        if (for_upload)
        {
            camera_recover_locked("FHD frame capture timeout");
            camera_upload_capture_active = false;
        }
        xSemaphoreGive(camera_mutex);
        return NULL;
    }

    if (for_upload)
    {
        camera_upload_frame = frame;
    }

    return frame;
}

camera_fb_t *camera_capture(void)
{
    return camera_capture_with_frame_size(CAMERA_PREVIEW_FRAME_SIZE,
                                          CAMERA_PREVIEW_MUTEX_TIMEOUT_MS,
                                          false);
}

camera_fb_t *camera_capture_for_upload(void)
{
    return camera_capture_with_frame_size(CAMERA_UPLOAD_FRAME_SIZE,
                                          CAMERA_UPLOAD_MUTEX_TIMEOUT_MS,
                                          true);
}

bool camera_upload_capture_is_active(void)
{
    return camera_upload_capture_active;
}

void camera_release(camera_fb_t *frame)
{
    if (frame == NULL)
    {
        return;
    }

    bool upload_frame = (frame == camera_upload_frame);

    esp_camera_fb_return(frame);

    if (upload_frame)
    {
        camera_upload_frame = NULL;
        esp_err_t error = camera_set_frame_size_locked(CAMERA_PREVIEW_FRAME_SIZE, NULL);
        if (error != ESP_OK)
        {
            camera_recover_locked("failed to return to preview frame size");
        }
        camera_upload_capture_active = false;
    }

    if (camera_mutex != NULL)
    {
        xSemaphoreGive(camera_mutex);
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
        .output_width = CAMERA_DISPLAY_WIDTH,
        .output_height = CAMERA_DISPLAY_HEIGHT,
        .output_big_endian = true,
        .rgb565_frame = camera_display_buffer,
    };

    error = esp_jpg_decode(frame->len,
                           camera_select_decode_scale(frame,
                                                      CAMERA_DISPLAY_WIDTH,
                                                      CAMERA_DISPLAY_HEIGHT),
                           camera_jpeg_reader,
                           camera_jpeg_writer,
                           &context);
    if (error != ESP_OK)
    {
        return error;
    }

    uint16_t display_y = lcd_self.height - CAMERA_DISPLAY_HEIGHT;
    lcd_set_window(CAMERA_DISPLAY_X,
                   display_y,
                   CAMERA_DISPLAY_X + CAMERA_DISPLAY_WIDTH - 1,
                   display_y + CAMERA_DISPLAY_HEIGHT - 1);
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

esp_err_t camera_decode_frame_rgb565(const camera_fb_t *frame,
                                     uint16_t *output,
                                     uint16_t output_width,
                                     uint16_t output_height)
{
    if (frame == NULL || output == NULL ||
        output_width == 0 || output_height == 0)
    {
        return ESP_ERR_INVALID_ARG;
    }

    if (frame->format != PIXFORMAT_JPEG)
    {
        return ESP_ERR_NOT_SUPPORTED;
    }

    camera_display_context_t context = {
        .frame = frame,
        .output_width = output_width,
        .output_height = output_height,
        .output_big_endian = false,
        .rgb565_frame = (uint8_t *)output,
    };

    return esp_jpg_decode(frame->len,
                          camera_select_decode_scale(frame,
                                                     output_width,
                                                     output_height),
                          camera_jpeg_reader,
                          camera_jpeg_writer,
                          &context);
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
