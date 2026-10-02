#include "image_task.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "app_config.h"
#include "beep.h"
#include "camera.h"
#include "cJSON.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "oss_client.h"
#include "time_sync.h"
#include "wifi_app.h"

static const char *TAG = "image_task";

static SemaphoreHandle_t image_task_mutex;
static image_task_state_t image_task_state = IMAGE_TASK_STATE_IDLE;
static image_task_result_callback_t image_task_callback;
static void *image_task_callback_user_data;

static esp_err_t image_task_lock(void)
{
    if (image_task_mutex == NULL)
    {
        image_task_mutex = xSemaphoreCreateMutex();
        if (image_task_mutex == NULL)
        {
            return ESP_ERR_NO_MEM;
        }
    }

    if (xSemaphoreTake(image_task_mutex, 0) != pdTRUE)
    {
        return ESP_ERR_INVALID_STATE;
    }

    return ESP_OK;
}

static void image_task_unlock(void)
{
    if (image_task_mutex != NULL)
    {
        xSemaphoreGive(image_task_mutex);
    }
}

static void image_task_set_state(image_task_state_t state)
{
    image_task_state = state;
}

void image_task_set_result_callback(image_task_result_callback_t callback,
                                    void *user_data)
{
    image_task_callback = callback;
    image_task_callback_user_data = user_data;
}

image_task_state_t image_task_get_state(void)
{
    return image_task_state;
}

static esp_err_t image_task_add_string_checked(cJSON *root,
                                               const char *name,
                                               const char *value)
{
    return cJSON_AddStringToObject(root, name, value) == NULL ?
           ESP_ERR_NO_MEM :
           ESP_OK;
}

static esp_err_t image_task_add_number_checked(cJSON *root,
                                               const char *name,
                                               double value)
{
    return cJSON_AddNumberToObject(root, name, value) == NULL ?
           ESP_ERR_NO_MEM :
           ESP_OK;
}

esp_err_t image_task_build_manifest_json(const device_identity_t *identity,
                                         const char *frame_id,
                                         uint16_t image_width,
                                         uint16_t image_height,
                                         char *json_buffer,
                                         size_t json_buffer_size)
{
    if (identity == NULL || frame_id == NULL ||
        json_buffer == NULL || json_buffer_size == 0U)
    {
        return ESP_ERR_INVALID_ARG;
    }

    char session_id[SESSION_ID_BUFFER_SIZE] = {0};
    char image_key[OSS_URL_BUFFER_SIZE] = {0};
    char result_key[OSS_URL_BUFFER_SIZE] = {0};

    esp_err_t error = device_identity_format_session_id(identity,
                                                        session_id,
                                                        sizeof(session_id));
    if (error == ESP_OK)
    {
        error = oss_build_image_key(identity->device_id,
                                    frame_id,
                                    image_key,
                                    sizeof(image_key));
    }
    if (error == ESP_OK)
    {
        error = oss_build_result_key(identity->device_id,
                                     frame_id,
                                     result_key,
                                     sizeof(result_key));
    }
    if (error != ESP_OK)
    {
        return error;
    }

    cJSON *root = cJSON_CreateObject();
    if (root == NULL)
    {
        return ESP_ERR_NO_MEM;
    }

    if (error == ESP_OK)
    {
        error = image_task_add_number_checked(root, "protocol_version", 1);
    }
    if (error == ESP_OK)
    {
        error = image_task_add_string_checked(root,
                                             "device_id",
                                             identity->device_id);
    }
    if (error == ESP_OK)
    {
        error = image_task_add_string_checked(root, "session_id", session_id);
    }
    if (error == ESP_OK)
    {
        error = image_task_add_string_checked(root, "frame_id", frame_id);
    }
    if (error == ESP_OK)
    {
        error = image_task_add_number_checked(root,
                                             "sequence",
                                             identity->sequence);
    }
    if (error == ESP_OK)
    {
        error = image_task_add_string_checked(root, "status", "pending");
    }
    if (error == ESP_OK)
    {
        error = image_task_add_string_checked(root, "image_key", image_key);
    }
    if (error == ESP_OK)
    {
        error = image_task_add_string_checked(root, "result_key", result_key);
    }
    if (error == ESP_OK)
    {
        error = image_task_add_number_checked(root,
                                             "image_width",
                                             image_width);
    }
    if (error == ESP_OK)
    {
        error = image_task_add_number_checked(root,
                                             "image_height",
                                             image_height);
    }
    if (error == ESP_OK)
    {
        error = image_task_add_number_checked(root,
                                             "captured_at_ms",
                                             (double)system_time_now_ms());
    }
    if (error == ESP_OK)
    {
        error = image_task_add_number_checked(root,
                                             "result_timeout_ms",
                                             RESULT_WAIT_TIMEOUT_MS);
    }

    if (error == ESP_OK)
    {
        char *printed = cJSON_PrintUnformatted(root);
        if (printed == NULL)
        {
            error = ESP_ERR_NO_MEM;
        }
        else
        {
            size_t length = strlen(printed);
            if (length >= json_buffer_size)
            {
                error = ESP_ERR_INVALID_SIZE;
            }
            else
            {
                memcpy(json_buffer, printed, length + 1U);
            }
            cJSON_free(printed);
        }
    }

    cJSON_Delete(root);
    return error;
}

static esp_err_t image_task_put_json_with_retry(const char *url,
                                                const char *json_text,
                                                size_t json_length)
{
    esp_err_t last_error = ESP_FAIL;

    for (uint32_t attempt = 0U;
         attempt < OSS_UPLOAD_RETRY_COUNT;
         attempt++)
    {
        last_error = oss_put_json(url, json_text, json_length);
        if (last_error == ESP_OK)
        {
            return ESP_OK;
        }

        ESP_LOGW(TAG,
                 "JSON upload attempt %u failed: %s",
                 (unsigned)(attempt + 1U),
                 esp_err_to_name(last_error));

        if (attempt + 1U < OSS_UPLOAD_RETRY_COUNT)
        {
            vTaskDelay(pdMS_TO_TICKS(OSS_UPLOAD_RETRY_BASE_DELAY_MS *
                                     (attempt + 1U)));
        }
    }

    return last_error;
}

static esp_err_t image_task_upload_jpeg_with_retry(const char *url,
                                                   const uint8_t *image_data,
                                                   size_t image_length)
{
    esp_err_t last_error = ESP_FAIL;

    for (uint32_t attempt = 0U;
         attempt < OSS_UPLOAD_RETRY_COUNT;
         attempt++)
    {
        last_error = oss_upload_jpeg(url, image_data, image_length);
        if (last_error == ESP_OK)
        {
            return ESP_OK;
        }

        ESP_LOGW(TAG,
                 "Image upload attempt %u failed: %s",
                 (unsigned)(attempt + 1U),
                 esp_err_to_name(last_error));

        if (attempt + 1U < OSS_UPLOAD_RETRY_COUNT)
        {
            vTaskDelay(pdMS_TO_TICKS(OSS_UPLOAD_RETRY_BASE_DELAY_MS *
                                     (attempt + 1U)));
        }
    }

    return last_error;
}

static void image_task_beep_success_once(void)
{
    BEEP_ON();
    vTaskDelay(pdMS_TO_TICKS(IMAGE_SUCCESS_BEEP_MS));
    BEEP_OFF();
}

static esp_err_t image_task_wait_result(const device_identity_t *identity,
                                        const char *frame_id,
                                        const char *result_url,
                                        uint16_t image_width,
                                        uint16_t image_height,
                                        image_process_result_t *process_result)
{
    char json_buffer[RESULT_JSON_BUFFER_SIZE] = {0};
    char query_url[OSS_URL_BUFFER_SIZE + 40U] = {0};
    uint32_t elapsed_ms = 0U;

    while (elapsed_ms < RESULT_WAIT_TIMEOUT_MS)
    {
        int length = snprintf(query_url,
                              sizeof(query_url),
                              "%s?t=%" PRIu64,
                              result_url,
                              system_time_now_ms());
        if (length < 0 || (size_t)length >= sizeof(query_url))
        {
            return ESP_ERR_INVALID_SIZE;
        }

        size_t json_length = 0U;
        int http_status = 0;
        esp_err_t error = oss_get_json(query_url,
                                       json_buffer,
                                       sizeof(json_buffer),
                                       &json_length,
                                       &http_status);
        if (error != ESP_OK)
        {
            ESP_LOGW(TAG, "Result GET failed: %s", esp_err_to_name(error));
        }
        else if (http_status == 404)
        {
            ESP_LOGI(TAG, "Result is not ready yet");
        }
        else if (http_status == 200)
        {
            return image_result_parse_and_validate(json_buffer,
                                                   json_length,
                                                   identity,
                                                   frame_id,
                                                   image_width,
                                                   image_height,
                                                   process_result);
        }
        else if (http_status == 408 || http_status == 429 ||
                 http_status == 500 || http_status == 503)
        {
            ESP_LOGW(TAG, "Result temporary HTTP status=%d", http_status);
        }
        else
        {
            ESP_LOGE(TAG, "Result fatal HTTP status=%d", http_status);
            return ESP_FAIL;
        }

        vTaskDelay(pdMS_TO_TICKS(RESULT_POLL_INTERVAL_MS));
        elapsed_ms += RESULT_POLL_INTERVAL_MS;
    }

    return ESP_ERR_TIMEOUT;
}

esp_err_t image_task_process_one_frame(device_identity_t *identity)
{
    if (identity == NULL)
    {
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t error = image_task_lock();
    if (error != ESP_OK)
    {
        return error;
    }

    char frame_id[FRAME_ID_BUFFER_SIZE] = {0};
    char image_url[OSS_URL_BUFFER_SIZE] = {0};
    char manifest_url[OSS_URL_BUFFER_SIZE] = {0};
    char result_url[OSS_URL_BUFFER_SIZE] = {0};
    char manifest_json[MANIFEST_JSON_BUFFER_SIZE] = {0};
    image_process_result_t process_result = {0};
    uint16_t image_width = 0U;
    uint16_t image_height = 0U;
    camera_fb_t *frame = NULL;
    uint8_t *image_copy = NULL;
    size_t image_length = 0U;

    image_task_set_state(IMAGE_TASK_STATE_CAPTURE);

    if (!wifi_app_is_connected())
    {
        error = ESP_ERR_INVALID_STATE;
        goto exit;
    }

    if (!system_time_is_valid())
    {
        error = system_time_sync(TIME_SYNC_TIMEOUT_MS);
        if (error != ESP_OK)
        {
            goto exit;
        }
    }

    error = device_identity_generate_frame_id(identity,
                                              frame_id,
                                              sizeof(frame_id));
    if (error != ESP_OK)
    {
        goto exit;
    }

    ESP_LOGI(TAG, "Frame created: %s", frame_id);

    frame = camera_capture_for_upload();
    if (frame == NULL)
    {
        error = ESP_FAIL;
        goto exit;
    }

    if (frame->buf == NULL || frame->len == 0U ||
        frame->format != PIXFORMAT_JPEG)
    {
        error = ESP_ERR_INVALID_STATE;
        goto exit;
    }

    image_width = (uint16_t)frame->width;
    image_height = (uint16_t)frame->height;
    image_length = frame->len;
    ESP_LOGI(TAG,
             "Captured image: %ux%u, %u bytes",
             image_width,
             image_height,
             (unsigned)image_length);

    image_copy = heap_caps_malloc(image_length,
                                  MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (image_copy == NULL)
    {
        image_copy = heap_caps_malloc(image_length, MALLOC_CAP_8BIT);
    }
    if (image_copy == NULL)
    {
        ESP_LOGE(TAG, "Unable to allocate %u-byte image upload buffer",
                 (unsigned)image_length);
        error = ESP_ERR_NO_MEM;
        goto exit;
    }

    memcpy(image_copy, frame->buf, image_length);
    camera_release(frame);
    frame = NULL;

    error = oss_build_image_url(identity->device_id,
                                frame_id,
                                image_url,
                                sizeof(image_url));
    if (error == ESP_OK)
    {
        image_task_set_state(IMAGE_TASK_STATE_UPLOAD_IMAGE);
        error = image_task_upload_jpeg_with_retry(image_url,
                                                  image_copy,
                                                  image_length);
    }

    if (error != ESP_OK)
    {
        goto exit;
    }

    image_task_beep_success_once();

    error = oss_build_manifest_url(identity->device_id,
                                   manifest_url,
                                   sizeof(manifest_url));
    if (error == ESP_OK)
    {
        error = image_task_build_manifest_json(identity,
                                               frame_id,
                                               image_width,
                                               image_height,
                                               manifest_json,
                                               sizeof(manifest_json));
    }
    if (error == ESP_OK)
    {
        image_task_set_state(IMAGE_TASK_STATE_UPLOAD_MANIFEST);
        error = image_task_put_json_with_retry(manifest_url,
                                               manifest_json,
                                               strlen(manifest_json));
    }
    if (error != ESP_OK)
    {
        goto exit;
    }

#if IMAGE_TRAINING_MODE_ENABLED
    ESP_LOGI(TAG,
             "Training mode: frame uploaded, skipping result polling: %s",
             frame_id);
    goto exit;
#endif

    error = oss_build_result_url(identity->device_id,
                                 frame_id,
                                 result_url,
                                 sizeof(result_url));
    if (error != ESP_OK)
    {
        goto exit;
    }

    image_task_set_state(IMAGE_TASK_STATE_WAIT_RESULT);
    error = image_task_wait_result(identity,
                                   frame_id,
                                   result_url,
                                   image_width,
                                   image_height,
                                   &process_result);
    if (error == ESP_OK)
    {
        image_task_set_state(IMAGE_TASK_STATE_RESULT_READY);
        image_task_beep_success_once();
        if (image_task_callback != NULL)
        {
            image_task_callback(&process_result, image_task_callback_user_data);
        }
    }
    else if (error == ESP_ERR_TIMEOUT)
    {
        image_task_set_state(IMAGE_TASK_STATE_TIMEOUT);
    }
    else
    {
        image_task_set_state(IMAGE_TASK_STATE_RESULT_FAILED);
    }

exit:
    if (frame != NULL)
    {
        camera_release(frame);
        frame = NULL;
    }

    if (image_copy != NULL)
    {
        heap_caps_free(image_copy);
        image_copy = NULL;
    }

    if (error != ESP_OK &&
        image_task_state != IMAGE_TASK_STATE_TIMEOUT &&
        image_task_state != IMAGE_TASK_STATE_RESULT_FAILED)
    {
        image_task_set_state(IMAGE_TASK_STATE_ERROR);
    }

    if (error != ESP_OK)
    {
        ESP_LOGE(TAG, "Image task failed: %s", esp_err_to_name(error));
    }

    image_task_set_state(IMAGE_TASK_STATE_IDLE);
    image_task_unlock();
    return error;
}
