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
#define CAMERA_PREVIEW_FRAME_SIZE  FRAMESIZE_FHD
#define CAMERA_UPLOAD_FRAME_SIZE   FRAMESIZE_FHD
#define CAMERA_PREVIEW_SOURCE_WIDTH 1920U
#define CAMERA_PREVIEW_CROP_SIZE   1080U
#define CAMERA_FRAME_SIZE_SWITCH_DELAY_MS 150
#define CAMERA_FRAME_SIZE_CAPTURE_ATTEMPTS 5
#define CAMERA_PREVIEW_MUTEX_TIMEOUT_MS 1000
#define CAMERA_UPLOAD_MUTEX_TIMEOUT_MS 8000

extern const uint8_t camera_test_jpg_start[] asm("_binary_camera_test_jpg_start");
extern const uint8_t camera_test_jpg_end[] asm("_binary_camera_test_jpg_end");

static esp_err_t camera_hardware_reset(void);

/* Called with capture stopped or camera_mutex held. No sensor writes. */
static esp_err_t camera_check_sensor_link(void)
{
    sensor_t *sensor = esp_camera_sensor_get();
    if (sensor == NULL || sensor->get_reg == NULL)
    {
        return ESP_ERR_INVALID_STATE;
    }
    for (unsigned i = 0; i < 3; ++i)
    {
        int high = sensor->get_reg(sensor, 0x300A, 0xFF);
        int low = sensor->get_reg(sensor, 0x300B, 0xFF);
        if (high != 0x56 || low != 0x40)
        {
            ESP_LOGE(TAG, "SCCB ID check %u/3 failed: high=0x%X low=0x%X, expected 0x5640",
                     i + 1, (unsigned)high, (unsigned)low);
            return ESP_FAIL;
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    ESP_LOGI(TAG, "SCCB PASS: OV5640 ID=0x5640, 3/3 register reads OK (addr=0x%02X)",
             sensor->slv_addr);
    return ESP_OK;
}

static void camera_diagnose_capture_timeout(void)
{
    static int64_t last_report;
    int64_t now = esp_timer_get_time();
    if (last_report != 0 && now - last_report < 15000000)
    {
        return;
    }
    last_report = now;
    esp_err_t error = camera_check_sensor_link();
    ESP_LOGE(TAG, "Frame timeout: SCCB=%s; check DVP VSYNC/HREF/PCLK, D0..D7 and sensor clock",
             esp_err_to_name(error));
    const int pins[] = {CAM_PIN_VSYNC, CAM_PIN_HREF, CAM_PIN_PCLK};
    unsigned changes[3] = {0};
    unsigned highs[3] = {0};
    unsigned samples = 0;
    int previous[3];
    for (unsigned i = 0; i < 3; ++i) previous[i] = gpio_get_level(pins[i]);
    /* Short sampling windows with yields: activity evidence, not a logic analyzer. */
    for (unsigned window = 0; window < 20; ++window)
    {
        int64_t until = esp_timer_get_time() + 1000;
        do
        {
            for (unsigned i = 0; i < 3; ++i)
            {
                int level = gpio_get_level(pins[i]);
                highs[i] += level;
                changes[i] += (level != previous[i]);
                previous[i] = level;
            }
            ++samples;
        } while (esp_timer_get_time() < until);
        vTaskDelay(1);
    }
    ESP_LOGI(TAG, "DVP sampled activity VSYNC/HREF/PCLK: changes=%u/%u/%u high_samples=%u/%u/%u total=%u",
             changes[0], changes[1], changes[2], highs[0], highs[1], highs[2], samples);
    ESP_LOGW(TAG, "Sampled activity is not signal frequency; zero changes does not prove a missing signal");
}

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
    uint16_t crop_x;
    uint16_t crop_y;
    uint16_t crop_width;
    uint16_t crop_height;
    uint16_t output_width;
    uint16_t output_height;
    bool output_big_endian;
    uint8_t *rgb565_frame;
} camera_display_context_t;

static const char *camera_frame_size_name(framesize_t frame_size)
{
    switch (frame_size)
    {
    case FRAMESIZE_QVGA:
        return "QVGA 320x240";
    case FRAMESIZE_HD:
        return "HD 1280x720";
    case FRAMESIZE_FHD:
        return "FHD 1920x1080";
    default:
        return "custom";
    }
}

static bool camera_jpeg_marker_is_sof(uint8_t marker)
{
    return (marker >= 0xC0U && marker <= 0xC3U) ||
           (marker >= 0xC5U && marker <= 0xC7U) ||
           (marker >= 0xC9U && marker <= 0xCBU) ||
           (marker >= 0xCDU && marker <= 0xCFU);
}

/* Read the dimensions encoded in the JPEG instead of trusting frame metadata. */
static bool camera_jpeg_get_dimensions(const uint8_t *data,
                                       size_t length,
                                       uint16_t *width,
                                       uint16_t *height)
{
    if (data == NULL || width == NULL || height == NULL || length < 4U ||
        data[0] != 0xFFU || data[1] != 0xD8U)
    {
        return false;
    }

    size_t offset = 2U;
    while (offset < length)
    {
        while (offset < length && data[offset] != 0xFFU)
        {
            offset++;
        }
        while (offset < length && data[offset] == 0xFFU)
        {
            offset++;
        }
        if (offset >= length)
        {
            return false;
        }

        uint8_t marker = data[offset++];
        if (marker == 0x00U || marker == 0xD8U)
        {
            continue;
        }
        if (marker == 0xD9U || marker == 0xDAU)
        {
            return false;
        }
        if (marker == 0x01U || (marker >= 0xD0U && marker <= 0xD7U))
        {
            continue;
        }
        if (offset + 2U > length)
        {
            return false;
        }

        size_t segment_length =
            ((size_t)data[offset] << 8U) | data[offset + 1U];
        if (segment_length < 2U || segment_length > length - offset)
        {
            return false;
        }

        if (camera_jpeg_marker_is_sof(marker))
        {
            if (segment_length < 7U)
            {
                return false;
            }
            *height = ((uint16_t)data[offset + 3U] << 8U) |
                      data[offset + 4U];
            *width = ((uint16_t)data[offset + 5U] << 8U) |
                     data[offset + 6U];
            return *width != 0U && *height != 0U;
        }

        offset += segment_length;
    }

    return false;
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

    if (sensor->set_brightness(sensor, 0) != 0 ||
        sensor->set_contrast(sensor, 0) != 0 ||
        sensor->set_saturation(sensor, 0) != 0 ||
        sensor->set_hmirror(sensor, 0) != 0)
    {
        ESP_LOGE(TAG, "Sensor settings failed over SCCB");
        return ESP_FAIL;
    }

    if (sensor->id.PID == OV3660_PID)
    {
        sensor->set_vflip(sensor, 1);
        sensor->set_brightness(sensor, 1);
        sensor->set_saturation(sensor, -2);
    }
    else if (sensor->id.PID == OV5640_PID)
    {
        if (sensor->set_vflip(sensor, 1) != 0)
        {
            return ESP_FAIL;
        }
    }

    return camera_check_sensor_link();
}

static esp_err_t camera_recover_locked(const char *reason)
{
    ESP_LOGW(TAG, "Recovering camera after %s", reason);

    esp_camera_deinit();
    camera_initialized = false;
    camera_current_frame_size = CAMERA_UPLOAD_FRAME_SIZE;
    vTaskDelay(pdMS_TO_TICKS(100));

    esp_err_t error = camera_hardware_reset();
    if (error != ESP_OK)
    {
        return error;
    }
    error = esp_camera_init(&camera_config);
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
    const uint32_t frame_width = frame->width;
    const uint32_t frame_height = frame->height;

    /*
     * Let the JPEG decoder discard as much detail as possible before RGB
     * conversion. The decoded image must still cover the requested output so
     * the writer only performs the final, small nearest-neighbour resize.
     */
    if (frame_width >= (uint32_t)output_width * 8U &&
        frame_height >= (uint32_t)output_height * 8U)
    {
        return JPG_SCALE_8X;
    }

    if (frame_width >= (uint32_t)output_width * 4U &&
        frame_height >= (uint32_t)output_height * 4U)
    {
        return JPG_SCALE_4X;
    }

    if (frame_width >= (uint32_t)output_width * 2U &&
        frame_height >= (uint32_t)output_height * 2U)
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

static esp_err_t camera_hardware_reset(void)
{
    esp_err_t error;
    if (CAM_PIN_PWDN == GPIO_NUM_NC)
    {
        error = pca9555a_pin_write_checked(PCA9555A_OV5640_PWDN_IO, 1);
        if (error != ESP_OK) return error;
        vTaskDelay(pdMS_TO_TICKS(CAMERA_PWDN_RESET_MS));
        error = pca9555a_pin_write_checked(PCA9555A_OV5640_PWDN_IO, 0);
        if (error != ESP_OK) return error;
        vTaskDelay(pdMS_TO_TICKS(CAMERA_PWDN_STABLE_MS));
    }

    if (CAM_PIN_RESET == GPIO_NUM_NC)
    {
        error = pca9555a_pin_write_checked(PCA9555A_OV5640_RESET_IO, 0);
        if (error != ESP_OK) return error;
        vTaskDelay(pdMS_TO_TICKS(CAMERA_RESET_ASSERT_MS));
        error = pca9555a_pin_write_checked(PCA9555A_OV5640_RESET_IO, 1);
        if (error != ESP_OK) return error;
        vTaskDelay(pdMS_TO_TICKS(CAMERA_RESET_STABLE_MS));
    }
    /* Address each register pair explicitly; only compare camera bits. */
    uint8_t regs[8];
    for (uint8_t reg = 0; reg < sizeof(regs); reg += 2)
    {
        error = pca9555a_read_registers(reg, &regs[reg], 2);
        if (error != ESP_OK)
        {
            ESP_LOGE(TAG, "PCA9555A reset readback reg=0x%02X failed: %s",
                     reg, esp_err_to_name(error));
            return error;
        }
    }
    unsigned mask = 0;
    unsigned expected = 0;
    if (CAM_PIN_PWDN == GPIO_NUM_NC) mask |= PCA9555A_OV5640_PWDN_IO;
    if (CAM_PIN_RESET == GPIO_NUM_NC)
    {
        mask |= PCA9555A_OV5640_RESET_IO;
        expected |= PCA9555A_OV5640_RESET_IO;
    }
    unsigned input = regs[0] | (regs[1] << 8);
    unsigned output = regs[2] | (regs[3] << 8);
    unsigned inversion = regs[4] | (regs[5] << 8);
    unsigned config = regs[6] | (regs[7] << 8);
    ESP_LOGI(TAG, "PCA9555A readback: input=0x%04X output=0x%04X config=0x%04X",
             input, output, config);
    if ((config & mask) != 0 || (output & mask) != expected ||
        ((input ^ inversion) & mask) != expected)
    {
        ESP_LOGE(TAG, "Camera reset state invalid: require PWDN=0 RESET=1, mask=0x%04X", mask);
        return ESP_ERR_INVALID_STATE;
    }
    ESP_LOGI(TAG, "PCA9555A PASS: camera control pins released");
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
                context->output_width == 0 ||
                context->output_height == 0 ||
                context->rgb565_frame == NULL)
            {
                return false;
            }

            context->crop_x = 0;
            context->crop_y = 0;
            context->crop_width = width;
            context->crop_height = height;

            if (context->output_width == context->output_height &&
                width >= height &&
                context->frame->width == CAMERA_PREVIEW_SOURCE_WIDTH &&
                context->frame->height == CAMERA_PREVIEW_CROP_SIZE)
            {
                /*
                 * esp_jpg_decode reports dimensions after its integer decode
                 * scale. Since the validated source is 1920x1080, using the
                 * scaled height here selects the exact central 1080x1080
                 * source region before it is reduced to the LCD preview.
                 */
                context->crop_width = height;
                context->crop_x = (width - context->crop_width) / 2U;
            }
            else
            {
                uint32_t source_aspect =
                    (uint32_t)width * context->output_height;
                uint32_t output_aspect =
                    (uint32_t)height * context->output_width;

                if (source_aspect > output_aspect)
                {
                    context->crop_width =
                        (uint32_t)height * context->output_width /
                        context->output_height;
                    context->crop_x = (width - context->crop_width) / 2U;
                }
                else if (source_aspect < output_aspect)
                {
                    context->crop_height =
                        (uint32_t)width * context->output_height /
                        context->output_width;
                    context->crop_y = (height - context->crop_height) / 2U;
                }
            }
        }

        return true;
    }

    /*
     * The decoder calls this function once per JPEG tile. Convert only the
     * destination pixels whose nearest source pixel belongs to this tile.
     * Scanning the complete output for every tile makes HD decoding hundreds
     * of times more expensive than the useful pixel conversion itself.
     */
    uint32_t source_x_begin = x > context->crop_x ? x : context->crop_x;
    uint32_t source_y_begin = y > context->crop_y ? y : context->crop_y;
    uint32_t source_x_end = (uint32_t)x + width;
    uint32_t source_y_end = (uint32_t)y + height;
    uint32_t crop_x_end = (uint32_t)context->crop_x + context->crop_width;
    uint32_t crop_y_end = (uint32_t)context->crop_y + context->crop_height;

    if (source_x_end > crop_x_end)
    {
        source_x_end = crop_x_end;
    }
    if (source_y_end > crop_y_end)
    {
        source_y_end = crop_y_end;
    }
    if (source_x_begin >= source_x_end || source_y_begin >= source_y_end)
    {
        return true;
    }

    uint32_t destination_y_begin =
        ((source_y_begin - context->crop_y) * context->output_height +
         context->crop_height - 1U) /
        context->crop_height;
    uint32_t destination_y_end =
        ((source_y_end - context->crop_y) * context->output_height +
         context->crop_height - 1U) /
        context->crop_height;
    uint32_t destination_x_begin =
        ((source_x_begin - context->crop_x) * context->output_width +
         context->crop_width - 1U) /
        context->crop_width;
    uint32_t destination_x_end =
        ((source_x_end - context->crop_x) * context->output_width +
         context->crop_width - 1U) /
        context->crop_width;

    if (destination_y_end > context->output_height)
    {
        destination_y_end = context->output_height;
    }
    if (destination_x_end > context->output_width)
    {
        destination_x_end = context->output_width;
    }

    for (uint32_t destination_y = destination_y_begin;
         destination_y < destination_y_end;
         destination_y++)
    {
        uint16_t source_y = context->crop_y +
            (uint32_t)destination_y * context->crop_height /
            context->output_height;

        for (uint32_t destination_x = destination_x_begin;
             destination_x < destination_x_end;
             destination_x++)
        {
            uint16_t source_x = context->crop_x +
                (uint32_t)destination_x * context->crop_width /
                context->output_width;

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
    ESP_LOGI(TAG, "Camera wiring: SDA=%d SCL=%d XCLK=%d (%u Hz configured)",
             CAM_PIN_SIOD, CAM_PIN_SIOC, CAM_PIN_XCLK, (unsigned)camera_config.xclk_freq_hz);
    ESP_LOGI(TAG, "DVP wiring: D0..D7=%d,%d,%d,%d,%d,%d,%d,%d VSYNC=%d HREF=%d PCLK=%d",
             CAM_PIN_D0, CAM_PIN_D1, CAM_PIN_D2, CAM_PIN_D3,
             CAM_PIN_D4, CAM_PIN_D5, CAM_PIN_D6, CAM_PIN_D7,
             CAM_PIN_VSYNC, CAM_PIN_HREF, CAM_PIN_PCLK);
    if (CAM_PIN_XCLK == GPIO_NUM_NC)
    {
        ESP_LOGW(TAG, "XCLK output disabled: sensor must have an external clock matching the configured frequency");
    }
    for (uint8_t attempt = 1; attempt <= CAMERA_INIT_ATTEMPTS; attempt++)
    {
        error = camera_hardware_reset();
        if (error != ESP_OK)
        {
            ESP_LOGE(TAG, "Camera hardware reset failed: %s", esp_err_to_name(error));
            return error;
        }
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

    const uint16_t expected_width = resolution[frame_size].width;
    const uint16_t expected_height = resolution[frame_size].height;
    camera_fb_t *frame = NULL;
    uint16_t jpeg_width = 0;
    uint16_t jpeg_height = 0;

    for (uint8_t attempt = 1U;
         attempt <= CAMERA_FRAME_SIZE_CAPTURE_ATTEMPTS;
         attempt++)
    {
        frame = esp_camera_fb_get();
        if (frame == NULL)
        {
            break;
        }

        jpeg_width = 0;
        jpeg_height = 0;
        bool dimensions_valid = camera_jpeg_get_dimensions(frame->buf,
                                                           frame->len,
                                                           &jpeg_width,
                                                           &jpeg_height);
        if (dimensions_valid &&
            jpeg_width == expected_width && jpeg_height == expected_height)
        {
            /* esp_camera_fb_get() reports the current sensor setting, which can
             * differ from a queued frame. Preserve the dimensions from JPEG. */
            frame->width = jpeg_width;
            frame->height = jpeg_height;
            break;
        }

        ESP_LOGW(TAG,
                 "Discarding stale JPEG %u/%u after switch to %s: jpeg=%ux%u bytes=%u",
                 (unsigned)attempt,
                 (unsigned)CAMERA_FRAME_SIZE_CAPTURE_ATTEMPTS,
                 camera_frame_size_name(frame_size),
                 (unsigned)jpeg_width,
                 (unsigned)jpeg_height,
                 (unsigned)frame->len);
        esp_camera_fb_return(frame);
        frame = NULL;
    }

    if (frame == NULL)
    {
        camera_diagnose_capture_timeout();
        if (for_upload)
        {
            camera_recover_locked("valid FHD frame capture timeout");
            camera_upload_capture_active = false;
        }
        xSemaphoreGive(camera_mutex);
        return NULL;
    }

    if (frame_size_changed)
    {
        ESP_LOGI(TAG,
                 "Validated first %s JPEG after frame-size switch: %ux%u, %u bytes",
                 camera_frame_size_name(frame_size),
                 (unsigned)jpeg_width,
                 (unsigned)jpeg_height,
                 (unsigned)frame->len);
    }

    static bool first_frame_reported;
    if (!first_frame_reported)
    {
        ESP_LOGI(TAG, "DVP frame received: %ux%u, %u bytes, JPEG SOI=%s",
                 (unsigned)frame->width, (unsigned)frame->height, (unsigned)frame->len,
                 frame->len >= 2 && frame->buf[0] == 0xFF && frame->buf[1] == 0xD8 ? "OK" : "BAD");
        first_frame_reported = true;
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
