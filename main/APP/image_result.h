#ifndef IMAGE_RESULT_H
#define IMAGE_RESULT_H

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "app_config.h"
#include "device_identity.h"

#define IMAGE_POINT_COUNT 4U

typedef enum
{
    IMAGE_POINT_PLAYER1_START = 0,
    IMAGE_POINT_PLAYER1_END,
    IMAGE_POINT_PLAYER2_START,
    IMAGE_POINT_PLAYER2_END,
} image_point_index_t;

typedef struct
{
    int32_t x;
    int32_t y;
} image_point_t;

typedef struct
{
    char device_id[DEVICE_ID_BUFFER_SIZE];
    char session_id[SESSION_ID_BUFFER_SIZE];
    char frame_id[FRAME_ID_BUFFER_SIZE];
    uint32_t sequence;
    uint16_t image_width;
    uint16_t image_height;
    image_point_t points[IMAGE_POINT_COUNT];
    float confidence;
    bool valid;
} image_process_result_t;

esp_err_t image_result_parse_and_validate(const char *json_text,
                                          size_t json_length,
                                          const device_identity_t *identity,
                                          const char *expected_frame_id,
                                          uint16_t expected_width,
                                          uint16_t expected_height,
                                          image_process_result_t *result);

#endif
