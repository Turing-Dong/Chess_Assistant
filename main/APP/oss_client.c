#include "oss_client.h"

#include <limits.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "app_config.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_log.h"

static const char *TAG = "oss_client";

typedef struct
{
    char *buffer;
    size_t capacity;
    size_t length;
    bool overflow;
} oss_response_buffer_t;

static esp_err_t oss_snprintf(char *buffer,
                              size_t buffer_size,
                              const char *format,
                              const char *first,
                              const char *second)
{
    if (buffer == NULL || buffer_size == 0U ||
        format == NULL || first == NULL)
    {
        return ESP_ERR_INVALID_ARG;
    }

    int length = snprintf(buffer, buffer_size, format, first, second);
    if (length < 0 || (size_t)length >= buffer_size)
    {
        return ESP_ERR_INVALID_SIZE;
    }

    return ESP_OK;
}

esp_err_t oss_build_image_key(const char *device_id,
                              const char *frame_id,
                              char *key,
                              size_t key_size)
{
    return oss_snprintf(key,
                        key_size,
                        "devices/%s/requests/%s.jpg",
                        device_id,
                        frame_id);
}

esp_err_t oss_build_result_key(const char *device_id,
                               const char *frame_id,
                               char *key,
                               size_t key_size)
{
    return oss_snprintf(key,
                        key_size,
                        "devices/%s/results/%s.json",
                        device_id,
                        frame_id);
}

esp_err_t oss_build_image_url(const char *device_id,
                              const char *frame_id,
                              char *url,
                              size_t url_size)
{
    return oss_snprintf(url,
                        url_size,
                        OSS_BASE_URL "/devices/%s/requests/%s.jpg",
                        device_id,
                        frame_id);
}

esp_err_t oss_build_manifest_url(const char *device_id,
                                 char *url,
                                 size_t url_size)
{
    if (device_id == NULL)
    {
        return ESP_ERR_INVALID_ARG;
    }

    int length = snprintf(url,
                          url_size,
                          OSS_BASE_URL "/devices/%s/requests/latest.json",
                          device_id);
    if (length < 0 || (size_t)length >= url_size)
    {
        return ESP_ERR_INVALID_SIZE;
    }

    return ESP_OK;
}

esp_err_t oss_build_result_url(const char *device_id,
                               const char *frame_id,
                               char *url,
                               size_t url_size)
{
    return oss_snprintf(url,
                        url_size,
                        OSS_BASE_URL "/devices/%s/results/%s.json",
                        device_id,
                        frame_id);
}

esp_err_t oss_build_heartbeat_url(const char *device_id,
                                  char *url,
                                  size_t url_size)
{
    if (device_id == NULL)
    {
        return ESP_ERR_INVALID_ARG;
    }

    int length = snprintf(url,
                          url_size,
                          OSS_BASE_URL "/devices/%s/status/heartbeat.json",
                          device_id);
    if (length < 0 || (size_t)length >= url_size)
    {
        return ESP_ERR_INVALID_SIZE;
    }

    return ESP_OK;
}

static esp_err_t oss_http_event_handler(esp_http_client_event_t *event)
{
    if (event == NULL || event->event_id != HTTP_EVENT_ON_DATA)
    {
        return ESP_OK;
    }

    oss_response_buffer_t *response =
        (oss_response_buffer_t *)event->user_data;
    if (response == NULL || response->buffer == NULL ||
        response->capacity == 0U)
    {
        return ESP_OK;
    }

    if ((size_t)event->data_len >= response->capacity - response->length)
    {
        response->overflow = true;
        return ESP_FAIL;
    }

    memcpy(response->buffer + response->length,
           event->data,
           (size_t)event->data_len);
    response->length += (size_t)event->data_len;
    response->buffer[response->length] = '\0';

    return ESP_OK;
}

static esp_err_t oss_put_buffer(const char *url,
                                const char *content_type,
                                const uint8_t *data,
                                size_t data_length,
                                uint32_t timeout_ms)
{
    if (url == NULL || content_type == NULL ||
        data == NULL || data_length == 0U ||
        data_length > INT_MAX)
    {
        return ESP_ERR_INVALID_ARG;
    }

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_PUT,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = (int)timeout_ms,
        .buffer_size = 2048,
        .buffer_size_tx = 4096,
        .keep_alive_enable = false,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL)
    {
        return ESP_ERR_NO_MEM;
    }

    esp_err_t error = esp_http_client_set_header(client,
                                                 "Content-Type",
                                                 content_type);
    if (error == ESP_OK)
    {
        error = esp_http_client_set_header(client,
                                           "Cache-Control",
                                           "no-cache");
    }

    if (error == ESP_OK)
    {
        error = esp_http_client_open(client, (int)data_length);
    }

    size_t written = 0U;
    while (error == ESP_OK && written < data_length)
    {
        size_t chunk = data_length - written;
        if (chunk > 4096U)
        {
            chunk = 4096U;
        }

        int write_result = esp_http_client_write(
            client,
            (const char *)(data + written),
            (int)chunk);
        if (write_result <= 0)
        {
            error = ESP_FAIL;
            break;
        }

        written += (size_t)write_result;
    }

    int status_code = 0;
    if (error == ESP_OK)
    {
        int64_t header_length = esp_http_client_fetch_headers(client);
        if (header_length < 0)
        {
            error = ESP_FAIL;
        }
        status_code = esp_http_client_get_status_code(client);
    }

    if (error == ESP_OK && status_code != 200)
    {
        ESP_LOGE(TAG, "PUT failed, HTTP status=%d", status_code);
        error = ESP_FAIL;
    }
    else if (error == ESP_OK)
    {
        ESP_LOGI(TAG, "PUT complete, HTTP status=%d, bytes=%u",
                 status_code,
                 (unsigned)data_length);
    }

    esp_http_client_cleanup(client);
    return error;
}

esp_err_t oss_upload_jpeg(const char *url,
                          const uint8_t *image_data,
                          size_t image_length)
{
    return oss_put_buffer(url,
                          "image/jpeg",
                          image_data,
                          image_length,
                          OSS_UPLOAD_TIMEOUT_MS);
}

esp_err_t oss_put_json(const char *url,
                       const char *json_text,
                       size_t json_length)
{
    return oss_put_buffer(url,
                          "application/json",
                          (const uint8_t *)json_text,
                          json_length,
                          OSS_UPLOAD_TIMEOUT_MS);
}

esp_err_t oss_get_json(const char *url,
                       char *json_buffer,
                       size_t json_buffer_size,
                       size_t *json_length,
                       int *http_status)
{
    if (url == NULL || json_buffer == NULL || json_buffer_size == 0U ||
        json_length == NULL || http_status == NULL)
    {
        return ESP_ERR_INVALID_ARG;
    }

    json_buffer[0] = '\0';
    *json_length = 0U;
    *http_status = 0;

    oss_response_buffer_t response = {
        .buffer = json_buffer,
        .capacity = json_buffer_size,
        .length = 0U,
        .overflow = false,
    };

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .event_handler = oss_http_event_handler,
        .user_data = &response,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = OSS_DOWNLOAD_TIMEOUT_MS,
        .buffer_size = 2048,
        .buffer_size_tx = 1024,
        .keep_alive_enable = false,
    };

    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL)
    {
        return ESP_ERR_NO_MEM;
    }

    esp_err_t error = esp_http_client_set_header(client,
                                                 "Cache-Control",
                                                 "no-cache");
    if (error == ESP_OK)
    {
        error = esp_http_client_perform(client);
    }

    *http_status = esp_http_client_get_status_code(client);
    *json_length = response.length;

    if (response.overflow)
    {
        error = ESP_ERR_INVALID_SIZE;
    }

    if (error == ESP_OK)
    {
        ESP_LOGI(TAG, "GET complete, HTTP status=%d, bytes=%u",
                 *http_status,
                 (unsigned)*json_length);
    }

    esp_http_client_cleanup(client);
    return error;
}
