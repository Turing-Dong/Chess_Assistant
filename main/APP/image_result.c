#include "image_result.h"

#include <math.h>
#include <string.h>

#include "cJSON.h"
#include "esp_log.h"

static const char *TAG = "image_result";

static esp_err_t image_result_copy_string(cJSON *root,
                                          const char *name,
                                          char *output,
                                          size_t output_size)
{
    cJSON *item = cJSON_GetObjectItemCaseSensitive(root, name);
    if (!cJSON_IsString(item) || item->valuestring == NULL)
    {
        return ESP_ERR_INVALID_RESPONSE;
    }

    size_t length = strlen(item->valuestring);
    if (length >= output_size)
    {
        return ESP_ERR_INVALID_SIZE;
    }

    memcpy(output, item->valuestring, length + 1U);
    return ESP_OK;
}

static esp_err_t image_result_get_u32(cJSON *root,
                                      const char *name,
                                      uint32_t *value)
{
    cJSON *item = cJSON_GetObjectItemCaseSensitive(root, name);
    if (!cJSON_IsNumber(item) || item->valuedouble < 0.0)
    {
        return ESP_ERR_INVALID_RESPONSE;
    }

    *value = (uint32_t)item->valuedouble;
    return ESP_OK;
}

static esp_err_t image_result_get_u16(cJSON *root,
                                      const char *name,
                                      uint16_t *value)
{
    uint32_t temp = 0U;
    esp_err_t error = image_result_get_u32(root, name, &temp);
    if (error != ESP_OK)
    {
        return error;
    }

    if (temp > UINT16_MAX)
    {
        return ESP_ERR_INVALID_RESPONSE;
    }

    *value = (uint16_t)temp;
    return ESP_OK;
}

static esp_err_t image_result_get_point(cJSON *points,
                                        const char *name,
                                        uint16_t image_width,
                                        uint16_t image_height,
                                        image_point_t *point)
{
    cJSON *point_json = cJSON_GetObjectItemCaseSensitive(points, name);
    cJSON *x = cJSON_GetObjectItemCaseSensitive(point_json, "x");
    cJSON *y = cJSON_GetObjectItemCaseSensitive(point_json, "y");

    if (!cJSON_IsObject(point_json) ||
        !cJSON_IsNumber(x) ||
        !cJSON_IsNumber(y))
    {
        return ESP_ERR_INVALID_RESPONSE;
    }

    int32_t px = (int32_t)x->valuedouble;
    int32_t py = (int32_t)y->valuedouble;

    if (px < 0 || py < 0 ||
        px >= (int32_t)image_width ||
        py >= (int32_t)image_height)
    {
        return ESP_ERR_INVALID_RESPONSE;
    }

    point->x = px;
    point->y = py;

    return ESP_OK;
}

esp_err_t image_result_parse_and_validate(const char *json_text,
                                          size_t json_length,
                                          const device_identity_t *identity,
                                          const char *expected_frame_id,
                                          uint16_t expected_width,
                                          uint16_t expected_height,
                                          image_process_result_t *result)
{
    if (json_text == NULL || json_length == 0U ||
        identity == NULL || expected_frame_id == NULL || result == NULL)
    {
        return ESP_ERR_INVALID_ARG;
    }

    memset(result, 0, sizeof(*result));

    cJSON *root = cJSON_ParseWithLength(json_text, json_length);
    if (root == NULL)
    {
        return ESP_ERR_INVALID_RESPONSE;
    }

    esp_err_t error = ESP_OK;
    uint32_t protocol_version = 0U;
    char expected_session_id[SESSION_ID_BUFFER_SIZE] = {0};

    if (error == ESP_OK)
    {
        error = image_result_get_u32(root,
                                     "protocol_version",
                                     &protocol_version);
    }
    if (error == ESP_OK && protocol_version != 1U)
    {
        error = ESP_ERR_INVALID_RESPONSE;
    }
    if (error == ESP_OK)
    {
        error = image_result_copy_string(root,
                                         "device_id",
                                         result->device_id,
                                         sizeof(result->device_id));
    }
    if (error == ESP_OK)
    {
        error = image_result_copy_string(root,
                                         "session_id",
                                         result->session_id,
                                         sizeof(result->session_id));
    }
    if (error == ESP_OK)
    {
        error = image_result_copy_string(root,
                                         "frame_id",
                                         result->frame_id,
                                         sizeof(result->frame_id));
    }
    if (error == ESP_OK)
    {
        error = image_result_get_u32(root, "sequence", &result->sequence);
    }
    if (error == ESP_OK)
    {
        error = image_result_get_u16(root,
                                     "image_width",
                                     &result->image_width);
    }
    if (error == ESP_OK)
    {
        error = image_result_get_u16(root,
                                     "image_height",
                                     &result->image_height);
    }
    if (error == ESP_OK)
    {
        cJSON *confidence = cJSON_GetObjectItemCaseSensitive(root,
                                                             "confidence");
        if (!cJSON_IsNumber(confidence) || !isfinite(confidence->valuedouble))
        {
            error = ESP_ERR_INVALID_RESPONSE;
        }
        else
        {
            result->confidence = (float)confidence->valuedouble;
        }
    }

    cJSON *status = cJSON_GetObjectItemCaseSensitive(root, "status");
    if (error == ESP_OK &&
        (!cJSON_IsString(status) || status->valuestring == NULL))
    {
        error = ESP_ERR_INVALID_RESPONSE;
    }

    if (error == ESP_OK && strcmp(status->valuestring, "ok") != 0)
    {
        ESP_LOGW(TAG, "Result status is not ok: %s", status->valuestring);
        error = ESP_FAIL;
    }

    if (error == ESP_OK)
    {
        error = device_identity_format_session_id(identity,
                                                  expected_session_id,
                                                  sizeof(expected_session_id));
    }

    if (error == ESP_OK &&
        (strcmp(result->device_id, identity->device_id) != 0 ||
         strcmp(result->session_id, expected_session_id) != 0 ||
         strcmp(result->frame_id, expected_frame_id) != 0 ||
         result->sequence != identity->sequence ||
         result->image_width != expected_width ||
         result->image_height != expected_height ||
         result->confidence < IMAGE_RESULT_MIN_CONFIDENCE))
    {
        error = ESP_ERR_INVALID_RESPONSE;
    }

    cJSON *points = cJSON_GetObjectItemCaseSensitive(root, "points");
    if (error == ESP_OK && !cJSON_IsObject(points))
    {
        error = ESP_ERR_INVALID_RESPONSE;
    }
    if (error == ESP_OK)
    {
        error = image_result_get_point(points,
                                       "player1_start",
                                       result->image_width,
                                       result->image_height,
                                       &result->points[IMAGE_POINT_PLAYER1_START]);
    }
    if (error == ESP_OK)
    {
        error = image_result_get_point(points,
                                       "player1_end",
                                       result->image_width,
                                       result->image_height,
                                       &result->points[IMAGE_POINT_PLAYER1_END]);
    }
    if (error == ESP_OK)
    {
        error = image_result_get_point(points,
                                       "player2_start",
                                       result->image_width,
                                       result->image_height,
                                       &result->points[IMAGE_POINT_PLAYER2_START]);
    }
    if (error == ESP_OK)
    {
        error = image_result_get_point(points,
                                       "player2_end",
                                       result->image_width,
                                       result->image_height,
                                       &result->points[IMAGE_POINT_PLAYER2_END]);
    }

    result->valid = (error == ESP_OK);

    if (error == ESP_OK)
    {
        ESP_LOGI(TAG,
                 "Result ok: P1_START=(%ld,%ld), P1_END=(%ld,%ld), P2_START=(%ld,%ld), P2_END=(%ld,%ld), confidence=%.3f",
                 (long)result->points[IMAGE_POINT_PLAYER1_START].x,
                 (long)result->points[IMAGE_POINT_PLAYER1_START].y,
                 (long)result->points[IMAGE_POINT_PLAYER1_END].x,
                 (long)result->points[IMAGE_POINT_PLAYER1_END].y,
                 (long)result->points[IMAGE_POINT_PLAYER2_START].x,
                 (long)result->points[IMAGE_POINT_PLAYER2_START].y,
                 (long)result->points[IMAGE_POINT_PLAYER2_END].x,
                 (long)result->points[IMAGE_POINT_PLAYER2_END].y,
                 (double)result->confidence);
    }
    else
    {
        ESP_LOGW(TAG, "Result validation failed: %s", esp_err_to_name(error));
    }

    cJSON_Delete(root);
    return error;
}
